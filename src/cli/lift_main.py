"""LIFT 评测 CLI 入口：warmup + hold-out baseline/evolved，可选后处理。

用法示例::

    python -m src.cli.lift_main -r openclaw --benchmark_dir assets/benchmarks --suite all
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from dotenv import load_dotenv

if TYPE_CHECKING:
    from src.lift.status.state import RunStateTracker

load_dotenv()

from src.config import LOGGER
from src.paths import report_json_path
from src.utils import make_run_id, resolve_suite_paths

from src.lift.adapters.registry import SUPPORTED_RUNTIMES, create_adapter
from src.lift.pipeline.lift_pipeline import LIFTPipeline
from src.lift.pipeline.run_options import RunOptions
from src.lift.policies.container import (
    HoldoutContainerPolicy,
    HoldoutPhasePolicy,
    WarmupContainerPolicy,
)


def build_parser() -> argparse.ArgumentParser:
    """构建 LIFT 评测命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        description="LIFT evaluation (Loaded Impact on Final Task, src)."
    )
    parser.add_argument(
        "-r",
        "--agent-runtime",
        required=True,
        choices=list(SUPPORTED_RUNTIMES),
        dest="agent_runtime",
        metavar="RUNTIME",
        help="Agent runtime adapter (e.g. openclaw → OpenClawAdapter).",
    )
    parser.add_argument(
        "--benchmark_dir",
        default="assets/benchmarks",
        help="Directory containing suite JSON files.",
    )
    parser.add_argument(
        "--suite",
        default="all",
        help="Comma-separated suite JSON filenames, or 'all'.",
    )
    parser.add_argument(
        "--warmup-only",
        action="store_true",
        help="Run warmup tasks and produce delta only; skip hold-out baseline/evolved.",
    )
    parser.add_argument(
        "-e",
        "--evaluate",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run post-process after evaluation (default: on). Use --no-evaluate to skip.",
    )
    parser.add_argument(
        "--evaluate-only",
        action="store_true",
        help="Only post-process an existing report (requires --run_id).",
    )
    parser.add_argument("--run_id", default=None, help="Custom run_id suffix.")
    parser.add_argument("--repeat", type=int, default=1, help="Repeat LIFT flow N times.")
    parser.add_argument(
        "--warmup-container-policy",
        default=WarmupContainerPolicy.PARALLEL_SINGLE.value,
        choices=[p.value for p in WarmupContainerPolicy],
        help=(
            "Warmup container orchestration policy "
            "(default: parallel_single — one container, asyncio.gather concurrent tasks). "
            "Other values: serial_single (one container, sequential), "
            "parallel_multi (one container per task, multi-user style)."
        ),
    )
    parser.add_argument(
        "--holdout-container-policy",
        default=HoldoutContainerPolicy.PARALLEL_MULTI.value,
        choices=[p.value for p in HoldoutContainerPolicy],
        help=(
            "Hold-out container orchestration policy: each task always gets its own "
            "container (image-split). Choose serial_multi (sequential) or "
            "parallel_multi (default; asyncio.gather across tasks)."
        ),
    )
    parser.add_argument(
        "--holdout-phase-policy",
        default=HoldoutPhasePolicy.PARALLEL.value,
        choices=[p.value for p in HoldoutPhasePolicy],
        help=(
            "Per-task baseline/evolved execution order. Default: parallel "
            "(asyncio.gather both phases — saves ~1/3 hold-out time). "
            "Set to serial to keep the legacy baseline→evolved order."
        ),
    )
    parser.add_argument(
        "--max-parallel-repeats",
        type=int,
        default=None,
        help=(
            "Cap parallel repeat workers. Default: no cap (all repeats run in parallel). "
            "Set to 1 to run repeats serially."
        ),
    )
    parser.add_argument(
        "--max-parallel-suites",
        type=int,
        default=3,
        help=(
            "Cap parallel suites within a repeat. Default: 3 (up to 3 suites run "
            "warmup+hold-out concurrently). Set to 1 for serial; <=0 for no cap. "
            "Note: concurrent suites multiply total containers with per-suite task "
            "parallelism."
        ),
    )
    parser.add_argument(
        "--max-concurrent-tasks",
        type=int,
        default=None,
        help=(
            "Cap concurrent task containers within a suite "
            "(applies to warmup parallel_single/parallel_multi and hold-out "
            "parallel_multi). Default: no cap."
        ),
    )
    parser.add_argument(
        "--max-conversation-turns",
        type=int,
        default=5,
        help=(
            "Max work->judge conversation turns per task: when the judge rejects, the "
            "task retries with the judge's reason as the next prompt, up to this many "
            "turns (default: 5). Replaces the former EVAL_MAX_TURNS env var."
        ),
    )
    parser.add_argument(
        "--container-memory",
        default=None,
        help=(
            "Per-container memory cap passed to 'docker run --memory' (e.g. 3g, 2048m). "
            "Default: no cap. A single OpenClaw container (node/V8 multi-process) can peak "
            "above 3g, and a cgroup cap gets the container OOM-killed mid-inference, so the "
            "default leaves memory to the VM kernel (overflow spills to VM swap). Control "
            "total memory via --max-parallel-suites and VM memory/swap instead."
        ),
    )
    parser.add_argument(
        "--container-cpus",
        default=None,
        help=(
            "Per-container CPU cap passed to 'docker run --cpus' (e.g. 2, 1.5). "
            "Default: no cap."
        ),
    )
    parser.add_argument(
        "--status-viz",
        action="store_true",
        help=(
            "Show a live terminal dashboard of run/repeat/suite/task/phase status and "
            "alive containers (default: off). Console logs are redirected to the log file "
            "while the dashboard is active to avoid clobbering the display."
        ),
    )
    parser.add_argument(
        "--status-http",
        default=None,
        metavar="[HOST:]PORT",
        help=(
            "Start a browser-side HTTP status dashboard (zero extra deps; stdlib http.server). "
            "Format: PORT (binds to 127.0.0.1) or HOST:PORT (e.g. 0.0.0.0:8765 for remote access). "
            "Independent from --status-viz; both can be enabled simultaneously."
        ),
    )
    return parser


