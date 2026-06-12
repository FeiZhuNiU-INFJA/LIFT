"""LIFT 主流程编排：repeat × suite → warmup/delta → hold-out 对照。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from src.config import LOGGER
from src.models import EvalRepeat, EvalReport, SuiteRun, TaskRun

from src.lift.adapters.base import AgentRuntimeAdapter, SuiteRunContext
from src.lift.eval.task_exec import bounded_gather
from src.lift.policies.artifact import WarmupThenUpdatePolicy
from src.lift.pipeline.run_options import RunOptions
from src.lift.runtime.delta_ref import DeltaRef
from src.lift.runtime.suite_run_resources import SuiteRunResources
from src.lift.suite.holdout import split_suite_tasks
from src.lift.suite.lift_suite import load_lift_suite
from src.models import SuiteTask
from src.paths import report_json_path, results_run_dir


class LIFTPipeline:
    """Loaded Impact on Final Task orchestration."""

    def __init__(self) -> None:
        self._report_lock = asyncio.Lock()  # 并行 repeat 时保护 report 增量写入

    async def run(
        self,
        *,
        run_id: str,
        suite_paths: list[Path],
        adapter: AgentRuntimeAdapter,
        options: RunOptions,
    ) -> EvalReport:
        """执行完整 LIFT 流程并写出 ``EvalReport`` JSON。"""
        eval_report = EvalReport(run_id=run_id)
        report_path = report_json_path(run_id)
        results_run_dir(run_id).mkdir(parents=True, exist_ok=True)
        eval_report.runs = [EvalRepeat() for _ in range(options.repeat)]

        # 多 repeat 默认并行；max_parallel_repeats=1 时串行；否则受其值上限约束
        if options.repeat > 1 and options.max_parallel_repeats != 1:
            await bounded_gather(
                (
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
                ),
                limit=options.max_parallel_repeats,
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
        """单轮 repeat 内依次跑所有 suite（warmup → hold-out）。"""
        LOGGER.info(
            "LIFT repeat %d/%d run_id=%s",
            repeat_index + 1,
            options.repeat,
            run_id,
        )
        repeat_run = eval_report.runs[repeat_index]

        for suite_path in suite_paths:
            suite = load_lift_suite(suite_path)
            warmup_tasks, holdout_tasks = split_suite_tasks(suite)
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
            # 本 suite 的资源簿：track 容器、存 delta；suite 结束 finally 里 cleanup
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
                # warmup 容器在 produce_delta 内部已 cleanup；delta 镜像留给 hold-out
                delta = await adapter.produce_delta(
                    resources, policy, warmup_tasks, ctx
                )

                if options.warmup_only:
                    # 只产 delta，不跑 before/after-load 对照
                    LOGGER.info(
                        "LIFT warmup-only %s: delta committed as %s",
                        suite.name,
                        delta.image_tag,
                    )
                else:
                    task_runs = await self._run_holdout_tasks(
                        adapter=adapter,
                        holdout_tasks=holdout_tasks,
                        resources=resources,
                        delta=delta,
                        ctx=ctx,
                        category_name=category_name,
                        options=options,
                    )
                    suite_run.tasks.extend(task_runs)

                # 每个 suite 完成后落盘：长跑中断时仍可从磁盘恢复部分 report
                async with self._report_lock:
                    eval_report.write_json(report_path)
            finally:
                # 删本 suite 登记的容器；delta 镜像也在 resources.cleanup 里 rmi
                await resources.cleanup()

        repeat_run.completed_at = datetime.now(timezone.utc).isoformat()

    async def _run_holdout_tasks(
        self,
        *,
        adapter: AgentRuntimeAdapter,
        holdout_tasks: list[SuiteTask],
        resources: SuiteRunResources,
        delta: DeltaRef,
        ctx: SuiteRunContext,
        category_name: str,
        options: RunOptions,
    ) -> list[TaskRun]:
        """按 ``holdout_container_policy`` 串行 / 并行执行 hold-out 多题。

        每题内部 baseline → evolved 仍然顺序执行（同题镜像分裂会读写同一 workspace
        子目录，且需要先后对照语义）；多题之间按 policy 决定是否 ``asyncio.gather``。
        """

        async def _one_task(task: SuiteTask) -> TaskRun:
            baseline = await adapter.run_before_load(task, resources, ctx)
            evolved = await adapter.run_after_load(task, resources, delta, ctx)
            LOGGER.info(
                "LIFT hold-out %s: baseline_success=%s evolved_success=%s",
                task.name,
                baseline.success,
                evolved.success,
            )
            return TaskRun(
                task_name=task.name,
                category=category_name,
                baseline=baseline,
                evolved=evolved,
            )

        if options.holdout_container_policy.tasks_parallel:
            return await bounded_gather(
                (_one_task(t) for t in holdout_tasks),
                limit=options.max_concurrent_tasks,
            )
        return [await _one_task(t) for t in holdout_tasks]
