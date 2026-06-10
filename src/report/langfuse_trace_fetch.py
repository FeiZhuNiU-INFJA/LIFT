"""Fetch full Langfuse trace details (``trace.get``) and map to ``LangfuseTraceRef``."""

from __future__ import annotations

from typing import Any

from src.report.langfuse_trace_parse import structure_trace_payload, is_plugin_trace
from src.models import (
    LangfuseObservationBrief,
    LangfuseTraceDetailRecord,
    LangfuseTraceRef,
    LangfuseTraceTokens,
)


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


def _obs_start_ts(od: dict[str, Any]) -> str:
    """observations 排序键：以 start_time 为准；缺失置空串以便排在最前。"""
    ts = od.get("start_time") or od.get("startTime")
    if ts is None:
        return ""
    if hasattr(ts, "isoformat"):
        return ts.isoformat()
    return str(ts)


def _hermes_messages_from_observations(obs_raw: list[Any]) -> list[Any] | None:
    """Hermes：trace 顶层 ``metadata.messages`` 通常缺失；改从每个 ``GENERATION`` observation 的
    ``metadata.messages`` 取（hermes 上报侧默认仅保留最近 11 条以控制数据量）。

    每条 ``GENERATION`` 的 ``metadata.messages`` 是该次 LLM 调用的 input（累计历史窗口），
    其 ``output`` 为本次 assistant 回复。取**最后一条 GENERATION**（按 ``start_time`` 排序）
    的 messages 作为基础，并追加其 output 作为最末 assistant message，整体即"该 turn 截止时的
    transcript（最多保留最近 11 条 + 最末一条 assistant）"，与 OpenClaw 模式语义对齐。
    """
    rows: list[dict[str, Any]] = []
    for o in obs_raw or []:
        od = o.model_dump() if hasattr(o, "model_dump") else (o if isinstance(o, dict) else {})
        rows.append(od)
    rows.sort(key=_obs_start_ts)

    last_msgs: list[Any] | None = None
    last_output: Any = None
    for od in rows:
        if str(od.get("type") or "").upper() != "GENERATION":
            continue
        om = od.get("metadata") or {}
        msgs = om.get("messages") if isinstance(om, dict) else None
        if isinstance(msgs, list) and msgs:
            last_msgs = msgs
            last_output = od.get("output")
    if last_msgs is None:
        return None
    merged: list[Any] = list(last_msgs)
    if isinstance(last_output, dict):
        last_msg: dict[str, Any] = {"role": "assistant"}
        last_msg.update(last_output)
        last_msg["role"] = "assistant"
        merged.append(last_msg)
    elif isinstance(last_output, str) and last_output.strip():
        merged.append({"role": "assistant", "content": last_output})
    return merged


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
    # Hermes turn：从 GENERATION observations 反推 messages，使下游 work_analytics.all_messages 可用。
    if (
        name == "Hermes turn"
        and structured.plugin_metadata is not None
        and not structured.plugin_metadata.messages
    ):
        injected = _hermes_messages_from_observations(obs_raw)
        if injected:
            structured = structured.model_copy(
                update={
                    "plugin_metadata": structured.plugin_metadata.model_copy(
                        update={"messages": injected, "message_count": len(injected)}
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


def fetch_trace_details(
    client: Any,
    trace_ids: list[str],
    cache: dict[str, LangfuseTraceDetailRecord] | None = None,
) -> dict[str, LangfuseTraceDetailRecord]:
    """Fetch full trace details for *trace_ids*, reusing entries in *cache* when provided."""
    out = cache if cache is not None else {}
    for tid in trace_ids:
        if tid not in out:
            full = client.api.trace.get(tid)
            out[tid] = trace_detail_from_api(full)
    return out


def trace_ref_from_detail(
    detail: LangfuseTraceDetailRecord,
    *,
    user_id: str | None = None,
    include_plugin_tokens: bool = True,
) -> LangfuseTraceRef:
    """Build a lightweight ``LangfuseTraceRef`` from a fetched trace detail."""
    tokens: LangfuseTraceTokens | None = None
    if include_plugin_tokens and is_plugin_trace(detail.name):
        tokens = tokens_from_detail(detail)
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
        latency_seconds=detail.latency_seconds,
    )
