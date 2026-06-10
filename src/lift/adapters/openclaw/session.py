"""OpenClaw gateway 容器启动：端口分配、volume、readiness 与 workspace seed。"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from pathlib import Path

from src.config import LOGGER
from src.lift.adapters.base import SuiteRunContext
from src.lift.adapters.container.session import ContainerSession
from src.lift.adapters.container.volumes import (
    default_volume_binds,
    task_volume_binds,
)
from src.lift.adapters.openclaw.container_env import (
    container_reclaim_ownership_script,
    container_runtime_env,
    host_user_ids,
)
from src.lift.adapters.container.exec import docker_exec_shell_async
from src.lift.adapters.openclaw.container_exec import OpenClawContainerContext
from src.lift.adapters.openclaw.workspace_seed import (
    container_workspace_seed_shell,
    seed_eval_workspace,
)
from src.models import SuiteTask

_BASE_GATEWAY_PORT = 18789  # 宿主机 gateway 端口起始
_BASE_FASTAPI_PORT = 18090  # 宿主机 FastAPI 端口起始
_PORT_STEP = 20  # 每 slot 端口步进，避免冲突
_CONTAINER_PREFIX = "evolve-openclaw"  # docker 容器名前缀


def _instance_ports(instance_key: str) -> tuple[int, int]:
    """按 instance_key hash 分配宿主机 gateway/fastapi 端口对。"""
    digest = hashlib.sha256(instance_key.encode()).hexdigest()
    slot = int(digest[:8], 16) % 500
    return (
        _BASE_GATEWAY_PORT + slot * _PORT_STEP,
        _BASE_FASTAPI_PORT + slot * _PORT_STEP,
    )


async def _wait_gateway(session: ContainerSession, tries: int = 90) -> None:
    """轮询 curl gateway health，超时仅 warning 不抛错。"""
    gateway_port = int(session.metadata["gateway_port"])
    for _ in range(tries):
        for path in ("/", "/health"):
            proc = await asyncio.create_subprocess_exec(
                "curl",
                "-sf",
                f"http://127.0.0.1:{gateway_port}{path}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            if proc.returncode == 0:
                return
        await asyncio.sleep(1)
    LOGGER.warning("Gateway health check timed out for %s", session.container_name)


async def _reclaim_volume_ownership(session: ContainerSession) -> None:
    """容器销毁前将 bind mount 目录 chown 回宿主机用户。"""
    await asyncio.sleep(2)
    uid, gid = host_user_ids()
    try:
        await docker_exec_shell_async(
            session.container_name,
            container_reclaim_ownership_script(uid, gid),
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning(
            "Failed to reclaim workspace ownership for %s: %s",
            session.container_name,
            exc,
        )


async def _reset_workspace_attestations(session: ContainerSession) -> None:
    """清除 OpenClaw workspace attestations，避免跨题状态污染。"""
    await docker_exec_shell_async(
        session.container_name,
        "rm -rf \"${OPENCLAW_STATE_DIR:-/root/.openclaw}\"/workspace-attestations 2>/dev/null || true",
        extra_env=container_runtime_env(),
    )


async def _ensure_workspace_seed(session: ContainerSession) -> None:
    """容器内同步镜像内 workspace seed 并移除 BOOTSTRAP。"""
    try:
        await docker_exec_shell_async(
            session.container_name,
            container_workspace_seed_shell(),
            extra_env=container_runtime_env(),
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning(
            "Failed to apply workspace seed in %s: %s",
            session.container_name,
            exc,
        )


def openclaw_context(session: ContainerSession) -> OpenClawContainerContext:
    """从 ``ContainerSession.metadata`` 构造 ``OpenClawContainerContext``。"""
    return OpenClawContainerContext(
        container_name=session.container_name,
        gateway_token=str(session.metadata["gateway_token"]),
        gateway_port=int(session.metadata["gateway_port"]),
    )


async def start_openclaw_container(
    *,
    instance_id: str,
    image: str,
    ctx: SuiteRunContext,
    workspace_dir: Path | None = None,
    seed_workspace: bool = False,
    task: SuiteTask | None = None,
) -> ContainerSession:
    """启动 OpenClaw gateway 容器：端口、token、volume、readiness 与 seed 钩子。

    ``seed_workspace``: 为 ``True`` 时调用 ``seed_eval_workspace`` 并执行容器内 seed
    shell，使 hold-out 工作区带固定人设、无 ``BOOTSTRAP.md``。
    """
    gateway_port, fastapi_port = _instance_ports(instance_id)
    token = secrets.token_hex(32)

    binds = default_volume_binds(
        run_id=ctx.run_id,
        repeat_index=ctx.repeat_index,
    )
    if workspace_dir is not None:
        if seed_workspace:
            seed_eval_workspace(workspace_dir)
        binds.append((str(workspace_dir.resolve()), "/workspace/task", "rw"))
    if task is not None:
        binds.extend(task_volume_binds(task))

    env_vars = {
        "OPENCLAW_GATEWAY_TOKEN": token,
        "EVOBENCH_EVAL_RUN_TAG": ctx.run_id,
        **container_runtime_env(),
    }

    post_start_hooks: list = []
    if workspace_dir is not None:
        post_start_hooks.append(_reset_workspace_attestations)
        if seed_workspace:
            post_start_hooks.append(_ensure_workspace_seed)

    return await ContainerSession.start(
        instance_id=instance_id,
        container_name_prefix=_CONTAINER_PREFIX,
        image=image,
        entrypoint_cmd=["openclaw", "gateway", "run", "--bind", "lan"],
        port_mappings=[
            (gateway_port, 18789),
            (fastapi_port, 18090),
        ],
        env_vars=env_vars,
        volume_binds=binds,
        env_file=Path.cwd() / ".env",
        readiness_check=_wait_gateway,
        post_start_hooks=post_start_hooks,
        pre_cleanup_hooks=[_reclaim_volume_ownership],
        metadata={
            "gateway_token": token,
            "gateway_port": gateway_port,
            "fastapi_port": fastapi_port,
        },
    )
