"""Legacy import path for trace backfill (``trace_backfill`` is canonical).

Re-exports backfill helpers and historical ``enrich_*`` aliases for code that
has not yet migrated to the ``trace_backfill`` module name.
"""

from src_new.postprocess.trace_backfill import (
    AgentSource,
    backfill_phase,
    backfill_report,
    get_langfuse_client,
)

# Historical aliases — do not use in new code.
enrich_phase = backfill_phase  # Deprecated alias for backfill_phase.
enrich_report = backfill_report  # Deprecated alias for backfill_report.

__all__ = [
    "AgentSource",
    "backfill_phase",
    "backfill_report",
    "enrich_phase",
    "enrich_report",
    "get_langfuse_client",
]
