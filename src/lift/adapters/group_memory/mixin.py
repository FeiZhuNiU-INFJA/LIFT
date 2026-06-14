"""GroupMemoryAdapterMixin：多容器并行 warmup + 外部群体记忆产物的编排层 Mixin。

设计目标：
    通过多重继承组合到具体 runtime adapter（如 ``OpenClawAdapter``）之前，让 warmup
    阶段为每道题各起一个独立容器（"模拟多个用户分别做自己的题"），evolve 产物经由
    runtime 内嵌的群体记忆插件写入**外部记忆系统**——而不是 docker commit 出新镜像。

设计约定：
    - **不继承** ``AgentRuntimeAdapter``：仅以 duck typing override 方法，避免菱形继承。
    - 必须放在 MRO 左侧（如 ``class X(GroupMemoryAdapterMixin, OpenClawAdapter)``），
      使其 ``produce_delta`` / ``evolve_after_task`` / ``materialize_delta`` 优先生效。
    - 依赖父类提供 ``self.start_container``、``self._docker_image``、``self._options``、
      ``self.warmup_workspace``、``self.worker_judger_factory``——这些都来自
      ``ContainerAgentRuntimeAdapter`` + 具体 runtime adapter。

延伸：
    具体 runtime adapter（如 ``MultiUserOpenClawAdapter``）需要在自己的 ``start_container``
    中读取 ``load_state`` 参数，决定是否注入群体记忆 namespace/token 等 evolved-only env。
"""

from __future__ import annotations

from pathlib import Path

from src.config import LOGGER
from src.lift.adapters.base import SuiteRunContext
from src.lift.adapters.environment import ExecutionEnvironment
from src.lift.eval.stage import SuiteRunPhase
from src.lift.eval.task_exec import bounded_gather, execute_task
from src.lift.policies.artifact import ArtifactPolicy, WarmupThenUpdatePolicy
from src.lift.policies.container import WarmupContainerPolicy
from src.lift.runtime.delta_ref import DeltaRef
from src.lift.runtime.suite_run_resources import SuiteRunResources
from src.models import SuiteTask
from src.utils import short_id


