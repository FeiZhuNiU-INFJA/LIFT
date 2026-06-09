"""LIFT suite JSON 加载：标准 ``Suite`` + hold-out 扩展字段。"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from src_new.models import Suite


class LiftSuiteConfig(BaseModel):
    """Suite JSON 解析结果：标准 Suite + LIFT hold-out 扩展字段。"""

    model_config = ConfigDict(frozen=True)

    suite: Suite = Field(description="标准评测集（name、category、tasks 等）")
    holdout_count: int = Field(
        default=1,
        description="从 tasks 尾部留出几道题作为 hold-out（默认 1）",
    )
    holdout_task_names: tuple[str, ...] | None = Field(
        default=None,
        description="显式指定 hold-out 题名；为 None 时按 holdout_count 从尾部切",
    )


def load_lift_suite(file_path: str | Path) -> LiftSuiteConfig:
    """读取 suite JSON 并解析为 ``LiftSuiteConfig``（含 hold-out 切分元数据）。"""
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

    suite = Suite.model_validate(data)
    for task in suite.tasks:
        task.category_name = suite.category

    return LiftSuiteConfig(
        suite=suite,
        holdout_count=holdout_count if holdout_count is not None else 1,
        holdout_task_names=holdout_task_names,
    )
