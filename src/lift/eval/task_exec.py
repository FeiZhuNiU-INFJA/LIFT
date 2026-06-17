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


def exc_summary(exc: BaseException, *, max_len: int = 240) -> str:
    """把异常压缩成一行摘要，喂给 ``emit_stage(detail=...)`` 展示在 dashboard。

    形如 ``"RuntimeError: container ... is not running"``，超长截断保护
    SSE / TUI 渲染。``CancelledError`` 是上游 fail-fast 取消（如 phase
    parallel 时另一边失败），单独标记便于和真正错误区分。
    """
    if isinstance(exc, asyncio.CancelledError):
        return "CancelledError: cancelled by sibling failure"
    name = type(exc).__name__
    msg = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    text = f"{name}: {msg}" if msg else name
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


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
    success, work_sid, judge_sid, content_score, turns = await run_task(
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
        turns=turns,
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
    on_task_status: Callable[[SuiteTask, str, str | None], None] | None = None,
    retry_each: bool = False,
    tasks_isolated: bool = False,
) -> list[PhaseRun]:
    """Run multiple tasks; ``parallel`` 选并发 vs 串行；``max_concurrent`` 限并发上限。

    - ``parallel=False``：``for`` 顺序执行（``max_concurrent`` 被忽略）。
    - ``parallel=True``：``bounded_gather``；``max_concurrent`` 为 None / <=0 → 无上限。
    - ``on_task_done``：每道题完成后立刻 ``await`` 的钩子（在并发模式下，仍是该题
      自己的协程槽位内调用——共用同一并发上限）。常用于"每题独立 evolve"场景。
    - ``on_task_status(task, status, detail)``: 单题状态回调（同步）；调用时机：
      ``running``（每次 attempt 开始）、``retrying``（首次失败要重试）、
      ``done``（成功，``detail`` 含 judge fail score 时形如 ``"judge fail (score=0.42)"``）、
      ``failed``（最终失败）。供 adapter 转发到 status 事件总线驱动 dashboard
      题级渲染。``retry_each=False`` 时不会出现 ``retrying`` 状态。
    - ``retry_each``: 单题异常时**原地重试一次**（仅对抛异常的 PhaseRun 路径生效；
      judge ``success=False`` 不算失败、不会触发重试）。第二次仍异常才向上抛。
    - ``tasks_isolated``: 题间隔离——并发模式下让 ``bounded_gather`` 用
      ``return_exceptions=True``，单题最终失败不会取消其它兄弟题；返回列表中失败位
      的元素是异常对象，调用方需要自行过滤。串行模式下表现为：失败题不中止后续题，
      仅 LOGGER 记录后跳过。
    """
    if not tasks:
        return []

    def _emit(task: SuiteTask, status: str, detail: str | None = None) -> None:
        if on_task_status is None:
            return
        try:
            on_task_status(task, status, detail)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("on_task_status callback failed: %r", exc)

    async def run_one(task: SuiteTask) -> PhaseRun:
        """单题包装：execute_task → 可选 on_task_done；可选异常重试一次。"""
        attempts = 2 if retry_each else 1
        last_exc: BaseException | None = None
        for attempt in range(attempts):
            _emit(task, "running")
            try:
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
                # judge fail 不当作失败：仍发 done，把 score 放到 detail 里给 hover 看
                if result.success:
                    _emit(task, "done")
                else:
                    _emit(
                        task, "done",
                        f"judge fail (score={result.content_score:.2f})",
                    )
                return result
            except BaseException as exc:  # noqa: BLE001
                last_exc = exc
                if attempt + 1 < attempts:
                    LOGGER.warning(
                        "Task %s failed (attempt %d/%d): %r — retrying once",
                        task.name, attempt + 1, attempts, exc,
                    )
                    _emit(
                        task, "retrying",
                        f"retry after: {exc_summary(exc)}",
                    )
                    continue
                _emit(task, "failed", exc_summary(exc))
                raise
        # 不可达：循环不是 break 就是 return / raise
        raise last_exc  # type: ignore[misc]

    if parallel:
        # 共享同一 factory/env/workspace：仅当 runtime 支持 warmup 多题并发时使用
        return await bounded_gather(
            (run_one(t) for t in tasks),
            limit=max_concurrent,
            return_exceptions=tasks_isolated,
        )
    results: list[PhaseRun] = []
    for task in tasks:
        try:
            results.append(await run_one(task))
        except BaseException as exc:  # noqa: BLE001
            if not tasks_isolated:
                raise
            LOGGER.error("Task %s failed in serial mode (isolated): %r", task.name, exc)
            results.append(exc)  # type: ignore[arg-type]
    return results
