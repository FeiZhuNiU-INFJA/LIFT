"""Tests for TOS benchmark markdown fetch helpers."""

from __future__ import annotations

import zipfile
from pathlib import Path

from src.preprocess.benchmark_mds_fetch import _resolve_extracted_root, extract_benchmark_mds_zip


def _write_zip(path: Path, entries: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)


def test_resolve_extracted_root_prefers_benchmark_mds_folder(tmp_path: Path) -> None:
    extract_dir = tmp_path / "extract"
    (extract_dir / "benchmark_mds" / "hello").mkdir(parents=True)
    assert _resolve_extracted_root(extract_dir).name == "benchmark_mds"


def test_extract_benchmark_mds_zip_writes_scene_tree(tmp_path: Path) -> None:
    zip_path = tmp_path / "benchmark_mds.zip"
    _write_zip(
        zip_path,
        {
            "benchmark_mds/hello/q1_task/q1_task.md": b"### query\nhi\n",
        },
    )
    target_dir = tmp_path / "assets" / "benchmark_mds"
    extract_benchmark_mds_zip(zip_path, target_dir)
    assert (target_dir / "hello" / "q1_task" / "q1_task.md").read_text(encoding="utf-8") == "### query\nhi\n"
