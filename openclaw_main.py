from __future__ import annotations

import argparse
import asyncio
import shutil
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from src.config import LOGGER
from src.eval_core import openclaw_run_task
from src.utils import short_id

from preprocess.convert_benchmark_mds_to_json import preprocess_benchmark_mds
from postprocess.run_post_process import process_report_to_outputs
from src.agents import OpenClawAgent
from src.models import (
    BenchmarkSpec,
    BenchmarkTask,
    OpenClawBenchmarkPhaseRun,
    OpenClawBenchmarkReport,
    OpenClawBenchmarkRun,
    OpenClawBenchmarkRunBenchmark,
    OpenClawBenchmarkTaskRun,
)



def openclaw_workspace(run_id: str, run_index: int, phase: str, category_name: str) -> Path:
    workspace_dir = Path("/tmp") / run_id / f"run-{run_index}" / phase / category_name
    workspace_dir.mkdir(parents=True, exist_ok=True)
    return workspace_dir


def openclaw_create_agent_factory(
    run_id: str,
    run_index: int,
    phase: str,
    task: BenchmarkTask,
    workspace_dir: Path
):
    def create_agent(session_role: str) -> OpenClawAgent:
        task_id = f"run-{run_index}-{phase}-{task.category_name}-{task.name}-{session_role}"
        agent_name = f"evobench-agent_name-{short_id()}"
        return OpenClawAgent(
            run_id=run_id,
            task_id=task_id,
            agent_name=agent_name,
            skills_dir=task.requirements.extra_skills_dir,
            material_dir=task.requirements.material_dir,
            workspace_dir=workspace_dir,
        )

    return create_agent


def openclaw_copy_evolved_skills(source_workspace: Path, target_workspace: Path) -> None:
    source_skills_dir = source_workspace / "skills"
    if not source_skills_dir.exists():
        LOGGER.warning("No skills directory found after evolution: %s", source_skills_dir)
        return
    target_skills_dir = target_workspace / "skills"
    shutil.copytree(source_skills_dir, target_skills_dir, dirs_exist_ok=True)
    LOGGER.info("Copied evolved skills: %s -> %s", source_skills_dir, target_skills_dir)


