"""EvoScientist active evolve runtime tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.lift.adapters.base import SuiteRunContext
from src.lift.adapters.environment import ExecutionEnvironment
from src.lift.adapters.evoscientist.adapter import EvoScientistAdapter
from src.lift.adapters.evoscientist_active_evolve.adapter import (
    EvoScientistActiveEvolveAdapter,
)
from src.lift.adapters.evoscientist_active_evolve.autoskills import (
    _autoskills_evolve_script,
    _parse_autoskills_result,
)
from src.lift.adapters.registry import SUPPORTED_RUNTIMES, create_adapter
from src.lift.pipeline.run_options import RunOptions
from src.paths import EVOSCIENTIST_DOCKER_IMAGE


def test_evoscientist_active_evolve_registered() -> None:
    assert "evoscientist_active_evolve" in SUPPORTED_RUNTIMES
    adapter = create_adapter("evoscientist_active_evolve", RunOptions())
    assert isinstance(adapter, EvoScientistActiveEvolveAdapter)
    assert isinstance(adapter, EvoScientistAdapter)
    assert adapter.resolve_docker_image() == EVOSCIENTIST_DOCKER_IMAGE


def test_autoskills_script_uses_backing_api_not_prompt_channel() -> None:
    script = _autoskills_evolve_script(workspace_dir=object())  # type: ignore[arg-type]
    assert "run_autoskill_now" in script
    assert "client.runs.join" in script
    assert 'set_config_value("memory_skill_synthesis_mode", "auto")' in script
    assert "EvoSci -p" not in script
    assert 'Path("/workspace/task")' in script


def test_parse_autoskills_result_reads_last_sentinel() -> None:
    first = {"run_id": "old", "approved_proposal_count": 0}
    latest = {"run_id": "new", "approved_proposal_count": 2}
    stdout = "\n".join(
        [
            "noise",
            "LIFT_AUTOSKILLS_RESULT=" + json.dumps(first),
            "more noise",
            "LIFT_AUTOSKILLS_RESULT=" + json.dumps(latest),
        ]
    )
    assert _parse_autoskills_result(stdout) == latest


async def test_active_evolve_uses_dedicated_observability_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_run_autoskills_evolve(**kwargs) -> dict[str, object]:
        captured.update(kwargs)
        return {"status": "success"}

    monkeypatch.setattr(
        "src.lift.adapters.evoscientist_active_evolve.adapter.run_autoskills_evolve",
        fake_run_autoskills_evolve,
    )
    adapter = EvoScientistActiveEvolveAdapter(RunOptions())
    env = ExecutionEnvironment(
        disposable=SimpleNamespace(),
        workspace_dir=Path("/tmp/workspace"),
        handle=SimpleNamespace(container_name="container-1"),
    )
    ctx = SuiteRunContext(
        run_id="lift-runid-test",
        repeat_index=2,
        suite_index=3,
        suite_path=Path("suite.json"),
        category_name="cat",
        suite_name="suite",
    )

    await adapter.evolve_after_warmup(env, ctx)

    assert captured["session_id"] == "evolve-autoskills-r2-s3"
