"""Single-task work + judge multi-turn loop (runtime-agnostic)."""

from __future__ import annotations

import json
from collections.abc import Callable

from json_repair import repair_json
from pydantic import BaseModel, Field

from src.config import LOGGER
from src.lift.adapters.openclaw.json_output import MAX_TOKENS_TRUNCATION_MARKER
from src.lift.eval.chat_agent import format_outbound_message
from src.lift.eval.worker_judger import WorkerJudgerPair
from src.models import CustomTags, SuiteTask
from src.report.langfuse_reporting import emit_pre_chat_state


class EvalJudgeResult(BaseModel):
    """Judge agent 输出的结构化评测结果（JSON 解析目标）。"""

    success: bool = Field(description="是否成功")
    reason: str = Field(description="失败原因")
    score: float = Field(description="任务完成率，0-1的分数，成功的时候应该是1")


OnTurnCallback = Callable[[int, str, str, EvalJudgeResult], None]
"""``run_task`` 每轮 work↔judge 完成后的回调：(turn_index, work_prompt, work_result, judge_result)。

供 adapter 基类 ``_run_holdout`` 注入，把对话坐标 + 内容 emit 到 status 事件总线，
驱动 dashboard 的"完整对话记录"视图。回调异常被 ``run_task`` 吞掉，绝不拖垮评测。"""


def _extract_judge_result(raw_text: str) -> EvalJudgeResult:
    """从 judge 原始回复中提取并解析 JSON 为 ``EvalJudgeResult``。"""
    try:
        start_idx = raw_text.find("{")
        end_idx = raw_text.rfind("}")
        if start_idx == -1 or end_idx == -1 or start_idx > end_idx:
            raise ValueError("Judge response does not contain a complete JSON object")
        # judge 常夹带 markdown/废话；截取首尾 {} 再 json_repair
        json_candidate = raw_text[start_idx : end_idx + 1]
        repaired_text = repair_json(json_candidate)
        data = json.loads(repaired_text)
        return EvalJudgeResult.model_validate(data)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("Judge response is not valid JSON. ") from exc


def _build_judge_prompt(user_prompt: str, agent_result: str, content_reqs: str) -> str:
    """构造 judge 首轮 prompt（含用户题面、期望结果与 agent 输出）。"""
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


def _emit_pre_chat(
    agent,
    *,
    session_id: str,
    tags: CustomTags,
    chat_role: str,
) -> None:
    """框架统一发 Langfuse pre-chat span（与具体 runtime 无关）。"""
    tags.agent_name = agent.agent_name  # report 层不依赖 ChatAgent 类型，在此桥接
    emit_pre_chat_state(session_id=session_id, tags=tags, chat_role=chat_role)


async def _agent_chat(
    agent,
    message: str,
    *,
    session_id: str,
    tags: CustomTags | None = None,
    chat_role: str | None = None,
) -> str:
    """时间戳 + transport chat；可选地先 emit Langfuse pre-chat span。

    - **首次发送**：传入 ``tags`` 与 ``chat_role``，先落一条 ``*_agent`` pre-chat
      span，然后插件侧（agent_end hook）会紧随其后再写一条 plugin trace。
    - **provider error 重试**：``tags`` / ``chat_role`` 留空（``None``），跳过
      pre-chat span。这样 worker / judge 因 LLM 超时被原地重试 N 次时，所有
      plugin trace 都挂在最初那条 ``*_agent`` span 之下；后处理
      ``_pair_single_session`` 扩展贪心配对算法据此统计
      ``provider_retry_count = 同 agent 下 plugin trace 数 - 1``，
      跨 runtime 通用（不依赖 OpenClaw 特有的 ``plugin_metadata.success`` 字段）。
    """
    if tags is not None and chat_role is not None:
        _emit_pre_chat(agent, session_id=session_id, tags=tags, chat_role=chat_role)
    return await agent.chat(
        format_outbound_message(message),  # GMT+8 时间戳前缀，OpenClaw 等 runtime 约定
        session_id=session_id,
    )


def _build_judge_prompt_retry(invalid_response: str, error_message: str) -> str:
    """构造 judge JSON 解析失败后的重试 prompt。"""
    return (
        "你上一轮作为评测器的输出无法被程序正确解析，请严格修复并重新输出。\n"
        "你必须只输出一个 JSON 对象，不要输出任何其他文字、解释或 markdown。\n"
        f"注意，输出的 JSON 对象格式固定为：{EvalJudgeResult.model_json_schema()}\n"
        f"【解析错误】\n{error_message}\n\n"
        f"【上一轮错误输出】\n{invalid_response}\n"
    )


