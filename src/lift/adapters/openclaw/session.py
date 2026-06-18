"""OpenClaw gateway 容器启动：端口分配、volume、readiness、workspace seed 与运行时 env。"""

from __future__ import annotations

import asyncio
import os
import secrets
import shutil
from pathlib import Path

from src.config import LOGGER
from src.lift.adapters.base import SuiteRunContext
from src.lift.adapters.container.exec import docker_exec_shell_async
from src.lift.adapters.container.session import ContainerSession
from src.lift.adapters.container.volumes import (
    default_volume_binds,
    task_volume_binds,
)
from src.lift.adapters.openclaw.container_exec import OpenClawContainerContext
from src.models import SuiteTask
from src.paths import OPENCLAW_WORKSPACE_SEED_DIR

_GATEWAY_CONTAINER_PORT = 18789  # 容器内 gateway 端口（agent --local 连此）
_FASTAPI_CONTAINER_PORT = 18090  # 容器内 self-evolving plugin HTTP 端口
_CONTAINER_PREFIX = "evolve-openclaw"  # docker 容器名前缀

CONTAINER_LANGFUSE_BASE_URL = "http://host.docker.internal:3000"  # 容器内访问宿主机 Langfuse
CONTAINER_WORKSPACE_SEED_DIR = "/opt/evolve-eval/workspace_seed"  # 镜像内 seed 路径
WORKSPACE_READY_MARKER = ".lift-workspace-ready"  # seed 完成标记文件


def _normalize_langfuse_base_url(raw: str | None) -> str:
    """将 localhost Langfuse URL 映射为 ``host.docker.internal``。"""
    if not raw or not raw.strip():
        return CONTAINER_LANGFUSE_BASE_URL
    lowered = raw.strip().lower()
    if "127.0.0.1" in lowered or "localhost" in lowered:
        return CONTAINER_LANGFUSE_BASE_URL
    return raw.strip()


def _container_runtime_env() -> dict[str, str]:
    """``docker run`` 时需要相对宿主机 ``.env`` **改写**的环境变量。

    其它 secret（``ARK_API_KEY`` / ``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY`` /
    ``FIRECRAWL_API_KEY`` 等）一律走 ``--env-file``，不在这里返回——避免 secret 重复
    出现在 ``docker run -e ...`` 命令行与日志里；这些值已经写入容器 ``Config.Env``，
    后续 ``docker exec`` 会自动继承，无需再次注入。
    """
    return {
        # 容器内 host.docker.internal 访问宿主机 Langfuse；宿主机 .env 通常配 localhost
        "LANGFUSE_BASE_URL": _normalize_langfuse_base_url(
            os.environ.get("LANGFUSE_BASE_URL")
        ),
    }


def _container_reclaim_ownership_script(uid: int, gid: int) -> str:
    """在容器内以 root 执行，将 volume 目录 chown 回宿主机用户。"""
    return f"""
for d in /workspace/task /workspace/outcome; do
  if [[ -d "$d" ]]; then
    chown -R {uid}:{gid} "$d" 2>/dev/null || true
  fi
done
""".strip()


