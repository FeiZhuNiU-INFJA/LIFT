"""HermesWithAgentMemoryAdapter：带 agentmemory memory provider plugin 的 Hermes runtime。

继承基础 ``HermesAdapter``，改动两点：

1. 使用带 agentmemory 的镜像 ``HERMES_WITH_AGENTMEMORY_DOCKER_IMAGE``（构建时
   ``INSTALL_AGENTMEMORY=true``，``agent-runtimes/hermes/build-image.sh --with-agentmemory``）。
2. ``force_bridge_network = True``：容器内起绑定 :3111 的 agentmemory server，host 网络下
   并发容器会抢同一宿主端口冲突，故强制 bridge（基础 ``start_container`` 已读该属性透传）。

agentmemory 采用 README「Option 2: Memory provider plugin」深度集成：镜像里把
``integrations/hermes`` 装进 Hermes profile 的 plugins 目录，``patch_hermes_config.py`` 在
容器启动时把 ``memory.provider`` 置为 agentmemory，``hermes-entrypoint.sh`` 后台拉起 :3111
server。chat 走 ``docker exec hermes_runner.py``（同容器同网络命名空间），runner 直连的
``AIAgent`` 通过 ``localhost:3111`` 访问 server。warmup 期 review 写入的记忆落在容器内
``/root/.agentmemory``，随 ``docker commit`` 进入 delta 镜像。warmup / review / commit 流程
完全复用基础 adapter，故无需 override ``start_container``。
"""

from __future__ import annotations

from typing import override

from src.lift.adapters.hermes.adapter import HermesAdapter
from src.paths import HERMES_WITH_AGENTMEMORY_DOCKER_IMAGE


class HermesWithAgentMemoryAdapter(HermesAdapter):
    """Hermes + agentmemory memory provider plugin：切换镜像 + 强制 bridge。"""

    #: 记忆真正落在容器内 agentmemory server 的数据目录 ``/root/.agentmemory``；随
    #: docker commit 进入 delta 镜像。追加到基础 Hermes 的 evolve_paths 之后。
    evolve_paths: tuple[str, ...] = HermesAdapter.evolve_paths + ("/root/.agentmemory",)

    #: 容器内自带绑定 :3111 的 agentmemory server；强制 bridge 避免并发容器抢端口。
    force_bridge_network: bool = True

    @classmethod
    @override
    def resolve_docker_image(cls, *, override: str | None = None) -> str:
        """``RunOptions.docker_image`` 覆盖优先，否则用带 agentmemory 的 Hermes 镜像。"""
        return override or HERMES_WITH_AGENTMEMORY_DOCKER_IMAGE
