"""Contract tests for abstract base classes and adapter configuration.

抽象基类与适配器配置的契约测试。
"""

from __future__ import annotations

import pytest

from src.lift.adapters.base import AgentRuntimeAdapter
from src.lift.eval.chat_agent import ChatAgent
from src.lift.adapters.container.adapter import ContainerAgentRuntimeAdapter
from src.lift.adapters.openclaw.adapter import OpenClawAdapter
from src.lift.adapters.openclaw_with_openspace.adapter import OpenClawWithOpenSpaceAdapter
from src.lift.adapters.hermes_with_openspace.adapter import HermesWithOpenSpaceAdapter
from src.lift.adapters.registry import SUPPORTED_RUNTIMES
from src.lift.policies.artifact import ArtifactPolicy, WarmupThenUpdatePolicy
from src.lift.runtime.disposable import Disposable
from src.models import SuiteTask


def test_runtime_adapter_cannot_instantiate_without_impl() -> None:
    """Verify ``AgentRuntimeAdapter`` cannot be instantiated directly.

    验证 ``AgentRuntimeAdapter`` 不可直接实例化。
    """
    with pytest.raises(TypeError):
        AgentRuntimeAdapter()  # type: ignore[abstract]


def test_container_runtime_adapter_cannot_instantiate_without_impl() -> None:
    """Verify ``ContainerAgentRuntimeAdapter`` cannot be instantiated directly.

    验证 ``ContainerAgentRuntimeAdapter`` 不可直接实例化。
    """
    with pytest.raises(TypeError):
        ContainerAgentRuntimeAdapter()  # type: ignore[abstract]


def test_openclaw_resolve_docker_image_default() -> None:
    """Verify default OpenClaw Docker image is the built evaluate image tag.

    验证 OpenClaw 默认 Docker 镜像为评测镜像 tag。
    """
    assert OpenClawAdapter.resolve_docker_image() == "lift-openclaw-base:latest"


def test_openclaw_resolve_docker_image_override() -> None:
    """Verify ``resolve_docker_image`` honors an explicit override tag.

    验证 ``resolve_docker_image`` 接受显式 override 标签。
    """
    assert OpenClawAdapter.resolve_docker_image(override="custom:tag") == "custom:tag"


def test_openclaw_with_openspace_resolve_docker_image() -> None:
    """Verify OpenClaw+OpenSpace adapter binds the with-openspace image tag.

    验证 OpenClaw+OpenSpace adapter 绑定 with-openspace 镜像 tag，且尊重 override。
    """
    assert (
        OpenClawWithOpenSpaceAdapter.resolve_docker_image()
        == "lift-openclaw-with-openspace:latest"
    )
    assert (
        OpenClawWithOpenSpaceAdapter.resolve_docker_image(override="custom:tag")
        == "custom:tag"
    )


def test_hermes_with_openspace_resolve_docker_image() -> None:
    """Verify Hermes+OpenSpace adapter binds the with-openspace image tag.

    验证 Hermes+OpenSpace adapter 绑定 with-openspace 镜像 tag，且尊重 override。
    """
    assert (
        HermesWithOpenSpaceAdapter.resolve_docker_image()
        == "lift-hermes-with-openspace:latest"
    )
    assert (
        HermesWithOpenSpaceAdapter.resolve_docker_image(override="custom:tag")
        == "custom:tag"
    )


def test_openspace_runtimes_registered() -> None:
    """Verify the new OpenSpace runtime keys are in ``SUPPORTED_RUNTIMES``.

    验证新增的 OpenSpace runtime key 已注册进 ``SUPPORTED_RUNTIMES``。
    """
    assert "openclaw_with_openspace" in SUPPORTED_RUNTIMES
    assert "hermes_with_openspace" in SUPPORTED_RUNTIMES


def test_artifact_policy_cannot_instantiate_without_impl() -> None:
    """Verify ``ArtifactPolicy`` cannot be instantiated directly.

    验证 ``ArtifactPolicy`` 不可直接实例化。
    """
    with pytest.raises(TypeError):
        ArtifactPolicy()  # type: ignore[abstract]


def test_chat_agent_cannot_instantiate_without_impl() -> None:
    """Verify ``ChatAgent`` cannot be instantiated without ``agent_name`` / ``chat``."""
    with pytest.raises(TypeError):
        ChatAgent()  # type: ignore[abstract]


def test_disposable_cannot_instantiate_without_impl() -> None:
    """Verify ``Disposable`` cannot be instantiated directly.

    验证 ``Disposable`` 不可直接实例化。
    """
    with pytest.raises(TypeError):
        Disposable()  # type: ignore[abstract]


def test_warmup_policy_is_artifact_policy() -> None:
    """Verify ``WarmupThenUpdatePolicy`` is an ``ArtifactPolicy`` with warmup tasks.

    验证 ``WarmupThenUpdatePolicy`` 是带 warmup 任务的 ``ArtifactPolicy`` 子类。
    """
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
