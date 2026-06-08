from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from src_new.lift.tests.mock_adapter import MockAdapter
from src_new.lift.pipeline.lift_pipeline import LIFTPipeline
from src_new.lift.pipeline.run_options import RunOptions


def _suite_json(path: Path) -> None:
    payload = {
        "name": "PipeTest",
        "category": "PipeTest",
        "holdout_count": 2,
        "tasks": [
            {
                "name": "Q1",
                "query": "w1",
                "requirements": {"default_skills": [], "extra_skills_dir": "", "material_dir": ""},
                "expected_result": {"content_reqs": "x", "trajectory_reqs": ""},
            },
            {
                "name": "Q2",
                "query": "h1",
                "requirements": {"default_skills": [], "extra_skills_dir": "", "material_dir": ""},
                "expected_result": {"content_reqs": "x", "trajectory_reqs": ""},
            },
            {
                "name": "Q3",
                "query": "h2",
                "requirements": {"default_skills": [], "extra_skills_dir": "", "material_dir": ""},
                "expected_result": {"content_reqs": "x", "trajectory_reqs": ""},
            },
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_pipeline_two_holdout_task_runs() -> None:
    asyncio.run(_test_pipeline_two_holdout_task_runs())


async def _test_pipeline_two_holdout_task_runs() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        suite_path = tmp_path / "suite.json"
        _suite_json(suite_path)
        report_root = tmp_path / "reports"
        adapter = MockAdapter()
        pipeline = LIFTPipeline(report_root=report_root)
        report = await pipeline.run(
            run_id="evobench-runid-test-pipeline",
            suite_paths=[suite_path],
            adapter=adapter,
            options=RunOptions(repeat=1, incremental_report=False, parallel_repeats=False),
        )
        assert adapter.produce_delta_count == 1
        assert adapter.before_load_count == 2
        assert adapter.after_load_count == 2
        assert len(report.runs) == 1
        suite_run = report.runs[0].suites[0]
        assert len(suite_run.tasks) == 2
        assert suite_run.tasks[0].task_name == "Q2"
        assert suite_run.tasks[1].task_name == "Q3"
        assert suite_run.tasks[0].evolved is not None


def test_pipeline_warmup_only_skips_holdout() -> None:
    asyncio.run(_test_pipeline_warmup_only())


async def _test_pipeline_warmup_only() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        suite_path = tmp_path / "suite.json"
        _suite_json(suite_path)
        adapter = MockAdapter()
        pipeline = LIFTPipeline(report_root=tmp_path / "reports")
        report = await pipeline.run(
            run_id="evobench-runid-test-warmup-only",
            suite_paths=[suite_path],
            adapter=adapter,
            options=RunOptions(warmup_only=True, incremental_report=False),
        )
        assert adapter.produce_delta_count == 1
        assert adapter.before_load_count == 0
        assert adapter.after_load_count == 0
        tasks = report.runs[0].suites[0].tasks
        assert len(tasks) == 0


def _run_all() -> None:
    test_pipeline_two_holdout_task_runs()
    test_pipeline_warmup_only_skips_holdout()
    print("pipeline tests ok")


if __name__ == "__main__":
    _run_all()
