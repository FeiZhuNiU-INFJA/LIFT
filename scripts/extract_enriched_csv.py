import argparse
import json
from pathlib import Path

import pandas as pd


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def dumps_json(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def extract_last_agent_input(side: dict) -> dict:
    traces = (((side or {}).get("langfuse") or {}).get("work_agent_traces")) or []
    if not traces:
        return {}
    return traces[-1].get("agent_input") or {}


def extract_work_analytics(side: dict) -> dict:
    return (((side or {}).get("langfuse") or {}).get("work_analytics")) or {}


def _int_value(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def aggregate_message_tokens(all_messages: list[dict]) -> tuple[int, int]:
    total_tokens = 0
    cached_tokens = 0

    for message in all_messages or []:
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue

        total_tokens += _int_value(usage.get("totalTokens", usage.get("total_tokens")))
        # `cacheRead` is the actual cache-hit token count in the transcript usage payload.
        cached_tokens += _int_value(usage.get("cacheRead", usage.get("cache_read")))

    return total_tokens, cached_tokens


def make_row(task: dict, variant_name: str) -> dict:
    side = (task or {}).get(variant_name) or {}
    agent_input = extract_last_agent_input(side)
    work_analytics = extract_work_analytics(side)
    global_stats = work_analytics.get("global_stats") or {}
    all_messages = work_analytics.get("all_messages") or []
    chat_turns = work_analytics.get("chat_turns") or []
    total_tokens, cached_tokens = aggregate_message_tokens(all_messages)
    trials = max(len(chat_turns) - 1, 0)

    return {
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


def build_dataframe(data: dict) -> pd.DataFrame:
    rows = []
    for task in data.get("tasks") or []:
        rows.append(make_row(task, "baseline"))
        rows.append(make_row(task, "evolved"))
    return pd.DataFrame(
        rows,
        columns=[
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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract selected fields from an enriched evaluation JSON into CSV."
    )
    parser.add_argument("input_json", help="Path to the enriched JSON file.")
    parser.add_argument(
        "-o",
        "--output",
        help="Output CSV path. Defaults to <input_stem>_extracted.csv next to the input file.",
    )
    args = parser.parse_args()

    input_path = Path(args.input_json).resolve()
    output_path = (
        Path(args.output).resolve()
        if args.output
        else input_path.with_name(f"{input_path.stem}_extracted.csv")
    )

    data = load_json(input_path)
    df = build_dataframe(data)
    df.to_csv(output_path, index=False, encoding="utf-8-sig")

    print(f"Input: {input_path}")
    print(f"Rows: {len(df)}")
    print(f"Output: {output_path}")


if __name__ == "__main__":
    main()
