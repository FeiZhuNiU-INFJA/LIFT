"""仓库目录布局：评测报告、agent workspace 与容器内路径常量。"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""仓库根目录（``evolve_eval``）。"""

OPENCLAW_AGENT_DIR = PROJECT_ROOT / "agent-runtimes" / "openclaw"
"""OpenClaw 镜像与容器配置目录（``agent-runtimes/openclaw/``）。"""

OPENCLAW_BASE_DOCKER_IMAGE = "lift-openclaw-base:latest"
"""不带 self-evolving-plugin-pro 进化插件的基础 OpenClaw 镜像（``OpenClawAdapter`` 使用；
``INSTALL_SELF_EVOLVING=false bash build-image.sh`` 构建）。"""

OPENCLAW_WITH_EVOLVE_DOCKER_IMAGE = "lift-openclaw-with-evolve:latest"
"""带 self-evolving-plugin-pro 进化插件的 OpenClaw 镜像（``OpenClawWithEvolveAdapter`` 使用；
``build-image.sh`` 默认 tag，对应 ``INSTALL_SELF_EVOLVING=true``）。"""

OPENCLAW_WITH_OPENSPACE_DOCKER_IMAGE = "lift-openclaw-with-openspace:latest"
"""带 OpenSpace MCP 插件的 OpenClaw 镜像（``OpenClawWithOpenSpaceAdapter`` 使用；
``build-image.sh --with-openspace`` 构建，对应 ``INSTALL_OPENSPACE=true``）。

注意：``--with-openspace`` 与 ``--with-evolve`` 互斥（两种进化插件二选一），
因此不存在 ``lift-openclaw-with-evolve-openspace`` 这样的叠加镜像。"""

OPENCLAW_WITH_AGENTMEMORY_DOCKER_IMAGE = "lift-openclaw-with-agentmemory:latest"
"""带 agentmemory memory plugin 的 OpenClaw 镜像（``OpenClawWithAgentMemoryAdapter`` 使用；
``build-image.sh --with-agentmemory`` 构建，对应 ``INSTALL_AGENTMEMORY=true``）。

agentmemory server（:3111，纯本地 all-MiniLM-L6-v2 嵌入 + BM25，离线）在容器内随
prelaunch 脚本后台启动；``--with-agentmemory`` 与 ``--with-evolve`` / ``--with-openspace``
三方互斥。该变体强制 bridge 网络（见 ``force_bridge_network``），避免并发容器抢 :3111。"""

GENERICAGENT_AGENT_DIR = PROJECT_ROOT / "agent-runtimes" / "genericagent"
"""GenericAgent 镜像与容器配置目录（``agent-runtimes/genericagent/``）。"""

GENERICAGENT_DOCKER_IMAGE = "lift-genericagent:latest"
"""GenericAgent baseline 镜像（``GenericAgentAdapter`` 使用；
``agent-runtimes/genericagent/build-image.sh`` 构建）。"""

GENERICAGENT_WORKSPACE_SEED_DIR = GENERICAGENT_AGENT_DIR / "workspace_seed"
"""宿主机 GenericAgent eval workspace seed 源目录。"""

HERMES_AGENT_DIR = PROJECT_ROOT / "agent-runtimes" / "hermes"
"""Hermes 镜像与容器配置目录（``agent-runtimes/hermes/``）。"""

HERMES_DOCKER_IMAGE = "lift-hermes:latest"
"""Hermes 评测镜像（``HermesAdapter`` 使用；``agent-runtimes/hermes/build-image.sh`` 构建，
默认基于上游 ``nousresearch/hermes-agent:v2026.5.16``）。"""

HERMES_WITH_OPENSPACE_DOCKER_IMAGE = "lift-hermes-with-openspace:latest"
"""带 OpenSpace MCP 插件的 Hermes 镜像（``HermesWithOpenSpaceAdapter`` 使用；
``agent-runtimes/hermes/build-image.sh --with-openspace`` 构建，对应 ``INSTALL_OPENSPACE=true``）。"""

HERMES_WITH_AGENTMEMORY_DOCKER_IMAGE = "lift-hermes-with-agentmemory:latest"
"""带 agentmemory memory provider plugin 的 Hermes 镜像（``HermesWithAgentMemoryAdapter`` 使用；
``agent-runtimes/hermes/build-image.sh --with-agentmemory`` 构建，对应 ``INSTALL_AGENTMEMORY=true``）。

agentmemory server（:3111，离线本地嵌入）由 ``hermes-entrypoint.sh`` 后台启动；config.yaml
的 ``memory.provider`` 由 ``patch_hermes_config.py`` 置为 agentmemory。与 ``--with-openspace``
互斥。该变体强制 bridge 网络（``force_bridge_network``）。"""

