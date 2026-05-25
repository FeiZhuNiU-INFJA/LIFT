from html import escape

import pandas as pd

from postprocess.metrics import METRIC_COLUMNS


def format_number(value) -> str:
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


def task_label(row: pd.Series) -> str:
    icons = []
    if bool(row.get("is_final_task")):
        icons.append("🎓")
    icons.append("✅" if bool(row.get("success")) else "❌")
    return f"{' '.join(icons)} {row['task_name']}".strip()


def summary_table_html(summary_row: pd.Series) -> str:
    lines = [
        "<table class='summary-table'>",
        "<thead><tr><th>Metric</th><th>Mean Improvement</th><th>Variance</th></tr></thead>",
        "<tbody>",
    ]
    for metric in METRIC_COLUMNS:
        lines.append(
            "<tr>"
            f"<td>{escape(metric)}</td>"
            f"<td>{format_number(summary_row[f'mean_impr_{metric}'])}</td>"
            f"<td>{format_number(summary_row[f'var_impr_{metric}'])}</td>"
            "</tr>"
        )
    lines.append("</tbody></table>")
    return "\n".join(lines)


def success_badges_html(summary_row: pd.Series) -> str:
    return (
        "<div class='success-row'>"
        f"<span class='chip'>Baseline Success Rate: {format_number(summary_row['baseline_success_rate'])}</span>"
        f"<span class='chip'>Evolved Success Rate: {format_number(summary_row['evolved_success_rate'])}</span>"
        f"<span class='chip'>Task Count: {format_number(summary_row['task_count'])}</span>"
        "</div>"
    )


def task_table_html(category_df: pd.DataFrame) -> str:
    headers = [
        "Run",
        "Benchmark",
        "Task",
        "Trials",
        "Impr Trials",
        "Tool Use Num",
        "Impr Tool Use Num",
        "Content Score",
        "Impr Content Score",
        "Cached Token",
        "Impr Cached Token",
        "Total Tokens",
        "Impr Total Tokens",
        "Latency",
        "Impr Latency",
        "Trajectory Score",
        "Impr Trajectory Score",
    ]

    lines = [
        "<table class='task-table'>",
        "<thead><tr>" + "".join(f"<th>{escape(header)}</th>" for header in headers) + "</tr></thead>",
        "<tbody>",
    ]

    for _, row in category_df.iterrows():
        lines.append(
            "<tr>"
            f"<td>{format_number(row['run'])}</td>"
            f"<td>{escape(str(row.get('benchmark_name', '')))}</td>"
            f"<td>{escape(task_label(row))}</td>"
            f"<td>{format_number(row['trials'])}</td>"
            f"<td>{format_number(row['impr_trials'])}</td>"
            f"<td>{format_number(row['tool_use_num'])}</td>"
            f"<td>{format_number(row['impr_tool_use_num'])}</td>"
            f"<td>{format_number(row['content_score'])}</td>"
            f"<td>{format_number(row['impr_content_score'])}</td>"
            f"<td>{format_number(row['cached_token'])}</td>"
            f"<td>{format_number(row['impr_cached_token'])}</td>"
            f"<td>{format_number(row['total_tokens'])}</td>"
            f"<td>{format_number(row['impr_total_tokens'])}</td>"
            f"<td>{format_number(row['total_latency_seconds'])}</td>"
            f"<td>{format_number(row['impr_total_latency_seconds'])}</td>"
            f"<td>{format_number(row['trajectory_score'])}</td>"
            f"<td>{format_number(row['impr_trajectory_score'])}</td>"
            "</tr>"
        )

    lines.append("</tbody></table>")
    return "\n".join(lines)


def render_report_html(comparison_df: pd.DataFrame, summary_df: pd.DataFrame, title: str) -> str:
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
        summary_table_html(global_row),
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
                    task_table_html(run_df),
                ]
            )
        parts.extend(
            [
                "<section class='section'>",
                f"<h2>Category: {escape(str(category))}</h2>",
                success_badges_html(summary_row),
                summary_table_html(summary_row),
                "<h3>Run Blocks</h3>",
                "\n".join(run_blocks),
                "</section>",
            ]
        )

    parts.extend(["</body>", "</html>"])
    return "\n".join(parts)
