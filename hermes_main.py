from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from src.agents import HermesAgent
from src.config import LOGGER
from src.eval_core import run_task
from src.models import (
    SuiteSpec,
    SuiteTask,
    EvalRepeat,
    EvalReport,
    PhaseRun,
    SuiteRun,
    TaskRun,
)
from src.utils import make_run_id, outcome_workspace, resolve_suite_paths, short_id
from preprocess.convert_suite_mds_to_json import preprocess_suite_mds
from postprocess.run_post_process import run_post_process_pipeline


def evaluate_only_mode(args: argparse.Namespace) -> None:
    if not args.run_id:
        raise ValueError("--evaluate-only requires --run_id to locate the benchmark report JSON.")
    run_id = make_run_id(args.run_id)
    report_path = Path.cwd() / "evobench-reports" / f"{run_id}.json"
    if not report_path.exists():
        raise FileNotFoundError(f"Benchmark report JSON not found: {report_path}")
    LOGGER.info("Running evaluate-only post-process for %s", report_path)
    run_post_process_pipeline(run_id, report_path, agent_source="hermes")


def _write_report(eval_report: EvalReport, out_path: Path) -> None:
    eval_report.write_json(out_path)
    LOGGER.info("Wrote benchmark report: %s", out_path)


async def run_hermes_task_phase(
    *,
    task: SuiteTask,
    run_id: str,
    agent: HermesAgent,
    user_session_id: str,
    judge_session_id: str,
    repeat_index: int,
    phase: str,
    workspace_dir: Path,
    task_index: int,
    total_tasks: int,
    is_evolve_turn: bool = False,
    is_final_task: bool | None = None,
    log_label: str = "task",
) -> PhaseRun:
    LOGGER.info(
        "Running %s %s: %s with run_id: %s suite_run: %d workspace: %s",
        phase,
        log_label,
        task.name,
        run_id,
        repeat_index,
        workspace_dir,
    )
    agent.copy_task_assets(
        task.requirements.extra_skills_dir,
        task.requirements.material_dir,
    )
    success, work_sid, judge_sid, content_score = await run_task(
        task,
        run_id,
        agent,
        user_session_id=user_session_id,
        judge_session_id=judge_session_id,
        is_evolve_turn=is_evolve_turn,
        is_final_task=(task_index == total_tasks - 1) if is_final_task is None else is_final_task,
    )
    agent.reset_pre_chat_state()
    LOGGER.info("%s %s %s: success: %s", phase.capitalize(), log_label, task.name, success)
    return PhaseRun(
        work_session_id=work_sid,
        judge_session_id=judge_sid,
        success=success,
        content_score=content_score,
        workspace_dir=str(workspace_dir.resolve()),
    )


async def run_hermes_task_phase_batch(
    *,
    tasks: list[SuiteTask],
    run_id: str,
    agent: HermesAgent,
    repeat_index: int,
    phase: str,
    workspace_dir: Path,
    is_evolve_turn: bool = False,
    is_final_task: bool | None = None,
    log_label: str = "task",
) -> list[PhaseRun]:
    if not tasks:
        return []

    results: list[PhaseRun] = []
    for idx, task in enumerate(tasks):
        user_session_id = f"user-{short_id()}"
        judge_session_id = f"judge-{short_id()}"
        results.append(
            await run_hermes_task_phase(
                task=task,
                run_id=run_id,
                agent=agent,
                user_session_id=user_session_id,
                judge_session_id=judge_session_id,
                repeat_index=repeat_index,
                phase=phase,
                workspace_dir=workspace_dir,
                task_index=idx,
                total_tasks=len(tasks),
                is_evolve_turn=is_evolve_turn,
                is_final_task=is_final_task,
                log_label=log_label,
            )
        )
    return results


