"""Render post-process comparison metrics as an HTML report.

Builds summary tables, success-rate badges, and per-task metric tables from
comparison and summary DataFrames produced by ``metrics.py``. Output is a
self-contained HTML document with collapsible run blocks, top-level legend,
direction-aware coloring (green = better, red = worse), and per-task
trajectory maps (snake-layout SVG with click-to-inspect nodes).
"""

import json
from html import escape
from typing import Any, Callable

import pandas as pd

from src.postprocess.extract import AgentSource, _should_ignore_tool_call_block
from src.postprocess.metrics import METRIC_COLUMNS, _outlier_mask

# Metrics hidden from HTML for all agent sources.
# Token 侧默认只显示"新增输入" / "cache read" / "输出" / "reasoning" / 派生的
# ``total_tokens`` / ``cache_hit_ratio``；``cache_write_tokens`` 对 OpenAI 家恒 0，
# HTML 里冗余，隐藏以减少列宽压力（CSV 仍保留全部 5 字段供离线分析）。
_HTML_HIDDEN_METRICS_BASE = {"cache_write_tokens"}
# Hermes 插件层没有 per-turn latency 数据（Hermes upstream 不上报），
# HTML 显示时隐藏该列避免误导。cache 侧字段已由插件 ``_fallback_extract_from_raw_usage``
# 从 ``prompt_tokens_details.cached_tokens`` 兜底提取，正常可见。
_HTML_HIDDEN_METRICS_HERMES = _HTML_HIDDEN_METRICS_BASE | {
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
    "input_tokens": "Input (fresh)",
    "cache_write_tokens": "Cache Write",
    "cache_read_tokens": "Cache Read",
    "output_tokens": "Output",
    "reasoning_tokens": "Reasoning",
    "total_tokens": "Total Tokens",
    "cache_hit_ratio": "Cache Hit Ratio",
    "total_latency_seconds": "Latency",
    "trajectory_score": "Trajectory Score",
}

