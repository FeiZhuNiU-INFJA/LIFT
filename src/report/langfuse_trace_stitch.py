"""
Collect Langfuse traces for one ``openclaw_run_task`` phase (single pipeline).

1. ``trace.list`` × N — discover trace ids（默认按 session_id 命中；Hermes 走 tag）。
2. ``trace.get`` — fetch all details (tokens from GENERATION observations).
3. Pair ``*_agent`` + plugin trace → ``work_agent_traces`` / ``judge_agent_traces``。
4. Build ``work_analytics`` from work turns.

拼装策略命名反映"如何匹配 trace"，而非某个具体 runtime：
- ``_stitch_by_session_id`` 是绝大多数 runtime（OpenClaw / GenericAgent / OpenHuman /
  multi_user_openclaw 等）走的默认路径——pre-chat 与 plugin trace 都写了同一个
  ``session_id``，直接按 session 命中 + 一路 session tag 兜底。
- ``_stitch_by_tags`` 是 Hermes 走的备用路径——Hermes 内部 ``Hermes turn`` 的
  ``session_id`` 是它自己的 task_id，与外部 eval session_id 对不上，只能靠
  外挂在 tags 上的 work/judge session id 反查。
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from src.config import LOGGER
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

# 服务端 zod 校验硬上限 ``limit <= 100``（Langfuse 4.11.0 实测；见
# ``langfuse/api/trace/client.py:list`` docstring "reduce the limit"），
# 不能再往上顶。所有分页拉取都以 100 为上界。
_LIST_PAGE_LIMIT_MAX = 100

# RunTraceIndex 分页并发拉取的 worker 数；实测 8 workers 达 ~7.9× 加速，
# 16 workers ~15.7×（每页 ~4.6s → 0.05s if fields=core）。默认 8，需要更快
# 或 langfuse-web 是集群时可通过 ``EVAL_INDEX_WORKERS`` 调整。
_INDEX_WORKERS_ENV = "EVAL_INDEX_WORKERS"
_INDEX_WORKERS_DEFAULT = 8

# 每拉几页打一条进度日志（dashboard live log 能看进度，避免"卡 45 分钟"错觉）。
_INDEX_PROGRESS_EVERY = 20


def _resolve_index_workers() -> int:
    raw = os.environ.get(_INDEX_WORKERS_ENV, "").strip()
    if not raw:
        return _INDEX_WORKERS_DEFAULT
    try:
        v = int(raw)
    except ValueError:
        LOGGER.warning(
            "Invalid %s=%r, falling back to %d.",
            _INDEX_WORKERS_ENV, raw, _INDEX_WORKERS_DEFAULT,
        )
        return _INDEX_WORKERS_DEFAULT
    return max(1, v)


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


class RunTraceIndex:
    """按 run tag 一次性拉整个 run 的 trace list，供各 phase 通过 session_id / session tag
    做本地字典查询，替代 per-phase 4 路 ``trace.list``。

    背景：``_stitch_by_session_id`` / ``_stitch_by_tags`` 各自 per-phase 触发 4 路
    ``trace.list``。一个 run 有 ``task 数 × 2 phase`` 个 phase，REST 数量按 ``O(phase×4)``
    放大（例如 28 task × 2 × 4 = 224 次），全部按 ``sessionId`` 打 ClickHouse——sessionId
    非排序键，每次几乎全表扫，容易压垮单进程 ``langfuse-web`` 触发 ``httpx.ReadTimeout``。

    本索引把发现阶段收敛到 O(pages) 次分页 REST：按 ``run_tag`` 拉一次（run_tag = run_id 唯一），
    Python 侧按 ``session_id`` 与每个 ``tag`` 建立二级字典，phase 层只做 dict 查询。
    完整 payload 仍由 ``fetch_trace_details`` 按需 ``trace.get``，本索引不影响。

    性能实现要点（实测 r10：745 页 × 100 traces = 74405 条）:

    - ``fields=core``: Langfuse ``trace.list`` 默认 join observations/scores/metrics 做汇总，
      单页 ~4.66s；``fields=core`` 只返回 id/name/session_id/tags/timestamp/metadata 等核心列，
      单页降至 ~0.05s（~90×）。discovery 阶段完全够用（``trace_ref_from_detail`` 拿完整
      payload 走 ``trace.get``），是本索引最大加速来源。
    - 分页并发: page 1 拿 ``total_pages`` 后，pages 2..N 用 ``ThreadPoolExecutor`` 并发拉。
      Langfuse-web 单进程实测 8 workers ~7.9× 加速，16 workers ~15.7×。worker 数由
      ``EVAL_INDEX_WORKERS`` 控制。
    - 进度日志: 每 ``_INDEX_PROGRESS_EVERY`` 页打一条 INFO，避免 dashboard live log
      长时间无输出误当卡死。

    仅承担 "发现"（list）阶段的缓存；后续 classify / pair 逻辑无变化。当外部传入 ``None``
    时，stitch 函数回落到 per-phase ``_list_traces_parallel``，便于灰度 / 排障对拍。
    """

    def __init__(self, client: Any, *, run_tag: str, page_limit: int = _LIST_PAGE_LIMIT_MAX):
        if not run_tag:
            raise ValueError("RunTraceIndex requires a non-empty run_tag")
        self._run_tag = run_tag
        page_limit = min(page_limit, _LIST_PAGE_LIMIT_MAX)
        # order_by=timestamp.asc 与 per-phase 查询保持一致，让 pair 阶段的时间顺序假设成立。
        # fields=core 只保留 discovery 需要的字段（id/session_id/tags/…），跳过 obs/score/metric
        # 聚合，把每页 ClickHouse 查询从 ~4.6s 降到 ~0.05s（实测 90×）。
        list_kwargs = {
            "tags": [run_tag],
            "order_by": "timestamp.asc",
            "fields": "core",
        }

        LOGGER.info(
            "RunTraceIndex: probing run_tag=%s (page 1, limit=%d, fields=core).",
            run_tag, page_limit,
        )
        first = client.api.trace.list(limit=page_limit, page=1, **list_kwargs)
        first_batch = list(first.data or [])
        total_pages = int(first.meta.total_pages) if first.meta is not None else 1
        total_items = int(first.meta.total_items) if first.meta is not None else len(first_batch)
        LOGGER.info(
            "RunTraceIndex: total_items=%d total_pages=%d — fetching pages 2..%d concurrently.",
            total_items, total_pages, total_pages,
        )

        items: list[Any] = list(first_batch)
        if total_pages > 1:
            remaining = list(range(2, total_pages + 1))
            workers = min(_resolve_index_workers(), len(remaining))

            def _fetch(page: int) -> list[Any]:
                resp = client.api.trace.list(limit=page_limit, page=page, **list_kwargs)
                return list(resp.data or [])

            done = 1  # page 1 already counted
            page_to_batch: dict[int, list[Any]] = {}
            with ThreadPoolExecutor(max_workers=workers) as pool:
                future_to_page = {pool.submit(_fetch, p): p for p in remaining}
                for fut in as_completed(future_to_page):
                    p = future_to_page[fut]
                    page_to_batch[p] = fut.result()
                    done += 1
                    if done % _INDEX_PROGRESS_EVERY == 0 or done == total_pages:
                        LOGGER.info(
                            "RunTraceIndex: fetched %d/%d pages (%d traces so far).",
                            done, total_pages,
                            len(items) + sum(len(v) for v in page_to_batch.values()),
                        )
            for p in remaining:
                items.extend(page_to_batch[p])

        self._items: list[Any] = items
        by_session: dict[str, list[Any]] = {}
        by_tag: dict[str, list[Any]] = {}
        for item in self._items:
            sid = getattr(item, "session_id", None)
            if sid:
                by_session.setdefault(str(sid), []).append(item)
            for tag in (getattr(item, "tags", None) or []):
                by_tag.setdefault(str(tag), []).append(item)
        self._by_session = by_session
        self._by_tag = by_tag

    def __len__(self) -> int:
        return len(self._items)

    @property
    def run_tag(self) -> str:
        return self._run_tag

    def by_session_id(self, session_id: str) -> list[Any]:
        return self._by_session.get(session_id, [])

    def by_tag(self, tag: str) -> list[Any]:
        return self._by_tag.get(tag, [])


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


def _classify_by_session(
    ref: LangfuseTraceRef,
    *,
    work_session_id: str,
    judge_session_id: str,
) -> str | None:
    """Return ``'work'``, ``'judge'``, or None based on ``session_id`` / session tag."""
    sid = ref.session_id
    tags = ref.tags or []
    if sid == work_session_id or work_session_id in tags:
        return "work"
    if sid == judge_session_id or judge_session_id in tags:
        return "judge"
    return None


def _discover_traces_four_ways(
    client: Any,
    *,
    work_session_id: str,
    judge_session_id: str,
    page_limit: int,
    index: RunTraceIndex | None,
) -> tuple[list[Any], list[Any], list[Any], list[Any]]:
    """4 路 trace 发现：按 work/judge 的 session_id 与 session tag 命中。

    - ``index`` 非空：从 per-run 索引本地查询（0 次 REST）。
    - ``index`` 为空：回落到原路径 ``_list_traces_parallel``（4 次 REST）。
    """
    if index is not None:
        return (
            index.by_session_id(work_session_id),
            index.by_session_id(judge_session_id),
            index.by_tag(work_session_id),
            index.by_tag(judge_session_id),
        )
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
    return by_work, by_judge, by_work_tag, by_judge_tag


def _stitch_by_session_id(
    client: Any,
    *,
    eval_run_tag: str,
    work_session_id: str,
    judge_session_id: str,
    page_limit: int,
    index: RunTraceIndex | None = None,
) -> PhaseLangfuseBundle:
    """默认拼装策略：pre-chat ``*_agent`` + plugin trace 都写了同一 ``session_id``，
    直接按 work/judge session id 命中 + session tag 兜底并 1:1 配对。

    只按 work/judge 的 ``session_id`` 与对应 session tag 检索（四路并集去重）。不再按
    ``eval_run_tag`` 全量拉取——该 tag 是整个 run 所有 task×phase×turn 的公共 tag，按它
    检索会把全 run trace 拖进 ``trace.get``，但 classify 阶段只保留本 phase 的 work/judge
    trace，其余全部丢弃，造成 O(phase 数 × 全 run trace 数) 的 N+1 放大。

    适用 runtime：OpenClaw 全家 / GenericAgent / OpenHuman —— 只要 pre-chat 与 plugin
    trace 的 ``session_id`` 与 eval 外部下发的 session id 一致，就走这条路径。

    传入 ``index`` 时，"发现" 阶段从 per-run 索引本地查询，不再触发 4 路 ``trace.list``。
    """
    by_work, by_judge, by_work_tag, by_judge_tag = _discover_traces_four_ways(
        client,
        work_session_id=work_session_id,
        judge_session_id=judge_session_id,
        page_limit=page_limit,
        index=index,
    )

    # 多路 trace.list 并集去重；完整 payload 一律 trace.get 拉取
    merged: dict[str, Any] = {}
    for t in (*by_work, *by_judge, *by_work_tag, *by_judge_tag):
        merged[str(t.id)] = t

    # 流式 transcript 归约：fetch worker 内把每条 trace 的 messages 摘下并只保留最晚一条
    # work transcript，避免 N 份全量 messages 同时驻留内存/落盘。work 判定与下方
    # `_classify_by_session` 一致（session_id 命中或 session tag 命中）。
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
        side = _classify_by_session(
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


def _stitch_by_tags(
    client: Any,
    *,
    eval_run_tag: str,
    work_session_id: str,
    judge_session_id: str,
    page_limit: int,
    index: RunTraceIndex | None = None,
) -> PhaseLangfuseBundle:
    """Tag-based 拼装（Hermes 专用）：
    - ``*_agent`` pre-chat span 的 ``session_id`` 与外部一致，走 session_id 命中。
    - ``Hermes turn`` 的 ``session_id`` 是 hermes 内部 task_id，不可用 session_id 命中；
      hermes 侧已把 work/judge session_id 写入 ``tags``，因此走 tag 路径查询。
    - 归类：work_ids / judge_ids 集合记录命中 work/judge 的 trace id，pair 时用集合判断
      （而不是 session_id 比对），避免 ``Hermes turn`` 因 sid 不一致被丢弃。
    - 配对：``pair_hermes_traces_to_agent_turns`` 不按 session_id 分组，纯按时间 1:1 合并。

    传入 ``index`` 时，"发现" 阶段从 per-run 索引本地查询，不再触发 4 路 ``trace.list``。
    """
    by_work_sid, by_judge_sid, by_work_tag, by_judge_tag = _discover_traces_four_ways(
        client,
        work_session_id=work_session_id,
        judge_session_id=judge_session_id,
        page_limit=page_limit,
        index=index,
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
    index: RunTraceIndex | None = None,
) -> PhaseLangfuseBundle:
    """按 ``agent_source`` 分发到 tag-based / session-id-based 实现。默认 ``openclaw`` 走 session_id 分支。

    ``index`` 若给出，则跳过 per-phase 4 路 ``trace.list``，改由 index 本地查询。
    索引由上层（如 ``backfill_report``）在整个 run 开始前构造一次即可。
    """
    # hermes 目前也是用session-id-base实现的了
    # if agent_source in ("hermes", "hermes_with_openspace"):
    #     return _stitch_by_tags(
    #         client,
    #         eval_run_tag=eval_run_tag,
    #         work_session_id=work_session_id,
    #         judge_session_id=judge_session_id,
    #         page_limit=page_limit,
    #         index=index,
    #     )
    # 其余 runtime 都复用默认 sid-only trace layout（``*_agent`` + plugin trace 按
    # ``session_id`` 配对）。合法 runtime 名以 ``SUPPORTED_RUNTIMES`` 为唯一事实源，
    # 新增 runtime 只要落到该 tuple 里就自动纳入这条分支。
    if agent_source in SUPPORTED_RUNTIMES:
        return _stitch_by_session_id(
            client,
            eval_run_tag=eval_run_tag,
            work_session_id=work_session_id,
            judge_session_id=judge_session_id,
            page_limit=page_limit,
            index=index,
        )
    raise ValueError(f"Unsupported agent_source: {agent_source!r}")
