"""单次 warmup 或 holdout 执行环境的句柄（容器 + workspace + runtime handle）。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.lift.runtime.disposable import Disposable


@dataclass(frozen=True)
class ExecutionEnvironment:
    """单次 warmup 或 holdout 执行环境的不可变句柄。

    Attributes:
        disposable: 容器会话等需在阶段结束时释放的资源。
        workspace_dir: 宿主机上本题/本阶段的 outcome workspace 路径。
        handle: 运行时特定对象（如 ``ContainerSession``），供 adapter 钩子使用。
    """

    disposable: Disposable
    workspace_dir: Path
    handle: Any
