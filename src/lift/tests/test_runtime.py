"""Unit tests for runtime helpers: delta tags and resource cleanup.

运行时辅助模块单元测试：delta 标签与资源清理。
"""

from __future__ import annotations

from typing import override

from src.lift.runtime.disposable import Disposable
from src.lift.runtime.delta_ref import DeltaRef
from src.lift.runtime.environment_cleaner import EnvironmentCleaner, delta_image_tag
from src.lift.runtime.suite_run_resources import SuiteRunResources


class _FakeDisposable(Disposable):
    """Records whether ``cleanup`` was invoked / 记录 ``cleanup`` 是否被调用。"""

    def __init__(self) -> None:
        """初始化 ``cleaned=False`` 计数器。"""
        self.cleaned = False  # cleanup 是否已执行

    @override
    async def cleanup(self) -> None:
        """标记资源已释放。"""
        self.cleaned = True


class _FakeCleaner(EnvironmentCleaner):
    """Captures container/image removal calls / 捕获容器与镜像删除调用。"""

    def __init__(self) -> None:
        """初始化容器/镜像删除记录列表。"""
        self.removed_images: list[str] = []  # 记录 remove_image 参数
        self.removed_containers: list[str] = []  # 记录 remove_container 参数

    async def remove_container(self, container_name: str) -> None:
        """记录容器删除请求（不调用 docker）。"""
        self.removed_containers.append(container_name)

    async def remove_image(self, image_tag: str, *, force: bool = True) -> None:
        """记录镜像删除请求（不调用 docker）。"""
        _ = force
        self.removed_images.append(image_tag)


def test_delta_image_tag_sanitizes() -> None:
    """Verify ``delta_image_tag`` sanitizes run id and suite name into a valid tag.

    验证 ``delta_image_tag`` 将 run id 与 suite 名 sanitize 为合法标签。
    """
    tag = delta_image_tag(run_id="run/id", repeat_index=2, suite_name="Suite A")
    assert tag == "lift-delta:run-id-r2-Suite-A"
    assert tag.count(":") == 1


async def test_suite_run_resources_cleanup_order() -> None:
    """Verify cleanup runs disposables LIFO, removes delta image, and is idempotent.

    验证 cleanup 按 LIFO 清理 disposable、删除 delta 镜像且可重复调用。
    """
    cleaner = _FakeCleaner()
    delta = DeltaRef(image_tag="lift-delta:test:r0:suite")
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
    assert cleaner.removed_images == ["lift-delta:test:r0:suite"]
