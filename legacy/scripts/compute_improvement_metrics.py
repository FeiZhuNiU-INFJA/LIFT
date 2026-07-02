import argparse
from pathlib import Path

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


def load_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, encoding="utf-8-sig")


def compute_ratio(evolved_value, baseline_value):
    baseline = pd.to_numeric(pd.Series([baseline_value]), errors="coerce").iloc[0]
    evolved = pd.to_numeric(pd.Series([evolved_value]), errors="coerce").iloc[0]
    if pd.isna(baseline) or pd.isna(evolved) or baseline == 0:
        return float("nan")
    return float(evolved) / float(baseline)


def validate_pairs(df: pd.DataFrame) -> None:
    required_columns = KEY_COLUMNS + ["baseline", "evolved", "success", "is_final_task"] + METRIC_COLUMNS
    missing = [col for col in required_columns if col not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    dup_mask = df.duplicated(subset=KEY_COLUMNS + ["baseline", "evolved"], keep=False)
    if dup_mask.any():
        dup_rows = df.loc[dup_mask, KEY_COLUMNS + ["baseline", "evolved"]]
        raise ValueError(f"Found duplicate task rows:\n{dup_rows.to_string(index=False)}")


def build_comparison_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    baseline_df = df[df["baseline"] == True].copy()
    evolved_df = df[df["evolved"] == True].copy()

    merged = baseline_df.merge(
        evolved_df,
        on=KEY_COLUMNS,
        suffixes=("_baseline", "_evolved"),
        how="inner",
        validate="one_to_one",
    )

    if merged.empty:
        raise ValueError("No baseline/evolved task pairs were found.")

    out = merged[KEY_COLUMNS].copy()
    out["is_final_task"] = merged["is_final_task_baseline"]
    out["success"] = merged["success_evolved"]

    for metric in METRIC_COLUMNS:
        out[metric] = merged[f"{metric}_evolved"]
        out[f"impr_{metric}"] = merged.apply(
            lambda row: compute_ratio(row[f"{metric}_evolved"], row[f"{metric}_baseline"]),
            axis=1,
        )

    return out


def print_improvement_summary(title: str, comparison_df: pd.DataFrame) -> None:
    if title:
        print(title)
    for metric in METRIC_COLUMNS:
        series = pd.to_numeric(comparison_df[f"impr_{metric}"], errors="coerce")
        print(
            f"impr_{metric}: "
            f"mean={series.mean():.6f} "
            f"var={series.var(ddof=0):.6f}"
        )


def print_success_rate_summary(title: str, df: pd.DataFrame) -> None:
    if title:
        print(title)
    baseline_success_rate = pd.to_numeric(
        df.loc[df["baseline"] == True, "success"].astype(int),
        errors="coerce",
    ).mean()
    evolved_success_rate = pd.to_numeric(
        df.loc[df["evolved"] == True, "success"].astype(int),
        errors="coerce",
    ).mean()

    print(f"baseline_success_rate={baseline_success_rate:.6f}")
    print(f"evolved_success_rate={evolved_success_rate:.6f}")


def print_global_stats(comparison_df: pd.DataFrame, original_df: pd.DataFrame) -> None:
    for category, category_comparison_df in comparison_df.groupby("category", dropna=False):
        category_label = category if pd.notna(category) else "UNKNOWN"
        print(f"=== Category: {category_label} Improvement Stats ===")
        print_improvement_summary("", category_comparison_df)
        category_original_df = original_df[original_df["category"] == category].copy()
        print(f"=== Category: {category_label} Success Rate ===")
        print_success_rate_summary("", category_original_df)

    print("=== Global Improvement Stats ===")
    print_improvement_summary("", comparison_df)
    print("=== Global Success Rate ===")
    print_success_rate_summary("", original_df)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compute evolved/baseline improvement ratios from a trajectory-scored CSV."
    )
    parser.add_argument("input_csv", help="Path to the trajectory-scored CSV file.")
    parser.add_argument(
        "-o",
        "--output",
        help="Output CSV path. Defaults to <input_stem>_improvement.csv next to the input file.",
    )
    args = parser.parse_args()

    input_path = Path(args.input_csv).resolve()
    output_path = (
        Path(args.output).resolve()
        if args.output
        else input_path.with_name(f"{input_path.stem}_improvement.csv")
    )

    df = load_csv(input_path)
    validate_pairs(df)
    comparison_df = build_comparison_dataframe(df)
    comparison_df.to_csv(output_path, index=False, encoding="utf-8-sig")

    print(f"Input: {input_path}")
    print(f"Rows: {len(comparison_df)}")
    print(f"Output: {output_path}")
    print_global_stats(comparison_df, df)


if __name__ == "__main__":
    main()
