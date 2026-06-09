from __future__ import annotations

from typing import TYPE_CHECKING

from src_new.lift.pipeline.run_options import RunOptions

if TYPE_CHECKING:
    from src_new.lift.adapters.base import AgentRuntimeAdapter

SUPPORTED_RUNTIMES = ("openclaw",)


def create_adapter(runtime: str, options: RunOptions) -> AgentRuntimeAdapter:
    normalized = runtime.strip().lower()
    if normalized == "openclaw":
        from src_new.lift.adapters.openclaw.adapter import OpenClawAdapter

        return OpenClawAdapter(options)
    supported = ", ".join(SUPPORTED_RUNTIMES)
    raise ValueError(f"Unknown runtime {runtime!r}; supported: {supported}")
