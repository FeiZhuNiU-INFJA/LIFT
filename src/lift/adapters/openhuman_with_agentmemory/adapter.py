"""OpenHumanWithAgentMemoryAdapter：带 agentmemory backend 的 OpenHuman runtime。

继承基础 ``OpenHumanAdapter``，改动两点：

1. 使用带 agentmemory 的镜像 ``OPENHUMAN_WITH_AGENTMEMORY_DOCKER_IMAGE``（构建时
   ``INSTALL_AGENTMEMORY=true``，``agent-runtimes/openhuman/build-image.sh --with-agentmemory``）。
2. ``force_bridge_network = True``：容器内起绑定 :3111 的 agentmemory server，host 网络下
   并发容器会抢同一宿主端口冲突，故强制 bridge（基础 ``start_container`` 已读该属性透传）。

agentmemory 采用官方 wiki 的 config.toml backend 切换：镜像 config.toml 写
``[memory] backend = "agentmemory"``，openhuman-core 旁路自家 SQLite + Embedder，把 Memory
trait 调用代理到容器内 :3111 server。镜像 ENTRYPOINT 包装脚本
``openhuman-agentmemory-entrypoint.sh`` 在 openhuman-core 启动前先拉起并等待 :3111 就绪
（OH agentmemory backend 无自动回退 SQLite，启动时 daemon 不可达会报错）。warmup 期写入的
记忆落在容器内 ``/root/.agentmemory``，随 ``docker commit`` 进入 delta 镜像。commit 流程完全
复用基础 adapter，故无需 override ``start_container``。
"""

from __future__ import annotations

from typing import override

from src.lift.adapters.openhuman.adapter import OpenHumanAdapter
from src.paths import OPENHUMAN_WITH_AGENTMEMORY_DOCKER_IMAGE


class OpenHumanWithAgentMemoryAdapter(OpenHumanAdapter):
    """OpenHuman + agentmemory backend：切换镜像 + 强制 bridge。"""

    #: 记忆改由容器内 agentmemory server 承载（旁路 memory_tree/wiki），数据目录
    #: ``/root/.agentmemory`` 随 docker commit 进入 delta 镜像。追加到基础 evolve_paths。
    evolve_paths: tuple[str, ...] = OpenHumanAdapter.evolve_paths + ("/root/.agentmemory",)

    #: 容器内自带绑定 :3111 的 agentmemory server；强制 bridge 避免并发容器抢端口。
    force_bridge_network: bool = True

    @classmethod
    @override
    def resolve_docker_image(cls, *, override: str | None = None) -> str:
        """``RunOptions.docker_image`` 覆盖优先，否则用带 agentmemory 的 OpenHuman 镜像。"""
        return override or OPENHUMAN_WITH_AGENTMEMORY_DOCKER_IMAGE
