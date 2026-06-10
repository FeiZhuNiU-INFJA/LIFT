"""warmup 容器 commit 为 delta 镜像的薄封装。"""

from __future__ import annotations

from src.lift.runtime.environment_cleaner import EnvironmentCleaner


async def commit_delta_image(container_name: str, image_tag: str) -> str:
    """将 warmup 容器文件系统 commit 为 delta 镜像 tag（容器须仍在运行）。"""
    cleaner = EnvironmentCleaner()
    return await cleaner.commit_container(container_name, image_tag)
