from __future__ import annotations

import json
import uuid

from json_repair import repair_json
from pydantic import BaseModel, Field

from src.agents import Agent, FornaxUdfTags
from src.benchmark_schema import BenchmarkTask
from src.config import CONFIG


class EvalJudgeResult(BaseModel):
    success: bool = Field(description="是否成功")
    reason: str = Field(description="失败原因")
    score: float = Field(description="任务完成率，0-1的分数，成功的时候应该是1")


def _extract_judge_result(raw_text: str) -> EvalJudgeResult:
    try:
        repaired_text = repair_json(raw_text)
        data = json.loads(repaired_text)
        return EvalJudgeResult.model_validate(data)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Judge response is not valid JSON: {raw_text}") from exc


def _build_judge_prompt(user_prompt: str, agent_result: str, content_reqs: str) -> str:
    return (
        "你是严格评测器。请根据【用户提示词】和【任务期望结果】判定当前任务是否已经完成。\n"
        "你必须只输出一个 JSON 对象，不要输出任何其他文字、解释或 markdown。\n"
        '输出格式固定为：{"success": true/false, "reason": "失败原因，成功时可为空字符串", "score": 0.0}\n'
        "其中 score 范围是 0 到 1。表示任务的完成率，success为true时，score为1\n"
        f"【用户提示词】\n{user_prompt}\n\n"
        f"【任务期望结果】\n{content_reqs}\n\n"
        f"【上一轮Agent回复】\n{agent_result}\n"
    )


def run_task(
    task: BenchmarkTask, run_id: str, agent: Agent, max_turns: int = CONFIG.eval_max_turns
) -> bool:
    # 初始化 FornaxUdfTags
    tags = FornaxUdfTags.init_tags(task, run_id)
    # 用于连续对话的 session ID
    user_session_id = str(uuid.uuid4())
    # 任务当前轮次的用户提示词（初始为任务的提示词）
    current_prompt = task.query

    for _ in range(max_turns):
        agent_result = agent.chat(current_prompt, user_session_id, tags)

        judge_session_id = str(uuid.uuid4())
        # 构建评测器提示词
        judge_prompt = _build_judge_prompt(
            user_prompt=task.query,
            agent_result=agent_result,
            content_reqs=task.expected_result.content_reqs,
        )
        # 评测器回复
        judge_result_text = agent.chat(judge_prompt, judge_session_id, tags)
        # 解析评测器回复
        judge_result = _extract_judge_result(judge_result_text)
        # 更新tags的content_score
        tags.content_score = judge_result.score
        # 如果评测器认为任务完成，则结束任务
        if judge_result.success:
            tags.is_ended = True
            tags.content_score = judge_result.score
            agent.chat("好的，你的任务完成了", user_session_id, tags)
            return True

        current_prompt = judge_result.reason

    tags.is_ended = True
    agent.chat("任务失败，已超过最大尝试次数。", user_session_id, tags)
    return False