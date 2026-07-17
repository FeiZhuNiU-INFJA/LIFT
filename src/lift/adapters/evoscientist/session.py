"""EvoScientist 容器启动与 workspace seed 钩子。

设计与 GenericAgent 对齐：

- 无 gateway / 无端口暴露：EvoScientist CLI (``EvoSci``) 是 stdin/stdout
  单发模型，chat 通过 ``docker exec`` + ``--output-format stream-json`` 直
  接跟 stdout 拿 JSONL 事件流。
- Secret bake：Work LLM + Langfuse 凭据在 build 期已渲染进
  ``/root/.config/evoscientist/config.yaml``；容器只额外注入 LIFT 自有标签
  ``LIFT_EVAL_RUN_TAG``，chat 时通过 ``docker exec -e`` 补上每轮的
  ``LIFT_EVOSCI_SESSION_ID``。
- workspace seed：与 GA 一致，宿主机预拷贝 + 容器内 tar 同步 + ready marker。
- delta 关键路径：``/root/.evoscientist``（sessions.db + memories/ + skills/）
  在 warmup 期由 EvoScientist 自然写入，``docker commit`` 时会一并带上，
  与 ``EvoScientistAdapter.evolve_paths`` 白名单一致。
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

from src.config import LOGGER
from src.lift.adapters.base import SuiteRunContext
from src.lift.adapters.container.exec import docker_exec_shell_async
from src.lift.adapters.container.langfuse import (
    rewrite_langfuse_base_url_for_container,
)
from src.lift.adapters.container.session import ContainerSession
from src.lift.adapters.container.volumes import (
    default_volume_binds,
    task_volume_binds,
)
from src.lift.adapters.evoscientist.container_exec import EvoScientistContainerContext
from src.models import SuiteTask
from src.paths import EVOSCIENTIST_WORKSPACE_SEED_DIR

_CONTAINER_PREFIX = "evolve-evoscientist"
CONTAINER_WORKSPACE_SEED_DIR = "/opt/lift/workspace_seed"
WORKSPACE_READY_MARKER = ".lift-workspace-ready"


def _container_reclaim_ownership_script(uid: int, gid: int) -> str:
    """容器 cleanup 前把 bind-mount 目录 chown 回宿主用户（root 执行）。"""
    return f"""
for d in /workspace/task /workspace/outcome; do
  if [[ -d "$d" ]]; then
    chown -R {uid}:{gid} "$d" 2>/dev/null || true
  fi
done
""".strip()


def seed_eval_workspace(workspace_dir: Path, *, seed_dir: Path | None = None) -> None:
    """把 EvoScientist workspace seed 复制到宿主 workspace 目录。"""
    source = seed_dir or EVOSCIENTIST_WORKSPACE_SEED_DIR
    workspace_dir.mkdir(parents=True, exist_ok=True)
    if not source.is_dir():
        # baseline 不强制 seed；空目录只放 ready marker
        (workspace_dir / WORKSPACE_READY_MARKER).touch()
        return
    for entry in sorted(source.iterdir()):
        dest = workspace_dir / entry.name
        if entry.is_dir():
            shutil.copytree(entry, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(entry, dest)
    (workspace_dir / WORKSPACE_READY_MARKER).touch()
    LOGGER.info("Seeded EvoScientist eval workspace: %s <- %s", workspace_dir, source)


def _container_workspace_seed_shell() -> str:
    """容器内把镜像 seed 目录（如果有）同步进 ``/workspace/task``。"""
    return f"""
if [[ -d "{CONTAINER_WORKSPACE_SEED_DIR}" ]]; then
  cp -a "{CONTAINER_WORKSPACE_SEED_DIR}/." /workspace/task/ 2>/dev/null || true
fi
touch /workspace/task/{WORKSPACE_READY_MARKER} 2>/dev/null || true
""".strip()


async def _reclaim_volume_ownership(session: ContainerSession) -> None:
    """容器销毁前 chown 回宿主用户。"""
    await asyncio.sleep(2)
    uid, gid = os.getuid(), os.getgid()
    try:
        await docker_exec_shell_async(
            session.container_name,
            _container_reclaim_ownership_script(uid, gid),
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning(
            "Failed to reclaim workspace ownership for %s: %s",
            session.container_name, exc,
        )


async def _ensure_workspace_seed(session: ContainerSession) -> None:
    """容器启动后同步镜像 seed（best-effort）。"""
    try:
        await docker_exec_shell_async(
            session.container_name,
            _container_workspace_seed_shell(),
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning(
            "Failed to apply workspace seed in %s: %s",
            session.container_name, exc,
        )


def evoscientist_context(session: ContainerSession) -> EvoScientistContainerContext:
    """从 ``ContainerSession`` 构造 EvoScientist 容器上下文。"""
    return EvoScientistContainerContext(container_name=session.container_name)


async def start_evoscientist_container(
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
    """启动 EvoScientist 评测容器。

    与 GA 类似：容器空转（tini + ``tail -f /dev/null``），LIFT 每轮 chat 通过
    ``docker exec ... EvoSci -p "..." --output-format stream-json`` 触发单发。
    """
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
        # 每条 trace 附着的批次标签（同 GA / OpenClaw 约定）
        "LIFT_EVAL_RUN_TAG": ctx.run_id,
    }
    # 与 GA 同理：把 LANGFUSE_BASE_URL / LANGFUSE_HOST 里的 loopback 改写为
    # host.docker.internal，避免容器 loopback 打不到宿主 Langfuse。
    ev_langfuse_host = rewrite_langfuse_base_url_for_container(
        os.environ.get("LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_HOST"),
    )
    if ev_langfuse_host:
        env_vars["LANGFUSE_BASE_URL"] = ev_langfuse_host
        env_vars["LANGFUSE_HOST"] = ev_langfuse_host

    extra_docker_args: list[str] = []
    if container_memory:
        extra_docker_args.extend(["--memory", container_memory])
    if container_cpus:
        extra_docker_args.extend(["--cpus", container_cpus])

    post_start_hooks: list = []
    if workspace_dir is not None and seed_workspace:
        post_start_hooks.append(_ensure_workspace_seed)

    return await ContainerSession.start(
        instance_id=instance_id,
        container_name_prefix=_CONTAINER_PREFIX,
        image=image,
        entrypoint_cmd=[],  # 用 Dockerfile 默认 tini + tail
        port_mappings=[],
        env_vars=env_vars,
        volume_binds=binds,
        env_file=Path.cwd() / ".env",
        extra_docker_args=extra_docker_args or None,
        readiness_check=None,
        post_start_hooks=post_start_hooks,
        pre_cleanup_hooks=[_reclaim_volume_ownership],
        metadata={},
        viz_repeat_index=ctx.repeat_index,
        viz_suite_name=ctx.suite_name,
    )
