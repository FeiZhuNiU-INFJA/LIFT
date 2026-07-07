"""Unit tests for holdout 对话事件流（A 运行期 + B 后处理注入）。

覆盖：

- ``DialogueTurnEvent`` → ``RunStateTracker`` 折叠到 ``PhaseNode.dialogue``（含单调去重）
- ``set_dialogue`` 后处理覆盖 + 迟到的运行期事件被忽略
- 坐标缺失时 ``set_dialogue`` 静默跳过
- ``build_dialogue_bundle_from_report`` 从 backfilled report 提取对话（数组下标坐标）
- ``run_task`` 的 ``on_turn`` 回调在每轮 work↔judge 后被正确调用，且异常被吞掉
"""

from __future__ import annotations

import json

import pytest

from src.lift.eval.chat_agent import ChatAgent
from src.lift.eval.run_task import run_task
from src.lift.eval.worker_judger import WorkerJudgerPair
from src.lift.status import events as ev
from src.lift.status.state import DialogueTurn, RunStateTracker
from src.models import ExpectedResult, SuiteTask, TaskRequirements
from src.postprocess.run_post_process import build_dialogue_bundle_from_report


class _FakeAgent(ChatAgent):
    """Stub agent that returns scripted chat responses and records calls."""

    def __init__(self, *, responses: list[str]) -> None:
        self._responses = list(responses)
        self.chat_calls: list[tuple[str, str]] = []

    @property
    def agent_name(self) -> str:
        return "fake-agent"

    async def activate_session(self, session_id: str) -> None:
        await super().activate_session(session_id)

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


@pytest.fixture
def tracker():
    """每个测试独立的 tracker：清空全局监听器 → attach → 测试后 detach。"""
    ev.clear_listeners()
    t = RunStateTracker()
    t.attach()
    yield t
    t.detach()
    ev.clear_listeners()


def _seed_skeleton(run_id: str = "r", task: str = "Q1") -> None:
    """广播 run/suite 计划，预建 holdout task 骨架。"""
    ev.emit_run_plan(run_id=run_id, repeats=1, suite_names=("s",))
    ev.emit_suite_plan(
        run_id=run_id,
        repeat_index=0,
        suite_index=0,
        suite_name="s",
        warmup_task_names=(),
        holdout_task_names=(task,),
    )


def _emit_turn(turn_index: int, *, work_prompt: str, work_result: str,
               judge_success: bool, judge_score: float, judge_reason: str,
               phase: str = "baseline", task: str = "Q1") -> None:
    ev.emit_dialogue_turn(
        run_id="r", repeat_index=0, suite_index=0, suite_name="s",
        task_name=task, phase=phase, turn_index=turn_index,
        work_prompt=work_prompt, work_result=work_result,
        judge_success=judge_success, judge_score=judge_score, judge_reason=judge_reason,
    )


def test_dialogue_turns_accumulate(tracker: RunStateTracker) -> None:
    """多轮 DialogueTurnEvent 按 turn_index 单调追加到 PhaseNode.dialogue。"""
    _seed_skeleton()
    _emit_turn(1, work_prompt="p1", work_result="r1", judge_success=False, judge_score=0.0, judge_reason="x")
    _emit_turn(2, work_prompt="p2", work_result="r2", judge_success=False, judge_score=0.5, judge_reason="y")
    _emit_turn(3, work_prompt="p3", work_result="r3", judge_success=True, judge_score=1.0, judge_reason="")

    snap = tracker.snapshot()
    ph = snap.repeats[0].suites[0].holdout_tasks["Q1"].phases["baseline"]
    assert ph.dialogue_source == "runtime"
    assert [t.turn_index for t in ph.dialogue] == [1, 2, 3]
    assert ph.dialogue[2].judge_success is True
    assert ph.dialogue[0].work_prompt == "p1"
    assert ph.dialogue[0].latency_seconds is None  # A 路径无 latency


def test_dialogue_turn_index_dedup(tracker: RunStateTracker) -> None:
    """同 turn_index 的事件（重试）不重复追加。"""
    _seed_skeleton()
    _emit_turn(1, work_prompt="p1", work_result="r1", judge_success=False, judge_score=0.0, judge_reason="x")
    _emit_turn(1, work_prompt="p1b", work_result="r1b", judge_success=False, judge_score=0.0, judge_reason="x")

    ph = tracker.snapshot().repeats[0].suites[0].holdout_tasks["Q1"].phases["baseline"]
    assert len(ph.dialogue) == 1
    assert ph.dialogue[0].work_prompt == "p1"  # 首条保留


def test_set_dialogue_overrides_and_ignores_late_runtime(tracker: RunStateTracker) -> None:
    """B 路径 set_dialogue 覆盖运行期对话，之后的运行期事件被忽略。"""
    _seed_skeleton()
    _emit_turn(1, work_prompt="runtime-p", work_result="runtime-r",
               judge_success=False, judge_score=0.0, judge_reason="runtime")

    tracker.set_dialogue({(0, 0, "Q1", "baseline"): [
        DialogueTurn(turn_index=1, work_prompt="BP", work_result="BR",
                     judge_success=False, judge_score=0.0, judge_reason="", latency_seconds=1.5),
    ]})

    # 迟到的运行期事件：应被忽略（source 已是 postprocess）
    _emit_turn(9, work_prompt="late", work_result="lr", judge_success=True, judge_score=1.0, judge_reason="late")

    ph = tracker.snapshot().repeats[0].suites[0].holdout_tasks["Q1"].phases["baseline"]
    assert ph.dialogue_source == "postprocess"
    assert len(ph.dialogue) == 1
    assert ph.dialogue[0].work_prompt == "BP"
    assert ph.dialogue[0].latency_seconds == 1.5