def iter_benchmark_paths(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    benchmark_dir = path / "benchmarks" if path.name == "assets" else path
    return sorted(benchmark_dir.glob("**/*.json"))

def post_process_results_dir(run_id: str) -> Path:
    results_dir = Path.cwd() / "results" / run_id
    results_dir.mkdir(parents=True, exist_ok=True)
    return results_dir

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


def reset_benchmark_evolution_state(run_id: str, category_name: str, repeat_index: int) -> None:
    LOGGER.info(
        "Resetting evolution state after benchmark cycle for category=%s suite_run=%d",
        category_name,
        repeat_index,
    )
    OpenClawAgent.reset_evolve(
        run_id=run_id,
        category=category_name,
        repeat_index=repeat_index,
    )


def _create_agents_for_task(
    task: BenchmarkTask,
    run_id: str,
    repeat_index: int,
    phase: str,
    workspace_dir: Path,
) -> tuple[OpenClawAgent, OpenClawAgent, str, str]:
    agent_factory = openclaw_create_agent_factory(
        run_id=run_id,
        run_index=repeat_index,
        phase=phase,
        task=task,
        workspace_dir=workspace_dir,
    )
    user_session_id = f"user-{short_id()}"
    judge_session_id = f"judge-{short_id()}"
    user_agent = agent_factory(f"user-{user_session_id}")
    judge_agent = agent_factory(f"judge-{judge_session_id}")
    user_agent.initialize()
    judge_agent.initialize()
    return user_agent, judge_agent, user_session_id, judge_session_id


async def run_openclaw_task_phase(
    *,
    task: BenchmarkTask,
    run_id: str,
    repeat_index: int,
    phase: str,
    workspace_dir: Path,
    task_index: int,
    total_tasks: int,
    is_evolve_turn: bool = False,
    is_final_task: bool | None = None,
    log_label: str = "task",
    agents: tuple[OpenClawAgent, OpenClawAgent, str, str],
) -> OpenClawBenchmarkPhaseRun:
    user_agent, judge_agent, user_session_id, judge_session_id = agents
    LOGGER.info(
        "Running %s %s: %s with run_id: %s suite_run: %d workspace: %s",
        phase,
        log_label,
        task.name,
        run_id,
        repeat_index,
        workspace_dir,
    )
    success, work_sid, judge_sid = await openclaw_run_task(
        task,
        run_id,
        user_agent=user_agent,
        judge_agent=judge_agent,
        user_session_id=user_session_id,
        judge_session_id=judge_session_id,
        is_evolve_turn=is_evolve_turn,
        is_final_task=(task_index == total_tasks - 1) if is_final_task is None else is_final_task,
    )
    LOGGER.info("%s %s %s: success: %s", phase.capitalize(), log_label, task.name, success)
    return OpenClawBenchmarkPhaseRun(
        work_session_id=work_sid,
        judge_session_id=judge_sid,
        success=success,
        workspace_dir=str(workspace_dir.resolve()),
    )


async def run_openclaw_task_phase_batch(
    *,
    tasks: list[BenchmarkTask],
    run_id: str,
    repeat_index: int,
    phase: str,
    workspace_dir: Path,
    parallel: bool,
    is_evolve_turn: bool = False,
    is_final_task: bool | None = None,
    log_label: str = "task",
) -> list[OpenClawBenchmarkPhaseRun]:
    if not tasks:
        return []

    LOGGER.info(
        "Serially creating agents for %d %s %s(s) suite_run=%d workspace=%s",
        len(tasks),
        phase,
        log_label,
        repeat_index,
        workspace_dir,
    )
    all_agents: list[tuple[OpenClawAgent, OpenClawAgent, str, str]] = []
    for task in tasks:
        all_agents.append(
            _create_agents_for_task(
                task=task,
                run_id=run_id,
                repeat_index=repeat_index,
                phase=phase,
                workspace_dir=workspace_dir,
            )
        )

    async def run_one(idx: int, task: BenchmarkTask) -> OpenClawBenchmarkPhaseRun:
        return await run_openclaw_task_phase(
            task=task,
            run_id=run_id,
            repeat_index=repeat_index,
            phase=phase,
            workspace_dir=workspace_dir,
            task_index=idx,
            total_tasks=len(tasks),
            is_evolve_turn=is_evolve_turn,
            is_final_task=is_final_task,
            log_label=log_label,
            agents=all_agents[idx],
        )

    if parallel:
        LOGGER.info(
            "Running %d %s %s(s) in parallel for suite_run=%d workspace=%s",
            len(tasks),
            phase,
            log_label,
            repeat_index,
            workspace_dir,
        )
        running_tasks = [
            asyncio.create_task(run_one(idx, task))
            for idx, task in enumerate(tasks)
        ]
        return await asyncio.gather(*running_tasks)

    results: list[OpenClawBenchmarkPhaseRun] = []
    for idx, task in enumerate(tasks):
        results.append(await run_one(idx, task))
    return results


def _write_report(bench_report: OpenClawBenchmarkReport, out_path: Path) -> None:
    bench_report.write_json(out_path)
    LOGGER.info("Wrote benchmark report: %s", out_path)


async def replay_mode(args: argparse.Namespace, benchmark_paths: list[Path]) -> None:
    OpenClawAgent.set_trace_plugin(args.trace)
    OpenClawAgent.initialize_environment(
        ensure_config_fields=True, # 确保model的compat字段存在，并且langfuse-plugin的hooks存在
        trace_plugin=args.trace, # 开启trace插件
        restart_gateway=True, # 更改插件时是否重启gateway
    )

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

        for benchmark_path in benchmark_paths:
            benchmark = BenchmarkSpec.from_json_file(benchmark_path)
            if not benchmark.tasks:
                raise ValueError(f"No tasks found in {benchmark_path}")

            category_name = benchmark.category
            if category_name not in bench_report.categories:
                bench_report.categories.append(category_name)
            benchmark_run = OpenClawBenchmarkRunBenchmark(
                benchmark_name=benchmark.name,
                benchmark_path=str(benchmark_path.resolve()),
                category=category_name,
                tasks=[],
            )
            suite_run.benchmarks.append(benchmark_run)
            baseline_workspace = openclaw_workspace(run_id, repeat_index, "baseline", category_name)
            evolved_workspace = openclaw_workspace(run_id, repeat_index, "evolved", category_name)

            LOGGER.info("Running benchmark: %s (suite run %d)", benchmark_path, repeat_index)

            baseline_tasks = benchmark.tasks[:1] if args.test else benchmark.tasks
            baseline_results = await run_openclaw_task_phase_batch(
                tasks=baseline_tasks,
                run_id=run_id,
                repeat_index=repeat_index,
                phase="baseline",
                workspace_dir=baseline_workspace,
                parallel=args.parallel,
                log_label="task",
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
                break

            LOGGER.info("Triggering baseline agent evolution for category=%s suite_run=%d...", category_name, repeat_index)
            await OpenClawAgent.evolve(f"evolve-run-{repeat_index}-{category_name}-{short_id()}")
            # openclaw_copy_evolved_skills(baseline_workspace, evolved_workspace)

            evolved_results = await run_openclaw_task_phase_batch(
                tasks=benchmark.tasks,
                run_id=run_id,
                repeat_index=repeat_index,
                phase="evolved",
                workspace_dir=evolved_workspace,
                parallel=args.parallel,
                is_evolve_turn=True,
                log_label="task",
            )
            for idx, evolved_result in enumerate(evolved_results):
                row = benchmark_run.tasks[idx]
                benchmark_run.tasks[idx] = row.model_copy(
                    update={
                        "evolved": evolved_result
                    }
                )

            reset_benchmark_evolution_state(run_id, category_name, repeat_index)
            _write_report(bench_report, report_root / f"{run_id}.json")

        suite_run.completed_at = datetime.now(timezone.utc).isoformat()
        if args.test:
            break

    bench_report.completed_at = datetime.now(timezone.utc).isoformat()
    out_path = report_root / f"{run_id}.json"
    _write_report(bench_report, out_path)

    if args.evaluate:
        results_dir = post_process_results_dir(run_id)
        try:
            enriched_json, comparison_csv, summary_csv, report_html = process_report_to_outputs(
                out_path,
                enriched_json=results_dir / f"{run_id}_enriched.json",
                comparison_csv=results_dir / f"{run_id}_comparison_metrics.csv",
                summary_csv=results_dir / f"{run_id}_summary_metrics.csv",
                report_html=results_dir / f"{run_id}_metrics_report.html",
            )
            LOGGER.info("Post-process enriched JSON: %s", enriched_json)
            LOGGER.info("Post-process comparison CSV: %s", comparison_csv)
            LOGGER.info("Post-process summary CSV: %s", summary_csv)
            LOGGER.info("Post-process HTML report: %s", report_html)
        except Exception:
            LOGGER.exception("Post-process pipeline failed after benchmark completion.")
            LOGGER.error("Benchmark report was still saved successfully at: %s", out_path)


async def exam_mode(args: argparse.Namespace, benchmark_paths: list[Path]) -> None:
    if args.test:
        LOGGER.info("Exam mode ignores --test and always runs the full benchmark workflow.")

    OpenClawAgent.set_trace_plugin(args.trace)
    OpenClawAgent.initialize_environment(
        ensure_config_fields=True,  # 确保model的compat字段存在，并且langfuse-plugin的hooks存在
        trace_plugin=args.trace,  # 开启trace插件
        restart_gateway=True,  # 更改插件时是否重启gateway
    )

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

        for benchmark_path in benchmark_paths:
            benchmark = BenchmarkSpec.from_json_file(benchmark_path)
            if not benchmark.tasks:
                raise ValueError(f"No tasks found in {benchmark_path}")

            category_name = benchmark.category
            if category_name not in bench_report.categories:
                bench_report.categories.append(category_name)
            benchmark_run = OpenClawBenchmarkRunBenchmark(
                benchmark_name=benchmark.name,
                benchmark_path=str(benchmark_path.resolve()),
                category=category_name,
                tasks=[],
            )
            suite_run.benchmarks.append(benchmark_run)
            baseline_workspace = openclaw_workspace(run_id, repeat_index, "baseline", category_name)
            evolved_workspace = openclaw_workspace(run_id, repeat_index, "evolved", category_name)

            LOGGER.info("Running exam benchmark: %s (suite run %d)", benchmark_path, repeat_index)

            warmup_tasks = benchmark.tasks[:-1]
            final_task = benchmark.tasks[-1]

            warmup_results = await run_openclaw_task_phase_batch(
                tasks=warmup_tasks,
                run_id=run_id,
                repeat_index=repeat_index,
                phase="baseline",
                workspace_dir=baseline_workspace,
                parallel=args.parallel,
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
            await OpenClawAgent.evolve(f"exam-evolve-run-{repeat_index}-{category_name}-{short_id()}")
            # 禁用进化信息，测试final task的baseline性能
            OpenClawAgent.disable_evolve()

            baseline_final_result = (
                await run_openclaw_task_phase_batch(
                    tasks=[final_task],
                    run_id=run_id,
                    repeat_index=repeat_index,
                    phase="baseline",
                    workspace_dir=baseline_workspace,
                    parallel=args.parallel,
                    is_evolve_turn=False,
                    is_final_task=True,
                    log_label="exam final task",
                )
            )[0]
            # 启用进化，测试evolve后的性能
            OpenClawAgent.enable_evolve()

            evolved_final_result = (
                await run_openclaw_task_phase_batch(
                    tasks=[final_task],
                    run_id=run_id,
                    repeat_index=repeat_index,
                    phase="evolved",
                    workspace_dir=evolved_workspace,
                    parallel=args.parallel,
                    is_evolve_turn=True,
                    is_final_task=True,
                    log_label="exam final task",
                )
            )[0]

            benchmark_run.tasks.append(
                OpenClawBenchmarkTaskRun(
                    task_name=final_task.name,
                    category=category_name,
                    baseline=baseline_final_result,
                    evolved=evolved_final_result,
                )
            )
            LOGGER.info(
                "Exam final task %s completed: baseline_success=%s evolved_success=%s",
                final_task.name,
                baseline_final_result.success,
                evolved_final_result.success,
            )

            reset_benchmark_evolution_state(run_id, category_name, repeat_index)
            _write_report(bench_report, report_root / f"{run_id}.json")

        suite_run.completed_at = datetime.now(timezone.utc).isoformat()

    bench_report.completed_at = datetime.now(timezone.utc).isoformat()
    out_path = report_root / f"{run_id}.json"
    _write_report(bench_report, out_path)

    if args.evaluate:
        results_dir = post_process_results_dir(run_id)
        try:
            enriched_json, comparison_csv, summary_csv, report_html = process_report_to_outputs(
                out_path,
                enriched_json=results_dir / f"{run_id}_enriched.json",
                comparison_csv=results_dir / f"{run_id}_comparison_metrics.csv",
                summary_csv=results_dir / f"{run_id}_summary_metrics.csv",
                report_html=results_dir / f"{run_id}_metrics_report.html",
            )
            LOGGER.info("Post-process enriched JSON: %s", enriched_json)
            LOGGER.info("Post-process comparison CSV: %s", comparison_csv)
            LOGGER.info("Post-process summary CSV: %s", summary_csv)
            LOGGER.info("Post-process HTML report: %s", report_html)
        except Exception:
            LOGGER.exception("Post-process pipeline failed after benchmark completion.")
            LOGGER.error("Benchmark report was still saved successfully at: %s", out_path)


async def openclaw_main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=("replay", "exam"),
        default="replay",
        help="Execution mode. 'replay' runs the benchmark replay pipeline, 'exam' is reserved for the future exam workflow.",
    )
    parser.add_argument(
        "--benchmark",
        default="assets/benchmarks",
        help="Benchmark file or directory to run.",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Test mode, only run one benchmark.",
    )
    parser.add_argument(
        "--trace",
        choices=("fornax", "langfuse"),
        default="langfuse",
        help="Trace plugin to enable during initialization.",
    )
    parser.add_argument(
        "-e",
        "--evaluate",
        action="store_true",
        help="After writing the benchmark report JSON, run the post-process pipeline and generate CSV/HTML outputs.",
    )
    parser.add_argument(
        "--task",
        default="all",
        help="Comma-separated benchmark json filenames (without .json) to run, or 'all' to run every benchmark. Default: all",
    )
    parser.add_argument(
        "--run_id",
        default=None,
        help="Custom run_id suffix. When set, the run_id becomes 'evobench-runid-{run_id}'. Default: auto-generated with date and short id.",
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
        help="Run tasks within each benchmark in parallel. Incompatible with the fornax tracer.",
    )
    args = parser.parse_args()
    if args.repeat < 1:
        raise ValueError("--repeat must be at least 1")
    if args.parallel and args.trace == "fornax":
        LOGGER.warning("Parallel mode is incompatible with the fornax tracer because it relies on shared gateway/env state.")
        raise ValueError("--parallel/-p is incompatible with --trace fornax. Use --trace langfuse or disable --parallel.")

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
    asyncio.run(openclaw_main())
