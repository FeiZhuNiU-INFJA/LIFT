"""Runtime-agnostic single-task evaluation (work + judge loop)."""

from src_new.lift.eval.agent_pair import TaskAgentPair, TaskAgentPairFactory
from src_new.lift.eval.phase import execute_phase, execute_phase_batch
from src_new.lift.eval.run_task import EvalJudgeResult, run_task

__all__ = [
    "EvalJudgeResult",
    "TaskAgentPair",
    "TaskAgentPairFactory",
    "execute_phase",
    "execute_phase_batch",
    "run_task",
]
