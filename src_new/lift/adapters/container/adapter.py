from __future__ import annotations

from abc import abstractmethod

from typing import override

from src_new.lift.adapters.base import AgentRuntimeAdapter, RunContext
from src_new.lift.adapters.container.delta import commit_delta_image
from src_new.lift.adapters.container.session import ContainerSession
from src_new.lift.adapters.environment import ExecutionEnvironment
from src_new.lift.policies.container import WarmupContainerPolicy
from src_new.lift.runtime.delta_ref import DeltaRef
from src_new.lift.runtime.environment_cleaner import delta_image_tag
from src_new.lift.runtime.suite_run_resources import SuiteRunResources
from src_new.models import SuiteTask
from src_new.lift.pipeline.run_options import RunOptions
from src_new.utils import short_id


class ContainerAgentRuntimeAdapter(AgentRuntimeAdapter):
    """Docker container agent runtime + default docker-commit delta materialization."""

    def __init__(self, options: RunOptions) -> None:
        super().__init__(options)
        self._docker_image = self.resolve_docker_image(override=options.docker_image)

    @classmethod
    @abstractmethod
    def resolve_docker_image(cls, *, override: str | None = None) -> str:
        """Resolve base container image from agent config or CLI override."""

    @override
    def baseline_image(self, ctx: RunContext) -> str:
        _ = ctx
        return self._docker_image

    @override
    async def produce_delta(
        self,
        resources: SuiteRunResources,
        policy,
        warmup_tasks: list[SuiteTask],
        ctx: RunContext,
    ) -> DeltaRef:
        policy_enum = self._options.warmup_container_policy
        if policy_enum == WarmupContainerPolicy.PARALLEL_MULTI:
            if self._options.parallel:
                raise NotImplementedError(
                    "parallel_multi warmup with per-task containers is not implemented yet"
                )
            raise NotImplementedError("parallel_multi warmup policy is not implemented yet")
        if policy_enum != WarmupContainerPolicy.SERIAL_SINGLE:
            raise ValueError(f"Unknown warmup container policy: {policy_enum}")
        return await super().produce_delta(resources, policy, warmup_tasks, ctx)

    @override
    async def start_warmup_environment(
        self,
        ctx: RunContext,
        resources: SuiteRunResources,
        workspace_dir,
    ) -> ExecutionEnvironment:
        _ = resources
        instance_id = f"{ctx.run_id}-r{ctx.repeat_index}-{ctx.suite_name}-warmup"
        session = await self.start_container(
            instance_id=instance_id,
            image=self._docker_image,
            ctx=ctx,
            workspace_dir=workspace_dir,
            seed_workspace=False,
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
        ctx: RunContext,
        resources: SuiteRunResources,
        task: SuiteTask,
        workspace_dir,
        *,
        image: str,
        seed_workspace: bool,
    ) -> ExecutionEnvironment:
        _ = resources
        instance_id = (
            f"{ctx.run_id}-r{ctx.repeat_index}-{task.name}-holdout-{short_id()}"
        )
        session = await self.start_container(
            instance_id=instance_id,
            image=image,
            ctx=ctx,
            workspace_dir=workspace_dir,
            seed_workspace=seed_workspace,
            task=task,
        )
        return ExecutionEnvironment(
            disposable=session,
            workspace_dir=workspace_dir,
            handle=session,
        )

    @override
    async def materialize_delta(
        self, env: ExecutionEnvironment, ctx: RunContext
    ) -> DeltaRef:
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
        ctx: RunContext,
        workspace_dir,
        seed_workspace: bool,
        task: SuiteTask | None,
    ) -> ContainerSession:
        """Start a runtime-specific container session."""