HERMES_WORKSPACE_SEED_DIR = HERMES_AGENT_DIR / "workspace_seed"
"""宿主机 Hermes eval workspace seed 源目录（可选；默认 Hermes 状态 baked 在镜像内 /opt/hermes-state）。""" 

OPENHUMAN_AGENT_DIR = PROJECT_ROOT / "agent-runtimes" / "openhuman"
"""OpenHuman 镜像与容器配置目录（``agent-runtimes/openhuman/``）。"""

OPENHUMAN_DOCKER_IMAGE = "lift-openhuman:latest"
"""OpenHuman baseline 镜像（``OpenHumanAdapter`` 使用；
``agent-runtimes/openhuman/build-image.sh`` 构建）。"""

OPENHUMAN_WITH_AGENTMEMORY_DOCKER_IMAGE = "lift-openhuman-with-agentmemory:latest"
"""带 agentmemory backend 的 OpenHuman 镜像（``OpenHumanWithAgentMemoryAdapter`` 使用；
``agent-runtimes/openhuman/build-image.sh --with-agentmemory`` 构建，对应 ``INSTALL_AGENTMEMORY=true``）。

config.toml 的 ``[memory] backend = "agentmemory"`` 让 openhuman-core 旁路自家 SQLite，
把记忆 trait 调用代理到容器内的 agentmemory server（:3111，离线本地嵌入）；server 由
``openhuman-agentmemory-entrypoint.sh`` 在 openhuman-core 启动前拉起。强制 bridge 网络
（``force_bridge_network``）。"""

OPENHUMAN_WORKSPACE_SEED_DIR = OPENHUMAN_AGENT_DIR / "workspace_seed"
"""宿主机 OpenHuman eval workspace seed 源目录。"""

EVOSCIENTIST_AGENT_DIR = PROJECT_ROOT / "agent-runtimes" / "evoscientist"
"""EvoScientist 镜像与容器配置目录（``agent-runtimes/evoscientist/``）。"""

EVOSCIENTIST_DOCKER_IMAGE = "lift-evoscientist:latest"
"""EvoScientist baseline 镜像（``EvoScientistAdapter`` 使用；
``agent-runtimes/evoscientist/build-image.sh`` 构建，基于 ``ghcr.io/evoscientist/evoscientist:latest`` 叠 overlay）。"""

EVOSCIENTIST_WORKSPACE_SEED_DIR = EVOSCIENTIST_AGENT_DIR / "workspace_seed"
"""宿主机 EvoScientist eval workspace seed 源目录。"""

PRIME_AGENT_AGENT_DIR = PROJECT_ROOT / "agent-runtimes" / "prime_agent"
"""Prime Agent 镜像与容器配置目录（``agent-runtimes/prime_agent/``；镜像尚待构建）。"""

PRIME_AGENT_DOCKER_IMAGE = "lift-prime-agent:latest"
"""Prime Agent runtime 镜像（``PrimeAgentAdapter`` 使用）。

内含 ``@earendil-works/pi-coding-agent``（bin ``pi``）+ LIFT overlay。构建脚本
``agent-runtimes/prime_agent/build-image.sh`` 尚待补齐（见 adapter 顶部 TODO）。
镜像内约定：Continual Harness / skills / sessions 全部落在
``PRIME_AGENT_STATE_DIR``（=容器内 ``PRIME_AGENT_CODING_AGENT_DIR``），使
``docker commit`` 能整体捕获 global harness。"""

PRIME_AGENT_STATE_DIR = "/root/.prime/agent"
"""容器内 Prime Agent 状态根目录（``getAgentDir()`` 默认值，显式钉死方便 commit）。

global harness 落在 ``{PRIME_AGENT_STATE_DIR}/harness/harness_state.json`` +
``refinements.jsonl``；skills/prompts/agents/sessions 亦在此目录下。启动容器时通过
env ``PRIME_AGENT_CODING_AGENT_DIR={PRIME_AGENT_STATE_DIR}`` 固定，避免 XDG 漂移。"""

PRIME_AGENT_WORKSPACE_SEED_DIR = PRIME_AGENT_AGENT_DIR / "workspace_seed"
"""宿主机 Prime Agent eval workspace seed 源目录。"""

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

DEFAULT_BENCHMARK_HF_REPO = "FeiZhuNiU-INFJA/EALE"
"""HuggingFace dataset 仓库默认 id（公开仓库，``BENCHMARK_HF_REPO`` 可覆盖）。"""

DEFAULT_BENCHMARK_MODELSCOPE_REPO = "Evolvon/EALE"
"""ModelScope dataset 仓库默认 id（``BENCHMARK_MODELSCOPE_REPO`` 可覆盖）。"""


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
