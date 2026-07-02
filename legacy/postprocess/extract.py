import json
from pathlib import Path
from typing import Any, Literal

import pandas as pd


AgentSource = Literal["openclaw", "hermes"]


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def dumps_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def extract_last_agent_input(side: dict[str, Any]) -> dict[str, Any]:
    traces = (((side or {}).get("langfuse") or {}).get("work_agent_traces")) or []
    if not traces:
        return {}
    return traces[-1].get("agent_input") or {}


def extract_work_analytics(side: dict[str, Any]) -> dict[str, Any]:
    return (((side or {}).get("langfuse") or {}).get("work_analytics")) or {}


def int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def aggregate_message_tokens(all_messages: list[dict[str, Any]]) -> tuple[int, int]:
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
    - ``cached_token`` / ``cached_token_ratio``：当前 hermes 链路未上报 cacheRead，置 0。
    - ``all_messages`` 来源：``LangfuseTraceDetailRecord.plugin_metadata.messages``，
      由插件在 root span（``Hermes turn`` chain）的 metadata.messages 全量写入。
      当前插件已不在 GENERATION 子节点 metadata 中保存 messages，因此 root 缺失即
      全量缺失（不再有兜底回填）。``all_messages`` 仅作为 transcript 留档，
      ``trials`` / ``tool_use_num`` / ``total_tokens`` 不依赖 messages。
    """
    global_stats = work_analytics.get("global_stats") or {}
    all_messages = work_analytics.get("all_messages") or []
    chat_turns = work_analytics.get("chat_turns") or []
    total_tokens = int_value(global_stats.get("total_tokens"))
    trials = len(chat_turns)
    tool_use_num = int_value(global_stats.get("tool_call_blocks"))
    return {
        "trials": trials,
        "tool_use_num": tool_use_num,
        "total_tokens": total_tokens,
        "cached_token": 0,
        "cached_token_ratio": 0.0,
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
    side = (task or {}).get(variant_name) or {}
    agent_input = extract_last_agent_input(side)
    work_analytics = extract_work_analytics(side)

    if agent_source == "hermes":
        metric_row = _make_row_hermes(side, work_analytics)
    else:
        metric_row = _make_row_openclaw(side, work_analytics)

    return {
        "run": run_index,
        "suite_name": suite_name,
        "suite_path": suite_path,
        "task_name": task.get("task_name"),
        "category": task.get("category"),
        "baseline": variant_name == "baseline",
        "evolved": variant_name == "evolved",
        "success": side.get("success"),
        "is_final_task": agent_input.get("is_final_task"),
        "content_reqs": agent_input.get("content_reqs"),
        "trajectory_reqs": agent_input.get("trajectory_reqs"),
        # ``content_score`` 优先取 PhaseRun 一级字段（由 ``run_task`` /
        # ``openclaw_run_task`` 直接返回），它在 hermes 路径下也保证有值；
        # 仅当一级字段缺失（旧 enriched JSON）才退回 agent_input。
        "content_score": (
            side.get("content_score")
            if side.get("content_score") is not None
            else agent_input.get("content_score")
        ),
        "task_query": agent_input.get("task_query"),
        **metric_row,
    }


def _iter_suites(run: dict[str, Any]) -> list[dict[str, Any]]:
    suites = (run or {}).get("suites")
    if isinstance(suites, list) and suites:
        return suites
    # legacy report JSON used "benchmarks"
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
    rows: list[dict[str, Any]] = []
    runs = data.get("runs")
    if not isinstance(runs, list):
        raise ValueError("Post-process expects report/enriched JSON to contain a top-level 'runs' list.")
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
            "category",
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