# 命中即认为是 OpenClaw / runtime 自身的 provider 错误（LLM 超时、限流等），
# 此时 judge 根本没机会真的输出 JSON——继续喂"修复 JSON"的重试 prompt 是
# 浪费配额，应该用原始 judge_prompt 重发。
_PROVIDER_ERROR_MARKERS: tuple[str, ...] = (
    "LLM request timed out",
    "model idle timeout",
    "timeoutSeconds",
    "rate limit",
    "chat exec timeout",  # 宿主机侧 docker exec wall-clock 超时（OpenClawContainerAgent.chat）
    MAX_TOKENS_TRUNCATION_MARKER,  # output_tokens 触达 maxTokens 视同 provider 错误
)


def _looks_like_provider_error(text: str) -> str | None:
    """判断 ``text`` 是否是 OpenClaw runtime 自带的非 JSON 错误回包。

    返回 ``None`` 表示不是；否则返回首行作为简短摘要，供日志 / detail 使用。
    """
    if not text:
        return None
    if not any(m in text for m in _PROVIDER_ERROR_MARKERS):
        return None
    first = text.strip().splitlines()[0].strip() if text.strip() else ""
    return first or "provider error"


async def _judge_with_retry(
    *,
    task: SuiteTask,
    pair: WorkerJudgerPair,
    tags: CustomTags,
    agent_result: str,
) -> EvalJudgeResult:
    """调用 judge chat 并带重试地解析 ``EvalJudgeResult``。

    两条独立的重试通道：

    - **provider 错误**（``LLM request timed out`` 这类 OpenClaw 自带英文错误）：
      用 **原始 judge_prompt** 重发，最多 5 次。这种情况下 judge 根本没机会
      生成内容，"修复 JSON" 的提示对它无意义。
    - **JSON 解析错误**（judge 真的吐了内容但格式不对）：用
      ``_build_judge_prompt_retry`` 把上一轮坏输出 + 错误信息塞回去让它修复，
      最多 8 次。
    """
    judge_user_prompt = pair.judge_agent.augment_judge_user_prompt(task, task.query)
    judge_prompt = _build_judge_prompt(
        user_prompt=judge_user_prompt,
        agent_result=agent_result,
        content_reqs=task.expected_result.content_reqs,
    )
    await pair.judge_agent.activate_session(pair.judge_session_id)
    judge_result_text = await _agent_chat(
        pair.judge_agent,
        judge_prompt,
        session_id=pair.judge_session_id,
        tags=tags,
        chat_role="judge_agent",
    )

    max_judge_retry_times = 8  # JSON 解析重试上限（用 retry prompt）
    max_provider_retry_times = 5  # provider 错误重试上限（用原始 prompt）
    judge_retry_count = 0
    provider_retry_count = 0
    while True:
        # 优先识别 provider error：避免被错当成 JSON 格式问题
        provider_summary = _looks_like_provider_error(judge_result_text)
        if provider_summary is not None:
            provider_retry_count += 1
            if provider_retry_count > max_provider_retry_times:
                LOGGER.error(
                    "Judge provider error after %d retries, session_id=%s, last=%r",
                    max_provider_retry_times,
                    pair.judge_session_id,
                    provider_summary,
                )
                raise RuntimeError(f"provider error: {provider_summary}")
            LOGGER.warning(
                "Judge provider error (retry %d/%d), session_id=%s: %s",
                provider_retry_count, max_provider_retry_times,
                pair.judge_session_id, provider_summary,
            )
            # 用原始 judge_prompt 重发；**不再 emit pre-chat span**，让多次
            # plugin trace 都挂在最初那条 ``judge_agent`` span 下，
            # 后处理 _pair_single_session 据此统计 provider_retry_count。
            judge_result_text = await _agent_chat(
                pair.judge_agent,
                judge_prompt,
                session_id=pair.judge_session_id,
            )
            continue
        try:
            return _extract_judge_result(judge_result_text)
        except ValueError as exc:
            judge_retry_count += 1
            if judge_retry_count > max_judge_retry_times:
                LOGGER.exception(
                    "Judge result parse failed after %d retries, session_id=%s, last_response=%r",
                    max_judge_retry_times,
                    pair.judge_session_id,
                    judge_result_text,
                )
                raise
            judge_retry_prompt = _build_judge_prompt_retry(judge_result_text, str(exc))
            judge_result_text = await _agent_chat(
                pair.judge_agent,
                judge_retry_prompt,
                session_id=pair.judge_session_id,
                tags=tags,
                chat_role="judge_agent",
            )


