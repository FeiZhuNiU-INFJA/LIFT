"""Baseline vs evolved metric comparison and summary aggregation.

Pairs task rows, computes improvement ratios and absolute diffs, and builds
category/global summary statistics with outlier exclusion for trials/tool_use.
"""

from typing import Any

import pandas as pd

# Metric columns compared between baseline and evolved variants.
METRIC_COLUMNS = [
    "trials",
    "tool_use_num",
    "content_score",
    "cached_token",
    "cached_token_ratio",
    "total_tokens",
    "total_latency_seconds",
    "trajectory_score",
]

# Columns that identify a task within a suite.
KEY_COLUMNS = ["task_name", "category"]

# Columns that uniquely identify a baseline/evolved pair.
PAIR_KEY_COLUMNS = ["run", "suite_name", "suite_path", "task_name", "category"]

# Summary 计算时，``impr_trials`` / ``impr_tool_use_num`` 超过该阈值的样本视为离群（退化过强），
# 仅在 task 详情表展示，不参与 category / global 的 mean_impr 与 mean_diff 聚合。
SUMMARY_IMPR_OUTLIER_METRICS = ("trials", "tool_use_num")
SUMMARY_IMPR_OUTLIER_THRESHOLD = 2.0


def compute_improvement_pct(evolved_value: Any, baseline_value: Any) -> float:
    """改进比例 ``(evolved - baseline) / baseline``，常用百分比形式展示。

    baseline 为 0 时无法定义相对改进，返回 NaN。
    """
    baseline = pd.to_numeric(pd.Series([baseline_value]), errors="coerce").iloc[0]
    evolved = pd.to_numeric(pd.Series([evolved_value]), errors="coerce").iloc[0]
    if pd.isna(baseline) or pd.isna(evolved) or baseline == 0:
        return float("nan")
    return (float(evolved) - float(baseline)) / float(baseline)


def compute_difference(evolved_value: Any, baseline_value: Any) -> float:
    """绝对差值 ``evolved - baseline``。"""
    baseline = pd.to_numeric(pd.Series([baseline_value]), errors="coerce").iloc[0]
    evolved = pd.to_numeric(pd.Series([evolved_value]), errors="coerce").iloc[0]
    if pd.isna(baseline) or pd.isna(evolved):
        return float("nan")
    return float(evolved) - float(baseline)


