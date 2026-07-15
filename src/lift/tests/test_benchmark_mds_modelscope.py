"""Tests for ModelScope benchmark markdown fetch helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.preprocess import benchmark_mds_modelscope
from src.preprocess.benchmark_mds_modelscope import (
    BenchmarkMdsModelScopeFetchError,
    download_benchmark_mds_tree_from_modelscope,
    ensure_benchmark_mds_from_modelscope,
)


def test_download_benchmark_mds_tree_from_modelscope_invokes_cli(
    tmp_path: Path,
    monkeypatch,
) -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        local_dir = Path(command[command.index("--local_dir") + 1])
        (local_dir / "scene_one").mkdir(parents=True)
        (local_dir / "README.md").write_text("dataset readme\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setenv("BENCHMARK_MODELSCOPE_REPO", "custom/EALE")
    monkeypatch.setattr(benchmark_mds_modelscope.shutil, "which", lambda name: "modelscope")
    monkeypatch.setattr(benchmark_mds_modelscope.subprocess, "run", fake_run)

    target_dir = tmp_path / "assets" / "benchmark_mds"
    assert download_benchmark_mds_tree_from_modelscope(target_dir) == target_dir
    assert commands == [
        [
            "modelscope",
            "download",
            "--dataset",
            "custom/EALE",
            "--local_dir",
            str(target_dir),
        ]
    ]


def test_download_benchmark_mds_tree_from_modelscope_requires_cli(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(benchmark_mds_modelscope.shutil, "which", lambda name: None)

    with pytest.raises(BenchmarkMdsModelScopeFetchError, match="pip install modelscope"):
        download_benchmark_mds_tree_from_modelscope(tmp_path / "benchmark_mds")


def test_ensure_benchmark_mds_from_modelscope_force_replaces_existing_tree(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target_dir = tmp_path / "benchmark_mds"
    target_dir.mkdir()
    (target_dir / "old_scene").mkdir()

    def fake_download(destination_dir: Path) -> Path:
        assert not (destination_dir / "old_scene").exists()
        (destination_dir / "new_scene").mkdir(parents=True)
        return destination_dir

    monkeypatch.setattr(
        benchmark_mds_modelscope,
        "download_benchmark_mds_tree_from_modelscope",
        fake_download,
    )

    assert ensure_benchmark_mds_from_modelscope(target_dir, force=True) == target_dir.resolve()
    assert (target_dir / "new_scene").is_dir()
    assert not (target_dir / "old_scene").exists()


def test_download_benchmark_mds_tree_from_modelscope_removes_duplicate_preview_tree(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        local_dir = Path(command[command.index("--local_dir") + 1])
        (local_dir / "scene_one").mkdir(parents=True)
        (local_dir / "benchmark_mds" / "scene_one").mkdir(parents=True)
        (local_dir / "README.md").write_text("dataset readme\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(benchmark_mds_modelscope.shutil, "which", lambda name: "modelscope")
    monkeypatch.setattr(benchmark_mds_modelscope.subprocess, "run", fake_run)

    target_dir = tmp_path / "benchmark_mds"
    download_benchmark_mds_tree_from_modelscope(target_dir)

    assert (target_dir / "scene_one").is_dir()
    assert not (target_dir / "benchmark_mds").exists()
