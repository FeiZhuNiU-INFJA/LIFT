"""``--agent-runtime`` 工厂注册：按名称构造 ``AgentRuntimeAdapter`` 实例。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src_new.lift.pipeline.run_options import RunOptions

if TYPE_CHECKING:
    from src_new.lift.adapters.base import AgentRuntimeAdapter

SUPPORTED_RUNTIMES = ("openclaw",)  # CLI 可选的运行时标识


def create_adapter(runtime: str, options: RunOptions) -> AgentRuntimeAdapter:
    """根据 ``runtime`` 名称构造对应的 ``AgentRuntimeAdapter``。"""
    normalized = runtime.strip().lower()
    if normalized == "openclaw":
        from src_new.lift.adapters.openclaw.adapter import OpenClawAdapter

        return OpenClawAdapter(options)
    supported = ", ".join(SUPPORTED_RUNTIMES)
    raise ValueError(f"Unknown runtime {runtime!r}; supported: {supported}")
