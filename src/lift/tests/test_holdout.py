"""Unit tests for suite warmup/holdout task loading."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from src.lift.suite.holdout import split_suite_tasks
from src.lift.suite.lift_suite import load_lift_suite


def _write_suite(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _minimal_suite_payload() -> dict:
    task = {
        "query": "task",
        "requirements": {"default_skills": [], "extra_skills_dir": "", "material_dir": ""},
        "expected_result": {"content_reqs": "ok", "trajectory_reqs": ""},
    }
    return {
        "name": "Test",
        "category": "Test",
        "warmup_tasks": [{"name": "Q1", **task}, {"name": "Q2", **task}],
        "holdout_tasks": [{"name": "Q3", **task}],
    }


def test_split_suite_tasks_returns_warmup_and_holdout() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "suite.json"
        _write_suite(path, _minimal_suite_payload())
        suite = load_lift_suite(path)
        warmup, holdout = split_suite_tasks(suite)
        assert [t.name for t in warmup] == ["Q1", "Q2"]
        assert [t.name for t in holdout] == ["Q3"]


def test_missing_warmup_tasks_raises() -> None:
    payload = _minimal_suite_payload()
    payload["warmup_tasks"] = []
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "suite.json"
        _write_suite(path, payload)
        with pytest.raises(ValueError, match="warmup_tasks"):
            split_suite_tasks(load_lift_suite(path))


def test_missing_holdout_tasks_raises() -> None:
    payload = _minimal_suite_payload()
    payload["holdout_tasks"] = []
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "suite.json"
        _write_suite(path, payload)
        with pytest.raises(ValueError, match="holdout_tasks"):
            split_suite_tasks(load_lift_suite(path))
