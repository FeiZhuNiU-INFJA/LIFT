from __future__ import annotations

import asyncio
from typing import override

from src_new.lift.runtime.disposable import Disposable
from src_new.lift.runtime.delta_ref import DeltaRef
from src_new.lift.runtime.environment_cleaner import EnvironmentCleaner, delta_image_tag
from src_new.lift.runtime.suite_run_resources import SuiteRunResources


class _FakeDisposable(Disposable):
    def __init__(self) -> None:
        self.cleaned = False

    @override
    async def cleanup(self) -> None:
        self.cleaned = True


class _FakeCleaner(EnvironmentCleaner):
    def __init__(self) -> None:
        self.removed_images: list[str] = []
        self.removed_containers: list[str] = []

    async def remove_container(self, container_name: str) -> None:
        self.removed_containers.append(container_name)

    async def remove_image(self, image_tag: str, *, force: bool = True) -> None:
        self.removed_images.append(image_tag)


def test_delta_image_tag_sanitizes() -> None:
    tag = delta_image_tag(run_id="run/id", repeat_index=2, suite_name="Suite A")
    assert tag == "evolve-eval-delta:run-id-r2-Suite-A"
    assert tag.count(":") == 1


def test_suite_run_resources_cleanup_order() -> None:
    asyncio.run(_test_suite_run_resources_cleanup_order())


async def _test_suite_run_resources_cleanup_order() -> None:
    cleaner = _FakeCleaner()
    delta = DeltaRef(image_tag="evolve-eval-delta:test:r0:suite")
    delta._cleaner = cleaner
    resources = SuiteRunResources(
        run_id="test", repeat_index=0, suite_name="suite", delta=delta
    )
    first = _FakeDisposable()
    second = _FakeDisposable()
    resources.track(first)
    resources.track(second)

    await resources.cleanup()
    await resources.cleanup()

    assert second.cleaned
    assert first.cleaned
    assert cleaner.removed_images == ["evolve-eval-delta:test:r0:suite"]


def _run_all() -> None:
    test_delta_image_tag_sanitizes()
    test_suite_run_resources_cleanup_order()
    print("runtime tests ok")


if __name__ == "__main__":
    _run_all()
