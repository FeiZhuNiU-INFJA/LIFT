"""仓库目录布局：评测报告、agent workspace 与容器内路径常量。"""

from __future__ import annotations

from pathlib import Path

EVOBENCH_REPORTS_DIR = "evobench-reports"
"""结构化评测报告根目录名（相对 cwd）。"""

RESULTS_DIR = "results"
"""workspace 与后处理输出根目录名（相对 cwd）。"""

CONTAINER_OUTCOME_ROOT = "/workspace/outcome"
"""容器内 agent workspace 根路径。"""

CONTAINER_BENCHMARKS_ROOT = "/workspace/benchmarks"
"""容器内 benchmark suite JSON 根路径。"""

CONTAINER_EVOBENCH_REPORTS_ROOT = "/workspace/evobench-reports"
"""容器内评测报告根路径。"""


def _cwd(cwd: Path | None) -> Path:
    """解析工作目录，``None`` 时使用 ``Path.cwd()``。"""
    return cwd if cwd is not None else Path.cwd()


def default_report_root(cwd: Path | None = None) -> Path:
    """结构化评测报告目录（``evobench-reports/``）。"""
    return _cwd(cwd) / EVOBENCH_REPORTS_DIR


def report_json_path(run_id: str, cwd: Path | None = None) -> Path:
    """单次 run 的主报告 JSON 路径（``evobench-reports/{run_id}.json``）。"""
    return default_report_root(cwd) / f"{run_id}.json"


def default_results_root(cwd: Path | None = None) -> Path:
    """workspace 与后处理输出顶层目录（``results/``）。"""
    return _cwd(cwd) / RESULTS_DIR


def results_run_dir(run_id: str, cwd: Path | None = None) -> Path:
    """单次 run 在 ``results/`` 下的目录（workspace + ``-e`` 后处理指标）。"""
    return default_results_root(cwd) / run_id


def outcome_root(run_id: str, cwd: Path | None = None) -> Path:
    """单次 run 的 agent workspace 根目录（``results/{run_id}/outcome/``）。"""
    return results_run_dir(run_id, cwd) / "outcome"
