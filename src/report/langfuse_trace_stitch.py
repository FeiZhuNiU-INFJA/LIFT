"""
Collect Langfuse traces for one ``openclaw_run_task`` phase (single pipeline).

1. ``trace.list`` × N — discover trace ids（OpenClaw 仅 sid；Hermes 走 sid + tag）。
2. ``trace.get`` — fetch all details (tokens from GENERATION observations).
3. Pair ``*_agent`` + plugin trace → ``work_agent_traces`` / ``judge_agent_traces``。
4. Build ``work_analytics`` from work turns.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from src.lift.adapters.registry import SUPPORTED_RUNTIMES
from src.report.langfuse_trace_fetch import (
    TranscriptChampion,
    fetch_trace_details,
    trace_ref_from_detail,
)
from src.report.langfuse_trace_merge import (
    pair_hermes_traces_to_agent_turns,
    pair_session_traces_to_agent_turns,
)
from src.report.langfuse_work_analytics import build_work_analytics
from src.models import LangfuseTraceRef, PhaseLangfuseBundle


# Agent backend whose trace pairing rules are applied during stitching.
# 值域 = ``SUPPORTED_RUNTIMES``；见 ``src.postprocess.extract.AgentSource`` 说明。
AgentSource = str


# 单 phase 内 4 路 ``trace.list`` 互相独立（work_sid / judge_sid / work_tag /
# judge_tag），用线程池并行 4 路即可消掉 4× RTT 串行累加。
_LIST_PARALLELISM = 4


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


def _list_traces_parallel(
    client: Any,
    queries: list[dict[str, Any]],
    *,
    page_limit: int,
) -> list[list[Any]]:
    """并行执行多路 ``trace.list``，返回每路的完整结果列表（保留输入顺序）。"""
    if not queries:
        return []
    workers = min(_LIST_PARALLELISM, len(queries))
    if workers <= 1:
        return [_list_traces_all_pages(client, page_limit=page_limit, **q) for q in queries]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(
            pool.map(lambda q: _list_traces_all_pages(client, page_limit=page_limit, **q), queries)
        )


def _normalize_eval_session(
    ref: LangfuseTraceRef,
    *,
    work_session_id: str,
    judge_session_id: str,
) -> LangfuseTraceRef:
    """Rewrite ``session_id`` when work/judge session id appears in trace tags."""
    tags = ref.tags or []
    if work_session_id in tags:
        return ref.model_copy(update={"session_id": work_session_id})
    if judge_session_id in tags:
        return ref.model_copy(update={"session_id": judge_session_id})
    return ref


def _classify_openclaw_side(
    ref: LangfuseTraceRef,
    *,
    work_session_id: str,
    judge_session_id: str,
) -> str | None:
    """Return ``'work'``, ``'judge'``, or None for an OpenClaw trace ref."""
    sid = ref.session_id
    tags = ref.tags or []
    if sid == work_session_id or work_session_id in tags:
        return "work"
    if sid == judge_session_id or judge_session_id in tags:
        return "judge"
    return None


def _stitch_openclaw(
    client: Any,
    *,
    eval_run_tag: str,
    work_session_id: str,
    judge_session_id: str,
    page_limit: int,
) -> PhaseLangfuseBundle:
    """OpenClaw：pre-chat ``*_agent`` + ``openclaw-plugin`` 按 eval session 归类并 1:1 配对。

    只按 work/judge 的 ``session_id`` 与对应 session tag 检索（四路并集去重）。不再按
    ``eval_run_tag`` 全量拉取——该 tag 是整个 run 所有 task×phase×turn 的公共 tag，按它
    检索会把全 run trace 拖进 ``trace.get``，但 classify 阶段只保留本 phase 的 work/judge
    trace，其余全部丢弃，造成 O(phase 数 × 全 run trace 数) 的 N+1 放大。
    """
    by_work, by_judge, by_work_tag, by_judge_tag = _list_traces_parallel(
        client,
        [
            {"session_id": work_session_id, "order_by": "timestamp.asc"},
            {"session_id": judge_session_id, "order_by": "timestamp.asc"},
            {"tags": [work_session_id], "order_by": "timestamp.asc"},
            {"tags": [judge_session_id], "order_by": "timestamp.asc"},
        ],
        page_limit=page_limit,
    )

    # 多路 trace.list 并集去重；完整 payload 一律 trace.get 拉取
    merged: dict[str, Any] = {}
    for t in (*by_work, *by_judge, *by_work_tag, *by_judge_tag):
        merged[str(t.id)] = t

    # 流式 transcript 归约：fetch worker 内把每条 trace 的 messages 摘下并只保留最晚一条
    # work transcript，避免 N 份全量 messages 同时驻留内存/落盘。work 判定与下方
    # `_classify_openclaw_side` 一致（session_id 命中或 session tag 命中）。
    champion = TranscriptChampion(
        lambda d: d.session_id == work_session_id or work_session_id in (d.tags or [])
    )
    details = fetch_trace_details(client, list(merged.keys()), champion=champion)
    work_raw: list[LangfuseTraceRef] = []
    judge_raw: list[LangfuseTraceRef] = []
    for tid, list_item in merged.items():
        detail = details[tid]
        ref = trace_ref_from_detail(detail, user_id=list_item.model_dump().get("user_id"))
        ref = _normalize_eval_session(
            ref,
            work_session_id=work_session_id,
            judge_session_id=judge_session_id,
        )
        side = _classify_openclaw_side(
            ref,
            work_session_id=work_session_id,
            judge_session_id=judge_session_id,
        )
        if side == "work":
            work_raw.append(ref)
        elif side == "judge":
            judge_raw.append(ref)

    work_turns = pair_session_traces_to_agent_turns(work_raw)
    judge_turns = pair_session_traces_to_agent_turns(judge_raw)
    return PhaseLangfuseBundle(
        eval_run_tag=eval_run_tag,
        work_session_id=work_session_id,
        judge_session_id=judge_session_id,
        work_agent_traces=work_turns,
        judge_agent_traces=judge_turns,
        work_analytics=build_work_analytics(work_turns, all_messages=champion.messages),
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
    by_work_sid, by_judge_sid, by_work_tag, by_judge_tag = _list_traces_parallel(
        client,
        [
            {"session_id": work_session_id, "order_by": "timestamp.asc"},
            {"session_id": judge_session_id, "order_by": "timestamp.asc"},
            {"tags": [work_session_id], "order_by": "timestamp.asc"},
            {"tags": [judge_session_id], "order_by": "timestamp.asc"},
        ],
        page_limit=page_limit,
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

    # 流式 transcript 归约：只保留最晚一条 work transcript（work 判定用 work_ids 集合，
    # 与下方 pair 归类口径一致），fetch worker 内即摘除并丢弃非冠军 messages。
    champion = TranscriptChampion(lambda d: d.id in work_ids)
    details = fetch_trace_details(client, list(merged.keys()), champion=champion)
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
        work_analytics=build_work_analytics(work_turns, all_messages=champion.messages),
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
    # 其余 runtime 都复用 OpenClaw 的 sid-only trace layout（``*_agent`` + plugin
    # trace 按 session_id 配对）。合法 runtime 名以 ``SUPPORTED_RUNTIMES`` 为唯一
    # 事实源，新增 runtime 只要落到该 tuple 里就自动纳入这条分支。
    if agent_source in SUPPORTED_RUNTIMES:
        return _stitch_openclaw(
            client,
            eval_run_tag=eval_run_tag,
            work_session_id=work_session_id,
            judge_session_id=judge_session_id,
            page_limit=page_limit,
        )
    raise ValueError(f"Unsupported agent_source: {agent_source!r}")