def seed_eval_workspace(workspace_dir: Path, *, seed_dir: Path | None = None) -> None:
    """Copy eval workspace seed into a host workspace before Docker volume mount."""
    source = seed_dir or OPENCLAW_WORKSPACE_SEED_DIR
    if not source.is_dir():
        raise FileNotFoundError(f"OpenClaw workspace seed not found: {source}")

    workspace_dir.mkdir(parents=True, exist_ok=True)
    for entry in sorted(source.iterdir()):
        dest = workspace_dir / entry.name
        if entry.is_dir():
            shutil.copytree(entry, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(entry, dest)

    (workspace_dir / "BOOTSTRAP.md").unlink(missing_ok=True)
    (workspace_dir / WORKSPACE_READY_MARKER).touch()
    LOGGER.info("Seeded eval workspace: %s <- %s", workspace_dir, source)


def _container_workspace_seed_shell() -> str:
    """Run inside container after mount: sync image seed and drop BOOTSTRAP."""
    return f"""
if [[ -d "{CONTAINER_WORKSPACE_SEED_DIR}" ]]; then
  cp -a "{CONTAINER_WORKSPACE_SEED_DIR}/." /workspace/task/ 2>/dev/null || true
fi
rm -f /workspace/task/BOOTSTRAP.md 2>/dev/null || true
touch /workspace/task/{WORKSPACE_READY_MARKER} 2>/dev/null || true
""".strip()


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
    # 不抛错：部分环境 health 路径慢；后续 docker exec 失败会再暴露
    LOGGER.warning("Gateway health check timed out for %s", session.container_name)


async def _reclaim_volume_ownership(session: ContainerSession) -> None:
    """容器销毁前将 bind mount 目录 chown 回宿主机用户。"""
    await asyncio.sleep(2)  # 等容器内进程释放 volume 文件句柄
    uid, gid = os.getuid(), os.getgid()
    try:
        await docker_exec_shell_async(
            session.container_name,
            _container_reclaim_ownership_script(uid, gid),
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
    )


async def _ensure_workspace_seed(session: ContainerSession) -> None:
    """容器内同步镜像内 workspace seed 并移除 BOOTSTRAP。"""
    try:
        await docker_exec_shell_async(
            session.container_name,
            _container_workspace_seed_shell(),
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


async def _resolve_gateway_port(session: ContainerSession) -> None:
    """readiness_check 前回填真实宿主机端口到 ``metadata['gateway_port']``。

    ``ContainerSession.start`` 启动后已通过 ``docker inspect`` 把端口映射写入
    ``session.published_ports``；这里将容器内 18789/18090 映射到的真实宿主机端口
    复制到 metadata，供 ``_wait_gateway`` 与 ``OpenClawContainerContext`` 使用。
    """
    session.metadata["gateway_port"] = session.published_ports[_GATEWAY_CONTAINER_PORT]
    session.metadata["fastapi_port"] = session.published_ports[_FASTAPI_CONTAINER_PORT]


async def start_openclaw_container(
    *,
    instance_id: str,
    image: str,
    ctx: SuiteRunContext,
    workspace_dir: Path | None = None,
    seed_workspace: bool = False,
    task: SuiteTask | None = None,
    container_memory: str | None = None,
    container_cpus: str | None = None,
) -> ContainerSession:
    """启动 OpenClaw gateway 容器：端口、token、volume、readiness 与 seed 钩子。

    ``seed_workspace``: 为 ``True`` 时调用 ``seed_eval_workspace`` 并执行容器内 seed
    shell，使 hold-out 工作区带固定人设、无 ``BOOTSTRAP.md``。

    ``container_memory`` / ``container_cpus``: 透传给 ``docker run --memory`` /
    ``--cpus`` 的单容器资源上限（None 表示不限制）。

    宿主机端口由 Docker 自动分配（避免确定性 hash 端口的碰撞与占用冲突）；启动后
    ``_resolve_gateway_port`` 把真实端口写回 ``metadata['gateway_port']``。
    """
    token = secrets.token_hex(32)

    binds = default_volume_binds(
        run_id=ctx.run_id,
        repeat_index=ctx.repeat_index,
    )
    if workspace_dir is not None:
        if seed_workspace:
            seed_eval_workspace(workspace_dir)  # 宿主机侧复制 IDENTITY/USER/SOUL
        binds.append((str(workspace_dir.resolve()), "/workspace/task", "rw"))
    if task is not None:
        binds.extend(task_volume_binds(task))

    env_vars = {
        "OPENCLAW_GATEWAY_TOKEN": token,
        "LIFT_EVAL_RUN_TAG": ctx.run_id,  # langfuse-tracer 写入 trace tags，对齐 pre-chat run
        **_container_runtime_env(),
    }

    # 单容器资源上限：防止单容器吃光 VM 内存触发整机卡死
    extra_docker_args: list[str] = []
    if container_memory:
        extra_docker_args.extend(["--memory", container_memory])
    if container_cpus:
        extra_docker_args.extend(["--cpus", container_cpus])

    post_start_hooks: list = []
    if workspace_dir is not None:
        post_start_hooks.append(_reset_workspace_attestations)  # 清跨题 attestations 状态
        if seed_workspace:
            post_start_hooks.append(_ensure_workspace_seed)  # 容器内删 BOOTSTRAP、同步 seed

    return await ContainerSession.start(
        instance_id=instance_id,
        container_name_prefix=_CONTAINER_PREFIX,
        image=image,
        entrypoint_cmd=["openclaw", "gateway", "run", "--bind", "lan"],
        port_mappings=[
            (None, _GATEWAY_CONTAINER_PORT),  # docker 自选宿主机端口
            (None, _FASTAPI_CONTAINER_PORT),
        ],
        env_vars=env_vars,
        volume_binds=binds,
        env_file=Path.cwd() / ".env",
        extra_docker_args=extra_docker_args or None,
        readiness_check=_check_gateway_with_resolved_port,
        post_start_hooks=post_start_hooks,
        pre_cleanup_hooks=[_reclaim_volume_ownership],
        metadata={
            "gateway_token": token,
        },
        viz_repeat_index=ctx.repeat_index,
        viz_suite_name=ctx.suite_name,
    )


async def _check_gateway_with_resolved_port(session: ContainerSession) -> None:
    """先回填真实端口，再做 gateway 健康检查。

    ``ContainerSession.start`` 在调 readiness_check 前已 inspect 出真实端口，
    这里把端口写回 metadata 后调用 ``_wait_gateway``（依赖 metadata['gateway_port']）。
    """
    await _resolve_gateway_port(session)
    await _wait_gateway(session)
