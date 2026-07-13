"""Extract per-task metric rows from backfilled eval report JSON.

Flattens nested run/suite/task structure into a DataFrame with baseline and
evolved rows, applying OpenClaw- or Hermes-specific metric derivation rules.
"""

import json
from pathlib import Path
from typing import Any, TypeAlias

import pandas as pd


# Agent backend used when deriving metrics from Langfuse work analytics.
# 值域 = ``src.lift.adapters.registry.SUPPORTED_RUNTIMES``；此处只做语义标记，
# 具体合法值由 CLI/registry 单点定义，避免 Literal 与 tuple 双份漂移。
AgentSource: TypeAlias = str


def load_json(path: Path) -> dict[str, Any]:
    """Load and parse a UTF-8 JSON file at *path*."""
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def dumps_json(value: Any) -> str:
    """Serialize *value* to a JSON string without ASCII escaping."""
    return json.dumps(value, ensure_ascii=False)


def extract_last_agent_input(side: dict[str, Any]) -> dict[str, Any]:
    """Return ``agent_input`` from the last entry in ``work_agent_traces``."""
    traces = (((side or {}).get("langfuse") or {}).get("work_agent_traces")) or []
    if not traces:
        return {}
    return traces[-1].get("agent_input") or {}


def extract_work_analytics(side: dict[str, Any]) -> dict[str, Any]:
    """Return the ``work_analytics`` dict nested under ``langfuse`` on *side*."""
    return (((side or {}).get("langfuse") or {}).get("work_analytics")) or {}


