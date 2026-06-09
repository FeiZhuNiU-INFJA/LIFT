"""Delta 镜像引用（warmup evolve 产物）与 Disposable cleanup。"""

from __future__ import annotations

from typing import override

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from src_new.lift.runtime.disposable import Disposable
from src_new.lift.runtime.environment_cleaner import EnvironmentCleaner


class DeltaRef(BaseModel, Disposable):
    """warmup 进化产物的引用（OpenClaw 实现为 docker commit 出的临时镜像）。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    image_tag: str = Field(
        description="delta 镜像名（如 evolve-eval-delta:{run_id}-r0-Hello），evolved 阶段 docker run 使用"
    )
    source_container: str | None = Field(
        default=None,
        description="commit 前的 warmup 容器名（可选，便于调试）",
    )

    _cleaner: EnvironmentCleaner = PrivateAttr(default_factory=EnvironmentCleaner)
    _cleaned: bool = PrivateAttr(default=False)

    @override
    async def cleanup(self) -> None:
        """幂等删除 delta 镜像（``docker rmi``）。"""
        if self._cleaned:
            return
        await self._cleaner.remove_image(self.image_tag)
        self._cleaned = True
