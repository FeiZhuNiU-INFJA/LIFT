"""OpenHuman 容器启动与 workspace seed 钩子。

OpenHuman 是 Rust 实现，进程为 ``openhuman-core serve``，容器内在 7788 端口暴露
HTTP JSON-RPC。LIFT 侧通过 ``POST http://127.0.0.1:{host_port}/rpc`` 调 ``agent.chat``。

与 OpenClaw / GenericAgent 的差异：

- **无内嵌 gateway 分层**：``openhuman-core`` 本身即 HTTP server；readiness 直接
  curl 该端口 ``/health`` / ``/`` 兜底。
- **secret 处理**：模型 / Langfuse 凭据通过 build-time bake（``config.toml``）+
  ``docker run -e`` 运行时注入。Langfuse 推送通过 ``push_spans`` / ``push_observations``
  是 best-effort（失败会被 caller swallow）；第一版镜像不 patch session token 校验，
  Langfuse push 若失败仅 warning，不影响 chat。
- **持久化路径**：``/root/.openhuman/workspace`` 是 memory_tree / wiki / thread
  history 的落盘目录，**LIFT 不挂 volume**，让 warmup 阶段写入的数据进入容器
  FS 层，供 ``docker commit`` 携带（``evolve_paths`` 白名单同步声明）。
"""

from __future__ import annotations

import asyncio
import os
import secrets
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
from src.lift.adapters.openhuman.container_exec import OpenHumanContainerContext
from src.models import SuiteTask
from src.paths import OPENHUMAN_WORKSPACE_SEED_DIR

_CONTAINER_PREFIX = "evolve-openhuman"
_RPC_CONTAINER_PORT = 7788  # openhuman-core serve 默认监听
CONTAINER_WORKSPACE_SEED_DIR = "/opt/lift/workspace_seed"
WORKSPACE_READY_MARKER = ".lift-workspace-ready"


def _container_reclaim_ownership_script(uid: int, gid: int) -> str:
    """在容器内以 root 执行，将 volume 目录 chown 回宿主机用户。

    注意：``/root/.openhuman`` **不被挂载**，因此不在 chown 名单里。
    """
    return f"""
for d in /workspace/task /workspace/outcome; do
  if [[ -d "$d" ]]; then
    chown -R {uid}:{gid} "$d" 2>/dev/null || true
  fi
done
""".strip()


