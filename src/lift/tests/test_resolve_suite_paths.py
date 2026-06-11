"""Tests for suite path resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.paths import BENCHMARKS_DEMO_DIR
from src.utils import iter_benchmark_paths, resolve_suite_paths


def test_resolve_hello_from_benchmarks_demo() -> None:
    paths = resolve_suite_paths(BENCHMARKS_DEMO_DIR, "hello.json")
    assert paths == [BENCHMARKS_DEMO_DIR / "hello.json"]


def test_missing_benchmark_dir_raises(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    with pytest.raises(ValueError, match="Benchmark directory not found"):
        iter_benchmark_paths(missing)


def test_missing_suite_raises() -> None:
    with pytest.raises(ValueError, match="non-existent"):
        resolve_suite_paths(BENCHMARKS_DEMO_DIR, "missing.json")
