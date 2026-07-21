"""HermesWithOpenSpaceAdapter：带 OpenSpace MCP 插件的 Hermes runtime。

继承基础 ``HermesAdapter``，仅 override 一点：使用带 OpenSpace 的镜像
``HERMES_WITH_OPENSPACE_DOCKER_IMAGE``（构建时 ``INSTALL_OPENSPACE=true``，
``agent-runtimes/hermes/build-image.sh --with-openspace``）。

OpenSpace 作为 Hermes 的 MCP server（``mcp_servers.openspace``，由容器启动时
``patch_hermes_config.py`` upsert 进 ``config.yaml``）在启动时被 discover 并注册为
native tool，不改变 Hermes 的 warmup / review 驱动演化语义，故 warmup / holdout / delta
流程完全复用基础 adapter。
"""

from __future__ import annotations

from typing import override

from src.lift.adapters.hermes.adapter import HermesAdapter
from src.paths import HERMES_WITH_OPENSPACE_DOCKER_IMAGE


class HermesWithOpenSpaceAdapter(HermesAdapter):
    """Hermes + OpenSpace MCP 插件：复用基础 adapter，仅切换镜像。"""

    @classmethod
    @override
    def resolve_docker_image(cls, *, override: str | None = None) -> str:
        """``RunOptions.docker_image`` 覆盖优先，否则用带 OpenSpace 的 Hermes 镜像。"""
        return override or HERMES_WITH_OPENSPACE_DOCKER_IMAGE
