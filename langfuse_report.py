"""
试验：读取 evobench report JSON，从 Langfuse 拉取 trace 并与各 phase 串联。

用法（需 .env 中 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY，及可选 LANGFUSE_HOST）::

    python langfuse_report.py --report evobench-reports/evobench-runid-....json
    python langfuse_report.py --report path/to/report.json --task 0 --out /tmp/enriched.json

``stitch_phase_langfuse_traces`` 固定执行 trace.list + trace.get，合并 agent/plugin 并生成 work_analytics。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from langfuse import get_client

from src.report.langfuse_trace_stitch import stitch_phase_langfuse_traces
from src.models import OpenClawBenchmarkReport, OpenClawBenchmarkTaskRun


def _enrich_phase(client, run_tag: str, phase):
    if phase is None:
        return None
    bundle = stitch_phase_langfuse_traces(
        client,
        eval_run_tag=run_tag,
        work_session_id=phase.work_session_id,
        judge_session_id=phase.judge_session_id,
    )
    return phase.model_copy(update={"langfuse": bundle})


def enrich_report(report: OpenClawBenchmarkReport, client) -> OpenClawBenchmarkReport:
    run_tag = report.run_id
    new_tasks: list[OpenClawBenchmarkTaskRun] = []
    for tr in report.tasks:
        nb = _enrich_phase(client, run_tag, tr.baseline)
        ne = _enrich_phase(client, run_tag, tr.evolved) if tr.evolved else None
        new_tasks.append(OpenClawBenchmarkTaskRun(task_name=tr.task_name, category=tr.category, baseline=nb, evolved=ne))
    return report.model_copy(update={"tasks": new_tasks})


def main() -> None:
    ap = argparse.ArgumentParser(description="Stitch Langfuse traces into evobench report JSON")
    ap.add_argument("--report", type=Path, required=True, help="Path to OpenClawBenchmarkReport JSON")
    ap.add_argument("--task", type=int, default=None, help="Only enrich task index (0-based); default: all")
    ap.add_argument("--out", type=Path, default=None, help="Write enriched full report JSON here")
    ap.add_argument("--print-summary", action="store_true", help="Print per-phase trace counts only")
    args = ap.parse_args()

    report = OpenClawBenchmarkReport.from_json_file(args.report)
    client = get_client()

    if args.task is not None:
        tr = report.tasks[args.task]
        run_tag = report.run_id
        nb = _enrich_phase(client, run_tag, tr.baseline)
        ne = _enrich_phase(client, run_tag, tr.evolved) if tr.evolved else None
        single = OpenClawBenchmarkTaskRun(
            task_name=tr.task_name,
            category=tr.category,
            baseline=nb,
            evolved=ne,
        )
        if args.print_summary:
            _print_task_summary(single)
        else:
            print(json.dumps(single.model_dump(mode="json"), ensure_ascii=False, indent=2))
        if args.out:
            args.out.write_text(json.dumps(single.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8")
        return

    enriched = enrich_report(report, client)
    if args.print_summary:
        for i, tr in enumerate(enriched.tasks):
            print(f"=== task[{i}] {tr.task_name} ===")
            _print_task_summary(tr)
    else:
        print(enriched.model_dump_json(indent=2))
    if args.out:
        args.out.write_text(enriched.model_dump_json(indent=2), encoding="utf-8")


def _print_task_summary(tr: OpenClawBenchmarkTaskRun) -> None:
    for label, ph in ("baseline", tr.baseline), ("evolved", tr.evolved):
        if ph is None:
            continue
        lf = ph.langfuse
        if lf is None:
            print(f"  {label}: (no langfuse bundle)")
            continue
        wa = lf.work_analytics
        g = wa.global_stats if wa else None
        extra = ""
        if g is not None:
            extra = (
                f" | work_global: tokens_total={g.total_tokens} (in={g.input_tokens} out={g.output_tokens}) "
                f"tool_roundtrips={g.tool_roundtrips} chats={len(wa.chat_turns) if wa else 0}"
            )
        tok = sum((t.tokens.total_tokens if t.tokens else 0) for t in lf.work_agent_traces)
        print(
            f"  {label}: work_turns={len(lf.work_agent_traces)} judge_turns={len(lf.judge_agent_traces)} "
            f"work_tokens_sum={tok}{extra}"
        )


if __name__ == "__main__":
    main()
