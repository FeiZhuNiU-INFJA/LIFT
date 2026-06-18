"""解析 ``openclaw agent --json`` 的 stdout。"""

from __future__ import annotations

import json

from src.config import LOGGER

# 当 LLM 输出被 max_output_tokens 截断时，``extract_agent_text`` 返回一段以
# 该 marker 开头的字符串；``_looks_like_provider_error`` 会命中，让上层 worker
# / judge chat 走 provider error 重试通道（5 次重发同 prompt）。
MAX_TOKENS_TRUNCATION_MARKER = "max_output_tokens truncation"


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


def _detect_max_tokens_truncation(payload: dict) -> tuple[bool, int, int]:
    """返回 ``(is_truncated, output_tokens, max_tokens)``。

    判定依据：``meta.lastCallUsage.output`` 触达 provider 配置的 ``maxTokens``
    上限即视为截断。``maxTokens`` 通过 ``meta.modelMeta.maxTokens`` 或
    ``meta.agentMeta.modelInfo.maxTokens`` 拿到；缺失则只能保守判 False。
    """
    meta = payload.get("meta") if isinstance(payload, dict) else None
    if not isinstance(meta, dict):
        return (False, 0, 0)
    last = meta.get("lastCallUsage")
    output_tokens = 0
    if isinstance(last, dict) and isinstance(last.get("output"), int):
        output_tokens = last["output"]
    # OpenClaw 不同版本 maxTokens 落点不同：模型块下 / agentMeta 下都见过
    max_tokens = 0
    for path in (
        ("modelMeta", "maxTokens"),
        ("agentMeta", "modelInfo", "maxTokens"),
        ("modelInfo", "maxTokens"),
    ):
        cur: object = meta
        for key in path:
            if not isinstance(cur, dict):
                cur = None
                break
            cur = cur.get(key)
        if isinstance(cur, int) and cur > 0:
            max_tokens = cur
            break
    if not max_tokens or not output_tokens:
        return (False, output_tokens, max_tokens)
    # 触达上限即视为截断（>= 保守一点，覆盖整数 floor 情况）
    return (output_tokens >= max_tokens, output_tokens, max_tokens)


def extract_agent_text(stdout: str) -> str:
    """从 ``openclaw agent --json --local`` 的 stdout 提取 assistant 文本。

    若检测到 ``meta.lastCallUsage.output`` 触达模型 ``maxTokens``，返回带
    ``max_output_tokens truncation`` marker 的字符串，让上层 provider 重试。
    """
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

    result = "\n".join(texts)

    truncated, output_tokens, max_tokens = _detect_max_tokens_truncation(payload)
    if truncated:
        LOGGER.warning(
            "[extract_agent_text] %s output=%d max=%d visible=%r",
            MAX_TOKENS_TRUNCATION_MARKER,
            output_tokens,
            max_tokens,
            result[:120],
        )
        # 把 marker 放在首行，``_looks_like_provider_error`` 会取首行作为摘要
        return (
            f"{MAX_TOKENS_TRUNCATION_MARKER} (output={output_tokens}, "
            f"max={max_tokens})\n{result}"
        )

    if len(result) < 10:
        # 完整原始 stdout，留作短输出根因诊断
        LOGGER.info(
            "[extract_agent_text] result: %s, input stdout (%d chars):\n%s",
            result, len(stdout), stdout,
        )

    return result