def evaluate_only_mode(args: argparse.Namespace) -> None:
    """仅对已有 report JSON 运行后处理（``--evaluate-only``）。

    始终把 report.json 反向 replay 成事件总线广播，重建 tracker 骨架（repeat /
    suite / task / phase 节点 + score / success / turns / tool_calls 状态），
    让 post-process pipeline 之后能用同一个 tracker 重导 ``dashboard.html``
    静态版（含 final summary、对话、tools 列）。

    ``--status-viz`` / ``--status-http`` 仍按需启用对应的 TUI / HTTP 面板。
    """
    from src.lift.status.state import RunStateTracker
    from src.postprocess.run_post_process import run_post_process_pipeline

    if not args.run_id:
        raise ValueError("--evaluate-only requires --run_id")
    run_id = make_run_id(args.run_id)
    report_path = report_json_path(run_id)
    if not report_path.exists():
        raise FileNotFoundError(f"Report not found: {report_path}")
    LOGGER.info("LIFT evaluate-only agent_runtime=%s: %s", args.agent_runtime, report_path)

    tracker = RunStateTracker()
    tracker.attach()
    try:
        _replay_report_to_tracker(run_id, report_path)
        with _optional_status_panels(
            tracker, viz_enabled=args.status_viz, http_endpoint=args.status_http
        ):
            run_post_process_pipeline(
                run_id, report_path, agent_source=args.agent_runtime, tracker=tracker
            )
            _export_dashboard_snapshot(run_id, tracker)
    finally:
        tracker.detach()


