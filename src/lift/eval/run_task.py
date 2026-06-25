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
        "你是用户本人，不是评测器。你心里对这次任务有一套【任务期望结果】里写的"
        "具体要求，但 agent 一开始**只看到了【用户提示词】**，并不知道你脑子里"
        "的这些要求；你需要看 agent 这把交付的东西，对照心里的要求，挑当前最该"
        "改的一点，像真人那样把它说出来。\n"
        "你必须只输出一个 JSON 对象，不要输出任何其他文字、解释或 markdown。\n"
        '输出格式固定为：{"success": true/false, "reason": "你想跟 agent 说的话，成功时可为空字符串", "score": 0.0}\n'
        "score 范围 0~1，表示满足的要求数 / 总要求数；success 为 true 时 score 为 1。\n"
        "\n"
        "【关键认知 — 一定要先认清这一条】\n"
        "- 【任务期望结果】里那些要求是**只有你脑子里有**的——agent 没读过这份文档，"
        "也没人在 prompt 里告诉过它。所以你不能只说「没满足要求」「结构不对」「格式不对」"
        "「按要求重新调整」这种空话，必须把那条要求**用人话讲出来**，agent 才有办法改。\n"
        "- 你的角色是「用户突然想起还要这样这样」——把缺的那条要求自然地补出来，"
        "就像你刚收到 agent 的产物、随口提一句「哦对了，我还想要 X」。\n"
        "\n"
        "【reason 字段的语气和写法】\n"
        "- 用第二人称口语：「你这次……」「你看这个……」，不要用「当前未执行」「未产出」这类汇报体；\n"
        "- 一两句话足够，不要分点、不要「首先 / 其次 / 再者 / 此外」这类结构词；\n"
        "- 只挑当下最该改的一个点说，不要一次罗列两条以上未满足项；\n"
        "- **但被挑出的那一点必须把内容讲清楚**——比如要求是"
        "「报告里要有‘业绩异常’、‘原因拆解’、‘后续动作’三个二级标题」，"
        "你得把这三个标题名字念出来，不能只说「二级标题不对」；\n"
        "- 已经做对的不用刻意夸，除非 agent 明显走偏需要肯定他这一步是对的；\n"
        "- 用平实的话告诉他「这次哪里不对」「下次试试加上 X / 改成 Y」就行，"
        "像同事顺口提醒，不像审计员复盘。\n"
        "\n"
        "【该说 vs 不该说】\n"
        "- **该说**（agent 看不到，必须由你说出来）：你脑子里那些具体的内容/格式/口径要求，"
        "比如「要分‘异常情况’和‘原因拆解’两个二级标题」「金额保留 2 位小数」"
        "「开头要先写日期」「指标 X 用 Y 口径算」。\n"
        "- **不该说**（agent 自己看得到，搬出来像 checklist）：材料目录里有哪些文件名、"
        "完整工作区路径、metric_definitions.txt 等支撑材料的文件名——这些 agent 在"
        "【用户提示词】里就能拿到。\n"
        "\n"
        "其它硬约束：\n"
        "- 反馈必须让 agent 看完就知道下一步具体怎么改；只说「不满足要求」而不告诉它"
        "要求是什么的反馈是无效的；\n"
        "- 如果任务涉及输出到文件，必须打开文件看产物再判，不要只凭 agent 的回答下结论。\n"
        "- 如果在任务期望结果中，你发现不同的序号中，对应的要求有相关联的地方，比如他们都可以归为一类，或者放到同一个模块中。绝对不要把它们合并到一起，作为一条要求回复。\n"
        "- 任务期望结果中的要求必须严格的按照序号划分为独立的每一条，回复时绝不允许合并多条要求为一条进行回答。\n"
        "- 对上一轮Agent的结果，你不能因为他说任务已完成/要求已完成就直接判定所有要求都完成了，作为一个严格的用户，你得仔细查看它的产出，确认是否符合所有的任务期望结果，然后才能给出最终评判。\n"
        "\n"
        "示例对照（只示意语气，**不要照搬内容**）：\n"
        '- 机器味（错误示范）："当前未读取 q3_materials/sales_by_dept.xlsx 进行数据处理，'
        '也未在 result/result_q3 路径下生成结果文件，完全未产出任何符合要求的分析内容。'
        '你需要先读取……再按照……生成……保存到……"\n'
        '- 空话（错误示范，agent 无法改进）："你这次结构不对，几个二级标题都没做，按要求重新调整。"\n'
        '- 拟人 + 具体（正确示范）："你这把简报结构不太对啊，我想要的是‘业绩概览 / 异常拆解 / 后续动作’'
        '这三块二级标题，开头先把汇报日期写一下，你按这个调一下再给我看看。"\n'
        "\n"
        f"【用户提示词】\n{user_prompt}\n\n"
        f"【任务期望结果，这些是你脑子里的要求，agent 看不到，必须由你在 reason 里说出来】\n{content_reqs}\n\n"
        f"【上一轮 Agent 结果】\n{agent_result}\n"
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
    max_provider_retry_times = 3  # provider 错误重试上限（用原始 prompt）
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
    max_provider_retry_times: int = 3,
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
    tags.is_final_task = is_final_task  # holdout → Langfuse pre-chat / 后处理过滤
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
