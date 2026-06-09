"""Render post-process comparison metrics as an HTML report.

Builds summary tables, success-rate badges, and per-task metric tables from
comparison and summary DataFrames produced by ``metrics.py``.
"""

from html import escape
from typing import Literal

import pandas as pd

from src_new.postprocess.metrics import METRIC_COLUMNS

# Agent backend that produced the traces; controls which metrics appear in HTML.
AgentSource = Literal["openclaw", "hermes"]

# Metrics hidden from HTML for all agent sources.
_HTML_HIDDEN_METRICS_BASE = {"cached_token"}
# Hermes 上报暂不提供缓存命中与每轮延迟，HTML 不展示这两项及其改进比例。
_HTML_HIDDEN_METRICS_HERMES = _HTML_HIDDEN_METRICS_BASE | {
    "cached_token_ratio",
    "total_latency_seconds",
}


def _hidden_metrics(agent_source: AgentSource) -> set[str]:
    """Return the set of metric column names to omit from HTML for *agent_source*."""
    if agent_source == "hermes":
        return _HTML_HIDDEN_METRICS_HERMES
    return _HTML_HIDDEN_METRICS_BASE


def _html_summary_metrics(agent_source: AgentSource) -> list[str]:
    """Return ``METRIC_COLUMNS`` entries visible in summary tables for *agent_source*."""
    hidden = _hidden_metrics(agent_source)
    return [m for m in METRIC_COLUMNS if m not in hidden]


def format_number(value) -> str:
    """Format a numeric or scalar cell value for HTML display (comma-separated, escaped)."""
    if pd.isna(value):
        return "NaN"
    if isinstance(value, bool):
        return "True" if value else "False"
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return escape(str(value))

    if abs(numeric) >= 1000 or numeric.is_integer():
        return f"{numeric:,.0f}"
    return f"{numeric:,.6f}".rstrip("0").rstrip(".")


def format_percent(value) -> str:
    """以 ``xx.xx%`` 形式展示改进比例 (输入为 0.1234 这类小数比值)。"""
    if pd.isna(value):
        return "NaN"
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return escape(str(value))
    return f"{numeric * 100:.2f}%"


def task_label(row: pd.Series) -> str:
    """Build a display label with success/final-task icons and the task name."""
    icons = []
    if bool(row.get("is_final_task")):
        icons.append("🎓")
    icons.append("✅" if bool(row.get("success")) else "❌")
    return f"{' '.join(icons)} {row['task_name']}".strip()


# Human-readable column headers keyed by internal metric name.
_METRIC_DISPLAY_LABELS: dict[str, str] = {
    "trials": "Trials",
    "tool_use_num": "Tool Use Num",
    "content_score": "Outcome Score",
    "cached_token": "Cached Token",
    "cached_token_ratio": "Cached Token Ratio",
    "total_tokens": "Total Tokens",
    "total_latency_seconds": "Latency",
    "trajectory_score": "Trajectory Score",
}


def _metric_label(metric: str) -> str:
    """Return the display label for *metric*, falling back to the raw name."""
    return _METRIC_DISPLAY_LABELS.get(metric, metric)


def summary_table_html(summary_row: pd.Series, agent_source: AgentSource) -> str:
    """Render an HTML table of mean improvement and mean diff per metric for one summary row."""
    lines = [
        "<table class='summary-table'>",
        "<thead><tr><th>Metric</th><th>Mean Improvement</th><th>Mean Diff (evolved - baseline)</th></tr></thead>",
        "<tbody>",
    ]
    for metric in _html_summary_metrics(agent_source):
        lines.append(
            "<tr>"
            f"<td>{escape(_metric_label(metric))}</td>"
            f"<td>{format_percent(summary_row[f'mean_impr_{metric}'])}</td>"
            f"<td>{format_number(summary_row[f'mean_diff_{metric}'])}</td>"
            "</tr>"
        )
    lines.append("</tbody></table>")
    return "\n".join(lines)


def success_badges_html(summary_row: pd.Series) -> str:
    """Render success-rate and task-count chips; include outlier exclusion when present."""
    chips = [
        f"<span class='chip'>Baseline Success Rate: {format_number(summary_row['baseline_success_rate'])}</span>",
        f"<span class='chip'>Evolved Success Rate: {format_number(summary_row['evolved_success_rate'])}</span>",
        f"<span class='chip'>Task Count: {format_number(summary_row['task_count'])}</span>",
    ]
    excluded = summary_row.get("task_count_excluded", 0)
    try:
        excluded_int = int(excluded) if pd.notna(excluded) else 0
    except (TypeError, ValueError):
        excluded_int = 0
    if excluded_int > 0:
        aggregated = summary_row.get("task_count_aggregated", 0)
        try:
            aggregated_int = int(aggregated) if pd.notna(aggregated) else 0
        except (TypeError, ValueError):
            aggregated_int = 0
        chips.append(
            "<span class='chip' style='background:#fff1f0;color:#a8071a;'>"
            f"Aggregated: {aggregated_int} (Excluded outliers: {excluded_int})"
            "</span>"
        )
    return "<div class='success-row'>" + "".join(chips) + "</div>"


