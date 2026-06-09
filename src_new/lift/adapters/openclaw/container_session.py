"""Deprecated: use ``openclaw.session`` and ``container.session``."""

from src_new.lift.adapters.container.session import ContainerSession
from src_new.lift.adapters.openclaw.session import openclaw_context, start_openclaw_container

__all__ = ["ContainerSession", "openclaw_context", "start_openclaw_container"]
