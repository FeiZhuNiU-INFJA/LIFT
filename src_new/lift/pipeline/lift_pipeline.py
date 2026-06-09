from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from src_new.config import LOGGER
from src_new.models import EvalRepeat, EvalReport, SuiteRun, TaskRun

from src_new.lift.adapters.base import AgentRuntimeAdapter, SuiteRunContext
from src_new.lift.policies.artifact import WarmupThenUpdatePolicy
from src_new.lift.pipeline.run_options import RunOptions
from src_new.lift.suite.holdout import split_suite_tasks
from src_new.lift.suite.lift_suite import load_lift_suite
from src_new.paths import default_report_root


class LIFTPipeline:
    """Loaded Impact on Final Task orchestration."""

    def __init__(
        self,
        *,
        report_root: Path | None = None,
    ) -> None:
        self.report_root = report_root or default_report_root()
        self._report_lock = asyncio.Lock()

    async def run(
        self,
        *,
        run_id: str,
        suite_paths: list[Path],
        adapter: AgentRuntimeAdapter,
        options: RunOptions,
    ) -> EvalReport:
        eval_report = EvalReport(run_id=run_id)
        report_path = self.report_root / f"{run_id}.json"
        self.report_root.mkdir(parents=True, exist_ok=True)
        eval_report.runs = [EvalRepeat() for _ in range(options.repeat)]

        if options.parallel_repeats and options.repeat > 1:
            await asyncio.gather(
                *[
                    self._run_suites(
                        repeat_index=i,
                        run_id=run_id,
                        suite_paths=suite_paths,
                        adapter=adapter,
                        options=options,
                        eval_report=eval_report,
                        report_path=report_path,
                    )
                    for i in range(options.repeat)
                ]
            )
        else:
            for repeat_index in range(options.repeat):
                await self._run_suites(
                    repeat_index=repeat_index,
                    run_id=run_id,
                    suite_paths=suite_paths,
                    adapter=adapter,
                    options=options,
                    eval_report=eval_report,
                    report_path=report_path,
                )

        eval_report.completed_at = datetime.now(timezone.utc).isoformat()
        async with self._report_lock:
            eval_report.write_json(report_path)
        LOGGER.info("LIFT report written: %s", report_path)
        return eval_report

    async def _run_suites(
        self,
        *,
        repeat_index: int,
        run_id: str,
        suite_paths: list[Path],
        adapter: AgentRuntimeAdapter,
        options: RunOptions,
        eval_report: EvalReport,
        report_path: Path,
    ) -> None:
        LOGGER.info(
            "LIFT repeat %d/%d run_id=%s",
            repeat_index + 1,
            options.repeat,
            run_id,
        )
        repeat_run = eval_report.runs[repeat_index]

        for suite_path in suite_paths:
            config = load_lift_suite(suite_path)
            suite = config.suite
            if not suite.tasks:
                raise ValueError(f"No tasks in {suite_path}")

            warmup_tasks, holdout_tasks = split_suite_tasks(config)
            category_name = suite.category

            suite_run = SuiteRun(
                suite_name=suite.name,
                suite_path=str(suite_path.resolve()),
                category=category_name,
                tasks=[],
            )
            repeat_run.suites.append(suite_run)

            ctx = SuiteRunContext(
                run_id=run_id,
                repeat_index=repeat_index,
                suite_path=suite_path,
                category_name=category_name,
                suite_name=suite.name,
            )
            resources = await adapter.create_suite_run_resources(ctx)
            try:
                async with self._report_lock:
                    if category_name not in eval_report.categories:
                        eval_report.categories.append(category_name)

                if not warmup_tasks:
                    raise ValueError(
                        f"No warmup tasks in {suite_path}; "
                        "produce_delta requires at least one non-hold-out task"
                    )

                policy = WarmupThenUpdatePolicy(warmup_tasks=warmup_tasks)
                delta = await adapter.produce_delta(
                    resources, policy, warmup_tasks, ctx
                )

                if options.warmup_only:
                    LOGGER.info(
                        "LIFT warmup-only %s: delta committed as %s",
                        suite.name,
                        delta.image_tag,
                    )
                else:
                    for holdout_task in holdout_tasks:
                        baseline = await adapter.run_before_load(
                            holdout_task, resources, ctx, phase="baseline"
                        )
                        evolved = await adapter.run_after_load(
                            holdout_task, resources, delta, ctx
                        )
                        suite_run.tasks.append(
                            TaskRun(
                                task_name=holdout_task.name,
                                category=category_name,
                                baseline=baseline,
                                evolved=evolved,
                            )
                        )
                        LOGGER.info(
                            "LIFT hold-out %s: baseline_success=%s evolved_success=%s",
                            holdout_task.name,
                            baseline.success,
                            evolved.success,
                        )

                if options.incremental_report:
                    async with self._report_lock:
                        eval_report.write_json(report_path)
            finally:
                await resources.cleanup()

        repeat_run.completed_at = datetime.now(timezone.utc).isoformat()
