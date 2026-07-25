"""LIFT 主流程编排：repeat × suite → warmup/delta → holdout 对照。"""

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


def _is_cell_complete(
    suite: SuiteRun | None,
    warmup_only: bool,
    expected_holdout_count: int | None = None,
) -> bool:
    """判断 (repeat, suite) cell 是否已跑完,可跳过。

    - ``warmup_only``: suite 有值即视为完成(produce_delta 成功过就写入了 suite)
    - 完整模式: 要求 tasks 非空、baseline/evolved 都非空,且当传入
      ``expected_holdout_count`` 时 task 数必须等于期望值——避免有 task
      在异常里被 gather 过滤掉,残缺 cell 被误判成完成。
    """
    if suite is None:
        return False
    if warmup_only:
        return True
    if not suite.tasks:
        return False
    if (
        expected_holdout_count is not None
        and len(suite.tasks) != expected_holdout_count
    ):
        return False
    return all(t.baseline is not None and t.evolved is not None for t in suite.tasks)


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
        # 占位：cell 并发回填时按 (repeat, suite) 索引写入，避免 append 顺序乱
        for repeat_run in eval_report.runs:
            repeat_run.suites = [None] * len(suite_paths)  # type: ignore[list-item]
        # 快照关键运行参数,供 --resume 时自动恢复未显式传入的 CLI 参数
        eval_report.run_options = {
            **{k: v for k, v in extra_params},
            **options.model_dump(mode="json"),
        }

        # 预加载所有 suite 算 holdout 静态总数；放进 params 供 dashboard 渲染
        # "X of Y"，避免分母随 suite 陆续 plan 而动态增长。
        # 同时收集每个 suite 的 viz name（取 JSON 内 ``name`` 字段，与容器层
        # ``ContainerInfo.suite_name`` 保持一致；预扫描失败回落到文件 stem）。
        # 副产品:每个 suite 的期望 holdout 数,给 resume 用于识别残缺 cell。
        holdout_total = 0
        viz_suite_names: list[str] = []
        suite_holdout_counts: list[int | None] = []
        for p in suite_paths:
            try:
                suite = load_lift_suite(p)
                _, holdouts = split_suite_tasks(suite)
                holdout_total += len(holdouts)
                viz_suite_names.append(suite.name or p.stem)
                suite_holdout_counts.append(len(holdouts))
            except Exception:  # noqa: BLE001 — 预扫描失败不阻塞 run
                LOGGER.warning("preload holdout count failed: %s", p, exc_info=True)
                viz_suite_names.append(p.stem)
                suite_holdout_counts.append(None)
        holdout_total *= options.repeat

        # 断点续跑：读旧 report,把已完整的 (repeat, suite) cell 预填回 eval_report
        # 并从待跑 cells 列表里剔除。半成品 / 缺失 cell 一律重跑(delta 已 rmi)。
        resumed_cells: set[tuple[int, int]] = set()
        if options.resume and report_path.exists():
            try:
                prev = EvalReport.from_json_file(report_path)
                for r_idx, prev_run in enumerate(prev.runs or []):
                    if r_idx >= options.repeat:
                        break
                    for s_idx, prev_suite in enumerate(prev_run.suites or []):
                        if s_idx >= len(suite_paths):
                            break
                        expected = (
                            suite_holdout_counts[s_idx]
                            if s_idx < len(suite_holdout_counts)
                            else None
                        )
                        if _is_cell_complete(
                            prev_suite, options.warmup_only, expected
                        ):
                            eval_report.runs[r_idx].suites[s_idx] = prev_suite
                            resumed_cells.add((r_idx, s_idx))
                # 保留旧的 categories 顺序,避免续跑时因分类顺序变动导致 dashboard 抖
                for cat in prev.categories or []:
                    if cat not in eval_report.categories:
                        eval_report.categories.append(cat)
                LOGGER.info(
                    "LIFT resume: reusing %d already-complete cell(s) from %s",
                    len(resumed_cells),
                    report_path,
                )
            except Exception:  # noqa: BLE001 — 解析失败时干净重跑
                LOGGER.warning(
                    "LIFT resume: failed to load %s, starting fresh",
                    report_path, exc_info=True,
                )
                resumed_cells.clear()
        elif options.resume:
            LOGGER.info(
                "LIFT resume: no existing report at %s, starting fresh",
                report_path,
            )


        # 广播整体执行计划：repeat 数 + suite 列表（题级骨架在 suite 加载后补全）
        status_events.emit_run_plan(
            run_id=run_id,
            repeats=options.repeat,
            suite_names=tuple(viz_suite_names),
            params=_build_run_params(
                options=options,
                suite_count=len(suite_paths),
                extra=(*extra_params, ("holdout_total", str(holdout_total))),
            ),
        )

        # 单层 cell 级并发：repeat × suite 笛卡尔积铺平后用同一个 limit 限流。
        # 失败 cell 全局收集，最后用同 limit 统一重跑一次。
        # ``--resume`` 时跳过 ``resumed_cells`` 中已完整的 (repeat, suite) 对。
        cells: list[tuple[int, int, Path]] = [
            (r, s, suite_paths[s])
            for r in range(options.repeat)
            for s in range(len(suite_paths))
            if (r, s) not in resumed_cells
        ]
        if options.resume and resumed_cells:
            LOGGER.info(
                "LIFT resume: skipping %d cell(s), scheduling %d cell(s)",
                len(resumed_cells),
                len(cells),
            )
            # 把已完成 cell 的骨架 + 状态 emit 给事件总线,让 dashboard/TUI 立刻
            # 显示为 done(否则跳过的 cell 一直是 pending 灰圈)。
            self._replay_resumed_cells(
                run_id=run_id,
                resumed_cells=resumed_cells,
                eval_report=eval_report,
            )
        failed = await self._run_cells(
            cells=cells,
            run_id=run_id,
            adapter=adapter,
            options=options,
            eval_report=eval_report,
            report_path=report_path,
        )
        if failed:
            LOGGER.info(
                "LIFT retrying %d failed cell(s) run_id=%s: %s",
                len(failed),
                run_id,
                ", ".join(f"r{r}/{p.name}" for r, _, p in failed),
            )
            still_failed = await self._run_cells(
                cells=failed,
                run_id=run_id,
                adapter=adapter,
                options=options,
                eval_report=eval_report,
                report_path=report_path,
            )
            for r, _, p in still_failed:
                LOGGER.error(
                    "LIFT cell failed after retry run_id=%s repeat=%d suite=%s",
                    run_id, r, p.name,
                )

        completed_at = datetime.now(timezone.utc).isoformat()
        for repeat_run in eval_report.runs:
            repeat_run.completed_at = completed_at
        eval_report.completed_at = completed_at
        async with self._report_lock:
            eval_report.write_json(report_path)
        LOGGER.info("LIFT report written: %s", report_path)
        return eval_report

    def _replay_resumed_cells(
        self,
        *,
        run_id: str,
        resumed_cells: set[tuple[int, int]],
        eval_report: EvalReport,
    ) -> None:
        """把 ``--resume`` 跳过的 cell 的完成状态 emit 给事件总线。

        与 ``src.lift.status.replay.replay_report_into_bus`` 的 evaluate-only
        路径同理:先补 ``suite_plan`` 骨架(告诉 tracker 这个 cell 有哪些 holdout
        task),再逐级 emit ``suite`` / ``warmup`` / ``task`` / ``phase`` 的
        ``done`` 事件,dashboard 里对应格子立刻显示绿点 + 分数。warmup 题不入
        report,补一条 ``warmup=done`` 让 suite 汇总不因 ``warmup_status=pending``
        整格停在 pending。
        """
        for (r_idx, s_idx) in sorted(resumed_cells):
            suite_run = eval_report.runs[r_idx].suites[s_idx]
            if suite_run is None:
                continue
            holdout_names = tuple(t.task_name for t in suite_run.tasks)
            status_events.emit_suite_plan(
                run_id=run_id,
                repeat_index=r_idx,
                suite_index=s_idx,
                suite_name=suite_run.suite_name,
                warmup_task_names=(),
                holdout_task_names=holdout_names,
            )
            status_events.emit_stage(
                kind="warmup", status="done",
                run_id=run_id, repeat_index=r_idx,
                suite_index=s_idx, suite_name=suite_run.suite_name,
            )
            for task in suite_run.tasks:
                for phase_name, phase in (
                    ("baseline", task.baseline),
                    ("evolved", task.evolved),
                ):
                    if phase is None:
                        continue
                    status_events.emit_stage(
                        kind="phase", status="done",
                        run_id=run_id, repeat_index=r_idx,
                        suite_index=s_idx, suite_name=suite_run.suite_name,
                        task_name=task.task_name, phase=phase_name,
                        score=phase.content_score, success=phase.success,
                        turns=phase.turns or None,
                        tool_calls=phase.tool_calls,
                    )
                status_events.emit_stage(
                    kind="task", status="done",
                    run_id=run_id, repeat_index=r_idx,
                    suite_index=s_idx, suite_name=suite_run.suite_name,
                    task_name=task.task_name,
                )
            status_events.emit_stage(
                kind="suite", status="done",
                run_id=run_id, repeat_index=r_idx,
                suite_index=s_idx, suite_name=suite_run.suite_name,
            )

    async def _run_cells(
        self,
        *,
        cells: list[tuple[int, int, Path]],
        run_id: str,
        adapter: AgentRuntimeAdapter,
        options: RunOptions,
        eval_report: EvalReport,
        report_path: Path,
    ) -> list[tuple[int, int, Path]]:
        """跑一批 cell（``(repeat_index, suite_index, suite_path)``），返回失败列表。

        cell 间隔离：``return_exceptions=True`` 让单个 cell 抛异常不会取消其余。
        """
        results = await bounded_gather(
            (
                self._run_one_suite(
                    suite_index=suite_index,
                    suite_path=suite_path,
                    repeat_index=repeat_index,
                    repeat_run=eval_report.runs[repeat_index],
                    run_id=run_id,
                    adapter=adapter,
                    options=options,
                    eval_report=eval_report,
                    report_path=report_path,
                )
                for repeat_index, suite_index, suite_path in cells
            ),
            limit=options.max_parallel_suites,
            return_exceptions=True,
        )
        failed: list[tuple[int, int, Path]] = []
        for cell, result in zip(cells, results):
            if isinstance(result, BaseException):
                repeat_index, _, suite_path = cell
                failed.append(cell)
                LOGGER.error(
                    "LIFT cell failed run_id=%s repeat=%d suite=%s: %r",
                    run_id, repeat_index, suite_path.name, result,
                )
        return failed

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
        """跑单个 suite：warmup → produce_delta → holdout 对照，结束清理资源。"""
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
                    "produce_delta requires at least one non-holdout task"
                )

            policy = WarmupThenUpdatePolicy(warmup_tasks=warmup_tasks)
            # warmup 容器在 produce_delta 内部已 cleanup；delta 镜像留给 holdout
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
        """按 ``holdout_container_policy`` 串行 / 并行执行 holdout 多题。

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
            tool_calls: int | None = None,
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
                tool_calls=tool_calls,
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
                        tool_calls=result.tool_calls,
                    )
                else:
                    _phase(
                        task.name, phase, "done",
                        detail=f"judge fail (score={result.content_score:.2f})",
                        score=result.content_score, success=False,
                        turns=result.turns,
                        tool_calls=result.tool_calls,
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
                "LIFT holdout %s: baseline_success=%s evolved_success=%s",
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
                    "LIFT holdout task failed (serial isolated) suite=%s task=%s: %r",
                    ctx.suite_name, t.name, exc,
                )
        return out
