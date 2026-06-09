from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field

from src_new.config import LOGGER
from src_new.lift.adapters.environment import ExecutionEnvironment
from src_new.lift.eval.worker_judger import WorkerJudgerPairFactory
from src_new.lift.eval.phase import execute_phase, execute_phase_batch
from src_new.lift.pipeline.run_options import RunOptions
from src_new.lift.policies.artifact import ArtifactPolicy, WarmupThenUpdatePolicy
from src_new.lift.runtime.delta_ref import DeltaRef
from src_new.lift.runtime.suite_run_resources import SuiteRunResources
from src_new.models import PhaseRun, SuiteTask
from src_new.utils import outcome_workspace


class LoadState(Enum):
    """hold-out 题评测时的产物加载状态。"""

    BEFORE_LOAD = "before_load"
    AFTER_LOAD = "after_load"


class RunContext(BaseModel):
    """单次 suite 评测的不可变运行坐标，由 pipeline 构造并传给 adapter 各方法。"""

    model_config = ConfigDict(frozen=True)

    run_id: str = Field(description="评测批次 ID（如 evobench-runid-hello-full）")
    repeat_index: int = Field(description="当前 repeat 序号（0 起，对应 --repeat 第几轮）")
    suite_path: Path = Field(description="suite JSON 文件路径")
    category_name: str = Field(description="场景分类名（来自 Suite.category）")
    suite_name: str = Field(description="suite 名称（来自 Suite.name）")


class AgentRuntimeAdapter(ABC):
    """Agent execution runtime base with template methods for warmup and hold-out."""

    def __init__(self, options: RunOptions | None = None) -> None:
        self._options = options or RunOptions()

    async def create_suite_run_resources(self, ctx: RunContext) -> SuiteRunResources:
        """为当前 suite 创建资源登记簿，供本 suite 内 warmup / hold-out 共用。

        pipeline 在每个 ``(repeat_index, suite)`` 开始时调用一次；adapter 在后续
        ``produce_delta`` / ``run_before_load`` / ``run_after_load`` 中通过
        ``resources.track()`` 登记容器，并在 ``produce_delta`` 后写入 ``delta``。
        suite 结束时由 pipeline 调用 ``resources.cleanup()`` 统一释放。

        子类可覆盖以注入额外状态；默认仅按 ``ctx`` 构造空的 ``SuiteRunResources``。
        """
        return SuiteRunResources(
            run_id=ctx.run_id,
            repeat_index=ctx.repeat_index,
            suite_name=ctx.suite_name,
        )

    async def produce_delta(
        self,
        resources: SuiteRunResources,
        policy: ArtifactPolicy,
        warmup_tasks: list[SuiteTask],
        ctx: RunContext,
    ) -> DeltaRef:
        if not isinstance(policy, WarmupThenUpdatePolicy):
            raise TypeError(f"Unsupported artifact policy: {type(policy)!r}")
        if not warmup_tasks:
            raise ValueError("WarmupThenUpdatePolicy requires warmup tasks")

        workspace = self.warmup_workspace(ctx)
        env = await self.start_warmup_environment(ctx, resources, workspace)
        resources.track(env.disposable)
        try:
            factory = self.worker_judger_factory(
                env, ctx, phase="warmup", workspace_dir=workspace
            )
            await execute_phase_batch(
                tasks=warmup_tasks,
                run_id=ctx.run_id,
                workspace_dir=workspace,
                factory=factory,
                parallel=self._options.parallel,
                phase="warmup",
                is_final_task=False,
                log_label="warmup",
            )
            await self.apply_evolve(env, ctx)
            delta = await self.materialize_delta(env, ctx)
            resources.delta = delta
            LOGGER.info("Delta materialized: %s", delta.image_tag)
            return delta
        finally:
            await env.disposable.cleanup()

    async def run_before_load(
        self,
        task: SuiteTask,
        resources: SuiteRunResources,
        ctx: RunContext,
        *,
        phase: str = "baseline",
    ) -> PhaseRun:
        return await self._run_holdout(
            task=task,
            resources=resources,
            ctx=ctx,
            image=self.baseline_image(ctx),
            phase=phase,
            is_evolve_turn=False,
            log_label="before-load",
        )

    async def run_after_load(
        self,
        task: SuiteTask,
        resources: SuiteRunResources,
        delta: DeltaRef,
        ctx: RunContext,
    ) -> PhaseRun:
        return await self._run_holdout(
            task=task,
            resources=resources,
            ctx=ctx,
            image=delta.image_tag,
            phase="evolved",
            is_evolve_turn=True,
            log_label="after-load",
        )

    async def _run_holdout(
        self,
        *,
        task: SuiteTask,
        resources: SuiteRunResources,
        ctx: RunContext,
        image: str,
        phase: str,
        is_evolve_turn: bool,
        log_label: str,
    ) -> PhaseRun:
        workspace = self.holdout_workspace(ctx, task, phase)
        seed_workspace = phase in {"baseline", "evolved"}
        env = await self.start_holdout_environment(
            ctx,
            resources,
            task,
            workspace,
            image=image,
            seed_workspace=seed_workspace,
        )
        resources.track(env.disposable)
        try:
            factory = self.worker_judger_factory(
                env, ctx, phase=phase, workspace_dir=workspace
            )
            return await execute_phase(
                task=task,
                run_id=ctx.run_id,
                workspace_dir=workspace,
                factory=factory,
                phase=phase,
                is_evolve_turn=is_evolve_turn,
                is_final_task=True,
                log_label=log_label,
            )
        finally:
            await env.disposable.cleanup()

    def warmup_workspace(self, ctx: RunContext) -> Path:
        return outcome_workspace(
            ctx.run_id, ctx.repeat_index, "warmup", ctx.category_name
        )

    def holdout_workspace(self, ctx: RunContext, task: SuiteTask, phase: str) -> Path:
        base = outcome_workspace(
            ctx.run_id, ctx.repeat_index, phase, ctx.category_name
        )
        path = base / task.name
        path.mkdir(parents=True, exist_ok=True)
        return path

    @abstractmethod
    def worker_judger_factory(
        self,
        env: ExecutionEnvironment,
        ctx: RunContext,
        *,
        phase: str,
        workspace_dir: Path,
    ) -> WorkerJudgerPairFactory:
        """Return a ``WorkerJudgerPairFactory`` bound to ``env`` for ``run_task``."""

    @abstractmethod
    async def start_warmup_environment(
        self,
        ctx: RunContext,
        resources: SuiteRunResources,
        workspace_dir: Path,
    ) -> ExecutionEnvironment:
        """Start the runtime used for warmup tasks."""

    @abstractmethod
    async def start_holdout_environment(
        self,
        ctx: RunContext,
        resources: SuiteRunResources,
        task: SuiteTask,
        workspace_dir: Path,
        *,
        image: str,
        seed_workspace: bool,
    ) -> ExecutionEnvironment:
        """Start the runtime used for one hold-out task."""

    @abstractmethod
    async def apply_evolve(self, env: ExecutionEnvironment, ctx: RunContext) -> None:
        """Trigger artifact update after warmup tasks complete."""

    @abstractmethod
    async def materialize_delta(
        self, env: ExecutionEnvironment, ctx: RunContext
    ) -> DeltaRef:
        """Persist warmup evolve state into a loadable delta."""

    @abstractmethod
    def baseline_image(self, ctx: RunContext) -> str:
        """Runtime image or identifier for before-load hold-out."""
