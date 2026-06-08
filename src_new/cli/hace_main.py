from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from src_new.config import LOGGER
from src_new.paths import report_json_path
from src_new.utils import make_run_id, resolve_suite_paths

from src_new.hace.adapters.registry import (
    SUPPORTED_RUNTIMES,
    create_adapter,
    default_docker_image,
)
from src_new.hace.pipeline.hace_pipeline import HACEPipeline
from src_new.hace.pipeline.run_options import RunOptions
from src_new.hace.policies.container import WarmupContainerPolicy


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="HACE evaluation (Hold-out Artifact-Contrast Evaluation, src_new)."
    )
    parser.add_argument(
        "--runtime",
        required=True,
        choices=list(SUPPORTED_RUNTIMES),
        help="Agent runtime adapter.",
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
        action="store_true",
        help="Run post-process after evaluation.",
    )
    parser.add_argument(
        "--evaluate-only",
        action="store_true",
        help="Only post-process an existing report (requires --run_id).",
    )
    parser.add_argument("--run_id", default=None, help="Custom run_id suffix.")
    parser.add_argument("--repeat", type=int, default=1, help="Repeat HACE flow N times.")
    parser.add_argument(
        "-p",
        "--parallel",
        action="store_true",
        help="Run warmup tasks in parallel (within warmup container policy).",
    )
    parser.add_argument(
        "--warmup-container-policy",
        default=WarmupContainerPolicy.SERIAL_SINGLE.value,
        choices=[p.value for p in WarmupContainerPolicy],
        help="Warmup container orchestration policy.",
    )
    parser.add_argument(
        "--serial-repeats",
        action="store_true",
        help="Run repeats serially instead of in parallel.",
    )
    parser.add_argument(
        "--max-parallel-repeats",
        type=int,
        default=None,
        help="Cap parallel repeat workers (default: repeat count).",
    )
    return parser


def evaluate_only_mode(args: argparse.Namespace) -> None:
    from src_new.postprocess.run_post_process import run_post_process_pipeline

    if not args.run_id:
        raise ValueError("--evaluate-only requires --run_id")
    run_id = make_run_id(args.run_id)
    report_path = report_json_path(run_id)
    if not report_path.exists():
        raise FileNotFoundError(f"Report not found: {report_path}")
    LOGGER.info("HACE evaluate-only runtime=%s: %s", args.runtime, report_path)
    run_post_process_pipeline(run_id, report_path, agent_source=args.runtime)


async def run_hace(args: argparse.Namespace, suite_paths: list[Path]) -> None:
    run_id = make_run_id(args.run_id)
    warmup_policy = WarmupContainerPolicy(args.warmup_container_policy)
    docker_image = default_docker_image(args.runtime)
    options = RunOptions(
        repeat=args.repeat,
        warmup_only=args.warmup_only,
        evaluate=args.evaluate,
        evaluate_only=False,
        parallel=args.parallel,
        docker_image=docker_image,
        warmup_container_policy=warmup_policy,
        parallel_repeats=not args.serial_repeats,
        max_parallel_repeats=args.max_parallel_repeats,
    )
    adapter = create_adapter(args.runtime, options)
    pipeline = HACEPipeline()
    LOGGER.info(f"HACE run_id={run_id} runtime={args.runtime} suites={len(suite_paths)}")
    await pipeline.run(
        run_id=run_id,
        suite_paths=suite_paths,
        adapter=adapter,
        options=options,
    )

    if args.evaluate:
        from src_new.postprocess.run_post_process import run_post_process_pipeline

        report_path = report_json_path(run_id)
        run_post_process_pipeline(run_id, report_path, agent_source=args.runtime)


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.evaluate_only:
        evaluate_only_mode(args)
        return

    suite_paths = resolve_suite_paths(Path(args.benchmark_dir), args.suite)
    asyncio.run(run_hace(args, suite_paths))


if __name__ == "__main__":
    main()
