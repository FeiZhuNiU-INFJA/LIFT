"""仓库目录布局：评测报告、agent workspace 与容器内路径常量。"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""仓库根目录（``evolve_eval``）。"""

OPENCLAW_AGENT_DIR = PROJECT_ROOT / "agent-runtimes" / "openclaw"
"""OpenClaw 镜像与容器配置目录（``agent-runtimes/openclaw/``）。"""

OPENCLAW_CONTAINER_DEFAULTS_PATH = OPENCLAW_AGENT_DIR / "container_defaults.yaml"
"""OpenClaw 默认容器镜像名配置。"""

OPENCLAW_WORKSPACE_SEED_DIR = OPENCLAW_AGENT_DIR / "workspace_seed"
"""宿主机 OpenClaw eval workspace seed 源目录。"""

RESULTS_DIR = "results"
"""单次 eval run 产物根目录名（相对 cwd）：report、outcome、后处理指标。"""

REPORT_JSON_FILENAME = "report.json"
"""单次 run 执行期 report 文件名（位于 ``results/{run_id}/``）。"""

CONTAINER_OUTCOME_ROOT = "/workspace/outcome"
"""容器内 agent workspace 根路径。"""

CONTAINER_BENCHMARKS_ROOT = "/workspace/benchmarks"
"""容器内 benchmark suite JSON 根路径。"""


def _cwd(cwd: Path | None) -> Path:
    """解析工作目录，``None`` 时使用 ``Path.cwd()``。"""
    return cwd if cwd is not None else Path.cwd()


def default_results_root(cwd: Path | None = None) -> Path:
    """评测产物顶层目录（``results/``）。"""
    return _cwd(cwd) / RESULTS_DIR


def results_run_dir(run_id: str, cwd: Path | None = None) -> Path:
    """单次 run 目录（``results/{run_id}/``）：report、outcome、后处理输出。"""
    return default_results_root(cwd) / run_id


def report_json_path(run_id: str, cwd: Path | None = None) -> Path:
    """单次 run 执行期 report JSON（``results/{run_id}/report.json``）。"""
    return results_run_dir(run_id, cwd) / REPORT_JSON_FILENAME


def outcome_root(run_id: str, cwd: Path | None = None) -> Path:
    """单次 run 的 agent workspace 根目录（``results/{run_id}/outcome/``）。"""
    return results_run_dir(run_id, cwd) / "outcome"
