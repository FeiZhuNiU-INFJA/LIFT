"""EvoScientist runtime adapter（M1 baseline）。

对应 LIFT CLI ``-r evoscientist``：

- 基础镜像 ``EVOSCIENTIST_DOCKER_IMAGE``（``ghcr.io/evoscientist/evoscientist:latest``
  叠 LIFT overlay）。
- ``evolve_after_warmup`` 为 no-op：EvoScientist baseline 没有显式蒸馏调用；
  warmup 期间 EvoScientist 自然写入的 ``~/.evoscientist/{sessions.db, memories/, skills/}``
  会被 ``ContainerAgentRuntimeAdapter.materialize_delta`` 通过 ``docker commit``
  自然带入 evolved 镜像。
- ``evolve_paths`` 白名单声明为 ``/root/.evoscientist``，供 delta preflight 输出
  evolve-only 摘要，负向判定 warmup 是否真的产出了进化产物。
- ``evoscientist_active_evolve`` 变体会 override ``evolve_after_warmup``，通过
  EvoScientist 的 AutoSkills backing API 触发并等待 EvoMemory AutoSkills graph。
"""

from __future__ import annotations

from pathlib import Path
from typing import override

from src.lift.adapters.base import SuiteRunContext
from src.lift.adapters.container.adapter import ContainerAgentRuntimeAdapter
from src.lift.adapters.container.session import ContainerSession
from src.lift.adapters.environment import ExecutionEnvironment
from src.lift.adapters.evoscientist.chat_agent import (
    EvoScientistWorkerJudgerPairFactory,
)
from src.lift.adapters.evoscientist.session import (
    evoscientist_context,
    start_evoscientist_container,
)
from src.lift.eval.stage import HoldoutLoadState, SuiteRunPhase
from src.lift.eval.worker_judger import WorkerJudgerPairFactory
from src.models import SuiteTask
from src.paths import EVOSCIENTIST_DOCKER_IMAGE


class EvoScientistAdapter(ContainerAgentRuntimeAdapter):
    """EvoScientist baseline runtime（M1；无显式 evolve 调用）。"""

    #: EvoScientist 把跨会话记忆写在容器内 ``/root/.evoscientist``（sessions.db +
    #: memories/ + skills/）；GLOBAL_SKILLS_DIR = DATA_DIR/skills（不是
    #: workspace/skills，后者是 bind mount 会被吞掉）。整个目录一并声明为
    #: evolve-only 白名单，让 delta preflight 能负向判定 warmup 是否产出。
    evolve_paths: tuple[str, ...] = ("/root/.evoscientist",)

    @classmethod
    @override
    def resolve_docker_image(cls, *, override: str | None = None) -> str:
        """``RunOptions.docker_image`` 优先，否则默认 EvoScientist 镜像。"""
        return override or EVOSCIENTIST_DOCKER_IMAGE

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
        """委托 ``start_evoscientist_container``；baseline 不区分 baseline/evolved。"""
        _ = load_state
        return await start_evoscientist_container(
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
        """绑定 EvoScientist 容器 → ``EvoScientistWorkerJudgerPairFactory``。"""
        session: ContainerSession = env.handle
        _ = (ctx, run_phase)
        return EvoScientistWorkerJudgerPairFactory(
            container=evoscientist_context(session),
            workspace_dir=workspace_dir,
        )

    @override
    async def evolve_after_warmup(
        self, env: ExecutionEnvironment, ctx: SuiteRunContext
    ) -> None:
        """baseline：无显式 evolve；warmup 期 ``~/.evoscientist`` 由 docker commit 携带。"""
        _ = (env, ctx)
        return None
