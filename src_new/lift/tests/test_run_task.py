from __future__ import annotations

import json

from pydantic import BaseModel

from src_new.lift.eval.agent_pair import TaskAgentPair
from src_new.lift.eval.run_task import run_task
from src_new.models import CustomTags, ExpectedResult, SuiteTask, TaskRequirements


class _FakeAgent:
    def __init__(self, *, responses: list[str]) -> None:
        self._responses = list(responses)
        self.chat_calls: list[tuple[str, str, str]] = []
        self.activate_calls: list[str] = []

    async def activate_session(self, session_id: str) -> None:
        self.activate_calls.append(session_id)

    def augment_work_prompt(self, task: SuiteTask, prompt: str) -> str:
        _ = task
        return prompt

    def augment_judge_user_prompt(self, task: SuiteTask, prompt: str) -> str:
        _ = task
        return prompt

    async def chat(
        self,
        msg: str,
        session_id: str,
        tags: CustomTags,
        response_schema: BaseModel | None = None,
        *,
        chat_role: str = "work_agent",
    ) -> str:
        _ = tags
        _ = response_schema
        self.chat_calls.append((chat_role, session_id, msg))
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
    judge_json = json.dumps({"success": True, "reason": "", "score": 1.0})
    work = _FakeAgent(responses=["hello"])
    judge = _FakeAgent(responses=[judge_json])
    pair = TaskAgentPair(
        work_agent=work,  # type: ignore[arg-type]
        judge_agent=judge,  # type: ignore[arg-type]
        work_session_id="work-1",
        judge_session_id="judge-1",
    )

    success, work_sid, judge_sid, score = await run_task(
        _task(),
        "run-1",
        pair,
        max_turns=2,
    )

    assert success is True
    assert work_sid == "work-1"
    assert judge_sid == "judge-1"
    assert score == 1.0
    assert work.chat_calls[0][0] == "work_agent"
    assert judge.chat_calls[0][0] == "judge_agent"
    assert work.activate_calls == ["work-1"]
    assert judge.activate_calls == ["judge-1"]


async def test_run_task_retries_work_after_judge_failure() -> None:
    fail_json = json.dumps(
        {"success": False, "reason": "missing greeting", "score": 0.0}
    )
    ok_json = json.dumps({"success": True, "reason": "", "score": 1.0})
    work = _FakeAgent(responses=["hi", "hello there"])
    judge = _FakeAgent(responses=[fail_json, ok_json])
    pair = TaskAgentPair(
        work_agent=work,  # type: ignore[arg-type]
        judge_agent=judge,  # type: ignore[arg-type]
        work_session_id="work-1",
        judge_session_id="judge-1",
    )

    success, _, _, score = await run_task(_task(), "run-1", pair, max_turns=2)

    assert success is True
    assert score == 1.0
    assert len(work.chat_calls) == 2
    assert "missing greeting" in work.chat_calls[1][2]


async def test_run_task_judge_parse_retry() -> None:
    ok_json = json.dumps({"success": True, "reason": "", "score": 1.0})
    work = _FakeAgent(responses=["hello"])
    judge = _FakeAgent(responses=["not json", ok_json])
    pair = TaskAgentPair(
        work_agent=work,  # type: ignore[arg-type]
        judge_agent=judge,  # type: ignore[arg-type]
        work_session_id="work-1",
        judge_session_id="judge-1",
    )

    success, _, _, score = await run_task(_task(), "run-1", pair, max_turns=1)

    assert success is True
    assert score == 1.0
    assert len(judge.chat_calls) == 2
    assert judge.chat_calls[1][0] == "judge_agent"
