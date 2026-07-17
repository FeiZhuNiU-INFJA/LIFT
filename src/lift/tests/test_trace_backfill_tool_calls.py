"""Backfill ``PhaseRun.tool_calls`` 兜底逻辑单测（`fallback_tool_calls`）。

覆盖两条 langfuse work_analytics 口径的 max 归并：
- ``tool_observation_count``：GA 侧路径（overlay 每次工具挂 as_type='tool' span）。
- ``tool_call_blocks``：EvoScientist 侧路径（overlay 只在 plugin trace metadata
  里累加 ``toolCallBlocks``，不挂 tool observation）。
"""

from __future__ import annotations

from src.models import (
    LangfuseTokenToolStats,
    LangfuseWorkSessionAnalytics,
    PhaseLangfuseBundle,
)
from src.postprocess.trace_backfill import fallback_tool_calls


def _bundle(*, tool_observation_count: int = 0, tool_call_blocks: int = 0) -> PhaseLangfuseBundle:
    return PhaseLangfuseBundle(
        eval_run_tag="t",
        work_session_id="w",
        judge_session_id="j",
        work_analytics=LangfuseWorkSessionAnalytics(
            global_stats=LangfuseTokenToolStats(
                tool_observation_count=tool_observation_count,
                tool_call_blocks=tool_call_blocks,
            ),
        ),
    )


def test_short_circuits_when_phase_tool_calls_already_set() -> None:
    # OpenClaw 主链路（trajectory.jsonl 已读出精确值）不能被兜底覆盖。
    bundle = _bundle(tool_observation_count=99, tool_call_blocks=42)
    assert fallback_tool_calls(3, bundle) is None


def test_returns_none_when_work_analytics_missing() -> None:
    bundle = PhaseLangfuseBundle(eval_run_tag="t", work_session_id="w", judge_session_id="j")
    assert fallback_tool_calls(None, bundle) is None


def test_returns_none_when_both_metrics_zero() -> None:
    # dashboard 显示 "—" 优于假 0（避免误导为“确认没调工具”）。
    assert fallback_tool_calls(None, _bundle()) is None


def test_falls_back_to_tool_observation_count() -> None:
    # GA overlay 路径：只有 tool_observation_count。
    assert fallback_tool_calls(None, _bundle(tool_observation_count=5)) == 5


def test_falls_back_to_tool_call_blocks() -> None:
    # EvoScientist overlay 路径：只有 tool_call_blocks（无 as_type='tool' 子 span）。
    assert fallback_tool_calls(None, _bundle(tool_call_blocks=7)) == 7


def test_max_of_two_metrics() -> None:
    # 两条口径都有值时按可信度取 max（对应上限口径）。
    assert fallback_tool_calls(None, _bundle(tool_observation_count=3, tool_call_blocks=8)) == 8
    assert fallback_tool_calls(None, _bundle(tool_observation_count=9, tool_call_blocks=4)) == 9
