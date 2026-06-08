from __future__ import annotations

from typing import override

from src_new.models import PhaseRun, SuiteTask
from src_new.utils import short_id

from src_new.lift.adapters.base import RunContext, RuntimeAdapter
from src_new.lift.policies.artifact import ArtifactPolicy
from src_new.lift.runtime.delta_ref import DeltaRef
from src_new.lift.runtime.suite_run_resources import SuiteRunResources


class MockAdapter(RuntimeAdapter):
    def __init__(self) -> None:
        self.produce_delta_count = 0
        self.before_load_count = 0
        self.after_load_count = 0
        self._last_warmup: list[SuiteTask] = []

    @override
    async def create_suite_run_resources(self, ctx: RunContext) -> SuiteRunResources:
        return SuiteRunResources(
            run_id=ctx.run_id,
            repeat_index=ctx.repeat_index,
            suite_name=ctx.suite_name,
        )

    @override
    async def produce_delta(
        self,
        resources: SuiteRunResources,
        policy: ArtifactPolicy,
        warmup_tasks: list[SuiteTask],
        ctx: RunContext,
    ) -> DeltaRef:
        self.produce_delta_count += 1
        self._last_warmup = list(warmup_tasks)
        delta = DeltaRef(image_tag=f"mock-delta:{ctx.run_id}:r{ctx.repeat_index}")
        resources.delta = delta
        return delta

    @override
    async def run_before_load(
        self,
        task: SuiteTask,
        resources: SuiteRunResources,
        ctx: RunContext,
        *,
        phase: str = "baseline",
    ) -> PhaseRun:
        self.before_load_count += 1
        return PhaseRun(
            work_session_id=f"work-{short_id()}",
            judge_session_id=f"judge-{short_id()}",
            success=True,
            content_score=1.0,
            workspace_dir=f"/mock/{phase}/{task.name}",
        )

    @override
    async def run_after_load(
        self,
        task: SuiteTask,
        resources: SuiteRunResources,
        delta: DeltaRef,
        ctx: RunContext,
    ) -> PhaseRun:
        self.after_load_count += 1
        return PhaseRun(
            work_session_id=f"work-{short_id()}",
            judge_session_id=f"judge-{short_id()}",
            success=True,
            content_score=1.0,
            workspace_dir=f"/mock/evolved/{task.name}",
        )
