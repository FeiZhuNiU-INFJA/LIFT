from __future__ import annotations

import json
import uuid

from json_repair import repair_json
from pydantic import BaseModel, Field

from src.agents import Agent, FornaxUdfTags
from src.benchmark_schema import BenchmarkTask
from src.config import CONFIG, LOGGER


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
        raise ValueError(
            "Judge response is not valid JSON. "
        ) from exc


def _build_judge_prompt(user_prompt: str, agent_result: str, content_reqs: str) -> str:
    return (
        "你是严格评测器。请根据【用户提示词】和【任务期望结果】判定当前任务是否已经完成。\n"
        "你必须只输出一个 JSON 对象，不要输出任何其他文字、解释或 markdown。\n"
        '输出格式固定为：{"success": true/false, "reason": "失败原因，需要详细给出不满足的问题点以及应该怎么做，成功时可为空字符串", "score": 0.0}\n'
        "其中 score 范围是 0 到 1。表示任务的完成率，success为true时，score为1\n"
        "注意：在填写reason字段时需要内容详细，不能只说没满足，要说哪些没满足，比如指定搜索5个平台上的信息，但是agent实际上只找了2个其中平台，那就得说正确的从2个平台(平台需要给出具体名字)上获取信息了，但是还有3个平台(平台需要给出具体名字)没有找。\n"
        "同时，reason不能只写负面的没满足的要求，满足的要求也得给出正面的反馈，告诉他做得对。\n"
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


def _short_id(n: int = 8) -> str:
    return uuid.uuid4().hex[:n]


async def run_task(
    task: BenchmarkTask, run_id: str, agent: Agent, max_turns: int = CONFIG.eval_max_turns, is_evolve_turn: bool = False
) -> bool:
    # 初始化 FornaxUdfTags
    tags = FornaxUdfTags.init_tags(task, run_id)
    tags.is_evolve_turn = is_evolve_turn
    # 用于连续对话的 session ID
    user_session_id = _short_id()
    # 任务当前轮次的用户提示词（初始为任务的提示词）
    current_prompt = task.query

    for _ in range(max_turns):
        LOGGER.info(f"[{run_id}] [{user_session_id}] User Prompt: {current_prompt}")
        agent_result = await agent.chat(current_prompt, user_session_id, tags)
        LOGGER.info(f"[{run_id}] [{user_session_id}] Agent result: {agent_result}")

        judge_session_id = _short_id()
        # 构建评测器提示词
        judge_prompt = _build_judge_prompt(
            user_prompt=task.query,
            agent_result=agent_result,
            content_reqs=task.expected_result.content_reqs,
        )
        # 评测器回复
        judge_result_text = await agent.chat(
            judge_prompt, judge_session_id, tags, response_schema=EvalJudgeResult
        )
        # 解析评测器回复
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
                judge_retry_prompt = _build_judge_prompt_retry(judge_result_text, str(exc))
                judge_result_text = await agent.chat(
                    judge_retry_prompt,
                    judge_session_id,
                    tags,
                    response_schema=EvalJudgeResult,
                )
        # 更新tags的content_score
        tags.content_score = judge_result.score
        # 如果评测器认为任务完成，则结束任务
        if judge_result.success:
            tags.is_ended = True
            tags.content_score = judge_result.score
            await agent.chat("好的，你的任务完成了", user_session_id, tags)
            # 任务结束，主动触发进化
            LOGGER.info("Triggering agent evolution...")
            await agent.evolve(user_session_id)
            return True
        
        # 如果评测器认为任务未完成，则更新当前提示词为失败原因和任务的提示词，并要求再试一次
        current_prompt = judge_result.reason + "你再试一次看看能不能完成任务"

    tags.is_ended = True
    await agent.chat("任务失败，已超过最大尝试次数。", user_session_id, tags)
    # 任务结束，主动触发进化
    LOGGER.info("Triggering agent evolution...")
    await agent.evolve(user_session_id)
    return False
