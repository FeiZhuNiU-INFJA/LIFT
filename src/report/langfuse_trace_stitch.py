"""
Collect Langfuse traces for one ``openclaw_run_task`` phase (single pipeline).

1. ``trace.list`` × N — discover trace ids（OpenClaw 仅 sid；Hermes 走 sid + tag）。
2. ``trace.get`` — fetch all details (tokens from GENERATION observations).
3. Pair ``*_agent`` + plugin trace → ``work_agent_traces`` / ``judge_agent_traces``。
4. Build ``work_analytics`` from work turns.
"""

from __future__ import annotations

from typing import Any, Literal

from src.report.langfuse_trace_fetch import fetch_trace_details, trace_ref_from_detail
from src.report.langfuse_trace_merge import (
    pair_hermes_traces_to_agent_turns,
    pair_session_traces_to_agent_turns,
)
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


def _stitch_openclaw(
    client: Any,
    *,
    eval_run_tag: str,
    work_session_id: str,
    judge_session_id: str,
    page_limit: int,
) -> PhaseLangfuseBundle:
    """OpenClaw 模式：``*_agent`` 与 ``openclaw-plugin`` 共享 work/judge session_id，
    走 session_id 路径查询并按 session_id 分组配对。"""
    by_run_tag = _list_traces_all_pages(
        client, tags=[eval_run_tag], page_limit=page_limit, order_by="timestamp.asc"
    )
    by_work = _list_traces_all_pages(
        client, session_id=work_session_id, page_limit=page_limit, order_by="timestamp.asc"
    )
    by_judge = _list_traces_all_pages(
        client, session_id=judge_session_id, page_limit=page_limit, order_by="timestamp.asc"
    )

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
    return PhaseLangfuseBundle(
        eval_run_tag=eval_run_tag,
        work_session_id=work_session_id,
        judge_session_id=judge_session_id,
        work_agent_traces=work_turns,
        judge_agent_traces=judge_turns,
        work_analytics=build_work_analytics(work_turns),
    )


def _stitch_hermes(
    client: Any,
    *,
    eval_run_tag: str,
    work_session_id: str,
    judge_session_id: str,
    page_limit: int,
) -> PhaseLangfuseBundle:
    """Hermes 模式：
    - ``*_agent`` pre-chat span 的 ``session_id`` 与外部一致，走 session_id 命中。
    - ``Hermes turn`` 的 ``session_id`` 是 hermes 内部 task_id，不可用 session_id 命中；
      hermes 侧已把 work/judge session_id 写入 ``tags``，因此走 tag 路径查询。
    - 归类：work_ids / judge_ids 集合记录命中 work/judge 的 trace id，pair 时用集合判断
      （而不是 session_id 比对），避免 ``Hermes turn`` 因 sid 不一致被丢弃。
    - 配对：``pair_hermes_traces_to_agent_turns`` 不按 session_id 分组，纯按时间 1:1 合并。
    """
    by_work_sid = _list_traces_all_pages(
        client, session_id=work_session_id, page_limit=page_limit, order_by="timestamp.asc"
    )
    by_judge_sid = _list_traces_all_pages(
        client, session_id=judge_session_id, page_limit=page_limit, order_by="timestamp.asc"
    )
    by_work_tag = _list_traces_all_pages(
        client, tags=[work_session_id], page_limit=page_limit, order_by="timestamp.asc"
    )
    by_judge_tag = _list_traces_all_pages(
        client, tags=[judge_session_id], page_limit=page_limit, order_by="timestamp.asc"
    )

    merged: dict[str, Any] = {}
    work_ids: set[str] = set()
    judge_ids: set[str] = set()
    for t in (*by_work_sid, *by_work_tag):
        tid = str(t.id)
        merged[tid] = t
        work_ids.add(tid)
    for t in (*by_judge_sid, *by_judge_tag):
        tid = str(t.id)
        merged[tid] = t
        judge_ids.add(tid)

    details = fetch_trace_details(client, list(merged.keys()))
    work_raw: list[LangfuseTraceRef] = []
    judge_raw: list[LangfuseTraceRef] = []
    for tid, list_item in merged.items():
        detail = details[tid]
        ref = trace_ref_from_detail(detail, user_id=list_item.model_dump().get("user_id"))
        # work/judge 互斥（一个 trace id 只可能命中一个 session tag）。
        if tid in work_ids:
            work_raw.append(ref)
        elif tid in judge_ids:
            judge_raw.append(ref)

    work_turns = pair_hermes_traces_to_agent_turns(work_raw)
    judge_turns = pair_hermes_traces_to_agent_turns(judge_raw)
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
    """按 ``agent_source`` 分发到 OpenClaw / Hermes 实现。默认 ``openclaw`` 保持原行为。"""
    if agent_source == "hermes":
        return _stitch_hermes(
            client,
            eval_run_tag=eval_run_tag,
            work_session_id=work_session_id,
            judge_session_id=judge_session_id,
            page_limit=page_limit,
        )
    if agent_source == "openclaw":
        return _stitch_openclaw(
            client,
            eval_run_tag=eval_run_tag,
            work_session_id=work_session_id,
            judge_session_id=judge_session_id,
            page_limit=page_limit,
        )
    raise ValueError(f"Unsupported agent_source: {agent_source!r}")
