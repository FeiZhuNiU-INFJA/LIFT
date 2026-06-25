"""Docker 容器 runtime adapter：默认 docker-commit delta 物化。"""

from __future__ import annotations

from abc import abstractmethod

from typing import override

from src.lift.adapters.base import AgentRuntimeAdapter, SuiteRunContext
from src.lift.adapters.container.delta import commit_delta_image
from src.lift.adapters.container.session import ContainerSession
from src.lift.adapters.container.session import clip_name_segment
from src.lift.adapters.environment import ExecutionEnvironment
from src.lift.eval.stage import HoldoutLoadState
from src.lift.policies.container import WarmupContainerPolicy
from src.lift.runtime.delta_ref import DeltaRef
from src.lift.runtime.environment_cleaner import delta_image_tag
from src.lift.runtime.suite_run_resources import SuiteRunResources
from src.models import SuiteTask
from src.lift.pipeline.run_options import RunOptions
from src.utils import short_id


class ContainerAgentRuntimeAdapter(AgentRuntimeAdapter):
    """Docker 容器 agent runtime + 默认 docker commit delta 物化。"""

    def __init__(self, options: RunOptions) -> None:
        """解析 base 镜像并缓存到 ``_docker_image``。"""
        super().__init__(options)
        self._docker_image = self.resolve_docker_image(override=options.docker_image)

    @classmethod
    @abstractmethod
    def resolve_docker_image(cls, *, override: str | None = None) -> str:
        """从 agent 配置或 CLI override 解析 base 容器镜像。"""

    @override
    def baseline_image(self, ctx: SuiteRunContext) -> str:
        """before-load holdout 使用的 baseline 镜像（即 base 镜像）。"""
        _ = ctx
        return self._docker_image

    @override
    async def produce_delta(
        self,
        resources: SuiteRunResources,
        policy,
        warmup_tasks: list[SuiteTask],
        ctx: SuiteRunContext,
    ) -> DeltaRef:
        """按 ``warmup_container_policy`` 校验后委托父类执行 warmup → delta。

        ``ContainerAgentRuntimeAdapter`` 默认是"单容器 commit 镜像"形态，仅支持
        ``SERIAL_SINGLE`` / ``PARALLEL_SINGLE``（同容器内并发）。``PARALLEL_MULTI``
        需要走 ``GroupMemoryAdapterMixin`` 等覆盖编排层。
        """
        policy_enum = self._options.warmup_container_policy
        if policy_enum is WarmupContainerPolicy.PARALLEL_MULTI:
            raise NotImplementedError(
                "parallel_multi warmup requires a GroupMemoryAdapterMixin-based adapter"
            )
        if policy_enum not in (
            WarmupContainerPolicy.SERIAL_SINGLE,
            WarmupContainerPolicy.PARALLEL_SINGLE,
        ):
            raise ValueError(f"Unknown warmup container policy: {policy_enum}")
        return await super().produce_delta(resources, policy, warmup_tasks, ctx)

    @override
    async def start_warmup_environment(
        self,
        ctx: SuiteRunContext,
        resources: SuiteRunResources,
        workspace_dir,
    ) -> ExecutionEnvironment:
        """warmup 阶段：单容器 + base 镜像；``seed_workspace=True`` 注入人设种子。

        warmup 容器同样需要 IDENTITY/USER/SOUL 等 seed 文件，否则 agent 会在每次 warmup
        首发时跑首次上线问名字 / emoji 的 onboarding 流程，浪费 turn 也污染评测语料。
        """
        _ = resources
        instance_id = (
            f"{ctx.run_id}-r{ctx.repeat_index}-{clip_name_segment(ctx.suite_name)}-warmup"
        )
        session = await self.start_container(
            instance_id=instance_id,
            image=self._docker_image,
            ctx=ctx,
            workspace_dir=workspace_dir,
            seed_workspace=True,
            task=None,
        )
        return ExecutionEnvironment(
            disposable=session,
            workspace_dir=workspace_dir,
            handle=session,
        )

    @override
    async def start_holdout_environment(
        self,
        ctx: SuiteRunContext,
        resources: SuiteRunResources,
        task: SuiteTask,
        workspace_dir,
        *,
        image: str,
        seed_workspace: bool,
        load_state: HoldoutLoadState,
    ) -> ExecutionEnvironment:
        """holdout 单题：独立容器 + 指定镜像（baseline 或 delta）。

        ``seed_workspace`` 原样传给 ``start_container``（见该方法的文档）。
        ``load_state`` 透传给 ``start_container``，由 runtime 决定是否注入
        evolved-only 配置（如群体记忆 namespace）。
        """
        _ = resources
        # short_id 保证并行 holdout 或重跑时容器名不撞；带上 suite_name 让运维能从
        # 容器名直接看出对应的 suite（中文 suite/task 会经 clip_name_segment 转写为
        # 拼音并各截到 20 字符，holdout 标记和 short_id 不再被截断）；load_state
        # （baseline/evolved）也编进容器名，方便 TUI / 日志一眼区分对照阶段。
        instance_id = (
            f"{ctx.run_id}-r{ctx.repeat_index}-{clip_name_segment(ctx.suite_name)}"
            f"-{clip_name_segment(task.name)}-holdout-{load_state.value}-{short_id()}"
        )
        session = await self.start_container(
            instance_id=instance_id,
            image=image,
            ctx=ctx,
            workspace_dir=workspace_dir,
            seed_workspace=seed_workspace,
            task=task,
            load_state=load_state,
        )
        return ExecutionEnvironment(
            disposable=session,
            workspace_dir=workspace_dir,
            handle=session,
        )

    @override
    async def materialize_delta(
        self, env: ExecutionEnvironment, ctx: SuiteRunContext
    ) -> DeltaRef:
        """将 warmup 容器 commit 为 delta 镜像并返回 ``DeltaRef``。"""
        session: ContainerSession = env.handle
        image_tag = delta_image_tag(
            run_id=ctx.run_id,
            repeat_index=ctx.repeat_index,
            suite_name=ctx.suite_name,
        )
        await commit_delta_image(session.container_name, image_tag)
        return DeltaRef(
            image_tag=image_tag,
            source_container=session.container_name,
        )

    @abstractmethod
    async def start_container(
        self,
        *,
        instance_id: str,
        image: str,
        ctx: SuiteRunContext,
        workspace_dir,
        seed_workspace: bool,
        task: SuiteTask | None,
        load_state: HoldoutLoadState | None = None,
    ) -> ContainerSession:
        """启动运行时特定的容器会话（子类实现 gateway/entrypoint 等）。

        ``seed_workspace``: 挂载 ``workspace_dir`` 之前是否预置工作区内容。
        - **OpenClaw**（``True``）：``workspace_seed/`` → 跳过 BOOTSTRAP 首次上线。
        - **其他 runtime**：可实现为写入 AGENTS.md / 规则 / 空操作；框架不规定文件格式。
        - **Warmup / Holdout** 路径均传 ``True`` 由调用方保证；避免 agent 重复跑首次
          上线 onboarding。

        ``load_state``: 仅在 holdout 路径有值（``BASELINE`` / ``EVOLVED``），warmup 路径
        为 ``None``。runtime 据此决定 evolved-only 注入（如群体记忆 namespace、token 等）。
        """
