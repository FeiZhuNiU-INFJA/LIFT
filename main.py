from __future__ import annotations

import uuid
from pathlib import Path

from src.agents import HermesAgent
from src.benchmark_schema import BenchmarkSpec
from src.eval_core import run_task


if __name__ == "__main__":
    benchmark_path = Path("assets/benchmarks/benchmark1.json")
    benchmark = BenchmarkSpec.from_json_file(benchmark_path)
    if not benchmark.tasks:
        raise ValueError(f"No tasks found in {benchmark_path}")

    first_task = benchmark.tasks[0]
    run_id = f"{benchmark.name}_{first_task.category_name}_{first_task.name}_{uuid.uuid4()}"
    agent = HermesAgent()

    success = run_task(first_task, run_id, agent)
    print(f"First task success: {success}")
