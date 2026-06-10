"""Build work-session analytics from already-fetched agent turn refs."""

from __future__ import annotations

from typing import Any

from src.models import (
    LangfuseDialogueTurn,
    LangfuseTokenToolStats,
    LangfuseTraceRef,
    LangfuseWorkChatTurn,
    LangfuseWorkSessionAnalytics,
)


def _stats_from_turn_ref(ref: LangfuseTraceRef) -> LangfuseTokenToolStats:
    """Build per-turn token/tool stats from a merged agent+plugin trace ref."""
    meta = ref.plugin_metadata
    if ref.tokens is not None:
        return LangfuseTokenToolStats(
            input_tokens=ref.tokens.input_tokens,
            output_tokens=ref.tokens.output_tokens,
            total_tokens=ref.tokens.total_tokens,
            tool_roundtrips=meta.tool_roundtrips if meta else 0,
            tool_call_blocks=meta.tool_call_blocks if meta else 0,
        )
    return LangfuseTokenToolStats()


def _dialogue_io(ref: LangfuseTraceRef) -> tuple[Any, Any]:
    """当轮对话 input/output：优先 plugin 侧文本，无 plugin 时回退 agent_input。"""
    inp: Any = ref.plugin_prompt
    out: Any = ref.plugin_response
    if inp is None and ref.agent_input is not None:
        inp = ref.agent_input.model_dump()
    return inp, out


def _dialogue_turn(turn_index: int, ref: LangfuseTraceRef) -> LangfuseDialogueTurn:
    """Build one ``LangfuseDialogueTurn`` from a trace ref at *turn_index*."""
    inp, out = _dialogue_io(ref)
    return LangfuseDialogueTurn(
        turn_index=turn_index,
        name=ref.name,
        timestamp=ref.timestamp,
        input=inp,
        output=out,
        latency_seconds=ref.latency_seconds,
    )


def _last_turn_messages(work_turns: list[LangfuseTraceRef]) -> list[Any]:
    """取最后一轮带 plugin messages 的 transcript（每轮 agent_end 多为全量，只保留末轮）。"""
    for ref in reversed(work_turns):
        meta = ref.plugin_metadata
        if meta is not None and meta.messages:
            return list(meta.messages)
    return []


def build_work_analytics(work_turns: list[LangfuseTraceRef]) -> LangfuseWorkSessionAnalytics:
    """仅 work 侧；每轮一条 trace_chain（input/output），与 work_agent_traces 对齐。"""
    trace_chain = [_dialogue_turn(i, ref) for i, ref in enumerate(work_turns)]
    all_messages = _last_turn_messages(work_turns)
    chat_turns: list[LangfuseWorkChatTurn] = []

    for i, ref in enumerate(work_turns):
        chat_turns.append(
            LangfuseWorkChatTurn(
                turn_index=i,
                agent_trace_id=ref.id,
                plugin_trace_id=ref.plugin_trace_id,
                latency_seconds=ref.latency_seconds,
                stats=_stats_from_turn_ref(ref),
            )
        )

    g = LangfuseTokenToolStats()
    total_latency = 0.0
    for t in chat_turns:
        g.input_tokens += t.stats.input_tokens
        g.output_tokens += t.stats.output_tokens
        g.total_tokens += t.stats.total_tokens
        g.tool_roundtrips += t.stats.tool_roundtrips
        g.tool_call_blocks += t.stats.tool_call_blocks
        g.tool_observation_count += t.stats.tool_observation_count
        if t.latency_seconds is not None:
            total_latency += t.latency_seconds

    return LangfuseWorkSessionAnalytics(
        trace_chain=trace_chain,
        chat_turns=chat_turns,
        global_stats=g,
        total_latency_seconds=total_latency,
        all_messages=all_messages,
    )
