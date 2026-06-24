"""把已有 ``report.json`` 反向 replay 成事件总线广播，重建 tracker 状态。

用既有 ``emit_run_plan`` / ``emit_suite_plan`` / ``emit_stage`` 三个 emitter，
监听器（``RunStateTracker``）会同步建好 repeat × suite × holdout_task × phase
节点并填上 score / success / turns / tool_calls / status。warmup 题在 report
里没存，留空（dashboard 只显示 holdout 也能用）。

供 ``--evaluate-only`` 路径在跑后处理前先把 dashboard 骨架填齐。
"""

from __future__ import annotations

from pathlib import Path

from src.lift.status import events as ev
from src.models import EvalReport


def replay_report_into_bus(run_id: str, report_path: Path) -> None:
    """读取 ``report_path`` 并向事件总线广播 plan/stage 事件。"""
    report = EvalReport.from_json_file(report_path)
    suite_names: list[str] = []
    for repeat in report.runs:
        for suite in repeat.suites:
            name = suite.suite_name or (
                Path(suite.suite_path).stem if suite.suite_path else "?"
            )
            if name not in suite_names:
                suite_names.append(name)

    holdout_total = sum(
        len(suite.tasks) for repeat in report.runs for suite in repeat.suites
    )

    ev.emit_run_plan(
        run_id=run_id,
        repeats=len(report.runs),
        suite_names=tuple(suite_names),
        params=(
            ("source", "evaluate-only replay"),
            ("holdout_total", str(holdout_total)),
        ),
    )

    for repeat_idx, repeat in enumerate(report.runs):
        ev.emit_stage(
            kind="repeat", status="done", run_id=run_id, repeat_index=repeat_idx
        )
        for suite in repeat.suites:
            suite_name = suite.suite_name or (
                Path(suite.suite_path).stem if suite.suite_path else "?"
            )
            try:
                suite_idx = suite_names.index(suite_name)
            except ValueError:
                continue
            holdout_names = tuple(t.task_name for t in suite.tasks)
            ev.emit_suite_plan(
                run_id=run_id,
                repeat_index=repeat_idx,
                suite_index=suite_idx,
                suite_name=suite_name,
                warmup_task_names=(),
                holdout_task_names=holdout_names,
            )
            ev.emit_stage(
                kind="suite",
                status="done",
                run_id=run_id,
                repeat_index=repeat_idx,
                suite_index=suite_idx,
                suite_name=suite_name,
            )
            # warmup 在 report.json 里没存，但既然 holdout 跑完了 warmup 必然
            # 成功；不补这条 dashboard.suiteOverall 会因 warmup_status='pending'
            # 把整 suite 判成 pending，导致总进度恒 0%。
            ev.emit_stage(
                kind="warmup",
                status="done",
                run_id=run_id,
                repeat_index=repeat_idx,
                suite_index=suite_idx,
                suite_name=suite_name,
            )
            for task in suite.tasks:
                ev.emit_stage(
                    kind="task",
                    status="done",
                    run_id=run_id,
                    repeat_index=repeat_idx,
                    suite_index=suite_idx,
                    suite_name=suite_name,
                    task_name=task.task_name,
                )
                for phase_name, phase in (
                    ("baseline", task.baseline),
                    ("evolved", task.evolved),
                ):
                    if phase is None:
                        continue
                    ev.emit_stage(
                        kind="phase",
                        status="done",
                        run_id=run_id,
                        repeat_index=repeat_idx,
                        suite_index=suite_idx,
                        suite_name=suite_name,
                        task_name=task.task_name,
                        phase=phase_name,
                        score=phase.content_score,
                        success=phase.success,
                        turns=phase.turns or None,
                        tool_calls=phase.tool_calls,
                    )
