"""Multi-task orchestration around the single-task ``run_task`` kernel."""

from __future__ import annotations

import asyncio
from pathlib import Path

from src_new.config import LOGGER
from src_new.lift.eval.worker_judger import WorkerJudgerPairFactory
from src_new.lift.eval.run_task import run_task
from src_new.models import PhaseRun, SuiteTask


async def execute_phase(
    *,
    task: SuiteTask,
    run_id: str,
    workspace_dir: Path,
    factory: WorkerJudgerPairFactory,
    phase: str = "task",
    is_evolve_turn: bool = False,
    is_final_task: bool = False,
    log_label: str = "task",
) -> PhaseRun:
    """Run a single task via ``factory`` → ``run_task`` → ``PhaseRun``."""
    pair = factory(task)
    LOGGER.info(
        "Running %s %s: %s run_id=%s workspace=%s",
        phase,
        log_label,
        task.name,
        run_id,
        workspace_dir,
    )
    success, work_sid, judge_sid, content_score = await run_task(
        task,
        run_id,
        pair,
        is_evolve_turn=is_evolve_turn,
        is_final_task=is_final_task,
    )
    return PhaseRun(
        work_session_id=work_sid,
        judge_session_id=judge_sid,
        success=success,
        content_score=content_score,
        workspace_dir=str(workspace_dir.resolve()),
    )


async def execute_phase_batch(
    *,
    tasks: list[SuiteTask],
    run_id: str,
    workspace_dir: Path,
    factory: WorkerJudgerPairFactory,
    parallel: bool,
    phase: str = "task",
    is_evolve_turn: bool = False,
    is_final_task: bool = False,
    log_label: str = "task",
) -> list[PhaseRun]:
    """Run multiple tasks; ``parallel`` selects serial ``for`` vs ``asyncio.gather``."""
    if not tasks:
        return []

    async def run_one(task: SuiteTask) -> PhaseRun:
        return await execute_phase(
            task=task,
            run_id=run_id,
            workspace_dir=workspace_dir,
            factory=factory,
            phase=phase,
            is_evolve_turn=is_evolve_turn,
            is_final_task=is_final_task,
            log_label=log_label,
        )

    if parallel:
        return list(await asyncio.gather(*[run_one(t) for t in tasks]))
    results: list[PhaseRun] = []
    for task in tasks:
        results.append(await run_one(task))
    return results
