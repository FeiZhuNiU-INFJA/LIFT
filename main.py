from __future__ import annotations

import uuid
from pathlib import Path

from src.agents import HermesAgent
from src.benchmark_schema import BenchmarkSpec
from src.eval_core import eval_task


if __name__ == "__main__":
    benchmark_path = Path("assets/benchmarks/benchmark_test.json")
    benchmark = BenchmarkSpec.from_json_file(benchmark_path)
    if not benchmark.tasks:
        raise ValueError(f"No tasks found in {benchmark_path}")

    first_task = benchmark.tasks[0]
    run_id = f"{benchmark.name}_{uuid.uuid4()}"
    agent = HermesAgent()

    success = eval_task(first_task, run_id, agent)
    print(f"First task success: {success}")
