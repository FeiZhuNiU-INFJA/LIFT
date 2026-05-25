import json
from pathlib import Path
from typing import Any

import pandas as pd


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


def make_row(
    task: dict[str, Any],
    variant_name: str,
    run_index: int,
    benchmark_name: str | None,
    benchmark_path: str | None,
) -> dict[str, Any]:
    side = (task or {}).get(variant_name) or {}
    agent_input = extract_last_agent_input(side)
    work_analytics = extract_work_analytics(side)
    global_stats = work_analytics.get("global_stats") or {}
    all_messages = work_analytics.get("all_messages") or []
    chat_turns = work_analytics.get("chat_turns") or []
    total_tokens, cached_tokens = aggregate_message_tokens(all_messages)
    trials = max(len(chat_turns) - 1, 0)

    return {
        "run": run_index,
        "benchmark_name": benchmark_name,
        "benchmark_path": benchmark_path,
        "task_name": task.get("task_name"),
        "category": task.get("category"),
        "baseline": variant_name == "baseline",
        "evolved": variant_name == "evolved",
        "success": side.get("success"),
        "is_final_task": agent_input.get("is_final_task"),
        "is_ended": agent_input.get("is_ended"),
        "content_reqs": agent_input.get("content_reqs"),
        "trajectory_reqs": agent_input.get("trajectory_reqs"),
        "content_score": agent_input.get("content_score"),
        "task_query": agent_input.get("task_query"),
        "trials": trials,
        "tool_use_num": global_stats.get("tool_call_blocks"),
        "total_tokens": total_tokens,
        "cached_token": cached_tokens,
        "total_latency_seconds": work_analytics.get("total_latency_seconds"),
        "all_messages": dumps_json(all_messages),
    }


def build_extracted_dataframe(data: dict[str, Any]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    runs = data.get("runs")
    if not isinstance(runs, list):
        raise ValueError("Post-process expects report/enriched JSON to contain a top-level 'runs' list.")
    for run_index, run in enumerate(runs):
        for benchmark in (run or {}).get("benchmarks") or []:
            benchmark_name = benchmark.get("benchmark_name")
            benchmark_path = benchmark.get("benchmark_path")
            for task in benchmark.get("tasks") or []:
                rows.append(make_row(task, "baseline", run_index, benchmark_name, benchmark_path))
                rows.append(make_row(task, "evolved", run_index, benchmark_name, benchmark_path))

    return pd.DataFrame(
        rows,
        columns=[
            "run",
            "benchmark_name",
            "benchmark_path",
            "task_name",
            "category",
            "baseline",
            "evolved",
            "success",
            "is_final_task",
            "is_ended",
            "content_reqs",
            "trajectory_reqs",
            "content_score",
            "task_query",
            "trials",
            "tool_use_num",
            "total_tokens",
            "cached_token",
            "total_latency_seconds",
            "all_messages",
        ],
    )
