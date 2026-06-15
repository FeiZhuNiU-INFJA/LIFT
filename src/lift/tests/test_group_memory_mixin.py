"""Tests for ``GroupMemoryAdapterMixin`` + ``MultiUserOpenClawAdapter`` glue.

针对群体记忆 Mixin 与 ``MultiUserOpenClawAdapter`` 组合的契约测试。
"""

from __future__ import annotations

import pytest

from src.lift.adapters.container.adapter import ContainerAgentRuntimeAdapter
from src.lift.adapters.group_memory.mixin import GroupMemoryAdapterMixin
from src.lift.adapters.openclaw.adapter import OpenClawAdapter
from src.lift.adapters.openclaw_multi_user.adapter import MultiUserOpenClawAdapter
from src.lift.policies.container import HoldoutContainerPolicy, WarmupContainerPolicy
from src.lift.runtime.delta_ref import DeltaRef


def test_multi_user_openclaw_mro_order() -> None:
    """Verify Mixin precedes runtime adapter in MRO so its overrides win.

    验证 Mixin 在 MRO 中位于 runtime adapter 之前，覆盖优先生效。
    """
    mro = MultiUserOpenClawAdapter.__mro__
    assert mro.index(GroupMemoryAdapterMixin) < mro.index(OpenClawAdapter)
    assert mro.index(OpenClawAdapter) < mro.index(ContainerAgentRuntimeAdapter)


def test_multi_user_openclaw_overrides_produce_delta() -> None:
    """``produce_delta`` must come from Mixin, not from base adapter.

    ``produce_delta`` 必须由 Mixin 提供，而非来自基类。
    """
    assert (
        MultiUserOpenClawAdapter.produce_delta.__qualname__.split(".")[0]
        == "GroupMemoryAdapterMixin"
    )


def test_warmup_container_policy_values() -> None:
    """``WarmupContainerPolicy`` 枚举值与 ``tasks_parallel`` helper 行为契约。"""
    assert WarmupContainerPolicy("serial_single") == WarmupContainerPolicy.SERIAL_SINGLE
    assert WarmupContainerPolicy("parallel_single") == WarmupContainerPolicy.PARALLEL_SINGLE
    assert WarmupContainerPolicy("parallel_multi") == WarmupContainerPolicy.PARALLEL_MULTI
    assert WarmupContainerPolicy.SERIAL_SINGLE.tasks_parallel is False
    assert WarmupContainerPolicy.PARALLEL_SINGLE.tasks_parallel is True
    assert WarmupContainerPolicy.PARALLEL_MULTI.tasks_parallel is True


def test_holdout_container_policy_values() -> None:
    """``HoldoutContainerPolicy`` 枚举值与 ``tasks_parallel`` 契约。"""
    assert HoldoutContainerPolicy("serial_multi") == HoldoutContainerPolicy.SERIAL_MULTI
    assert HoldoutContainerPolicy("parallel_multi") == HoldoutContainerPolicy.PARALLEL_MULTI
    assert HoldoutContainerPolicy.SERIAL_MULTI.tasks_parallel is False
    assert HoldoutContainerPolicy.PARALLEL_MULTI.tasks_parallel is True


def test_multi_user_openclaw_defaults_to_parallel_multi() -> None:
    """``MultiUserOpenClawAdapter`` 默认把 policy 升级为 ``PARALLEL_MULTI``。"""
    adapter = MultiUserOpenClawAdapter()
    assert (
        adapter._options.warmup_container_policy is WarmupContainerPolicy.PARALLEL_MULTI
    )


async def test_delta_ref_unowned_cleanup_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """``owned=False`` 时 ``DeltaRef.cleanup`` 不调用 docker rmi。"""
    delta = DeltaRef(image_tag="evolve-eval-openclaw-base:latest", owned=False)

    called: list[str] = []

    async def fake_remove_image(self, image_tag: str, *, force: bool = True) -> None:  # noqa: ARG001
        called.append(image_tag)

    monkeypatch.setattr(
        "src.lift.runtime.environment_cleaner.EnvironmentCleaner.remove_image",
        fake_remove_image,
    )
    await delta.cleanup()
    await delta.cleanup()  # 幂等
    assert called == []


async def test_delta_ref_owned_cleanup_invokes_rmi(monkeypatch: pytest.MonkeyPatch) -> None:
    """``owned=True`` 时 ``DeltaRef.cleanup`` 仅 rmi 一次（幂等）。"""
    delta = DeltaRef(image_tag="evolve-eval-delta:foo", owned=True)

    called: list[str] = []

    async def fake_remove_image(self, image_tag: str, *, force: bool = True) -> None:  # noqa: ARG001
        called.append(image_tag)

    monkeypatch.setattr(
        "src.lift.runtime.environment_cleaner.EnvironmentCleaner.remove_image",
        fake_remove_image,
    )
    await delta.cleanup()
    await delta.cleanup()  # 第二次幂等
    assert called == ["evolve-eval-delta:foo"]
