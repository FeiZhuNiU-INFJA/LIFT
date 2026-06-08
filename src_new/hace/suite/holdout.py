from __future__ import annotations

from src_new.models import SuiteTask

from src_new.hace.suite.spec_extensions import HaceSuiteConfig


def split_suite_tasks(config: HaceSuiteConfig) -> tuple[list[SuiteTask], list[SuiteTask]]:
    """Split suite tasks into warmup (artifact production) and hold-out (HACE contrast)."""
    tasks = config.spec.tasks
    if not tasks:
        raise ValueError(f"No tasks in suite {config.spec.name!r}")

    if config.holdout_task_names:
        name_set = set(config.holdout_task_names)
        holdout = [t for t in tasks if t.name in name_set]
        warmup = [t for t in tasks if t.name not in name_set]
        missing = name_set - {t.name for t in holdout}
        if missing:
            raise ValueError(
                f"holdout_task_names not found in suite {config.spec.name!r}: {sorted(missing)}"
            )
        if not holdout:
            raise ValueError(f"holdout_task_names matched no tasks in {config.spec.name!r}")
        return warmup, holdout

    n = config.holdout_count
    if n > len(tasks):
        raise ValueError(
            f"holdout_count={n} exceeds task count {len(tasks)} in suite {config.spec.name!r}"
        )
    warmup = tasks[:-n]
    holdout = tasks[-n:]
    return warmup, holdout