def _replay_report_to_tracker(run_id: str, report_path: Path) -> None:
    """把 ``report.json`` 反向 replay 成事件总线广播，重建 tracker 骨架与状态。

    用既有 ``emit_run_plan`` / ``emit_suite_plan`` / ``emit_stage`` 三个 emitter，
    监听器（``RunStateTracker``）会同步建好 repeat × suite × holdout_task × phase
    节点并填上 score / success / turns / tool_calls / status。warmup 题在 report
    里没存，留空（dashboard 只显示 holdout 也能用）。
    """
    from src.lift.status import events as ev
    from src.models import EvalReport

    report = EvalReport.from_json_file(report_path)
    suite_names: list[str] = []
    for repeat in report.runs:
        for suite in repeat.suites:
            name = suite.suite_name or (
                Path(suite.suite_path).stem if suite.suite_path else "?"
            )
            if name not in suite_names:
                suite_names.append(name)

    ev.emit_run_plan(
        run_id=run_id,
        repeats=len(report.runs),
        suite_names=tuple(suite_names),
        params=(("source", "evaluate-only replay"),),
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


@contextmanager
def _status_dashboard(
    *, viz_enabled: bool, http_endpoint: str | None
) -> Iterator["RunStateTracker | None"]:
    """启用状态面板：终端 TUI（``--status-viz``）与 / 或 HTTP 仪表盘（``--status-http``）。

    两者共享一份 ``RunStateTracker``：tracker 仅在至少一种面板被启用时注册到事件
    总线，否则保持完全 no-op。``--status-viz`` 期间会暂时摘掉 console 日志
    handler（保留 FileHandler），避免日志冲掉 ``rich.Live`` 渲染区；HTTP
    dashboard 不动 console 日志。

    yield 出 tracker（未启用时为 ``None``），调用方可以在 pipeline 之后把后处理
    结果通过 ``tracker.set_final_summary(...)`` 注入快照供 dashboard 展示。
    """
    if not viz_enabled and not http_endpoint:
        yield None
        return

    from src.lift.status.state import RunStateTracker

    tracker = RunStateTracker()
    tracker.attach()
    try:
        with _optional_status_panels(
            tracker, viz_enabled=viz_enabled, http_endpoint=http_endpoint
        ):
            yield tracker
    finally:
        tracker.detach()


@contextmanager
def _optional_status_panels(
    tracker: "RunStateTracker",
    *,
    viz_enabled: bool,
    http_endpoint: str | None,
) -> Iterator[None]:
    """在已有 ``tracker`` 之上启用 TUI / HTTP 面板（按需）。

    与 ``_status_dashboard`` 的区别：tracker 由调用方传入并管理 attach/detach
    生命周期。``--evaluate-only`` 路径下 tracker 必须先于 panels 启动以便
    replay 阶段事件能被订阅，因此走此函数。
    """
    # --status-viz: 摘 console 日志 + rich.Live 看板
    stream_handlers: list[logging.Handler] = []
    dashboard = None
    if viz_enabled:
        from src.lift.status.tui import StatusDashboard

        root_logger = logging.getLogger()
        stream_handlers = [
            h
            for h in root_logger.handlers
            if isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.FileHandler)
        ]
        for h in stream_handlers:
            root_logger.removeHandler(h)
        dashboard = StatusDashboard(tracker)
        dashboard.start()

    # --status-http: 后台线程 HTTP 服务器
    http_dashboard = None
    if http_endpoint:
        from src.lift.status.http_dashboard import HttpDashboard

        host, port = _parse_http_endpoint(http_endpoint)
        http_dashboard = HttpDashboard(tracker, host=host, port=port)
        http_dashboard.start()

    try:
        yield
    finally:
        if http_dashboard is not None:
            http_dashboard.stop()
        if dashboard is not None:
            dashboard.stop()
        for h in stream_handlers:
            logging.getLogger().addHandler(h)


def _parse_http_endpoint(endpoint: str) -> tuple[str, int]:
    """解析 ``--status-http`` 参数为 ``(host, port)``。

    - 纯数字：默认绑定 ``127.0.0.1``（仅本机访问）。
    - ``HOST:PORT``：按 host:port 解析，host 可以是 ``0.0.0.0`` / 域名 / IP。
    """
    if ":" in endpoint:
        host, _, port_str = endpoint.rpartition(":")
        host = host.strip() or "127.0.0.1"
    else:
        host = "127.0.0.1"
        port_str = endpoint
    try:
        port = int(port_str)
    except ValueError as exc:
        raise ValueError(
            f"Invalid --status-http endpoint {endpoint!r}; expected PORT or HOST:PORT"
        ) from exc
    return host, port