def test_set_dialogue_missing_coords_skipped(tracker: RunStateTracker) -> None:
    """坐标定位失败（节点不存在）静默跳过，不抛错。"""
    _seed_skeleton()
    tracker.set_dialogue({(99, 99, "ghost", "baseline"): [
        DialogueTurn(turn_index=1, work_prompt="x", work_result="y",
                     judge_success=False, judge_score=0.0, judge_reason="")]})

    ph = tracker.snapshot().repeats[0].suites[0].holdout_tasks["Q1"].phases["baseline"]
    assert ph.dialogue == []


def test_build_dialogue_bundle_from_report() -> None:
    """从 backfilled report 按数组下标坐标提取 trace_chain 文本对话。"""
    fake = {
        "runs": [
            {"suites": [
                {"tasks": [
                    {"task_name": "Q1", "baseline": {"langfuse": {"work_analytics": {"trace_chain": [
                        {"turn_index": 1, "input": "do ppt", "output": "draft", "latency_seconds": 2.3},
                        {"turn_index": 2, "input": {"role": "user", "content": "fix cover"},
                         "output": ["x", "y"]},
                    ]}}}},
                    {"task_name": "Q2", "baseline": {"langfuse": {"work_analytics": {}}},
                     "evolved": {"langfuse": {"work_analytics": {"trace_chain": [
                        {"turn_index": 1, "input": "q2", "output": "a2"},
                     ]}}}},
                ]}
            ]}
        ]
    }
    bundle = build_dialogue_bundle_from_report(fake)
    assert (0, 0, "Q1", "baseline") in bundle
    assert (0, 0, "Q2", "evolved") in bundle
    assert (0, 0, "Q2", "baseline") not in bundle  # 空 work_analytics → 不进 bundle

    q1 = bundle[(0, 0, "Q1", "baseline")]
    assert len(q1) == 2
    assert q1[0].work_prompt == "do ppt"
    assert q1[0].latency_seconds == 2.3
    assert q1[1].work_prompt == '{"role": "user", "content": "fix cover"}'  # dict → JSON 文本
    assert q1[1].judge_reason == ""  # B 路径 trace_chain 仅 work 侧，无 judge


def test_build_dialogue_bundle_skips_phase_with_all_none_outputs() -> None:
    """plugin trace 全缺失（trace_chain 每轮 output 均为 None）时整个 phase 跳过。

    此时 ``_dialogue_io`` 把 ``input`` 兜底成 CustomTags 元 JSON，``output`` 留空——
    B 路径信息量不如 A 路径 runtime dialogue，不进 bundle 从而保留运行期版本。
    """
    fake = {
        "runs": [
            {"suites": [
                {"tasks": [
                    {"task_name": "Q1", "baseline": {"langfuse": {"work_analytics": {"trace_chain": [
                        {"turn_index": 0, "input": {"task_query": "hi"}, "output": None},
                    ]}}},
                     "evolved": {"langfuse": {"work_analytics": {"trace_chain": [
                        {"turn_index": 0, "input": "real prompt", "output": "real answer"},
                     ]}}}},
                ]}
            ]}
        ]
    }
    bundle = build_dialogue_bundle_from_report(fake)
    assert (0, 0, "Q1", "baseline") not in bundle  # 全 None output → 跳过
    assert (0, 0, "Q1", "evolved") in bundle       # 有 output → 保留


async def test_run_task_invokes_on_turn_per_turn() -> None:
    """on_turn 在每轮 work↔judge 完成后被调用，参数含 work prompt/result + judge_result。"""
    fail_json = json.dumps({"success": False, "reason": "missing greeting", "score": 0.0})
    ok_json = json.dumps({"success": True, "reason": "", "score": 1.0})
    pair = WorkerJudgerPair(
        work_agent=_FakeAgent(responses=["hi", "hello there"]),
        judge_agent=_FakeAgent(responses=[fail_json, ok_json]),
        work_session_id="work-1",
        judge_session_id="judge-1",
    )
    calls: list[tuple] = []

    def on_turn(turn_idx, work_prompt, work_result, judge_result):  # noqa: ANN001
        calls.append((turn_idx, work_prompt, work_result,
                      judge_result.success, judge_result.score, judge_result.reason))

    success, _, _, _, turns = await run_task(
        _task(), "run-1", pair, max_conversation_turns=2, on_turn=on_turn)

    assert success is True and turns == 2
    assert len(calls) == 2
    assert calls[0][0] == 1 and calls[1][0] == 2
    assert calls[0][3] is False and calls[0][5] == "missing greeting"
    assert calls[1][3] is True and calls[1][4] == 1.0


async def test_run_task_on_turn_exception_swallowed() -> None:
    """on_turn 抛异常时被吞掉，不拖垮评测。"""
    ok_json = json.dumps({"success": True, "reason": "", "score": 1.0})
    pair = WorkerJudgerPair(
        work_agent=_FakeAgent(responses=["hello"]),
        judge_agent=_FakeAgent(responses=[ok_json]),
        work_session_id="w",
        judge_session_id="j",
    )

    def bad_on_turn(*_a, **_kw):  # noqa: ANN001
        raise RuntimeError("boom")

    success, *_ = await run_task(
        _task(), "run-1", pair, max_conversation_turns=1, on_turn=bad_on_turn)
    assert success is True
