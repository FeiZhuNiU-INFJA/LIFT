"""Unit tests for ``run_task`` work/judge turn loop.

``run_task`` 工作/评判轮次循环的单元测试。
"""

from __future__ import annotations

import json

from pydantic import BaseModel

from src_new.lift.eval.worker_judger import WorkerJudgerPair
from src_new.lift.eval.run_task import run_task
from src_new.models import CustomTags, ExpectedResult, SuiteTask, TaskRequirements


class _FakeAgent:
    """Stub agent that returns scripted chat responses and records calls.

    返回预设 chat 响应并记录调用参数的桩 agent。
    """

    def __init__(self, *, responses: list[str]) -> None:
        """``responses``: FIFO 队列，每次 ``chat`` 弹出一条返回值。"""
        self._responses = list(responses)  # 预设 chat 响应队列
        self.chat_calls: list[tuple[str, str, str]] = []  # (role, session_id, msg)
        self.activate_calls: list[str] = []  # 已 activate 的 session id

    async def activate_session(self, session_id: str) -> None:
        """记录 session 激活（无实际操作）。"""
        self.activate_calls.append(session_id)

    def augment_work_prompt(self, task: SuiteTask, prompt: str) -> str:
        """透传 work prompt。"""
        _ = task
        return prompt

    def augment_judge_user_prompt(self, task: SuiteTask, prompt: str) -> str:
        """透传 judge user prompt。"""
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
        """弹出下一条预设响应并记录调用参数。"""
        _ = tags
        _ = response_schema
        self.chat_calls.append((chat_role, session_id, msg))
        if not self._responses:
            raise RuntimeError("no more fake responses")
        return self._responses.pop(0)


def _task() -> SuiteTask:
    """构造最小合法 ``SuiteTask`` 供单测复用。"""
    return SuiteTask(
        name="Q1",
        query="say hello",
        requirements=TaskRequirements(),
        expected_result=ExpectedResult(content_reqs="reply hello"),
        category_name="Test",
    )


async def test_run_task_success_on_first_turn() -> None:
    """Verify success when work and judge pass on the first turn.

    验证首轮 work 与 judge 均通过时任务成功返回。
    """
    judge_json = json.dumps({"success": True, "reason": "", "score": 1.0})
    work = _FakeAgent(responses=["hello"])
    judge = _FakeAgent(responses=[judge_json])
    pair = WorkerJudgerPair(
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
    """Verify work is retried with judge feedback after a failed judgment.

    验证 judge 失败后携带反馈重试 work agent。
    """
    fail_json = json.dumps(
        {"success": False, "reason": "missing greeting", "score": 0.0}
    )
    ok_json = json.dumps({"success": True, "reason": "", "score": 1.0})
    work = _FakeAgent(responses=["hi", "hello there"])
    judge = _FakeAgent(responses=[fail_json, ok_json])
    pair = WorkerJudgerPair(
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
    """Verify judge retries when its first response is invalid JSON.

    验证 judge 首次返回非 JSON 时会重试解析。
    """
    ok_json = json.dumps({"success": True, "reason": "", "score": 1.0})
    work = _FakeAgent(responses=["hello"])
    judge = _FakeAgent(responses=["not json", ok_json])
    pair = WorkerJudgerPair(
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
