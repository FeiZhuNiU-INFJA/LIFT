"""GenericAgent runtime adapter（baseline only）。

对应 LIFT CLI ``-r genericagent``：

- 基础镜像 ``GENERICAGENT_DOCKER_IMAGE``（不带 evolve 插件）；
- ``evolve_after_warmup`` 为 no-op：GA 没有显式 ``learn review`` 阶段，warmup
  期间 GA 写入容器内 ``memory/global_mem.txt`` / ``memory/global_mem_insight.txt``
  会被 ``ContainerAgentRuntimeAdapter.materialize_delta`` 通过 ``docker commit``
  自然带入 evolved 镜像；
- ``count_tool_calls`` 默认 None（GA 没有 OpenClaw 那种 trajectory.jsonl）。
"""

from __future__ import annotations

from pathlib import Path
from typing import override

from src.lift.adapters.base import SuiteRunContext
from src.lift.adapters.container.adapter import ContainerAgentRuntimeAdapter
from src.lift.adapters.container.session import ContainerSession
from src.lift.adapters.environment import ExecutionEnvironment
from src.lift.adapters.genericagent.chat_agent import (
    GenericAgentWorkerJudgerPairFactory,
)
from src.lift.adapters.genericagent.session import (
    genericagent_context,
    start_genericagent_container,
)
from src.lift.eval.stage import HoldoutLoadState, SuiteRunPhase
from src.lift.eval.worker_judger import WorkerJudgerPairFactory
from src.models import SuiteTask
from src.paths import GENERICAGENT_DOCKER_IMAGE


class GenericAgentAdapter(ContainerAgentRuntimeAdapter):
    """GenericAgent baseline runtime（无 learn review）。"""

    #: GA 把 warmup 期间学到的记忆写入容器内 ``/opt/GenericAgent/memory/`` 下的
    #: ``global_mem.txt`` / ``global_mem_insight.txt``；这里作为"真进化产物"目录
    #: 供 delta preflight diff 单独统计——``/usr/local/lib`` 里的 pip 副作用、
    #: ``/opt/GenericAgent/temp`` 里的 IO 副作用都不算证据。
    evolve_paths: tuple[str, ...] = ("/opt/GenericAgent/memory",)

    @classmethod
    @override
    def resolve_docker_image(cls, *, override: str | None = None) -> str:
        """``RunOptions.docker_image`` 优先，否则用默认 GA 镜像。"""
        return override or GENERICAGENT_DOCKER_IMAGE

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
        viz_role: str | None = None,
    ) -> ContainerSession:
        """委托 ``start_genericagent_container``；GA 不区分 baseline / evolved。"""
        _ = load_state
        return await start_genericagent_container(
            instance_id=instance_id,
            image=image,
            ctx=ctx,
            workspace_dir=workspace_dir,
            seed_workspace=seed_workspace,
            task=task,
            container_memory=self._options.container_memory,
            container_cpus=self._options.container_cpus,
            viz_role=viz_role,
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
        """绑定 GA 容器，返回 ``GenericAgentWorkerJudgerPairFactory``。"""
        session: ContainerSession = env.handle
        _ = (ctx, run_phase)
        judge_session = env.judge_handle or env.handle
        return GenericAgentWorkerJudgerPairFactory(
            container=genericagent_context(session),
            judge_container=genericagent_context(judge_session),
            workspace_dir=workspace_dir,
        )

    @override
    async def evolve_after_warmup(
        self, env: ExecutionEnvironment, ctx: SuiteRunContext
    ) -> None:
        """GA baseline 无显式 evolve；warmup 期间 ``memory/`` 由 docker commit 携带。"""
        _ = (env, ctx)
        return None
