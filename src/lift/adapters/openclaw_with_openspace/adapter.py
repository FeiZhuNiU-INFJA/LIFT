"""OpenClawWithOpenSpaceAdapter：带 OpenSpace MCP 插件的 OpenClaw runtime。

继承基础 ``OpenClawAdapter``，仅 override 一点：使用带 OpenSpace 的镜像
``OPENCLAW_WITH_OPENSPACE_DOCKER_IMAGE``（构建时 ``INSTALL_OPENSPACE=true``，
``build-image.sh --with-openspace``）。

OpenSpace 是基于 MCP 的 skill hub 工具增强，不改变 warmup / evolve 语义，因此
warmup / holdout / delta（``docker commit``）流程完全复用基础 adapter：warmup 期
OpenSpace 产生的本地 skill / 记忆变化随镜像层被 commit 进 delta 镜像。镜像内未装
self-evolving-plugin-pro，故不需要 evolve hook。
"""

from __future__ import annotations

from typing import override

from src.lift.adapters.openclaw.adapter import OpenClawAdapter
from src.paths import OPENCLAW_WITH_OPENSPACE_DOCKER_IMAGE


class OpenClawWithOpenSpaceAdapter(OpenClawAdapter):
    """OpenClaw + OpenSpace MCP 插件：复用基础 adapter，仅切换镜像。"""

    @classmethod
    @override
    def resolve_docker_image(cls, *, override: str | None = None) -> str:
        """``RunOptions.docker_image`` 覆盖优先，否则用带 OpenSpace 的镜像。"""
        return override or OPENCLAW_WITH_OPENSPACE_DOCKER_IMAGE
