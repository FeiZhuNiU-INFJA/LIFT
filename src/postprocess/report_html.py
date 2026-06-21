"""Render post-process comparison metrics as an HTML report.

duiyuBuilds summary tables, success-rate badges, and per-task metric tables from
comparison and summary DataFrames produced by ``metrics.py``. Output is a
self-contained HTML document with collapsible run blocks, top-level legend,
and direction-aware coloring (green = better, red = worse).
"""

from html import escape
from typing import Callable, Literal

import pandas as pd

from src.postprocess.metrics import METRIC_COLUMNS

# Agent backend that produced the traces; controls which metrics appear in HTML.
AgentSource = Literal["openclaw", "openclaw_with_evolve", "hermes"]

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

# Direction map: True = lower is better (cost-style), False = higher is better.
_METRIC_LOWER_IS_BETTER: dict[str, bool] = {
    "trials": True,
    "tool_use_num": True,
    "total_tokens": True,
    "total_latency_seconds": True,
    "cached_token": True,
    "cached_token_ratio": False,
    "content_score": False,
    "trajectory_score": False,
}


def _metric_label(metric: str) -> str:
    """Return the display label for *metric*, falling back to the raw name."""
    return _METRIC_DISPLAY_LABELS.get(metric, metric)


def _value_color_class(metric: str, value) -> str:
    """Return CSS class for a signed delta/impr value of *metric* (good/bad/zero/nan)."""
    if pd.isna(value):
        return "val-nan"
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "val-nan"
    if numeric == 0:
        return "val-zero"
    lower_is_better = _METRIC_LOWER_IS_BETTER.get(metric, True)
    is_good = (numeric < 0) if lower_is_better else (numeric > 0)
    return "val-good" if is_good else "val-bad"


def _colored_td(
    value,
    metric: str,
    formatter: Callable[[object], str],
    inner: str | None = None,
) -> str:
    """Wrap *inner* (or formatted value) in a ``<td>`` colored by metric direction."""
    css = _value_color_class(metric, value)
    body = inner if inner is not None else formatter(value)
    return f"<td class='{css}'>{body}</td>"


def summary_table_html(summary_row: pd.Series, agent_source: AgentSource) -> str:
    """Render an HTML table of mean improvement and mean diff per metric for one summary row."""
    lines = [
        "<table class='summary-table'>",
        "<thead><tr><th>Metric</th><th>Mean Improvement</th>"
        "<th>Mean Diff (evolved - baseline)</th></tr></thead>",
        "<tbody>",
    ]
    for metric in _html_summary_metrics(agent_source):
        impr = summary_row[f"mean_impr_{metric}"]
        diff = summary_row[f"mean_diff_{metric}"]
        lines.append(
            "<tr>"
            f"<td class='metric-name'>{escape(_metric_label(metric))}</td>"
            f"{_colored_td(impr, metric, format_percent)}"
            f"{_colored_td(diff, metric, format_number)}"
            "</tr>"
        )
    lines.append("</tbody></table>")
    return "\n".join(lines)


def success_badges_html(summary_row: pd.Series) -> str:
    """Render success-rate and task-count chips; include outlier exclusion when present."""
    chips = [
        "<span class='chip chip-success'>"
        f"Baseline Success Rate: {format_number(summary_row['baseline_success_rate'])}</span>",
        "<span class='chip chip-success'>"
        f"Evolved Success Rate: {format_number(summary_row['evolved_success_rate'])}</span>",
        "<span class='chip chip-info'>"
        f"Task Count: {format_number(summary_row['task_count'])}</span>",
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
            "<span class='chip chip-warn'>"
            f"Aggregated: {aggregated_int} (Excluded outliers: {excluded_int})"
            "</span>"
        )
    return "<div class='success-row'>" + "".join(chips) + "</div>"