def task_table_html(category_df: pd.DataFrame, agent_source: AgentSource) -> str:
    """Render per-task evolved vs baseline metrics and improvement columns as an HTML table."""
    # 按 agent_source 决定要展示的指标列；hermes 不展示缓存命中率与延迟。
    hidden = _hidden_metrics(agent_source)
    metric_columns: list[str] = [
        "trials",
        "tool_use_num",
        "content_score",
    ]
    if "cached_token_ratio" not in hidden:
        metric_columns.append("cached_token_ratio")
    metric_columns.append("total_tokens")
    if "total_latency_seconds" not in hidden:
        metric_columns.append("total_latency_seconds")
    metric_columns.append("trajectory_score")

    headers = ["Run", "Benchmark", "Task"]
    for metric in metric_columns:
        label = _metric_label(metric)
        headers.append(f"{label} (evolved / baseline)")
        headers.append(f"Impr {label}")

    lines = [
        "<table class='task-table'>",
        "<thead><tr>" + "".join(f"<th>{escape(header)}</th>" for header in headers) + "</tr></thead>",
        "<tbody>",
    ]

    for _, row in category_df.iterrows():
        cells = [
            f"<td>{format_number(row['run'])}</td>",
            f"<td>{escape(str(row.get('suite_name', '')))}</td>",
            f"<td>{escape(task_label(row))}</td>",
        ]
        for metric in metric_columns:
            evolved_val = format_number(row[metric])
            baseline_val = format_number(row[f"baseline_{metric}"])
            cells.append(f"<td>{evolved_val} ({baseline_val})</td>")
            cells.append(f"<td>{format_percent(row[f'impr_{metric}'])}</td>")
        lines.append("<tr>" + "".join(cells) + "</tr>")

    lines.append("</tbody></table>")
    return "\n".join(lines)


def render_report_html(
    comparison_df: pd.DataFrame,
    summary_df: pd.DataFrame,
    title: str,
    agent_source: AgentSource = "openclaw",
) -> str:
    """Assemble a full HTML metrics report with global and per-category sections."""
    global_row = summary_df[summary_df["scope"] == "global"].iloc[0]
    category_rows = summary_df[summary_df["scope"] == "category"]

    parts = [
        "<!DOCTYPE html>",
        "<html lang='zh-CN'>",
        "<head>",
        "<meta charset='UTF-8'>",
        f"<title>{escape(title)}</title>",
        "<style>",
        "body{font-family:Arial,sans-serif;margin:24px;background:#f7f8fa;color:#222;}",
        "h1,h2,h3{margin:0 0 12px 0;}",
        ".section{background:#fff;border-radius:12px;padding:20px;margin-bottom:20px;box-shadow:0 2px 12px rgba(0,0,0,0.06);}",
        ".chip{display:inline-block;background:#eef3ff;color:#2546b8;padding:8px 12px;border-radius:999px;margin-right:8px;margin-bottom:8px;font-size:14px;}",
        ".summary-table,.task-table{width:100%;border-collapse:collapse;margin-top:12px;}",
        ".summary-table th,.summary-table td,.task-table th,.task-table td{border:1px solid #e5e7eb;padding:10px 12px;text-align:left;vertical-align:top;}",
        ".summary-table th,.task-table th{background:#f3f4f6;}",
        ".task-table td:first-child{white-space:nowrap;font-weight:600;}",
        ".muted{color:#666;font-size:14px;}",
        "</style>",
        "</head>",
        "<body>",
        f"<h1>{escape(title)}</h1>",
        "<div class='muted'>每个 category 单独展示汇总指标与任务对比明细。</div>",
        "<section class='section'>",
        "<h2>Global Summary</h2>",
        success_badges_html(global_row),
        summary_table_html(global_row, agent_source),
        "</section>",
    ]

    for _, summary_row in category_rows.iterrows():
        category = summary_row["category"]
        category_df = comparison_df[comparison_df["category"] == category].copy()
        run_blocks: list[str] = []
        for run_index, run_df in category_df.groupby("run", dropna=False):
            run_blocks.extend(
                [
                    f"<h3>Run {escape(str(run_index))}</h3>",
                    task_table_html(run_df, agent_source),
                ]
            )
        parts.extend(
            [
                "<section class='section'>",
                f"<h2>Category: {escape(str(category))}</h2>",
                success_badges_html(summary_row),
                summary_table_html(summary_row, agent_source),
                "<h3>Run Blocks</h3>",
                "\n".join(run_blocks),
                "</section>",
            ]
        )

    parts.extend(["</body>", "</html>"])
    return "\n".join(parts)
