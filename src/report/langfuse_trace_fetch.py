"""Fetch full Langfuse trace details (``trace.get``) and map to ``LangfuseTraceRef``."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
import json
from typing import Any

from src.report.langfuse_trace_parse import structure_trace_payload, is_plugin_trace
from src.models import (
    LangfuseObservationBrief,
    LangfuseTraceDetailRecord,
    LangfuseTraceRef,
    LangfuseTraceTokens,
)


# 单个 phase 内 ``trace.get`` 的线程池上限。Langfuse SDK 同步客户端底层是
# httpx.Client，连接池天然线程安全，可直接喂 ThreadPoolExecutor。注意外层
# ``backfill_report`` 已并发 N phase（``EVAL_BACKFILL_WORKERS``），总并发
# = N × _TRACE_GET_WORKERS，所以这里默认压到 4 防止打挂自托管 Langfuse。
_TRACE_GET_WORKERS_ENV = "EVAL_BACKFILL_TRACE_GET_WORKERS"
_TRACE_GET_WORKERS_DEFAULT = 4


def _resolve_trace_get_workers() -> int:
    raw = os.environ.get(_TRACE_GET_WORKERS_ENV)
    if raw is None:
        return _TRACE_GET_WORKERS_DEFAULT
    try:
        return max(1, int(raw))
    except ValueError:
        return _TRACE_GET_WORKERS_DEFAULT


def _latency_seconds(raw: Any) -> float | None:
    """Coerce Langfuse latency field to float seconds, or None."""
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _iso(ts: Any) -> str | None:
    """Format a timestamp object or value as an ISO string."""
    if ts is None:
        return None
    if hasattr(ts, "isoformat"):
        return ts.isoformat()
    return str(ts)


def _usage_triplet(usage: Any) -> tuple[int, int, int]:
    """Extract (input_tokens, output_tokens, total_tokens) from a usage dict or model."""
    if usage is None:
        return 0, 0, 0
    if hasattr(usage, "model_dump"):
        d = usage.model_dump()
    elif isinstance(usage, dict):
        d = usage
    else:
        return 0, 0, 0
    inp = int(d.get("input") or d.get("input_tokens") or 0)
    out = int(d.get("output") or d.get("output_tokens") or 0)
    tot = d.get("total") or d.get("total_tokens")
    if tot is None and isinstance(d.get("usage_details"), dict):
        ud = d["usage_details"]
        inp = int(ud.get("input") or inp)
        out = int(ud.get("output") or out)
        tot = ud.get("total")
    if tot is None:
        tot = inp + out
    return inp, out, int(tot or 0)


def observation_briefs(observations: list[Any]) -> list[LangfuseObservationBrief]:
    """Map raw Langfuse observation objects to ``LangfuseObservationBrief`` models."""
    briefs: list[LangfuseObservationBrief] = []
    for o in observations or []:
        d = o.model_dump() if hasattr(o, "model_dump") else {}
        inp_t, out_t, tot_t = _usage_triplet(d.get("usage"))
        briefs.append(
            LangfuseObservationBrief(
                id=str(d.get("id") or ""),
                type=str(d.get("type") or "").upper(),
                name=str(d["name"]) if d.get("name") is not None else None,
                input_tokens=inp_t,
                output_tokens=out_t,
                total_tokens=tot_t,
            )
        )
    return briefs


def _hermes_tool_call_count_from_output(raw_output: Any) -> int | None:
    """Hermes ``Hermes turn`` chain 的 output 形如 ``{content, reasoning, tool_calls: [...]}``。

    ``tool_calls`` 由插件在 ``_finish_trace`` 时通过 ``_merge_trace_output`` 注入，
    覆盖整轮累计的工具调用（包括被上下文压缩遮蔽的早期调用），是 Hermes 工具调用数
    的权威来源。返回 ``None`` 表示无法解析（保留旧 fallback 行为）。
    """
    data: Any = raw_output
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    tool_calls = data.get("tool_calls")
    if not isinstance(tool_calls, list):
        return None
    return len(tool_calls)


def trace_detail_from_api(full: Any) -> LangfuseTraceDetailRecord:
    """Convert a Langfuse ``trace.get`` response into a ``LangfuseTraceDetailRecord``."""
    d = full.model_dump()
    obs_raw = getattr(full, "observations", None) or []
    name = d.get("name")
    structured = structure_trace_payload(
        name,
        d.get("input"),
        d.get("output"),
        d.get("metadata") if isinstance(d.get("metadata"), dict) else {},
    )
    # Hermes 现行链路：
    # - trace 顶层 metadata.messages 包含全量 transcript（由插件
    #   `_publish_messages_to_root` 写入）。当前插件已经不再在 GENERATION 子节点
    #   metadata 中保存 messages，所以这里没有回退路径——顶层缺失就是真的缺失。
    # - tool_call_blocks 权威来源：trace 顶层 output.tool_calls 的长度。插件在
    #   `_finish_trace` 中把整轮累计 tool_calls 写入 root output，不受上下文压缩影响；
    #   下游 `_make_row_hermes` 通过 `global_stats.tool_call_blocks` 读取。
    if name == "Hermes turn" and structured.plugin_metadata is not None:
        tc_count = _hermes_tool_call_count_from_output(d.get("output"))
        if tc_count is not None:
            structured = structured.model_copy(
                update={
                    "plugin_metadata": structured.plugin_metadata.model_copy(
                        update={"tool_call_blocks": tc_count}
                    )
                }
            )
    return LangfuseTraceDetailRecord(
        id=str(d["id"]),
        name=name,
        timestamp=_iso(d.get("timestamp")),
        session_id=d.get("session_id"),
        tags=[str(x) for x in (d.get("tags") or [])],
        agent_input=structured.agent_input,
        plugin_prompt=structured.plugin_prompt,
        plugin_response=structured.plugin_response,
        plugin_metadata=structured.plugin_metadata,
        latency_seconds=_latency_seconds(d.get("latency")),
        observations=observation_briefs(obs_raw),
    )


def tokens_from_detail(detail: LangfuseTraceDetailRecord) -> LangfuseTraceTokens:
    """Sum GENERATION observation token counts from a trace detail record."""
    inp = out = tot = 0
    for ob in detail.observations:
        if ob.type == "GENERATION":
            inp += ob.input_tokens
            out += ob.output_tokens
            tot += ob.total_tokens
    return LangfuseTraceTokens(
        input_tokens=inp,
        output_tokens=out,
        total_tokens=tot if tot else inp + out,
    )


def count_tool_observations(detail: LangfuseTraceDetailRecord) -> int:
    """Count ``type=TOOL`` observations in a trace detail record.

    Runtime-agnostic 工具调用兜底数：只要 runtime 的 langfuse overlay 给每次工具调用挂
    了 ``as_type='tool'`` observation（GA / OpenClaw / 任何走 LIFT trace 拼装的 plugin
    trace 都满足），本函数就能给出正确计数；OpenClaw 仍以 ``plugin_metadata.toolRoundtrips``
    为权威值，本字段作为缺失 metadata 时的兜底（dashboard 显示）。
    """
    return sum(1 for ob in detail.observations if ob.type == "TOOL")


def fetch_trace_details(
    client: Any,
    trace_ids: list[str],
    cache: dict[str, LangfuseTraceDetailRecord] | None = None,
) -> dict[str, LangfuseTraceDetailRecord]:
    """Fetch full trace details for *trace_ids*, reusing entries in *cache* when provided.

    用线程池并发 ``trace.get``：单 phase 通常几十~上百条 trace，每条 RTT
    ~200-500ms（要拉完整 observations），串行 fetch 是 backfill 的主要瓶颈。
    并发上限通过 ``EVAL_BACKFILL_TRACE_GET_WORKERS`` 调节。
    """
    out = cache if cache is not None else {}
    pending = [tid for tid in trace_ids if tid not in out]
    if not pending:
        return out
    workers = min(_resolve_trace_get_workers(), len(pending))
    if workers <= 1:
        for tid in pending:
            full = client.api.trace.get(tid)
            out[tid] = trace_detail_from_api(full)
        return out
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for tid, detail in zip(pending, pool.map(lambda t: trace_detail_from_api(client.api.trace.get(t)), pending)):
            out[tid] = detail
    return out


def trace_ref_from_detail(
    detail: LangfuseTraceDetailRecord,
    *,
    user_id: str | None = None,
    include_plugin_tokens: bool = True,
) -> LangfuseTraceRef:
    """Build a lightweight ``LangfuseTraceRef`` from a fetched trace detail."""
    tokens: LangfuseTraceTokens | None = None
    tool_count = 0
    if include_plugin_tokens and is_plugin_trace(detail.name):
        tokens = tokens_from_detail(detail)
        tool_count = count_tool_observations(detail)
    return LangfuseTraceRef(
        id=detail.id,
        name=detail.name,
        timestamp=detail.timestamp,
        session_id=detail.session_id,
        user_id=user_id,
        tags=list(detail.tags),
        agent_input=detail.agent_input,
        plugin_prompt=detail.plugin_prompt,
        plugin_response=detail.plugin_response,
        plugin_metadata=detail.plugin_metadata,
        tokens=tokens,
        tool_observation_count=tool_count,
        latency_seconds=detail.latency_seconds,
    )