def task_table_html(category_df: pd.DataFrame, agent_source: AgentSource) -> str:
    """Render per-task evolved vs baseline metrics and improvement columns as an HTML table."""
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
        headers.append(f"{label} [evolved (baseline)]")
        headers.append(f"Impr {label}")

    lines = [
        "<div class='table-scroll'>",
        "<table class='task-table'>",
        "<thead><tr>" + "".join(f"<th>{escape(header)}</th>" for header in headers) + "</tr></thead>",
        "<tbody>",
    ]

    for _, row in category_df.iterrows():
        cells = [
            f"<td class='cell-run'>{format_number(row['run'])}</td>",
            f"<td class='cell-benchmark'>{escape(str(row.get('suite_name', '')))}</td>",
            f"<td class='cell-task'>{escape(task_label(row))}</td>",
        ]
        for metric in metric_columns:
            evolved_val = format_number(row[metric])
            baseline_val = format_number(row[f"baseline_{metric}"])
            diff_val = row.get(f"diff_{metric}")
            impr_val = row[f"impr_{metric}"]
            paired = (
                f"<span class='evolved'>{evolved_val}</span>"
                f" <span class='baseline'>({baseline_val})</span>"
            )
            cells.append(_colored_td(diff_val, metric, format_number, inner=paired))
            cells.append(_colored_td(impr_val, metric, format_percent))
        lines.append("<tr>" + "".join(cells) + "</tr>")

    lines.append("</tbody></table>")
    lines.append("</div>")
    return "\n".join(lines)


_LEGEND_HTML = """
<section class='section legend'>
  <h2>Legend &amp; Formulas</h2>
  <div class='legend-grid'>
    <div class='legend-card'>
      <h3>Task Icons</h3>
      <ul>
        <li><span class='legend-icon'>🎓</span> Test-set task (final task in suite)</li>
        <li><span class='legend-icon'>✅</span> All task requirements satisfied</li>
        <li><span class='legend-icon'>❌</span> Some task requirements not satisfied</li>
      </ul>
    </div>
    <div class='legend-card'>
      <h3>Cell Colors</h3>
      <ul>
        <li><span class='swatch val-good'>&nbsp;</span> Better than baseline (green)</li>
        <li><span class='swatch val-bad'>&nbsp;</span> Worse than baseline (red)</li>
        <li><span class='swatch val-zero'>&nbsp;</span> Equal to baseline (black)</li>
        <li><span class='swatch val-nan'>&nbsp;</span> Undefined / NaN (gray)</li>
      </ul>
      <p class='muted'>Direction: cost-style metrics (Trials, Tool Use Num, Total Tokens, Latency, Cached Token) are
      <em>lower is better</em>; score-style metrics (Outcome Score, Trajectory Score, Cached Token Ratio) are
      <em>higher is better</em>.</p>
    </div>
    <div class='legend-card'>
      <h3>Header Notation</h3>
      <ul>
        <li><code>metric [evolved (baseline)]</code>: cell shows the evolved value with the baseline value in parentheses.</li>
        <li><code>Impr metric</code>: relative improvement of evolved over baseline.</li>
      </ul>
    </div>
    <div class='legend-card formulas'>
      <h3>Formulas</h3>
      <ul>
        <li>$\\displaystyle \\mathrm{impr}_{\\text{metric}} = \\frac{\\mathrm{evolved} - \\mathrm{baseline}}{\\mathrm{baseline}}$</li>
        <li>$\\displaystyle \\mathrm{diff}_{\\text{metric}} = \\mathrm{evolved} - \\mathrm{baseline}$</li>
        <li>$\\displaystyle \\overline{\\mathrm{impr}}_{\\text{metric}} = \\frac{1}{|S|}\\sum_{i \\in S} \\mathrm{impr}_{\\text{metric}}^{(i)},\\quad
            \\overline{\\mathrm{diff}}_{\\text{metric}} = \\frac{1}{|S|}\\sum_{i \\in S} \\mathrm{diff}_{\\text{metric}}^{(i)}$,
            where $S$ is the set of non-outlier samples in a category / global scope (see Outlier Rule).</li>
        <li>$\\displaystyle \\mathrm{success\\_rate} = \\frac{1}{N}\\sum_{i=1}^{N} \\mathbb{1}[\\mathrm{success}^{(i)}]$, computed separately on baseline and evolved runs.</li>
      </ul>
    </div>
    <div class='legend-card formulas'>
      <h3>Outlier Rule</h3>
      <p class='muted' style='margin:6px 0 0;'>When aggregating $\\overline{\\mathrm{impr}}$ /
      $\\overline{\\mathrm{diff}}$, sample $i$ is dropped iff
      $\\mathrm{impr}_{\\text{trials}}^{(i)} \\ge 2.0$ or
      $\\mathrm{impr}_{\\text{tool\\_use\\_num}}^{(i)} \\ge 2.0$
      (i.e. the evolved run consumed at least 3&times; the baseline trials or tool calls,
      treated as a degenerate regression). Excluded counts are surfaced via the
      <span class='chip chip-warn' style='padding:2px 8px;font-size:12px;'>Aggregated / Excluded outliers</span>
      chip on each section.<br>
      Outlier samples are still shown in the per-task Run Blocks below; only the
      summary aggregation drops them.</p>
    </div>
  </div>
</section>
"""