async def replay_mode(args: argparse.Namespace, suite_paths: list[Path]) -> None:
    run_id = make_run_id(args.run_id)
    report_root = Path.cwd() / "evobench-reports"
    eval_report = EvalReport(run_id=run_id)

    for repeat_index in range(args.repeat):
        LOGGER.info("Starting benchmark suite run %d/%d", repeat_index + 1, args.repeat)
        repeat_run = EvalRepeat()
        eval_report.runs.append(repeat_run)

        async def run_suite_path(suite_path: Path) -> SuiteRun:
            suite = SuiteSpec.from_json_file(suite_path)
            if not suite.tasks:
                raise ValueError(f"No tasks found in {suite_path}")

            category_name = suite.category
            baseline_workspace = outcome_workspace(run_id, repeat_index, "baseline", category_name)
            evolved_workspace = outcome_workspace(run_id, repeat_index, "evolved", category_name)

            LOGGER.info("Running suite: %s (repeat %d)", suite_path, repeat_index)

            baseline_agent = await HermesAgent.create(workspace_path=baseline_workspace)
            try:
                baseline_tasks = suite.tasks[:1] if args.test else suite.tasks
                baseline_results = await run_hermes_task_phase_batch(
                    tasks=baseline_tasks,
                    run_id=run_id,
                    agent=baseline_agent,
                    repeat_index=repeat_index,
                    phase="baseline",
                    workspace_dir=baseline_workspace,
                    log_label="task",
                )

                suite_run = SuiteRun(
                    suite_name=suite.name,
                    suite_path=str(suite_path.resolve()),
                    category=category_name,
                    tasks=[],
                )
                for task, baseline_result in zip(baseline_tasks, baseline_results):
                    suite_run.tasks.append(
                        TaskRun(
                            task_name=task.name,
                            category=category_name,
                            baseline=baseline_result,
                            evolved=None,
                        )
                    )

                if args.test:
                    LOGGER.info("Test mode, stopping after baseline task %s", baseline_tasks[0].name)
                    return suite_run

                LOGGER.info(
                    "Triggering agent evolution for category=%s suite_run=%d...",
                    category_name,
                    repeat_index,
                )
                await HermesAgent.evolve(f"evolve-run-{repeat_index}-{category_name}-{short_id()}")

                baseline_agent._workspace_path = evolved_workspace
                evolved_results = await run_hermes_task_phase_batch(
                    tasks=suite.tasks,
                    run_id=run_id,
                    agent=baseline_agent,
                    repeat_index=repeat_index,
                    phase="evolved",
                    workspace_dir=evolved_workspace,
                    is_evolve_turn=True,
                    log_label="task",
                )
                for idx, evolved_result in enumerate(evolved_results):
                    row = suite_run.tasks[idx]
                    suite_run.tasks[idx] = row.model_copy(
                        update={"evolved": evolved_result}
                    )

                return suite_run
            finally:
                await baseline_agent.aclose()

        if args.parallel:
            suite_runs = await asyncio.gather(
                *[run_suite_path(p) for p in suite_paths]
            )
            for sr in suite_runs:
                if sr.category and sr.category not in eval_report.categories:
                    eval_report.categories.append(sr.category)
                repeat_run.suites.append(sr)
        else:
            for suite_path in suite_paths:
                sr = await run_suite_path(suite_path)
                if sr.category and sr.category not in eval_report.categories:
                    eval_report.categories.append(sr.category)
                repeat_run.suites.append(sr)
                _write_report(eval_report, report_root / f"{run_id}.json")

        repeat_run.completed_at = datetime.now(timezone.utc).isoformat()
        if args.test:
            break

    eval_report.completed_at = datetime.now(timezone.utc).isoformat()
    out_path = report_root / f"{run_id}.json"
    _write_report(eval_report, out_path)

    if args.evaluate:
        run_post_process_pipeline(run_id, out_path, agent_source="hermes")