def seed_eval_workspace(workspace_dir: Path, *, seed_dir: Path | None = None) -> None:
    """把 OpenHuman workspace seed 复制到宿主机 workspace 目录。"""
    source = seed_dir or OPENHUMAN_WORKSPACE_SEED_DIR
    workspace_dir.mkdir(parents=True, exist_ok=True)
    if not source.is_dir():
        (workspace_dir / WORKSPACE_READY_MARKER).touch()
        return
    for entry in sorted(source.iterdir()):
        dest = workspace_dir / entry.name
        if entry.is_dir():
            shutil.copytree(entry, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(entry, dest)
    (workspace_dir / WORKSPACE_READY_MARKER).touch()
    LOGGER.info("Seeded OpenHuman eval workspace: %s <- %s", workspace_dir, source)


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


async def _resolve_rpc_endpoint(session: ContainerSession) -> None:
    """readiness_check 前把宿主机真实端口写回 ``metadata['rpc_endpoint']``。"""
    host_port = session.published_ports[_RPC_CONTAINER_PORT]
    session.metadata["rpc_port"] = host_port
    session.metadata["rpc_endpoint"] = f"http://127.0.0.1:{host_port}"


async def _wait_rpc_ready(
    session: ContainerSession, *, tries: int = 120
) -> None:
    """轮询 curl RPC 端口探活，直到 openhuman-core serve 完成初始化。

    OpenHuman 冷启动包含配置加载、controller 注册等，耗时较长；tries=120 ×
    1s → 最多等 2min，足够 Rust binary 完成初始化。超时仅 warning 不抛错。

    ``/health`` 不需要 auth token；这里刻意避开 ``/rpc``（POST-only 且强制
    带 Bearer），readiness 阶段只做 socket-level 探活。
    """
    host_port = session.published_ports[_RPC_CONTAINER_PORT]
    for _ in range(tries):
        for path in ("/health", "/"):
            proc = await asyncio.create_subprocess_exec(
                "curl",
                "-sf",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                f"http://127.0.0.1:{host_port}{path}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            code = stdout.decode(errors="replace").strip()
            # 任意 2xx / 4xx 都说明 server socket 已开始接收；``/`` 通常直接
            # 返回 200 或路由文案。0 表示 curl 未连上。
            if code and code != "000":
                return
        await asyncio.sleep(1)
    LOGGER.warning(
        "OpenHuman RPC health check timed out for %s", session.container_name
    )


async def _check_rpc_with_resolved_port(session: ContainerSession) -> None:
    """先回填 endpoint，再做 RPC 健康检查。"""
    await _resolve_rpc_endpoint(session)
    await _wait_rpc_ready(session)


def openhuman_context(session: ContainerSession) -> OpenHumanContainerContext:
    """从 ``ContainerSession.metadata`` 构造 OpenHuman 容器上下文。"""
    return OpenHumanContainerContext(
        container_name=session.container_name,
        rpc_endpoint=str(session.metadata["rpc_endpoint"]),
        rpc_token=str(session.metadata["rpc_token"]),
    )


async def start_openhuman_container(
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
    """启动 OpenHuman 评测容器：端口分配、readiness、workspace seed。

    ``/root/.openhuman`` **不挂载**：memory_tree / wiki / thread history
    是"真进化产物"，必须留在容器 FS 层供 ``docker commit`` 捕获（同时
    ``evolve_paths`` 白名单声明这些子路径）。宿主侧仅挂 ``/workspace/task``
    用于 task materials / artifacts 交换。
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
        "LIFT_EVAL_RUN_TAG": ctx.run_id,
    }
    # openhuman-core 拒绝在非 loopback 地址（0.0.0.0）上无 token 裸绑。
    # 每容器独立生成 64 hex（256 bit）token，通过 ``-e`` 注入进程 +
    # 落到 ``session.metadata['rpc_token']``，adapter 侧 ``chat_agent`` 每次
    # ``POST /rpc`` 时带 ``Authorization: Bearer <token>``。
    rpc_token = secrets.token_hex(32)
    env_vars["OPENHUMAN_CORE_TOKEN"] = rpc_token

    # AgentBox / GMI MaaS bypass：openhuman-core 未登录时会拒绝 chat（`SESSION_EXPIRED:
    # backend session not active`）。设 ``OPENHUMAN_AGENTBOX_MODE=1`` 且 GMI 三件套
    # 齐全时，``chat-factory`` 走 "AgentBox mode ... bypassing app-session gate for
    # custom provider" 分支直连 OpenAI 兼容端点。镜像已 bake 一份缺省值；这里根据
    # 宿主 .env 再覆盖一次，方便运行时不重建镜像也能换凭据 / model。
    # 兼容两套变量名：优先 ``WORK_OPENAI_*``（LIFT 统一约定），回退历史 ``ARK_*``。
    work_api_key = (
        os.environ.get("WORK_OPENAI_API_KEY")
        or os.environ.get("ARK_API_KEY")
        or ""
    ).strip()
    work_base_url = (
        os.environ.get("WORK_OPENAI_BASE_URL")
        or os.environ.get("ARK_BASE_URL")
        or ""
    ).strip()
    model_name = (os.environ.get("OPENHUMAN_MODEL_NAME") or os.environ.get("MODEL_NAME") or "").strip()
    if model_name and "/" in model_name:
        # `provider/model` 复合形式（GA/OpenClaw 约定）里 provider 前缀对直连无意义
        model_name = model_name.rsplit("/", 1)[-1]
    if work_api_key and work_base_url and model_name:
        env_vars["OPENHUMAN_AGENTBOX_MODE"] = "1"
        env_vars["GMI_MAAS_BASE_URL"] = work_base_url
        env_vars["GMI_MAAS_API_KEY"] = work_api_key
        env_vars["GMI_MODELS"] = model_name
    else:
        LOGGER.warning(
            "OpenHuman AgentBox bypass env incomplete "
            "(work_api_key=%s work_base_url=%s model=%s); "
            "relying on image-baked defaults.",
            bool(work_api_key), bool(work_base_url), bool(model_name),
        )

    oh_langfuse_host = rewrite_langfuse_base_url_for_container(
        os.environ.get("LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_HOST"),
    )
    if oh_langfuse_host:
        # OpenHuman langfuse.rs 读的是 ``LANGFUSE_HOST``；同名 ``LANGFUSE_BASE_URL``
        # 同步覆写以对齐 GA 语义。
        env_vars["LANGFUSE_HOST"] = oh_langfuse_host
        env_vars["LANGFUSE_BASE_URL"] = oh_langfuse_host

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
        entrypoint_cmd=[],  # 镜像默认 ENTRYPOINT tini + openhuman-core serve
        port_mappings=[(None, _RPC_CONTAINER_PORT)],
        env_vars=env_vars,
        volume_binds=binds,
        env_file=Path.cwd() / ".env",
        extra_docker_args=extra_docker_args or None,
        readiness_check=_check_rpc_with_resolved_port,
        post_start_hooks=post_start_hooks,
        pre_cleanup_hooks=[_reclaim_volume_ownership],
        metadata={"rpc_token": rpc_token},
        viz_repeat_index=ctx.repeat_index,
        viz_suite_name=ctx.suite_name,
    )
