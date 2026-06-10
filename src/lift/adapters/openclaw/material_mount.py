"""Backward-compatible re-exports; prefer ``lift.adapters.container.volumes``."""

from src.lift.adapters.container.volumes import (
    default_volume_binds,
    material_digest_for_task,
    resolve_host_path,
    task_volume_binds,
)

__all__ = [
    "default_volume_binds",
    "material_digest_for_task",
    "resolve_host_path",
    "task_volume_binds",
]
