"""Langfuse trace backfill (轨迹回填) for eval reports.

Loads traces from Langfuse, stitches them with framework pre-chat spans, and
writes ``PhaseRun.langfuse``.
"""

from __future__ import annotations

from typing import Any, Literal

from langfuse import get_client

from src.models import EvalReport, PhaseRun
from src.report.langfuse_trace_stitch import stitch_phase_langfuse_traces


# Agent backend whose traces are stitched during backfill.
AgentSource = Literal["openclaw", "openclaw_with_evolve", "hermes", "genericagent", "genericagent_active_evolve"]


def get_langfuse_client():
    """Return a configured Langfuse client with API access, or raise ``RuntimeError``."""
    client = get_client()
    if not hasattr(client, "api"):
        raise RuntimeError(
            "Langfuse client is unavailable. Configure LANGFUSE_PUBLIC_KEY and "
            "related Langfuse settings before running trace backfill."
        )
    return client


def backfill_phase(
    client: Any,
    run_tag: str,
    phase: PhaseRun | None,
    agent_source: AgentSource = "openclaw",
):
    """Attach stitched Langfuse traces to a single ``PhaseRun``, or return None if *phase* is None."""
    if phase is None:
        return None
    # 按 PhaseRun 存的 session id 拉 Langfuse，合并 *_agent + openclaw-plugin
    bundle = stitch_phase_langfuse_traces(
        client,
        eval_run_tag=run_tag,
        work_session_id=phase.work_session_id,
        judge_session_id=phase.judge_session_id,
        agent_source=agent_source,
    )
    return phase.model_copy(update={"langfuse": bundle})


def backfill_report(
    report: EvalReport,
    client: Any,
    agent_source: AgentSource = "openclaw",
) -> EvalReport:
    """Backfill Langfuse data for every baseline/evolved phase in *report*."""
    run_tag = report.run_id
    new_runs = []
    for repeat in report.runs:
        new_suites = []
        for suite in repeat.suites:
            new_tasks = []
            for task_run in suite.tasks:
                baseline = backfill_phase(client, run_tag, task_run.baseline, agent_source)
                evolved = (
                    backfill_phase(client, run_tag, task_run.evolved, agent_source)
                    if task_run.evolved
                    else None
                )
                new_tasks.append(
                    task_run.model_copy(update={"baseline": baseline, "evolved": evolved})
                )
            new_suites.append(suite.model_copy(update={"tasks": new_tasks}))
        new_runs.append(repeat.model_copy(update={"suites": new_suites}))
    return report.model_copy(update={"runs": new_runs})
