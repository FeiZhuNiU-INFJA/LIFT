"""Merge ``*_agent`` pre-chat traces with the following ``openclaw-plugin`` trace (1:1 per turn)."""

from __future__ import annotations

from src.report.langfuse_trace_parse import is_agent_trace, is_plugin_trace
from src.models import LANGFUSE_PLUGIN_TRACE_NAME, LangfuseTraceRef


def _sort_key_ts(ref: LangfuseTraceRef) -> str:
    return ref.timestamp or ""


def merge_plugin_into_agent(agent: LangfuseTraceRef, plugin: LangfuseTraceRef) -> LangfuseTraceRef:
    return agent.model_copy(
        update={
            "plugin_trace_id": plugin.id,
            "plugin_prompt": plugin.plugin_prompt,
            "plugin_response": plugin.plugin_response,
            "plugin_metadata": plugin.plugin_metadata,
            "tokens": plugin.tokens,
            "latency_seconds": plugin.latency_seconds,
        }
    )


def _pair_single_session(refs: list[LangfuseTraceRef]) -> list[LangfuseTraceRef]:
    ordered = sorted(refs, key=_sort_key_ts)
    turns: list[LangfuseTraceRef] = []
    pending_agent: LangfuseTraceRef | None = None

    for ref in ordered:
        if is_plugin_trace(ref.name):
            if pending_agent is not None:
                turns.append(merge_plugin_into_agent(pending_agent, ref))
                pending_agent = None
            else:
                turns.append(
                    LangfuseTraceRef(
                        id=ref.id,
                        name=LANGFUSE_PLUGIN_TRACE_NAME,
                        timestamp=ref.timestamp,
                        session_id=ref.session_id,
                        user_id=ref.user_id,
                        tags=list(ref.tags),
                        plugin_trace_id=ref.id,
                        plugin_prompt=ref.plugin_prompt,
                        plugin_response=ref.plugin_response,
                        plugin_metadata=ref.plugin_metadata,
                        tokens=ref.tokens,
                        latency_seconds=ref.latency_seconds,
                    )
                )
            continue

        if is_agent_trace(ref.name):
            if pending_agent is not None:
                turns.append(pending_agent)
            pending_agent = ref
            continue

        if pending_agent is not None:
            turns.append(pending_agent)
            pending_agent = None
        turns.append(ref)

    if pending_agent is not None:
        turns.append(pending_agent)
    return turns


def pair_session_traces_to_agent_turns(refs: list[LangfuseTraceRef]) -> list[LangfuseTraceRef]:
    """
    按 ``session_id`` 分组后，在每个 session 内按时间排序，
    将每条 ``openclaw-plugin`` 合并到同 session 内**前面最近**的一条 ``*_agent`` trace。

    分组确保并行 task 的 trace 不会交叉错配。其它 trace（如 ``notify_failure``）
    单独保留；未配对的 agent 也会保留。
    """
    session_groups: dict[str, list[LangfuseTraceRef]] = {}
    for ref in refs:
        sid = ref.session_id or "_unknown"
        session_groups.setdefault(sid, []).append(ref)

    turns: list[LangfuseTraceRef] = []
    for sid, group_refs in session_groups.items():
        turns.extend(_pair_single_session(group_refs))

    turns.sort(key=_sort_key_ts)
    return turns
