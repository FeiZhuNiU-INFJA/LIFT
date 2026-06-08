from __future__ import annotations

import json
import uuid

from json_repair import repair_json
from pydantic import BaseModel, Field

from src_new.agents import Agent, HermesAgent
from src_new.models import SuiteTask, CustomTags
from src_new.config import CONFIG, LOGGER


class EvalJudgeResult(BaseModel):
    success: bool = Field(description="是否成功")
    reason: str = Field(description="失败原因")
    score: float = Field(description="任务完成率，0-1的分数，成功的时候应该是1")


def _extract_judge_result(raw_text: str) -> EvalJudgeResult:
    try:
        start_idx = raw_text.find("{")
        end_idx = raw_text.rfind("}")
        if start_idx == -1 or end_idx == -1 or start_idx > end_idx:
            raise ValueError("Judge response does not contain a complete JSON object")
        json_candidate = raw_text[start_idx : end_idx + 1]
        repaired_text = repair_json(json_candidate)
        data = json.loads(repaired_text)
        return EvalJudgeResult.model_validate(data)
    except Exception as exc:  # noqa: BLE001
        schema = EvalJudgeResult.model_json_schema()
        raise ValueError("Judge response is not valid JSON. ") from exc


def _build_judge_prompt(user_prompt: str, agent_result: str, content_reqs: str) -> str:
    return (
        "你是严格评测器。请根据【用户提示词】和【任务期望结果】判定当前任务是否已经完成。\n"
        "你必须只输出一个 JSON 对象，不要输出任何其他文字、解释或 markdown。\n"
        '输出格式固定为：{"success": true/false, "reason": "失败原因，需要详细给出不满足的问题点以及应该怎么做，成功时可为空字符串", "score": 0.0}\n'
        "其中 score 范围是 0 到 1。表示任务的完成率，也就是满足的要求数除以总要求数。success为true时，score为1\n"
        "注意：在填写reason字段时需要内容详细，不能只说没满足，要说清楚第一个没有被满足的要求具体是什么、当前哪里没做到、应该怎么改。不要一次性罗列所有未满足要求，只反馈前两个未满足的要求即可，如果只剩一个要求没满足就只需要反馈哪一个就行了。\n"
        "同时，reason不能只写负面的没满足要求，已经满足的要求也得给出正面的反馈，告诉他做得对，而且这些已满足项可以正常说明。\n"
        "此外，reason的填写得保证Agent能够根据这个反馈进行改进来做到满足更多的要求，Agent本身不知道这些任务期望和要求，所以你得在reason中写清楚。\n"
        "最后，reason的语言风格需要自然，符合日常对话习惯。\n"
        "注意，如果存在输出内容到文件的情况，必须读取文件检查成果是否达标，不允许仅根据Agent的回答进行判断。\n"
        f"【用户提示词】\n{user_prompt}\n\n"
        f"【任务期望结果，重点关注输出以及产物内容质量是否达标】\n{content_reqs}\n\n"
        f"【上一轮Agent结果】\n{agent_result}\n"
    )


