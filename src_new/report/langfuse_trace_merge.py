"""Merge ``*_agent`` pre-chat traces with the following plugin trace (1:1 per turn).

Plugin trace = ``openclaw-plugin``（OpenClaw 模式）或 ``Hermes turn``（Hermes 模式）。
"""

from __future__ import annotations

from src_new.report.langfuse_trace_parse import is_agent_trace, is_plugin_trace
from src_new.models import LangfuseTraceRef


def _sort_key_ts(ref: LangfuseTraceRef) -> str:
    """Sort key: trace timestamp string (empty string sorts first)."""
    return ref.timestamp or ""


def merge_plugin_into_agent(agent: LangfuseTraceRef, plugin: LangfuseTraceRef) -> LangfuseTraceRef:
    """Copy plugin fields (prompt, response, metadata, tokens) onto an agent trace ref."""
    return agent.model_copy(
        update={
            "plugin_trace_id": plugin.id,
            "plugin_trace_name": plugin.name,
            "plugin_prompt": plugin.plugin_prompt,
            "plugin_response": plugin.plugin_response,
            "plugin_metadata": plugin.plugin_metadata,
            "tokens": plugin.tokens,
            "latency_seconds": plugin.latency_seconds,
        }
    )


def _pair_single_session(refs: list[LangfuseTraceRef]) -> list[LangfuseTraceRef]:
    """Pair agent and plugin traces within one session by chronological order."""
    ordered = sorted(refs, key=_sort_key_ts)
    turns: list[LangfuseTraceRef] = []
    pending_agent: LangfuseTraceRef | None = None

    for ref in ordered:
        if is_plugin_trace(ref.name):
            if pending_agent is not None:
                turns.append(merge_plugin_into_agent(pending_agent, ref))
                pending_agent = None
            else:
                # 未配对的 plugin trace：保留原始 name（openclaw-plugin 或 Hermes turn）。
                turns.append(_orphan_plugin_ref(ref))
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
    将每条 plugin trace（``openclaw-plugin`` / ``Hermes turn``）
    合并到同 session 内**前面最近**的一条 ``*_agent`` trace。

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


def _orphan_plugin_ref(ref: LangfuseTraceRef) -> LangfuseTraceRef:
    """未配对的 plugin trace：保留为独立 turn，原 name（openclaw-plugin 或 Hermes turn）不变。"""
    return LangfuseTraceRef(
        id=ref.id,
        name=ref.name,
        timestamp=ref.timestamp,
        session_id=ref.session_id,
        user_id=ref.user_id,
        tags=list(ref.tags),
        plugin_trace_id=ref.id,
        plugin_trace_name=ref.name,
        plugin_prompt=ref.plugin_prompt,
        plugin_response=ref.plugin_response,
        plugin_metadata=ref.plugin_metadata,
        tokens=ref.tokens,
        latency_seconds=ref.latency_seconds,
    )


def pair_hermes_traces_to_agent_turns(refs: list[LangfuseTraceRef]) -> list[LangfuseTraceRef]:
    """
    Hermes 模式专用：``*_agent`` pre-chat span 的 ``session_id`` 是外部 work/judge session_id；
    ``Hermes turn`` 的 ``session_id`` 是 hermes 内部 task_id（不一致），但 tags 中带有同样的
    work/judge session_id（即 "session tag"）。

    配对规则：对每条 ``*_agent``，选择满足以下条件的 ``Hermes turn`` 中**时间最近**的一条
    （即时间在其之后、session tag 一致、尚未被占用）：
      1. ``timestamp`` 严格晚于该 agent；
      2. agent 的 ``session_id`` 出现在 plugin 的 ``tags`` 中（session tag 一致）；
      3. 尚未被其它 agent 占用。

    未配对的 ``*_agent`` / ``Hermes turn`` 以及其它非 plugin / 非 agent 的 trace
    一律不录入结果（即丢弃 orphan）。返回结果按 timestamp 升序排序。
    """
    ordered = sorted(refs, key=_sort_key_ts)
    plugin_indices = [i for i, r in enumerate(ordered) if is_plugin_trace(r.name)]
    used_plugin_idx: set[int] = set()

    turns: list[LangfuseTraceRef] = []

    for agent_ref in ordered:
        if not is_agent_trace(agent_ref.name):
            continue
        agent_ts = agent_ref.timestamp or ""
        agent_sid = agent_ref.session_id
        # plugin_indices 已按时间升序，第一个满足条件的即"时间后最近的一条"。
        chosen: int | None = None
        for j in plugin_indices:
            if j in used_plugin_idx:
                continue
            plugin = ordered[j]
            plugin_ts = plugin.timestamp or ""
            if plugin_ts <= agent_ts:
                continue
            if agent_sid and agent_sid not in (plugin.tags or []):
                continue
            chosen = j
            break
        if chosen is None:
            # 未匹配到 hermes turn 的 agent 直接丢弃。
            continue
        used_plugin_idx.add(chosen)
        turns.append(merge_plugin_into_agent(agent_ref, ordered[chosen]))

    turns.sort(key=_sort_key_ts)
    return turns
