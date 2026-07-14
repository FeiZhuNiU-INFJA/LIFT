"""GenericAgent 容器启动与 workspace seed 钩子。

与 OpenClaw 的差异：

- **无 gateway**：GA 不需要 readiness check 与 published_ports。容器启动后 LIFT
  通过 ``docker exec`` 直接调起 ``agentmain.py --task``，无需等待端口就绪。
- **secret 烧入镜像**：``mykey.py`` 已在 build 期由 sed 渲染，运行期 ``docker run``
  不需要再注入模型 / Langfuse secret，仅注入 LIFT 自有标签 ``LIFT_EVAL_RUN_TAG``
  与 chat 路径上动态计算的 ``LIFT_GA_SESSION_ID``（通过 ``docker exec -e`` 注入）。
- **workspace seed**：复用与 OpenClaw 一致的"宿主机预拷贝 + 容器内同步 + ready
  marker"模式；GA baseline 当前不强制人设文件，目录可以仅有 README。
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
from src.lift.adapters.genericagent.container_exec import GenericAgentContainerContext
from src.models import SuiteTask
from src.paths import GENERICAGENT_WORKSPACE_SEED_DIR

_CONTAINER_PREFIX = "evolve-genericagent"
CONTAINER_WORKSPACE_SEED_DIR = "/opt/lift/workspace_seed"
WORKSPACE_READY_MARKER = ".lift-workspace-ready"


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
    """把 GA workspace seed 复制到宿主机 workspace 目录。"""
    source = seed_dir or GENERICAGENT_WORKSPACE_SEED_DIR
    workspace_dir.mkdir(parents=True, exist_ok=True)
    if not source.is_dir():
        # GA baseline 不强制 seed；目录缺失时只放一个 ready marker
        (workspace_dir / WORKSPACE_READY_MARKER).touch()
        return
    for entry in sorted(source.iterdir()):
        dest = workspace_dir / entry.name
        if entry.is_dir():
            shutil.copytree(entry, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(entry, dest)
    (workspace_dir / WORKSPACE_READY_MARKER).touch()
    LOGGER.info("Seeded GA eval workspace: %s <- %s", workspace_dir, source)


def _container_workspace_seed_shell() -> str:
    """容器内同步镜像 seed 内容（如果有）。"""
    return f"""
if [[ -d "{CONTAINER_WORKSPACE_SEED_DIR}" ]]; then
  cp -a "{CONTAINER_WORKSPACE_SEED_DIR}/." /workspace/task/ 2>/dev/null || true
fi
touch /workspace/task/{WORKSPACE_READY_MARKER} 2>/dev/null || true
""".strip()


async def _reclaim_volume_ownership(session: ContainerSession) -> None:
    """容器销毁前将 bind mount 目录 chown 回宿主机用户。"""
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
    """容器内同步镜像 seed（best-effort）。"""
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


def genericagent_context(session: ContainerSession) -> GenericAgentContainerContext:
    """从 ``ContainerSession`` 构造最小 GA 容器上下文。"""
    return GenericAgentContainerContext(container_name=session.container_name)


async def start_genericagent_container(
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
    """启动 GA 评测容器（无 gateway / readiness）。

    GA 镜像默认 ``ENTRYPOINT [/usr/bin/tini]`` + ``CMD ["tail", "-f", "/dev/null"]``，
    容器一直空转等 ``docker exec``。LIFT 在 chat 时通过 ``docker exec`` 启动
    ``agentmain.py --task <iodir> --nobg``。

    与 OpenClaw 的关键区别：

    - 不传 ``readiness_check``：GA 没有 HTTP gateway 可健康检查。
    - ``port_mappings`` 为空：GA 不暴露任何网络端口。
    - ``env_vars`` 仅注入 LIFT 自有标签 ``LIFT_EVAL_RUN_TAG``——其它 secret
      在 build 期已写进 ``mykey.py``，``docker exec`` 通过 ``Config.Env`` 继承。
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
        # langfuse_tracing_overlay 在每条 trace 写入 tags；run id 必须在容器启动时
        # 注入到 Config.Env，因为 GA 进程是后续 docker exec 启的子进程，会继承
        # 容器全局 env。session_id 则在每轮 chat 通过 docker exec -e 注入（每轮不同）
        "LIFT_EVAL_RUN_TAG": ctx.run_id,
    }
    # 宿主 ``.env`` 中的 ``LANGFUSE_BASE_URL=http://localhost:3888`` 会通过
    # ``env_file`` 被注入容器，但容器内 ``localhost`` 不通宿主 Langfuse。
    # Langfuse SDK v4 的 OTel span exporter 会读 ``LANGFUSE_BASE_URL`` env
    # 覆盖 ``Langfuse(host=...)`` 显式参数，导致 GA overlay 推的 span 全部
    # 打到 localhost 失败 → dashboard 缺 ``genericagent-plugin`` trace。
    # 这里在 ``env_vars`` 层把 host 段改写为容器内可达的 ``host.docker.internal``
    # （scheme / port / path 从 ``.env`` 原值继承），优先级高于 ``env_file``。
    # 同名 ``LANGFUSE_HOST`` 同步覆写（部分 SDK 走它）。
    ga_langfuse_host = rewrite_langfuse_base_url_for_container(
        os.environ.get("LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_HOST"),
    )
    if ga_langfuse_host:
        env_vars["LANGFUSE_BASE_URL"] = ga_langfuse_host
        env_vars["LANGFUSE_HOST"] = ga_langfuse_host

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
        entrypoint_cmd=[],  # 用镜像默认 ENTRYPOINT/CMD（tini + tail -f /dev/null）
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
