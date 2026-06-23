"""CLI and pipeline entry points for post-processing eval report JSON.

Orchestrates trace backfill, metric extraction, trajectory judging, CSV export,
and HTML report generation from a benchmark report or pre-backfilled JSON.
"""

import argparse
import json
import math
import time
from pathlib import Path
import sys
from typing import Literal, TYPE_CHECKING

import pandas as pd

# Project root added to ``sys.path`` so the module can be run as a script.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.postprocess.extract import build_extracted_dataframe, load_json
from src.postprocess.trace_backfill import backfill_report, get_langfuse_client
from src.postprocess.judge import attach_trajectory_scores
from src.postprocess.metrics import build_comparison_dataframe, build_summary_dataframe, print_summary_to_console, validate_pairs
from src.postprocess.report_html import build_trajectory_map, render_report_html
from src.config import LOGGER
from src.models import EvalReport
from src.paths import results_run_dir

if TYPE_CHECKING:
    from src.lift.status.state import RunStateTracker


# Agent backend for trace stitching and metric derivation.
AgentSource = Literal["openclaw", "openclaw_with_evolve", "hermes", "genericagent", "genericagent_active_evolve"]


def default_output_paths(output_dir: Path, output_prefix: str) -> tuple[Path, Path, Path, Path]:
    """Create *output_dir* and return default paths for backfilled JSON, CSVs, and HTML."""
    output_dir.mkdir(parents=True, exist_ok=True)
    backfilled_json = output_dir / f"{output_prefix}_backfilled.json"
    comparison_csv = output_dir / f"{output_prefix}_comparison_metrics.csv"
    summary_csv = output_dir / f"{output_prefix}_summary_metrics.csv"
    report_html = output_dir / f"{output_prefix}_metrics_report.html"
    return backfilled_json, comparison_csv, summary_csv, report_html


def default_results_dir(input_path: Path) -> tuple[Path, str]:
    """Resolve the results output directory and run-id prefix from *input_path*."""
    data = load_json(input_path)
    run_id = data.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        run_id = input_path.stem
    output_dir = results_run_dir(run_id)
    return output_dir, run_id


def is_backfilled_report(data: dict) -> bool:
    """Return True if any task variant in *data* already has Langfuse traces attached."""
    runs = data.get("runs")
    if not isinstance(runs, list):
        raise ValueError(
            "Post-process expects report/backfilled JSON to contain a top-level 'runs' list."
        )
    for run in runs:
        suites = (run or {}).get("suites") or (run or {}).get("benchmarks") or []
        for suite in suites:
            for task in suite.get("tasks") or []:
                for variant_name in ("baseline", "evolved"):
                    variant = task.get(variant_name) or {}
                    if variant.get("langfuse") is not None:
                        return True
    return False


# Legacy alias
is_enriched_report = is_backfilled_report  # Deprecated alias for is_backfilled_report.


def load_or_backfill_report(
    input_path: Path,
    agent_source: AgentSource = "openclaw",
) -> tuple[dict, str]:
    """Load report JSON from disk, backfilling Langfuse traces when not already present."""
    data = load_json(input_path)
    if is_backfilled_report(data):
        return data, data.get("run_id") or input_path.stem

    report = EvalReport.from_json_file(input_path)
    client = get_langfuse_client()
    backfilled = backfill_report(report, client, agent_source)
    backfilled_data = json.loads(backfilled.model_dump_json())
    return backfilled_data, report.run_id


# Legacy alias
load_or_enrich_report = load_or_backfill_report  # Deprecated alias for load_or_backfill_report.


