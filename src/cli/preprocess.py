"""独立 CLI：拉取 benchmark_mds 并转换为 suite JSON。

在 benchmark 源变更后、运行 ``python -m src.cli.lift_main`` 前执行：

    python -m src.cli.preprocess
    python -m src.cli.preprocess --force-download
    python -m src.cli.preprocess --input-root assets/benchmark_mds --output-root assets/benchmarks
"""

from __future__ import annotations

import argparse
from pathlib import Path

from src.preprocess.convert_suite_mds_to_json import preprocess_suite_mds


def build_parser() -> argparse.ArgumentParser:
    """构建 benchmark 预处理命令行参数解析器。"""
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
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="Skip benchmark_mds download and use the existing local assets/benchmark_mds directory.",
    )
    parser.add_argument(
        "--force-download",
        action="store_true",
        help="Re-download benchmark_mds even if assets/benchmark_mds already exists.",
    )
    parser.add_argument(
        "--source",
        choices=("tos", "huggingface", "modelscope"),
        default=None,
        help=(
            "Download source for benchmark_mds. Defaults to BENCHMARK_SOURCE env "
            "(or 'tos' if unset). Use BENCHMARK_HF_REPO or BENCHMARK_MODELSCOPE_REPO to override mirrors."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    """CLI 入口：转换 markdown benchmark 并打印生成的 JSON 路径。"""
    args = build_parser().parse_args(argv)
    written = preprocess_suite_mds(
        input_root=args.input_root,
        output_root=args.output_root,
        skip_download=args.skip_download,
        force_download=args.force_download,
        source=args.source,
    )
    for path in written:
        print(path)
    print(f"preprocess done: {len(written)} file(s) written.")


if __name__ == "__main__":
    main()