async def _work_chat_with_provider_retry(
    *,
    pair: WorkerJudgerPair,
    current_prompt: str,
    tags: CustomTags,
    max_provider_retry_times: int = 5,
) -> str:
    """worker chat + provider error 自动重试。

    与 ``_judge_with_retry`` 的 provider 重试通道对称：

    - 第一次正常发：``_agent_chat`` 带 ``tags`` / ``chat_role`` 落
      ``work_agent`` pre-chat span + transport.chat → plugin trace。
    - 命中 ``LLM request timed out`` / ``rate limit`` / ``chat exec timeout`` 等
      marker → 用 ``_agent_chat`` **不带** ``tags`` / ``chat_role`` 重发同 prompt
      （不再开新 pre-chat span），让多次重试 plugin trace 全挂在最初那条
      ``work_agent`` 之下，方便后处理统计 ``provider_retry_count``。
    - 超过 ``max_provider_retry_times`` 仍超时 → 抛
      ``RuntimeError("provider error: ...")`` 让外层题级重试机制接管。
    """
    agent_result = await _agent_chat(
        pair.work_agent,
        current_prompt,
        session_id=pair.work_session_id,
        tags=tags,
        chat_role="work_agent",
    )
    provider_retry_count = 0
    while True:
        provider_summary = _looks_like_provider_error(agent_result)
        if provider_summary is None:
            return agent_result
        provider_retry_count += 1
        if provider_retry_count > max_provider_retry_times:
            LOGGER.error(
                "Worker provider error after %d retries, session_id=%s, last=%r",
                max_provider_retry_times,
                pair.work_session_id,
                provider_summary,
            )
            raise RuntimeError(f"provider error: {provider_summary}")
        LOGGER.warning(
            "Worker provider error (retry %d/%d), session_id=%s: %s",
            provider_retry_count, max_provider_retry_times,
            pair.work_session_id, provider_summary,
        )
        agent_result = await _agent_chat(
            pair.work_agent,
            current_prompt,
            session_id=pair.work_session_id,
        )


async def run_task(
    task: SuiteTask,
    run_id: str,
    pair: WorkerJudgerPair,
    *,
    max_conversation_turns: int = 5,
    is_evolve_turn: bool = False,
    is_final_task: bool = False,
    on_turn: OnTurnCallback | None = None,
) -> tuple[bool, str, str, float, int]:
    """Run one task: work chat + judge loop until success or ``max_conversation_turns``.

    Returns ``(success, work_session_id, judge_session_id, content_score, turns)``。
    ``turns`` 是实际进行的 work↔judge 对话轮数（成功时为达成成功的那轮序号 +1，
    超出最大轮数时等于 ``max_conversation_turns``）。
    Does not schedule multiple tasks; use ``execute_tasks`` for that.
    """
    tags = CustomTags.init_tags(task, run_id)
    tags.is_final_task = is_final_task  # hold-out → Langfuse pre-chat / 后处理过滤
    tags.is_evolve_turn = is_evolve_turn  # after-load → 标记加载了 warmup delta
    current_prompt = pair.work_agent.augment_work_prompt(task, task.query)
    last_content_score: float = 0.0
    turns_executed: int = 0

    for turn_idx in range(max_conversation_turns):
        turns_executed = turn_idx + 1
        LOGGER.info(
            "[%s] [%s] User Prompt: %s",
            run_id,
            pair.work_session_id,
            current_prompt,
        )
        await pair.work_agent.activate_session(pair.work_session_id)
        agent_result = await _work_chat_with_provider_retry(
            pair=pair,
            current_prompt=current_prompt,
            tags=tags,
        )
        LOGGER.info(
            "[%s] [%s] Agent result: %s",
            run_id,
            pair.work_session_id,
            agent_result,
        )

        judge_result = await _judge_with_retry(
            task=task,
            pair=pair,
            tags=tags,
            agent_result=agent_result,
        )

        if on_turn is not None:
            try:
                on_turn(turns_executed, current_prompt, agent_result, judge_result)
            except Exception:  # noqa: BLE001 — 回调绝不能拖垮评测
                LOGGER.warning("on_turn callback failed", exc_info=True)

        tags.content_score = judge_result.score  # 下一轮 pre-chat 会带上最新 score
        last_content_score = float(judge_result.score)
        if judge_result.success:
            return (
                True,
                pair.work_session_id,
                pair.judge_session_id,
                last_content_score,
                turns_executed,
            )

        # judge reason 作为下一轮 work 的用户消息（多轮改进循环）
        current_prompt = judge_result.reason + "你再试一次看看能不能完成任务"

    return (  # 耗尽 max_conversation_turns：success=False，score 为最后一轮 judge 分
        False,
        pair.work_session_id,
        pair.judge_session_id,
        last_content_score,
        turns_executed,
    )
