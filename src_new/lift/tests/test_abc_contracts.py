from __future__ import annotations

import pytest

from src_new.lift.adapters.base import AgentRuntimeAdapter
from src_new.lift.adapters.container.adapter import ContainerAgentRuntimeAdapter
from src_new.lift.adapters.openclaw.adapter import OpenClawAdapter
from src_new.lift.policies.artifact import ArtifactPolicy, WarmupThenUpdatePolicy
from src_new.lift.runtime.disposable import Disposable
from src_new.models import SuiteTask


def test_runtime_adapter_cannot_instantiate_without_impl() -> None:
    with pytest.raises(TypeError):
        AgentRuntimeAdapter()  # type: ignore[abstract]


def test_container_runtime_adapter_cannot_instantiate_without_impl() -> None:
    with pytest.raises(TypeError):
        ContainerAgentRuntimeAdapter()  # type: ignore[abstract]


def test_openclaw_resolve_docker_image_from_agent_config() -> None:
    assert OpenClawAdapter.resolve_docker_image() == "evolve-eval-openclaw:latest"


def test_openclaw_resolve_docker_image_override() -> None:
    assert OpenClawAdapter.resolve_docker_image(override="custom:tag") == "custom:tag"


def test_artifact_policy_cannot_instantiate_without_impl() -> None:
    with pytest.raises(TypeError):
        ArtifactPolicy()  # type: ignore[abstract]


def test_disposable_cannot_instantiate_without_impl() -> None:
    with pytest.raises(TypeError):
        Disposable()  # type: ignore[abstract]


def test_warmup_policy_is_artifact_policy() -> None:
    policy = WarmupThenUpdatePolicy(
        warmup_tasks=[
            SuiteTask.model_validate(
                {
                    "name": "Q1",
                    "query": "hi",
                    "requirements": {
                        "default_skills": [],
                        "extra_skills_dir": "",
                        "material_dir": "",
                    },
                    "expected_result": {"content_reqs": "x", "trajectory_reqs": ""},
                }
            )
        ]
    )
    assert isinstance(policy, ArtifactPolicy)
    assert policy.warmup_tasks[0].name == "Q1"
