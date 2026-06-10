"""warmup 阶段容器编排策略枚举。"""

from __future__ import annotations

from enum import Enum


class WarmupContainerPolicy(str, Enum):
    """warmup 阶段容器编排策略。"""

    SERIAL_SINGLE = "serial_single"  # 单容器串行跑全部 warmup 题（当前默认、已实现）
    PARALLEL_MULTI = "parallel_multi"  # 每道 warmup 题独立容器（尚未实现）
