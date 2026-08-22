"""Prime Agent 容器启动与 workspace seed 钩子。

设计与 EvoScientist / GenericAgent 对齐：

- 无 gateway / 无端口暴露：``prime-agent`` 是本地 daemon-backed CLI，chat 通过
  ``docker exec`` + ``prime-agent --mode json "<msg>"``（JSON 事件流单发）触发，
  从 stdout 的 JSON Lines 拿回复 + session id（见 ``chat_agent.py``）。
- Secret bake：Work LLM 凭据在 build 期已渲染进容器内 ``prime-agent`` 配置
  （``models.json`` + ``settings.json``，见 ``agent-runtimes/prime_agent``）；
  容器只额外注入 LIFT 自有标签 ``LIFT_EVAL_RUN_TAG`` + 每轮的 session id。
- **状态目录钉死**：注入 ``PRIME_AGENT_CODING_AGENT_DIR=PRIME_AGENT_STATE_DIR``，
  强制 ``getAgentDir()`` 落到固定路径，避免 XDG / HOME 漂移，让 ``docker commit``
  能稳定捕获 global harness（``{state}/harness/harness_state.json``）。
- delta 关键路径：``PRIME_AGENT_STATE_DIR``（harness + skills + sessions）在
  warmup 期写入，``docker commit`` 时一并带上，与
  ``PrimeAgentAdapter.evolve_paths`` 白名单一致。

镜像（``agent-runtimes/prime_agent/``）：① 从 Prime Intellect R2 下载校验并
  ``npm install -g`` 真 ``prime-agent``；② 渲染 provider 配置（``models.json``
  自定义 OpenAI-compatible provider + ``settings.json`` 默认选型）；③ 设
  ``ENV PRIME_AGENT_CODING_AGENT_DIR``；④ 默认 entrypoint 空转（tini +
  ``tail -f /dev/null``），与 EvoScientist 一致。
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
from src.lift.adapters.prime_agent.container_exec import PrimeAgentContainerContext
from src.models import SuiteTask
from src.paths import PRIME_AGENT_STATE_DIR, PRIME_AGENT_WORKSPACE_SEED_DIR

_CONTAINER_PREFIX = "lift-prime-agent"
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
    """把 Prime Agent workspace seed 复制到宿主 workspace 目录。"""
    source = seed_dir or PRIME_AGENT_WORKSPACE_SEED_DIR
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
    LOGGER.info("Seeded Prime Agent eval workspace: %s <- %s", workspace_dir, source)


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


async def _clear_stale_daemon_state(session: ContainerSession) -> None:
    """容器启动后、首轮 chat 前清理 stale daemon 状态（best-effort）。

    Prime Agent 后台 daemon supervisor 启动时要「抢所有权」——把
    ``supervisor-owners/<uuid>.owner`` ``renameSync`` 成 ``.owner.stale-*``。
    ``docker commit`` 会把 warmup 容器里这些 **运行时状态** 一起固化进 delta 镜像；
    evolved 容器从 delta 启动时，这些目录落在只读镜像层，跨设备 ``rename`` 触发
    ``EXDEV: cross-device link not permitted``，daemon 起不来 → 首轮 chat 非零退出
    秒崩（baseline 用干净 base 镜像则无此问题）。

    entrypoint 是 ``tini + tail -f /dev/null``，首轮 ``prime-agent`` 调用前没有存活
    daemon 持锁，启动即清理是安全的；只清运行时瞬态，不动 ``harness/``（进化产物）。
    """
    state = PRIME_AGENT_STATE_DIR
    script = "\n".join(
        [
            f'rm -rf "{state}/../supervisor-owners" 2>/dev/null || true',
            f'rm -rf "{state}/supervisor-owners" 2>/dev/null || true',
            f'rm -rf "{state}/daemon-workers" 2>/dev/null || true',
            f'rm -rf "{state}/session-leases" 2>/dev/null || true',
            f'rm -f "{state}/logs/"*.sock* 2>/dev/null || true',
        ]
    )
    try:
        await docker_exec_shell_async(session.container_name, script)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning(
            "Failed to clear stale daemon state in %s: %s",
            session.container_name, exc,
        )


def prime_agent_context(session: ContainerSession) -> PrimeAgentContainerContext:
    """从 ``ContainerSession`` 构造 Prime Agent 容器上下文。"""
    return PrimeAgentContainerContext(container_name=session.container_name)


async def start_prime_agent_container(
    *,
    instance_id: str,
    image: str,
    ctx: SuiteRunContext,
    workspace_dir: Path | None = None,
    seed_workspace: bool = False,
    task: SuiteTask | None = None,
    container_memory: str | None = None,
    container_cpus: str | None = None,
    viz_role: str | None = None,
) -> ContainerSession:
    """启动 Prime Agent 评测容器（空转 + docker exec 单发 chat）。"""
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
        # 每条 trace 附着的批次标签（同 EvoScientist / OpenClaw 约定）
        "LIFT_EVAL_RUN_TAG": ctx.run_id,
        # 关键：钉死 Prime Agent 状态根目录，使 getAgentDir() 稳定落到
        # PRIME_AGENT_STATE_DIR。global harness / skills / sessions 全在此目录下，
        # 供 docker commit 整体捕获（见本模块 docstring 与 paths.py）。
        "PRIME_AGENT_CODING_AGENT_DIR": PRIME_AGENT_STATE_DIR,
    }
    # 把 LANGFUSE_BASE_URL / LANGFUSE_HOST 里的 loopback 改写为
    # host.docker.internal，避免容器 loopback 打不到宿主 Langfuse。
    pa_langfuse_host = rewrite_langfuse_base_url_for_container(
        os.environ.get("LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_HOST"),
    )
    if pa_langfuse_host:
        env_vars["LANGFUSE_BASE_URL"] = pa_langfuse_host
        env_vars["LANGFUSE_HOST"] = pa_langfuse_host

    extra_docker_args: list[str] = []
    if container_memory:
        extra_docker_args.extend(["--memory", container_memory])
    if container_cpus:
        extra_docker_args.extend(["--cpus", container_cpus])

    post_start_hooks: list = []
    # delta 镜像里可能带着 warmup 容器 commit 进来的 stale daemon/supervisor 状态，
    # evolved 容器启动时跨设备 rename 会触发 EXDEV 让首轮 chat 秒崩——先清理再放行。
    post_start_hooks.append(_clear_stale_daemon_state)
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
        viz_role=viz_role,
    )
