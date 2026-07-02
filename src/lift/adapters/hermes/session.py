"""Hermes 容器启动（无 gateway / 无端口 / 无 readiness）。

与 GenericAgent 一致的"容器常驻空转 + docker exec 驱动"模型：

- 不发布端口、不做 readiness check（Hermes 不跑 gateway）。
- 只挂载评测 IO（outcome / benchmarks / task workspace / extra skills）。
- **不挂 Hermes 数据目录**：``/opt/data`` 留在容器镜像 FS 内，warmup 期 review
  写入的 memory / skills / sessions 随 ``docker commit`` 进入 delta 镜像，且天然
  隔离多容器并发（对齐官方"不共享数据目录"约束）。
- 容器 ENTRYPOINT（``hermes-entrypoint.sh``）会从 env patch ``/opt/data/config.yaml``
  的 model 块，再 ``tail -f /dev/null`` 空转。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from src.config import CONFIG, LOGGER
from src.lift.adapters.base import SuiteRunContext
from src.lift.adapters.container.exec import docker_exec_shell_async
from src.lift.adapters.container.session import ContainerSession
from src.lift.adapters.container.volumes import default_volume_binds, task_volume_binds
from src.lift.adapters.hermes.container_exec import HERMES_TASK_CWD
from src.models import SuiteTask

_CONTAINER_PREFIX = "evolve-hermes"


def _container_reclaim_ownership_script(uid: int, gid: int) -> str:
    """容器销毁前把 bind mount 目录 chown 回宿主机用户。"""
    return f"""
for d in /workspace/task /workspace/outcome; do
  if [[ -d "$d" ]]; then
    chown -R {uid}:{gid} "$d" 2>/dev/null || true
  fi
done
""".strip()


async def _reclaim_volume_ownership(session: ContainerSession) -> None:
    """容器销毁前 chown bind mount（best-effort）。"""
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


async def _end_leftover_runners(session: ContainerSession) -> None:
    """容器 cleanup 前收尾所有残留 runner（避免宿主机 docker exec 客户端孤儿进程）。"""
    # 延迟 import 避免与 chat_agent 的模块级循环依赖。
    from src.lift.adapters.hermes.chat_agent import end_all_runners

    await end_all_runners(session)


def hermes_context_container_name(session: ContainerSession) -> str:
    """从 ``ContainerSession`` 取容器名（Hermes 无额外 metadata）。"""
    return session.container_name


async def start_hermes_container(
    *,
    instance_id: str,
    image: str,
    ctx: SuiteRunContext,
    workspace_dir: Path | None = None,
    task: SuiteTask | None = None,
    container_memory: str | None = None,
    container_cpus: str | None = None,
) -> ContainerSession:
    """启动 Hermes 评测容器（无 gateway / readiness / 端口）。

    - ``entrypoint_cmd`` 为空：走镜像默认 ENTRYPOINT（hermes-entrypoint.sh）+ CMD
      （tail -f /dev/null）。
    - ``env_vars`` 注入 LIFT run tag、work LLM 凭据与 Langfuse 凭据；容器入口据此
      patch config.yaml，并把 ``LANGFUSE_*`` 映射为 Hermes 插件所需的
      ``HERMES_LANGFUSE_*``。
    - secret 仍走 ``--env-file``（仓库根 ``.env``）+ 显式 env_vars，不 bake 进镜像。
    """
    binds = default_volume_binds(run_id=ctx.run_id, repeat_index=ctx.repeat_index)
    if workspace_dir is not None:
        binds.append((str(workspace_dir.resolve()), HERMES_TASK_CWD, "rw"))
    if task is not None:
        binds.extend(task_volume_binds(task))

    env_vars: dict[str, str] = {
        "LIFT_EVAL_RUN_TAG": ctx.run_id,
        # config.yaml patch 依赖（容器入口读取）：
        "MODEL_NAME": CONFIG.model,
    }
    if CONFIG.hermes_model_name:
        env_vars["HERMES_MODEL_NAME"] = CONFIG.hermes_model_name
    if CONFIG.work_openai_api_key:
        env_vars["WORK_OPENAI_API_KEY"] = CONFIG.work_openai_api_key
    if CONFIG.work_openai_base_url:
        env_vars["WORK_OPENAI_BASE_URL"] = CONFIG.work_openai_base_url
    if CONFIG.hermes_api_url:
        env_vars["HERMES_API_URL"] = CONFIG.hermes_api_url
    # Langfuse：容器内访问宿主机需要 host.docker.internal（ContainerSession 已加 --add-host）。
    # 同时以 HERMES_ 前缀注入（Hermes langfuse 插件要求）；入口脚本还会把这些 append 进
    # /opt/data/.env。非前缀版一并保留，作为插件的兜底 fallback。
    if CONFIG.langfuse_public_key:
        env_vars["LANGFUSE_PUBLIC_KEY"] = CONFIG.langfuse_public_key
        env_vars["HERMES_LANGFUSE_PUBLIC_KEY"] = CONFIG.langfuse_public_key
    if CONFIG.langfuse_secret_key:
        env_vars["LANGFUSE_SECRET_KEY"] = CONFIG.langfuse_secret_key
        env_vars["HERMES_LANGFUSE_SECRET_KEY"] = CONFIG.langfuse_secret_key
    if CONFIG.langfuse_base_url:
        normalized_lf = _normalize_langfuse_base_url(CONFIG.langfuse_base_url)
        env_vars["LANGFUSE_BASE_URL"] = normalized_lf
        env_vars["HERMES_LANGFUSE_BASE_URL"] = normalized_lf
    # Firecrawl：镜像 build 期已按非空 key init；运行期同样注入，供 Hermes agent 使用。
    if CONFIG.firecrawl_api_key:
        env_vars["FIRECRAWL_API_KEY"] = CONFIG.firecrawl_api_key
    # Hermes API server 鉴权字段（legacy 会写入 Hermes .env）；容器入口 append 进
    # /opt/data/.env。enabled 始终注入布尔串，key 仅非空时注入。
    env_vars["API_SERVER_ENABLED"] = "true" if CONFIG.api_server_enabled else "false"
    if CONFIG.api_server_key:
        env_vars["API_SERVER_KEY"] = CONFIG.api_server_key

    extra_docker_args: list[str] = []
    if container_memory:
        extra_docker_args.extend(["--memory", container_memory])
    if container_cpus:
        extra_docker_args.extend(["--cpus", container_cpus])

    return await ContainerSession.start(
        instance_id=instance_id,
        container_name_prefix=_CONTAINER_PREFIX,
        image=image,
        entrypoint_cmd=[],  # 用镜像默认 ENTRYPOINT/CMD（entrypoint 空转）
        port_mappings=[],
        env_vars=env_vars,
        volume_binds=binds,
        env_file=Path.cwd() / ".env",
        extra_docker_args=extra_docker_args or None,
        readiness_check=None,
        post_start_hooks=[],
        pre_cleanup_hooks=[_end_leftover_runners, _reclaim_volume_ownership],
        metadata={},
        viz_repeat_index=ctx.repeat_index,
        viz_suite_name=ctx.suite_name,
    )


def _normalize_langfuse_base_url(raw: str) -> str:
    """localhost / 127.0.0.1 的 Langfuse URL 映射为 host.docker.internal。"""
    lowered = raw.strip().lower()
    if "127.0.0.1" in lowered or "localhost" in lowered:
        return "http://host.docker.internal:3000"
    return raw.strip()
