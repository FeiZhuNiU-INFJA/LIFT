from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

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
from src.config import LOGGER
from src.eval_core import openclaw_run_task
from src.utils import short_id


load_dotenv()


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
        return OpenClawAgent(
            run_id=run_id,
            task_id=task_id,
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


async def replay_mode(args: argparse.Namespace) -> None:
    # 解析markdown文件为benchmark json
    generated_benchmarks = preprocess_benchmark_mds()
    # LOGGER.info("Preprocessed markdown benchmarks into json: %s", generated_benchmarks)

    OpenClawAgent.initialize_environment(
        ensure_config_fields=True, # 确保model的compat字段存在，并且langfuse-plugin的hooks存在
        trace_plugin=args.trace, # 开启trace插件
        restart_gateway=True, # 更改插件时是否重启gateway
    )

    benchmark_root = Path(args.benchmark)
    benchmark_paths = iter_benchmark_paths(benchmark_root)
    if not benchmark_paths:
        raise ValueError(f"No benchmark json files found in {benchmark_root}")
    
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

            task_start_idx = len(benchmark_run.tasks)

            for idx, task in enumerate(benchmark.tasks):
                agent_factory = openclaw_create_agent_factory(
                    run_id=run_id,
                    run_index=repeat_index,
                    phase="baseline",
                    task=task,
                    workspace_dir=baseline_workspace,
                )
                LOGGER.info(
                    "Running baseline task: %s with run_id: %s suite_run: %d workspace: %s",
                    task.name,
                    run_id,
                    repeat_index,
                    baseline_workspace,
                )
                success, work_sid, judge_sid = await openclaw_run_task(
                    task,
                    run_id,
                    agent_factory,
                    is_final_task=idx == len(benchmark.tasks) - 1,
                )
                benchmark_run.tasks.append(
                    OpenClawBenchmarkTaskRun(
                        task_name=task.name,
                        category=category_name,
                        baseline=OpenClawBenchmarkPhaseRun(
                            work_session_id=work_sid,
                            judge_session_id=judge_sid,
                            success=success,
                            workspace_dir=str(baseline_workspace.resolve()),
                        ),
                        evolved=None,
                    )
                )
                LOGGER.info("Baseline task %s: success: %s", task.name, success)
                if args.test:
                    LOGGER.info("Test mode, stopping after baseline task %s", task.name)
                    break

            if args.test:
                break

            LOGGER.info("Triggering baseline agent evolution for category=%s suite_run=%d...", category_name, repeat_index)
            await OpenClawAgent.evolve(f"evolve-run-{repeat_index}-{category_name}-{short_id()}")
            # openclaw_copy_evolved_skills(baseline_workspace, evolved_workspace)

            for idx, task in enumerate(benchmark.tasks):
                agent_factory = openclaw_create_agent_factory(
                    run_id=run_id,
                    run_index=repeat_index,
                    phase="evolved",
                    task=task,
                    workspace_dir=evolved_workspace
                )
                LOGGER.info(
                    "Running evolved task: %s with run_id: %s suite_run: %d workspace: %s",
                    task.name,
                    run_id,
                    repeat_index,
                    evolved_workspace,
                )
                success, work_sid, judge_sid = await openclaw_run_task(
                    task,
                    run_id,
                    agent_factory,
                    is_evolve_turn=True,
                    is_final_task=idx == len(benchmark.tasks) - 1,
                )
                row = benchmark_run.tasks[task_start_idx + idx]
                benchmark_run.tasks[task_start_idx + idx] = row.model_copy(
                    update={
                        "evolved": OpenClawBenchmarkPhaseRun(
                            work_session_id=work_sid,
                            judge_session_id=judge_sid,
                            success=success,
                            workspace_dir=str(evolved_workspace.resolve()),
                        )
                    }
                )
                LOGGER.info("Evolved task %s: success: %s", task.name, success)

            reset_benchmark_evolution_state(run_id, category_name, repeat_index)

        suite_run.completed_at = datetime.now(timezone.utc).isoformat()
        if args.test:
            break

    bench_report.completed_at = datetime.now(timezone.utc).isoformat()
    out_path = report_root / f"{run_id}.json"
    bench_report.write_json(out_path)
    LOGGER.info("Wrote benchmark report: %s", out_path)

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


async def exam_mode(args: argparse.Namespace) -> None:
    if args.test:
        LOGGER.info("Exam mode ignores --test and always runs the full benchmark workflow.")

    # 解析markdown文件为benchmark json
    generated_benchmarks = preprocess_benchmark_mds()

    OpenClawAgent.initialize_environment(
        ensure_config_fields=True,  # 确保model的compat字段存在，并且langfuse-plugin的hooks存在
        trace_plugin=args.trace,  # 开启trace插件
        restart_gateway=True,  # 更改插件时是否重启gateway
    )

    benchmark_root = Path(args.benchmark)
    benchmark_paths = iter_benchmark_paths(benchmark_root)
    if not benchmark_paths:
        raise ValueError(f"No benchmark json files found in {benchmark_root}")

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

            for task in warmup_tasks:
                agent_factory = openclaw_create_agent_factory(
                    run_id=run_id,
                    run_index=repeat_index,
                    phase="baseline",
                    task=task,
                    workspace_dir=baseline_workspace,
                )
                LOGGER.info(
                    "Running exam warmup baseline task: %s run_id: %s suite_run: %d workspace: %s",
                    task.name,
                    run_id,
                    repeat_index,
                    baseline_workspace,
                )
                success, work_sid, judge_sid = await openclaw_run_task(
                    task,
                    run_id,
                    agent_factory,
                    is_evolve_turn=False,
                    is_final_task=False,
                )
                LOGGER.info(
                    "Exam warmup task %s finished: success=%s work_session=%s judge_session=%s",
                    task.name,
                    success,
                    work_sid,
                    judge_sid,
                )

            LOGGER.info(
                "Triggering exam evolution for category=%s suite_run=%d after %d warmup tasks...",
                category_name,
                repeat_index,
                len(warmup_tasks),
            )
            await OpenClawAgent.evolve(f"exam-evolve-run-{repeat_index}-{category_name}-{short_id()}")

            baseline_agent_factory = openclaw_create_agent_factory(
                run_id=run_id,
                run_index=repeat_index,
                phase="baseline",
                task=final_task,
                workspace_dir=baseline_workspace,
            )
            LOGGER.info(
                "Running exam final baseline task: %s run_id: %s suite_run: %d workspace: %s",
                final_task.name,
                run_id,
                repeat_index,
                baseline_workspace,
            )
            baseline_success, baseline_work_sid, baseline_judge_sid = await openclaw_run_task(
                final_task,
                run_id,
                baseline_agent_factory,
                is_evolve_turn=False,
                is_final_task=True,
            )

            evolved_agent_factory = openclaw_create_agent_factory(
                run_id=run_id,
                run_index=repeat_index,
                phase="evolved",
                task=final_task,
                workspace_dir=evolved_workspace,
            )
            LOGGER.info(
                "Running exam final evolved task: %s run_id: %s suite_run: %d workspace: %s",
                final_task.name,
                run_id,
                repeat_index,
                evolved_workspace,
            )
            evolved_success, evolved_work_sid, evolved_judge_sid = await openclaw_run_task(
                final_task,
                run_id,
                evolved_agent_factory,
                is_evolve_turn=True,
                is_final_task=True,
            )

            benchmark_run.tasks.append(
                OpenClawBenchmarkTaskRun(
                    task_name=final_task.name,
                    category=category_name,
                    baseline=OpenClawBenchmarkPhaseRun(
                        work_session_id=baseline_work_sid,
                        judge_session_id=baseline_judge_sid,
                        success=baseline_success,
                        workspace_dir=str(baseline_workspace.resolve()),
                    ),
                    evolved=OpenClawBenchmarkPhaseRun(
                        work_session_id=evolved_work_sid,
                        judge_session_id=evolved_judge_sid,
                        success=evolved_success,
                        workspace_dir=str(evolved_workspace.resolve()),
                    ),
                )
            )
            LOGGER.info(
                "Exam final task %s completed: baseline_success=%s evolved_success=%s",
                final_task.name,
                baseline_success,
                evolved_success,
            )

            reset_benchmark_evolution_state(run_id, category_name, repeat_index)

        suite_run.completed_at = datetime.now(timezone.utc).isoformat()

    bench_report.completed_at = datetime.now(timezone.utc).isoformat()
    out_path = report_root / f"{run_id}.json"
    bench_report.write_json(out_path)
    LOGGER.info("Wrote benchmark report: %s", out_path)

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
        "--repeat",
        type=int,
        default=1,
        help="Run the full benchmark suite N times and store each full execution under report.runs.",
    )
    args = parser.parse_args()
    if args.repeat < 1:
        raise ValueError("--repeat must be at least 1")

    if args.mode == "replay":
        await replay_mode(args)
        return
    if args.mode == "exam":
        await exam_mode(args)
        return
    raise ValueError(f"Unsupported mode: {args.mode}")


if __name__ == "__main__":
    asyncio.run(openclaw_main())
