"""Merge ``*_agent`` pre-chat traces with the following plugin trace(s).

Plugin trace = ``openclaw-plugin``（OpenClaw 模式）或 ``Hermes turn``（Hermes 模式）。

**配对算法（扩展贪心）**：

eval 侧 ``run_task`` 的 worker / judge 在碰到 LLM 超时 / 限流之类 provider 错误
时会**用同一原始 prompt 原地重试**（`_agent_chat_no_emit` 路径），并且**不再**
emit 新的 ``*_agent`` pre-chat span。这样多次重试产生的 plugin trace 都会落在
**同一条** ``*_agent`` 之后。

后处理因此采用扩展贪心：在同一 session 内按时间排序，遇到 agent 就开新桶；
后续连续若干条 plugin trace 全部归入当前桶；遇到下一条 agent 时 flush。
flush 规则：

- 桶内有 plugin trace ⇒ 选**最后一条** ``success=True`` 的 plugin（兜底
  最后一条），其字段 merge 到 agent，`provider_retry_count = len(plugins) - 1`。
- 桶内无 plugin（agent 没等到任何回包，例如 transport 整体崩） ⇒ 保留 agent 自身。
- agent 不存在但有零散 plugin（孤儿） ⇒ 走 ``_orphan_plugin_ref``。

跨 runtime 通用：不依赖 OpenClaw 特有的 ``plugin_metadata.success`` 字段做识别
（Hermes 不写 success），只在 flush 阶段用 success 做"哪条作为代表 plugin"
的次要选择。
"""

from __future__ import annotations

from src.report.langfuse_trace_parse import is_agent_trace, is_plugin_trace
from src.models import LangfuseTraceRef


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


def _choose_representative_plugin(plugins: list[LangfuseTraceRef]) -> LangfuseTraceRef:
    """从同 agent 下挂的多个 plugin trace 里选一条作为 merge 代表。

    优先选**最后一条** ``plugin_metadata.success == True`` 的 plugin（OpenClaw 写了
    success；Hermes 不写）。若全部都不是 success=True（或 metadata 缺失），
    fallback 到最后一条 —— 通常即"最后一次重试"对应的 plugin。
    """
    for p in reversed(plugins):
        meta = p.plugin_metadata
        if meta is not None and getattr(meta, "success", None) is True:
            return p
    return plugins[-1]


def _flush_bucket(
    pending_agent: LangfuseTraceRef | None,
    pending_plugins: list[LangfuseTraceRef],
    out: list[LangfuseTraceRef],
) -> None:
    """把当前 (agent, [plugins...]) 桶里的内容写入 ``out``。

    - 无 agent 但有 plugin ⇒ 全部走 orphan。
    - 有 agent 无 plugin ⇒ 直接保留 agent。
    - 有 agent 有 plugin ⇒ 选代表 plugin merge，``provider_retry_count = len(plugins) - 1``。
    """
    if pending_agent is None:
        for p in pending_plugins:
            out.append(_orphan_plugin_ref(p))
        return
    if not pending_plugins:
        out.append(pending_agent)
        return
    chosen = _choose_representative_plugin(pending_plugins)
    merged = merge_plugin_into_agent(pending_agent, chosen)
    merged = merged.model_copy(update={"provider_retry_count": len(pending_plugins) - 1})
    out.append(merged)


def _pair_single_session(refs: list[LangfuseTraceRef]) -> list[LangfuseTraceRef]:
    """扩展贪心：同 session 内按时间排序，把同 agent 之后的多条 plugin 全部累积到同一桶。"""
    ordered = sorted(refs, key=_sort_key_ts)
    turns: list[LangfuseTraceRef] = []

    pending_agent: LangfuseTraceRef | None = None
    pending_plugins: list[LangfuseTraceRef] = []

    for ref in ordered:
        if is_plugin_trace(ref.name):
            pending_plugins.append(ref)
            continue

        if is_agent_trace(ref.name):
            _flush_bucket(pending_agent, pending_plugins, turns)
            pending_agent = ref
            pending_plugins = []
            continue

        # 既不是 agent 也不是 plugin（例如 notify_failure）：先 flush 再原样保留。
        _flush_bucket(pending_agent, pending_plugins, turns)
        pending_agent = None
        pending_plugins = []
        turns.append(ref)

    _flush_bucket(pending_agent, pending_plugins, turns)
    return turns


