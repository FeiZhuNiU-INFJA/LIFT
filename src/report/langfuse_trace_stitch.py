"""
Collect Langfuse traces for one ``openclaw_run_task`` phase (single pipeline).

1. ``trace.list`` × 3 — discover trace ids (run tag + work/judge session).
2. ``trace.get`` — fetch all details (tokens from GENERATION observations).
3. Pair ``*_agent`` + ``openclaw-plugin`` → ``work_agent_traces`` / ``judge_agent_traces``.
4. Build ``work_analytics`` from work turns.
"""

from __future__ import annotations

from typing import Any

from src.report.langfuse_trace_fetch import fetch_trace_details, trace_ref_from_detail
from src.report.langfuse_trace_merge import pair_session_traces_to_agent_turns
from src.report.langfuse_work_analytics import build_work_analytics
from src.models import LangfuseTraceRef, OpenClawBenchmarkPhaseLangfuseBundle


def _list_traces_all_pages(client: Any, *, page_limit: int = 100, **kwargs: Any) -> list[Any]:
    """Discover trace ids; full payload always loaded via ``trace.get`` afterward."""
    page = 1
    out: list[Any] = []
    while True:
        resp = client.api.trace.list(limit=page_limit, page=page, **kwargs)
        batch = resp.data or []
        out.extend(batch)
        meta = resp.meta
        if not batch or meta is None or page >= int(meta.total_pages):
            break
        page += 1
    return out


def stitch_phase_langfuse_traces(
    client: Any,
    *,
    eval_run_tag: str,
    work_session_id: str,
    judge_session_id: str,
    page_limit: int = 100,
) -> OpenClawBenchmarkPhaseLangfuseBundle:
    by_run_tag = _list_traces_all_pages(client, tags=[eval_run_tag], page_limit=page_limit, order_by="timestamp.asc")
    by_work = _list_traces_all_pages(client, session_id=work_session_id, page_limit=page_limit, order_by="timestamp.asc")
    by_judge = _list_traces_all_pages(client, session_id=judge_session_id, page_limit=page_limit, order_by="timestamp.asc")

    merged: dict[str, Any] = {}
    for t in (*by_run_tag, *by_work, *by_judge):
        merged[str(t.id)] = t

    details = fetch_trace_details(client, list(merged.keys()))
    work_raw: list[LangfuseTraceRef] = []
    judge_raw: list[LangfuseTraceRef] = []
    for tid, list_item in merged.items():
        detail = details[tid]
        sid = detail.session_id or list_item.model_dump().get("session_id")
        ref = trace_ref_from_detail(detail, user_id=list_item.model_dump().get("user_id"))
        if sid == work_session_id:
            work_raw.append(ref)
        elif sid == judge_session_id:
            judge_raw.append(ref)

    work_turns = pair_session_traces_to_agent_turns(work_raw)
    judge_turns = pair_session_traces_to_agent_turns(judge_raw)

    return OpenClawBenchmarkPhaseLangfuseBundle(
        eval_run_tag=eval_run_tag,
        work_session_id=work_session_id,
        judge_session_id=judge_session_id,
        work_agent_traces=work_turns,
        judge_agent_traces=judge_turns,
        work_analytics=build_work_analytics(work_turns),
    )
