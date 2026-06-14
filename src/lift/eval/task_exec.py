"""Multi-task orchestration around the single-task ``run_task`` kernel."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import TypeVar

from src.config import LOGGER
from src.lift.eval.stage import SuiteRunPhase
from src.lift.eval.worker_judger import WorkerJudgerPairFactory
from src.lift.eval.run_task import run_task
from src.models import PhaseRun, SuiteTask

_T = TypeVar("_T")


async def bounded_gather(
    coros: Iterable[Awaitable[_T]],
    *,
    limit: int | None,
    return_exceptions: bool = False,
) -> list[_T]:
    """``asyncio.gather`` 加并发上限版本。

    - ``limit is None`` 或 ``limit <= 0``：行为同 ``asyncio.gather``（无上限）。
    - 其他情况：用 ``asyncio.Semaphore(limit)`` 包裹每个 coroutine，确保
      任意时刻至多 ``limit`` 个 coroutine 在执行（其余在 ``async with sem`` 处等待）。

    ``return_exceptions`` 透传给 ``asyncio.gather``：为 ``True`` 时单个协程抛异常
    不会取消其余协程，异常对象按位置写入返回列表（用于失败隔离）。

    返回顺序与输入顺序一致（与 ``asyncio.gather`` 一致）。
    """
    coros_list = list(coros)
    if not coros_list:
        return []
    if limit is None or limit <= 0 or limit >= len(coros_list):
        return list(
            await asyncio.gather(*coros_list, return_exceptions=return_exceptions)
        )

    sem = asyncio.Semaphore(limit)

    async def _bounded(coro: Awaitable[_T]) -> _T:
        async with sem:
            return await coro

    return list(
        await asyncio.gather(
            *[_bounded(c) for c in coros_list],
            return_exceptions=return_exceptions,
        )
    )


async def execute_task(
    *,
    task: SuiteTask,
    run_id: str,
    workspace_dir: Path,
    factory: WorkerJudgerPairFactory,
    run_phase: SuiteRunPhase,
    max_conversation_turns: int = 5,
) -> PhaseRun:
    """Run a single task via ``factory`` → ``run_task`` → ``PhaseRun``."""
    pair = factory(task)  # 每题新建 work/judge agent + 独立 Langfuse session id
    LOGGER.info(
        "Running %s %s: %s run_id=%s workspace=%s",
        run_phase.stage.value,
        run_phase.log_label,
        task.name,
        run_id,
        workspace_dir,
    )
    success, work_sid, judge_sid, content_score = await run_task(
        task,
        run_id,
        pair,
        max_conversation_turns=max_conversation_turns,
        is_evolve_turn=run_phase.is_evolve_turn,
        is_final_task=run_phase.is_final_task,
    )
    return PhaseRun(
        work_session_id=work_sid,
        judge_session_id=judge_sid,
        success=success,
        content_score=content_score,
        workspace_dir=str(workspace_dir.resolve()),
    )


async def execute_tasks(
    *,
    tasks: list[SuiteTask],
    run_id: str,
    workspace_dir: Path,
    factory: WorkerJudgerPairFactory,
    run_phase: SuiteRunPhase,
    parallel: bool,
    max_concurrent: int | None = None,
    max_conversation_turns: int = 5,
    on_task_done: Callable[[SuiteTask, PhaseRun], Awaitable[None]] | None = None,
) -> list[PhaseRun]:
    """Run multiple tasks; ``parallel`` 选并发 vs 串行；``max_concurrent`` 限并发上限。

    - ``parallel=False``：``for`` 顺序执行（``max_concurrent`` 被忽略）。
    - ``parallel=True``：``bounded_gather``；``max_concurrent`` 为 None / <=0 → 无上限。
    - ``on_task_done``：每道题完成后立刻 ``await`` 的钩子（在并发模式下，仍是该题
      自己的协程槽位内调用——共用同一并发上限）。常用于"每题独立 evolve"场景。
    """
    if not tasks:
        return []

    async def run_one(task: SuiteTask) -> PhaseRun:
        """单题包装：execute_task → 可选 on_task_done。"""
        result = await execute_task(
            task=task,
            run_id=run_id,
            workspace_dir=workspace_dir,
            factory=factory,
            run_phase=run_phase,
            max_conversation_turns=max_conversation_turns,
        )
        if on_task_done is not None:
            await on_task_done(task, result)
        return result

    if parallel:
        # 共享同一 factory/env/workspace：仅当 runtime 支持 warmup 多题并发时使用
        return await bounded_gather(
            (run_one(t) for t in tasks), limit=max_concurrent
        )
    results: list[PhaseRun] = []
    for task in tasks:
        results.append(await run_one(task))
    return results
