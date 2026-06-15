"""OpenClaw runtime adapter（基础版）：镜像解析、容器启动、chat factory。

本类对应**不带** ``self-evolving-plugin-pro`` 进化插件的 OpenClaw runtime：

    1. 使用基础镜像 ``OPENCLAW_BASE_DOCKER_IMAGE``（构建时 ``INSTALL_SELF_EVOLVING=false``）；
    2. ``evolve_after_warmup`` 为 no-op，不调用 ``openclaw learn review``。

仍**保留**继承自 ``ContainerAgentRuntimeAdapter`` 的 ``docker commit`` delta 物化：warmup
阶段 OpenClaw 在使用过程中自然产生的 skill/memory 文件系统变化，会被 commit 进 delta 镜像并
带到 hold-out 的 evolved 阶段。

带进化插件的变体见 ``OpenClawWithEvolveAdapter``（继承本类，仅 override 镜像与 evolve 钩子）。
"""

from __future__ import annotations

from pathlib import Path
from typing import override

from src.lift.adapters.base import SuiteRunContext
from src.lift.adapters.container.adapter import ContainerAgentRuntimeAdapter
from src.lift.adapters.container.session import ContainerSession
from src.lift.adapters.environment import ExecutionEnvironment
from src.lift.adapters.openclaw.chat_agent import OpenClawWorkerJudgerPairFactory
from src.lift.adapters.openclaw.session import openclaw_context, start_openclaw_container
from src.lift.eval.stage import HoldoutLoadState, SuiteRunPhase
from src.lift.eval.worker_judger import WorkerJudgerPairFactory
from src.models import SuiteTask
from src.paths import OPENCLAW_BASE_DOCKER_IMAGE


class OpenClawAdapter(ContainerAgentRuntimeAdapter):
    """OpenClaw（不带进化插件）：镜像配置、容器启动、chat factory。"""

    @classmethod
    @override
    def resolve_docker_image(cls, *, override: str | None = None) -> str:
        """``RunOptions.docker_image`` 覆盖优先，否则用基础（不带进化）镜像。"""
        return override or OPENCLAW_BASE_DOCKER_IMAGE

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
        load_state: HoldoutLoadState | None = None,
    ) -> ContainerSession:
        """委托 ``start_openclaw_container`` 启动 gateway 容器。

        OpenClaw baseline 与 evolved 的差异完全由 ``image``（base vs delta 镜像）
        承载，因此忽略 ``load_state``。Mixin/子类如需根据 load_state 注入额外
        env（如群体记忆配置）可在覆盖中读取该参数。
        """
        _ = load_state  # OpenClaw 主路径不区分；GroupMemoryAdapterMixin 会用到
        return await start_openclaw_container(
            instance_id=instance_id,
            image=image,
            ctx=ctx,
            workspace_dir=workspace_dir,
            seed_workspace=seed_workspace,
            task=task,
            container_memory=self._options.container_memory,
            container_cpus=self._options.container_cpus,
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
    async def evolve_after_warmup(
        self, env: ExecutionEnvironment, ctx: SuiteRunContext
    ) -> None:
        """不带进化插件：warmup 后不做显式 learn review。

        OpenClaw 运行中自然产生的 skill/memory 变化由 docker commit 带入 delta 镜像。
        """
        _ = (env, ctx)
        return None
