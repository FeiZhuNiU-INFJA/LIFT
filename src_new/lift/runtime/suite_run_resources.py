"""单次 suite 评测的资源登记簿（容器、delta）与统一 cleanup。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from src_new.lift.runtime.delta_ref import DeltaRef
from src_new.lift.runtime.disposable import Disposable


class SuiteRunResources(BaseModel):
    """单次 suite 评测（一个 repeat 内的一道 benchmark）的资源登记簿。

    在一次 ``(run_id, repeat_index, suite)`` 评测过程中，adapter 通过 ``track()``
    登记临时容器等资源，``produce_delta`` 完成后写入 ``delta``；suite 跑完后由
    ``cleanup()`` 逆序释放容器并删除 delta 镜像。

    注意：并非整个 ``--repeat`` 共享一份，而是每个 suite 各有一份。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    run_id: str = Field(description="与 SuiteRunContext.run_id 一致")
    repeat_index: int = Field(description="与 SuiteRunContext.repeat_index 一致")
    suite_name: str = Field(description="与 SuiteRunContext.suite_name 一致")
    delta: DeltaRef | None = Field(
        default=None,
        description="warmup + evolve 完成后写入；供 run_after_load 使用",
    )
    disposables: list[Disposable] = Field(
        default_factory=list,
        description="track() 登记的待释放资源（容器会话等），cleanup 时逆序释放",
    )

    _cleaned: bool = PrivateAttr(default=False)

    def track(self, disposable: Disposable) -> Disposable:
        """登记需要在 ``cleanup()`` 时释放的资源（如容器会话）。"""
        self.disposables.append(disposable)
        return disposable

    async def cleanup(self) -> None:
        """释放本 suite 评测登记的所有资源（幂等）。"""
        if self._cleaned:
            return
        for disposable in reversed(self.disposables):
            await disposable.cleanup()
        if self.delta is not None:
            await self.delta.cleanup()
        self._cleaned = True
