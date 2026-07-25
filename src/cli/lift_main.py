"""LIFT 评测 CLI 入口：warmup + holdout baseline/evolved，可选后处理。

用法示例::

    python -m src.cli.lift_main -r openclaw --benchmark_dir assets/benchmarks --suite all
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from src.config import LOGGER
from src.models import EvalReport
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
from src.lift.status.http_dashboard import export_dashboard_snapshot
from src.lift.status.panels import optional_status_panels, status_dashboard
from src.lift.status.replay import replay_report_into_bus


# --resume 时从旧 report.run_options 里可以恢复的 CLI 参数
# key: argparse dest, value: 从 run_options dict 里取的 key
_RESUMABLE_ARGS: dict[str, str] = {
    "agent_runtime": "agent_runtime",
    "benchmark_dir": "benchmark_dir",
    "suite": "suite",
    "repeat": "repeat",
    "warmup_only": "warmup_only",
    "warmup_container_policy": "warmup_container_policy",
    "holdout_container_policy": "holdout_container_policy",
    "holdout_phase_policy": "holdout_phase_policy",
    "max_parallel_suites": "max_parallel_suites",
    "max_concurrent_tasks": "max_concurrent_tasks",
    "max_conversation_turns": "max_conversation_turns",
    "container_memory": "container_memory",
    "container_cpus": "container_cpus",
}


def _explicit_cli_dests(argv: list[str], parser: argparse.ArgumentParser) -> set[str]:
    """扫 argv 找出用户显式传入的参数(argparse dest)。

    通过 parser._actions 匹配 option_strings,识别 ``--foo`` / ``--foo=bar`` /
    ``-r`` 三种形式;跳过 ``--`` 分隔符和纯 positional。用于 --resume 时区分
    \"用了默认值\" vs \"用户传了值\",只把\"未显式传入\"的字段从旧 report 恢复。
    """
    option_to_dest: dict[str, str] = {}
    for action in parser._actions:
        for opt in action.option_strings:
            option_to_dest[opt] = action.dest
    explicit: set[str] = set()
    for tok in argv:
        if tok == "--":
            break
        key = tok.split("=", 1)[0]
        if key in option_to_dest:
            explicit.add(option_to_dest[key])
    return explicit


def _apply_resume_defaults(
    args: argparse.Namespace,
    explicit: set[str],
    parser: argparse.ArgumentParser,
) -> None:
    """--resume 时读旧 report.run_options,把未显式传入的字段覆盖到 args。

    只对 ``_RESUMABLE_ARGS`` 里的字段生效。找不到旧 report 或字段缺失时静默跳过
    (启用完全为空的 resume 时会走 pipeline 里现有的 \"no existing report\" 分支)。
    """
    if not args.run_id:
        return
    run_id = make_run_id(args.run_id)
    report_path = report_json_path(run_id)
    if not report_path.exists():
        LOGGER.info(
            "LIFT resume: no prior report at %s, using CLI defaults", report_path
        )
        return
    try:
        prev = EvalReport.from_json_file(report_path)
    except Exception:  # noqa: BLE001 — 解析失败当作没有 prior run_options
        LOGGER.warning(
            "LIFT resume: failed to parse %s for run_options, using CLI defaults",
            report_path, exc_info=True,
        )
        return
    prior = prev.run_options or {}
    if not prior:
        LOGGER.info(
            "LIFT resume: prior report has no run_options snapshot (created "
            "before this feature), using CLI defaults"
        )
        return
    restored: list[tuple[str, object]] = []
    for dest, key in _RESUMABLE_ARGS.items():
        if dest in explicit:
            continue  # 用户显式传了 → 尊重 CLI
        if key not in prior:
            continue
        value = prior[key]
        # argparse choices 用的是原始字符串;policy 枚举也序列化成 value 字符串
        setattr(args, dest, value)
        restored.append((dest, value))
    if restored:
        LOGGER.info(
            "LIFT resume: restored %d arg(s) from %s: %s",
            len(restored), report_path,
            ", ".join(f"{k}={v}" for k, v in restored),
        )


def build_parser() -> argparse.ArgumentParser:
    """构建 LIFT 评测命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        description="LIFT evaluation (Loaded Impact on Final Task, src)."
    )
    parser.add_argument(
        "-r",
        "--agent-runtime",
        required=False,
        default=None,
        choices=list(SUPPORTED_RUNTIMES),
        dest="agent_runtime",
        metavar="RUNTIME",
        help=(
            "Agent runtime adapter (e.g. openclaw → OpenClawAdapter). "
            "Required unless --resume can recover it from a prior report."
        ),
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
        help="Run warmup tasks and produce delta only; skip holdout baseline/evolved.",
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
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Resume from results/{run_id}/report.json: skip (repeat, suite) cells "
            "that already have all holdout tasks with both baseline and evolved "
            "phases; re-run partial or missing cells. Cell-level granularity "
            "because delta images are rmi'd at suite end."
        ),
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
            "Holdout container orchestration policy: each task always gets its own "
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
            "(asyncio.gather both phases — saves ~1/3 holdout time). "
            "Set to serial to run baseline before evolved for each task."
        ),
    )
    parser.add_argument(
        "--max-parallel-suites",
        type=int,
        default=3,
        help=(
            "Cap parallel cells in the suites x repeats matrix (one cell = one "
            "(repeat, suite) pair = one warmup+holdout run). Default: 3. "
            "Set to 1 for serial; <=0 for no cap. Note: each cell carries its own "
            "task-level concurrency, so total containers = parallel cells x task parallelism."
        ),
    )
    parser.add_argument(
        "--max-concurrent-tasks",
        type=int,
        default=None,
        help=(
            "Cap concurrent task containers within a suite "
            "(applies to warmup parallel_single/parallel_multi and holdout "
            "parallel_multi). Default: no cap."
        ),
    )
    parser.add_argument(
        "--max-conversation-turns",
        type=int,
        default=30,
        help=(
            "Max work->judge conversation turns per task: when the judge rejects, the "
            "task retries with the judge's reason as the next prompt, up to this many "
            "turns (default: 30). Replaces the former EVAL_MAX_TURNS env var."
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
        "--tui",
        action="store_true",
        help=(
            "Show a live terminal dashboard of run/repeat/suite/task/phase status and "
            "alive containers (default: off). Console logs are redirected to the log file "
            "while the dashboard is active to avoid clobbering the display."
        ),
    )
    parser.add_argument(
        "--dashboard",
        default=None,
        metavar="[HOST:]PORT",
        help=(
            "Start a browser-side HTTP status dashboard (zero extra deps; stdlib http.server). "
            "Format: PORT (binds to 127.0.0.1) or HOST:PORT (e.g. 0.0.0.0:8765 for remote access). "
            "Independent from --tui; both can be enabled simultaneously."
        ),
    )
    return parser


def evaluate_only_mode(args: argparse.Namespace) -> None:
    """仅对已有 report JSON 运行后处理（``--evaluate-only``）。

    始终把 report.json 反向 replay 成事件总线广播，重建 tracker 骨架（repeat /
    suite / task / phase 节点 + score / success / turns / tool_calls 状态），
    让 post-process pipeline 之后能用同一个 tracker 重导 ``dashboard.html``
    静态版（含 final summary、对话、tools 列）。

    ``--tui`` / ``--dashboard`` 仍按需启用对应的 TUI / HTTP 面板。
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
        replay_report_into_bus(run_id, report_path)
        with optional_status_panels(
            tracker, viz_enabled=args.tui, http_endpoint=args.dashboard
        ):
            run_post_process_pipeline(
                run_id, report_path, agent_source=args.agent_runtime, tracker=tracker
            )
            export_dashboard_snapshot(run_id, tracker)
    finally:
        tracker.detach()


async def run_lift(args: argparse.Namespace, suite_paths: list[Path]) -> None:
    """执行完整 LIFT pipeline（warmup + holdout），可选后处理。"""
    run_id = make_run_id(args.run_id)
    options = RunOptions(
        repeat=args.repeat,
        warmup_only=args.warmup_only,
        evaluate=args.evaluate,
        evaluate_only=False,
        resume=args.resume,
        warmup_container_policy=WarmupContainerPolicy(args.warmup_container_policy),
        holdout_container_policy=HoldoutContainerPolicy(args.holdout_container_policy),
        holdout_phase_policy=HoldoutPhasePolicy(args.holdout_phase_policy),
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
    with status_dashboard(
        viz_enabled=args.tui, http_endpoint=args.dashboard
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
            if tracker is not None and args.dashboard:
                export_dashboard_snapshot(run_id, tracker)


def main(argv: list[str] | None = None) -> None:
    """CLI 入口：解析参数并分发到 evaluate-only 或完整 LIFT run。"""
    import sys

    parser = build_parser()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(raw_argv)

    if args.evaluate_only:
        if not args.agent_runtime:
            parser.error("-r/--agent-runtime is required (unavailable in evaluate-only)")
        evaluate_only_mode(args)
        return

    # --resume: 未显式传入的字段从旧 report.run_options 恢复
    if args.resume:
        explicit = _explicit_cli_dests(raw_argv, parser)
        _apply_resume_defaults(args, explicit, parser)

    if not args.agent_runtime:
        parser.error(
            "-r/--agent-runtime is required (no prior report to recover from)"
        )

    # Map --benchmark_dir + --suite (all or comma-separated JSON names) → suite file paths
    suite_paths = resolve_suite_paths(Path(args.benchmark_dir), args.suite)
    asyncio.run(run_lift(args, suite_paths))


if __name__ == "__main__":
    main()