def validate_pairs(df: pd.DataFrame) -> None:
    """Raise if required columns are missing or duplicate baseline/evolved rows exist."""
    required_columns = (
        ["run", "suite_name", "suite_path"]
        + KEY_COLUMNS
        + ["baseline", "evolved", "success", "is_final_task"]
        + METRIC_COLUMNS
    )
    missing = [col for col in required_columns if col not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    dup_mask = df.duplicated(subset=PAIR_KEY_COLUMNS + ["baseline", "evolved"], keep=False)
    if dup_mask.any():
        dup_rows = df.loc[dup_mask, PAIR_KEY_COLUMNS + ["baseline", "evolved"]]
        raise ValueError(f"Found duplicate task rows:\n{dup_rows.to_string(index=False)}")


def build_comparison_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Inner-join baseline and evolved rows and attach per-metric impr/diff columns."""
    baseline_df = df[df["baseline"] == True].copy()
    evolved_df = df[df["evolved"] == True].copy()

    merged = baseline_df.merge(
        evolved_df,
        on=PAIR_KEY_COLUMNS,
        suffixes=("_baseline", "_evolved"),
        how="inner",
        validate="one_to_one",
    )
    if merged.empty:
        raise ValueError("No baseline/evolved task pairs were found.")

    out = merged[PAIR_KEY_COLUMNS].copy()
    out["is_final_task"] = merged["is_final_task_baseline"]
    out["success"] = merged["success_evolved"]

    for metric in METRIC_COLUMNS:
        # 同时保留 evolved 与 baseline 原值，HTML 详情列展示"evolved (baseline)"形式。
        out[metric] = merged[f"{metric}_evolved"]
        out[f"baseline_{metric}"] = merged[f"{metric}_baseline"]
        out[f"impr_{metric}"] = merged.apply(
            lambda row, m=metric: compute_improvement_pct(
                row[f"{m}_evolved"], row[f"{m}_baseline"]
            ),
            axis=1,
        )
        out[f"diff_{metric}"] = merged.apply(
            lambda row, m=metric: compute_difference(
                row[f"{m}_evolved"], row[f"{m}_baseline"]
            ),
            axis=1,
        )

    return out


def _outlier_mask(comparison_df: pd.DataFrame) -> pd.Series:
    """返回 summary 聚合时应剔除的样本布尔掩码。

    - ``impr_trials`` / ``impr_tool_use_num`` 任一项 >= ``SUMMARY_IMPR_OUTLIER_THRESHOLD``
      表示进化后比基线退化过强（消耗过高），视为离群。
    """
    if comparison_df.empty:
        return pd.Series(dtype=bool)
    mask = pd.Series(False, index=comparison_df.index)
    for metric in SUMMARY_IMPR_OUTLIER_METRICS:
        col = f"impr_{metric}"
        if col not in comparison_df.columns:
            continue
        series = pd.to_numeric(comparison_df[col], errors="coerce")
        mask = mask | (series >= SUMMARY_IMPR_OUTLIER_THRESHOLD)
    return mask


def build_summary_row(
    scope: str,
    category: Any,
    comparison_df: pd.DataFrame,
    original_df: pd.DataFrame,
) -> dict[str, Any]:
    """Build one summary dict for a category or global scope, excluding outlier tasks."""
    # Summary 聚合：剔除 impr_trials / impr_tool_use_num >= 阈值的离群样本。
    outlier_mask = _outlier_mask(comparison_df)
    aggregate_df = comparison_df.loc[~outlier_mask] if not comparison_df.empty else comparison_df
    excluded_count = int(outlier_mask.sum()) if not comparison_df.empty else 0

    row: dict[str, Any] = {
        "scope": scope,
        "category": category if pd.notna(category) else "UNKNOWN",
        "task_count": int(len(comparison_df)),
        "task_count_aggregated": int(len(aggregate_df)),
        "task_count_excluded": excluded_count,
    }

    baseline_success_rate = pd.to_numeric(
        original_df.loc[original_df["baseline"] == True, "success"].astype(int),
        errors="coerce",
    ).mean()
    evolved_success_rate = pd.to_numeric(
        original_df.loc[original_df["evolved"] == True, "success"].astype(int),
        errors="coerce",
    ).mean()
    row["baseline_success_rate"] = float(baseline_success_rate) if pd.notna(baseline_success_rate) else float("nan")
    row["evolved_success_rate"] = float(evolved_success_rate) if pd.notna(evolved_success_rate) else float("nan")

    for metric in METRIC_COLUMNS:
        impr_series = pd.to_numeric(aggregate_df.get(f"impr_{metric}"), errors="coerce")
        diff_series = pd.to_numeric(aggregate_df.get(f"diff_{metric}"), errors="coerce")
        row[f"mean_impr_{metric}"] = (
            float(impr_series.mean()) if impr_series is not None and pd.notna(impr_series.mean()) else float("nan")
        )
        row[f"mean_diff_{metric}"] = (
            float(diff_series.mean()) if diff_series is not None and pd.notna(diff_series.mean()) else float("nan")
        )

    return row


def build_summary_dataframe(comparison_df: pd.DataFrame, original_df: pd.DataFrame) -> pd.DataFrame:
    """Build per-category and global summary rows from comparison and original DataFrames."""
    rows: list[dict[str, Any]] = []

    for category, category_comparison_df in comparison_df.groupby("category", dropna=False):
        category_original_df = original_df[original_df["category"] == category].copy()
        rows.append(
            build_summary_row(
                scope="category",
                category=category,
                comparison_df=category_comparison_df,
                original_df=category_original_df,
            )
        )

    rows.append(
        build_summary_row(
            scope="global",
            category="ALL",
            comparison_df=comparison_df,
            original_df=original_df,
        )
    )

    return pd.DataFrame(rows)


def print_summary_to_console(summary_df: pd.DataFrame) -> None:
    """Print category and global summary metrics to stdout for quick inspection."""
    category_rows = summary_df[summary_df["scope"] == "category"]
    global_rows = summary_df[summary_df["scope"] == "global"]

    def _print_block(title: str, row: pd.Series) -> None:
        """Print one summary row's mean improvement/diff and success rates."""
        print(f"=== {title} ===")
        excluded = int(row.get("task_count_excluded", 0) or 0)
        if excluded:
            print(
                f"task_count={int(row['task_count'])} aggregated={int(row['task_count_aggregated'])} "
                f"excluded_outliers={excluded}"
            )
        for metric in METRIC_COLUMNS:
            impr = row[f"mean_impr_{metric}"]
            diff = row[f"mean_diff_{metric}"]
            impr_str = "NaN" if pd.isna(impr) else f"{impr * 100:.2f}%"
            diff_str = "NaN" if pd.isna(diff) else f"{diff:.6f}"
            print(f"{metric}: mean_impr={impr_str} mean_diff={diff_str}")
        print(
            f"baseline_success_rate={row['baseline_success_rate']:.6f} "
            f"evolved_success_rate={row['evolved_success_rate']:.6f}"
        )

    for _, row in category_rows.iterrows():
        _print_block(f"Category: {row['category']} Summary", row)

    for _, row in global_rows.iterrows():
        _print_block("Global Summary", row)