def _build_judge_prompt_retry(invalid_response: str, error_message: str) -> str:
    return (
        "你上一轮作为评测器的输出无法被程序正确解析，请严格修复并重新输出。\n"
        "你必须只输出一个 JSON 对象，不要输出任何其他文字、解释或 markdown。\n"
        f"注意，输出的 JSON 对象格式固定为：{EvalJudgeResult.model_json_schema()}\n"
        f"【解析错误】\n{error_message}\n\n"
        f"【上一轮错误输出】\n{invalid_response}\n"
    )


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
    """Returns (success, work_session_id, judge_session_id, content_score)."""
    tags = CustomTags.init_tags(task, run_id)
    tags.is_final_task = is_final_task
    tags.is_evolve_turn = is_evolve_turn
    current_prompt = task.query + f"\n你的工作区路径是: {agent._workspace_path}"
    last_content_score: float = 0.0

    for _ in range(max_turns):
        LOGGER.info(f"[{run_id}] [{user_session_id}] User Prompt: {current_prompt}")
        await agent.switch_session(user_session_id)
        agent_result = await agent.chat(
            current_prompt, 
            user_session_id,
            tags,
            chat_role="work_agent",
        )
        LOGGER.info(f"[{run_id}] [{user_session_id}] Agent result: {agent_result}")

        judge_prompt = _build_judge_prompt(
            user_prompt=task.query + f"\n你的工作区路径是: {agent._workspace_path}",
            agent_result=agent_result,
            content_reqs=task.expected_result.content_reqs,
        )
        await agent.switch_session(judge_session_id)
        judge_result_text = await agent.chat(
            judge_prompt,
            judge_session_id,
            tags,
            response_schema=EvalJudgeResult,
            chat_role="judge_agent",
        )
        max_judge_retry_times = 8
        judge_retry_count = 0
        while True:
            try:
                judge_result = _extract_judge_result(judge_result_text)
                break
            except ValueError as exc:
                judge_retry_count += 1
                if judge_retry_count > max_judge_retry_times:
                    LOGGER.exception(
                        "Judge result parse failed after %d retries, session_id=%s, last_response=%r",
                        max_judge_retry_times,
                        judge_session_id,
                        judge_result_text,
                    )
                    raise
                judge_retry_prompt = _build_judge_prompt_retry(
                    judge_result_text, str(exc)
                )
                judge_result_text = await agent.chat(
                    judge_retry_prompt,
                    judge_session_id,
                    tags,
                    response_schema=EvalJudgeResult,
                    chat_role="judge_agent",
                )
        tags.content_score = judge_result.score
        last_content_score = float(judge_result.score)
        if judge_result.success:
            tags.content_score = judge_result.score
            return (True, user_session_id, judge_session_id, last_content_score)

        current_prompt = judge_result.reason + "你再试一次看看能不能完成任务"

    return (False, user_session_id, judge_session_id, last_content_score)


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
    """Returns (success, work_session_id, judge_session_id, content_score)."""
    tags = CustomTags.init_tags(task, run_id)
    tags.is_final_task = is_final_task
    tags.is_evolve_turn = is_evolve_turn
    current_prompt = task.query
    last_content_score: float = 0.0

    for _ in range(max_turns):
        LOGGER.info(f"[{run_id}] [{user_session_id}] User Prompt: {current_prompt}")
        agent_result = await user_agent.chat(
            current_prompt,
            user_session_id,
            tags,
            chat_role="work_agent",
        )
        LOGGER.info(f"[{run_id}] [{user_session_id}] Agent result: {agent_result}")

        judge_prompt = _build_judge_prompt(
            user_prompt=task.query,
            agent_result=agent_result,
            content_reqs=task.expected_result.content_reqs,
        )
        judge_result_text = await judge_agent.chat(
            judge_prompt,
            judge_session_id,
            tags,
            response_schema=EvalJudgeResult,
            chat_role="judge_agent",
        )

        max_judge_retry_times = 8
        judge_retry_count = 0
        while True:
            try:
                judge_result = _extract_judge_result(judge_result_text)
                break
            except ValueError as exc:
                judge_retry_count += 1
                if judge_retry_count > max_judge_retry_times:
                    LOGGER.exception(
                        "Judge result parse failed after %d retries, session_id=%s, last_response=%r",
                        max_judge_retry_times,
                        judge_session_id,
                        judge_result_text,
                    )
                    raise
                judge_retry_prompt = _build_judge_prompt_retry(
                    judge_result_text, str(exc)
                )
                judge_result_text = await judge_agent.chat(
                    judge_retry_prompt,
                    judge_session_id,
                    tags,
                    response_schema=EvalJudgeResult,
                    chat_role="judge_agent",
                )

        tags.content_score = judge_result.score
        last_content_score = float(judge_result.score)
        if judge_result.success:
            tags.content_score = judge_result.score
            return (True, user_session_id, judge_session_id, last_content_score)

        current_prompt = judge_result.reason + "你再试一次看看能不能完成任务"

    return (False, user_session_id, judge_session_id, last_content_score)
