from typing import Any

import pandas as pd

METRIC_COLUMNS = [
    "trials",
    "tool_use_num",
    "content_score",
    "cached_token",
    "total_tokens",
    "total_latency_seconds",
    "trajectory_score",
]

KEY_COLUMNS = ["task_name", "category"]
PAIR_KEY_COLUMNS = ["run", "benchmark_name", "benchmark_path", "task_name", "category"]


def compute_ratio(evolved_value: Any, baseline_value: Any) -> float:
    baseline = pd.to_numeric(pd.Series([baseline_value]), errors="coerce").iloc[0]
    evolved = pd.to_numeric(pd.Series([evolved_value]), errors="coerce").iloc[0]
    if pd.isna(baseline) or pd.isna(evolved) or baseline == 0:
        return float("nan")
    return float(evolved) / float(baseline)


def validate_pairs(df: pd.DataFrame) -> None:
    required_columns = ["run", "benchmark_name", "benchmark_path"] + KEY_COLUMNS + ["baseline", "evolved", "success", "is_final_task"] + METRIC_COLUMNS
    missing = [col for col in required_columns if col not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    dup_mask = df.duplicated(subset=PAIR_KEY_COLUMNS + ["baseline", "evolved"], keep=False)
    if dup_mask.any():
        dup_rows = df.loc[dup_mask, PAIR_KEY_COLUMNS + ["baseline", "evolved"]]
        raise ValueError(f"Found duplicate task rows:\n{dup_rows.to_string(index=False)}")


def build_comparison_dataframe(df: pd.DataFrame) -> pd.DataFrame:
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
        out[metric] = merged[f"{metric}_evolved"]
        out[f"impr_{metric}"] = merged.apply(
            lambda row: compute_ratio(row[f"{metric}_evolved"], row[f"{metric}_baseline"]),
            axis=1,
        )

    return out


def build_summary_row(
    scope: str,
    category: Any,
    comparison_df: pd.DataFrame,
    original_df: pd.DataFrame,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "scope": scope,
        "category": category if pd.notna(category) else "UNKNOWN",
        "task_count": int(len(comparison_df)),
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
        series = pd.to_numeric(comparison_df[f"impr_{metric}"], errors="coerce")
        row[f"mean_impr_{metric}"] = float(series.mean()) if pd.notna(series.mean()) else float("nan")
        row[f"var_impr_{metric}"] = float(series.var(ddof=0)) if pd.notna(series.var(ddof=0)) else float("nan")

    return row


def build_summary_dataframe(comparison_df: pd.DataFrame, original_df: pd.DataFrame) -> pd.DataFrame:
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
    category_rows = summary_df[summary_df["scope"] == "category"]
    global_rows = summary_df[summary_df["scope"] == "global"]

    for _, row in category_rows.iterrows():
        category = row["category"]
        print(f"=== Category: {category} Improvement Stats ===")
        for metric in METRIC_COLUMNS:
            print(
                f"impr_{metric}: "
                f"mean={row[f'mean_impr_{metric}']:.6f} "
                f"var={row[f'var_impr_{metric}']:.6f}"
            )
        print(f"=== Category: {category} Success Rate ===")
        print(f"baseline_success_rate={row['baseline_success_rate']:.6f}")
        print(f"evolved_success_rate={row['evolved_success_rate']:.6f}")

    for _, row in global_rows.iterrows():
        print("=== Global Improvement Stats ===")
        for metric in METRIC_COLUMNS:
            print(
                f"impr_{metric}: "
                f"mean={row[f'mean_impr_{metric}']:.6f} "
                f"var={row[f'var_impr_{metric}']:.6f}"
            )
        print("=== Global Success Rate ===")
        print(f"baseline_success_rate={row['baseline_success_rate']:.6f}")
        print(f"evolved_success_rate={row['evolved_success_rate']:.6f}")
