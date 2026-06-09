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
    """Directory for structured eval reports (``evobench-reports/``)."""
    return _cwd(cwd) / EVOBENCH_REPORTS_DIR


def report_json_path(run_id: str, cwd: Path | None = None) -> Path:
    """Path to the main report JSON for a run (``evobench-reports/{run_id}.json``)."""
    return default_report_root(cwd) / f"{run_id}.json"


def default_results_root(cwd: Path | None = None) -> Path:
    """Top-level directory for workspaces and post-process outputs (``results/``)."""
    return _cwd(cwd) / RESULTS_DIR


def results_run_dir(run_id: str, cwd: Path | None = None) -> Path:
    """Per-run folder under ``results/`` (workspaces + metrics after ``-e``)."""
    return default_results_root(cwd) / run_id


def outcome_root(run_id: str, cwd: Path | None = None) -> Path:
    """Agent workspace root for a run (``results/{run_id}/outcome/``)."""
    return results_run_dir(run_id, cwd) / "outcome"
