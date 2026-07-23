"""OpenClawWithAgentMemoryAdapter：带 agentmemory memory plugin 的 OpenClaw runtime。

继承基础 ``OpenClawAdapter``，改动三点：

1. 使用带 agentmemory 的镜像 ``OPENCLAW_WITH_AGENTMEMORY_DOCKER_IMAGE``（构建时
   ``INSTALL_AGENTMEMORY=true``，``build-image.sh --with-agentmemory``）。
2. ``force_bridge_network = True``：容器内起一个绑定 :3111 的 agentmemory server，
   host 网络下并发容器会抢同一宿主端口冲突，故强制 bridge（忽略全局 CONTAINER_NETWORK_MODE）。
3. ``start_container`` 打开 ``agentmemory_prelaunch``：把 gateway 启动命令前置一个 prelaunch
   包装脚本，容器启动时先后台拉起 agentmemory server（离线本地嵌入，:3111）再 exec gateway。

agentmemory 采用 README「Option 2: OpenClaw memory plugin」深度集成：镜像里已把插件装进
``/root/.openclaw/extensions/agentmemory`` 并在 ``openclaw.json`` claim
``plugins.slots.memory = "agentmemory"``。warmup 期写入的记忆落在容器内 ``/root/.agentmemory``
（image FS 层，非 VOLUME/mount），随 ``docker commit`` 进入 delta 镜像；因此 warmup / holdout /
delta 流程完全复用基础 adapter。镜像内未装 self-evolving-plugin-pro，故不需要 evolve hook。
"""

from __future__ import annotations

from pathlib import Path
from typing import override

from src.lift.adapters.base import SuiteRunContext
from src.lift.adapters.container.session import ContainerSession
from src.lift.adapters.openclaw.adapter import OpenClawAdapter
from src.lift.adapters.openclaw.session import start_openclaw_container
from src.lift.eval.stage import HoldoutLoadState
from src.models import SuiteTask
from src.paths import OPENCLAW_WITH_AGENTMEMORY_DOCKER_IMAGE


class OpenClawWithAgentMemoryAdapter(OpenClawAdapter):
    """OpenClaw + agentmemory memory plugin：切换镜像 + 强制 bridge + prelaunch server。"""

    #: 记忆真正落在容器内 agentmemory server 的数据目录 ``/root/.agentmemory``；随
    #: docker commit 进入 delta 镜像。追加到基础 OpenClaw 的 evolve_paths 之后。
    evolve_paths: tuple[str, ...] = OpenClawAdapter.evolve_paths + ("/root/.agentmemory",)

    #: 容器内自带绑定 :3111 的 agentmemory server；强制 bridge 避免并发容器抢端口。
    force_bridge_network: bool = True

    @classmethod
    @override
    def resolve_docker_image(cls, *, override: str | None = None) -> str:
        """``RunOptions.docker_image`` 覆盖优先，否则用带 agentmemory 的镜像。"""
        return override or OPENCLAW_WITH_AGENTMEMORY_DOCKER_IMAGE

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
        viz_role: str | None = None,
    ) -> ContainerSession:
        """委托 ``start_openclaw_container``，打开 agentmemory prelaunch 与强制 bridge。"""
        _ = load_state  # OpenClaw 主路径不区分 baseline/evolved（差异由镜像承载）
        return await start_openclaw_container(
            instance_id=instance_id,
            image=image,
            ctx=ctx,
            workspace_dir=workspace_dir,
            seed_workspace=seed_workspace,
            task=task,
            container_memory=self._options.container_memory,
            container_cpus=self._options.container_cpus,
            force_bridge_network=self.force_bridge_network,
            agentmemory_prelaunch=True,
            viz_role=viz_role,
        )
