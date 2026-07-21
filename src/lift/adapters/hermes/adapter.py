"""Hermes runtime adapter（baseline，review 驱动的隐式演化）。

对应 LIFT CLI ``-r hermes``：

- 基础镜像 ``HERMES_DOCKER_IMAGE``（基于上游 ``nousresearch/hermes-agent``，默认 tag
  ``v2026.5.16``）；容器空转，chat 由 ``docker exec`` 起 ``hermes_runner.py`` 驱动。
- 演化语义沿用 Hermes runner 协议：warmup 阶段每题 work session 结束时触发 background review，
  把学到的 memory/skills 写入容器内 ``/opt/hermes-state``；``evolve_after_warmup`` 不额外执行
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

    #: Hermes 的进化产物落在容器内 ``/opt/hermes-state/skills`` (background review
    #: 蒸馏出的技能包，含 ``SKILL.md``) 和 ``/opt/hermes-state/memories``
    #: (``MEMORY.md`` / ``USER.md`` 等长期记忆)。``sessions/`` 是每题会话流水、
    #: ``logs/`` 是运行时日志，不作为进化证据。
    evolve_paths: tuple[str, ...] = (
        "/opt/hermes-state/skills",
        "/opt/hermes-state/memories",
    )

    def __init__(self, options: RunOptions) -> None:
        """初始化并对 warmup 并发策略做竞态提示。

        Hermes 的演化是"每题 work session 结束触发 background review 写共享
        ``/opt/hermes-state``"。在 ``parallel_single`` 下多题几乎同时结束，多个 review 进程
        会并发写同一 memory 存储，存在竞态。推荐 warmup 用 ``serial_single``
        （``--warmup-container-policy serial_single``），与 Hermes suite 内串行评测语义
        一致；跨 suite/repeat 的并发仍由 ``--max-parallel-suites`` 提供。
        """
        super().__init__(options)
        if self._options.warmup_container_policy is WarmupContainerPolicy.PARALLEL_SINGLE:
            LOGGER.warning(
                "Hermes warmup uses parallel_single: per-task reviews write shared "
                "/opt/hermes-state concurrently and may race. Recommend "
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
        _ = (seed_workspace, load_state)  # Hermes 状态 baked 在 /opt/hermes-state，不区分 seed
        session = await start_hermes_container(
            instance_id=instance_id,
            image=image,
            ctx=ctx,
            workspace_dir=workspace_dir,
            task=task,
            container_memory=self._options.container_memory,
            container_cpus=self._options.container_cpus,
            force_bridge_network=self.force_bridge_network,
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
        """每道 warmup 题完成后收尾其 runner：work session 触发 review 写入 /opt/hermes-state。

        **holdout 前的硬保证**：delta 是在这里所有 warmup 题 review 完成后、经
        ``materialize_delta`` 的 docker commit 固化的（时序见 ``base.py::produce_delta``
        L114-136：``execute_tasks`` 内每题 ``run_one`` 会 ``await on_task_done``，即本
        钩子，全部返回后才 commit）。因此若某题的 work review 没跑完就放行，holdout 就会
        在“演化未完成”的 delta 上测量，破坏评测语义。为杜绝这种情况：

        - work runner：``enable_review=True``（warmup spawn 时锁定）。收到 task_end 后
          runner 会跑 background review 并阻塞到完成，``end_session`` 返回 ``True`` 才算
          review 干净落盘。**这里对 work 做硬保证**——runner 缺失或未干净退出即 ``raise``，
          交给 ``_run_evolve_after_task_with_retry`` 重试（3 次）；仍失败则整个
          ``produce_delta`` 中止，绝不放行到 holdout。
        - judge runner：``enable_review=False``，纯收尾（不 review），失败无害，
          保持 best-effort。
        是否 review 完全由 spawn 时的 --enable-review 决定，end_session 不带 review 参数；
        review 写入的 memory/skills 随后由 ``materialize_delta`` 的 docker commit 带入 delta。
        """
        _ = (task, ctx)
        session: ContainerSession = env.handle
        registry = register_runner_registry(session)
        work_agent = registry.get(result.work_session_id)
        judge_agent = registry.get(result.judge_session_id)

        # judge 先收尾（best-effort）：judge 不 review，即使失败也不影响 delta 语义，
        # 且要在 work 可能 raise 之前把 judge runner 释放掉，避免残留。
        if judge_agent is not None:
            try:
                await judge_agent.end_session()
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("hermes judge end failed task=%s: %r", result.judge_session_id, exc)

        # work 硬保证：warmup work runner 一定启用了 review，必须跑完 review 才放行。
        if work_agent is None:
            # warmup work 一定 chat 过并注册到 registry；缺失说明状态异常，
            # 宁可 raise 触发重试/中止，也不放行未 review 的 delta 到 holdout。
            raise RuntimeError(
                f"hermes work runner missing for warmup task={result.work_session_id}; "
                "cannot guarantee review before holdout"
            )
        clean = await work_agent.end_session()
        if not clean:
            raise RuntimeError(
                f"hermes work review did not complete cleanly for warmup "
                f"task={result.work_session_id} (runner killed on timeout or exited "
                "non-zero); refusing to materialize delta with incomplete review"
            )

    @override
    async def evolve_after_warmup(
        self, env: ExecutionEnvironment, ctx: SuiteRunContext
    ) -> None:
        """Hermes 无显式 evolve 命令：演化已在每题 work session 结束时的 review 完成。"""
        _ = (env, ctx)
        return None
