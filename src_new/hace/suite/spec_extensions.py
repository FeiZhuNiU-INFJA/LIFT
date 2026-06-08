from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from src_new.models import SuiteSpec


@dataclass(frozen=True)
class HaceSuiteConfig:
    """Suite JSON plus hold-out settings (hace-only fields, not in src.models.SuiteSpec)."""

    spec: SuiteSpec
    holdout_count: int = 1
    holdout_task_names: tuple[str, ...] | None = None


def load_hace_suite(file_path: str | Path) -> HaceSuiteConfig:
    path = Path(file_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Suite JSON must be an object: {path}")

    holdout_count = data.pop("holdout_count", 1)
    holdout_names_raw = data.pop("holdout_task_names", None)

    if holdout_count is not None and not isinstance(holdout_count, int):
        raise ValueError(f"holdout_count must be int: {path}")
    if holdout_count < 1:
        raise ValueError(f"holdout_count must be >= 1: {path}")

    holdout_task_names: tuple[str, ...] | None = None
    if holdout_names_raw is not None:
        if not isinstance(holdout_names_raw, list) or not all(
            isinstance(n, str) for n in holdout_names_raw
        ):
            raise ValueError(f"holdout_task_names must be a list of strings: {path}")
        holdout_task_names = tuple(holdout_names_raw)

    spec = SuiteSpec.model_validate(data)
    for task in spec.tasks:
        task.category_name = spec.category

    return HaceSuiteConfig(
        spec=spec,
        holdout_count=holdout_count if holdout_count is not None else 1,
        holdout_task_names=holdout_task_names,
    )
