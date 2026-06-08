from __future__ import annotations

import asyncio
import hashlib
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from src_new.config import LOGGER
from src_new.hace.adapters.openclaw.container_env import (
    container_reclaim_ownership_script,
    container_runtime_env,
    host_user_ids,
)
from src_new.hace.adapters.openclaw.container_exec import (
    OpenClawContainerContext,
    exec_shell_async,
)
from src_new.hace.adapters.openclaw.material_mount import (
    default_volume_binds,
    resolve_host_path,
    task_volume_binds,
)
from src_new.hace.runtime.disposable import Disposable
from src_new.hace.runtime.environment_cleaner import EnvironmentCleaner
from src_new.models import SuiteTask

_BASE_GATEWAY_PORT = 18789
_BASE_FASTAPI_PORT = 18090
_PORT_STEP = 20
_CONTAINER_PREFIX = "evolve-openclaw"


def _instance_ports(instance_key: str) -> tuple[int, int]:
    digest = hashlib.sha256(instance_key.encode()).hexdigest()
    slot = int(digest[:8], 16) % 500
    return (
        _BASE_GATEWAY_PORT + slot * _PORT_STEP,
        _BASE_FASTAPI_PORT + slot * _PORT_STEP,
    )


def _sanitize_id(value: str) -> str:
    out = []
    for ch in value:
        if ch.isalnum() or ch in "._-":
            out.append(ch)
        else:
            out.append("-")
    result = "".join(out).strip("-")[:64]
    return result or "session"


@dataclass
class ContainerSession(Disposable):
    """One ephemeral OpenClaw Docker container."""

    instance_id: str
    container_name: str
    image: str
    gateway_port: int
    fastapi_port: int
    gateway_token: str
    workspace_dir: Path | None = None
    _cleaner: EnvironmentCleaner = field(default_factory=EnvironmentCleaner)
    _cleaned: bool = field(default=False, repr=False)

    @property
    def context(self) -> OpenClawContainerContext:
        return OpenClawContainerContext(
            container_name=self.container_name,
            gateway_token=self.gateway_token,
            gateway_port=self.gateway_port,
        )

    async def cleanup(self) -> None:
        if self._cleaned:
            return
        # Allow langfuse-tracer ingestion to finish before the gateway process exits.
        await asyncio.sleep(2)
        await self._reclaim_volume_ownership()
        await self._cleaner.remove_container(self.container_name)
        self._cleaned = True

    async def _reclaim_volume_ownership(self) -> None:
        """OpenClaw runs as root in-container; chown bind mounts back to the host user."""
        uid, gid = host_user_ids()
        try:
            await exec_shell_async(
                self.container_name,
                container_reclaim_ownership_script(uid, gid),
            )
        except Exception as exc:
            LOGGER.warning(
                "Failed to reclaim workspace ownership for %s: %s",
                self.container_name,
                exc,
            )

    @classmethod
    async def start(
        cls,
        *,
        instance_id: str,
        image: str,
        run_id: str,
        repeat_index: int,
        workspace_dir: Path | None = None,
        task: SuiteTask | None = None,
        extra_binds: list[tuple[str, str, str]] | None = None,
    ) -> ContainerSession:
        safe_id = _sanitize_id(instance_id)
        container_name = f"{_CONTAINER_PREFIX}-{safe_id}"[:128]
        gateway_port, fastapi_port = _instance_ports(instance_id)
        token = secrets.token_hex(32)

        await EnvironmentCleaner().remove_container(container_name)

        binds = default_volume_binds(run_id=run_id, repeat_index=repeat_index)
        if workspace_dir is not None:
            workspace_dir.mkdir(parents=True, exist_ok=True)
            binds.append((str(workspace_dir.resolve()), "/workspace/task", "rw"))
        if task is not None:
            binds.extend(task_volume_binds(task))
        if extra_binds:
            binds.extend(extra_binds)

        cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            container_name,
            "--add-host",
            "host.docker.internal:host-gateway",
            "-p",
            f"{gateway_port}:18789",
            "-p",
            f"{fastapi_port}:18090",
            "-v",
            "/tmp:/tmp",
            "-e",
            f"OPENCLAW_GATEWAY_TOKEN={token}",
            "-e",
            f"EVOBENCH_EVAL_RUN_TAG={run_id}",
        ]
        env_file = Path.cwd() / ".env"
        if env_file.is_file():
            cmd.extend(["--env-file", str(env_file.resolve())])
        for key, val in container_runtime_env().items():
            cmd.extend(["-e", f"{key}={val}"])
        for host_path, container_path, mode in binds:
            cmd.extend(["-v", f"{host_path}:{container_path}:{mode}"])
        cmd.append(image)
        cmd.extend(["openclaw", "gateway", "run", "--bind", "lan"])

        LOGGER.info("Starting container session: %s", " ".join(cmd))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"docker run failed: {stderr.decode(errors='replace')}"
            )

        session = cls(
            instance_id=safe_id,
            container_name=container_name,
            image=image,
            gateway_port=gateway_port,
            fastapi_port=fastapi_port,
            gateway_token=token,
            workspace_dir=workspace_dir,
        )
        await session._wait_gateway()
        if workspace_dir is not None:
            await session._reset_workspace_attestations()
        return session

    async def _wait_gateway(self, tries: int = 90) -> None:
        for _ in range(tries):
            proc = await asyncio.create_subprocess_exec(
                "curl",
                "-sf",
                f"http://127.0.0.1:{self.gateway_port}/",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            if proc.returncode == 0:
                return
            proc = await asyncio.create_subprocess_exec(
                "curl",
                "-sf",
                f"http://127.0.0.1:{self.gateway_port}/health",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            if proc.returncode == 0:
                return
            await asyncio.sleep(1)
        LOGGER.warning("Gateway health check timed out for %s", self.container_name)

    async def _reset_workspace_attestations(self) -> None:
        """Drop stale workspace attestations when mounting a fresh task workspace."""
        await exec_shell_async(
            self.container_name,
            "rm -rf \"${OPENCLAW_STATE_DIR:-/root/.openclaw}\"/workspace-attestations 2>/dev/null || true",
            extra_env=container_runtime_env(),
        )
