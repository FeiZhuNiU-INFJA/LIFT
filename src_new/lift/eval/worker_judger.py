"""Worker + judger agents bound for a single task ``run_task`` invocation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from src_new.agents import Agent
from src_new.models import SuiteTask


@dataclass(frozen=True)
class WorkerJudgerPair:
    """单题 ``run_task`` 所需的 worker/judger agent 及其 Langfuse session id。

    Attributes:
        work_agent: 做题 agent。
        judge_agent: 评测 agent。
        work_session_id: worker 的 Langfuse session id。
        judge_session_id: judger 的 Langfuse session id。
    """

    work_agent: Agent
    judge_agent: Agent
    work_session_id: str
    judge_session_id: str


class WorkerJudgerPairFactory(Protocol):
    """Build a ``WorkerJudgerPair`` for a single ``SuiteTask`` (after env is bound)."""

    def __call__(self, task: SuiteTask) -> WorkerJudgerPair:
        """Create and initialize worker/judger agents for ``task``."""
