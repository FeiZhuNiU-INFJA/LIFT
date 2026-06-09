"""Unit tests for suite holdout splitting and configuration loading.

套件 holdout 划分与配置加载的单元测试。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from src_new.lift.suite.holdout import split_suite_tasks
from src_new.lift.suite.lift_suite import load_lift_suite


def _write_suite(path: Path, payload: dict) -> None:
    """将 suite JSON payload 写入临时文件。"""
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _minimal_suite_payload(*, holdout_count: int | None = None, holdout_names: list[str] | None = None) -> dict:
    """构造 3 道题的最小 suite dict，可注入 holdout 配置。"""
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
    """Verify default ``holdout_count`` is 1 and splits the last task into holdout.

    验证默认 ``holdout_count`` 为 1，最后一个任务划入 holdout。
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "suite.json"
        _write_suite(path, _minimal_suite_payload())
        config = load_lift_suite(path)
        assert config.holdout_count == 1
        warmup, holdout = split_suite_tasks(config)
        assert [t.name for t in warmup] == ["Q1", "Q2"]
        assert [t.name for t in holdout] == ["Q3"]


def test_holdout_count_two() -> None:
    """Verify ``holdout_count=2`` puts the last two tasks in holdout.

    验证 ``holdout_count=2`` 时最后两个任务划入 holdout。
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "suite.json"
        _write_suite(path, _minimal_suite_payload(holdout_count=2))
        config = load_lift_suite(path)
        warmup, holdout = split_suite_tasks(config)
        assert [t.name for t in warmup] == ["Q1"]
        assert [t.name for t in holdout] == ["Q2", "Q3"]


def test_holdout_task_names() -> None:
    """Verify explicit ``holdout_task_names`` selects holdout by name.

    验证显式 ``holdout_task_names`` 按名称指定 holdout 任务。
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "suite.json"
        _write_suite(path, _minimal_suite_payload(holdout_names=["Q1", "Q3"]))
        config = load_lift_suite(path)
        warmup, holdout = split_suite_tasks(config)
        assert [t.name for t in warmup] == ["Q2"]
        assert [t.name for t in holdout] == ["Q1", "Q3"]


def test_holdout_count_exceeds_tasks() -> None:
    """Verify ``holdout_count`` larger than task count raises ``ValueError``.

    验证 ``holdout_count`` 超过任务总数时抛出 ``ValueError``。
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "suite.json"
        _write_suite(path, _minimal_suite_payload(holdout_count=5))
        with pytest.raises(ValueError, match="holdout_count"):
            split_suite_tasks(load_lift_suite(path))
