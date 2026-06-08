from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass

from src_new.config import LOGGER
from src_new.lift.adapters.openclaw.container_env import container_runtime_env


@dataclass(frozen=True)
class OpenClawContainerContext:
    container_name: str
    gateway_token: str
    gateway_port: int

    def exec_prefix(self, extra_env: dict[str, str] | None = None) -> list[str]:
        cmd = [
            "docker",
            "exec",
            "-e",
            f"OPENCLAW_GATEWAY_TOKEN={self.gateway_token}",
        ]
        for key, val in container_runtime_env().items():
            cmd.extend(["-e", f"{key}={val}"])
        if extra_env:
            for key, val in extra_env.items():
                cmd.extend(["-e", f"{key}={val}"])
        cmd.append(self.container_name)
        return cmd

    def wrap_openclaw(self, args: list[str], extra_env: dict[str, str] | None = None) -> list[str]:
        return [*self.exec_prefix(extra_env), "openclaw", *args]


async def exec_openclaw_async(
    ctx: OpenClawContainerContext,
    args: list[str],
    *,
    extra_env: dict[str, str] | None = None,
) -> str:
    cmd = ctx.wrap_openclaw(args, extra_env=extra_env)
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
        raise RuntimeError(
            f"docker exec openclaw failed ({' '.join(args)}): {stderr_text or stdout_text}"
        )
    return stdout_text


async def exec_shell_async(
    container_name: str,
    script: str,
    *,
    extra_env: dict[str, str] | None = None,
) -> str:
    cmd = ["docker", "exec"]
    if extra_env:
        for key, val in extra_env.items():
            cmd.extend(["-e", f"{key}={val}"])
    cmd.extend([container_name, "bash", "-lc", script])
    LOGGER.info("Container shell: %s", script[:120])
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    stdout_text = stdout.decode("utf-8", errors="replace")
    stderr_text = stderr.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(
            f"docker exec shell failed: {stderr_text or stdout_text}"
        )
    return stdout_text


def exec_openclaw_sync(
    ctx: OpenClawContainerContext,
    args: list[str],
    *,
    extra_env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    cmd = ctx.wrap_openclaw(args, extra_env=extra_env)
    LOGGER.info("Container exec (sync): %s", " ".join(cmd))
    return subprocess.run(cmd, check=check, capture_output=True, text=True)
