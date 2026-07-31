"""Extract per-task metric rows from backfilled eval report JSON.

Flattens nested run/suite/task structure into a DataFrame with baseline and
evolved rows. Token 字段全部走 ``PhaseLangfuseBundle.work_analytics.global_stats``
（5 字段 ``LangfuseTokenToolStats``），provider 差异已在 Langfuse 归一层
``langfuse_trace_fetch._usage_breakdown`` 消化：``input_tokens`` / ``cache_write_tokens``
/ ``cache_read_tokens`` / ``output_tokens`` / ``reasoning_tokens``。

指标提取已统一为 **runtime-agnostic**（见
docs/langfuse-unified-observation-contract）：所有 runtime 的插件都遵循同一观测契约
（root span 承载"同 session 跨轮累积"的 messages / ``output.tool_calls`` /
``metadata.toolCallBlocks``），因此不再按 ``agent_source`` 分叉出
``_make_row_<runtime>``，统一走 ``_make_metric_row``。
"""

import json
from pathlib import Path
from typing import Any, TypeAlias

import pandas as pd


# Agent backend used when deriving metrics from Langfuse work analytics.
# 值域 = ``src.lift.adapters.registry.SUPPORTED_RUNTIMES``；此处只做语义标记，
# 具体合法值由 CLI/registry 单点定义，避免 Literal 与 tuple 双份漂移。
AgentSource: TypeAlias = str

# 仅供 report_html 决定"隐藏哪些展示列"（Hermes 无 per-turn latency）；
# **指标提取本身不再消费它**——统一契约后所有 runtime 走同一套读法。
HERMES_AGENT_SOURCES = {
    "hermes",
    "hermes_with_openspace",
    "hermes_with_agentmemory",
}


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


def _should_ignore_tool_call_block(block: dict[str, Any]) -> bool:
    """Return True for OpenClaw self-evolution ``exec`` calls to the local signal port."""
    if block.get("type") != "toolCall" or block.get("name") != "exec":
        return False
    command = (block.get("arguments") or {}).get("command", "")
    return isinstance(command, str) and "http://127.0.0.1:18090" in command


def _base_token_row(global_stats: dict[str, Any]) -> dict[str, Any]:
    """从 ``global_stats``（``LangfuseTokenToolStats`` 序列化后的 dict）读 5 字段 token。

    ``total_tokens`` 与 ``cache_hit_ratio`` 是 pydantic ``computed_field``，落盘时会写进
    dict；读取时优先取，缺失/为 0 时按同样规则自算（防御旧 backfilled JSON 里没落该字段）。
    """
    input_tokens = int_value(global_stats.get("input_tokens"))
    cache_write_tokens = int_value(global_stats.get("cache_write_tokens"))
    cache_read_tokens = int_value(global_stats.get("cache_read_tokens"))
    output_tokens = int_value(global_stats.get("output_tokens"))
    reasoning_tokens = int_value(global_stats.get("reasoning_tokens"))
    total_tokens = int_value(global_stats.get("total_tokens"))
    if total_tokens == 0:
        total_tokens = input_tokens + cache_write_tokens + cache_read_tokens + output_tokens
    denom = input_tokens + cache_write_tokens + cache_read_tokens
    cache_hit_ratio = (cache_read_tokens / denom) if denom > 0 else 0.0
    return {
        "input_tokens": input_tokens,
        "cache_write_tokens": cache_write_tokens,
        "cache_read_tokens": cache_read_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": total_tokens,
        "cache_hit_ratio": cache_hit_ratio,
    }


def _make_metric_row(work_analytics: dict[str, Any]) -> dict[str, Any]:
    """构建单个 phase 的指标行（runtime-agnostic，统一观测契约下无需按 runtime 分叉）。

    统一契约（见 docs/langfuse-unified-observation-contract）保证所有 runtime 的
    ``work_analytics`` 都对齐同一口径，因此这里对所有 runtime 用同一套读法：

    - ``trials``：``chat_turns`` 长度（= 该 session 内 eval turn 数），跨 runtime 一致，
      避免 ``all_messages`` 在 context compaction 后丢早期 message 导致误算。
    - ``tool_use_num``：``global_stats.tool_call_blocks`` —— 由 ``build_work_analytics``
      对各轮 root span 的"跨轮累积计数"取 max（= 最末轮累积值，权威来源是各插件写入
      root ``output.tool_calls`` 的长度，见 ``langfuse_trace_fetch``）。缺失/为 0 时统一
      回退 ``tool_observation_count``（``type=TOOL`` 子 observation 数，overlay 每次工具
      调用挂 ``as_type='tool'`` 时才有值）。不再有任何 runtime 专属分支。
    - Token 5 字段走归一后的 ``global_stats``（``_usage_breakdown`` 已消化 provider 差异）。
    """
    global_stats = work_analytics.get("global_stats") or {}
    all_messages = work_analytics.get("all_messages") or []
    chat_turns = work_analytics.get("chat_turns") or []
    tool_use_num = int_value(global_stats.get("tool_call_blocks"))
    if tool_use_num == 0:
        tool_use_num = int_value(global_stats.get("tool_observation_count"))
    return {
        "trials": len(chat_turns),
        "tool_use_num": tool_use_num,
        **_base_token_row(global_stats),
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
    """Build one flat metric row for a single task variant (baseline or evolved).

    ``agent_source`` 保留在签名里仅为调用方兼容（下游 ``report_html`` 等仍按 runtime
    调整展示列），指标提取本身已统一为 runtime-agnostic，不再按 ``agent_source`` 分叉。
    """
    side = (task or {}).get(variant_name) or {}
    agent_input = extract_last_agent_input(side)
    work_analytics = extract_work_analytics(side)
    metric_row = _make_metric_row(work_analytics)

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
            "input_tokens",
            "cache_write_tokens",
            "cache_read_tokens",
            "output_tokens",
            "reasoning_tokens",
            "total_tokens",
            "cache_hit_ratio",
            "total_latency_seconds",
            "all_messages",
        ],
    )