def pair_session_traces_to_agent_turns(refs: list[LangfuseTraceRef]) -> list[LangfuseTraceRef]:
    """
    按 ``session_id`` 分组后，在每个 session 内按时间排序，
    将每条 plugin trace（``openclaw-plugin`` / ``Hermes turn``）
    合并到同 session 内**前面最近**的一条 ``*_agent`` trace。

    分组确保并行 task 的 trace 不会交叉错配。其它 trace（如 ``notify_failure``）
    单独保留；未配对的 agent 也会保留。

    扩展贪心配对支持 provider 错误原地重试场景：worker / judge 用同一 prompt
    重试时**不再** emit 新的 pre-chat span，多条 plugin trace 会全部挂在同一
    agent span 下，并据此推算 ``provider_retry_count``。
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
    Hermes 模式专用：``*_agent`` pre-chat span 的 ``session_id`` 是外部 work/judge
    session_id；``Hermes turn`` 的 ``session_id`` 是 hermes 内部 task_id（不一致），
    但 tags 中带有同样的 work/judge session_id（即 "session tag"）。

    扩展贪心：对每条 ``*_agent``，**贪心吃下**所有满足以下条件的 ``Hermes turn``：

      1. ``timestamp`` 严格晚于该 agent；
      2. agent 的 ``session_id`` 出现在 plugin 的 ``tags`` 中；
      3. 尚未被其它 agent 占用；
      4. ``timestamp`` 早于"下一个时间上更晚的 ``*_agent``"（即不能跨到下一轮）。

    所有命中的 plugin 累积到同一桶，flush 时选代表 plugin 并计算
    ``provider_retry_count``。未配对的 ``*_agent`` / ``Hermes turn`` 一律不录入结果
    （丢弃 orphan），与历史行为一致。
    """
    ordered = sorted(refs, key=_sort_key_ts)
    used_plugin_idx: set[int] = set()

    # 收集所有 agent 的索引，方便确定每条 agent 的"配对窗口右边界"（下一条 agent 时间）。
    agent_indices = [i for i, r in enumerate(ordered) if is_agent_trace(r.name)]
    plugin_indices = [i for i, r in enumerate(ordered) if is_plugin_trace(r.name)]

    turns: list[LangfuseTraceRef] = []

    for k, idx in enumerate(agent_indices):
        agent_ref = ordered[idx]
        agent_ts = agent_ref.timestamp or ""
        agent_sid = agent_ref.session_id

        # 下一条 agent 的时间作为右边界；最后一条则不限。
        if k + 1 < len(agent_indices):
            next_agent_ts = ordered[agent_indices[k + 1]].timestamp or ""
        else:
            next_agent_ts = None

        bucket: list[LangfuseTraceRef] = []
        for j in plugin_indices:
            if j in used_plugin_idx:
                continue
            plugin = ordered[j]
            plugin_ts = plugin.timestamp or ""
            if plugin_ts <= agent_ts:
                continue
            if next_agent_ts is not None and plugin_ts >= next_agent_ts:
                continue
            if agent_sid and agent_sid not in (plugin.tags or []):
                continue
            used_plugin_idx.add(j)
            bucket.append(plugin)

        if not bucket:
            # 历史行为：未匹配到 hermes turn 的 agent 直接丢弃。
            continue

        chosen = _choose_representative_plugin(bucket)
        merged = merge_plugin_into_agent(agent_ref, chosen)
        merged = merged.model_copy(update={"provider_retry_count": len(bucket) - 1})
        turns.append(merged)

    turns.sort(key=_sort_key_ts)
    return turns