_KATEX_HEAD = """
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css" crossorigin="anonymous">
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js" crossorigin="anonymous"></script>
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/contrib/auto-render.min.js" crossorigin="anonymous"
  onload="renderMathInElement(document.body, {delimiters:[
    {left:'$$', right:'$$', display:true},
    {left:'$',  right:'$',  display:false},
    {left:'\\\\(', right:'\\\\)', display:false},
    {left:'\\\\[', right:'\\\\]', display:true}
  ], throwOnError:false});"></script>
"""

_TOGGLE_SCRIPT = """
<script>
function toggleAllRuns(btn, openState) {
  var section = btn.closest('section');
  if (!section) return;
  var blocks = section.querySelectorAll('details.run-block');
  blocks.forEach(function (d) { d.open = openState; });
}
</script>
"""

_STYLE = """
:root {
  --good: #16a34a;
  --bad: #dc2626;
  --zero: #111827;
  --nan: #9ca3af;
  --bg: #f4f6fb;
  --card: #ffffff;
  --border: #e5e7eb;
  --muted: #6b7280;
  --accent: #2546b8;
  --accent-soft: #eef3ff;
}
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue",
    Arial, "Noto Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif;
  margin: 0;
  padding: 28px 24px 48px;
  background: var(--bg);
  color: #1f2937;
  line-height: 1.5;
}
.container { max-width: 1400px; margin: 0 auto; }
h1 { font-size: 28px; margin: 0 0 8px; }
h2 { font-size: 20px; margin: 0 0 14px; }
h3 { font-size: 16px; margin: 0 0 10px; }
.muted { color: var(--muted); font-size: 13px; }
.section {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 24px;
  margin-bottom: 20px;
  box-shadow: 0 4px 18px rgba(15, 23, 42, 0.04);
}
.legend-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 16px;
}
.legend-card {
  background: #fafbff;
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 14px 16px;
}
.legend-card ul { margin: 6px 0 0; padding-left: 20px; }
.legend-card li { margin-bottom: 6px; font-size: 14px; }
.legend-icon { display: inline-block; min-width: 22px; }
.swatch {
  display: inline-block;
  width: 14px;
  height: 14px;
  border-radius: 3px;
  margin-right: 6px;
  vertical-align: middle;
  border: 1px solid rgba(0,0,0,0.06);
}
.swatch.val-good { background: var(--good); }
.swatch.val-bad { background: var(--bad); }
.swatch.val-zero { background: var(--zero); }
.swatch.val-nan { background: var(--nan); }
.formulas code { background: #f3f4f6; padding: 1px 6px; border-radius: 4px; }

.success-row { margin: 4px 0 14px; }
.chip {
  display: inline-block;
  padding: 6px 12px;
  border-radius: 999px;
  margin-right: 8px;
  margin-bottom: 8px;
  font-size: 13px;
  font-weight: 500;
  border: 1px solid transparent;
}
.chip-info { background: var(--accent-soft); color: var(--accent); border-color: #d6e0ff; }
.chip-success { background: #ecfdf5; color: #047857; border-color: #c6f0db; }
.chip-warn { background: #fff7ed; color: #b45309; border-color: #fde6c8; }

.table-scroll { overflow-x: auto; border-radius: 10px; border: 1px solid var(--border); }
.summary-table, .task-table {
  width: 100%;
  border-collapse: separate;
  border-spacing: 0;
  font-size: 13px;
}
.summary-table { border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }
.summary-table th, .summary-table td,
.task-table th, .task-table td {
  padding: 10px 12px;
  text-align: left;
  vertical-align: top;
  border-bottom: 1px solid var(--border);
}
.summary-table th, .task-table th {
  background: #f3f4f6;
  font-weight: 600;
  position: sticky;
  top: 0;
  z-index: 1;
}
.task-table tbody tr:nth-child(even) { background: #fafbff; }
.task-table tbody tr:hover { background: #f0f4ff; }
.task-table .cell-run { white-space: nowrap; font-weight: 600; color: var(--accent); }
.task-table .cell-benchmark { white-space: nowrap; color: #374151; }
.task-table .cell-task { font-weight: 500; }
.task-table .baseline { color: var(--muted); font-size: 12px; }
.metric-name { font-weight: 600; }

.val-good { color: var(--good); font-weight: 600; }
.val-bad { color: var(--bad); font-weight: 600; }
.val-zero { color: var(--zero); }
.val-nan { color: var(--nan); }

details.run-block {
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 0;
  margin: 12px 0;
  background: #fbfcff;
  overflow: hidden;
}
details.run-block > summary {
  list-style: none;
  cursor: pointer;
  padding: 12px 16px;
  font-weight: 600;
  background: #f3f4f6;
  border-bottom: 1px solid transparent;
  display: flex;
  align-items: center;
  gap: 10px;
}
details.run-block > summary::-webkit-details-marker { display: none; }
details.run-block > summary::before {
  content: "▶";
  font-size: 12px;
  color: var(--muted);
  transition: transform 0.15s ease;
}
details.run-block[open] > summary::before { transform: rotate(90deg); }
details.run-block[open] > summary { border-bottom-color: var(--border); }
details.run-block > summary:hover { background: #e8eefc; }
details.run-block .table-scroll { border: none; border-radius: 0; }
details.run-block .task-table th { background: #eef1f7; }

.run-toolbar {
  display: flex;
  gap: 8px;
  margin: 12px 0 4px;
}
.run-toolbar button {
  background: var(--accent-soft);
  color: var(--accent);
  border: 1px solid #d6e0ff;
  padding: 6px 12px;
  border-radius: 8px;
  font-size: 13px;
  cursor: pointer;
  transition: background 0.15s ease;
}
.run-toolbar button:hover { background: #dde6ff; }
"""


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
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        f"<title>{escape(title)}</title>",
        _KATEX_HEAD,
        "<style>",
        _STYLE,
        "</style>",
        "</head>",
        "<body>",
        "<div class='container'>",
        f"<h1>{escape(title)}</h1>",
        "<div class='muted'>每个 category 单独展示汇总指标与任务对比明细。</div>",
        _LEGEND_HTML,
        "<section class='section'>",
        "<h2>Global Summary</h2>",
        success_badges_html(global_row),
        summary_table_html(global_row, agent_source),
        "</section>",
    ]

    for _, summary_row in category_rows.iterrows():
        category = summary_row["category"]
        category_df = comparison_df[comparison_df["category"] == category].copy()
        run_groups = list(category_df.groupby("run", dropna=False))

        run_blocks: list[str] = []
        for _, (run_index, run_df) in enumerate(run_groups):
            task_count = len(run_df)
            run_blocks.append(
                "<details class='run-block'>"
                f"<summary>Run {escape(str(run_index))} "
                f"<span class='muted'>({task_count} tasks)</span></summary>"
                f"{task_table_html(run_df, agent_source)}"
                "</details>"
            )

        parts.extend(
            [
                "<section class='section'>",
                f"<h2>Category: {escape(str(category))}</h2>",
                success_badges_html(summary_row),
                summary_table_html(summary_row, agent_source),
                "<h3>Run Blocks</h3>",
                "<div class='run-toolbar'>"
                "<button type='button' onclick='toggleAllRuns(this, true)'>Expand All</button>"
                "<button type='button' onclick='toggleAllRuns(this, false)'>Collapse All</button>"
                "</div>",
                "\n".join(run_blocks),
                "</section>",
            ]
        )

    parts.extend(
        [
            "</div>",  # container
            _TOGGLE_SCRIPT,
            "</body>",
            "</html>",
        ]
    )
    return "\n".join(parts)
