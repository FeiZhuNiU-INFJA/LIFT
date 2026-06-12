"""Mock AgentRuntimeAdapter for LIFT pipeline unit tests.

用于 LIFT 流水线单元测试的 AgentRuntimeAdapter 模拟实现。
"""

from __future__ import annotations

from pathlib import Path
from typing import override

from src.models import PhaseRun, SuiteTask
from src.utils import short_id

from src.lift.adapters.base import AgentRuntimeAdapter, SuiteRunContext
from src.lift.adapters.environment import ExecutionEnvironment
from src.lift.eval.stage import HoldoutLoadState, SuiteRunPhase
from src.lift.eval.worker_judger import WorkerJudgerPairFactory
from src.lift.policies.artifact import ArtifactPolicy
from src.lift.runtime.delta_ref import DeltaRef
from src.lift.runtime.suite_run_resources import SuiteRunResources


class MockAdapter(AgentRuntimeAdapter):
    """Minimal adapter stub that records pipeline callbacks without running real tasks.

    最小化适配器桩：记录流水线回调计数，不执行真实任务。
    """

    def __init__(self) -> None:
        """Initialize counters and warmup task cache / 初始化计数器与 warmup 任务缓存。"""
        super().__init__()
        #: Number of times ``produce_delta`` was invoked / ``produce_delta`` 调用次数。
        self.produce_delta_count = 0
        #: Number of times ``run_before_load`` was invoked / ``run_before_load`` 调用次数。
        self.before_load_count = 0
        #: Number of times ``run_after_load`` was invoked / ``run_after_load`` 调用次数。
        self.after_load_count = 0
        #: Warmup tasks passed to the most recent ``produce_delta`` call /
        #: 最近一次 ``produce_delta`` 收到的 warmup 任务列表。
        self._last_warmup: list[SuiteTask] = []

    @override
    def worker_judger_factory(
        self,
        env: ExecutionEnvironment,
        ctx: SuiteRunContext,
        *,
        run_phase: SuiteRunPhase,
        workspace_dir: Path,
    ) -> WorkerJudgerPairFactory:
        """Not implemented; MockAdapter does not run tasks / 未实现，MockAdapter 不执行任务。"""
        _ = (env, ctx, run_phase, workspace_dir)
        raise NotImplementedError("MockAdapter does not run tasks")

    @override
    async def start_warmup_environment(
        self,
        ctx: SuiteRunContext,
        resources: SuiteRunResources,
        workspace_dir: Path,
    ) -> ExecutionEnvironment:
        """Not implemented; MockAdapter does not run tasks / 未实现，MockAdapter 不执行任务。"""
        _ = (ctx, resources, workspace_dir)
        raise NotImplementedError("MockAdapter does not run tasks")

    @override
    async def start_holdout_environment(
        self,
        ctx: SuiteRunContext,
        resources: SuiteRunResources,
        task: SuiteTask,
        workspace_dir: Path,
        *,
        image: str,
        seed_workspace: bool,
        load_state: HoldoutLoadState,
    ) -> ExecutionEnvironment:
        """Not implemented; MockAdapter does not run tasks / 未实现，MockAdapter 不执行任务。"""
        _ = (ctx, resources, task, workspace_dir, image, seed_workspace, load_state)
        raise NotImplementedError("MockAdapter does not run tasks")

    @override
    async def apply_evolve(self, env: ExecutionEnvironment, ctx: SuiteRunContext) -> None:
        """Not implemented; MockAdapter does not run tasks / 未实现，MockAdapter 不执行任务。"""
        _ = (env, ctx)
        raise NotImplementedError("MockAdapter does not run tasks")

    @override
    async def materialize_delta(
        self, env: ExecutionEnvironment, ctx: SuiteRunContext
    ) -> DeltaRef:
        """Not implemented; MockAdapter does not run tasks / 未实现，MockAdapter 不执行任务。"""
        _ = (env, ctx)
        raise NotImplementedError("MockAdapter does not run tasks")

    @override
    def baseline_image(self, ctx: SuiteRunContext) -> str:
        """Return a fixed mock baseline image tag / 返回固定的 mock 基线镜像标签。"""
        _ = ctx
        return "mock:base"

    @override
    async def produce_delta(
        self,
        resources: SuiteRunResources,
        policy: ArtifactPolicy,
        warmup_tasks: list[SuiteTask],
        ctx: SuiteRunContext,
    ) -> DeltaRef:
        """Record delta production, stash warmup tasks, and attach delta to resources.

        记录 delta 生成、保存 warmup 任务，并将 delta 写入 resources。
        """
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
        ctx: SuiteRunContext,
    ) -> PhaseRun:
        """Return a successful baseline PhaseRun and increment ``before_load_count``.

        返回成功的 baseline PhaseRun，并递增 ``before_load_count``。
        """
        _ = (resources, ctx)
        self.before_load_count += 1
        return PhaseRun(
            work_session_id=f"work-{short_id()}",
            judge_session_id=f"judge-{short_id()}",
            success=True,
            content_score=1.0,
            workspace_dir=f"/mock/{HoldoutLoadState.BASELINE.value}/{task.name}",
        )

    @override
    async def run_after_load(
        self,
        task: SuiteTask,
        resources: SuiteRunResources,
        delta: DeltaRef,
        ctx: SuiteRunContext,
    ) -> PhaseRun:
        """Return a successful evolved PhaseRun and increment ``after_load_count``.

        返回成功的 evolved PhaseRun，并递增 ``after_load_count``。
        """
        _ = (resources, delta, ctx)
        self.after_load_count += 1
        return PhaseRun(
            work_session_id=f"work-{short_id()}",
            judge_session_id=f"judge-{short_id()}",
            success=True,
            content_score=1.0,
            workspace_dir=f"/mock/evolved/{task.name}",
        )
