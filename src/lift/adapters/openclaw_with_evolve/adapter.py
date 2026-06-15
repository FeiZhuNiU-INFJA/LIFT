"""OpenClawWithEvolveAdapter：带 self-evolving-plugin-pro 进化插件的 OpenClaw runtime。

继承基础 ``OpenClawAdapter``，仅 override 两点：

    1. 使用带进化插件的镜像 ``OPENCLAW_WITH_EVOLVE_DOCKER_IMAGE``（构建时
       ``INSTALL_SELF_EVOLVING=true``，``build-image.sh`` 默认）；
    2. ``evolve_after_warmup`` 在容器内执行 ``openclaw learn review``，把 warmup 期间产生的
       memory/skill 变化经由插件评审落盘，再由 ``docker commit`` 带入 delta 镜像。
"""

from __future__ import annotations

from typing import override

from src.lift.adapters.base import SuiteRunContext
from src.lift.adapters.container.session import ContainerSession
from src.lift.adapters.environment import ExecutionEnvironment
from src.lift.adapters.openclaw.adapter import OpenClawAdapter
from src.lift.adapters.openclaw.evolve import openclaw_learn_review
from src.lift.adapters.openclaw.session import openclaw_context
from src.paths import OPENCLAW_WITH_EVOLVE_DOCKER_IMAGE


class OpenClawWithEvolveAdapter(OpenClawAdapter):
    """OpenClaw + 进化插件：复用基础 adapter，仅切换镜像与 evolve 钩子。"""

    @classmethod
    @override
    def resolve_docker_image(cls, *, override: str | None = None) -> str:
        """``RunOptions.docker_image`` 覆盖优先，否则用带进化插件的镜像。"""
        return override or OPENCLAW_WITH_EVOLVE_DOCKER_IMAGE

    @override
    async def evolve_after_warmup(
        self, env: ExecutionEnvironment, ctx: SuiteRunContext
    ) -> None:
        """warmup 完成后在容器内执行 ``openclaw learn review``。"""
        _ = ctx
        session: ContainerSession = env.handle
        await openclaw_learn_review(openclaw_context(session))
