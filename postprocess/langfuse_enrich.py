from __future__ import annotations

from typing import Any

from dotenv import load_dotenv
from langfuse import get_client

from src.models import OpenClawBenchmarkPhaseRun, OpenClawBenchmarkReport
from src.report.langfuse_trace_stitch import stitch_phase_langfuse_traces


load_dotenv()


def get_langfuse_client():
    client = get_client()
    if not hasattr(client, "api"):
        raise RuntimeError(
            "Langfuse client is unavailable. Configure LANGFUSE_PUBLIC_KEY and related Langfuse settings before running report enrichment."
        )
    return client


def enrich_phase(client: Any, run_tag: str, phase: OpenClawBenchmarkPhaseRun | None):
    if phase is None:
        return None
    bundle = stitch_phase_langfuse_traces(
        client,
        eval_run_tag=run_tag,
        work_session_id=phase.work_session_id,
        judge_session_id=phase.judge_session_id,
    )
    return phase.model_copy(update={"langfuse": bundle})


def enrich_report(report: OpenClawBenchmarkReport, client: Any) -> OpenClawBenchmarkReport:
    run_tag = report.run_id
    new_runs = []
    for run in report.runs:
        new_benchmarks = []
        for benchmark in run.benchmarks:
            new_tasks = []
            for task_run in benchmark.tasks:
                baseline = enrich_phase(client, run_tag, task_run.baseline)
                evolved = enrich_phase(client, run_tag, task_run.evolved) if task_run.evolved else None
                new_tasks.append(task_run.model_copy(update={"baseline": baseline, "evolved": evolved}))
            new_benchmarks.append(benchmark.model_copy(update={"tasks": new_tasks}))
        new_runs.append(run.model_copy(update={"benchmarks": new_benchmarks}))
    return report.model_copy(update={"runs": new_runs})
