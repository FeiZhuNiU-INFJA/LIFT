"""OpenClaw 容器内 ``docker exec`` 封装（CLI 与 shell）。"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from src.lift.adapters.container.exec import docker_exec_async, docker_exec_sync


@dataclass(frozen=True)
class OpenClawContainerContext:
    """OpenClaw 容器 exec 所需的最小上下文。

    Attributes:
        container_name: ``docker exec`` 目标容器名。
        gateway_token: ``OPENCLAW_GATEWAY_TOKEN`` 环境变量值（仅记录用，
            实际注入由 ``docker run`` 阶段的 ``Config.Env`` 完成；exec 默认继承）。
        gateway_port: 宿主机上映射的 gateway 端口（metadata 用）。
    """

    container_name: str
    gateway_token: str
    gateway_port: int


async def exec_openclaw_async(
    ctx: OpenClawContainerContext,
    args: list[str],
    *,
    extra_env: dict[str, str] | None = None,
    timeout_seconds: float | None = None,
) -> str:
    """异步 ``docker exec openclaw ...``，失败抛 ``RuntimeError``。

    OpenClaw CLI 所需的 ``OPENCLAW_GATEWAY_TOKEN`` / ``LANGFUSE_*`` / ``WORK_OPENAI_API_KEY``
    等环境变量在 ``docker run`` 阶段已经写入 ``Config.Env``，``docker exec`` 默认
    继承——因此这里不再传 ``-e``，避免 secret 在命令行/日志中重复出现。
    ``extra_env`` 仅用于偶发的运行时附加变量。
    ``timeout_seconds`` 透传给底层 ``docker_exec_async``，仅 chat 类长调用使用。
    """
    return await docker_exec_async(
        ctx.container_name,
        ["openclaw", *args],
        env=extra_env,
        label=f"openclaw {' '.join(args)}",
        timeout_seconds=timeout_seconds,
    )


def exec_openclaw_sync(
    ctx: OpenClawContainerContext,
    args: list[str],
    *,
    extra_env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """同步 ``docker exec openclaw ...``（用于 initialize 等阻塞场景）。

    详见 ``exec_openclaw_async`` 的说明：env 默认继承容器 ``Config.Env``。
    """
    return docker_exec_sync(
        ctx.container_name,
        ["openclaw", *args],
        env=extra_env,
        check=check,
    )

