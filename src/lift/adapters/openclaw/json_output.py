"""解析 ``openclaw agent --json`` 的 stdout。"""

from __future__ import annotations

import json

from src.config import LOGGER


def parse_json_loose(raw: str) -> dict:
    """宽松解析 JSON：CLI 输出可能含非 JSON 前缀，取最后一个 ``{...}`` 对象。"""
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        pass

    # CLI 可能在 JSON 前打日志行；取最外层 {...}
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def extract_agent_text(stdout: str) -> str:
    """从 ``openclaw agent --json --local`` 的 stdout 提取 assistant 文本。"""
    if not stdout.strip():
        raise ValueError("OpenClaw returned empty stdout")

    payload = parse_json_loose(stdout)
    if not payload:
        LOGGER.error("Failed to parse OpenClaw output as JSON: %s", stdout)
        raise ValueError("OpenClaw returned invalid JSON")

    payloads = payload.get("payloads")
    if not isinstance(payloads, list):
        raise ValueError("OpenClaw JSON missing payloads list")

    texts: list[str] = []
    for item in payloads:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str) and text:
            texts.append(text)

    media_urls: list[str] = []
    for item in payloads:
        if not isinstance(item, dict):
            continue
        media_url = item.get("mediaUrl")
        if isinstance(media_url, str) and media_url:
            media_urls.append(media_url)
    if media_urls:
        LOGGER.info("mediaUrls: %s", "\n".join(media_urls))

    return "\n".join(texts)
