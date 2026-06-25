"""Delta 镜像引用（warmup evolve 产物）与 Disposable cleanup。"""

from __future__ import annotations

from typing import override

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from src.lift.runtime.disposable import Disposable
from src.lift.runtime.environment_cleaner import EnvironmentCleaner


class DeltaRef(BaseModel, Disposable):
    """warmup 进化产物的引用。

    主流实现（OpenClaw）= ``docker commit`` 出的**框架专属**镜像，cleanup 时
    ``docker rmi`` 删除。但部分编排策略（如群体记忆）的 delta **不是新镜像**——
    它复用 base 镜像，evolved 信号通过外部系统（如群体记忆 namespace）传递。
    此时 ``owned=False``，cleanup 必须 no-op，避免误删 base 镜像。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    image_tag: str = Field(
        description="evolved holdout 启动时 docker run 用的镜像；可能等于 base 镜像（owned=False）"
    )
    source_container: str | None = Field(
        default=None,
        description="commit 前的 warmup 容器名（可选，便于调试）",
    )
    owned: bool = Field(
        default=True,
        description="cleanup 时是否 docker rmi。False 表示该镜像由外部拥有（如 base 镜像），不可删",
    )

    _cleaner: EnvironmentCleaner = PrivateAttr(default_factory=EnvironmentCleaner)
    _cleaned: bool = PrivateAttr(default=False)

    @override
    async def cleanup(self) -> None:
        """幂等地按 ``owned`` 决定是否 ``docker rmi``。"""
        if self._cleaned:
            return
        if self.owned:
            await self._cleaner.remove_image(self.image_tag)
        self._cleaned = True