async def run_lift(args: argparse.Namespace, suite_paths: list[Path]) -> None:
    """执行完整 LIFT pipeline（warmup + hold-out），可选后处理。"""
    run_id = make_run_id(args.run_id)
    options = RunOptions(
        repeat=args.repeat,
        warmup_only=args.warmup_only,
        evaluate=args.evaluate,
        evaluate_only=False,
        warmup_container_policy=WarmupContainerPolicy(args.warmup_container_policy),
        holdout_container_policy=HoldoutContainerPolicy(args.holdout_container_policy),
        holdout_phase_policy=HoldoutPhasePolicy(args.holdout_phase_policy),
        max_parallel_repeats=args.max_parallel_repeats,
        max_parallel_suites=args.max_parallel_suites,
        max_concurrent_tasks=args.max_concurrent_tasks,
        max_conversation_turns=args.max_conversation_turns,
        container_memory=args.container_memory or None,
        container_cpus=args.container_cpus or None,
    )
    adapter = create_adapter(args.agent_runtime, options)
    pipeline = LIFTPipeline()
    LOGGER.info(
        "LIFT run_id=%s agent_runtime=%s suites=%d args=\n%s",
        run_id,
        args.agent_runtime,
        len(suite_paths),
        json.dumps(vars(args), default=str, ensure_ascii=False, sort_keys=True, indent=2),
    )
    with _status_dashboard(
        viz_enabled=args.status_viz, http_endpoint=args.status_http
    ) as tracker:
        await pipeline.run(
            run_id=run_id,
            suite_paths=suite_paths,
            adapter=adapter,
            options=options,
            extra_params=(
                ("agent_runtime", args.agent_runtime),
                ("benchmark_dir", str(args.benchmark_dir)),
                ("suite", args.suite),
            ),
        )

        if args.evaluate:
            # 执行期 report 无 langfuse 字段；此处 trace_backfill + CSV/HTML。
            # 在 dashboard 仍在线时跑后处理：完成后把 FinalSummary 注入 tracker，
            # 浏览器侧立即看到 final summary 表；随后 ctx 退出关停 dashboard。
            from src.postprocess.run_post_process import run_post_process_pipeline

            report_path = report_json_path(run_id)
            LOGGER.info("LIFT post-process run_id=%s", run_id)
            run_post_process_pipeline(
                run_id,
                report_path,
                agent_source=args.agent_runtime,
                tracker=tracker,
            )

            # dashboard 关停前导出一份静态 HTML 快照，便于会后回看。
            if tracker is not None and args.status_http:
                _export_dashboard_snapshot(run_id, tracker)


def _export_dashboard_snapshot(run_id: str, tracker) -> None:
    """把当前 tracker 快照渲染为静态 HTML 写到 ``results/<run_id>/dashboard.html``。"""
    from src.paths import results_run_dir
    from src.lift.status.http_dashboard import build_static_dashboard_html

    out = results_run_dir(run_id) / "dashboard.html"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            build_static_dashboard_html(tracker.snapshot()), encoding="utf-8"
        )
        LOGGER.info("Dashboard static snapshot: %s", out)
    except Exception:
        LOGGER.exception("Failed to export dashboard snapshot to %s", out)


def main(argv: list[str] | None = None) -> None:
    """CLI 入口：解析参数并分发到 evaluate-only 或完整 LIFT run。"""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.evaluate_only:
        evaluate_only_mode(args)
        return

    # Map --benchmark_dir + --suite (all or comma-separated JSON names) → suite file paths
    suite_paths = resolve_suite_paths(Path(args.benchmark_dir), args.suite)
    asyncio.run(run_lift(args, suite_paths))


if __name__ == "__main__":
    main()
