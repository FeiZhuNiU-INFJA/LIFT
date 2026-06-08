from __future__ import annotations

from enum import Enum


class WarmupContainerPolicy(str, Enum):
    SERIAL_SINGLE = "serial_single"
    PARALLEL_MULTI = "parallel_multi"
