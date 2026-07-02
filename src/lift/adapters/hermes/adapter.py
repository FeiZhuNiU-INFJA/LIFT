"""Hermes runtime adapter（baseline，review 驱动的隐式演化）。

对应 LIFT CLI ``-r hermes``：

- 基础镜像 ``HERMES_DOCKER_IMAGE``（基于上游 ``nousresearch/hermes-agent``，默认 tag
  ``v2026.5.16``）；容器空转，chat 由 ``docker exec`` 起 ``hermes_runner.py`` 驱动。
- 演化语义沿用 legacy：warmup 阶段每题 work session 结束时触发 background review，
  把学到的 memory/skills 写入容器内 ``/opt/data``；``evolve_after_warmup`` 不额外执行
  显式命令，delta 由 ``ContainerAgentRuntimeAdapter.materialize_delta`` 的
  ``docker commit`` 自然携带。
- ``count_tool_calls`` 默认 None：Hermes 工具调用数走 Langfuse ``Hermes turn`` 兜底
  （后处理 ``_make_row_hermes`` 从 root output.tool_calls 统计）。
"""

from __future__ import annotations

from pathlib import Path
from typing import override

from src.config import LOGGER
from src.lift.adapters.base import SuiteRunContext
from src.lift.adapters.container.adapter import ContainerAgentRuntimeAdapter
from src.lift.adapters.container.session import ContainerSession
from src.lift.adapters.environment import ExecutionEnvironment
from src.lift.adapters.hermes.chat_agent import (
    HermesWorkerJudgerPairFactory,
    register_runner_registry,
)
from src.lift.adapters.hermes.container_exec import HermesContainerContext, read_hermes_paths
from src.lift.adapters.hermes.session import start_hermes_container
from src.lift.eval.stage import HoldoutLoadState, SuiteRunPhase, SuiteStage
from src.lift.eval.worker_judger import WorkerJudgerPairFactory
from src.lift.pipeline.run_options import RunOptions
from src.lift.policies.container import WarmupContainerPolicy
from src.models import PhaseRun, SuiteTask
from src.paths import HERMES_DOCKER_IMAGE


class HermesAdapter(ContainerAgentRuntimeAdapter):
    """Hermes runtime：容器 exec runner + review 驱动的隐式演化。"""

    def __init__(self, options: RunOptions) -> None:
        """初始化并对 warmup 并发策略做竞态提示。

        Hermes 的演化是"每题 work session 结束触发 background review 写共享
        ``/opt/data``"。在 ``parallel_single`` 下多题几乎同时结束，多个 review 进程
        会并发写同一 memory 存储，存在竞态。推荐 warmup 用 ``serial_single``
        （``--warmup-container-policy serial_single``），与 legacy"suite 内串行"语义
        一致；跨 suite/repeat 的并发仍由 ``--max-parallel-suites`` 提供。
        """
        super().__init__(options)
        if self._options.warmup_container_policy is WarmupContainerPolicy.PARALLEL_SINGLE:
            LOGGER.warning(
                "Hermes warmup uses parallel_single: per-task reviews write shared "
                "/opt/data concurrently and may race. Recommend "
                "'--warmup-container-policy serial_single' for Hermes."
            )

    @classmethod
    @override
    def resolve_docker_image(cls, *, override: str | None = None) -> str:
        """``RunOptions.docker_image`` 覆盖优先，否则用默认 Hermes 镜像。"""
        return override or HERMES_DOCKER_IMAGE

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
        """启动 Hermes 容器；启动后读回构建期发现的 venv/源码路径写入 metadata。"""
        _ = (seed_workspace, load_state)  # Hermes 状态 baked 在 /opt/data，不区分 seed
        session = await start_hermes_container(
            instance_id=instance_id,
            image=image,
            ctx=ctx,
            workspace_dir=workspace_dir,
            task=task,
            container_memory=self._options.container_memory,
            container_cpus=self._options.container_cpus,
        )
        venv_py, src_dir = await read_hermes_paths(session.container_name)
        session.metadata["hermes_venv_py"] = venv_py
        session.metadata["hermes_src_dir"] = src_dir
        return session

    def _context(self, session: ContainerSession) -> HermesContainerContext:
        return HermesContainerContext(
            container_name=session.container_name,
            venv_py=str(session.metadata["hermes_venv_py"]),
            src_dir=str(session.metadata.get("hermes_src_dir") or ""),
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
        """绑定 Hermes 容器，返回 ``HermesWorkerJudgerPairFactory``。

        warmup 阶段 work agent 带 review；holdout 阶段一律不 review。
        """
        _ = workspace_dir
        session: ContainerSession = env.handle
        return HermesWorkerJudgerPairFactory(
            container=self._context(session),
            session=session,
            run_id=ctx.run_id,
            warmup=(run_phase.stage == SuiteStage.WARMUP),
        )

    @override
    async def evolve_after_task(
        self,
        env: ExecutionEnvironment,
        task: SuiteTask,
        result: PhaseRun,
        ctx: SuiteRunContext,
    ) -> None:
        """每道 warmup 题完成后收尾其 runner：work session 触发 review 写入 /opt/data。

        - work runner：以 ``review=True`` end（若 spawn 时带 ``--enable-review``，
          runner 会在退出前跑 background review 并阻塞到完成）；
        - judge runner：以 ``review=False`` end，直接退出。
        review 写入的 memory/skills 随后由 ``materialize_delta`` 的 docker commit 带入 delta。
        """
        _ = (task, ctx)
        session: ContainerSession = env.handle
        registry = register_runner_registry(session)
        work_agent = registry.get(result.work_session_id)
        judge_agent = registry.get(result.judge_session_id)
        if work_agent is not None:
            try:
                await work_agent.end_session(review=True)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("hermes work review end failed task=%s: %r", result.work_session_id, exc)
        if judge_agent is not None:
            try:
                await judge_agent.end_session(review=False)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("hermes judge end failed task=%s: %r", result.judge_session_id, exc)

    @override
    async def evolve_after_warmup(
        self, env: ExecutionEnvironment, ctx: SuiteRunContext
    ) -> None:
        """Hermes 无显式 evolve 命令：演化已在每题 work session 结束时的 review 完成。"""
        _ = (env, ctx)
        return None
