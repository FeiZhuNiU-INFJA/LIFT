"""
Collect Langfuse traces for one ``openclaw_run_task`` phase (single pipeline).

1. ``trace.list`` × N — discover trace ids（统一通过 work/judge session_id 命中）。
2. ``trace.get`` — fetch all details (tokens from GENERATION observations).
3. Pair ``*_agent`` + plugin trace → ``work_agent_traces`` / ``judge_agent_traces``。
4. Build ``work_analytics`` from work turns.
"""

from __future__ import annotations

from typing import Any, Literal

from src.report.langfuse_trace_fetch import fetch_trace_details, trace_ref_from_detail
from src.report.langfuse_trace_merge import pair_session_traces_to_agent_turns
from src.report.langfuse_work_analytics import build_work_analytics
from src.models import LangfuseTraceRef, PhaseLangfuseBundle


AgentSource = Literal["openclaw", "hermes"]


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


def _stitch_by_session(
    client: Any,
    *,
    eval_run_tag: str,
    work_session_id: str,
    judge_session_id: str,
    page_limit: int,
    include_run_tag: bool,
) -> PhaseLangfuseBundle:
    """通用实现：``*_agent`` 与 plugin trace 共享 work/judge session_id，
    走 session_id 路径查询并按 session_id 分组配对。

    OpenClaw 模式额外按 ``eval_run_tag`` 兜底拉取（``include_run_tag=True``）；
    Hermes 模式不再依赖 session tag，仅按 session_id 命中。
    """
    sources: list[list[Any]] = []
    if include_run_tag:
        sources.append(
            _list_traces_all_pages(
                client, tags=[eval_run_tag], page_limit=page_limit, order_by="timestamp.asc"
            )
        )
    sources.append(
        _list_traces_all_pages(
            client, session_id=work_session_id, page_limit=page_limit, order_by="timestamp.asc"
        )
    )
    sources.append(
        _list_traces_all_pages(
            client, session_id=judge_session_id, page_limit=page_limit, order_by="timestamp.asc"
        )
    )

    merged: dict[str, Any] = {}
    for batch in sources:
        for t in batch:
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
    return PhaseLangfuseBundle(
        eval_run_tag=eval_run_tag,
        work_session_id=work_session_id,
        judge_session_id=judge_session_id,
        work_agent_traces=work_turns,
        judge_agent_traces=judge_turns,
        work_analytics=build_work_analytics(work_turns),
    )


def stitch_phase_langfuse_traces(
    client: Any,
    *,
    eval_run_tag: str,
    work_session_id: str,
    judge_session_id: str,
    agent_source: AgentSource = "openclaw",
    page_limit: int = 100,
) -> PhaseLangfuseBundle:
    """按 ``agent_source`` 分发查询策略。

    - ``openclaw``：在 session_id 之外额外按 ``eval_run_tag`` 兜底拉取。
    - ``hermes``：现在 hermes_runner 直接接收外部 session_id，langfuse trace
      的 session_id 与 work/judge session_id 一致，直接按 session_id 命中即可，
      不再需要 session tag 兜底。
    """
    if agent_source not in ("openclaw", "hermes"):
        raise ValueError(f"Unsupported agent_source: {agent_source!r}")
    return _stitch_by_session(
        client,
        eval_run_tag=eval_run_tag,
        work_session_id=work_session_id,
        judge_session_id=judge_session_id,
        page_limit=page_limit,
        include_run_tag=(agent_source == "openclaw"),
    )
