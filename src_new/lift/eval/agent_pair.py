from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from src_new.agents import Agent
from src_new.models import SuiteTask


@dataclass(frozen=True)
class TaskAgentPair:
    """Work and judge agents plus session ids for one task execution."""

    work_agent: Agent
    judge_agent: Agent
    work_session_id: str
    judge_session_id: str


class TaskAgentPairFactory(Protocol):
    """Create a work/judge pair for a single ``SuiteTask``."""

    def __call__(self, task: SuiteTask) -> TaskAgentPair:
        """Build and initialize agents for ``task``."""
