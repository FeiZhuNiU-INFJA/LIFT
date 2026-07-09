"""OpenHuman runtime adapter（baseline only）。

对应 LIFT CLI ``-r openhuman``：

- 基础镜像 ``OPENHUMAN_DOCKER_IMAGE``；容器 entrypoint 直接跑 ``openhuman-core serve``，
  chat 走 HTTP JSON-RPC（见 ``chat_agent.py``）。
- ``evolve_after_warmup`` no-op：OpenHuman 没有独立的 evolve 命令；warmup 期间
  写入容器内 ``/home/openhuman/.openhuman/{memory_tree,wiki}`` 的进化产物由
  ``ContainerAgentRuntimeAdapter.materialize_delta`` 通过 ``docker commit`` 自然带入
  evolved 镜像。``evolve_paths`` 白名单声明这两个子路径作为"真进化"证据。
- ``count_tool_calls`` 默认 None（OpenHuman 无 trajectory.jsonl；工具调用统计后续
  从 Langfuse trace 侧完成）。
"""

from __future__ import annotations

from pathlib import Path
from typing import override

from src.lift.adapters.base import SuiteRunContext
from src.lift.adapters.container.adapter import ContainerAgentRuntimeAdapter
from src.lift.adapters.container.session import ContainerSession
from src.lift.adapters.environment import ExecutionEnvironment
from src.lift.adapters.openhuman.chat_agent import (
    OpenHumanWorkerJudgerPairFactory,
)
from src.lift.adapters.openhuman.session import (
    openhuman_context,
    start_openhuman_container,
)
from src.lift.eval.stage import HoldoutLoadState, SuiteRunPhase
from src.lift.eval.worker_judger import WorkerJudgerPairFactory
from src.models import SuiteTask
from src.paths import OPENHUMAN_DOCKER_IMAGE


class OpenHumanAdapter(ContainerAgentRuntimeAdapter):
    """OpenHuman baseline runtime（无显式 evolve 命令）。"""

    #: OpenHuman warmup 期间的"真进化产物"目录：memory_tree（结构化长期记忆树）
    #: 与 wiki（自建知识库）。thread history 等 IO 副作用不算证据，落在
    #: ``.openhuman/threads`` 之类的兄弟目录，不入白名单。
    evolve_paths: tuple[str, ...] = (
        "/root/.openhuman/workspace/memory_tree",
        "/root/.openhuman/workspace/wiki",
    )

    @classmethod
    @override
    def resolve_docker_image(cls, *, override: str | None = None) -> str:
        """``RunOptions.docker_image`` 优先，否则用默认 OpenHuman 镜像。"""
        return override or OPENHUMAN_DOCKER_IMAGE

    @override
    async def start_container(
        self,
        *,
        instance_id: str,
        image: str,
        ctx: SuiteRunContext,
        workspace_dir: Path,
        seed_workspace: bool,
        task: SuiteTask | None,
        load_state: HoldoutLoadState | None = None,
    ) -> ContainerSession:
        """委托 ``start_openhuman_container``；baseline 与 evolved 走同一路径。"""
        _ = load_state
        return await start_openhuman_container(
            instance_id=instance_id,
            image=image,
            ctx=ctx,
            workspace_dir=workspace_dir,
            seed_workspace=seed_workspace,
            task=task,
            container_memory=self._options.container_memory,
            container_cpus=self._options.container_cpus,
        )

    @override
    def worker_judger_factory(
        self,
        env: ExecutionEnvironment,
        ctx: SuiteRunContext,
        *,
        run_phase: SuiteRunPhase,
        workspace_dir: Path,
    ) -> WorkerJudgerPairFactory:
        """绑定 OpenHuman 容器，返回 ``OpenHumanWorkerJudgerPairFactory``。

        ``ctx.run_id`` 作为 ``run_tag`` 透传给 factory / chat agent，用于
        Langfuse push 时给 root trace 打上评测 run tag（与 pre-chat span 对齐）。
        """
        session: ContainerSession = env.handle
        _ = run_phase
        return OpenHumanWorkerJudgerPairFactory(
            container=openhuman_context(session),
            workspace_dir=workspace_dir,
            run_tag=ctx.run_id,
        )

    @override
    async def evolve_after_warmup(
        self, env: ExecutionEnvironment, ctx: SuiteRunContext
    ) -> None:
        """OpenHuman baseline 无显式 evolve；memory_tree / wiki 由 docker commit 携带。"""
        _ = (env, ctx)
        return None
