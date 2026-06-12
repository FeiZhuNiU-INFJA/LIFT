"""warmup / hold-out 阶段容器编排策略枚举。

只表达**容器编排维度**（"几个容器、是否并发"）。``apply_evolve`` /
``materialize_delta`` 的行为（产物落到镜像还是外部记忆）由 adapter 类型决定，
不在本枚举里。
"""

from __future__ import annotations

from enum import Enum


class WarmupContainerPolicy(str, Enum):
    """warmup 阶段容器编排策略。

    - ``SERIAL_SINGLE``: 单容器，逐题串行（``for`` 循环）。
    - ``PARALLEL_SINGLE``: 单容器，同容器内 ``asyncio.gather`` 并发跑题。
    - ``PARALLEL_MULTI``: 多容器并发，每题一个独立容器（"模拟多用户"）。
    """

    SERIAL_SINGLE = "serial_single"
    PARALLEL_SINGLE = "parallel_single"
    PARALLEL_MULTI = "parallel_multi"

    @property
    def tasks_parallel(self) -> bool:
        """warmup 多题是否在 Python 层 ``asyncio.gather`` 并发执行。"""
        return self in (
            WarmupContainerPolicy.PARALLEL_SINGLE,
            WarmupContainerPolicy.PARALLEL_MULTI,
        )


class HoldoutContainerPolicy(str, Enum):
    """hold-out 阶段容器编排策略。

    hold-out 每题必须独立容器（baseline 与 evolved 镜像分裂、避免状态污染），
    因此本枚举不提供"单容器"形态，只决定多题之间是否并发。

    - ``SERIAL_MULTI``: 多容器、逐题串行（``for`` 循环），与历史行为兼容。
    - ``PARALLEL_MULTI``: 多容器、``asyncio.gather`` 并发跑题。
    """

    SERIAL_MULTI = "serial_multi"
    PARALLEL_MULTI = "parallel_multi"

    @property
    def tasks_parallel(self) -> bool:
        """hold-out 多题是否在 Python 层 ``asyncio.gather`` 并发执行。"""
        return self is HoldoutContainerPolicy.PARALLEL_MULTI

