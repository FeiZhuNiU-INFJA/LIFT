import argparse
import json
from pathlib import Path
import sys
from typing import Literal

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src_new.postprocess.extract import build_extracted_dataframe, load_json
from src_new.postprocess.trace_backfill import backfill_report, get_langfuse_client
from src_new.postprocess.judge import attach_trajectory_scores
from src_new.postprocess.metrics import build_comparison_dataframe, build_summary_dataframe, print_summary_to_console, validate_pairs
from src_new.postprocess.report_html import render_report_html
from src_new.config import LOGGER
from src_new.models import EvalReport


AgentSource = Literal["openclaw", "hermes"]


def default_output_paths(output_dir: Path, output_prefix: str) -> tuple[Path, Path, Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    backfilled_json = output_dir / f"{output_prefix}_backfilled.json"
    comparison_csv = output_dir / f"{output_prefix}_comparison_metrics.csv"
    summary_csv = output_dir / f"{output_prefix}_summary_metrics.csv"
    report_html = output_dir / f"{output_prefix}_metrics_report.html"
    return backfilled_json, comparison_csv, summary_csv, report_html


def default_results_dir(input_path: Path) -> tuple[Path, str]:
    data = load_json(input_path)
    run_id = data.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        run_id = input_path.stem
    output_dir = Path.cwd() / "results" / run_id
    return output_dir, run_id


def is_backfilled_report(data: dict) -> bool:
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
is_enriched_report = is_backfilled_report


def load_or_backfill_report(
    input_path: Path,
    agent_source: AgentSource = "openclaw",
) -> tuple[dict, str]:
    data = load_json(input_path)
    if is_backfilled_report(data):
        return data, data.get("run_id") or input_path.stem

    report = EvalReport.from_json_file(input_path)
    client = get_langfuse_client()
    backfilled = backfill_report(report, client, agent_source)
    backfilled_data = json.loads(backfilled.model_dump_json())
    return backfilled_data, report.run_id


# Legacy alias
load_or_enrich_report = load_or_backfill_report


def process_report_to_outputs(
    input_path: Path,
    *,
    backfilled_json: Path | None = None,
    comparison_csv: Path,
    summary_csv: Path,
    report_html: Path,
    agent_source: AgentSource = "openclaw",
) -> tuple[Path | None, Path, Path, Path]:
    data, title_stem = load_or_backfill_report(input_path, agent_source)
    extracted_df = build_extracted_dataframe(data, agent_source)
    scored_df = attach_trajectory_scores(extracted_df)
    validate_pairs(scored_df)
    comparison_df = build_comparison_dataframe(scored_df)
    summary_df = build_summary_dataframe(comparison_df, scored_df)

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
    )
    report_html.write_text(html_text, encoding="utf-8")
    print_summary_to_console(summary_df)
    return backfilled_json, comparison_csv, summary_csv, report_html


def post_process_results_dir(run_id: str) -> Path:
    results_dir = Path.cwd() / "results" / run_id
    results_dir.mkdir(parents=True, exist_ok=True)
    return results_dir


def run_post_process_pipeline(
    run_id: str,
    report_path: Path,
    agent_source: AgentSource = "openclaw",
) -> None:
    """对已生成的 benchmark report JSON 执行后处理（trace_backfill + 指标 + HTML）。"""
    results_dir = post_process_results_dir(run_id)
    backfilled_json, comparison_csv, summary_csv, report_html = default_output_paths(
        results_dir, run_id
    )
    try:
        backfilled_json, comparison_csv, summary_csv, report_html = process_report_to_outputs(
            report_path,
            backfilled_json=backfilled_json,
            comparison_csv=comparison_csv,
            summary_csv=summary_csv,
            report_html=report_html,
            agent_source=agent_source,
        )
        LOGGER.info("Post-process backfilled JSON: %s", backfilled_json)
        LOGGER.info("Post-process comparison CSV: %s", comparison_csv)
        LOGGER.info("Post-process summary CSV: %s", summary_csv)
        LOGGER.info("Post-process HTML report: %s", report_html)
    except Exception:
        LOGGER.exception("Post-process pipeline failed.")
        LOGGER.error("Benchmark report was still saved successfully at: %s", report_path)


def main() -> None:
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
        choices=["openclaw", "hermes"],
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

    backfilled_json, comparison_csv, summary_csv, report_html = process_report_to_outputs(
        input_path,
        backfilled_json=backfilled_json,
        comparison_csv=comparison_csv,
        summary_csv=summary_csv,
        report_html=report_html,
        agent_source=args.agent_source,
    )

    print(f"Input: {input_path}")
    print(f"Backfilled JSON: {backfilled_json}")
    print(f"Comparison CSV: {comparison_csv}")
    print(f"Summary CSV: {summary_csv}")
    print(f"HTML Report: {report_html}")


if __name__ == "__main__":
    main()
