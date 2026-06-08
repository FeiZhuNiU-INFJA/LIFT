from __future__ import annotations

from pathlib import Path
from typing import override

from src_new.config import LOGGER
from src_new.hace.adapters.base import RunContext, RuntimeAdapter
from src_new.hace.adapters.openclaw.container_session import ContainerSession
from src_new.hace.adapters.openclaw.delta_producer import produce_delta_from_warmup
from src_new.hace.adapters.openclaw.task_runner import run_openclaw_task_phase
from src_new.hace.policies.artifact import ArtifactPolicy, WarmupThenUpdatePolicy
from src_new.hace.runtime.delta_ref import DeltaRef
from src_new.hace.runtime.repeat_scope import RepeatScope
from src_new.models import PhaseRun, SuiteTask
from src_new.hace.pipeline.run_options import RunOptions
from src_new.utils import outcome_workspace, short_id


class OpenClawAdapter(RuntimeAdapter):
    """OpenClaw runtime: host orchestration, agent execution inside Docker."""

    DEFAULT_IMAGE = "evolve-eval-openclaw:latest"

    def __init__(self, options: RunOptions) -> None:
        self._options = options
        self._docker_image = options.docker_image or self.DEFAULT_IMAGE

    @override
    async def open_repeat_scope(self, ctx: RunContext) -> RepeatScope:
        return RepeatScope(
            run_id=ctx.run_id,
            repeat_index=ctx.repeat_index,
            suite_name=ctx.suite_name,
        )

    @override
    async def produce_delta(
        self,
        scope: RepeatScope,
        policy: ArtifactPolicy,
        warmup_tasks: list[SuiteTask],
        ctx: RunContext,
    ) -> DeltaRef:
        if not isinstance(policy, WarmupThenUpdatePolicy):
            raise TypeError(f"Unsupported artifact policy: {type(policy)!r}")
        if not warmup_tasks:
            raise ValueError("WarmupThenUpdatePolicy requires warmup tasks")
        return await produce_delta_from_warmup(
            scope=scope,
            warmup_tasks=warmup_tasks,
            run_id=ctx.run_id,
            repeat_index=ctx.repeat_index,
            category_name=ctx.category_name,
            suite_name=ctx.suite_name,
            docker_image=self._docker_image,
            warmup_policy=self._options.warmup_container_policy,
            parallel_warmup_tasks=self._options.parallel,
        )

    @override
    async def run_before_load(
        self,
        task: SuiteTask,
        scope: RepeatScope,
        ctx: RunContext,
        *,
        phase: str = "baseline",
    ) -> PhaseRun:
        return await self._run_holdout_phase(
            task=task,
            scope=scope,
            ctx=ctx,
            image=self._docker_image,
            phase=phase,
            is_evolve_turn=False,
            log_label="before-load",
        )

    @override
    async def run_after_load(
        self,
        task: SuiteTask,
        scope: RepeatScope,
        delta: DeltaRef,
        ctx: RunContext,
    ) -> PhaseRun:
        return await self._run_holdout_phase(
            task=task,
            scope=scope,
            ctx=ctx,
            image=delta.image_tag,
            phase="evolved",
            is_evolve_turn=True,
            log_label="after-load",
        )

    async def _run_holdout_phase(
        self,
        *,
        task: SuiteTask,
        scope: RepeatScope,
        ctx: RunContext,
        image: str,
        phase: str,
        is_evolve_turn: bool,
        log_label: str,
    ) -> PhaseRun:
        workspace = self._task_workspace(ctx, task, phase)
        instance_id = (
            f"{ctx.run_id}-r{ctx.repeat_index}-{task.name}-{phase}-{short_id()}"
        )
        session = await ContainerSession.start(
            instance_id=instance_id,
            image=image,
            run_id=ctx.run_id,
            repeat_index=ctx.repeat_index,
            workspace_dir=workspace,
            seed_workspace=True,
            task=task,
        )
        scope.track(session)
        try:
            return await run_openclaw_task_phase(
                task=task,
                run_id=ctx.run_id,
                repeat_index=ctx.repeat_index,
                phase=phase,
                workspace_dir=workspace,
                container=session.context,
                is_evolve_turn=is_evolve_turn,
                is_final_task=True,
                log_label=log_label,
            )
        finally:
            await session.cleanup()

    @staticmethod
    def _task_workspace(ctx: RunContext, task: SuiteTask, phase: str) -> Path:
        base = outcome_workspace(
            ctx.run_id,
            ctx.repeat_index,
            phase,
            ctx.category_name,
        )
        path = base / task.name
        path.mkdir(parents=True, exist_ok=True)
        return path
