"""LLM-based trajectory quality scoring for post-process metric extraction.

Formats agent message transcripts into judge prompts and attaches a
``trajectory_score`` column (0–1) to the extracted metrics DataFrame.
"""

import json
import re
from typing import Any
from time import sleep
import pandas as pd

from src.config import CONFIG, LOGGER

# System prompt template for the trajectory judge LLM (Chinese).
SYSTEM_PROMPT_TEMPLATE = """你是一个Agent轨迹评判高手，你会了解到目标任务、任务要求以及一个其他Agent的执行轨迹，你需要根据你对任务以及要求的理解，在脑海中构建一条最佳实现路径来完美根据要求完成任务，然后在和Agent的实际执行轨迹对比，评判实际执行的轨迹质量高低，并且输出一个得分，值在0到1之间，越高说明质量越好。你只需要关注轨迹信息，不需要在意内容质量，因此，你需要根据提供的文本轨迹信息和轨迹要求进行分析，不需要了解实际的文件内容。

目标任务:
{target_task}

任务要求:
{requirements}

请仅输出 JSON，格式如下：
{{"trajectory_score": 0.0}}
"""

# User prompt template wrapping the formatted agent trajectory text.
USER_PROMPT_TEMPLATE = """Agent执行轨迹：
{traj}
"""


def parse_all_messages(raw: Any) -> list[dict[str, Any]]:
    """Parse ``all_messages`` from a JSON string or list into a message dict list."""
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
    """Render message ``content`` blocks (text, thinking, toolCall) as plain text."""
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
    """Format a single agent message dict into a human-readable trajectory line."""
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
    """Join all formatted messages into a single trajectory string for the judge."""
    messages = parse_all_messages(all_messages_raw)
    if not messages:
        return ""
    return "\n\n".join(format_message(message) for message in messages)


def build_judge_messages(row: pd.Series) -> list[dict[str, str]]:
    """Build OpenAI chat messages (system + user) for trajectory judging from a DataFrame row."""
    requirements = row.get("trajectory_reqs")
    target_task = row.get("task_query", "")
    if pd.isna(requirements):
        requirements = ""

    return [
        {
            "role": "system",
            "content": SYSTEM_PROMPT_TEMPLATE.format(
                target_task=target_task,
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
    """Clamp *value* to the inclusive range [0.0, 1.0]."""
    return max(0.0, min(1.0, value))


def extract_score_from_response(response_text: str) -> float:
    """Parse ``trajectory_score`` from JSON or a bare numeric substring in *response_text*."""
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
    """Return a fixed score of 1.0 when trajectory judging is disabled."""
    return 1.0


def judge_trajectory_with_openai(messages: list[dict[str, str]]) -> float:
    """Call the configured OpenAI-compatible judge model and parse the trajectory score."""
    from openai import OpenAI

    client = OpenAI(
        api_key=CONFIG.trajectory_judge_openai_api_key,
        base_url=CONFIG.trajectory_judge_openai_base_url,
    )
    last_error: Exception | None = None

    for _ in range(3):
        sleep(1)
        try:
            response = client.chat.completions.create(
                model=CONFIG.trajectory_judge_model,
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
    """Score one row's trajectory via mock or live OpenAI judge depending on config."""
    judge_messages = build_judge_messages(row)
    if not CONFIG.do_trajectory_judge:
        return judge_trajectory_with_mock(judge_messages)
    return judge_trajectory_with_openai(judge_messages)


def attach_trajectory_scores(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of *df* with a ``trajectory_score`` column computed per row."""
    out = df.copy()
    out["trajectory_score"] = out.apply(lambda row: compute_trajectory_score(row), axis=1)
    return out
