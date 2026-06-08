from __future__ import annotations

from abc import ABC, abstractmethod
from typing import override

from pydantic import BaseModel, ConfigDict, Field

from src_new.models import SuiteTask


class ArtifactPolicy(ABC):
    """hold-out 评测前如何产生产物（UpdateArtifact）的策略。

    将「进化状态如何产生」与「终测题上如何对照加载」解耦。
    默认 HACE 路径为 warmup 题 + 运行时更新钩子；也可扩展为外部注入、跳过 warmup 等。
    """

    @property
    @abstractmethod
    def warmup_tasks(self) -> list[SuiteTask]:
        """触发产物更新前需要执行的题目列表。

        默认 pipeline 中为 suite 的非 hold-out 前缀（``split_suite_tasks`` → warmup 切片）。
        adapter 在 ``produce_delta`` 内执行这些题，再调用 evolve/update；
        它们的 ``PhaseRun`` 不会追加到 eval report。

        返回:
            有序的 warmup 题列表。``HACEPipeline`` 使用的默认
            ``WarmupThenUpdatePolicy`` 要求列表非空。
        """


class WarmupThenUpdatePolicy(BaseModel, ArtifactPolicy):
    """默认策略：先跑 warmup 题，再触发产物更新（evolve）。"""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    tasks: list[SuiteTask] = Field(
        alias="warmup_tasks",
        description="由 split_suite_tasks 切出的非 hold-out 题列表",
    )

    @property
    @override
    def warmup_tasks(self) -> list[SuiteTask]:
        return self.tasks