# Direction map: True = lower is better (cost-style), False = higher is better.
_METRIC_LOWER_IS_BETTER: dict[str, bool] = {
    "trials": True,
    "tool_use_num": True,
    "input_tokens": True,
    "cache_write_tokens": True,
    "output_tokens": True,
    "reasoning_tokens": True,
    "total_tokens": True,
    "total_latency_seconds": True,
    # ``cache_read_tokens`` 越多说明命中越多、越省钱，与命中率同向：越大越好。
    "cache_read_tokens": False,
    "cache_hit_ratio": False,
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
        "<thead><tr><th>Metric</th><th>Improvement rate</th>"
        "<th>Mean diff (evolved - baseline)</th></tr></thead>",
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


# Anthropic-style accent (warm terracotta / "Crail orange") used for profound marks.
_PROFOUND_COLOR = "#d97757"


def build_profound_flags(
    comparison_df: pd.DataFrame, agent_source: AgentSource = "openclaw"
) -> dict[tuple, set[str]]:
    """Map each task-row key to the set of metrics where evolution is *profound*.

    A metric is *profound* for a given (run) row when its evolved value is strictly
    better than the **best baseline value of that task across all runs**. "Best" is
    direction-aware: the minimum baseline for cost-style metrics (lower is better) and
    the maximum baseline for score-style metrics (higher is better). By construction
    ``profound ⊆ good`` (beating the best baseline implies beating the same-run one).

    Only metrics *visible* for *agent_source* are considered, so a profound mark always
    has a corresponding ``Impr`` column on screen (e.g. hermes hides latency / cached
    token, which must not silently star a task without a visible starred Impr cell).

    The row key is ``(run, suite_path, task_name, suite)`` — matching
    ``build_trajectory_map`` — which uniquely identifies a comparison row.
    """
    flags: dict[tuple, set[str]] = {}
    if comparison_df.empty:
        return flags

    task_keys = ["suite_name", "suite_path", "task_name", "suite"]
    if any(k not in comparison_df.columns for k in task_keys):
        return flags

    for metric in _html_summary_metrics(agent_source):
        base_col = f"baseline_{metric}"
        if metric not in comparison_df.columns or base_col not in comparison_df.columns:
            continue
        lower_is_better = _METRIC_LOWER_IS_BETTER.get(metric, True)

        grouped = comparison_df[task_keys].copy()
        grouped["_base"] = pd.to_numeric(comparison_df[base_col], errors="coerce")
        best = grouped.groupby(task_keys, dropna=False)["_base"].transform(
            "min" if lower_is_better else "max"
        )
        evolved = pd.to_numeric(comparison_df[metric], errors="coerce")
        is_profound = (evolved < best) if lower_is_better else (evolved > best)

        for idx, profound in is_profound.items():
            if not bool(profound):
                continue
            row = comparison_df.loc[idx]
            key = (
                row.get("run"),
                row.get("suite_path"),
                row.get("task_name"),
                row.get("suite"),
            )
            flags.setdefault(key, set()).add(metric)
    return flags


def _row_profound_key(row: pd.Series) -> tuple:
    """Return the profound-flags lookup key for a comparison *row*."""
    return (
        row.get("run"),
        row.get("suite_path"),
        row.get("task_name"),
        row.get("suite"),
    )


def _profound_count(
    scope_df: pd.DataFrame, profound_flags: dict[tuple, set[str]] | None, metric: str
) -> int:
    """Count rows within *scope_df* whose *metric* is profound (per *profound_flags*)."""
    if not profound_flags:
        return 0
    count = 0
    for _, row in scope_df.iterrows():
        if metric in profound_flags.get(_row_profound_key(row), ()):
            count += 1
    return count


def _good_bad_counts(scope_df: pd.DataFrame, metric: str) -> tuple[int, int, int]:
    """Count tasks per outcome for *metric* within *scope_df*: (good, tie, bad).

    Classification reuses ``_value_color_class`` on the per-task ``diff_{metric}``:
    ``val-good`` → better, ``val-bad`` → worse, ``val-zero`` → tie (no change).
    ``val-nan`` (undefined / missing baseline) is counted as neither.
    """
    good = 0
    tie = 0
    bad = 0
    diff_col = f"diff_{metric}"
    if diff_col not in scope_df.columns:
        return good, tie, bad
    for value in scope_df[diff_col]:
        css = _value_color_class(metric, value)
        if css == "val-good":
            good += 1
        elif css == "val-bad":
            bad += 1
        elif css == "val-zero":
            tie += 1
    return good, tie, bad


def good_bad_chart_html(
    scope_df: pd.DataFrame,
    agent_source: AgentSource,
    profound_flags: dict[tuple, set[str]] | None = None,
    chart_id: str = "gb",
) -> str:
    """Render a horizontal good/tie/bad stacked-bar SVG (one bar per metric).

    Each bar shares the same total length; segments are green (better, left),
    gray (tie / no change, middle) and red (worse, right), sized proportionally
    to their task counts. Each segment's count is labeled below the bar, centered
    on that segment, in black text (so even very short segments stay readable).

    When *profound_flags* is supplied, the *profound* sub-portion of the green
    (better) segment is overlaid with a terracotta diagonal hatch (sized
    proportionally within the green segment) and the profound count is printed in
    terracotta to the left of the bar, so it does not disturb the existing labels.

    Outlier tasks (per ``metrics._outlier_mask``: evolved vs. baseline differs too much
    on trials / tool_use_num) are excluded from every chart count so the SVG matches the
    summary aggregation口径. Those tasks still render in the per-task Run Block tables.
    """
    metrics = _html_summary_metrics(agent_source)
    if not scope_df.empty:
        scope_df = scope_df.loc[~_outlier_mask(scope_df)]
    rows = [
        (metric, *_good_bad_counts(scope_df, metric), _profound_count(scope_df, profound_flags, metric))
        for metric in metrics
    ]

    label_w = 165
    bar_x = 235
    bar_w = 570
    total_w = 820
    row_h = 28
    count_h = 16  # vertical space below each bar for the count labels
    row_gap = 18
    top_pad = 18
    bottom_pad = 14
    n = len(rows)
    block_h = row_h + count_h + row_gap
    height = top_pad + n * block_h + bottom_pad

    hatch_id = f"profound-hatch-{chart_id}"
    svg: list[str] = [
        f"<svg class='gb-chart' viewBox='0 0 {total_w} {height}' "
        f"role='img' aria-label='Better / tie / worse task counts per metric' "
        "preserveAspectRatio='xMinYMin meet'>",
        "<defs>"
        f"<pattern id='{escape(hatch_id)}' width='7' height='7' patternUnits='userSpaceOnUse' "
        "patternTransform='rotate(45)'>"
        f"<line x1='0' y1='0' x2='0' y2='7' stroke='{_PROFOUND_COLOR}' stroke-width='2.4'/>"
        "</pattern>"
        "</defs>",
    ]

    for i, (metric, good, tie, bad, profound) in enumerate(rows):
        y_top = top_pad + i * block_h
        y_center = y_top + row_h / 2
        count_y = y_top + row_h + count_h - 4  # baseline for count labels below bar
        label = escape(_metric_label(metric))
        svg.append(
            f"<text x='{label_w - 10}' y='{y_center:.1f}' class='gb-label' "
            f"text-anchor='end' dominant-baseline='middle'>{label}</text>"
        )
        total = good + tie + bad
        if total == 0:
            svg.append(
                f"<rect x='{bar_x}' y='{y_top}' width='{bar_w}' height='{row_h}' "
                "rx='4' class='gb-empty'/>"
            )
            svg.append(
                f"<text x='{bar_x + bar_w / 2:.1f}' y='{y_center:.1f}' class='gb-empty-text' "
                "text-anchor='middle' dominant-baseline='middle'>no comparable data</text>"
            )
        else:
            good_w = bar_w * (good / total)
            tie_w = bar_w * (tie / total)
            bad_w = bar_w - good_w - tie_w
            segments = [
                ("gb-good", good, bar_x, good_w),
                ("gb-tie", tie, bar_x + good_w, tie_w),
                ("gb-bad", bad, bar_x + good_w + tie_w, bad_w),
            ]
            for css, count, seg_x, seg_w in segments:
                if seg_w <= 0:
                    continue
                svg.append(
                    f"<rect x='{seg_x:.2f}' y='{y_top}' width='{seg_w:.2f}' height='{row_h}' "
                    f"class='{css}'/>"
                )
                if count > 0:
                    svg.append(
                        f"<text x='{seg_x + seg_w / 2:.2f}' y='{count_y:.1f}' class='gb-count' "
                        f"text-anchor='middle'>{count}</text>"
                    )

            # Profound overlay: hatch the left sub-portion of the green segment and
            # print the profound count to the left of the whole bar.
            if profound > 0 and good > 0 and good_w > 0:
                prof_w = good_w * (min(profound, good) / good)
                svg.append(
                    f"<rect x='{bar_x:.2f}' y='{y_top}' width='{prof_w:.2f}' height='{row_h}' "
                    f"fill='url(#{escape(hatch_id)})' stroke='none'/>"
                )
                svg.append(
                    f"<text x='{bar_x - 12:.1f}' y='{y_center:.1f}' class='gb-profound-count' "
                    f"text-anchor='end' dominant-baseline='middle'>&#9733;{profound}</text>"
                )

    svg.append("</svg>")

    legend_spans = [
        "<span><span class='swatch gb-sw-good'>&nbsp;</span> Better (green)</span>",
        "<span><span class='swatch gb-sw-tie'>&nbsp;</span> Tie / no change (gray)</span>",
        "<span><span class='swatch gb-sw-bad'>&nbsp;</span> Worse (red)</span>",
    ]
    if profound_flags:
        legend_spans.append(
            "<span><span class='swatch gb-sw-profound'>&nbsp;</span> "
            "&#9733; Profound (better than best baseline)</span>"
        )

    return (
        "<div class='gb-wrap'>"
        "<div class='gb-legend'>" + "".join(legend_spans) + "</div>"
        + "\n".join(svg)
        + "</div>"
    )


# ---------------------------------------------------------------------------
# Trajectory visualization
# ---------------------------------------------------------------------------

# Per-node-kind palette (draw.io-like). Keyed by node ``kind``.
_TRAJ_NODE_STYLE: dict[str, tuple[str, str]] = {
    # kind: (fill, stroke)
    "user": ("#eef3ff", "#2546b8"),
    "assistant": ("#ecfdf5", "#047857"),
    "tool": ("#fff7ed", "#b45309"),
}


def _parse_messages(raw: Any) -> list[dict[str, Any]]:
    """Parse ``all_messages`` (JSON string or list) into a list of message dicts."""
    if isinstance(raw, list):
        return [m for m in raw if isinstance(m, dict)]
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [m for m in parsed if isinstance(m, dict)] if isinstance(parsed, list) else []


def _block_text(content: Any) -> tuple[str, str, list[dict[str, Any]], bool]:
    """Split message ``content`` into (text, reasoning, tool_call_blocks, had_signal_only_tool_calls).

    Supports OpenClaw block-style content (list of ``text`` / ``thinking`` /
    ``toolCall`` dicts) as well as plain string content.

    ``had_signal_only_tool_calls``: True 表示 message 里存在 toolCall block，且
    **全部** 被 ``_should_ignore_tool_call_block`` 过滤（即整条 message 的工具调用
    只是 self-evolution signal）。调用方据此把 signal-only 的 assistant message
    整条 skip，避免在 trajectory 图上留下"只有 reasoning"的 orphan node。
    """
    if isinstance(content, str):
        return content, "", [], False
    if not isinstance(content, list):
        if content is None:
            return "", "", [], False
        return json.dumps(content, ensure_ascii=False), "", [], False

    texts: list[str] = []
    reasonings: list[str] = []
    tool_blocks: list[dict[str, Any]] = []
    raw_tool_count = 0
    ignored_tool_count = 0
    for block in content:
        if not isinstance(block, dict):
            texts.append(str(block))
            continue
        btype = block.get("type")
        if btype == "text":
            texts.append(block.get("text", ""))
        elif btype == "thinking":
            reasonings.append(block.get("thinking", ""))
        elif btype == "toolCall":
            raw_tool_count += 1
            # Skip OpenClaw self-evolution signal calls (exec to the local signal
            # port) so trajectory tool nodes match the ``tool_use_num`` metric,
            # which excludes them via the same predicate.
            if _should_ignore_tool_call_block(block):
                ignored_tool_count += 1
                continue
            tool_blocks.append(
                {
                    "name": block.get("name", "tool"),
                    "arguments": block.get("arguments"),
                    "reasoning": "",
                }
            )
        else:
            texts.append(json.dumps(block, ensure_ascii=False))
    had_signal_only_tool_calls = raw_tool_count > 0 and raw_tool_count == ignored_tool_count
    return (
        "\n".join(t for t in texts if t),
        "\n".join(r for r in reasonings if r),
        tool_blocks,
        had_signal_only_tool_calls,
    )


def _stringify(value: Any) -> str:
    """Render an arbitrary value as a readable string (JSON for dict/list)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return str(value)


def build_trajectory_nodes(raw_messages: Any) -> list[dict[str, Any]]:
    """Normalize ``all_messages`` into trajectory nodes for visualization.

    Rules:
    - Keep ``user`` / ``assistant`` nodes, showing reasoning + content.
    - An ``assistant`` with embedded ``tool_calls`` (OpenAI-style) or ``toolCall``
      blocks (OpenClaw-style) emits one ``tool`` node per call (name, reasoning,
      arguments), after the assistant's text node when it also has visible text.
    - Drop tool *result* messages (e.g. ``role == 'tool'`` / ``role == 'toolResult'``
      or messages carrying ``tool_call_id``).
      Each surviving tool node is therefore a distinct tool *call*; consecutive
      calls are all kept (no adjacency collapse).
    - Each ``user`` node carries an incrementing ``turn`` counter starting at 1.
    """
    messages = _parse_messages(raw_messages)
    nodes: list[dict[str, Any]] = []
    turn = 0

    for message in messages:
        role = message.get("role")
        # Tool *result* messages in Hermes/OpenAI/OpenClaw formats -> drop.
        if role in {"tool", "toolResult"} or message.get("tool_call_id") is not None:
            continue

        text, reasoning, content_tool_blocks, signal_only_toolcalls = _block_text(
            message.get("content")
        )
        if not reasoning:
            reasoning = message.get("reasoning") or ""

        if role == "user":
            turn += 1
            nodes.append(
                {
                    "kind": "user",
                    "label": "user",
                    "turn": turn,
                    "reasoning": reasoning,
                    "content": text,
                }
            )
            continue

        if role == "assistant":
            # Skip signal-only assistant messages entirely: OpenClaw self-evolution
            # 会让 agent 先发一条只含 planning-thinking + signal exec toolCall 的
            # message（没有 text）。signal toolCall 已在 ``_block_text`` 里被过滤，
            # 但如果不整条 skip，就会在 trajectory 图上留下一个"只有 reasoning"的
            # orphan assistant node（reasoning 内容还是内部 signal 决策）。
            if (
                not text
                and signal_only_toolcalls
                and not (message.get("tool_calls") or [])
            ):
                continue
            # Emit the assistant text/reasoning node when there is something to show.
            if text or reasoning:
                nodes.append(
                    {
                        "kind": "assistant",
                        "label": "assistant",
                        "reasoning": reasoning,
                        "content": text,
                    }
                )
            # OpenAI-style tool_calls.
            tool_calls = message.get("tool_calls") or []
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") or {}
                name = fn.get("name") or call.get("name") or "tool"
                arguments = fn.get("arguments", call.get("arguments"))
                nodes.append(
                    {
                        "kind": "tool",
                        "label": f"tool: {name}",
                        "tool_name": name,
                        "reasoning": reasoning,
                        "arguments": arguments,
                    }
                )
            # OpenClaw block-style tool calls.
            for block in content_tool_blocks:
                nodes.append(
                    {
                        "kind": "tool",
                        "label": f"tool: {block['name']}",
                        "tool_name": block["name"],
                        "reasoning": reasoning,
                        "arguments": block.get("arguments"),
                    }
                )
            continue

        # Any other role: keep as a generic node so nothing silently disappears.
        if text or reasoning:
            nodes.append(
                {
                    "kind": "assistant",
                    "label": str(role or "node"),
                    "reasoning": reasoning,
                    "content": text,
                }
            )

    # Tool *result* messages were already dropped above by role (e.g. ``tool`` /
    # ``toolResult`` / tool_call_id), so each surviving tool node is a distinct
    # tool *call*. We do NOT
    # collapse adjacent tool nodes here: in OpenAI/hermes format an assistant message
    # often carries empty content + one tool_call, so consecutive calls produce
    # neighboring tool nodes that are legitimately different and must all be kept.
    return nodes


def _render_trajectory_svg(nodes: list[dict[str, Any]], traj_id: str) -> str:
    """Render the snake-layout ``<svg>`` for *nodes* (clickable, keyed by *traj_id*)."""
    # Layout constants.
    cols = 4
    node_w = 150
    node_h = 56
    gap_x = 46
    gap_y = 52
    pad = 20
    sub_h = 16  # space under user nodes for the turn counter
    total_w = pad * 2 + cols * node_w + (cols - 1) * gap_x
    n_rows = (len(nodes) + cols - 1) // cols
    total_h = pad * 2 + n_rows * node_h + (n_rows - 1) * gap_y + sub_h

    def cell_xy(index: int) -> tuple[float, float]:
        """Return top-left (x, y) for the *index*-th node in snake order."""
        r = index // cols
        c = index % cols
        if r % 2 == 1:  # right-to-left on odd rows
            c = cols - 1 - c
        x = pad + c * (node_w + gap_x)
        y = pad + r * (node_h + gap_y)
        return x, y

    svg: list[str] = [
        f"<svg class='traj-svg' viewBox='0 0 {total_w} {total_h}' "
        "role='img' aria-label='Agent execution trajectory' "
        "preserveAspectRatio='xMinYMin meet'>",
        "<defs>"
        f"<marker id='traj-arrow-{escape(traj_id)}' viewBox='0 0 10 10' refX='9' refY='5' "
        "markerWidth='7' markerHeight='7' orient='auto-start-reverse'>"
        "<path d='M0,0 L10,5 L0,10 z' fill='#94a3b8'/></marker>"
        "</defs>",
    ]
    arrow = f"url(#traj-arrow-{escape(traj_id)})"

    centers: list[tuple[float, float]] = []
    for i in range(len(nodes)):
        x, y = cell_xy(i)
        centers.append((x + node_w / 2, y + node_h / 2))

    # Connectors (draw first so nodes sit on top).
    for i in range(len(nodes) - 1):
        x1, y1 = centers[i]
        x2, y2 = centers[i + 1]
        same_row = (i // cols) == ((i + 1) // cols)
        if same_row:
            if x2 >= x1:
                sx, ex = x1 + node_w / 2, x2 - node_w / 2
            else:
                sx, ex = x1 - node_w / 2, x2 + node_w / 2
            svg.append(
                f"<line x1='{sx:.1f}' y1='{y1:.1f}' x2='{ex:.1f}' y2='{y2:.1f}' "
                f"class='traj-edge' marker-end='{arrow}'/>"
            )
        else:
            svg.append(
                f"<line x1='{x1:.1f}' y1='{y1 + node_h / 2:.1f}' x2='{x2:.1f}' y2='{y2 - node_h / 2:.1f}' "
                f"class='traj-edge' marker-end='{arrow}'/>"
            )

    # Nodes.
    for i, node in enumerate(nodes):
        x, y = cell_xy(i)
        fill, stroke = _TRAJ_NODE_STYLE.get(node["kind"], ("#f3f4f6", "#6b7280"))
        label = escape(node["label"])
        svg.append(
            f"<g class='traj-node' data-traj='{escape(traj_id)}' data-idx='{i}' "
            f"tabindex='0' role='button'>"
            f"<rect x='{x:.1f}' y='{y:.1f}' width='{node_w}' height='{node_h}' rx='8' "
            f"fill='{fill}' stroke='{stroke}' stroke-width='1.5'/>"
            f"<text x='{x + node_w / 2:.1f}' y='{y + node_h / 2:.1f}' class='traj-node-label' "
            f"text-anchor='middle' dominant-baseline='middle'>{label}</text>"
            "</g>"
        )
        if node["kind"] == "user":
            svg.append(
                f"<text x='{x + node_w / 2:.1f}' y='{y + node_h + 12:.1f}' class='traj-turn' "
                f"text-anchor='middle'>turn {node['turn']}</text>"
            )

    svg.append("</svg>")
    return "\n".join(svg)


def trajectory_column_html(raw_messages: Any, traj_id: str, title: str) -> str:
    """Render one trajectory column: title, SVG flowchart, and a detail panel below it.

    Clicking a node reveals its reasoning / content / tool arguments in the detail
    panel directly beneath the flowchart. The normalized trajectory is also embedded
    as AI-readable JSON.
    """
    nodes = build_trajectory_nodes(raw_messages)
    if not nodes:
        return (
            f"<div class='traj-col-title'>{escape(title)}</div>"
            "<div class='traj-empty muted'>No trajectory messages captured.</div>"
        )

    svg = _render_trajectory_svg(nodes, traj_id)

    # AI-native: embed the normalized trajectory as machine-readable JSON.
    # NOTE: script data blocks are raw text — only neutralize ``<`` so a literal
    # ``</script>`` cannot break out; do NOT HTML-escape (JSON.parse needs raw quotes).
    ai_payload = json.dumps(
        {"trajectory_id": traj_id, "variant": title, "node_count": len(nodes), "nodes": nodes},
        ensure_ascii=False,
    ).replace("<", "\\u003c")

    return (
        f"<div class='traj-col-title'>{escape(title)} "
        f"<span class='muted'>({len(nodes)} nodes)</span></div>"
        "<div class='traj-col-toolbar'>"
        f"<button type='button' class='traj-dl-btn' data-svgname='{escape(traj_id)}' "
        "onclick='downloadTrajPng(this)' title='Download this flowchart as a PNG image (for Feishu/Lark docs)'>"
        "&#x1F5BC; Download PNG</button>"
        f"<button type='button' class='traj-dl-btn' data-svgname='{escape(traj_id)}' "
        "onclick='downloadTrajSvg(this)' title='Download this flowchart as an SVG file'>"
        "&#x2B07; Download SVG</button>"
        "</div>"
        "<div class='traj-canvas'>" + svg + "</div>"
        f"<div class='traj-detail' id='detail-{escape(traj_id)}'>"
        "<div class='traj-detail-empty muted'>Click a node to view its reasoning, content, or tool arguments.</div>"
        "</div>"
        f"<script type='application/json' class='traj-data' data-traj='{escape(traj_id)}'>{ai_payload}</script>"
    )


def trajectory_compare_html(
    evolved_messages: Any,
    baseline_messages: Any,
    base_id: str,
) -> str:
    """Render side-by-side evolved/baseline trajectory columns with toggle buttons.

    Two buttons independently toggle each variant; both can be shown at once so the
    flowcharts sit side by side for comparison, each with its detail panel below.
    """
    return (
        "<div class='traj-compare-wrap'>"
        "<div class='traj-legend'>"
        "<span><span class='swatch traj-sw-user'>&nbsp;</span> user</span>"
        "<span><span class='swatch traj-sw-assistant'>&nbsp;</span> assistant</span>"
        "<span><span class='swatch traj-sw-tool'>&nbsp;</span> tool</span>"
        "<span class='muted'>· click a node to inspect</span>"
        "</div>"
        "<div class='traj-toolbar'>"
        "<button type='button' class='traj-toggle-btn' onclick=\"toggleTraj(this,'evolved')\">"
        "Show evolved trajectory</button>"
        "<button type='button' class='traj-toggle-btn' onclick=\"toggleTraj(this,'baseline')\">"
        "Show baseline trajectory</button>"
        "</div>"
        "<div class='traj-compare'>"
        "<div class='traj-col' data-variant='evolved' hidden>"
        + trajectory_column_html(evolved_messages, f"{base_id}-evolved", "Evolved")
        + "</div>"
        "<div class='traj-col' data-variant='baseline' hidden>"
        + trajectory_column_html(baseline_messages, f"{base_id}-baseline", "Baseline")
        + "</div>"
        "</div>"
        "</div>"
    )


def build_trajectory_map(scored_df: pd.DataFrame) -> dict[tuple, dict[str, Any]]:
    """Map ``(run, suite_path, task_name, suite)`` to both variants' ``all_messages``.

    Returns ``{key: {"evolved": <all_messages>, "baseline": <all_messages>}}`` built
    from the extracted/scored DataFrame (one row per variant), so the HTML can show
    evolved and baseline trajectories side by side.
    """
    traj_map: dict[tuple, dict[str, Any]] = {}
    if "all_messages" not in scored_df.columns:
        return traj_map
    for variant in ("evolved", "baseline"):
        if variant not in scored_df.columns:
            continue
        subset = scored_df[scored_df.get(variant) == True]
        for _, row in subset.iterrows():
            key = (
                row.get("run"),
                row.get("suite_path"),
                row.get("task_name"),
                row.get("suite"),
            )
            traj_map.setdefault(key, {})[variant] = row.get("all_messages")
    return traj_map


def success_badges_html(summary_row: pd.Series) -> str:
    """Render success-rate and task-count chips; include outlier exclusion when present."""
    chips = [
        "<span class='chip chip-success'>"
        f"Baseline Success Rate: {format_number(summary_row['baseline_success_rate'])}</span>",
        "<span class='chip chip-success'>"
        f"Evolved Success Rate: {format_number(summary_row['evolved_success_rate'])}</span>",
        "<span class='chip chip-info'>"
        f"Data Count: {format_number(summary_row['task_count'])}</span>",
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


def task_table_html(
    suite_df: pd.DataFrame,
    agent_source: AgentSource,
    trajectory_map: dict[tuple, Any] | None = None,
    profound_flags: dict[tuple, set[str]] | None = None,
) -> str:
    """Render per-task evolved vs baseline metrics and improvement columns as an HTML table.

    When *trajectory_map* is provided, each task row is followed by a collapsible
    "Show trajectory" row rendering the evolved run's execution map.

    When *profound_flags* is provided, a star (★) is added to the Task cell for any
    task with at least one profound metric, and each profound ``Impr <metric>`` cell is
    prefixed with a star.
    """
    hidden = _hidden_metrics(agent_source)
    # 按 ``METRIC_COLUMNS`` 顺序取交集（去掉 hidden），保持一致的展示顺序，
    # 未来在 ``METRIC_COLUMNS`` 加/删列时不用再改这里。
    metric_columns: list[str] = [m for m in METRIC_COLUMNS if m not in hidden]

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

    total_cols = 3 + len(metric_columns) * 2
    for seq, (_, row) in enumerate(suite_df.iterrows()):
        row_profound = profound_flags.get(_row_profound_key(row), set()) if profound_flags else set()
        task_cell = escape(task_label(row))
        if row_profound:
            task_cell = (
                "<span class='profound-star' title='Profound: better than the best baseline "
                "on at least one metric'>&#9733;</span> " + task_cell
            )
        cells = [
            f"<td class='cell-run'>{format_number(row['run'])}</td>",
            f"<td class='cell-benchmark'>{escape(str(row.get('suite_name', '')))}</td>",
            f"<td class='cell-task'>{task_cell}</td>",
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
            if metric in row_profound:
                impr_inner = (
                    "<span class='profound-star' title='Profound on this metric: "
                    "better than the best baseline across runs'>&#9733;</span> "
                    + format_percent(impr_val)
                )
                cells.append(_colored_td(impr_val, metric, format_percent, inner=impr_inner))
            else:
                cells.append(_colored_td(impr_val, metric, format_percent))
        lines.append("<tr>" + "".join(cells) + "</tr>")

        # Optional trajectory comparison row (evolved vs baseline) for this task.
        if trajectory_map is not None:
            key = (
                row.get("run"),
                row.get("suite_path"),
                row.get("task_name"),
                row.get("suite"),
            )
            variants = trajectory_map.get(key)
            if variants:
                base_id = f"traj-{escape(str(row.get('suite')))}-{format_number(row['run'])}-{seq}"
                base_id = base_id.replace(" ", "_")
                compare = trajectory_compare_html(
                    variants.get("evolved"),
                    variants.get("baseline"),
                    base_id,
                )
                lines.append(
                    f"<tr class='traj-row'><td colspan='{total_cols}'>"
                    f"{compare}"
                    "</td></tr>"
                )

    lines.append("</tbody></table>")
    lines.append("</div>")
    return "\n".join(lines)


_LEGEND_HTML = """
<section class='section legend'>
  <h2>Legend</h2>
  <div class='legend-grid'>
    <div class='legend-card'>
      <h3>Task Icons</h3>
      <ul>
        <li><span class='legend-icon'>🎓</span> Test-set task</li>
        <li><span class='legend-icon'>✅</span> All task requirements satisfied</li>
        <li><span class='legend-icon'>❌</span> Some task requirements not satisfied</li>
        <li><span class='legend-icon profound-star'>&#9733;</span> Profound task: evolved beats the
        <em>best</em> baseline of that task across all runs on at least one metric (the matching
        <code>Impr</code> cell is also starred).</li>
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
    </div>
    <div class='legend-card'>
      <h3>Notation &amp; Outlier Rule</h3>
      <ul>
        <li><code>Impr metric</code>: relative improvement of evolved over baseline.</li>
        <li><span class='profound-star'>&#9733;</span> <strong>Profound</strong>: evolved metric is better
        than the best baseline of that task across runs.</li>
      </ul>
      <p class='muted' style='margin:6px 0 0;'>Outlier Rule: if a task's evolved vs. baseline differs too much on
      trials or tool use num, that task is excluded from the summary aggregation (it still appears in the
      per-task Run Blocks below).</p>
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

(function () {
  var cache = {};
  function trajData(id) {
    if (cache[id]) return cache[id];
    var el = document.querySelector("script.traj-data[data-traj='" + (window.CSS && CSS.escape ? CSS.escape(id) : id) + "']");
    if (!el) return null;
    try { cache[id] = JSON.parse(el.textContent); } catch (e) { cache[id] = null; }
    return cache[id];
  }
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  function block(title, value) {
    if (value == null || value === '') return '';
    var text = (typeof value === 'string') ? value : JSON.stringify(value, null, 2);
    return "<div class='traj-block'><div class='traj-block-title'>" + esc(title) +
      "</div><pre class='traj-block-body'>" + esc(text) + "</pre></div>";
  }
  function showDetail(id, idx) {
    var data = trajData(id);
    var panel = document.getElementById('detail-' + id);
    if (!data || !panel || !data.nodes[idx]) return;
    var node = data.nodes[idx];
    var html = "<div class='traj-detail-head traj-kind-" + esc(node.kind) + "'>" +
      esc(node.label) + (node.turn ? " · turn " + node.turn : "") + "</div>";
    html += block('reasoning', node.reasoning);
    html += block('content', node.content);
    if (node.kind === 'tool') {
      html += block('tool', node.tool_name);
      html += block('arguments', node.arguments);
    }
    if (html.indexOf('traj-block') === -1) {
      html += "<div class='traj-detail-empty muted'>(node has no extra detail)</div>";
    }
    panel.innerHTML = html;
    var nodes = document.querySelectorAll("g.traj-node[data-traj='" + id + "']");
    nodes.forEach(function (g) { g.classList.toggle('selected', g.getAttribute('data-idx') === String(idx)); });
  }
  function handler(ev) {
    var g = ev.target.closest('g.traj-node');
    if (!g) return;
    showDetail(g.getAttribute('data-traj'), parseInt(g.getAttribute('data-idx'), 10));
  }
  document.addEventListener('click', handler);
  document.addEventListener('keydown', function (ev) {
    if (ev.key !== 'Enter' && ev.key !== ' ') return;
    var g = ev.target.closest && ev.target.closest('g.traj-node');
    if (!g) return;
    ev.preventDefault();
    showDetail(g.getAttribute('data-traj'), parseInt(g.getAttribute('data-idx'), 10));
  });
  window.toggleTraj = function (btn, variant) {
    var wrap = btn.closest('.traj-compare-wrap');
    if (!wrap) return;
    var col = wrap.querySelector(".traj-col[data-variant='" + variant + "']");
    if (!col) return;
    var nowHidden = !col.hasAttribute('hidden');
    if (nowHidden) {
      col.setAttribute('hidden', '');
    } else {
      col.removeAttribute('hidden');
    }
    btn.classList.toggle('active', !nowHidden);
    var label = variant.charAt(0).toUpperCase() + variant.slice(1);
    btn.textContent = (nowHidden ? 'Show ' : 'Hide ') + variant + ' trajectory';
  };

  // Inlined style so a downloaded standalone SVG looks identical to the on-page one.
  var SVG_STYLE = [
    '.traj-edge{stroke:#94a3b8;stroke-width:1.5;fill:none;}',
    '.traj-node-label{font-size:12px;font-weight:600;fill:#1f2937;'
      + "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Noto Sans SC','PingFang SC','Microsoft YaHei',sans-serif;}",
    '.traj-turn{font-size:11px;fill:#6b7280;font-weight:600;'
      + "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Noto Sans SC','PingFang SC','Microsoft YaHei',sans-serif;}"
  ].join('');

  // Build a standalone, self-styled SVG string from an on-page trajectory SVG.
  // Returns { xml, width, height } or null.
  function buildStandaloneSvg(btn) {
    var col = btn.closest('.traj-col') || (btn.parentNode && btn.parentNode.parentNode);
    var svg = col ? col.querySelector('svg.traj-svg') : null;
    if (!svg) return null;
    var clone = svg.cloneNode(true);
    clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
    clone.setAttribute('xmlns:xlink', 'http://www.w3.org/1999/xlink');
    var vb = (svg.getAttribute('viewBox') || '').split(/\\s+/);
    var w = vb.length === 4 ? parseFloat(vb[2]) : (svg.clientWidth || 800);
    var h = vb.length === 4 ? parseFloat(vb[3]) : (svg.clientHeight || 600);
    clone.setAttribute('width', w);
    clone.setAttribute('height', h);
    // Embed CSS + white background rect (class styles aren't carried out of the page).
    var style = document.createElementNS('http://www.w3.org/2000/svg', 'style');
    style.textContent = SVG_STYLE;
    clone.insertBefore(style, clone.firstChild);
    var bg = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    bg.setAttribute('x', vb.length === 4 ? vb[0] : '0');
    bg.setAttribute('y', vb.length === 4 ? vb[1] : '0');
    bg.setAttribute('width', '100%');
    bg.setAttribute('height', '100%');
    bg.setAttribute('fill', '#ffffff');
    clone.insertBefore(bg, style.nextSibling);
    var xml = new XMLSerializer().serializeToString(clone);
    if (xml.indexOf('<?xml') !== 0) {
      xml = '<?xml version="1.0" encoding="UTF-8"?>\\n' + xml;
    }
    return { xml: xml, width: w, height: h };
  }

  function triggerDownload(blob, filename) {
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(url); }, 0);
  }

  window.downloadTrajSvg = function (btn) {
    var built = buildStandaloneSvg(btn);
    if (!built) return;
    var blob = new Blob([built.xml], { type: 'image/svg+xml;charset=utf-8' });
    triggerDownload(blob, (btn.getAttribute('data-svgname') || 'trajectory') + '.svg');
  };

  // Rasterize the standalone SVG to a PNG (default 2x scale) for tools that don't
  // accept SVG (e.g. Feishu / Lark docs).
  window.downloadTrajPng = function (btn, scale) {
    var built = buildStandaloneSvg(btn);
    if (!built) return;
    scale = scale || 2;
    var name = (btn.getAttribute('data-svgname') || 'trajectory') + '.png';
    // Encode as a data URL (UTF-8 safe) so the image loads without CORS taint.
    var svg64 = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(built.xml);
    var img = new Image();
    img.onload = function () {
      var canvas = document.createElement('canvas');
      canvas.width = Math.max(1, Math.round(built.width * scale));
      canvas.height = Math.max(1, Math.round(built.height * scale));
      var ctx = canvas.getContext('2d');
      ctx.fillStyle = '#ffffff';
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
      if (canvas.toBlob) {
        canvas.toBlob(function (blob) {
          if (blob) triggerDownload(blob, name);
        }, 'image/png');
      } else {
        var durl = canvas.toDataURL('image/png');
        triggerDownload(dataUrlToBlob(durl), name);
      }
    };
    img.onerror = function () {
      alert('PNG export failed for this trajectory. You can still download the SVG.');
    };
    img.src = svg64;
  };

  function dataUrlToBlob(durl) {
    var parts = durl.split(',');
    var bin = atob(parts[1]);
    var arr = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
    return new Blob([arr], { type: 'image/png' });
  }
})();
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
  --profound: #d97757;
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

