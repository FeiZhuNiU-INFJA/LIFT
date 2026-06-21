"""OpenClawWithEvolveAdapter：带 self-evolving-plugin-pro 进化插件的 OpenClaw runtime。

继承基础 ``OpenClawAdapter``，仅 override 四点：

    1. 使用带进化插件的镜像 ``OPENCLAW_WITH_EVOLVE_DOCKER_IMAGE``（构建时
       ``INSTALL_SELF_EVOLVING=true``，``build-image.sh`` 默认）；
    2. ``start_warmup_environment`` 启动后立即跑一次 ``openclaw learn status``，
       触发 plugin onboarding 把 ``runtime-state.json`` 写入容器；否则
       ``post_signal`` 会因 ``instance_id/token`` 缺失而短路；
    3. ``evolve_after_warmup`` 在容器内执行 ``openclaw learn review``，把 warmup 期间产生的
       memory/skill 变化经由插件评审落盘，再由 ``docker commit`` 带入 delta 镜像；
    4. ``evolve_after_task`` 每题完成后代 work agent ``POST /signals``——把本题
       ``PhaseRun`` 的 ``success`` / ``score`` 摘要直接写入 plugin 后端，绕开
       agent 自上报通道（work agent 在 LIFT 评测语境里把 critique 当工单不当反馈，
       不会执行 plugin 协议里的 ``exec curl`` 自报）。
"""

from __future__ import annotations

from typing import override

from src.config import LOGGER
from src.lift.adapters.base import SuiteRunContext
from src.lift.adapters.container.session import ContainerSession
from src.lift.adapters.environment import ExecutionEnvironment
from src.lift.adapters.openclaw.adapter import OpenClawAdapter
from src.lift.adapters.openclaw.session import openclaw_context
from src.lift.adapters.openclaw_with_evolve.evolve import (
    bootstrap_evolution_runtime,
    openclaw_learn_review,
    post_signal_via_container,
)
from src.lift.runtime.suite_run_resources import SuiteRunResources
from src.models import PhaseRun, SuiteTask
from src.paths import OPENCLAW_WITH_EVOLVE_DOCKER_IMAGE


# warmup 题完成后转 signal 的策略：judge 通过 → success_confirmation；
# 没通过 → correction（plugin 协议里专门给"明确指出做错了"用的 kind，
# 最贴近 LIFT critique 的语义）。trust 设成与 plugin 协议示例一致的高位
# 档（critique 是显式判定，不是模糊隐式信号），避免被下游聚合阶段过滤掉。
_SIGNAL_TRUST_SUCCESS = 0.95
_SIGNAL_TRUST_CORRECTION = 0.9


class OpenClawWithEvolveAdapter(OpenClawAdapter):
    """OpenClaw + 进化插件：复用基础 adapter，仅切换镜像与 evolve 钩子。"""

    @classmethod
    @override
    def resolve_docker_image(cls, *, override: str | None = None) -> str:
        """``RunOptions.docker_image`` 覆盖优先，否则用带进化插件的镜像。"""
        return override or OPENCLAW_WITH_EVOLVE_DOCKER_IMAGE

    @override
    async def start_warmup_environment(
        self,
        ctx: SuiteRunContext,
        resources: SuiteRunResources,
        workspace_dir,
    ) -> ExecutionEnvironment:
        """启动 warmup 容器后立即跑一次 plugin onboarding。

        plugin 写 ``runtime-state.json`` 的唯一路径是 ``bootstrapInstance``——
        ``openclaw learn status`` CLI handler 内部会 ``await ensureReady()``
        把它跑通。LIFT 用 ``openclaw agent --local`` 单次 CLI 驱动 work agent
        本身不会触发该路径，故这里在容器进入 warmup 主循环前显式补一次。
        没有这一步，所有 ``evolve_after_task`` 的 ``post_signal`` 都会因
        ``runtime-state.json missing`` 短路 ``exit 0``。
        """
        env = await super().start_warmup_environment(ctx, resources, workspace_dir)
        session: ContainerSession = env.handle
        await bootstrap_evolution_runtime(openclaw_context(session))
        return env

    @override
    async def evolve_after_warmup(
        self, env: ExecutionEnvironment, ctx: SuiteRunContext
    ) -> None:
        """warmup 完成后在容器内执行 ``openclaw learn review``。"""
        _ = ctx
        session: ContainerSession = env.handle
        await openclaw_learn_review(openclaw_context(session))

    @override
    async def evolve_after_task(
        self,
        env: ExecutionEnvironment,
        task: SuiteTask,
        result: PhaseRun,
        ctx: SuiteRunContext,
    ) -> None:
        """每题完成后用 ``PhaseRun`` 摘要 ``POST /signals``。

        - ``result.success=True`` → kind=``success_confirmation``，trust=0.95
        - ``result.success=False`` → kind=``correction``，trust=0.90

        ``session_id`` 用 ``result.work_session_id``——和容器内 gateway 流量、
        Langfuse session id、plugin SignalRecord.session_id 全部对齐，
        ``learn review._select_review_sessions`` 才能根据 SignalRecord 反查到
        本题 session。content 只放最小够用的 task / success / score / turns
        摘要——plugin review worker 实际读的是 ``sessionrecord`` 完整对话，
        signal.content 仅用作选 session 的索引。
        """
        _ = ctx
        session: ContainerSession = env.handle
        container = openclaw_context(session)
        if result.success:
            kind = "success_confirmation"
            trust = _SIGNAL_TRUST_SUCCESS
        else:
            kind = "correction"
            trust = _SIGNAL_TRUST_CORRECTION
        content = (
            f"[lift] task={task.name} success={result.success} "
            f"score={result.content_score:.2f} turns={result.turns}"
        )
        LOGGER.info(
            "evolve_after_task: posting signal task=%s kind=%s trust=%.2f session=%s",
            task.name, kind, trust, result.work_session_id,
        )
        await post_signal_via_container(
            container,
            session_id=result.work_session_id,
            kind=kind,
            content=content,
            trust=trust,
            tags=["lift_eval", kind, f"task:{task.name}"],
            raise_on_error=True,
        )
