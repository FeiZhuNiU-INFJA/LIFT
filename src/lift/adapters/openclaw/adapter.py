"""OpenClaw runtime adapter：镜像解析、容器启动、chat factory 与 evolve 钩子。"""

from __future__ import annotations

from pathlib import Path
from typing import override

from src.lift.adapters.base import SuiteRunContext
from src.lift.adapters.container.adapter import ContainerAgentRuntimeAdapter
from src.lift.adapters.container.session import ContainerSession
from src.lift.adapters.environment import ExecutionEnvironment
from src.lift.adapters.openclaw.chat_agent import OpenClawWorkerJudgerPairFactory
from src.lift.adapters.openclaw.evolve import openclaw_learn_review
from src.lift.adapters.openclaw.session import openclaw_context, start_openclaw_container
from src.lift.eval.stage import SuiteRunPhase
from src.lift.eval.worker_judger import WorkerJudgerPairFactory
from src.models import SuiteTask
from src.paths import OPENCLAW_DOCKER_IMAGE


class OpenClawAdapter(ContainerAgentRuntimeAdapter):
    """OpenClaw：镜像配置、容器启动、chat factory 与 evolve 钩子。"""

    @classmethod
    @override
    def resolve_docker_image(cls, *, override: str | None = None) -> str:
        """``RunOptions.docker_image`` 覆盖优先，否则 ``OPENCLAW_DOCKER_IMAGE``。"""
        return override or OPENCLAW_DOCKER_IMAGE

    @override
    async def start_container(
        self,
        *,
        instance_id: str,
        image: str,
        ctx: SuiteRunContext,
        workspace_dir: Path,
        seed_workspace: bool,
        task: SuiteTask | None,
    ) -> ContainerSession:
        """委托 ``start_openclaw_container`` 启动 gateway 容器。"""
        return await start_openclaw_container(
            instance_id=instance_id,
            image=image,
            ctx=ctx,
            workspace_dir=workspace_dir,
            seed_workspace=seed_workspace,
            task=task,
        )

    @override
    def worker_judger_factory(
        self,
        env: ExecutionEnvironment,
        ctx: SuiteRunContext,
        *,
        run_phase: SuiteRunPhase,
        workspace_dir: Path,
    ) -> WorkerJudgerPairFactory:
        """绑定 OpenClaw 容器上下文，返回 ``OpenClawWorkerJudgerPairFactory``。"""
        session: ContainerSession = env.handle
        _ = (ctx, run_phase)  # factory 按题创建 pair；phase 标签在 execute_task → run_task 传入
        return OpenClawWorkerJudgerPairFactory(
            container=openclaw_context(session),
            workspace_dir=workspace_dir,
        )

    @override
    async def apply_evolve(self, env: ExecutionEnvironment, ctx: SuiteRunContext) -> None:
        """warmup 完成后在容器内执行 ``openclaw learn review``。"""
        _ = ctx
        session: ContainerSession = env.handle
        await openclaw_learn_review(openclaw_context(session))
