"""Suite 题目分组：warmup（train）与 holdout（test）。"""

from __future__ import annotations

from src.models import Suite, SuiteTask


def split_suite_tasks(suite: Suite) -> tuple[list[SuiteTask], list[SuiteTask]]:
    """返回 suite 的 warmup 与 holdout 题列表。"""
    if not suite.warmup_tasks:
        raise ValueError(f"No warmup_tasks in suite {suite.name!r}")
    if not suite.holdout_tasks:
        raise ValueError(f"No holdout_tasks in suite {suite.name!r}")
    return list(suite.warmup_tasks), list(suite.holdout_tasks)
