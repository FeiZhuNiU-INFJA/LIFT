from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from src.agents import OpenClawAgent
from src.models import (
    BenchmarkSpec,
    BenchmarkTask,
    OpenClawBenchmarkPhaseRun,
    OpenClawBenchmarkReport,
    OpenClawBenchmarkTaskRun,
)
from src.config import LOGGER
from src.eval_core import openclaw_run_task
from src.utils import short_id


def openclaw_workspace(run_id: str, phase: str, category_name: str) -> Path:
    workspace_dir = Path("/tmp") / run_id / phase / category_name
    workspace_dir.mkdir(parents=True, exist_ok=True)
    return workspace_dir


def openclaw_create_agent_factory(
    run_id: str,
    phase: str,
    task: BenchmarkTask,
    workspace_dir: Path,
    copy_extra_skills: bool = True,
):
    def create_agent(session_role: str) -> OpenClawAgent:
        task_id = f"{phase}-{task.category_name}-{task.name}-{session_role}"
        return OpenClawAgent(
            run_id=run_id,
            task_id=task_id,
            skills_dir=task.requirements.extra_skills_dir if copy_extra_skills else None,
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


def reinstall_openclaw_evolve_plugin() -> None:
    plugin_path = os.getenv("OPENCLAW_EVOLVE_PLUGIN_PATH")
    if not plugin_path:
        raise ValueError("OPENCLAW_EVOLVE_PLUGIN_PATH is required for openclaw_main")

    plugin_dir = Path(plugin_path).expanduser().resolve()
    if not plugin_dir.exists():
        raise ValueError(f"OPENCLAW_EVOLVE_PLUGIN_PATH does not exist: {plugin_dir}")

    uninstall_cmd = ["openclaw", "plugins", "uninstall", "self-evolving-plugin-pro"]
    install_cmd = ["openclaw", "plugins", "install", str(plugin_dir)]
    restart_cmd = ["openclaw", "gateway", "restart"]

    LOGGER.info("Running command: %s", " ".join(uninstall_cmd))
    subprocess.run(uninstall_cmd, check=True)
    LOGGER.info("Running command: %s", " ".join(restart_cmd))
    subprocess.run(restart_cmd, check=True)
    LOGGER.info("Running command: %s", " ".join(install_cmd))
    subprocess.run(install_cmd, check=True)
    LOGGER.info("Running command: %s", " ".join(restart_cmd))
    subprocess.run(restart_cmd, check=True)


async def openclaw_main() -> None:
    parser = argparse.ArgumentParser()
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
    args = parser.parse_args()

    # reinstall_openclaw_evolve_plugin()

    benchmark_root = Path(args.benchmark)
    benchmark_paths = iter_benchmark_paths(benchmark_root)
    if not benchmark_paths:
        raise ValueError(f"No benchmark json files found in {benchmark_root}")
    
    run_id = f"evobench-runid-{datetime.now().strftime('%Y%m%d')}-{short_id()}"
    report_root = Path.cwd() / "evobench-reports"
    for benchmark_path in benchmark_paths:
        benchmark = BenchmarkSpec.from_json_file(benchmark_path)
        if not benchmark.tasks:
            raise ValueError(f"No tasks found in {benchmark_path}")

        category_name = benchmark.category
        baseline_workspace = openclaw_workspace(run_id, "baseline", category_name)
        evolved_workspace = openclaw_workspace(run_id, "evolved", category_name)

        bench_report = OpenClawBenchmarkReport(
            run_id=run_id,
            benchmark_path=str(benchmark_path.resolve()),
            benchmark_name=benchmark.name,
            category=category_name,
        )

        LOGGER.info("Running benchmark: %s", benchmark_path)

        for idx, task in enumerate(benchmark.tasks):
            agent_factory = openclaw_create_agent_factory(
                run_id=run_id,
                phase="baseline",
                task=task,
                workspace_dir=baseline_workspace,
            )
            LOGGER.info(
                "Running baseline task: %s with run_id: %s workspace: %s",
                task.name,
                run_id,
                baseline_workspace,
            )
            success, work_sid, judge_sid = await openclaw_run_task(
                task,
                run_id,
                agent_factory,
                is_final_task=idx == len(benchmark.tasks) - 1,
            )
            bench_report.tasks.append(
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
            LOGGER.info("Test mode, stopping after baseline task %s", task.name)
            bench_report.completed_at = datetime.now(timezone.utc).isoformat()
            safe_cat = category_name.replace("/", "_").replace(" ", "_")
            out_path = report_root / f"{run_id}__{safe_cat}__{benchmark_path.stem}.json"
            bench_report.write_json(out_path)
            LOGGER.info("Wrote benchmark report: %s", out_path)
            break

        LOGGER.info("Triggering baseline agent evolution for category=%s...", category_name)
        evolve_agent = OpenClawAgent(
            run_id=run_id,
            task_id=f"baseline-evolve-{category_name}",
            workspace_dir=baseline_workspace,
        )
        await evolve_agent.evolve(f"evolve-{category_name}-{short_id()}")
        openclaw_copy_evolved_skills(baseline_workspace, evolved_workspace)

        for idx, task in enumerate(benchmark.tasks):
            agent_factory = openclaw_create_agent_factory(
                run_id=run_id,
                phase="evolved",
                task=task,
                workspace_dir=evolved_workspace,
                copy_extra_skills=False,
            )
            LOGGER.info(
                "Running evolved task: %s with run_id: %s workspace: %s",
                task.name,
                run_id,
                evolved_workspace,
            )
            success, work_sid, judge_sid = await openclaw_run_task(
                task,
                run_id,
                agent_factory,
                is_evolve_turn=True,
                is_final_task=idx == len(benchmark.tasks) - 1,
            )
            row = bench_report.tasks[idx]
            bench_report.tasks[idx] = row.model_copy(
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

        bench_report.completed_at = datetime.now(timezone.utc).isoformat()
        safe_cat = category_name.replace("/", "_").replace(" ", "_")
        out_path = report_root / f"{run_id}__{safe_cat}__{benchmark_path.stem}.json"
        bench_report.write_json(out_path)
        LOGGER.info("\nBenchmark completed. Run_id: %s benchmark: %s report: %s", run_id, benchmark_path, out_path)


if __name__ == "__main__":
    asyncio.run(openclaw_main())