def int_value(value: Any) -> int:
    """Coerce *value* to int; return 0 on None or conversion failure."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def aggregate_message_tokens(all_messages: list[dict[str, Any]]) -> tuple[int, int]:
    """Sum ``totalTokens`` and ``cacheRead`` across message usage dicts."""
    total_tokens = 0
    cached_tokens = 0

    for message in all_messages or []:
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        total_tokens += int_value(usage.get("totalTokens", usage.get("total_tokens")))
        cached_tokens += int_value(usage.get("cacheRead", usage.get("cache_read")))

    return total_tokens, cached_tokens


def _should_ignore_tool_call_block(block: dict[str, Any]) -> bool:
    """Return True for OpenClaw self-evolution ``exec`` calls to the local signal port."""
    if block.get("type") != "toolCall" or block.get("name") != "exec":
        return False
    command = (block.get("arguments") or {}).get("command", "")
    return isinstance(command, str) and "http://127.0.0.1:18090" in command


def _phase_plugin_trace_name(side: dict[str, Any]) -> str | None:
    """从 ``work_agent_traces`` 中取末轮的 ``plugin_trace_name``，用于识别 OpenClaw / Hermes。"""
    traces = (((side or {}).get("langfuse") or {}).get("work_agent_traces")) or []
    for ref in reversed(traces):
        name = ref.get("plugin_trace_name")
        if name:
            return name
    return None


def _make_row_openclaw(
    side: dict[str, Any],
    work_analytics: dict[str, Any],
) -> dict[str, Any]:
    """OpenClaw 模式：依赖 plugin metadata 的 toolCallBlocks 与 messages.usage 累加 token。

    现已不再对"最后一条 user 之后的 tool / token"做特殊裁剪，也不再对 ``trials``
    做 ``-1`` 处理：``run_task`` 不再在判定结束后追加额外 chat。
    """
    global_stats = work_analytics.get("global_stats") or {}
    all_messages = work_analytics.get("all_messages") or []
    chat_turns = work_analytics.get("chat_turns") or []
    total_tokens, cached_tokens = aggregate_message_tokens(all_messages)
    total_tool_calls = int_value(global_stats.get("tool_call_blocks"))
    cached_token_ratio = cached_tokens / total_tokens if total_tokens > 0 else 0.0
    trials = len(chat_turns)
    return {
        "trials": trials,
        "tool_use_num": total_tool_calls,
        "total_tokens": total_tokens,
        "cached_token": cached_tokens,
        "cached_token_ratio": cached_token_ratio,
        "total_latency_seconds": work_analytics.get("total_latency_seconds"),
        "all_messages": dumps_json(all_messages),
    }


def _aggregate_openhuman_cached_tokens(all_messages: list[dict[str, Any]]) -> int:
    """OpenHuman：assistant 消息的 ``usage.cached_input`` 累加。

    OpenHuman transcript usage schema 是 ``{input, output, cached_input, ...}``，
    没有 ``totalTokens``；``total_tokens`` 走 ``global_stats``（由 Langfuse GENERATION
    observation usage_details 累加，见 ``langfuse_trace_fetch._usage_triplet``），
    本函数只补 cache_read_input 缺失的一块。
    """
    cached = 0
    for message in all_messages or []:
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        cached += int_value(usage.get("cached_input", usage.get("cache_read_input_tokens")))
    return cached


def _make_row_openhuman(
    side: dict[str, Any],
    work_analytics: dict[str, Any],
) -> dict[str, Any]:
    """OpenHuman 模式：``usage`` schema 与 OpenClaw 不同，走 ``global_stats``。

    - ``total_tokens``：``global_stats.total_tokens``（Langfuse GENERATION usage 累加，权威）。
    - ``cached_token``：从 assistant messages 的 ``usage.cached_input`` 累加，``global_stats``
      不含该字段所以只能走 messages 通路。
    - ``tool_use_num``：``global_stats.tool_call_blocks``（transcript_langfuse 已按
      assistant 消息 tool_calls 计数）。
    - ``trials``：``chat_turns`` 长度（与 OpenClaw 保持一致语义）。
    """
    global_stats = work_analytics.get("global_stats") or {}
    all_messages = work_analytics.get("all_messages") or []
    chat_turns = work_analytics.get("chat_turns") or []
    total_tokens = int_value(global_stats.get("total_tokens"))
    cached_tokens = _aggregate_openhuman_cached_tokens(all_messages)
    total_tool_calls = int_value(global_stats.get("tool_call_blocks"))
    cached_token_ratio = cached_tokens / total_tokens if total_tokens > 0 else 0.0
    trials = len(chat_turns)
    return {
        "trials": trials,
        "tool_use_num": total_tool_calls,
        "total_tokens": total_tokens,
        "cached_token": cached_tokens,
        "cached_token_ratio": cached_token_ratio,
        "total_latency_seconds": work_analytics.get("total_latency_seconds"),
        "all_messages": dumps_json(all_messages),
    }


def _make_row_hermes(
    side: dict[str, Any],
    work_analytics: dict[str, Any],
) -> dict[str, Any]:
    """Hermes 模式：
    - ``total_tokens``：直接使用 trace 聚合的 ``global_stats.total_tokens``（与 messages 无关，
      由 GENERATION observation usage 累加得到，准确）。
    - ``trials``：``chat_turns`` 长度，与 OpenClaw 口径对齐，避免 ``all_messages`` 在
      context compaction 后丢失早期 user message 导致少算 / 把 compaction summary
      误算成 user message。
    - ``tool_use_num``：``global_stats.tool_call_blocks``，由每个 ``Hermes turn`` chain 的
      ``output.tool_calls`` 长度累加得到（见 ``langfuse_trace_fetch._hermes_tool_call_count_from_output``）。
      插件在 ``_finish_trace`` 时把整轮累计 tool_calls 注入 root output，不受上下文压缩影响。
      兼容旧 enriched JSON：若 ``tool_call_blocks`` 缺失/为 0（插件修复前的产物未回填该字段），
      回退为 ``all_messages`` 中 ``role == 'tool'`` 的 message 数。
    - ``cached_token`` / ``cached_token_ratio``：当前 hermes 链路未上报 cacheRead，置 0。
    - ``all_messages`` 来源：``LangfuseTraceDetailRecord.plugin_metadata.messages``，
      由插件在 root span（``Hermes turn`` chain）的 metadata.messages 全量写入。
      当前插件已不在 GENERATION 子节点 metadata 中保存 messages，因此 root 缺失即
      全量缺失（不再有兜底回填）。``all_messages`` 仅作为 transcript 留档，
      ``trials`` / ``total_tokens`` 不依赖 messages。
    """
    global_stats = work_analytics.get("global_stats") or {}
    all_messages = work_analytics.get("all_messages") or []
    chat_turns = work_analytics.get("chat_turns") or []
    total_tokens = int_value(global_stats.get("total_tokens"))
    trials = len(chat_turns)
    tool_use_num = int_value(global_stats.get("tool_call_blocks"))
    if tool_use_num == 0:
        tool_use_num = sum(
            1 for m in all_messages or [] if isinstance(m, dict) and m.get("role") == "tool"
        )
    return {
        "trials": trials,
        "tool_use_num": tool_use_num,
        "total_tokens": total_tokens,
        "cached_token": 0,
        "cached_token_ratio": 0.0,
        "total_latency_seconds": work_analytics.get("total_latency_seconds"),
        "all_messages": dumps_json(all_messages),
    }


def _make_row_genericagent(
    side: dict[str, Any],
    work_analytics: dict[str, Any],
) -> dict[str, Any]:
    """GenericAgent 模式：token 走 ``global_stats``（GA overlay 的 generation usage 累加）。

    - ``total_tokens``：``global_stats.total_tokens``（GA overlay 每次 LLM 调用挂
      GENERATION observation，``usage_details`` 由 SSE tee 解析得到；messages 里不带
      ``totalTokens``，故不能走 ``_make_row_openclaw`` 的 messages 累加口径）。
    - ``tool_use_num``：``global_stats.tool_call_blocks``（GA overlay 在 agent_after
      写 metadata.toolCallBlocks 后有值）；缺失/为 0 时回退 ``global_stats.tool_observation_count``
      （``type=TOOL`` observation 数），保证有工具调用时不被静默丢成 0。
    - ``cached_token``：从 ``all_messages`` 的 ``usage.cache_read_input_tokens`` / ``cacheRead``
      累加（``global_stats`` 不含该字段；GA 多数情况下 messages 无 usage，则为 0）。
    - ``trials``：``chat_turns`` 长度（与 OpenClaw 保持一致语义）。
    """
    global_stats = work_analytics.get("global_stats") or {}
    all_messages = work_analytics.get("all_messages") or []
    chat_turns = work_analytics.get("chat_turns") or []
    total_tokens = int_value(global_stats.get("total_tokens"))
    total_tool_calls = int_value(global_stats.get("tool_call_blocks"))
    if total_tool_calls == 0:
        total_tool_calls = int_value(global_stats.get("tool_observation_count"))
    cached_tokens = _aggregate_openhuman_cached_tokens(all_messages)
    cached_token_ratio = cached_tokens / total_tokens if total_tokens > 0 else 0.0
    trials = len(chat_turns)
    return {
        "trials": trials,
        "tool_use_num": total_tool_calls,
        "total_tokens": total_tokens,
        "cached_token": cached_tokens,
        "cached_token_ratio": cached_token_ratio,
        "total_latency_seconds": work_analytics.get("total_latency_seconds"),
        "all_messages": dumps_json(all_messages),
    }


def make_row(
    task: dict[str, Any],
    variant_name: str,
    run_index: int,
    suite_name: str | None,
    suite_path: str | None,
    agent_source: AgentSource = "openclaw",
) -> dict[str, Any]:
    """Build one flat metric row for a single task variant (baseline or evolved)."""
    side = (task or {}).get(variant_name) or {}
    agent_input = extract_last_agent_input(side)
    work_analytics = extract_work_analytics(side)

    if agent_source == "hermes":
        metric_row = _make_row_hermes(side, work_analytics)
    elif agent_source == "openhuman":
        metric_row = _make_row_openhuman(side, work_analytics)
    elif agent_source in ("genericagent", "genericagent_active_evolve"):
        metric_row = _make_row_genericagent(side, work_analytics)
    else:
        metric_row = _make_row_openclaw(side, work_analytics)

    return {
        "run": run_index,
        "suite_name": suite_name,
        "suite_path": suite_path,
        "task_name": task.get("task_name"),
        # 口径统一为 ``suite``（即 asset/benchmarks 下的 JSON 名）。兼容旧 report JSON
        # 仍用 ``category`` 写入场景名的情况。
        "suite": task.get("suite") if task.get("suite") is not None else task.get("category"),
        "baseline": variant_name == "baseline",
        "evolved": variant_name == "evolved",
        "success": side.get("success"),
        "is_final_task": agent_input.get("is_final_task"),
        "content_reqs": agent_input.get("content_reqs"),
        "trajectory_reqs": agent_input.get("trajectory_reqs"),
        # ``content_score`` 优先取 PhaseRun 一级字段（由 ``run_task`` /
        # ``openclaw_run_task`` 直接返回），它在 hermes 路径下也保证有值；
        # 仅当一级字段缺失（旧 backfilled JSON）才退回 agent_input。
        "content_score": (
            side.get("content_score")
            if side.get("content_score") is not None
            else agent_input.get("content_score")
        ),
        "task_query": agent_input.get("task_query"),
        **metric_row,
    }


def _iter_suites(run: dict[str, Any]) -> list[dict[str, Any]]:
    """Yield suite dicts from a run, supporting the older ``benchmarks`` key."""
    suites = (run or {}).get("suites")
    if isinstance(suites, list) and suites:
        return suites
    # Older report JSON used "benchmarks".
    benchmarks = (run or {}).get("benchmarks")
    if isinstance(benchmarks, list) and benchmarks:
        return benchmarks
    if isinstance(run, dict) and "tasks" in run:
        return [run]
    return []


def build_extracted_dataframe(
    data: dict[str, Any],
    agent_source: AgentSource = "openclaw",
) -> pd.DataFrame:
    """Flatten report JSON into a DataFrame with one row per task variant."""
    rows: list[dict[str, Any]] = []
    runs = data.get("runs")
    if not isinstance(runs, list):
        raise ValueError("Post-process expects report/backfilled JSON to contain a top-level 'runs' list.")
    for run_index, run in enumerate(runs):
        for suite in _iter_suites(run):
            suite_name = suite.get("suite_name") or suite.get("benchmark_name")
            suite_path = suite.get("suite_path") or suite.get("benchmark_path")
            for task in suite.get("tasks") or []:
                rows.append(
                    make_row(task, "baseline", run_index, suite_name, suite_path, agent_source)
                )
                rows.append(
                    make_row(task, "evolved", run_index, suite_name, suite_path, agent_source)
                )

    return pd.DataFrame(
        rows,
        columns=[
            "run",
            "suite_name",
            "suite_path",
            "task_name",
            "suite",
            "baseline",
            "evolved",
            "success",
            "is_final_task",
            "content_reqs",
            "trajectory_reqs",
            "content_score",
            "task_query",
            "trials",
            "tool_use_num",
            "total_tokens",
            "cached_token",
            "cached_token_ratio",
            "total_latency_seconds",
            "all_messages",
        ],
    )
