"""Worker + judger agents bound for a single task ``run_task`` invocation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from src_new.agents import Agent
from src_new.models import SuiteTask


@dataclass(frozen=True)
class WorkerJudgerPair:
    """Worker agent, judger agent, and their Langfuse session ids for one task."""

    work_agent: Agent
    judge_agent: Agent
    work_session_id: str
    judge_session_id: str


class WorkerJudgerPairFactory(Protocol):
    """Build a ``WorkerJudgerPair`` for a single ``SuiteTask`` (after env is bound)."""

    def __call__(self, task: SuiteTask) -> WorkerJudgerPair:
        """Create and initialize worker/judger agents for ``task``."""
