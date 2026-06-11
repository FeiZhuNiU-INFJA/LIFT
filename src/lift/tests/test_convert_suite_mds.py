"""Tests for benchmark markdown → JSON conversion (train/test layout)."""

from __future__ import annotations

from pathlib import Path

from src.preprocess.convert_suite_mds_to_json import build_benchmark_spec


def _write_task_md(task_dir: Path, name: str) -> None:
    task_dir.mkdir(parents=True)
    (task_dir / f"{name}.md").write_text(
        "### query\nhello\n\n### 要求\nreq\n\n### 轨迹要求\ntraj\n",
        encoding="utf-8",
    )


def test_build_benchmark_spec_splits_train_and_test(tmp_path: Path) -> None:
    scene = tmp_path / "DemoScene"
    _write_task_md(scene / "train" / "q1_warmup", "q1_warmup")
    _write_task_md(scene / "train" / "q2_warmup", "q2_warmup")
    _write_task_md(scene / "test" / "q3_final", "q3_final")

    spec = build_benchmark_spec(scene)
    assert spec["name"] == "DemoScene"
    assert [task["name"] for task in spec["warmup_tasks"]] == ["Q1", "Q2"]
    assert [task["name"] for task in spec["holdout_tasks"]] == ["Q3"]
