"""LIFT 主流程编排：repeat × suite → warmup/delta → hold-out 对照。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from src.config import LOGGER
from src.models import EvalRepeat, EvalReport, SuiteRun, TaskRun

from src.lift.adapters.base import AgentRuntimeAdapter, SuiteRunContext
from src.lift.eval.task_exec import bounded_gather, exc_summary as _exc_summary
from src.lift.status import events as status_events
from src.lift.policies.artifact import WarmupThenUpdatePolicy
from src.lift.pipeline.run_options import RunOptions
from src.lift.runtime.delta_ref import DeltaRef
from src.lift.runtime.suite_run_resources import SuiteRunResources
from src.lift.suite.holdout import split_suite_tasks
from src.lift.suite.lift_suite import load_lift_suite
from src.models import PhaseRun, SuiteTask
from src.paths import report_json_path, results_run_dir


def _fmt_optional_int(value: int | None) -> str:
    """``None`` / 非正整数视作 unlimited；正整数转字符串。"""
    if value is None or value <= 0:
        return "unlimited"
    return str(value)


def _build_run_params(
    *,
    options: RunOptions,
    suite_count: int,
    extra: tuple[tuple[str, str], ...] = (),
) -> tuple[tuple[str, str], ...]:
    """从 ``RunOptions`` 抽取关键参数，序列化成 ``(key, value)`` 对，
    用于 dashboard / TUI 顶部展示。``extra`` 由 CLI 追加（如 agent_runtime）。"""
    pairs: list[tuple[str, str]] = list(extra)
    pairs.extend(
        [
            ("suites", str(suite_count)),
            ("repeat", str(options.repeat)),
            ("warmup_only", str(options.warmup_only)),
            ("evaluate", str(options.evaluate)),
            ("max_parallel_repeats", _fmt_optional_int(options.max_parallel_repeats)),
            ("max_parallel_suites", _fmt_optional_int(options.max_parallel_suites)),
            ("max_concurrent_tasks", _fmt_optional_int(options.max_concurrent_tasks)),
            ("max_conversation_turns", str(options.max_conversation_turns)),
            ("warmup_container_policy", options.warmup_container_policy.value),
            ("holdout_container_policy", options.holdout_container_policy.value),
            ("holdout_phase_policy", options.holdout_phase_policy.value),
            ("container_memory", options.container_memory or "-"),
            ("container_cpus", options.container_cpus or "-"),
        ]
    )
    return tuple(pairs)


class LIFTPipeline:
    """Loaded Impact on Final Task orchestration."""

    def __init__(self) -> None:
        self._report_lock = asyncio.Lock()  # 并行 repeat 时保护 report 增量写入

    async def run(
        self,
        *,
        run_id: str,
        suite_paths: list[Path],
        adapter: AgentRuntimeAdapter,
        options: RunOptions,
        extra_params: tuple[tuple[str, str], ...] = (),
    ) -> EvalReport:
        """执行完整 LIFT 流程并写出 ``EvalReport`` JSON。

        ``extra_params`` 由调用方（如 CLI）追加，例如 ``agent_runtime`` /
        ``benchmark_dir``，用于在 dashboard / TUI 顶部展示。
        """
        eval_report = EvalReport(run_id=run_id)
        report_path = report_json_path(run_id)
        results_run_dir(run_id).mkdir(parents=True, exist_ok=True)
        eval_report.runs = [EvalRepeat() for _ in range(options.repeat)]

        # 广播整体执行计划：repeat 数 + suite 列表（题级骨架在 suite 加载后补全）
        status_events.emit_run_plan(
            run_id=run_id,
            repeats=options.repeat,
            suite_names=tuple(p.stem for p in suite_paths),
            params=_build_run_params(
                options=options,
                suite_count=len(suite_paths),
                extra=extra_params,
            ),
        )

        # 多 repeat 默认并行；max_parallel_repeats=1 时串行；否则受其值上限约束
        if options.repeat > 1 and options.max_parallel_repeats != 1:
            await bounded_gather(
                (
                    self._run_suites(
                        repeat_index=i,
                        run_id=run_id,
                        suite_paths=suite_paths,
                        adapter=adapter,
                        options=options,
                        eval_report=eval_report,
                        report_path=report_path,
                    )
                    for i in range(options.repeat)
                ),
                limit=options.max_parallel_repeats,
            )
        else:
            for repeat_index in range(options.repeat):
                await self._run_suites(
                    repeat_index=repeat_index,
                    run_id=run_id,
                    suite_paths=suite_paths,
                    adapter=adapter,
                    options=options,
                    eval_report=eval_report,
                    report_path=report_path,
                )

        eval_report.completed_at = datetime.now(timezone.utc).isoformat()
        async with self._report_lock:
            eval_report.write_json(report_path)
        LOGGER.info("LIFT report written: %s", report_path)
        return eval_report

    async def _run_suites(
        self,
        *,
        repeat_index: int,
        run_id: str,
        suite_paths: list[Path],
        adapter: AgentRuntimeAdapter,
        options: RunOptions,
        eval_report: EvalReport,
        report_path: Path,
    ) -> None:
        """单轮 repeat 内跑所有 suite（warmup → hold-out）。

        suite 间默认并发（``max_parallel_suites`` 控制上限）；每个 suite 拥有独立的
        ``SuiteRunResources``（容器、delta 镜像），互不干扰。``repeat_run.suites``
        先按输入顺序占位，再由各 suite 协程按索引回填，保证报告顺序稳定。

        失败隔离：并发 gather 用 ``return_exceptions=True``，单个 suite 抛异常不会
        取消其余 suite。首轮失败的 suite 会被收集起来放到队列最后**重跑一次**；
        重跑仍失败则记录错误并保留占位（该 suite 在报告中缺最终结果）。
        """
        LOGGER.info(
            "LIFT repeat %d/%d run_id=%s",
            repeat_index + 1,
            options.repeat,
            run_id,
        )
        status_events.emit_stage(
            kind="repeat", status="running", run_id=run_id, repeat_index=repeat_index
        )
        repeat_run = eval_report.runs[repeat_index]
        # 先占位：并发回填时按索引写入，避免 append 顺序随完成时间错乱
        repeat_run.suites = [None] * len(suite_paths)  # type: ignore[list-item]

        async def _attempt(indexed: list[tuple[int, Path]]) -> list[int]:
            """跑一批 suite，返回抛异常的 suite 索引列表（失败隔离）。"""
            results = await bounded_gather(
                (
                    self._run_one_suite(
                        suite_index=idx,
                        suite_path=suite_path,
                        repeat_index=repeat_index,
                        repeat_run=repeat_run,
                        run_id=run_id,
                        adapter=adapter,
                        options=options,
                        eval_report=eval_report,
                        report_path=report_path,
                    )
                    for idx, suite_path in indexed
                ),
                limit=options.max_parallel_suites,
                return_exceptions=True,
            )
            failed: list[int] = []
            for (idx, suite_path), result in zip(indexed, results):
                if isinstance(result, BaseException):
                    failed.append(idx)
                    LOGGER.error(
                        "LIFT suite failed run_id=%s repeat=%d suite=%s: %r",
                        run_id,
                        repeat_index,
                        suite_path.name,
                        result,
                    )
            return failed

        indexed_suites = list(enumerate(suite_paths))
        failed_indices = await _attempt(indexed_suites)

        # 失败的 suite 放队列最后重跑一次
        if failed_indices:
            retry_indexed = [(idx, suite_paths[idx]) for idx in failed_indices]
            LOGGER.info(
                "LIFT retrying %d failed suite(s) run_id=%s repeat=%d: %s",
                len(retry_indexed),
                run_id,
                repeat_index,
                ", ".join(p.name for _, p in retry_indexed),
            )
            still_failed = await _attempt(retry_indexed)
            for idx in still_failed:
                LOGGER.error(
                    "LIFT suite failed after retry run_id=%s repeat=%d suite=%s",
                    run_id,
                    repeat_index,
                    suite_paths[idx].name,
                )

        repeat_run.completed_at = datetime.now(timezone.utc).isoformat()
        status_events.emit_stage(
            kind="repeat", status="done", run_id=run_id, repeat_index=repeat_index
        )

    async def _run_one_suite(
        self,
        *,
        suite_index: int,
        suite_path: Path,
        repeat_index: int,
        repeat_run: EvalRepeat,
        run_id: str,
        adapter: AgentRuntimeAdapter,
        options: RunOptions,
        eval_report: EvalReport,
        report_path: Path,
    ) -> None:
        """跑单个 suite：warmup → produce_delta → hold-out 对照，结束清理资源。"""
        suite = load_lift_suite(suite_path)
        warmup_tasks, holdout_tasks = split_suite_tasks(suite)
        category_name = suite.category

        suite_run = SuiteRun(
            suite_name=suite.name,
            suite_path=str(suite_path.resolve()),
            category=category_name,
            tasks=[],
        )
        repeat_run.suites[suite_index] = suite_run

        # suite 加载后广播题级骨架与 suite 开始
        status_events.emit_suite_plan(
            run_id=run_id,
            repeat_index=repeat_index,
            suite_index=suite_index,
            suite_name=suite.name,
            warmup_task_names=tuple(t.name for t in warmup_tasks),
            holdout_task_names=tuple(t.name for t in holdout_tasks),
        )
        status_events.emit_stage(
            kind="suite",
            status="running",
            run_id=run_id,
            repeat_index=repeat_index,
            suite_index=suite_index,
            suite_name=suite.name,
        )

        ctx = SuiteRunContext(
            run_id=run_id,
            repeat_index=repeat_index,
            suite_index=suite_index,
            suite_path=suite_path,
            category_name=category_name,
            suite_name=suite.name,
        )
        # 本 suite 的资源簿：track 容器、存 delta；suite 结束 finally 里 cleanup
        resources = await adapter.create_suite_run_resources(ctx)
        try:
            async with self._report_lock:
                if category_name not in eval_report.categories:
                    eval_report.categories.append(category_name)

            if not warmup_tasks:
                raise ValueError(
                    f"No warmup tasks in {suite_path}; "
                    "produce_delta requires at least one non-hold-out task"
                )

            policy = WarmupThenUpdatePolicy(warmup_tasks=warmup_tasks)
            # warmup 容器在 produce_delta 内部已 cleanup；delta 镜像留给 hold-out
            status_events.emit_stage(
                kind="warmup",
                status="running",
                run_id=run_id,
                repeat_index=repeat_index,
                suite_index=suite_index,
                suite_name=suite.name,
            )
            delta = await adapter.produce_delta(resources, policy, warmup_tasks, ctx)
            status_events.emit_stage(
                kind="warmup",
                status="done",
                run_id=run_id,
                repeat_index=repeat_index,
                suite_index=suite_index,
                suite_name=suite.name,
            )

            if options.warmup_only:
                # 只产 delta，不跑 before/after-load 对照
                LOGGER.info(
                    "LIFT warmup-only %s: delta committed as %s",
                    suite.name,
                    delta.image_tag,
                )
            else:
                task_runs = await self._run_holdout_tasks(
                    adapter=adapter,
                    holdout_tasks=holdout_tasks,
                    resources=resources,
                    delta=delta,
                    ctx=ctx,
                    suite_index=suite_index,
                    category_name=category_name,
                    options=options,
                )
                suite_run.tasks.extend(task_runs)

            # 每个 suite 完成后落盘：长跑中断时仍可从磁盘恢复部分 report
            async with self._report_lock:
                eval_report.write_json(report_path)
            status_events.emit_stage(
                kind="suite",
                status="done",
                run_id=run_id,
                repeat_index=repeat_index,
                suite_index=suite_index,
                suite_name=suite.name,
            )
        except BaseException as exc:
            status_events.emit_stage(
                kind="suite",
                status="failed",
                run_id=run_id,
                repeat_index=repeat_index,
                suite_index=suite_index,
                suite_name=suite.name,
                detail=_exc_summary(exc),
            )
            raise
        finally:
            # 删本 suite 登记的容器；delta 镜像也在 resources.cleanup 里 rmi
            await resources.cleanup()

    async def _run_holdout_tasks(
        self,
        *,
        adapter: AgentRuntimeAdapter,
        holdout_tasks: list[SuiteTask],
        resources: SuiteRunResources,
        delta: DeltaRef,
        ctx: SuiteRunContext,
        suite_index: int,
        category_name: str,
        options: RunOptions,
    ) -> list[TaskRun]:
        """按 ``holdout_container_policy`` 串行 / 并行执行 hold-out 多题。

        ``holdout_phase_policy`` 控制单 task 内 baseline / evolved 是否并行
        （二者镜像与 workspace 子目录互不依赖，并行后单题最多有 2 个容器存活）。

        失败处理（核心约定）：

        - **judge ``success=False`` 不算失败**：``run_task`` 内部已多轮重试到
          ``max_conversation_turns``，``PhaseRun`` 正常返回 ``success=False`` +
          ``content_score``；这种情况下 phase 仍 emit ``done``（detail 带 score），
          dashboard 显示绿点而非 ✗。
        - **真正的异常**（容器/网络/agent runtime 异常）才视作 phase 失败：
          phase 内部**原地重试一次**，emit ``retrying`` 中间态；二次仍失败才
          emit ``failed`` 并向上抛。
        - **baseline / evolved 互不连坐**：phase parallel 时用
          ``return_exceptions=True`` 隔离，一边失败不取消另一边。
        - **task 间隔离**：单题最终失败不取消同 suite 内的其它 task；
          单题级失败仍可被 suite 重试（pipeline 上层）兜住，但其它 task 至少能跑完。
        """

        def _phase(
            task_name: str,
            phase: str,
            status: str,
            *,
            detail: str | None = None,
            score: float | None = None,
            success: bool | None = None,
            turns: int | None = None,
        ) -> None:
            status_events.emit_stage(
                kind="phase",
                status=status,
                run_id=ctx.run_id,
                repeat_index=ctx.repeat_index,
                suite_index=suite_index,
                suite_name=ctx.suite_name,
                task_name=task_name,
                phase=phase,
                detail=detail,
                score=score,
                success=success,
                turns=turns,
            )

        async def _run_phase(
            task: SuiteTask,
            phase: str,
            runner,  # async () -> PhaseRun
        ) -> PhaseRun:
            """跑一个 phase（baseline 或 evolved），异常时**原地重试一次**。

            judge ``success=False`` 不抛异常，phase 始终 emit ``done`` +
            score detail；只有 runner 抛异常才会触发重试 / 最终 ``failed``。
            """
            _phase(task.name, phase, "running")
            last_exc: BaseException | None = None
            for attempt in range(2):
                try:
                    result = await runner()
                except BaseException as exc:
                    last_exc = exc
                    if attempt == 0:
                        _phase(
                            task.name, phase, "retrying",
                            detail=f"retry after: {_exc_summary(exc)}",
                        )
                        continue
                    _phase(task.name, phase, "failed", detail=_exc_summary(exc))
                    raise
                # 成功路径（含 judge fail）：phase 视为完成
                if result.success:
                    _phase(
                        task.name, phase, "done",
                        score=result.content_score, success=True,
                        turns=result.turns,
                    )
                else:
                    _phase(
                        task.name, phase, "done",
                        detail=f"judge fail (score={result.content_score:.2f})",
                        score=result.content_score, success=False,
                        turns=result.turns,
                    )
                return result
            raise last_exc  # type: ignore[misc]

        async def _before(task: SuiteTask) -> PhaseRun:
            return await _run_phase(
                task, "baseline",
                lambda: adapter.run_before_load(task, resources, ctx),
            )

        async def _after(task: SuiteTask) -> PhaseRun:
            return await _run_phase(
                task, "evolved",
                lambda: adapter.run_after_load(task, resources, delta, ctx),
            )

        async def _one_task(task: SuiteTask) -> TaskRun:
            status_events.emit_stage(
                kind="task",
                status="running",
                run_id=ctx.run_id,
                repeat_index=ctx.repeat_index,
                suite_index=suite_index,
                suite_name=ctx.suite_name,
                task_name=task.name,
            )
            try:
                if options.holdout_phase_policy.phases_parallel:
                    # 关键：return_exceptions=True 让 baseline 和 evolved 互不连坐
                    baseline_r, evolved_r = await asyncio.gather(
                        _before(task),
                        _after(task),
                        return_exceptions=True,
                    )
                    errs = [
                        r for r in (baseline_r, evolved_r)
                        if isinstance(r, BaseException)
                    ]
                    if errs:
                        # 任一边最终失败 → task 失败抛出（phase 内部重试已用过）
                        raise errs[0]
                    baseline, evolved = baseline_r, evolved_r  # type: ignore[assignment]
                else:
                    baseline = await _before(task)
                    evolved = await _after(task)
            except BaseException as exc:
                status_events.emit_stage(
                    kind="task",
                    status="failed",
                    run_id=ctx.run_id,
                    repeat_index=ctx.repeat_index,
                    suite_index=suite_index,
                    suite_name=ctx.suite_name,
                    task_name=task.name,
                    detail=_exc_summary(exc),
                )
                raise
            LOGGER.info(
                "LIFT hold-out %s: baseline_success=%s evolved_success=%s",
                task.name,
                baseline.success,
                evolved.success,
            )
            status_events.emit_stage(
                kind="task",
                status="done",
                run_id=ctx.run_id,
                repeat_index=ctx.repeat_index,
                suite_index=suite_index,
                suite_name=ctx.suite_name,
                task_name=task.name,
            )
            return TaskRun(
                task_name=task.name,
                category=category_name,
                baseline=baseline,
                evolved=evolved,
            )

        if options.holdout_container_policy.tasks_parallel:
            # 题间隔离：单题最终失败不取消同 suite 兄弟题
            results = await bounded_gather(
                (_one_task(t) for t in holdout_tasks),
                limit=options.max_concurrent_tasks,
                return_exceptions=True,
            )
            return [r for r in results if isinstance(r, TaskRun)]
        # 串行：单题失败也不中止后续题（保留隔离语义一致性）
        out: list[TaskRun] = []
        for t in holdout_tasks:
            try:
                out.append(await _one_task(t))
            except BaseException as exc:  # noqa: BLE001
                LOGGER.error(
                    "LIFT hold-out task failed (serial isolated) suite=%s task=%s: %r",
                    ctx.suite_name, t.name, exc,
                )
        return out
