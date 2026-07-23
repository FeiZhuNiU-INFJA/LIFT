"""OpenClaw gateway 容器启动：端口分配、volume、readiness、workspace bridge 与运行时 env。"""

from __future__ import annotations

import asyncio
import os
import secrets
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
from src.lift.adapters.openclaw.container_exec import OpenClawContainerContext
from src.models import SuiteTask

_GATEWAY_CONTAINER_PORT = 18789  # 容器内 gateway 端口（agent --local 连此）
_FASTAPI_CONTAINER_PORT = 18090  # 容器内 self-evolving plugin HTTP 端口
_CONTAINER_PREFIX = "lift-openclaw"  # docker 容器名前缀

_FALLBACK_CONTAINER_LANGFUSE_BASE_URL = "http://host.docker.internal:3000"  # 未配 LANGFUSE_BASE_URL 时容器内使用的默认
CONTAINER_AGENT_WORKSPACE = "/root/.openclaw/workspace"  # 与 agents.fragment.json 对齐
CONTAINER_TASK_DIR = "/workspace/task"  # host bind mount：任务素材 + 当题产物
CONTAINER_EXTRA_SKILLS_DIR = f"{CONTAINER_TASK_DIR}/skills"  # task.requirements.extra_skills_dir 挂载点


def _container_runtime_env() -> dict[str, str]:
    """``docker run`` 时需要相对宿主机 ``.env`` **改写**的环境变量。

    其它 secret（``WORK_OPENAI_API_KEY`` / ``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY`` /
    ``FIRECRAWL_API_KEY`` 等）一律走 ``--env-file``，不在这里返回——避免 secret 重复
    出现在 ``docker run -e ...`` 命令行与日志里；这些值已经写入容器 ``Config.Env``，
    后续 ``docker exec`` 会自动继承，无需再次注入。
    """
    return {
        # 容器内 host.docker.internal 访问宿主机 Langfuse；宿主机 .env 通常配 localhost
        "LANGFUSE_BASE_URL": rewrite_langfuse_base_url_for_container(
            os.environ.get("LANGFUSE_BASE_URL")
        ) or _FALLBACK_CONTAINER_LANGFUSE_BASE_URL,
        # 让 langfuse-tracer 的 append log 落到 bind mount，便于事后诊断 accumulator 是否 populate
        "LANGFUSE_TRACER_LOG_FILE": f"{CONTAINER_TASK_DIR}/langfuse-tracer.log",
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
    """Deprecated no-op: workspace seed now ships baked into the image at
    ``/root/.openclaw/workspace``. Kept as a no-op for callers (e.g. group_memory
    mixin) that still pass ``seed_workspace=True``.
    """
    _ = (workspace_dir, seed_dir)


def _bridge_workspace_shell() -> str:
    """Symlink task materials (bind mount) into the agent workspace, and route
    the agent's ``result/`` directory back out to the bind mount so produced
    files are visible on the host while seed/memory stays inside the image.
    """
    return f"""
ws={CONTAINER_AGENT_WORKSPACE}
task={CONTAINER_TASK_DIR}
mkdir -p "$ws" "$task"
# Material directories: bind mount -> workspace (so agent reads `qN_materials/`
# via cwd-relative path, but the bytes live on the host).
for d in "$task"/*_materials; do
  [[ -d "$d" ]] || continue
  name=$(basename "$d")
  rm -rf "$ws/$name"
  ln -s "$d" "$ws/$name"
done
# Result directory: workspace -> bind mount (agent writes `result/result_qN/`
# under workspace; bytes land on the host where evaluation/observability reads).
mkdir -p "$task/result"
rm -rf "$ws/result"
ln -s "$task/result" "$ws/result"
""".strip()


def _container_extra_skills_shell() -> str:
    """Install task-provided skills into OpenClaw's state dir.

    ``/workspace/task/skills`` is a bind mount and will not be captured by
    ``docker commit``. Copying it into ``$OPENCLAW_STATE_DIR/skills`` makes the
    skills visible from OpenClaw's own state tree and preserves them in evolved
    delta images.
    """
    return f"""
if [[ -d "{CONTAINER_EXTRA_SKILLS_DIR}" ]]; then
  state_dir="${{OPENCLAW_STATE_DIR:-/root/.openclaw}}"
  mkdir -p "${{state_dir}}/skills"
  find "{CONTAINER_EXTRA_SKILLS_DIR}" -mindepth 1 -maxdepth 1 -exec cp -a {{}} "${{state_dir}}/skills/" \\;
  chmod -R u+rwX,go+rX "${{state_dir}}/skills" 2>/dev/null || true
fi
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


async def _bridge_workspace(session: ContainerSession) -> None:
    """Symlink task materials/result between bind mount and agent workspace."""
    try:
        await docker_exec_shell_async(
            session.container_name,
            _bridge_workspace_shell(),
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning(
            "Failed to bridge workspace in %s: %s",
            session.container_name,
            exc,
        )


async def _install_extra_skills(session: ContainerSession) -> None:
    """把 task 级 extra skills 从 bind mount 安装进 OpenClaw state dir。"""
    try:
        await docker_exec_shell_async(
            session.container_name,
            _container_extra_skills_shell(),
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning(
            "Failed to install extra skills in %s: %s",
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
    force_bridge_network: bool = False,
    agentmemory_prelaunch: bool = False,
    viz_role: str | None = None,
) -> ContainerSession:
    """启动 OpenClaw gateway 容器：端口、token、volume、readiness 与 workspace bridge。

    Workspace 布局：agent 真正的 cwd 是镜像内 ``/root/.openclaw/workspace``（参与
    ``docker commit``，承载 SOUL/memory 等持久态）；host 侧任务素材与产物通过
    ``/workspace/task`` bind mount 传入传出，并由 ``_bridge_workspace`` 把
    ``*_materials/`` 与 ``result/`` 软链桥接到 agent workspace。

    ``seed_workspace``: 保留以兼容 group_memory mixin；当前为 no-op（seed 已 baked
    进镜像，不再需要宿主机侧复制）。

    ``container_memory`` / ``container_cpus``: 透传给 ``docker run --memory`` /
    ``--cpus`` 的单容器资源上限（None 表示不限制）。

    宿主机端口由 Docker 自动分配（避免确定性 hash 端口的碰撞与占用冲突）；启动后
    ``_resolve_gateway_port`` 把真实端口写回 ``metadata['gateway_port']``。
    """
    _ = seed_workspace  # 保留参数签名兼容 group_memory mixin；seed 已 baked 进镜像
    token = secrets.token_hex(32)

    binds = default_volume_binds(
        run_id=ctx.run_id,
        repeat_index=ctx.repeat_index,
    )
    if workspace_dir is not None:
        binds.append((str(workspace_dir.resolve()), CONTAINER_TASK_DIR, "rw"))
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
    # 镜像里 baked 的 healthcheck 是 ``openclaw plugins list``，每 30s 在每个容器
    # 内冷启一次 node 进程；我们 readiness 由 ``_wait_gateway`` 自己 curl，
    # docker 端的 health 状态既不被消费、又持续吃 CPU——禁掉。
    extra_docker_args.append("--no-healthcheck")

    post_start_hooks: list = []
    if workspace_dir is not None:
        post_start_hooks.append(_reset_workspace_attestations)  # 清跨题 attestations 状态
        post_start_hooks.append(_bridge_workspace)  # 任务素材/产物 ↔ agent workspace 软链
    if task is not None:
        post_start_hooks.append(_install_extra_skills)  # 注册 workspace skills 到 OpenClaw state

    # gateway 启动命令。agentmemory 变体在其前置一个 prelaunch 包装脚本：先在容器内
    # 后台起 agentmemory server（:3111，离线本地嵌入），再 exec 原 gateway 命令。
    gateway_cmd = ["openclaw", "gateway", "run", "--bind", "lan"]
    if agentmemory_prelaunch:
        entrypoint_cmd = ["/opt/lift/openclaw-agentmemory-prelaunch.sh", *gateway_cmd]
    else:
        entrypoint_cmd = gateway_cmd

    return await ContainerSession.start(
        instance_id=instance_id,
        container_name_prefix=_CONTAINER_PREFIX,
        image=image,
        entrypoint_cmd=entrypoint_cmd,
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
        viz_role=viz_role,
        force_bridge_network=force_bridge_network,
    )


async def _check_gateway_with_resolved_port(session: ContainerSession) -> None:
    """先回填真实端口，再做 gateway 健康检查。

    ``ContainerSession.start`` 在调 readiness_check 前已 inspect 出真实端口，
    这里把端口写回 metadata 后调用 ``_wait_gateway``（依赖 metadata['gateway_port']）。
    """
    await _resolve_gateway_port(session)
    await _wait_gateway(session)
