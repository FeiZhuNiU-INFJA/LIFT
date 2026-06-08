"""Standalone CLI: convert benchmark_mds markdown dirs to suite JSON.

Run before `python -m src_new.cli.lift_main` whenever benchmark sources change:

    python -m src_new.cli.preprocess
    python -m src_new.cli.preprocess --input-root assets/benchmark_mds --output-root assets/benchmarks
"""

from __future__ import annotations

import argparse
from pathlib import Path

from src_new.preprocess.convert_suite_mds_to_json import preprocess_suite_mds


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert benchmark_mds markdown directories to suite JSON files.",
    )
    parser.add_argument(
        "--input-root",
        type=Path,
        default=None,
        help="Source directory containing markdown benchmark scene folders (default: assets/benchmark_mds).",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=None,
        help="Destination directory for generated suite JSON files (default: assets/benchmarks).",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    written = preprocess_suite_mds(
        input_root=args.input_root,
        output_root=args.output_root,
    )
    for path in written:
        print(path)
    print(f"preprocess done: {len(written)} file(s) written.")


if __name__ == "__main__":
    main()
