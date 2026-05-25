import json
import re
from typing import Any
from time import sleep
from src.config import LOGGER
import pandas as pd

from postprocess.config import JUDGE_MODEL_NAME, OPENAI_API_KEY, OPENAI_BASE_URL, USE_JUDGE

SYSTEM_PROMPT_TEMPLATE = """你是一个Agent轨迹评判高手，你会了解到目标任务、任务要求以及一个其他Agent的执行轨迹，你需要根据你对任务以及要求的理解，在脑海中构建一条最佳实现路径来完美根据要求完成任务，然后在和Agent的实际执行轨迹对比，评判实际执行的轨迹质量高低，并且输出一个得分，值在0到1之间，越高说明质量越好。你只需要关注轨迹信息，不需要在意内容质量，因此，你需要根据提供的文本轨迹信息和轨迹要求进行分析，不需要了解实际的文件内容。

目标任务:
{target_task}

任务要求:
{requirements}

请仅输出 JSON，格式如下：
{{"trajectory_score": 0.0}}
"""

USER_PROMPT_TEMPLATE = """Agent执行轨迹：
{traj}
"""


def parse_all_messages(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def format_content_blocks(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return json.dumps(content, ensure_ascii=False)

    blocks: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            blocks.append(str(block))
            continue
        block_type = block.get("type")
        if block_type == "text":
            blocks.append(block.get("text", ""))
        elif block_type == "thinking":
            blocks.append(f"[thinking] {block.get('thinking', '')}")
        elif block_type == "toolCall":
            blocks.append(
                "[toolCall] "
                + json.dumps(
                    {
                        "id": block.get("id"),
                        "name": block.get("name"),
                        "arguments": block.get("arguments"),
                    },
                    ensure_ascii=False,
                )
            )
        else:
            blocks.append(json.dumps(block, ensure_ascii=False))
    return "\n".join(part for part in blocks if part)


def format_message(message: dict[str, Any]) -> str:
    parts = [f"role: {message.get('role', 'unknown')}"]
    if "toolName" in message:
        parts.append(f"tool_name: {message.get('toolName')}")
    if "toolCallId" in message:
        parts.append(f"tool_call_id: {message.get('toolCallId')}")

    content = message.get("content")
    if content is not None:
        parts.append("content:")
        parts.append(format_content_blocks(content))

    usage = message.get("usage")
    if isinstance(usage, dict):
        parts.append(f"usage: {json.dumps(usage, ensure_ascii=False)}")

    timestamp = message.get("timestamp")
    if timestamp is not None:
        parts.append(f"timestamp: {timestamp}")

    return "\n".join(parts)


def format_trajectory(all_messages_raw: Any) -> str:
    messages = parse_all_messages(all_messages_raw)
    if not messages:
        return ""
    return "\n\n".join(format_message(message) for message in messages)


def build_judge_messages(row: pd.Series) -> list[dict[str, str]]:
    requirements = row.get("trajectory_reqs")
    if pd.isna(requirements):
        requirements = ""

    return [
        {
            "role": "system",
            "content": SYSTEM_PROMPT_TEMPLATE.format(
                target_task=row.get("task_query", ""),
                requirements=requirements,
            ),
        },
        {
            "role": "user",
            "content": USER_PROMPT_TEMPLATE.format(
                traj=format_trajectory(row.get("all_messages", "")),
            ),
        },
    ]


def clamp_score(value: float) -> float:
    return max(0.0, min(1.0, value))


def extract_score_from_response(response_text: str) -> float:
    try:
        parsed = json.loads(response_text)
        value = parsed.get("trajectory_score")
        if value is not None:
            return clamp_score(float(value))
    except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
        pass

    match = re.search(r"(?<!\d)(0(?:\.\d+)?|1(?:\.0+)?)(?!\d)", response_text)
    if match:
        return clamp_score(float(match.group(1)))
    raise ValueError(f"Unable to parse trajectory score from response: {response_text!r}")


def judge_trajectory_with_mock(_: list[dict[str, str]]) -> float:
    return 1.0


def judge_trajectory_with_openai(messages: list[dict[str, str]]) -> float:
    from openai import OpenAI

    client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)
    last_error: Exception | None = None

    for _ in range(3):
        sleep(1)
        try:
            response = client.chat.completions.create(
                model=JUDGE_MODEL_NAME,
                messages=messages,
                temperature=0,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content or ""
            return extract_score_from_response(content)
        except Exception as exc:
            last_error = exc

    if last_error is not None:
        LOGGER.error(f"OpenAI trajectory judge failed with a captured exception: {str(last_error)}")
        raise last_error
    LOGGER.error("OpenAI trajectory judge failed without a captured exception.")
    raise RuntimeError("OpenAI trajectory judge failed without a captured exception.")


def compute_trajectory_score(row: pd.Series) -> float:
    judge_messages = build_judge_messages(row)
    if not USE_JUDGE:
        return judge_trajectory_with_mock(judge_messages)
    return judge_trajectory_with_openai(judge_messages)


def attach_trajectory_scores(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["trajectory_score"] = out.apply(lambda row: compute_trajectory_score(row), axis=1)
    return out
