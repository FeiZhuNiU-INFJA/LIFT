"""AgentRuntimeAdapter 模板方法与 SuiteRunContext（LIFT 适配层核心）。"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field

from src.config import LOGGER
from src.lift.adapters.environment import ExecutionEnvironment
from src.lift.eval.stage import HoldoutLoadState, SuiteRunPhase
from src.lift.eval.task_exec import exc_summary, execute_task, execute_tasks
from src.lift.eval.worker_judger import WorkerJudgerPairFactory
from src.lift.pipeline.run_options import RunOptions
from src.lift.policies.artifact import ArtifactPolicy, WarmupThenUpdatePolicy
from src.lift.runtime.delta_ref import DeltaRef
from src.lift.runtime.suite_run_resources import SuiteRunResources
from src.lift.status import events as status_events
from src.models import PhaseRun, SuiteTask
from src.utils import outcome_workspace, stage_task_materials


def _truncate(text: str, limit: int = 8000) -> str:
    """截断超长文本，保护 SSE / snapshot 体量（对话内容可能很长）。"""
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


class SuiteRunContext(BaseModel):
    """单次 suite 评测的不可变运行坐标，由 pipeline 构造并传给 adapter 各方法。"""

    model_config = ConfigDict(frozen=True)

    run_id: str = Field(description="评测批次 ID（如 lift-runid-hello-full）")
    repeat_index: int = Field(description="当前 repeat 序号（0 起，对应 --repeat 第几轮）")
    suite_index: int = Field(description="当前 suite 在 repeat 内的索引（0 起，与 status 事件维度一致）")
    suite_path: Path = Field(description="suite JSON 文件路径")
    category_name: str = Field(description="场景分类名（来自 Suite.category）")
    suite_name: str = Field(description="suite 名称（来自 Suite.name）")


class AgentRuntimeAdapter(ABC):
    """Agent execution runtime base with template methods for warmup and holdout."""

    _EVOLVE_HOOK_ATTEMPTS = 3

    def __init__(self, options: RunOptions | None = None) -> None:
        """初始化 adapter，``options`` 为 None 时使用默认 ``RunOptions``。"""
        self._options = options or RunOptions()  # CLI 解析后的运行时配置

    async def create_suite_run_resources(self, ctx: SuiteRunContext) -> SuiteRunResources:
        """为当前 suite 创建资源登记簿，供本 suite 内 warmup / holdout 共用。

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
        # warmup 多题共享同一 workspace：把每题 materials 按原目录名复制进去，
        # 使 query 中的 ``qN_materials/`` 相对引用可在容器 /workspace/task 下命中
        for task in warmup_tasks:
            stage_task_materials(workspace, task.requirements.material_dir)
        env = await self.start_warmup_environment(ctx, resources, workspace)
        resources.track(env.disposable)  # suite 级登记；produce_delta 结束后再统一 cleanup

        def _emit_warmup_task(task: SuiteTask, status: str, detail: str | None) -> None:
            """把 execute_tasks 的单题状态回调转发到 status 事件总线。

            走 ``kind=warmup_task`` 后端 state 会更新 ``SuiteNode.warmup_tasks[name]``，
            前端 hover 整个 ``w`` 单元格能看到逐题状态。
            """
            status_events.emit_stage(
                kind="warmup_task",
                status=status,
                run_id=ctx.run_id,
                repeat_index=ctx.repeat_index,
                suite_index=ctx.suite_index,
                suite_name=ctx.suite_name,
                task_name=task.name,
                detail=detail,
            )

        try:
            factory = self.worker_judger_factory(
                env, ctx, run_phase=run_phase, workspace_dir=workspace
            )
            task_results = await execute_tasks(
                tasks=warmup_tasks,
                run_id=ctx.run_id,
                workspace_dir=workspace,
                factory=factory,
                run_phase=run_phase,
                parallel=self._options.warmup_container_policy.tasks_parallel,  # 由 warmup_container_policy 决定
                max_concurrent=self._options.max_concurrent_tasks,
                max_conversation_turns=self._options.max_conversation_turns,
                on_task_done=lambda task, result: self._run_evolve_after_task_with_retry(
                    env, task, result, ctx, on_task_status=_emit_warmup_task
                ),  # 每题完成钩子；默认 no-op；hook 自己按 3 次重试
                on_task_status=_emit_warmup_task,  # 题级状态 → dashboard tooltip
                retry_each=True,  # 单题异常原地重试一次（judge fail 不算失败）
                tasks_isolated=True,  # warmup 题间隔离：单题最终失败不取消兄弟题
            )
            task_errors = [r for r in task_results if isinstance(r, BaseException)]
            if task_errors:
                raise task_errors[0]
            await self._run_evolve_after_warmup_with_retry(env, ctx)  # 所有 warmup 完成钩子：OpenClaw = learn review
            delta = await self.materialize_delta(env, ctx)  # 须在容器仍存活时 commit
            resources.delta = delta
            LOGGER.info("Delta materialized: %s", delta.image_tag)
            return delta
        finally:
            # warmup 容器使命结束；holdout 会起新容器加载 delta 镜像
            await env.disposable.cleanup()

    async def _run_evolve_after_task_with_retry(
        self,
        env: ExecutionEnvironment,
        task: SuiteTask,
        result: PhaseRun,
        ctx: SuiteRunContext,
        *,
        on_task_status,
    ) -> None:
        """Run ``evolve_after_task`` with a fixed retry budget.

        Hook retry is deliberately separate from task retry: a finished task should not
        be re-run just because the evolve side effect had a transient failure.
        """
        attempts = self._EVOLVE_HOOK_ATTEMPTS
        last_exc: BaseException | None = None
        for attempt in range(1, attempts + 1):
            try:
                await self.evolve_after_task(env, task, result, ctx)
                return
            except BaseException as exc:  # noqa: BLE001
                last_exc = exc
                detail = f"evolve_after_task retry {attempt}/{attempts}: {exc_summary(exc)}"
                if attempt >= attempts:
                    on_task_status(task, "failed", detail)
                    LOGGER.error(
                        "evolve_after_task failed after %d attempts suite=%s task=%s: %r",
                        attempts, ctx.suite_name, task.name, exc,
                    )
                    raise RuntimeError(detail) from exc
                LOGGER.warning(
                    "evolve_after_task failed suite=%s task=%s attempt=%d/%d: %r; retrying",
                    ctx.suite_name, task.name, attempt, attempts, exc,
                )
                on_task_status(task, "retrying", detail)
                await asyncio.sleep(min(attempt, 3))
        raise last_exc  # type: ignore[misc]

    async def _run_evolve_after_warmup_with_retry(
        self, env: ExecutionEnvironment, ctx: SuiteRunContext
    ) -> None:
        """Run ``evolve_after_warmup`` with a fixed retry budget."""
        attempts = self._EVOLVE_HOOK_ATTEMPTS
        last_exc: BaseException | None = None
        for attempt in range(1, attempts + 1):
            try:
                await self.evolve_after_warmup(env, ctx)
                return
            except BaseException as exc:  # noqa: BLE001
                last_exc = exc
                detail = f"evolve_after_warmup retry {attempt}/{attempts}: {exc_summary(exc)}"
                if attempt >= attempts:
                    status_events.emit_stage(
                        kind="warmup",
                        status="failed",
                        run_id=ctx.run_id,
                        repeat_index=ctx.repeat_index,
                        suite_index=ctx.suite_index,
                        suite_name=ctx.suite_name,
                        detail=detail,
                    )
                    LOGGER.error(
                        "evolve_after_warmup failed after %d attempts suite=%s: %r",
                        attempts, ctx.suite_name, exc,
                    )
                    break
                status_events.emit_stage(
                    kind="warmup",
                    status="retrying",
                    run_id=ctx.run_id,
                    repeat_index=ctx.repeat_index,
                    suite_index=ctx.suite_index,
                    suite_name=ctx.suite_name,
                    detail=detail,
                )
                LOGGER.warning(
                    "evolve_after_warmup failed suite=%s attempt=%d/%d: %r; retrying",
                    ctx.suite_name, attempt, attempts, exc,
                )
                await asyncio.sleep(min(attempt, 3))
        raise last_exc  # type: ignore[misc]

    async def run_before_load(
        self,
        task: SuiteTask,
        resources: SuiteRunResources,
        ctx: SuiteRunContext,
    ) -> PhaseRun:
        """Holdout before-load 对照：干净 baseline 镜像上跑单题。"""
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
        """Holdout after-load 对照：加载 warmup delta 镜像后跑单题。"""
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
        """Holdout 单题执行内核：起容器 → factory → execute_task。"""
        run_phase = SuiteRunPhase.holdout(load_state)
        workspace = self.holdout_workspace(ctx, task, load_state)
        # 把本题 materials 按原目录名复制进 workspace（容器内 /workspace/task），
        # 使 query 中的 ``qN_materials/`` 相对引用命中
        stage_task_materials(workspace, task.requirements.material_dir)
        # Holdout 每题新 workspace：seed 由 runtime 解释（OpenClaw=预置人设跳过 BOOTSTRAP）
        env = await self.start_holdout_environment(
            ctx,
            resources,
            task,
            workspace,
            image=image,
            seed_workspace=True,
            load_state=load_state,  # runtime 据此区分 baseline/evolved（如群体记忆是否注入）
        )
        resources.track(env.disposable)  # holdout 每题独立容器，题末 finally 立刻 rm
        try:
            factory = self.worker_judger_factory(
                env, ctx, run_phase=run_phase, workspace_dir=workspace
            )

            # 每轮 work↔judge 完成后 emit 对话事件，驱动 dashboard 对话视图。
            # load_state.value（"baseline" / "evolved"）即 phase 坐标。
            def _on_turn(turn_idx, work_prompt, work_result, judge_result):  # noqa: ANN001
                status_events.emit_dialogue_turn(
                    run_id=ctx.run_id,
                    repeat_index=ctx.repeat_index,
                    suite_index=ctx.suite_index,
                    suite_name=ctx.suite_name,
                    task_name=task.name,
                    phase=load_state.value,
                    turn_index=turn_idx,
                    work_prompt=_truncate(work_prompt),
                    work_result=_truncate(work_result),
                    judge_success=judge_result.success,
                    judge_score=judge_result.score,
                    judge_reason=_truncate(judge_result.reason, 4000),
                )

            result = await execute_task(
                task=task,
                run_id=ctx.run_id,
                workspace_dir=workspace,
                factory=factory,
                run_phase=run_phase,
                max_conversation_turns=self._options.max_conversation_turns,
                on_turn=_on_turn,
            )
            # adapter 自报 work agent tool 调用次数；OpenClaw 走 trajectory.jsonl，
            # 其他 runtime 默认 None（dashboard 显示 "—"）。失败仅 warning，绝不
            # 拖垮 holdout。
            try:
                tool_calls = await self.count_tool_calls(env, task, result, ctx)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning(
                    "count_tool_calls failed (task=%s phase=%s): %r",
                    task.name, load_state.value, exc,
                )
                tool_calls = None
            if tool_calls is not None:
                result.tool_calls = tool_calls
            return result
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
        """Holdout 单题隔离 workspace（``.../holdout/{task.name}/``）。"""
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
        """Start the runtime used for one holdout task.

        ``seed_workspace``: 是否在挂载 ``workspace_dir`` 前写入「评测用初始工作区」。
        框架在 holdout 与 warmup 阶段均传 ``True``——避免 agent 在每次新建容器时重跑
        首次上线 onboarding（问名字 / emoji 等），既浪费 turn 又污染评测语料；具体文件
        与是否 no-op 由 runtime 的 ``start_container`` 实现决定（OpenClaw 复制
        IDENTITY/USER/SOUL 并删 BOOTSTRAP；其他 agent 可注入项目指令、规则文件，或忽略
        该标志）。

        ``load_state``: 当前 holdout 加载态（``BASELINE`` / ``EVOLVED``）。多数
        runtime 通过 ``image`` 即可区分（base vs delta），但群体记忆等不依赖镜像
        commit 的策略需要这个显式信号决定是否注入"已学经验"配置。
        """

    async def evolve_after_task(
        self,
        env: ExecutionEnvironment,
        task: SuiteTask,
        result: PhaseRun,
        ctx: SuiteRunContext,
    ) -> None:
        """每道 warmup 题完成后立刻调用的 evolve 钩子；默认 no-op。

        典型用法：在群体记忆 / 多容器场景下，每题独立容器跑完即写一次外部记忆 flush；
        OpenClaw + 进化插件子类则在此把本题 ``PhaseRun`` 摘要 ``POST /signals``，让
        ``learn review`` 阶段能根据 SignalRecord 反查到本题 session。
        在共享容器（``SERIAL_SINGLE`` / ``PARALLEL_SINGLE``）模式下也可被覆写为
        增量 evolve，但要注意 ``PARALLEL_SINGLE`` 下多次并发调用同一容器的
        evolve 操作可能产生竞态——具体由子类自行评估。
        """
        _ = (env, task, result, ctx)
        return None

    async def count_tool_calls(
        self,
        env: ExecutionEnvironment,
        task: SuiteTask,
        result: PhaseRun,
        ctx: SuiteRunContext,
    ) -> int | None:
        """Holdout 单题 work agent tool 调用总次数；默认返回 None。

        子类按 runtime 自行覆写：OpenClaw 走 trajectory.jsonl 的
        ``model.completed.messagesSnapshot`` 数 ``toolCall`` block，其他 runtime
        如果拿不到就保持 None——dashboard 会显示 "—"。返回 None 时上层不会写入
        ``PhaseRun.tool_calls``，保持 default。

        被调用时机：``_run_holdout`` 中 ``execute_task`` 完成后、容器 cleanup
        之前——容器尚存活，子类可 ``docker exec`` 读容器内文件。
        """
        _ = (env, task, result, ctx)
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
        """Runtime image or identifier for before-load holdout."""
