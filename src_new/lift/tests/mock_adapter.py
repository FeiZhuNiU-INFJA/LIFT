from __future__ import annotations

from pathlib import Path
from typing import override

from src_new.models import PhaseRun, SuiteTask
from src_new.utils import short_id

from src_new.lift.adapters.base import RunContext, RuntimeAdapter
from src_new.lift.adapters.environment import ExecutionEnvironment
from src_new.lift.eval.agent_pair import TaskAgentPairFactory
from src_new.lift.policies.artifact import ArtifactPolicy
from src_new.lift.runtime.delta_ref import DeltaRef
from src_new.lift.runtime.suite_run_resources import SuiteRunResources


class MockAdapter(RuntimeAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.produce_delta_count = 0
        self.before_load_count = 0
        self.after_load_count = 0
        self._last_warmup: list[SuiteTask] = []

    @override
    def create_agent_pair_factory(
        self,
        env: ExecutionEnvironment,
        ctx: RunContext,
        *,
        phase: str,
        workspace_dir: Path,
    ) -> TaskAgentPairFactory:
        _ = (env, ctx, phase, workspace_dir)
        raise NotImplementedError("MockAdapter does not run tasks")

    @override
    async def start_warmup_environment(
        self,
        ctx: RunContext,
        resources: SuiteRunResources,
        workspace_dir: Path,
    ) -> ExecutionEnvironment:
        _ = (ctx, resources, workspace_dir)
        raise NotImplementedError("MockAdapter does not run tasks")

    @override
    async def start_holdout_environment(
        self,
        ctx: RunContext,
        resources: SuiteRunResources,
        task: SuiteTask,
        workspace_dir: Path,
        *,
        image: str,
        seed_workspace: bool,
    ) -> ExecutionEnvironment:
        _ = (ctx, resources, task, workspace_dir, image, seed_workspace)
        raise NotImplementedError("MockAdapter does not run tasks")

    @override
    async def apply_evolve(self, env: ExecutionEnvironment, ctx: RunContext) -> None:
        _ = (env, ctx)
        raise NotImplementedError("MockAdapter does not run tasks")

    @override
    async def materialize_delta(
        self, env: ExecutionEnvironment, ctx: RunContext
    ) -> DeltaRef:
        _ = (env, ctx)
        raise NotImplementedError("MockAdapter does not run tasks")

    @override
    def baseline_image(self, ctx: RunContext) -> str:
        _ = ctx
        return "mock:base"

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
        _ = (resources, ctx)
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
        _ = (resources, delta, ctx)
        self.after_load_count += 1
        return PhaseRun(
            work_session_id=f"work-{short_id()}",
            judge_session_id=f"judge-{short_id()}",
            success=True,
            content_score=1.0,
            workspace_dir=f"/mock/evolved/{task.name}",
        )
