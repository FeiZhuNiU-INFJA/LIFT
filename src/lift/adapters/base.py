"""AgentRuntimeAdapter 模板方法与 SuiteRunContext（LIFT 适配层核心）。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field

from src.config import LOGGER
from src.lift.adapters.environment import ExecutionEnvironment
from src.lift.eval.stage import HoldoutLoadState, SuiteRunPhase
from src.lift.eval.task_exec import execute_task, execute_tasks
from src.lift.eval.worker_judger import WorkerJudgerPairFactory
from src.lift.pipeline.run_options import RunOptions
from src.lift.policies.artifact import ArtifactPolicy, WarmupThenUpdatePolicy
from src.lift.runtime.delta_ref import DeltaRef
from src.lift.runtime.suite_run_resources import SuiteRunResources
from src.models import PhaseRun, SuiteTask
from src.utils import outcome_workspace


class SuiteRunContext(BaseModel):
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
        """初始化 adapter，``options`` 为 None 时使用默认 ``RunOptions``。"""
        self._options = options or RunOptions()  # CLI 解析后的运行时配置

    async def create_suite_run_resources(self, ctx: SuiteRunContext) -> SuiteRunResources:
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
        ctx: SuiteRunContext,
    ) -> DeltaRef:
        """执行 warmup 题 → evolve → 物化 delta 的完整产物生产流程。"""
        if not isinstance(policy, WarmupThenUpdatePolicy):
            raise TypeError(f"Unsupported artifact policy: {type(policy)!r}")
        if not warmup_tasks:
            raise ValueError("WarmupThenUpdatePolicy requires warmup tasks")

        run_phase = SuiteRunPhase.warmup()
        workspace = self.warmup_workspace(ctx)
        env = await self.start_warmup_environment(ctx, resources, workspace)
        resources.track(env.disposable)  # suite 级登记；produce_delta 结束后再统一 cleanup
        try:
            factory = self.worker_judger_factory(
                env, ctx, run_phase=run_phase, workspace_dir=workspace
            )
            await execute_tasks(
                tasks=warmup_tasks,
                run_id=ctx.run_id,
                workspace_dir=workspace,
                factory=factory,
                run_phase=run_phase,
                parallel=self._options.warmup_container_policy.tasks_parallel,  # 由 warmup_container_policy 决定
                max_concurrent=self._options.max_concurrent_tasks,
                max_conversation_turns=self._options.max_conversation_turns,
                on_task_done=lambda task, _result: self.evolve_after_task(env, task, ctx),  # 每题完成钩子；默认 no-op
            )
            await self.evolve_after_warmup(env, ctx)  # 所有 warmup 完成钩子：OpenClaw = learn review
            delta = await self.materialize_delta(env, ctx)  # 须在容器仍存活时 commit
            resources.delta = delta
            LOGGER.info("Delta materialized: %s", delta.image_tag)
            return delta
        finally:
            # warmup 容器使命结束；hold-out 会起新容器加载 delta 镜像
            await env.disposable.cleanup()

    async def run_before_load(
        self,
        task: SuiteTask,
        resources: SuiteRunResources,
        ctx: SuiteRunContext,
    ) -> PhaseRun:
        """Hold-out before-load 对照：干净 baseline 镜像上跑单题。"""
        return await self._run_holdout(
            task=task,
            resources=resources,
            ctx=ctx,
            image=self.baseline_image(ctx),
            load_state=HoldoutLoadState.BASELINE,
        )

    async def run_after_load(
        self,
        task: SuiteTask,
        resources: SuiteRunResources,
        delta: DeltaRef,
        ctx: SuiteRunContext,
    ) -> PhaseRun:
        """Hold-out after-load 对照：加载 warmup delta 镜像后跑单题。"""
        return await self._run_holdout(
            task=task,
            resources=resources,
            ctx=ctx,
            image=delta.image_tag,
            load_state=HoldoutLoadState.EVOLVED,
        )

    async def _run_holdout(
        self,
        *,
        task: SuiteTask,
        resources: SuiteRunResources,
        ctx: SuiteRunContext,
        image: str,
        load_state: HoldoutLoadState,
    ) -> PhaseRun:
        """Hold-out 单题执行内核：起容器 → factory → execute_task。"""
        run_phase = SuiteRunPhase.holdout(load_state)
        workspace = self.holdout_workspace(ctx, task, load_state)
        # Hold-out 每题新 workspace：seed 由 runtime 解释（OpenClaw=预置人设跳过 BOOTSTRAP）
        env = await self.start_holdout_environment(
            ctx,
            resources,
            task,
            workspace,
            image=image,
            seed_workspace=True,
            load_state=load_state,  # runtime 据此区分 baseline/evolved（如群体记忆是否注入）
        )
        resources.track(env.disposable)  # hold-out 每题独立容器，题末 finally 立刻 rm
        try:
            factory = self.worker_judger_factory(
                env, ctx, run_phase=run_phase, workspace_dir=workspace
            )
            return await execute_task(
                task=task,
                run_id=ctx.run_id,
                workspace_dir=workspace,
                factory=factory,
                run_phase=run_phase,
                max_conversation_turns=self._options.max_conversation_turns,
            )
        finally:
            await env.disposable.cleanup()

    def warmup_workspace(self, ctx: SuiteRunContext) -> Path:
        """warmup 阶段 outcome 目录（多题共享同一 workspace）。"""
        return outcome_workspace(
            ctx.run_id, ctx.repeat_index, SuiteRunPhase.warmup().workspace_segment, ctx.category_name
        )

    def holdout_workspace(
        self, ctx: SuiteRunContext, task: SuiteTask, load_state: HoldoutLoadState
    ) -> Path:
        """Hold-out 单题隔离 workspace（``.../holdout/{task.name}/``）。"""
        base = outcome_workspace(
            ctx.run_id,
            ctx.repeat_index,
            SuiteRunPhase.holdout(load_state).workspace_segment,
            ctx.category_name,
        )
        path = base / task.name
        path.mkdir(parents=True, exist_ok=True)
        return path

    @abstractmethod
    def worker_judger_factory(
        self,
        env: ExecutionEnvironment,
        ctx: SuiteRunContext,
        *,
        run_phase: SuiteRunPhase,
        workspace_dir: Path,
    ) -> WorkerJudgerPairFactory:
        """Return a ``WorkerJudgerPairFactory`` bound to ``env`` for ``run_task``."""

    @abstractmethod
    async def start_warmup_environment(
        self,
        ctx: SuiteRunContext,
        resources: SuiteRunResources,
        workspace_dir: Path,
    ) -> ExecutionEnvironment:
        """Start the runtime used for warmup tasks."""

    @abstractmethod
    async def start_holdout_environment(
        self,
        ctx: SuiteRunContext,
        resources: SuiteRunResources,
        task: SuiteTask,
        workspace_dir: Path,
        *,
        image: str,
        seed_workspace: bool,
        load_state: HoldoutLoadState,
    ) -> ExecutionEnvironment:
        """Start the runtime used for one hold-out task.

        ``seed_workspace``: 是否在挂载 ``workspace_dir`` 前写入「评测用初始工作区」。
        框架在 hold-out 传 ``True``、warmup 传 ``False``；具体文件与是否 no-op 由
        runtime 的 ``start_container`` 实现决定（OpenClaw 复制 IDENTITY/USER/SOUL 并删
        BOOTSTRAP；其他 agent 可注入项目指令、规则文件，或忽略该标志）。

        ``load_state``: 当前 hold-out 加载态（``BASELINE`` / ``EVOLVED``）。多数
        runtime 通过 ``image`` 即可区分（base vs delta），但群体记忆等不依赖镜像
        commit 的策略需要这个显式信号决定是否注入"已学经验"配置。
        """

    async def evolve_after_task(
        self,
        env: ExecutionEnvironment,
        task: SuiteTask,
        ctx: SuiteRunContext,
    ) -> None:
        """每道 warmup 题完成后立刻调用的 evolve 钩子；默认 no-op。

        典型用法：在群体记忆 / 多容器场景下，每题独立容器跑完即写一次外部记忆 flush。
        在共享容器（``SERIAL_SINGLE`` / ``PARALLEL_SINGLE``）模式下也可被覆写为
        增量 evolve，但要注意 ``PARALLEL_SINGLE`` 下多次并发调用同一容器的
        evolve 操作可能产生竞态——具体由子类自行评估。
        """
        _ = (env, task, ctx)
        return None

    @abstractmethod
    async def evolve_after_warmup(
        self, env: ExecutionEnvironment, ctx: SuiteRunContext
    ) -> None:
        """所有 warmup 题完成后调用的 evolve 钩子（``produce_delta`` 主路径）。

        OpenClaw 在此触发 ``openclaw learn review``；群体记忆 runtime 通常 no-op。
        """

    @abstractmethod
    async def materialize_delta(
        self, env: ExecutionEnvironment, ctx: SuiteRunContext
    ) -> DeltaRef:
        """Persist warmup evolve state into a loadable delta."""

    @abstractmethod
    def baseline_image(self, ctx: SuiteRunContext) -> str:
        """Runtime image or identifier for before-load hold-out."""