.gb-wrap { margin: 8px 0 4px; }
.gb-legend { display: flex; justify-content: center; gap: 18px; font-size: 13px; color: var(--muted); margin-bottom: 8px; }
.gb-legend .swatch { width: 12px; height: 12px; }
.gb-sw-good { background: var(--good); }
.gb-sw-tie { background: var(--nan); }
.gb-sw-bad { background: var(--bad); }
.gb-sw-profound {
  background: repeating-linear-gradient(45deg, var(--profound) 0 2px, transparent 2px 5px), #ecfdf5;
}
.gb-chart { width: 100%; height: auto; max-width: 880px; display: block; margin: 0 auto; }
.gb-chart .gb-label { font-size: 13px; fill: #1f2937; font-weight: 600; }
.gb-chart .gb-good { fill: var(--good); }
.gb-chart .gb-tie { fill: var(--nan); }
.gb-chart .gb-bad { fill: var(--bad); }
.gb-chart .gb-empty { fill: #eef0f4; stroke: var(--border); }
.gb-chart .gb-empty-text { font-size: 12px; fill: var(--nan); }
.gb-chart .gb-count { font-size: 13px; fill: #111827; font-weight: 700; }
.gb-chart .gb-profound-count { font-size: 12.5px; fill: var(--profound); font-weight: 700; }

.profound-star { color: var(--profound); font-weight: 700; }

.traj-row > td { background: #fbfcff; padding: 0 12px 14px; }
.traj-compare-wrap { border: 1px solid var(--border); border-radius: 10px; padding: 12px; background: #fff; margin-top: 8px; }
.traj-legend { display: flex; flex-wrap: wrap; gap: 16px; font-size: 12px; color: var(--muted); margin-bottom: 10px; align-items: center; }
.traj-legend .swatch { width: 12px; height: 12px; }
.traj-sw-user { background: #2546b8; }
.traj-sw-assistant { background: #047857; }
.traj-sw-tool { background: #b45309; }
.traj-toolbar { display: flex; gap: 8px; margin-bottom: 12px; }
.traj-toggle-btn {
  background: var(--accent-soft);
  color: var(--accent);
  border: 1px solid #d6e0ff;
  padding: 6px 14px;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.15s ease;
}
.traj-toggle-btn:hover { background: #dde6ff; }
.traj-toggle-btn.active { background: var(--accent); color: #fff; border-color: var(--accent); }
.traj-compare { display: flex; gap: 16px; align-items: flex-start; }
.traj-compare > .traj-col { flex: 1 1 0; min-width: 0; border: 1px solid var(--border); border-radius: 8px; padding: 10px; background: #fcfdff; }
.traj-compare > .traj-col[hidden] { display: none; }
.traj-col-title { font-size: 14px; font-weight: 700; margin-bottom: 8px; color: #1f2937; }
.traj-col-toolbar { display: flex; justify-content: flex-end; gap: 8px; margin-bottom: 6px; }
.traj-dl-btn {
  background: #fff;
  color: var(--accent);
  border: 1px solid #d6e0ff;
  padding: 4px 10px;
  border-radius: 7px;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.15s ease;
}
.traj-dl-btn:hover { background: var(--accent-soft); }
.traj-canvas { overflow-x: auto; }
.traj-svg { width: 100%; height: auto; min-width: 560px; }
.traj-edge { stroke: #94a3b8; stroke-width: 1.5; fill: none; }
.traj-node { cursor: pointer; }
.traj-node rect { transition: filter 0.12s ease; }
.traj-node:hover rect { filter: brightness(0.97); }
.traj-node.selected rect { stroke-width: 3; }
.traj-node-label { font-size: 12px; font-weight: 600; fill: #1f2937; }
.traj-turn { font-size: 11px; fill: var(--muted); font-weight: 600; }
.traj-detail {
  margin-top: 10px;
  max-height: 360px;
  overflow: auto;
  border-top: 1px solid var(--border);
  padding-top: 10px;
}
.traj-detail-head { font-weight: 700; font-size: 14px; margin-bottom: 8px; padding: 4px 8px; border-radius: 6px; display: inline-block; }
.traj-kind-user { background: #eef3ff; color: #2546b8; }
.traj-kind-assistant { background: #ecfdf5; color: #047857; }
.traj-kind-tool { background: #fff7ed; color: #b45309; }
.traj-block { margin-bottom: 10px; }
.traj-block-title { font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); margin-bottom: 3px; font-weight: 700; }
.traj-block-body {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  background: #f6f8fc;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px 10px;
  font-size: 12px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  color: #1f2937;
}
.traj-detail-empty { font-size: 13px; padding: 6px 0; }
.traj-empty { padding: 10px 0; }
@media (max-width: 1000px) {
  .traj-compare { flex-direction: column; }
  .traj-compare > .traj-col { width: 100%; }
}
"""


def render_report_html(
    comparison_df: pd.DataFrame,
    summary_df: pd.DataFrame,
    title: str,
    agent_source: AgentSource = "openclaw",
    trajectory_map: dict[tuple, Any] | None = None,
) -> str:
    """Assemble a full HTML metrics report with global and per-suite sections.

    *trajectory_map* maps ``(run, suite_path, task_name, suite)`` to
    ``{"evolved": <all_messages>, "baseline": <all_messages>}``; when provided, each
    task row gains a side-by-side evolved/baseline trajectory comparison.
    """
    global_row = summary_df[summary_df["scope"] == "global"].iloc[0]
    suite_rows = summary_df[summary_df["scope"] == "suite"]

    # Profound flags: per task-row, the metrics where evolved beats the best baseline
    # across runs. The global chart sums them automatically by iterating all rows.
    profound_flags = build_profound_flags(comparison_df, agent_source)

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
        "<div class='muted'>每个 suite 单独展示汇总指标与任务对比明细。</div>",
        _LEGEND_HTML,
        "<section class='section'>",
        "<h2>Global Summary</h2>",
        success_badges_html(global_row),
        summary_table_html(global_row, agent_source),
        "<h3>Better / Worse Task Counts</h3>",
        good_bad_chart_html(comparison_df, agent_source, profound_flags, chart_id="global"),
        "</section>",
    ]

    for suite_seq, (_, summary_row) in enumerate(suite_rows.iterrows()):
        suite = summary_row["suite"]
        suite_df = comparison_df[comparison_df["suite"] == suite].copy()
        run_groups = list(suite_df.groupby("run", dropna=False))

        run_blocks: list[str] = []
        for _, (run_index, run_df) in enumerate(run_groups):
            task_count = len(run_df)
            run_blocks.append(
                "<details class='run-block'>"
                f"<summary>Run {escape(str(run_index))} "
                f"<span class='muted'>({task_count} tasks)</span></summary>"
                f"{task_table_html(run_df, agent_source, trajectory_map, profound_flags)}"
                "</details>"
            )

        parts.extend(
            [
                "<section class='section'>",
                f"<h2>Suite: {escape(str(suite))}</h2>",
                success_badges_html(summary_row),
                summary_table_html(summary_row, agent_source),
                "<h3>Better / Worse Task Counts</h3>",
                good_bad_chart_html(suite_df, agent_source, profound_flags, chart_id=f"suite{suite_seq}"),
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
