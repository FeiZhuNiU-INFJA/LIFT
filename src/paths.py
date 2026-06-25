"""仓库目录布局：评测报告、agent workspace 与容器内路径常量。"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""仓库根目录（``evolve_eval``）。"""

OPENCLAW_AGENT_DIR = PROJECT_ROOT / "agent-runtimes" / "openclaw"
"""OpenClaw 镜像与容器配置目录（``agent-runtimes/openclaw/``）。"""

OPENCLAW_BASE_DOCKER_IMAGE = "evolve-eval-openclaw-base:latest"
"""不带 self-evolving-plugin-pro 进化插件的基础 OpenClaw 镜像（``OpenClawAdapter`` 使用；
``INSTALL_SELF_EVOLVING=false bash build-image.sh`` 构建）。"""

OPENCLAW_WITH_EVOLVE_DOCKER_IMAGE = "evolve-eval-openclaw-with-evolve:latest"
"""带 self-evolving-plugin-pro 进化插件的 OpenClaw 镜像（``OpenClawWithEvolveAdapter`` 使用；
``build-image.sh`` 默认 tag，对应 ``INSTALL_SELF_EVOLVING=true``）。"""

OPENCLAW_WORKSPACE_SEED_DIR = OPENCLAW_AGENT_DIR / "workspace_seed"
"""宿主机 OpenClaw eval workspace seed 源目录。"""

GENERICAGENT_AGENT_DIR = PROJECT_ROOT / "agent-runtimes" / "genericagent"
"""GenericAgent 镜像与容器配置目录（``agent-runtimes/genericagent/``）。"""

GENERICAGENT_DOCKER_IMAGE = "evolve-eval-genericagent:latest"
"""GenericAgent baseline 镜像（``GenericAgentAdapter`` 使用；
``agent-runtimes/genericagent/build-image.sh`` 构建）。"""

GENERICAGENT_WORKSPACE_SEED_DIR = GENERICAGENT_AGENT_DIR / "workspace_seed"
"""宿主机 GenericAgent eval workspace seed 源目录。"""

RESULTS_DIR = "results"
"""单次 eval run 产物根目录名（相对 cwd）：report、outcome、后处理指标。"""

REPORT_JSON_FILENAME = "report.json"
"""单次 run 执行期 report 文件名（位于 ``results/{run_id}/``）。"""

CONTAINER_OUTCOME_ROOT = "/workspace/outcome"
"""容器内 agent workspace 根路径。"""

CONTAINER_BENCHMARKS_ROOT = "/workspace/benchmarks"
"""容器内 benchmark suite JSON 根路径。"""

BENCHMARK_MDS_DIR = PROJECT_ROOT / "assets" / "benchmark_mds"
"""人类可读 benchmark markdown 根目录（preprocess 时从 TOS 下载，不纳入 git）。"""

BENCHMARKS_DIR = PROJECT_ROOT / "assets" / "benchmarks"
"""机器可读 suite JSON 目录（preprocess 生成，不纳入 git）。"""

BENCHMARKS_DEMO_DIR = PROJECT_ROOT / "assets" / "benchmarks_demo"
"""冒烟 / demo suite JSON 目录（如 hello.json，随仓库提供）。"""

BENCHMARK_MDS_TOS_BUCKET = "aml-fde-boe"
"""存放 ``benchmark_mds.zip`` 的 TOS bucket（BOE）。"""

BENCHMARK_MDS_TOS_OBJECT_KEY = "benchmark_mds.zip"
"""TOS 上 benchmark markdown 压缩包对象名。"""

BENCHMARK_MDS_HF_PATH_IN_REPO = "benchmark_mds.zip"
"""HuggingFace dataset 仓库内 benchmark markdown 压缩包路径。"""


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
