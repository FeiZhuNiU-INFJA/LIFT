"""Hermes 容器内 ``docker exec`` 上下文与 runner 参数解析。

与 OpenClaw / GenericAgent 不同，Hermes 通过容器内**常驻 runner 子进程**
（``hermes_runner.py``，stdin/stdout sentinel 协议）驱动，而不是每轮一次性
``docker exec``。本模块只负责：

- ``HermesContainerContext``：exec 目标容器名 + 构建期发现的 Hermes venv python /
  源码目录（见 ``agent-runtimes/hermes/install-in-image.sh`` 写入的
  ``/opt/evolve-eval/hermes-paths.env``）。
- ``read_hermes_paths``：容器启动后从该文件读回发现的路径。
- ``hermes_runner_params``：从 ``CONFIG`` 解析 work LLM 的 model / base_url /
  api_key / max_tokens（work/judge 共用同一 work LLM）。
"""

from __future__ import annotations

from dataclasses import dataclass

from src.config import CONFIG
from src.lift.adapters.container.exec import docker_exec_async

# 镜像内固定路径（见 agent-runtimes/hermes/Dockerfile / install-in-image.sh）。
HERMES_PATHS_ENV_FILE = "/opt/evolve-eval/hermes-paths.env"
HERMES_RUNNER_PATH = "/opt/evolve-eval/hermes_runner.py"
HERMES_HOME_DIR = "/opt/data"
HERMES_TASK_CWD = "/workspace/task"


@dataclass(frozen=True)
class HermesContainerContext:
    """Hermes 容器 exec 所需的最小上下文。

    Attributes:
        container_name: ``docker exec`` 目标容器名。
        venv_py: 容器内 Hermes venv 的 python 路径（跑 runner 用）。
        src_dir: 容器内 Hermes 源码目录（runner ``--hermes-agent-dir``）。
    """

    container_name: str
    venv_py: str
    src_dir: str


@dataclass(frozen=True)
class HermesRunnerParams:
    """一次 runner 启动所需的 work LLM 参数（work / judge 共用 work LLM）。"""

    model: str
    base_url: str
    api_key: str
    max_tokens: int


def _model_default() -> str:
    """Hermes ``--model``：优先 ``HERMES_MODEL_NAME``，否则取 ``MODEL_NAME`` 的 / 后缀。"""
    explicit = (CONFIG.hermes_model_name or "").strip()
    if explicit:
        return explicit
    model_name = (CONFIG.model or "").strip()
    if not model_name or model_name == "unknown":
        return ""
    return model_name.split("/", 1)[1] if "/" in model_name else model_name


def hermes_runner_params() -> HermesRunnerParams:
    """从 ``CONFIG`` 解析 runner 的 work LLM 参数。"""
    base_url = (CONFIG.hermes_api_url or "").strip() or (CONFIG.work_openai_base_url or "").strip()
    return HermesRunnerParams(
        model=_model_default(),
        base_url=base_url,
        api_key=(CONFIG.work_openai_api_key or "").strip(),
        max_tokens=CONFIG.max_tokens,
    )


async def read_hermes_paths(container_name: str) -> tuple[str, str]:
    """读回容器内 ``hermes-paths.env``，返回 ``(venv_py, src_dir)``。

    构建期 ``install-in-image.sh`` 已探测并写入这些路径；这里在容器启动后读回，
    避免把不稳定的 Hermes 镜像布局硬编码进 adapter。
    """
    text = await docker_exec_async(
        container_name,
        ["cat", HERMES_PATHS_ENV_FILE],
        label="read hermes-paths.env",
    )
    venv_py = ""
    src_dir = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("HERMES_VENV_PY="):
            venv_py = line.split("=", 1)[1].strip()
        elif line.startswith("HERMES_SRC_DIR="):
            src_dir = line.split("=", 1)[1].strip()
    if not venv_py:
        raise RuntimeError(
            f"HERMES_VENV_PY missing in {HERMES_PATHS_ENV_FILE} on {container_name}; "
            "check the Hermes image build (install-in-image.sh path discovery)."
        )
    return venv_py, src_dir