def process_report_to_outputs(
    input_path: Path,
    *,
    backfilled_json: Path | None = None,
    comparison_csv: Path,
    summary_csv: Path,
    report_html: Path,
    agent_source: AgentSource = "openclaw",
) -> tuple[Path | None, Path, Path, Path, pd.DataFrame]:
    """Run the full post-process pipeline and write CSV/HTML (and optional backfilled JSON).

    Returns the tuple of output paths plus the summary DataFrame so callers can
    forward it to a status tracker / dashboard for live display.
    """
    data, title_stem = load_or_backfill_report(input_path, agent_source)
    extracted_df = build_extracted_dataframe(data, agent_source)
    scored_df = attach_trajectory_scores(extracted_df)
    validate_pairs(scored_df)
    comparison_df = build_comparison_dataframe(scored_df)
    summary_df = build_summary_dataframe(comparison_df, scored_df)
    trajectory_map = build_trajectory_map(scored_df)

    if backfilled_json is not None:
        backfilled_json.parent.mkdir(parents=True, exist_ok=True)
        backfilled_json.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    comparison_csv.parent.mkdir(parents=True, exist_ok=True)
    summary_csv.parent.mkdir(parents=True, exist_ok=True)
    report_html.parent.mkdir(parents=True, exist_ok=True)

    comparison_df.to_csv(comparison_csv, index=False, encoding="utf-8-sig")
    summary_df.to_csv(summary_csv, index=False, encoding="utf-8-sig")

    html_text = render_report_html(
        comparison_df=comparison_df,
        summary_df=summary_df,
        title=f"{title_stem} Metrics Report",
        agent_source=agent_source,
        trajectory_map=trajectory_map,
    )
    report_html.write_text(html_text, encoding="utf-8")
    print_summary_to_console(summary_df)
    return backfilled_json, comparison_csv, summary_csv, report_html, summary_df


def post_process_results_dir(run_id: str) -> Path:
    """Return (and create) the results directory for *run_id*."""
    results_dir = results_run_dir(run_id)
    results_dir.mkdir(parents=True, exist_ok=True)
    return results_dir


def _coerce_metric_value(v: object) -> float | None:
    """``NaN`` / non-numeric → None, otherwise float."""
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def build_final_summary_from_df(
    summary_df: pd.DataFrame,
    *,
    artifacts: dict[str, Path],
):
    """Convert the post-process ``summary_df`` into a ``FinalSummary`` dataclass
    suitable for ``RunStateTracker.set_final_summary``.

    Imported lazily so this module stays importable without the status package.
    """
    from src.lift.status.state import FinalSummary, FinalSummaryRow

    rows: list[FinalSummaryRow] = []
    for _, raw in summary_df.iterrows():
        scope = str(raw.get("scope") or "")
        # category 行用 "category" 字段，global 用固定 "ALL"
        label = str(raw.get("category") or raw.get("label") or "")
        metrics: dict[str, float | None] = {}
        for col, val in raw.items():
            if not isinstance(col, str):
                continue
            if col.startswith("mean_impr_") or col.startswith("mean_diff_"):
                metrics[col] = _coerce_metric_value(val)
        rows.append(
            FinalSummaryRow(
                scope=scope,
                label=label,
                task_count=int(raw.get("task_count") or 0),
                task_count_aggregated=int(raw.get("task_count_aggregated") or 0),
                task_count_excluded=int(raw.get("task_count_excluded") or 0),
                baseline_success_rate=_coerce_metric_value(raw.get("baseline_success_rate")),
                evolved_success_rate=_coerce_metric_value(raw.get("evolved_success_rate")),
                metrics=metrics,
            )
        )

    return FinalSummary(
        rows=rows,
        artifact_paths={k: str(v) for k, v in artifacts.items() if v is not None},
        completed_at=time.time(),
    )


