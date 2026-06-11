"""Integration-style tests for ``LIFTPipeline`` using ``MockAdapter``.

使用 ``MockAdapter`` 对 ``LIFTPipeline`` 的集成式测试。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from src.lift.pipeline.lift_pipeline import LIFTPipeline
from src.paths import report_json_path
from src.lift.pipeline.run_options import RunOptions
from src.lift.tests.mock_adapter import MockAdapter


def _suite_json(path: Path) -> None:
    """写入含 1 warmup + 2 final 的 PipeTest suite JSON。"""
    task = {
        "requirements": {"default_skills": [], "extra_skills_dir": "", "material_dir": ""},
        "expected_result": {"content_reqs": "x", "trajectory_reqs": ""},
    }
    payload = {
        "name": "PipeTest",
        "category": "PipeTest",
        "warmup_tasks": [{"name": "Q1", "query": "w1", **task}],
        "holdout_tasks": [
            {"name": "Q2", "query": "h1", **task},
            {"name": "Q3", "query": "h2", **task},
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


async def test_pipeline_two_holdout_task_runs() -> None:
    """Verify full pipeline runs delta production plus before/after load for holdout tasks.

    验证完整流水线为 holdout 任务执行 delta 生成及 load 前后阶段。
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        suite_path = tmp_path / "suite.json"
        _suite_json(suite_path)
        adapter = MockAdapter()
        pipeline = LIFTPipeline()
        run_id = "evobench-runid-test-pipeline"
        prev_cwd = Path.cwd()
        os.chdir(tmp_path)
        try:
            report = await pipeline.run(
                run_id=run_id,
                suite_paths=[suite_path],
                adapter=adapter,
                options=RunOptions(repeat=1, incremental_report=False, parallel_repeats=False),
            )
        finally:
            os.chdir(prev_cwd)
        assert report_json_path(run_id, cwd=tmp_path).is_file()
        assert adapter.produce_delta_count == 1
        assert adapter.before_load_count == 2
        assert adapter.after_load_count == 2
        assert len(report.runs) == 1
        suite_run = report.runs[0].suites[0]
        assert len(suite_run.tasks) == 2
        assert suite_run.tasks[0].task_name == "Q2"
        assert suite_run.tasks[1].task_name == "Q3"
        assert suite_run.tasks[0].evolved is not None


async def test_pipeline_warmup_only_skips_holdout() -> None:
    """Verify ``warmup_only`` produces delta but skips holdout before/after load.

    验证 ``warmup_only`` 仅生成 delta，跳过 holdout 的 load 前后阶段。
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        suite_path = tmp_path / "suite.json"
        _suite_json(suite_path)
        adapter = MockAdapter()
        pipeline = LIFTPipeline()
        run_id = "evobench-runid-test-warmup-only"
        prev_cwd = Path.cwd()
        os.chdir(tmp_path)
        try:
            report = await pipeline.run(
                run_id=run_id,
                suite_paths=[suite_path],
                adapter=adapter,
                options=RunOptions(warmup_only=True, incremental_report=False),
            )
        finally:
            os.chdir(prev_cwd)
        assert report_json_path(run_id, cwd=tmp_path).is_file()
        assert adapter.produce_delta_count == 1
        assert adapter.before_load_count == 0
        assert adapter.after_load_count == 0
        tasks = report.runs[0].suites[0].tasks
        assert len(tasks) == 0
