from __future__ import annotations

from src_new.lift.runtime.environment_cleaner import EnvironmentCleaner


async def commit_delta_image(container_name: str, image_tag: str) -> str:
    """Commit a warmup container filesystem to a delta image tag."""
    cleaner = EnvironmentCleaner()
    return await cleaner.commit_container(container_name, image_tag)