def _stringify(value: object) -> str:
    """把 input/output（可能是 str / dict / list[message]）统一转成可读文本。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def build_dialogue_bundle_from_report(data: dict):
    """从 backfilled report dict 抽取 per-phase 对话（B 路径）。

    对话来自 ``runs[r].suites[s].tasks[t].{baseline|evolved}.langfuse.work_analytics.trace_chain``
    （每轮 input/output 文本）。坐标：``r``→repeat_index、``s``→suite_index（数组下标，
    与 pipeline ``repeat_run.suites[suite_index]`` 占位回填一致），task 用 ``task["task_name"]``。
    ``trace_chain`` 仅 work 侧，``judge_*`` 留空；无 ``trace_chain`` 的 phase 不进 bundle
    （保留运行期 dialogue）。返回 ``{(repeat_index, suite_index, task_name, phase): [DialogueTurn]}``。
    """
    from src.lift.status.state import DialogueTurn

    bundle: dict[tuple[int, int, str, str], list[DialogueTurn]] = {}
    for r_idx, run in enumerate(data.get("runs", [])):
        for s_idx, suite in enumerate(run.get("suites", [])):
            for task in suite.get("tasks", []):
                task_name = task.get("task_name")
                if not task_name:
                    continue
                for phase in ("baseline", "evolved"):
                    pr = task.get(phase) or {}
                    wa = (pr.get("langfuse") or {}).get("work_analytics") or {}
                    trace_chain = wa.get("trace_chain") or []
                    if not trace_chain:
                        continue
                    turns: list[DialogueTurn] = []
                    for i, tc in enumerate(trace_chain):
                        turns.append(
                            DialogueTurn(
                                turn_index=int(tc.get("turn_index", i)),
                                work_prompt=_stringify(tc.get("input")),
                                work_result=_stringify(tc.get("output")),
                                judge_success=False,
                                judge_score=0.0,
                                judge_reason="",
                                latency_seconds=tc.get("latency_seconds"),
                            )
                        )
                    bundle[(r_idx, s_idx, task_name, phase)] = turns
    return bundle


def build_phase_tool_calls_from_report(
    data: dict,
) -> dict[tuple[int, int, str, str], int]:
    """从 backfilled report dict 抽取 per-phase ``tool_calls``（B 路径）。

    适配器 baseline 没 override ``count_tool_calls`` 时（GA / Hermes），运行期
    ``phase.tool_calls`` 是 None；trace_backfill 通过 langfuse 兜底已经把数填回
    ``runs[r].suites[s].tasks[t].{baseline|evolved}.tool_calls``。这里把这份
    后处理结果再推回 tracker，让静态 dashboard 渲染时能用上。
    """
    bundle: dict[tuple[int, int, str, str], int] = {}
    for r_idx, run in enumerate(data.get("runs", [])):
        for s_idx, suite in enumerate(run.get("suites", [])):
            for task in suite.get("tasks", []):
                task_name = task.get("task_name")
                if not task_name:
                    continue
                for phase in ("baseline", "evolved"):
                    pr = task.get(phase) or {}
                    tc = pr.get("tool_calls")
                    if isinstance(tc, int) and tc >= 0:
                        bundle[(r_idx, s_idx, task_name, phase)] = tc
    return bundle


def run_post_process_pipeline(
    run_id: str,
    report_path: Path,
    agent_source: AgentSource = "openclaw",
    *,
    tracker: "RunStateTracker | None" = None,
) -> None:
    """对已生成的 benchmark report JSON 执行后处理（trace_backfill + 指标 + HTML）。

    若提供 ``tracker``，后处理完成后把 ``FinalSummary`` 注入快照，供 dashboard
    展示并用于静态 HTML 导出。
    """
    results_dir = post_process_results_dir(run_id)
    backfilled_json, comparison_csv, summary_csv, report_html = default_output_paths(
        results_dir, run_id
    )
    try:
        backfilled_json, comparison_csv, summary_csv, report_html, summary_df = (
            process_report_to_outputs(
                report_path,
                backfilled_json=backfilled_json,
                comparison_csv=comparison_csv,
                summary_csv=summary_csv,
                report_html=report_html,
                agent_source=agent_source,
            )
        )
        LOGGER.info("Post-process backfilled JSON: %s", backfilled_json)
        LOGGER.info("Post-process comparison CSV: %s", comparison_csv)
        LOGGER.info("Post-process summary CSV: %s", summary_csv)
        LOGGER.info("Post-process HTML report: %s", report_html)
        if tracker is not None:
            try:
                summary = build_final_summary_from_df(
                    summary_df,
                    artifacts={
                        "backfilled_json": backfilled_json,
                        "comparison_csv": comparison_csv,
                        "summary_csv": summary_csv,
                        "report_html": report_html,
                    },
                )
                tracker.set_final_summary(summary)
            except Exception:
                LOGGER.exception("Failed to forward final summary to tracker.")
            # B 路径：把后处理拉取的 work 对话 + per-phase tool_calls 注入 snapshot，
            # 供 dashboard 对话视图覆盖运行期文本版本（含 latency，更完整），并填上
            # tools 列。从已写盘的 backfilled JSON 读取（含完整 work_analytics.trace_chain
            # 与 trace_backfill 兜底后的 tool_calls）。
            try:
                if backfilled_json is not None and backfilled_json.exists():
                    backfilled_data = json.loads(
                        backfilled_json.read_text(encoding="utf-8")
                    )
                    bundle = build_dialogue_bundle_from_report(backfilled_data)
                    if bundle:
                        tracker.set_dialogue(bundle)
                    tool_calls_bundle = build_phase_tool_calls_from_report(
                        backfilled_data
                    )
                    if tool_calls_bundle:
                        tracker.set_phase_tool_calls(tool_calls_bundle)
            except Exception:
                LOGGER.exception("Failed to forward dialogue to tracker.")
    except Exception:
        LOGGER.exception("Post-process pipeline failed.")
        LOGGER.error("Benchmark report was still saved successfully at: %s", report_path)


def main() -> None:
    """Parse CLI arguments and run the post-process pipeline on the input report JSON."""
    parser = argparse.ArgumentParser(
        description="Post-process a benchmark report JSON (trace_backfill + metrics CSVs + HTML)."
    )
    parser.add_argument(
        "input_json",
        help="Path to a benchmark report JSON or backfilled Langfuse JSON.",
    )
    parser.add_argument("--output-dir", help="Optional output directory for generated files.")
    parser.add_argument("--output-prefix", help="Optional filename prefix for generated files.")
    parser.add_argument(
        "--backfilled-json",
        help="Optional override for trace-backfill JSON output path.",
    )
    parser.add_argument(
        "--enriched-json",
        help="Deprecated alias for --backfilled-json.",
    )
    parser.add_argument("--comparison-csv", help="Optional override for comparison metrics CSV output path.")
    parser.add_argument("--summary-csv", help="Optional override for summary metrics CSV output path.")
    parser.add_argument("--report-html", help="Optional override for HTML report output path.")
    parser.add_argument(
        "--agent-source",
        choices=["openclaw", "openclaw_with_evolve", "hermes", "genericagent", "genericagent_active_evolve"],
        default="openclaw",
        help="Agent source for trace stitching (default: openclaw).",
    )
    args = parser.parse_args()

    input_path = Path(args.input_json).resolve()
    default_output_dir, default_prefix = default_results_dir(input_path)
    output_dir = Path(args.output_dir).resolve() if args.output_dir else default_output_dir
    output_prefix = args.output_prefix or default_prefix
    backfilled_json, comparison_csv, summary_csv, report_html = default_output_paths(
        output_dir, output_prefix
    )
    if args.backfilled_json:
        backfilled_json = Path(args.backfilled_json).resolve()
    elif args.enriched_json:
        backfilled_json = Path(args.enriched_json).resolve()
    if args.comparison_csv:
        comparison_csv = Path(args.comparison_csv).resolve()
    if args.summary_csv:
        summary_csv = Path(args.summary_csv).resolve()
    if args.report_html:
        report_html = Path(args.report_html).resolve()

    backfilled_json, comparison_csv, summary_csv, report_html, _summary_df = (
        process_report_to_outputs(
            input_path,
            backfilled_json=backfilled_json,
            comparison_csv=comparison_csv,
            summary_csv=summary_csv,
            report_html=report_html,
            agent_source=args.agent_source,
        )
    )

    print(f"Input: {input_path}")
    print(f"Backfilled JSON: {backfilled_json}")
    print(f"Comparison CSV: {comparison_csv}")
    print(f"Summary CSV: {summary_csv}")
    print(f"HTML Report: {report_html}")


if __name__ == "__main__":
    main()
