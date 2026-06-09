"""Suite 题目切分：warmup（产物生产）与 hold-out（LIFT 终测对照）。"""

from __future__ import annotations

from src_new.models import SuiteTask

from src_new.lift.suite.lift_suite import LiftSuiteConfig


def split_suite_tasks(config: LiftSuiteConfig) -> tuple[list[SuiteTask], list[SuiteTask]]:
    """将 suite 题目切分为 warmup（产物进化）与 hold-out（终测对照）两列表。"""
    tasks = config.suite.tasks
    if not tasks:
        raise ValueError(f"No tasks in suite {config.suite.name!r}")

    if config.holdout_task_names:
        name_set = set(config.holdout_task_names)
        holdout = [t for t in tasks if t.name in name_set]
        warmup = [t for t in tasks if t.name not in name_set]
        missing = name_set - {t.name for t in holdout}
        if missing:
            raise ValueError(
                f"holdout_task_names not found in suite {config.suite.name!r}: {sorted(missing)}"
            )
        if not holdout:
            raise ValueError(f"holdout_task_names matched no tasks in {config.suite.name!r}")
        return warmup, holdout

    n = config.holdout_count
    if n > len(tasks):
        raise ValueError(
            f"holdout_count={n} exceeds task count {len(tasks)} in suite {config.suite.name!r}"
        )
    warmup = tasks[:-n]
    holdout = tasks[-n:]
    return warmup, holdout