class GroupMemoryAdapterMixin:
    """覆盖 LIFT 编排层：多容器并行 warmup + 外部群体记忆，不做 docker commit。

    覆盖的方法：
        - ``produce_delta``: 为每道 warmup 题各起一个容器（``PARALLEL_MULTI``），跑完后
          返回 ``owned=False`` 的占位 ``DeltaRef``——hold-out evolved 阶段会复用 base
          镜像，evolved 信号通过 ``load_state`` 透传给 ``start_container``，由 runtime
          决定如何加载群体记忆。
        - ``evolve_after_task``: 默认 no-op（群体记忆通常在 chat 期间由 runtime 插件
          实时写入，无需显式触发）。子类可覆盖为 flush / 索引重建调用。
        - ``evolve_after_warmup``: 默认 no-op（群体记忆是"题级"产物，没有"批次级收尾"语义）。
        - ``materialize_delta``: 默认返回 ``owned=False`` 的占位（实际不会被
          ``produce_delta`` 调用，仅为兜底契约）。
    """

    async def produce_delta(  # type: ignore[override]
        self,
        resources: SuiteRunResources,
        policy: ArtifactPolicy,
        warmup_tasks: list[SuiteTask],
        ctx: SuiteRunContext,
    ) -> DeltaRef:
        """每道 warmup 题各起一个容器（串行或并行），evolve 落到外部群体记忆。

        与 ``AgentRuntimeAdapter.produce_delta``（单容器串行 → commit）不同，本实现
        不做 docker commit；返回的 ``DeltaRef`` 复用 base 镜像，``owned=False``。
        """
        if not isinstance(policy, WarmupThenUpdatePolicy):
            raise TypeError(f"Unsupported artifact policy: {type(policy)!r}")
        if not warmup_tasks:
            raise ValueError("WarmupThenUpdatePolicy requires warmup tasks")

        container_policy: WarmupContainerPolicy = (
            self._options.warmup_container_policy  # type: ignore[attr-defined]
        )
        if container_policy is not WarmupContainerPolicy.PARALLEL_MULTI:
            raise ValueError(
                "GroupMemoryAdapterMixin requires warmup_container_policy=PARALLEL_MULTI; "
                f"got {container_policy.value!r}"
            )

        workspace_root = self.warmup_workspace(ctx)  # type: ignore[attr-defined]
        # 多容器并行写同一目录会互踩：每容器一个子目录（按 task.name 隔离）
        coros = [
            self._run_warmup_in_isolated_container(
                task=task,
                workspace_root=workspace_root,
                resources=resources,
                ctx=ctx,
            )
            for task in warmup_tasks
        ]
        if container_policy.tasks_parallel:
            await bounded_gather(
                coros,
                limit=self._options.max_concurrent_tasks,  # type: ignore[attr-defined]
            )
        else:
            for coro in coros:
                await coro

        delta = DeltaRef(
            image_tag=self._docker_image,  # type: ignore[attr-defined]
            owned=False,  # base 镜像，cleanup 不可 docker rmi
        )
        resources.delta = delta
        LOGGER.info(
            "GroupMemory delta materialized (external memory; image=%s, not owned)",
            delta.image_tag,
        )
        return delta

    async def _run_warmup_in_isolated_container(
        self,
        *,
        task: SuiteTask,
        workspace_root: Path,
        resources: SuiteRunResources,
        ctx: SuiteRunContext,
    ) -> None:
        """单题独立容器跑完 warmup → 调用 ``evolve_after_task`` 钩子 → 立刻 cleanup。"""
        workspace = workspace_root / task.name
        workspace.mkdir(parents=True, exist_ok=True)

        instance_id = (
            f"{ctx.run_id}-r{ctx.repeat_index}-{ctx.suite_name}-warmup-{task.name}-{short_id()}"
        )
        session = await self.start_container(  # type: ignore[attr-defined]
            instance_id=instance_id,
            image=self._docker_image,  # type: ignore[attr-defined]
            ctx=ctx,
            workspace_dir=workspace,
            seed_workspace=False,
            task=None,
            load_state=None,  # warmup 阶段不区分 baseline/evolved
        )
        resources.track(session)
        env = ExecutionEnvironment(
            disposable=session,
            workspace_dir=workspace,
            handle=session,
        )
        run_phase = SuiteRunPhase.warmup()
        try:
            factory = self.worker_judger_factory(  # type: ignore[attr-defined]
                env, ctx, run_phase=run_phase, workspace_dir=workspace
            )
            await execute_task(
                task=task,
                run_id=ctx.run_id,
                workspace_dir=workspace,
                factory=factory,
                run_phase=run_phase,
                max_conversation_turns=self._options.max_conversation_turns,  # type: ignore[attr-defined]
            )
            # 题级 evolve 钩子：默认 no-op；子类可覆盖为外部记忆 flush
            await self.evolve_after_task(env, task, ctx)  # type: ignore[attr-defined]
        finally:
            await session.cleanup()

    async def evolve_after_task(  # type: ignore[override]
        self,
        env: ExecutionEnvironment,
        task: SuiteTask,
        ctx: SuiteRunContext,
    ) -> None:
        """默认 no-op：群体记忆在 chat 期间由 runtime 插件实时写入，无需显式触发。

        子类如需在每题末尾对外部记忆系统做收尾（如 flush / 索引重建），可覆写此方法。
        """
        _ = (env, task, ctx)
        return None

    async def evolve_after_warmup(  # type: ignore[override]
        self,
        env: ExecutionEnvironment,
        ctx: SuiteRunContext,
    ) -> None:
        """默认 no-op：本 Mixin 的 ``produce_delta`` 不会调用此方法（钩子在每题后触发）。

        若被外部代码调用（如未走 Mixin 路径的容器编排），返回 no-op。
        """
        _ = (env, ctx)
        return None

    async def materialize_delta(  # type: ignore[override]
        self,
        env: ExecutionEnvironment,
        ctx: SuiteRunContext,
    ) -> DeltaRef:
        """兜底契约实现：本 Mixin 的 ``produce_delta`` 不会调用此方法。

        若被外部代码调用，返回与 ``produce_delta`` 一致的占位 ``DeltaRef``。
        """
        _ = (env, ctx)
        return DeltaRef(
            image_tag=self._docker_image,  # type: ignore[attr-defined]
            owned=False,
        )
