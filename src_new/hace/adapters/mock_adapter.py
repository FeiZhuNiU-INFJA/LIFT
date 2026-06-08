from __future__ import annotations

from src_new.models import PhaseRun, SuiteTask
from src_new.utils import short_id

from src_new.hace.adapters.base import RunContext
from src_new.hace.policies.artifact import ArtifactPolicy
from src_new.hace.runtime.delta_ref import DeltaRef
from src_new.hace.runtime.repeat_scope import RepeatScope


class MockAdapter:
    def __init__(self) -> None:
        self.produce_delta_count = 0
        self.before_load_count = 0
        self.after_load_count = 0
        self.scopes_cleaned = 0
        self._last_warmup: list[SuiteTask] = []

    async def open_repeat_scope(self, ctx: RunContext) -> RepeatScope:
        return RepeatScope(
            run_id=ctx.run_id,
            repeat_index=ctx.repeat_index,
            suite_name=ctx.suite_name,
        )

    async def produce_delta(
        self,
        scope: RepeatScope,
        policy: ArtifactPolicy,
        warmup_tasks: list[SuiteTask],
        ctx: RunContext,
    ) -> DeltaRef:
        self.produce_delta_count += 1
        self._last_warmup = list(warmup_tasks)
        delta = DeltaRef(image_tag=f"mock-delta:{ctx.run_id}:r{ctx.repeat_index}")
        scope.delta = delta
        return delta

    async def run_before_load(
        self,
        task: SuiteTask,
        scope: RepeatScope,
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

    async def run_after_load(
        self,
        task: SuiteTask,
        scope: RepeatScope,
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
