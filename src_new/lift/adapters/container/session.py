"""参数化 ``docker run`` 容器会话与 Disposable 生命周期。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, override

from src_new.config import LOGGER
from src_new.lift.runtime.disposable import Disposable
from src_new.lift.runtime.environment_cleaner import EnvironmentCleaner

ContainerHook = Callable[["ContainerSession"], Awaitable[None]]  # 容器启动/销毁前后钩子


def sanitize_container_id(value: str) -> str:
    """将 instance id sanitize 为 Docker 容器名合法字符（最长 64）。"""
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
    """容器 runtime adapter 使用的 ephemeral Docker 容器会话。

    Attributes:
        instance_id: sanitize 后的逻辑实例 id。
        container_name: ``docker run --name`` 实际容器名。
        image: 启动时使用的镜像 tag。
        metadata: 运行时附加信息（如 OpenClaw gateway 端口/token）。
    """

    instance_id: str
    container_name: str
    image: str
    metadata: dict[str, Any] = field(default_factory=dict)
    _cleaner: EnvironmentCleaner = field(default_factory=EnvironmentCleaner)  # 容器/镜像清理
    _pre_cleanup_hooks: list[ContainerHook] = field(default_factory=list, repr=False)  # rm 前钩子
    _cleaned: bool = field(default=False, repr=False)  # 幂等 cleanup 标记

    @override
    async def cleanup(self) -> None:
        """执行 pre-cleanup 钩子后 ``docker rm -f`` 删除容器。"""
        if self._cleaned:
            return
        for hook in self._pre_cleanup_hooks:
            try:
                await hook(self)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning(
                    "Container pre-cleanup hook failed for %s: %s",
                    self.container_name,
                    exc,
                )
        await self._cleaner.remove_container(self.container_name)
        self._cleaned = True

    @classmethod
    async def start(
        cls,
        *,
        instance_id: str,
        container_name_prefix: str,
        image: str,
        entrypoint_cmd: list[str],
        port_mappings: list[tuple[int, int]],
        env_vars: dict[str, str],
        volume_binds: list[tuple[str, str, str]],
        env_file: Path | None = None,
        extra_docker_args: list[str] | None = None,
        readiness_check: ContainerHook | None = None,
        post_start_hooks: list[ContainerHook] | None = None,
        pre_cleanup_hooks: list[ContainerHook] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ContainerSession:
        """组装 ``docker run -d`` 命令并启动容器，可选 readiness 与 post-start 钩子。"""
        safe_id = sanitize_container_id(instance_id)
        container_name = f"{container_name_prefix}-{safe_id}"[:128]

        await EnvironmentCleaner().remove_container(container_name)

        cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            container_name,
            "--add-host",
            "host.docker.internal:host-gateway",
            "-v",
            "/tmp:/tmp",
        ]
        if extra_docker_args:
            cmd.extend(extra_docker_args)
        for host_port, container_port in port_mappings:
            cmd.extend(["-p", f"{host_port}:{container_port}"])
        if env_file is not None and env_file.is_file():
            cmd.extend(["--env-file", str(env_file.resolve())])
        for key, val in env_vars.items():
            cmd.extend(["-e", f"{key}={val}"])
        for host_path, container_path, mode in volume_binds:
            cmd.extend(["-v", f"{host_path}:{container_path}:{mode}"])
        cmd.append(image)
        cmd.extend(entrypoint_cmd)

        LOGGER.info("Starting container session: %s", " ".join(cmd))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"docker run failed: {stderr.decode(errors='replace')}"
            )

        session = cls(
            instance_id=safe_id,
            container_name=container_name,
            image=image,
            metadata=dict(metadata or {}),
            _pre_cleanup_hooks=list(pre_cleanup_hooks or []),
        )
        if readiness_check is not None:
            await readiness_check(session)
        for hook in post_start_hooks or []:
            await hook(session)
        return session
