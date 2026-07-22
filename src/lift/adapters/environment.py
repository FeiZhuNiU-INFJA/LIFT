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
        disposable: 容器会话等需在阶段结束时释放的资源。对"work + judge 分容器"的
            runtime，这是一个 ``CompositeDisposable``，同时覆盖 work 与 judge 两个容器。
        workspace_dir: 宿主机上本题/本阶段的 outcome workspace 路径。
        handle: 运行时特定对象（如 ``ContainerSession``），供 adapter 钩子使用。约定
            为 **work 容器**——evolve / materialize_delta / count_tool_calls 等钩子只
            作用于 work 容器（judge 容器纯临时，永不 commit）。
        judge_handle: judge 专属的运行时对象（如 judge 容器的 ``ContainerSession``）。
            work agent 跑在 ``handle`` 容器，judge agent 跑在 ``judge_handle`` 容器，
            二者除所在容器外完全一致。为 ``None`` 时表示 legacy / 自定义编排未拆分，
            factory 会回退到与 work 共用 ``handle`` 容器。
    """

    disposable: Disposable
    workspace_dir: Path
    handle: Any
    judge_handle: Any = None
