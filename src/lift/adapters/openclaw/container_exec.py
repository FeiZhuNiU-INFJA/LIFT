"""OpenClaw 容器内 ``docker exec`` 封装（CLI 与 shell）。"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from src.lift.adapters.container.exec import docker_exec_async, docker_exec_sync
from src.lift.adapters.openclaw.container_env import container_runtime_env


@dataclass(frozen=True)
class OpenClawContainerContext:
    """OpenClaw 容器 exec 所需的最小上下文。

    Attributes:
        container_name: ``docker exec`` 目标容器名。
        gateway_token: ``OPENCLAW_GATEWAY_TOKEN`` 环境变量值。
        gateway_port: 宿主机上映射的 gateway 端口（metadata 用）。
    """

    container_name: str
    gateway_token: str
    gateway_port: int

    def _exec_env(self, extra_env: dict[str, str] | None = None) -> dict[str, str]:
        """OpenClaw CLI 所需的容器内环境变量。"""
        env = {
            "OPENCLAW_GATEWAY_TOKEN": self.gateway_token,
            **container_runtime_env(),
        }
        if extra_env:
            env.update(extra_env)
        return env


async def exec_openclaw_async(
    ctx: OpenClawContainerContext,
    args: list[str],
    *,
    extra_env: dict[str, str] | None = None,
) -> str:
    """异步 ``docker exec openclaw ...``，失败抛 ``RuntimeError``。"""
    return await docker_exec_async(
        ctx.container_name,
        ["openclaw", *args],
        env=ctx._exec_env(extra_env),
        label=f"openclaw {' '.join(args)}",
    )


def exec_openclaw_sync(
    ctx: OpenClawContainerContext,
    args: list[str],
    *,
    extra_env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """同步 ``docker exec openclaw ...``（用于 initialize 等阻塞场景）。"""
    return docker_exec_sync(
        ctx.container_name,
        ["openclaw", *args],
        env=ctx._exec_env(extra_env),
        check=check,
    )
