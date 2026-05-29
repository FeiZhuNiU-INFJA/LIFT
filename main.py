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
    BenchmarkSpec,
    BenchmarkTask,
    OpenClawBenchmarkPhaseRun,
    OpenClawBenchmarkReport,
    OpenClawBenchmarkRun,
    OpenClawBenchmarkRunBenchmark,
    OpenClawBenchmarkTaskRun,
)
from src.utils import short_id
from preprocess.convert_benchmark_mds_to_json import preprocess_benchmark_mds
from postprocess.run_post_process import process_report_to_outputs


def hermes_workspace(run_id: str, run_index: int, phase: str, category_name: str) -> Path:
    workspace_dir = (
        Path.cwd()
        / "results"
        / run_id
        / "outcome"
        / f"run-{run_index}"
        / phase
        / category_name
    )
    workspace_dir.mkdir(parents=True, exist_ok=True)
    return workspace_dir


def iter_benchmark_paths(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    benchmark_dir = path / "benchmarks" if path.name == "assets" else path
    return sorted(benchmark_dir.glob("**/*.json"))


def resolve_benchmark_paths(benchmark_root: Path, task_filter: str) -> list[Path]:
    all_paths = iter_benchmark_paths(benchmark_root)
    if not all_paths:
        raise ValueError(f"No benchmark json files found in {benchmark_root}")
    if task_filter == "all":
        return all_paths
    task_names = [t.strip() for t in task_filter.split(",")]
    allowed_stems = {t if t.endswith(".json") else f"{t}.json" for t in task_names}
    missing = allowed_stems - {p.name for p in all_paths}
    if missing:
        available = [p.name for p in all_paths]
        raise ValueError(
            f"--task specified non-existent benchmark(s): {sorted(missing)}. "
            f"Available: {available}"
        )
    return [p for p in all_paths if p.name in allowed_stems]


def post_process_results_dir(run_id: str) -> Path:
    results_dir = Path.cwd() / "results" / run_id
    results_dir.mkdir(parents=True, exist_ok=True)
    return results_dir


def _run_post_process_pipeline(run_id: str, report_path: Path, agent_source: str) -> None:
    """对已生成的 benchmark report JSON 执行后处理，输出 enriched JSON / CSV / HTML。"""
    results_dir = post_process_results_dir(run_id)
    try:
        enriched_json, comparison_csv, summary_csv, report_html = process_report_to_outputs(
            report_path,
            enriched_json=results_dir / f"{run_id}_enriched.json",
            comparison_csv=results_dir / f"{run_id}_comparison_metrics.csv",
            summary_csv=results_dir / f"{run_id}_summary_metrics.csv",
            report_html=results_dir / f"{run_id}_metrics_report.html",
            agent_source=agent_source,
        )
        LOGGER.info("Post-process enriched JSON: %s", enriched_json)
        LOGGER.info("Post-process comparison CSV: %s", comparison_csv)
        LOGGER.info("Post-process summary CSV: %s", summary_csv)
        LOGGER.info("Post-process HTML report: %s", report_html)
    except Exception:
        LOGGER.exception("Post-process pipeline failed.")
        LOGGER.error("Benchmark report was still saved successfully at: %s", report_path)


def evaluate_only_mode(args: argparse.Namespace) -> None:
    if not args.run_id:
        raise ValueError("--evaluate-only requires --run_id to locate the benchmark report JSON.")
    run_id = f"evobench-runid-{args.run_id}"
    report_path = Path.cwd() / "evobench-reports" / f"{run_id}.json"
    if not report_path.exists():
        raise FileNotFoundError(f"Benchmark report JSON not found: {report_path}")
    LOGGER.info("Running evaluate-only post-process for %s", report_path)
    _run_post_process_pipeline(run_id, report_path, agent_source="hermes")


def _write_report(bench_report: OpenClawBenchmarkReport, out_path: Path) -> None:
    bench_report.write_json(out_path)
    LOGGER.info("Wrote benchmark report: %s", out_path)


async def run_hermes_task_phase(
    *,
    task: BenchmarkTask,
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
) -> OpenClawBenchmarkPhaseRun:
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
    return OpenClawBenchmarkPhaseRun(
        work_session_id=work_sid,
        judge_session_id=judge_sid,
        success=success,
        content_score=content_score,
        workspace_dir=str(workspace_dir.resolve()),
    )


async def run_hermes_task_phase_batch(
    *,
    tasks: list[BenchmarkTask],
    run_id: str,
    agent: HermesAgent,
    repeat_index: int,
    phase: str,
    workspace_dir: Path,
    is_evolve_turn: bool = False,
    is_final_task: bool | None = None,
    log_label: str = "task",
) -> list[OpenClawBenchmarkPhaseRun]:
    if not tasks:
        return []

    results: list[OpenClawBenchmarkPhaseRun] = []
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


async def replay_mode(args: argparse.Namespace, benchmark_paths: list[Path]) -> None:
    if args.run_id:
        run_id = f"evobench-runid-{args.run_id}"
    else:
        run_id = f"evobench-runid-{datetime.now().strftime('%Y%m%d')}-{short_id()}"
    report_root = Path.cwd() / "evobench-reports"
    bench_report = OpenClawBenchmarkReport(run_id=run_id)

    for repeat_index in range(args.repeat):
        LOGGER.info("Starting benchmark suite run %d/%d", repeat_index + 1, args.repeat)
        suite_run = OpenClawBenchmarkRun()
        bench_report.runs.append(suite_run)

        async def run_benchmark_path(benchmark_path: Path) -> OpenClawBenchmarkRunBenchmark:
            benchmark = BenchmarkSpec.from_json_file(benchmark_path)
            if not benchmark.tasks:
                raise ValueError(f"No tasks found in {benchmark_path}")

            category_name = benchmark.category
            baseline_workspace = hermes_workspace(run_id, repeat_index, "baseline", category_name)
            evolved_workspace = hermes_workspace(run_id, repeat_index, "evolved", category_name)

            LOGGER.info("Running benchmark: %s (suite run %d)", benchmark_path, repeat_index)

            baseline_agent = await HermesAgent.create(workspace_path=baseline_workspace)
            try:
                baseline_tasks = benchmark.tasks[:1] if args.test else benchmark.tasks
                baseline_results = await run_hermes_task_phase_batch(
                    tasks=baseline_tasks,
                    run_id=run_id,
                    agent=baseline_agent,
                    repeat_index=repeat_index,
                    phase="baseline",
                    workspace_dir=baseline_workspace,
                    log_label="task",
                )

                benchmark_run = OpenClawBenchmarkRunBenchmark(
                    benchmark_name=benchmark.name,
                    benchmark_path=str(benchmark_path.resolve()),
                    category=category_name,
                    tasks=[],
                )
                for task, baseline_result in zip(baseline_tasks, baseline_results):
                    benchmark_run.tasks.append(
                        OpenClawBenchmarkTaskRun(
                            task_name=task.name,
                            category=category_name,
                            baseline=baseline_result,
                            evolved=None,
                        )
                    )

                if args.test:
                    LOGGER.info("Test mode, stopping after baseline task %s", baseline_tasks[0].name)
                    return benchmark_run

                LOGGER.info(
                    "Triggering agent evolution for category=%s suite_run=%d...",
                    category_name,
                    repeat_index,
                )
                await HermesAgent.evolve(f"evolve-run-{repeat_index}-{category_name}-{short_id()}")

                baseline_agent._workspace_path = evolved_workspace
                evolved_results = await run_hermes_task_phase_batch(
                    tasks=benchmark.tasks,
                    run_id=run_id,
                    agent=baseline_agent,
                    repeat_index=repeat_index,
                    phase="evolved",
                    workspace_dir=evolved_workspace,
                    is_evolve_turn=True,
                    log_label="task",
                )
                for idx, evolved_result in enumerate(evolved_results):
                    row = benchmark_run.tasks[idx]
                    benchmark_run.tasks[idx] = row.model_copy(
                        update={"evolved": evolved_result}
                    )

                return benchmark_run
            finally:
                await baseline_agent.aclose()

        if args.parallel:
            benchmark_runs = await asyncio.gather(
                *[run_benchmark_path(p) for p in benchmark_paths]
            )
            for br in benchmark_runs:
                if br.category and br.category not in bench_report.categories:
                    bench_report.categories.append(br.category)
                suite_run.benchmarks.append(br)
        else:
            for benchmark_path in benchmark_paths:
                br = await run_benchmark_path(benchmark_path)
                if br.category and br.category not in bench_report.categories:
                    bench_report.categories.append(br.category)
                suite_run.benchmarks.append(br)
                _write_report(bench_report, report_root / f"{run_id}.json")

        suite_run.completed_at = datetime.now(timezone.utc).isoformat()
        if args.test:
            break

    bench_report.completed_at = datetime.now(timezone.utc).isoformat()
    out_path = report_root / f"{run_id}.json"
    _write_report(bench_report, out_path)

    if args.evaluate:
        _run_post_process_pipeline(run_id, out_path, agent_source="hermes")


async def exam_mode(args: argparse.Namespace, benchmark_paths: list[Path]) -> None:
    if args.test:
        LOGGER.info("Exam mode ignores --test and always runs the full benchmark workflow.")

    if args.run_id:
        run_id = f"evobench-runid-{args.run_id}"
    else:
        run_id = f"evobench-runid-{datetime.now().strftime('%Y%m%d')}-{short_id()}"
    report_root = Path.cwd() / "evobench-reports"
    bench_report = OpenClawBenchmarkReport(run_id=run_id)

    for repeat_index in range(args.repeat):
        LOGGER.info("Starting exam benchmark suite run %d/%d", repeat_index + 1, args.repeat)
        suite_run = OpenClawBenchmarkRun()
        bench_report.runs.append(suite_run)

        async def run_benchmark_path(benchmark_path: Path) -> OpenClawBenchmarkRunBenchmark:
            benchmark = BenchmarkSpec.from_json_file(benchmark_path)
            if not benchmark.tasks:
                raise ValueError(f"No tasks found in {benchmark_path}")

            category_name = benchmark.category
            baseline_workspace = hermes_workspace(run_id, repeat_index, "baseline", category_name)
            baseline_final_workspace = hermes_workspace(run_id, repeat_index, "baseline-final", category_name)

            LOGGER.info("Running exam benchmark: %s (suite run %d)", benchmark_path, repeat_index)

            warmup_tasks = benchmark.tasks[:-1]
            final_task = benchmark.tasks[-1]

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
                    "Triggering exam evolution for category=%s suite_run=%d after %d warmup tasks...",
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

                benchmark_run = OpenClawBenchmarkRunBenchmark(
                    benchmark_name=benchmark.name,
                    benchmark_path=str(benchmark_path.resolve()),
                    category=category_name,
                    tasks=[
                        OpenClawBenchmarkTaskRun(
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
                return benchmark_run
            finally:
                if baseline_final_agent is not None:
                    await baseline_final_agent.aclose()
                await warmup_agent.aclose()

        if args.parallel:
            benchmark_runs = await asyncio.gather(
                *[run_benchmark_path(p) for p in benchmark_paths]
            )
            for br in benchmark_runs:
                if br.category and br.category not in bench_report.categories:
                    bench_report.categories.append(br.category)
                suite_run.benchmarks.append(br)
        else:
            for benchmark_path in benchmark_paths:
                br = await run_benchmark_path(benchmark_path)
                if br.category and br.category not in bench_report.categories:
                    bench_report.categories.append(br.category)
                suite_run.benchmarks.append(br)
                _write_report(bench_report, report_root / f"{run_id}.json")

        suite_run.completed_at = datetime.now(timezone.utc).isoformat()

    bench_report.completed_at = datetime.now(timezone.utc).isoformat()
    out_path = report_root / f"{run_id}.json"
    _write_report(bench_report, out_path)

    if args.evaluate:
        _run_post_process_pipeline(run_id, out_path, agent_source="hermes")


async def hermes_main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=("replay", "exam"),
        default="replay",
        help="Execution mode. 'replay' runs the benchmark replay pipeline, 'exam' is the exam workflow.",
    )
    parser.add_argument(
        "--benchmark",
        default="assets/benchmarks",
        help="Benchmark file or directory to run.",
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
        "--task",
        default="all",
        help="Comma-separated benchmark json filenames (without .json) to run, or 'all' to run every benchmark. Default: all",
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

    preprocess_benchmark_mds()

    benchmark_paths = resolve_benchmark_paths(Path(args.benchmark), args.task)

    if args.mode == "replay":
        await replay_mode(args, benchmark_paths)
        return
    if args.mode == "exam":
        await exam_mode(args, benchmark_paths)
        return
    raise ValueError(f"Unsupported mode: {args.mode}")


if __name__ == "__main__":
    asyncio.run(hermes_main())
