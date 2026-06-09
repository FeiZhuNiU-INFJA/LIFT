"""Deprecated shim — prefer ``src_new.lift.eval``.

Legacy entry points keep their old signatures and delegate to the unified
``run_task`` kernel in ``lift/eval/run_task.py``.
"""

from __future__ import annotations

from src_new.agents import Agent, HermesAgent
from src_new.config import CONFIG
from src_new.lift.eval.agent_pair import TaskAgentPair
from src_new.lift.eval.run_task import EvalJudgeResult, run_task as _run_task
from src_new.models import SuiteTask

__all__ = [
    "EvalJudgeResult",
    "openclaw_run_task",
    "run_task",
]


async def run_task(
    task: SuiteTask,
    run_id: str,
    agent: HermesAgent,
    user_session_id: str,
    judge_session_id: str,
    max_turns: int = CONFIG.eval_max_turns,
    is_evolve_turn: bool = False,
    is_final_task: bool = False,
) -> tuple[bool, str, str, float]:
    """Hermes legacy API: one physical agent, two logical sessions."""
    pair = TaskAgentPair(
        work_agent=agent,
        judge_agent=agent,
        work_session_id=user_session_id,
        judge_session_id=judge_session_id,
    )
    return await _run_task(
        task,
        run_id,
        pair,
        max_turns=max_turns,
        is_evolve_turn=is_evolve_turn,
        is_final_task=is_final_task,
    )


async def openclaw_run_task(
    task: SuiteTask,
    run_id: str,
    user_agent: Agent,
    judge_agent: Agent,
    user_session_id: str,
    judge_session_id: str,
    max_turns: int = CONFIG.eval_max_turns,
    is_evolve_turn: bool = False,
    is_final_task: bool = False,
) -> tuple[bool, str, str, float]:
    """OpenClaw legacy API: separate work and judge agents."""
    pair = TaskAgentPair(
        work_agent=user_agent,
        judge_agent=judge_agent,
        work_session_id=user_session_id,
        judge_session_id=judge_session_id,
    )
    return await _run_task(
        task,
        run_id,
        pair,
        max_turns=max_turns,
        is_evolve_turn=is_evolve_turn,
        is_final_task=is_final_task,
    )
