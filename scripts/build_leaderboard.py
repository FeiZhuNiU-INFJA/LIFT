#!/usr/bin/env python3
"""Build the LIFT Evolution-Impact Leaderboard data file (leaderboard.json).

This is the single source of truth (SSoT) for the GitHub Pages leaderboard at
``docs/leaderboard.html``. It reads the per-task ``*_comparison_metrics.csv``
that ships inside each ``results/*.tar.xz`` run archive and re-derives, with the
*exact* population-ratio methodology used in the paper (see RQ5 / Table 6.7 and
``/tmp/newexp/compute_deltas.py``):

    delta_X% = (sum(evolved_X) - sum(baseline_X)) / sum(baseline_X) * 100

over paired holdout task-repeats where both sides are present. Stability is the
per-repeat population-ratio spread, using population std (ddof=0) and a normal
95% CI = mean +/- 1.96 * std / sqrt(n_repeats). A runtime is flagged
significant ("Sig.") when the whole ΔTurns% CI lies below zero.

IMPORTANT (methodology boundary): this leaderboard ranks *evolution impact*
(how much each runtime improved from Base to Loaded), NOT absolute capability.
Each row differences out its own Base, so the ordering never compares raw
capability across runtimes. This is the only ranking the paper authorizes.

To add a future runtime: drop its ``lift-runid-<tag>.tar.xz`` into ``results/``
and add one entry to ``RUNTIME_REGISTRY`` keyed by the archive tag. Re-run this
script; the HTML page reads whatever rows land in leaderboard.json.

Usage:
    python scripts/build_leaderboard.py                 # auto-discover results/*.tar.xz
    python scripts/build_leaderboard.py --out docs/leaderboard.json
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import sys
import tarfile
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = REPO_ROOT / "results"
DEFAULT_OUT = REPO_ROOT / "docs" / "leaderboard.json"

# ---------------------------------------------------------------------------
# Runtime registry: archive-tag -> presentation + provenance metadata.
# `tag` is matched against the tarball stem (lift-runid-<tag>.tar.xz). The first
# entry whose `tag` is a substring of the stem wins, so keep tags specific.
# `mech_class`: C = carry-only (no distillation), D = per-task distillation,
#               R = explicit post-hoc reflection (see paper Appendix C).
# ---------------------------------------------------------------------------
@dataclass
class RuntimeMeta:
    tag: str            # unique substring of the tarball stem
    runtime_key: str    # canonical adapter key
    display: str        # short label shown on the leaderboard
    base_family: str    # openclaw | hermes | genericagent | ...
    mech_class: str     # C | D | R
    mech_short: str     # one-line mechanism summary


RUNTIME_REGISTRY: list[RuntimeMeta] = [
    RuntimeMeta("openclaw-openspace", "openclaw_with_openspace", "openclaw+openspace",
                "openclaw", "C", "no-op evolve; OpenSpace adds an MCP tool surface only"),
    RuntimeMeta("openclaw-am", "openclaw_with_agentmemory", "openclaw+agentmemory",
                "openclaw", "C", "no-op evolve; passive agentmemory stores raw observations (no distillation)"),
    RuntimeMeta("openclaw-full", "openclaw", "openclaw",
                "openclaw", "C", "no-op evolve; warmup memory/skill files carried by docker commit"),
    RuntimeMeta("hermes-openspace", "hermes_with_openspace", "hermes+openspace",
                "hermes", "D", "per-task blocking review distills each session; OpenSpace as MCP tool"),
    RuntimeMeta("herems-am", "hermes_with_agentmemory", "hermes+agentmemory",
                "hermes", "D", "per-task blocking review; memory routed through agentmemory backend"),
    RuntimeMeta("hermes-10", "hermes", "hermes",
                "hermes", "D", "per-task blocking review distills each session into compact skills/memories"),
    RuntimeMeta("genericagent-active", "genericagent_active_evolve", "genericagent+active",
                "genericagent", "R", "extra reflection chat after each task and after the batch"),
    RuntimeMeta("genericagent-full", "genericagent", "genericagent",
                "genericagent", "C", "no-op evolve; memory files carried by docker commit"),
    RuntimeMeta("evosci-full", "evoscientist", "evoscientist",
                "evoscientist", "C", "no-op evolve; warmup raw observations carried by docker commit (autoskills/proposals empty, no distillation)"),
    RuntimeMeta("evosci-active", "evoscientist_active_evolve", "evoscientist+active",
                "evoscientist", "R", "post-hoc AutoSkills pass after the warmup batch distills observations into skill proposals before commit"),
]

MECH_CLASS_LABEL = {
    "C": "Carry-only (no distillation)",
    "D": "Per-task distillation",
    "R": "Post-hoc reflection",
}

# Paired (evolved, baseline) columns for each efficiency metric. All four are
# "lower-is-better": a negative delta means the Loaded run was more efficient.
PAIRS = {
    "turns":   ("trials", "baseline_trials"),
    "tools":   ("tool_use_num", "baseline_tool_use_num"),
    "tokens":  ("total_tokens", "baseline_total_tokens"),
    "latency": ("total_latency_seconds", "baseline_total_latency_seconds"),
}
RUN_COL = "run"


def _fnum(x):
    if x is None or x == "":
        return None
    try:
        return float(x)
    except ValueError:
        return None


def population_ratio(rows, evolved_col, baseline_col):
    """(sum(evolved) - sum(baseline)) / sum(baseline) * 100 over paired rows."""
    se = sb = 0.0
    n = 0
    for row in rows:
        e = _fnum(row.get(evolved_col))
        b = _fnum(row.get(baseline_col))
        if e is None or b is None:
            continue
        se += e
        sb += b
        n += 1
    if sb == 0:
        return None, n
    return (se - sb) / sb * 100.0, n


def per_repeat_ratios(rows, evolved_col, baseline_col):
    """Population-ratio delta computed within each repeat bucket."""
    buckets: dict[str, list[float]] = {}
    for row in rows:
        e = _fnum(row.get(evolved_col))
        b = _fnum(row.get(baseline_col))
        if e is None or b is None:
            continue
        run = row.get(RUN_COL, "0")
        buckets.setdefault(run, [0.0, 0.0])
        buckets[run][0] += e
        buckets[run][1] += b
    out = []
    for _run, (se, sb) in sorted(buckets.items()):
        if sb != 0:
            out.append((se - sb) / sb * 100.0)
    return out


def mean_std_ci(vals):
    n = len(vals)
    if n == 0:
        return None
    m = sum(vals) / n
    var = sum((v - m) ** 2 for v in vals) / n  # ddof=0, matches paper
    sd = math.sqrt(var)
    ci = 1.96 * sd / math.sqrt(n)
    return m, sd, (m - ci, m + ci), n


def base_mean(rows, col):
    vals = [_fnum(r.get(col)) for r in rows]
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def find_comparison_csv(tar_path: Path):
    """Return (csv_text, member_name) for the *_comparison_metrics.csv inside a tarball."""
    with tarfile.open(tar_path, "r:*") as tf:
        member = next(
            (m for m in tf.getmembers()
             if m.name.endswith("_comparison_metrics.csv")),
            None,
        )
        if member is None:
            return None, None
        fobj = tf.extractfile(member)
        raw = fobj.read().decode("utf-8-sig")
        return raw, member.name


def load_rows(csv_text: str):
    return list(csv.DictReader(io.StringIO(csv_text)))


def match_meta(stem: str) -> RuntimeMeta | None:
    for meta in RUNTIME_REGISTRY:
        if meta.tag in stem:
            return meta
    return None


def analyze_tar(tar_path: Path):
    stem = tar_path.name
    for suffix in (".tar.xz", ".tar.gz", ".tgz", ".tar"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    meta = match_meta(stem)
    if meta is None:
        print(f"  [skip] no registry entry matches '{stem}'", file=sys.stderr)
        return None

    csv_text, _member = find_comparison_csv(tar_path)
    if csv_text is None:
        print(f"  [skip] no comparison CSV in {tar_path.name}", file=sys.stderr)
        return None
    rows = load_rows(csv_text)

    deltas = {}
    paired_n = None
    for metric, (ec, bc) in PAIRS.items():
        d, n = population_ratio(rows, ec, bc)
        deltas[metric] = round(d, 2) if d is not None else None
        paired_n = n

    # ΔTurns% stability across repeats -> significance flag
    turns_repeats = per_repeat_ratios(rows, *PAIRS["turns"])
    stab = mean_std_ci(turns_repeats)
    if stab:
        t_mean, t_std, (t_lo, t_hi), n_rep = stab
        significant = t_hi < 0  # whole CI below zero
    else:
        t_mean = t_std = t_lo = t_hi = None
        n_rep = 0
        significant = False

    return {
        "runtime_key": meta.runtime_key,
        "display": meta.display,
        "base_family": meta.base_family,
        "mech_class": meta.mech_class,
        "mech_class_label": MECH_CLASS_LABEL[meta.mech_class],
        "mech_short": meta.mech_short,
        "source_archive": tar_path.name,
        "paired_task_repeats": paired_n,
        "n_repeats": n_rep,
        "delta_turns_pct": deltas["turns"],
        "delta_tools_pct": deltas["tools"],
        "delta_tokens_pct": deltas["tokens"],
        "delta_latency_pct": deltas["latency"],
        "turns_ci_low": round(t_lo, 2) if t_lo is not None else None,
        "turns_ci_high": round(t_hi, 2) if t_hi is not None else None,
        "turns_std": round(t_std, 2) if t_std is not None else None,
        "significant": significant,
        "base_turns_mean": round(base_mean(rows, "baseline_trials"), 1)
            if base_mean(rows, "baseline_trials") is not None else None,
    }


def main():
    ap = argparse.ArgumentParser(description="Build LIFT evolution-impact leaderboard JSON")
    ap.add_argument("--results-dir", default=str(RESULTS_DIR),
                    help="directory of lift-runid-*.tar.xz archives")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="output JSON path")
    args = ap.parse_args()

    results_dir = Path(args.results_dir)
    archives = sorted(results_dir.glob("*.tar.xz")) + sorted(results_dir.glob("*.tar.gz"))
    if not archives:
        print(f"No archives found in {results_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"Scanning {len(archives)} archive(s) in {results_dir} ...")
    entries = []
    for tar_path in archives:
        print(f"- {tar_path.name}")
        row = analyze_tar(tar_path)
        if row is not None:
            entries.append(row)

    # Rank by ΔTurns% ascending (most negative = biggest efficiency gain = rank 1).
    entries.sort(key=lambda r: (r["delta_turns_pct"] if r["delta_turns_pct"] is not None else 1e9))
    for i, e in enumerate(entries, start=1):
        e["rank"] = i

    payload = {
        "schema_version": 1,
        "title": "LIFT Evolution-Impact Leaderboard",
        "methodology": (
            "Runtimes are ranked by evolution impact (ΔTurns%), NOT by absolute "
            "capability. Each Δ = (Σ evolved − Σ baseline) / Σ baseline × 100 over "
            "paired holdout task-repeats, so every row differences out its own Base. "
            "ΔTurns/Tools/Tokens/Latency are lower-is-better (negative = more efficient). "
            "Sig. = the 95% CI on ΔTurns% (population std, ddof=0) lies entirely below zero."
        ),
        "primary_sort": "delta_turns_pct",
        "primary_sort_direction": "asc",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n_repeats_nominal": 10,
        "entries": entries,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nWrote {len(entries)} runtime rows -> {out_path}")
    # Console preview
    print("\n  rank  runtime                     ΔTurns%  ΔTools%  ΔTokens%  ΔLat%   Sig")
    for e in entries:
        print(f"  {e['rank']:>3}   {e['display']:<26} "
              f"{e['delta_turns_pct']:>7}  {e['delta_tools_pct']:>7}  "
              f"{e['delta_tokens_pct']:>8}  {e['delta_latency_pct']:>6}   "
              f"{'✓' if e['significant'] else '--'}")


if __name__ == "__main__":
    main()