async def exam_mode(args: argparse.Namespace, suite_paths: list[Path]) -> None:
    if args.test:
        LOGGER.info("Exam mode ignores --test and always runs the full benchmark workflow.")

    run_id = make_run_id(args.run_id)
    report_root = Path.cwd() / "evobench-reports"
    eval_report = EvalReport(run_id=run_id)

    for repeat_index in range(args.repeat):
        LOGGER.info("Starting exam benchmark suite run %d/%d", repeat_index + 1, args.repeat)
        repeat_run = EvalRepeat()
        eval_report.runs.append(repeat_run)

        async def run_suite_path(suite_path: Path) -> SuiteRun:
            suite = SuiteSpec.from_json_file(suite_path)
            if not suite.tasks:
                raise ValueError(f"No tasks found in {suite_path}")

            category_name = suite.category
            baseline_workspace = outcome_workspace(run_id, repeat_index, "baseline", category_name)
            baseline_final_workspace = outcome_workspace(
                run_id, repeat_index, "baseline-final", category_name
            )

            LOGGER.info("Running exam suite: %s (repeat %d)", suite_path, repeat_index)

            warmup_tasks = suite.tasks[:-1]
            final_task = suite.tasks[-1]

            warmup_agent = await HermesAgent.create(workspace_path=baseline_workspace)
            baseline_final_agent: HermesAgent | None = None
            try:
                warmup_results = await run_hermes_task_phase_batch(
                    tasks=warmup_tasks,
                    run_id=run_id,
                    agent=warmup_agent,
                    repeat_index=repeat_index,
                    phase="baseline",
                    workspace_dir=baseline_workspace,
                    is_final_task=False,
                    log_label="exam warmup task",
                )
                for task, warmup_result in zip(warmup_tasks, warmup_results):
                    LOGGER.info(
                        "Exam warmup task %s finished: success=%s work_session=%s judge_session=%s",
                        task.name,
                        warmup_result.success,
                        warmup_result.work_session_id,
                        warmup_result.judge_session_id,
                    )

                LOGGER.info(
                    "Triggering exam evolution for category=%s repeat=%d after %d warmup tasks...",
                    category_name,
                    repeat_index,
                    len(warmup_tasks),
                )
                await HermesAgent.evolve(f"exam-evolve-run-{repeat_index}-{category_name}-{short_id()}")

                baseline_final_agent = await HermesAgent.create(workspace_path=baseline_final_workspace)
                baseline_final_result = (
                    await run_hermes_task_phase_batch(
                        tasks=[final_task],
                        run_id=run_id,
                        agent=baseline_final_agent,
                        repeat_index=repeat_index,
                        phase="baseline",
                        workspace_dir=baseline_final_workspace,
                        is_evolve_turn=False,
                        is_final_task=True,
                        log_label="exam final task",
                    )
                )[0]

                evolved_final_result = (
                    await run_hermes_task_phase_batch(
                        tasks=[final_task],
                        run_id=run_id,
                        agent=warmup_agent,
                        repeat_index=repeat_index,
                        phase="evolved",
                        workspace_dir=baseline_workspace,
                        is_evolve_turn=True,
                        is_final_task=True,
                        log_label="exam final task",
                    )
                )[0]

                suite_run = SuiteRun(
                    suite_name=suite.name,
                    suite_path=str(suite_path.resolve()),
                    category=category_name,
                    tasks=[
                        TaskRun(
                            task_name=final_task.name,
                            category=category_name,
                            baseline=baseline_final_result,
                            evolved=evolved_final_result,
                        )
                    ],
                )
                LOGGER.info(
                    "Exam final task %s completed: baseline_success=%s evolved_success=%s",
                    final_task.name,
                    baseline_final_result.success,
                    evolved_final_result.success,
                )
                return suite_run
            finally:
                if baseline_final_agent is not None:
                    await baseline_final_agent.aclose()
                await warmup_agent.aclose()

        if args.parallel:
            suite_runs = await asyncio.gather(
                *[run_suite_path(p) for p in suite_paths]
            )
            for sr in suite_runs:
                if sr.category and sr.category not in eval_report.categories:
                    eval_report.categories.append(sr.category)
                repeat_run.suites.append(sr)
        else:
            for suite_path in suite_paths:
                sr = await run_suite_path(suite_path)
                if sr.category and sr.category not in eval_report.categories:
                    eval_report.categories.append(sr.category)
                repeat_run.suites.append(sr)
                _write_report(eval_report, report_root / f"{run_id}.json")

        repeat_run.completed_at = datetime.now(timezone.utc).isoformat()

    eval_report.completed_at = datetime.now(timezone.utc).isoformat()
    out_path = report_root / f"{run_id}.json"
    _write_report(eval_report, out_path)

    if args.evaluate:
        run_post_process_pipeline(run_id, out_path, agent_source="hermes")


async def hermes_main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=("replay", "exam"),
        default="replay",
        help="Execution mode. 'replay' runs the benchmark replay pipeline, 'exam' is the exam workflow.",
    )
    parser.add_argument(
        "--benchmark_dir",
        default="assets/benchmarks",
        help="Directory containing suite JSON files.",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Test mode, only run one benchmark task.",
    )
    parser.add_argument(
        "-e",
        "--evaluate",
        action="store_true",
        help="After writing the benchmark report JSON, run the post-process pipeline and generate CSV/HTML outputs.",
    )
    parser.add_argument(
        "--evaluate-only",
        action="store_true",
        help="Skip benchmark execution and only run the post-process pipeline against an existing benchmark report JSON. Requires --run_id.",
    )
    parser.add_argument(
        "--suite",
        default="all",
        help="Comma-separated suite JSON filenames (with or without .json), or 'all' for every file in --benchmark_dir.",
    )
    parser.add_argument(
        "--run_id",
        default=None,
        help="Custom run_id suffix. When set, the run_id becomes 'evobench-runid-{run_id}'. Default: auto-generated.",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="Run the full benchmark suite N times and store each full execution under report.runs.",
    )
    parser.add_argument(
        "-p",
        "--parallel",
        action="store_true",
        help="Run benchmark paths in parallel. Tasks within each path are always serial.",
    )
    args = parser.parse_args()
    if args.repeat < 1:
        raise ValueError("--repeat must be at least 1")

    if args.evaluate_only:
        evaluate_only_mode(args)
        return

    preprocess_suite_mds()

    suite_paths = resolve_suite_paths(Path(args.benchmark_dir), args.suite)

    if args.mode == "replay":
        await replay_mode(args, suite_paths)
        return
    if args.mode == "exam":
        await exam_mode(args, suite_paths)
        return
    raise ValueError(f"Unsupported mode: {args.mode}")


if __name__ == "__main__":
    asyncio.run(hermes_main())
