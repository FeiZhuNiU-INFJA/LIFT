from __future__ import annotations

import asyncio
import argparse
import uuid
from datetime import datetime
from pathlib import Path

from src.agents import HermesAgent, OpenClawAgent
from src.models import BenchmarkSpec
from src.eval_core import run_task
from src.config import LOGGER
from src.utils import short_id


def create_agent() -> HermesAgent:
    return HermesAgent()
    # return OpenClawAgent('test-for-bug','test-bug',workspace_dir=Path('/tmp/test'))


def iter_benchmark_paths(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    benchmark_dir = path / "benchmarks" if path.name == "assets" else path
    return sorted(benchmark_dir.glob("**/*.json"))



async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--benchmark", "-f",
        default="assets/benchmarks",
        help="Benchmark file or directory to run.",
    )
    parser.add_argument(
        "--no-evolve",
        action="store_true",
        default=False,
        help="Do not perform agent evolution after baseline run (default: evolve after baseline).",
    )
    args = parser.parse_args()
    benchmark_root = Path(args.benchmark)
    benchmark_paths = iter_benchmark_paths(benchmark_root)
    if not benchmark_paths:
        raise ValueError(f"No benchmark json files found in {benchmark_root}")

    run_id = f"evobench-runid-{datetime.now().strftime('%Y%m%d')}-{short_id()}"
    for benchmark_path in benchmark_paths:
        benchmark = BenchmarkSpec.from_json_file(benchmark_path)
        if not benchmark.tasks:
            raise ValueError(f"No tasks found in {benchmark_path}")

        
        
        # basic run
        for idx, task in enumerate(benchmark.tasks):
            agent = create_agent()
            LOGGER.info(f"Running task: {task.name} with run_id: {run_id}")
            success = await run_task(
                task,
                run_id,
                agent,
                is_final_task=idx == len(benchmark.tasks) - 1,
            )
            LOGGER.info(f"Task {task.name}: success: {success}")
            
        # 一轮任务结束，统一主动触发进化
        LOGGER.info("Triggering agent evolution...")
        await type(agent).evolve("review_session")
            
        # evolved run
        for idx, task in enumerate(benchmark.tasks):
            agent = create_agent()
            LOGGER.info(f"Running task: {task.name} with run_id: {run_id}")
            success = await run_task(
                task,
                run_id,
                agent,
                is_evolve_turn=True,
                is_final_task=idx == len(benchmark.tasks) - 1,
            )
            LOGGER.info(f"Task {task.name}: success: {success}")
        LOGGER.info(f"\nBenchmark completed. Run_id: {run_id} benchmark: {benchmark_path}")

if __name__ == "__main__":
    asyncio.run(main())
