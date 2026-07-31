"""Recompute paper tables using population-ratio Δ = (mean(E) - mean(B)) / mean(B).

The per-task ratio mean (impr_metric column) is dominated by low-baseline outliers
and reverses the direction of the population effect (see hermes case). We therefore
recompute from raw baseline/evolved columns.
"""

from __future__ import annotations

import csv
import math
from collections import defaultdict
from pathlib import Path
from statistics import mean, pstdev

ROOT = Path("/root/workspace/agent_evolve_evaluation/results/_reports")

RUNS = {
    "openclaw": "lift-runid-openclaw-full-r10",
    "openclaw+openspace": "lift-runid-openclaw-openspace-full",
    "hermes": "lift-runid-hermes-10-a",
    "hermes+openspace": "lift-runid-hermes-openspace-full",
    "genericagent": "lift-runid-genericagent-full",
}

METRICS = [
    ("trials", "turns"),
    ("tool_use_num", "tools"),
    ("total_tokens", "tokens"),
    ("total_latency_seconds", "latency"),
]


def _f(v: str) -> float | None:
    if v is None or v == "":
        return None
    try:
        x = float(v)
        if math.isnan(x):
            return None
        return x
    except (TypeError, ValueError):
        return None


def _load(run_dir: Path) -> list[dict]:
    csv_path = next(run_dir.glob("*_comparison_metrics.csv"))
    with csv_path.open(encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def _load_summary_all(run_dir: Path) -> dict:
    csv_path = next(run_dir.glob("*_summary_metrics.csv"))
    with csv_path.open(encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            if row.get("suite") == "ALL":
                return row
    return {}


def population_delta(rows: list[dict], metric_col: str) -> dict:
    b_vals, e_vals = [], []
    for r in rows:
        b = _f(r.get(f"baseline_{metric_col}", ""))
        e = _f(r.get(metric_col, ""))
        if b is None or e is None:
            continue
        b_vals.append(b)
        e_vals.append(e)
    if not b_vals:
        return {"mean_b": None, "mean_e": None, "delta_abs": None, "delta_pct": None, "n": 0}
    mb, me = mean(b_vals), mean(e_vals)
    return {
        "mean_b": mb,
        "mean_e": me,
        "delta_abs": me - mb,
        "delta_pct": (me - mb) / mb * 100 if mb else None,
        "n": len(b_vals),
    }


def per_repeat_population_delta(rows: list[dict], metric_col: str) -> dict:
    """Split rows by 'run' (each --repeat produces its own run_id), then compute
    a population Δ% per repeat, then mean/std/CI over repeats."""
    by_run = defaultdict(lambda: {"b": [], "e": []})
    for r in rows:
        run_id = r.get("run") or r.get("run_id") or ""
        b = _f(r.get(f"baseline_{metric_col}", ""))
        e = _f(r.get(metric_col, ""))
        if b is None or e is None:
            continue
        by_run[run_id]["b"].append(b)
        by_run[run_id]["e"].append(e)
    per_repeat_pcts = []
    for vals in by_run.values():
        if not vals["b"]:
            continue
        mb, me = mean(vals["b"]), mean(vals["e"])
        if mb:
            per_repeat_pcts.append((me - mb) / mb * 100)
    if not per_repeat_pcts:
        return {"mean": None, "std": None, "ci_low": None, "ci_high": None, "n": 0}
    m = mean(per_repeat_pcts)
    s = pstdev(per_repeat_pcts) if len(per_repeat_pcts) > 1 else 0.0
    n = len(per_repeat_pcts)
    half = 1.96 * s / math.sqrt(n) if n > 1 else 0.0
    return {"mean": m, "std": s, "ci_low": m - half, "ci_high": m + half, "n": n}


def fmt(v, prec=2):
    if v is None:
        return "n/a"
    return f"{v:.{prec}f}"


def sgn(v, prec=2):
    if v is None:
        return "n/a"
    return f"{v:+.{prec}f}"


def main() -> None:
    # === RQ1 main table (population Δ) ===
    print("## RQ1 — Base vs. Loaded (population Δ = (mean_E - mean_B) / mean_B, macro over holdout tasks × 10 repeats)\n")
    print("| runtime | tasks | Base Pass | Loaded Pass | ΔPass | ΔTurns% | ΔTools% | ΔTokens% | ΔLatency% |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    rq1 = {}
    for name, run in RUNS.items():
        rows = _load(ROOT / run)
        summary_all = _load_summary_all(ROOT / run)
        b_pass = _f(summary_all.get("baseline_success_rate", ""))
        e_pass = _f(summary_all.get("evolved_success_rate", ""))
        d_pass = (e_pass - b_pass) if (b_pass is not None and e_pass is not None) else None
        deltas = {label: population_delta(rows, col) for col, label in METRICS}
        rq1[name] = {"rows": rows, "b_pass": b_pass, "e_pass": e_pass, "d_pass": d_pass, "deltas": deltas}
        n_agg = _f(summary_all.get("task_count_aggregated", ""))
        print(
            f"| `{name}` | {len(rows)} ({int(n_agg) if n_agg else '-'}) | "
            f"{fmt(b_pass, 4)} | {fmt(e_pass, 4)} | "
            f"{sgn(d_pass * 100, 2) if d_pass is not None else 'n/a'} | "
            f"{sgn(deltas['turns']['delta_pct'])} | "
            f"{sgn(deltas['tools']['delta_pct'])} | "
            f"{sgn(deltas['tokens']['delta_pct'])} | "
            f"{sgn(deltas['latency']['delta_pct'])} |"
        )

    # === RQ2 delta-of-delta ===
    print("\n## RQ2 — Augmentation gain (delta-of-delta, same base runtime)\n")
    print("Gain = Δ_augmented − Δ_base. Negative Gain means augmentation is more efficient than implicit alone.\n")
    print("| base | ΔTurns%_base | ΔTurns%_aug | Gain (turns) | ΔTools%_base | ΔTools%_aug | Gain (tools) | ΔTokens%_base | ΔTokens%_aug | Gain (tokens) |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for base_name, aug_name in [("openclaw", "openclaw+openspace"), ("hermes", "hermes+openspace")]:
        b = rq1[base_name]["deltas"]
        a = rq1[aug_name]["deltas"]
        row = [f"`{base_name}`"]
        for label in ["turns", "tools", "tokens"]:
            db = b[label]["delta_pct"]
            da = a[label]["delta_pct"]
            gain = da - db
            row.extend([sgn(db), sgn(da), sgn(gain)])
        print("| " + " | ".join(row) + " |")

    # === RQ3 per-repeat stability ===
    print("\n## RQ3 — Per-repeat stability (population Δ per repeat, then mean/std/95% CI over 10 repeats)\n")
    print("| runtime | ΔTurns% mean | std | 95% CI | ΔTools% mean | std |")
    print("|---|---:|---:|---|---:|---:|")
    for name in RUNS:
        rows = rq1[name]["rows"]
        t = per_repeat_population_delta(rows, "trials")
        u = per_repeat_population_delta(rows, "tool_use_num")
        ci = f"[{sgn(t['ci_low'])}, {sgn(t['ci_high'])}]" if t['ci_low'] is not None else "n/a"
        print(
            f"| `{name}` | {sgn(t['mean'])} | {fmt(t['std'])} | {ci} | "
            f"{sgn(u['mean'])} | {fmt(u['std'])} |"
        )

    # === RQ4 cost view ===
    print("\n## RQ4 — Cost view (population means + Δ)\n")
    print("| runtime | Base turns | Base tools | Base tokens | ΔTurns% | ΔTools% | ΔTokens% | ΔLatency% |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|")
    for name, d in rq1.items():
        t = d["deltas"]["turns"]; tl = d["deltas"]["tools"]; tk = d["deltas"]["tokens"]; lt = d["deltas"]["latency"]
        print(
            f"| `{name}` | {fmt(t['mean_b'], 1)} | {fmt(tl['mean_b'], 1)} | {int(tk['mean_b']):,} | "
            f"{sgn(t['delta_pct'])} | {sgn(tl['delta_pct'])} | {sgn(tk['delta_pct'])} | {sgn(lt['delta_pct'])} |"
        )


if __name__ == "__main__":
    main()
