"""Unit tests for ``run_task`` work/judge turn loop.

``run_task`` 工作/评判轮次循环的单元测试。
"""

from __future__ import annotations

import json

from src.lift.eval.chat_agent import ChatAgent
from src.lift.eval.run_task import run_task
from src.lift.eval.worker_judger import WorkerJudgerPair
from src.models import ExpectedResult, SuiteTask, TaskRequirements


class _FakeAgent(ChatAgent):
    """Stub agent that returns scripted chat responses and records calls."""

    def __init__(self, *, responses: list[str]) -> None:
        """``responses``: FIFO 队列，每次 ``chat`` 弹出一条返回值。"""
        self._responses = list(responses)
        self.chat_calls: list[tuple[str, str]] = []  # (session_id, msg)
        self.activate_calls: list[str] = []

    @property
    def agent_name(self) -> str:
        return "fake-agent"

    async def activate_session(self, session_id: str) -> None:
        await super().activate_session(session_id)
        self.activate_calls.append(session_id)

    async def chat(self, message: str, *, session_id: str) -> str:
        self.chat_calls.append((session_id, message))
        if not self._responses:
            raise RuntimeError("no more fake responses")
        return self._responses.pop(0)


def _task() -> SuiteTask:
    return SuiteTask(
        name="Q1",
        query="say hello",
        requirements=TaskRequirements(),
        expected_result=ExpectedResult(content_reqs="reply hello"),
        category_name="Test",
    )


async def test_run_task_success_on_first_turn() -> None:
    """Verify success when work and judge pass on the first turn."""
    judge_json = json.dumps({"success": True, "reason": "", "score": 1.0})
    work = _FakeAgent(responses=["hello"])
    judge = _FakeAgent(responses=[judge_json])
    pair = WorkerJudgerPair(
        work_agent=work,
        judge_agent=judge,
        work_session_id="work-1",
        judge_session_id="judge-1",
    )

    success, work_sid, judge_sid, score, turns = await run_task(
        _task(),
        "run-1",
        pair,
        max_conversation_turns=2,
    )

    assert success is True
    assert work_sid == "work-1"
    assert judge_sid == "judge-1"
    assert score == 1.0
    assert turns == 1
    assert work.chat_calls[0][0] == "work-1"
    assert judge.chat_calls[0][0] == "judge-1"
    assert work.activate_calls == ["work-1"]
    assert judge.activate_calls == ["judge-1"]


async def test_run_task_retries_work_after_judge_failure() -> None:
    """Verify work is retried with judge feedback after a failed judgment."""
    fail_json = json.dumps(
        {"success": False, "reason": "missing greeting", "score": 0.0}
    )
    ok_json = json.dumps({"success": True, "reason": "", "score": 1.0})
    work = _FakeAgent(responses=["hi", "hello there"])
    judge = _FakeAgent(responses=[fail_json, ok_json])
    pair = WorkerJudgerPair(
        work_agent=work,
        judge_agent=judge,
        work_session_id="work-1",
        judge_session_id="judge-1",
    )

    success, _, _, score, turns = await run_task(_task(), "run-1", pair, max_conversation_turns=2)

    assert success is True
    assert score == 1.0
    assert turns == 2
    assert len(work.chat_calls) == 2
    assert "missing greeting" in work.chat_calls[1][1]


async def test_run_task_judge_parse_retry() -> None:
    """Verify judge retries when its first response is invalid JSON."""
    ok_json = json.dumps({"success": True, "reason": "", "score": 1.0})
    work = _FakeAgent(responses=["hello"])
    judge = _FakeAgent(responses=["not json", ok_json])
    pair = WorkerJudgerPair(
        work_agent=work,
        judge_agent=judge,
        work_session_id="work-1",
        judge_session_id="judge-1",
    )

    success, _, _, score, turns = await run_task(_task(), "run-1", pair, max_conversation_turns=1)

    assert success is True
    assert score == 1.0
    assert turns == 1
    assert len(judge.chat_calls) == 2
