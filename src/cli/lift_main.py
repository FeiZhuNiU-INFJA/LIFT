"""LIFT 评测 CLI 入口：warmup + hold-out baseline/evolved，可选后处理。

用法示例::

    python -m src.cli.lift_main -r openclaw --benchmark_dir assets/benchmarks --suite all
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from src.config import LOGGER
from src.paths import report_json_path
from src.utils import make_run_id, resolve_suite_paths

from src.lift.adapters.registry import SUPPORTED_RUNTIMES, create_adapter
from src.lift.pipeline.lift_pipeline import LIFTPipeline
from src.lift.pipeline.run_options import RunOptions
from src.lift.policies.container import HoldoutContainerPolicy, WarmupContainerPolicy


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
        "--max-parallel-repeats",
        type=int,
        default=None,
        help=(
            "Cap parallel repeat workers. Default: no cap (all repeats run in parallel). "
            "Set to 1 to run repeats serially."
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
    return parser


def evaluate_only_mode(args: argparse.Namespace) -> None:
    """仅对已有 report JSON 运行后处理（``--evaluate-only``）。"""
    from src.postprocess.run_post_process import run_post_process_pipeline

    if not args.run_id:
        raise ValueError("--evaluate-only requires --run_id")
    run_id = make_run_id(args.run_id)
    report_path = report_json_path(run_id)
    if not report_path.exists():
        raise FileNotFoundError(f"Report not found: {report_path}")
    LOGGER.info("LIFT evaluate-only agent_runtime=%s: %s", args.agent_runtime, report_path)
    run_post_process_pipeline(run_id, report_path, agent_source=args.agent_runtime)


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
        max_parallel_repeats=args.max_parallel_repeats,
        max_concurrent_tasks=args.max_concurrent_tasks,
    )
    adapter = create_adapter(args.agent_runtime, options)
    pipeline = LIFTPipeline()
    LOGGER.info(
        "LIFT run_id=%s agent_runtime=%s suites=%d",
        run_id,
        args.agent_runtime,
        len(suite_paths),
    )
    await pipeline.run(
        run_id=run_id,
        suite_paths=suite_paths,
        adapter=adapter,
        options=options,
    )

    if args.evaluate:
        # 执行期 report 无 langfuse 字段；此处 trace_backfill + CSV/HTML
        from src.postprocess.run_post_process import run_post_process_pipeline

        report_path = report_json_path(run_id)
        LOGGER.info("LIFT post-process run_id=%s", run_id)
        run_post_process_pipeline(run_id, report_path, agent_source=args.agent_runtime)


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
