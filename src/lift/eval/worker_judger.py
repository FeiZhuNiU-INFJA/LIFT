"""Worker + judger agents bound for a single task ``run_task`` invocation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Protocol

from src.lift.eval.chat_agent import ChatAgent
from src.models import SuiteTask


@dataclass(frozen=True)
class WorkerJudgerPair:
    """单题 ``run_task`` 所需的 worker/judger agent 及其 Langfuse session id。

    Attributes:
        work_agent: 做题 agent。
        judge_agent: 评测 agent。
        work_session_id: worker 的 Langfuse session id。
        judge_session_id: judger 的 Langfuse session id。
    """

    work_agent: ChatAgent
    judge_agent: ChatAgent
    work_session_id: str  # Langfuse session；OpenClaw = user-*
    judge_session_id: str  # Langfuse session；OpenClaw = judge-*


class WorkerJudgerPairFactory(Protocol):
    """Build a ``WorkerJudgerPair`` for a single ``SuiteTask`` (after env is bound).

    工厂返回 awaitable：单题构建可能需要 docker exec 注册 agent 等同步阻塞操作，
    必须走 async 让事件循环可以调度其它就绪协程；否则 PARALLEL_SINGLE 下多题
    会因前一题 initialize 占据事件循环而被串行化。
    """

    def __call__(self, task: SuiteTask) -> Awaitable[WorkerJudgerPair]:
        """Create and initialize worker/judger agents for ``task``."""
