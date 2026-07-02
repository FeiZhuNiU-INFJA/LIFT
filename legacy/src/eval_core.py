from __future__ import annotations

import json
import uuid

from json_repair import repair_json
from pydantic import BaseModel, Field

from src.agents import Agent, HermesAgent
from src.models import SuiteTask, CustomTags
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
        raise ValueError("Judge response is not valid JSON. ") from exc


def _build_judge_prompt(user_prompt: str, agent_result: str, content_reqs: str) -> str:
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
    enable_review: bool = True,
) -> tuple[bool, str, str, float]:
    """Returns (success, work_session_id, judge_session_id, content_score)."""
    tags = CustomTags.init_tags(task, run_id)
    tags.is_final_task = is_final_task
    tags.is_evolve_turn = is_evolve_turn
    tags.enable_review = enable_review
    current_prompt = task.query + f"\n你的工作区路径是: {agent._workspace_path}"
    last_content_score: float = 0.0
    success_flag = False

    try:
        for _ in range(max_turns):
            LOGGER.info(f"[{run_id}] [{user_session_id}] User Prompt: {current_prompt}")
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
                success_flag = True
                return (True, user_session_id, judge_session_id, last_content_score)

            current_prompt = judge_result.reason + "你再试一次看看能不能完成任务"

        return (False, user_session_id, judge_session_id, last_content_score)
    finally:
        # 关闭 work / judge 两个 session 对应的 hermes_runner 子进程：
        # - work_agent runner 退出前会跑 background review（阻塞到 review 完成）；
        # - judge_agent runner 不跑 review，立即退出。
        # judge 先关，避免长时间持有不再使用的子进程；work 后关，让 review 与 judge 关闭并行不可能（同协程串行）。
        _ = success_flag
        try:
            await agent.end_session(judge_session_id)
        except Exception:
            LOGGER.exception(
                "Failed to end judge session %s in run_task", judge_session_id
            )
        try:
            await agent.end_session(user_session_id)
        except Exception:
            LOGGER.exception(
                "Failed to end work session %s in run_task", user_session_id
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
