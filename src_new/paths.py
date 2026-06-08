"""Repository layout paths for eval reports and agent workspaces."""

from __future__ import annotations

from pathlib import Path

EVOBENCH_REPORTS_DIR = "evobench-reports"
RESULTS_DIR = "results"

CONTAINER_OUTCOME_ROOT = "/workspace/outcome"
CONTAINER_BENCHMARKS_ROOT = "/workspace/benchmarks"
CONTAINER_EVOBENCH_REPORTS_ROOT = "/workspace/evobench-reports"


def _cwd(cwd: Path | None) -> Path:
    return cwd if cwd is not None else Path.cwd()


def default_report_root(cwd: Path | None = None) -> Path:
    return _cwd(cwd) / EVOBENCH_REPORTS_DIR


def report_json_path(run_id: str, cwd: Path | None = None) -> Path:
    return default_report_root(cwd) / f"{run_id}.json"


def default_results_root(cwd: Path | None = None) -> Path:
    return _cwd(cwd) / RESULTS_DIR


def results_run_dir(run_id: str, cwd: Path | None = None) -> Path:
    return default_results_root(cwd) / run_id


def outcome_root(run_id: str, cwd: Path | None = None) -> Path:
    return results_run_dir(run_id, cwd) / "outcome"
