from __future__ import annotations

import asyncio
import argparse
import uuid
from datetime import datetime
from pathlib import Path

from src.agents import HermesAgent, OpenClawAgent
from src.benchmark_schema import BenchmarkSpec
from src.eval_core import run_task
from src.config import LOGGER

def create_agent(framework: str, run_id: str | None = None, task_id: str | None = None, skills_dir: str | None = None):
    if framework == "hermes":
        return HermesAgent()
    if framework == "openclaw":
        if run_id is None:
            raise ValueError("run_id is required for openclaw agent")
        return OpenClawAgent(run_id, task_id, skills_dir)
    raise ValueError(f"Unsupported framework: {framework}")

async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--framework",
        choices=("hermes", "openclaw"),
        default="openclaw",
        help="Agent framework to run.",
    )
    args = parser.parse_args()

    benchmark_path = Path("assets/benchmarks/benchmark1.json")
    benchmark = BenchmarkSpec.from_json_file(benchmark_path)
    if not benchmark.tasks:
        raise ValueError(f"No tasks found in {benchmark_path}")
    
    run_id = f"evobench-{datetime.now().strftime('%Y-%m-%d')}-{uuid.uuid4()}"
    
    for task in benchmark.tasks:
        skills_dir = task.requirements.extra_skills_dir
        # basic run
        task_id = f"baseline-{task.category_name}-{task.name}"
        agent = create_agent(args.framework, run_id, task_id, skills_dir)
        LOGGER.info(f"Running task: {task.name} with run_id: {run_id}")
        success = await run_task(task, run_id, agent)
        LOGGER.info(f"Task {task.name}: success: {success}")
        
        # evolved run
        task_id = f"evolved-{task.category_name}-{task.name}"
        agent = create_agent(args.framework, run_id, task_id, skills_dir)
        LOGGER.info(f"Running task: {task.name} with run_id: {run_id}")
        success = await run_task(task, run_id, agent, is_evolve_turn=True)
        LOGGER.info(f"Task {task.name}: success: {success}")
    LOGGER.info(f"\nAll tasks completed. Run_id: {run_id}")


if __name__ == "__main__":
    asyncio.run(main())
