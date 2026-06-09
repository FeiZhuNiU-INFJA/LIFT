from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from src_new.lift.suite.holdout import split_suite_tasks
from src_new.lift.suite.lift_suite import load_lift_suite


def _write_suite(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _minimal_suite_payload(*, holdout_count: int | None = None, holdout_names: list[str] | None = None) -> dict:
    tasks = [
        {
            "name": f"Q{i}",
            "query": f"task {i}",
            "requirements": {"default_skills": [], "extra_skills_dir": "", "material_dir": ""},
            "expected_result": {"content_reqs": "ok", "trajectory_reqs": ""},
        }
        for i in range(1, 4)
    ]
    payload: dict = {"name": "Test", "category": "Test", "tasks": tasks}
    if holdout_count is not None:
        payload["holdout_count"] = holdout_count
    if holdout_names is not None:
        payload["holdout_task_names"] = holdout_names
    return payload


def test_holdout_count_default_one() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "suite.json"
        _write_suite(path, _minimal_suite_payload())
        config = load_lift_suite(path)
        assert config.holdout_count == 1
        warmup, holdout = split_suite_tasks(config)
        assert [t.name for t in warmup] == ["Q1", "Q2"]
        assert [t.name for t in holdout] == ["Q3"]


def test_holdout_count_two() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "suite.json"
        _write_suite(path, _minimal_suite_payload(holdout_count=2))
        config = load_lift_suite(path)
        warmup, holdout = split_suite_tasks(config)
        assert [t.name for t in warmup] == ["Q1"]
        assert [t.name for t in holdout] == ["Q2", "Q3"]


def test_holdout_task_names() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "suite.json"
        _write_suite(path, _minimal_suite_payload(holdout_names=["Q1", "Q3"]))
        config = load_lift_suite(path)
        warmup, holdout = split_suite_tasks(config)
        assert [t.name for t in warmup] == ["Q2"]
        assert [t.name for t in holdout] == ["Q1", "Q3"]


def test_holdout_count_exceeds_tasks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "suite.json"
        _write_suite(path, _minimal_suite_payload(holdout_count=5))
        with pytest.raises(ValueError, match="holdout_count"):
            split_suite_tasks(load_lift_suite(path))
