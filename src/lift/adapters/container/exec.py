"""容器内 ``docker exec`` 通用封装（供各 agent runtime 复用）。"""

from __future__ import annotations

import asyncio
import subprocess

from src.config import LOGGER


def build_docker_exec_argv(
    container_name: str,
    command: list[str],
    *,
    env: dict[str, str] | None = None,
) -> list[str]:
    """拼装 ``docker exec [-e KEY=VAL ...] CONTAINER COMMAND...`` argv。"""
    cmd: list[str] = ["docker", "exec"]
    if env:
        for key, val in env.items():
            cmd.extend(["-e", f"{key}={val}"])  # 逐对 -e，兼容任意 runtime CLI 环境
    cmd.append(container_name)
    cmd.extend(command)
    return cmd


async def docker_exec_async(
    container_name: str,
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    label: str | None = None,
) -> str:
    """异步 ``docker exec``，非零退出码抛 ``RuntimeError``，返回 stdout 文本。"""
    cmd = build_docker_exec_argv(container_name, command, env=env)
    LOGGER.info("Container exec: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    stdout_text = stdout.decode("utf-8", errors="replace")
    stderr_text = stderr.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        detail = stderr_text or stdout_text
        hint = label or " ".join(command)
        raise RuntimeError(
            f"docker exec failed for {container_name} ({hint}): {detail}"
        )
    return stdout_text


def docker_exec_sync(
    container_name: str,
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """同步 ``docker exec``（阻塞场景，如 initialize）。"""
    cmd = build_docker_exec_argv(container_name, command, env=env)
    LOGGER.info("Container exec (sync): %s", " ".join(cmd))
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


async def docker_exec_shell_async(
    container_name: str,
    script: str,
    *,
    env: dict[str, str] | None = None,
) -> str:
    """异步 ``docker exec ... bash -lc <script>``。"""
    LOGGER.info("Container shell: %s", script[:120])
    return await docker_exec_async(
        container_name,
        ["bash", "-lc", script],
        env=env,
        label=f"shell: {script[:120]}",
    )
