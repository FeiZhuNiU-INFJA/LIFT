"""Prime Agent host 侧 Langfuse ``prime-agent-plugin`` trace 补写。

Prime Agent 镜像里**没有**容器内 Langfuse 插件（不像 OpenClaw 的
``langfuse-tracer``），``prime-agent --mode json`` 只把 usage 打进 stdout 的 JSON
事件流。若不补写，post-process 的 ``trace_backfill`` 找不到配对 plugin trace，5
字段 token 全 NaN。

因此和 OpenHuman(``transcript_langfuse.push_openhuman_plugin_trace``) 同构：在
host 侧 chat transport 拿到本轮 stdout 后，解析事件流构造一条
``prime-agent-plugin`` trace，每个 assistant round-trip 一个 GENERATION
observation（带 ``usage_details``），toolCall 一个 tool observation。session_id
与 pre-chat span 一致，post-process 的 greedy pairing 就能把 tokens 挂回 PhaseRun。

Prime Agent ``usage`` schema（见镜像内 ``docs/session-format.md`` 的 ``Usage``）：

    {"input": N, "output": N, "cacheRead": N, "cacheWrite": N, "totalTokens": N, "cost": {...}}

- ``input``：本轮**新增** prompt（**不含** cache，Anthropic 风格）
- ``output``：完整 completion（**含** reasoning；Prime 不单列 reasoning 字段）
- ``cacheRead`` / ``cacheWrite``：cache 命中 / 首写
- ``totalTokens == input + output + cacheRead + cacheWrite``（已实测核对）

落库口径（对齐 ``langfuse_trace_fetch._usage_breakdown``）：为了让 backfill 的
"OpenAI 家 input 含 cache 就 ``input - cache`` 拿 fresh" 启发式**恒定命中**，这里把
``usage_details.input`` 写成**含 cache 的完整 prompt**（``input+cacheRead+cacheWrite``），
再单列 ``cache_read_input_tokens`` / ``cache_creation_input_tokens``。这样无论
fresh 与 cache 的相对大小如何，``_usage_breakdown`` 都能稳定还原出 Prime 原始
``input`` 作为 ``input_tokens``。``reasoning_tokens`` Prime 不吐则恒 0（合规，同
OpenHuman）。
"""

from __future__ import annotations

import json
from typing import Any

from src.config import CONFIG, LOGGER
from src.models import LangfusePluginTraceMetadata

#: post-process 的 plugin trace 白名单（``src.models.LANGFUSE_PLUGIN_TRACE_NAMES``）
#: 必须包含此名，否则 pairing 认不出 Prime Agent 的 turn trace。
PRIME_AGENT_PLUGIN_TRACE_NAME = "prime-agent-plugin"

_MAX_TEXT_CHARS = 20000


def _iter_json_lines(text: str):
    """逐行解析 JSON Lines，跳过非 JSON 行（与 chat_agent._iter_json_lines 同规则）。"""
    for line in text.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(obj, dict):
            yield obj


def _usage_details_from_prime(usage: Any) -> dict[str, int] | None:
    """Prime ``usage`` 到 Langfuse ``usage_details``（含 cache 的 input 口径）。

    返回 None 表示这条 assistant 无有效 usage（不产 GENERATION）。
    """
    if not isinstance(usage, dict):
        return None
    inp = int(usage.get("input") or 0)
    out = int(usage.get("output") or 0)
    cache_read = int(usage.get("cacheRead") or 0)
    cache_write = int(usage.get("cacheWrite") or 0)
    if inp == 0 and out == 0 and cache_read == 0 and cache_write == 0:
        return None
    # input 写成"含 cache 的完整 prompt"，让 _usage_breakdown 的 OpenAI 家启发式恒命中。
    prompt_incl_cache = inp + cache_read + cache_write
    details: dict[str, int] = {
        "input": prompt_incl_cache,
        "output": out,
        "total": prompt_incl_cache + out,
    }
    if cache_read:
        details["cache_read_input_tokens"] = cache_read
    if cache_write:
        details["cache_creation_input_tokens"] = cache_write
    return details


def _text_from_content(content: Any) -> str:
    """从 message.content（block 数组或纯字符串）拼接可见文本。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]
    return "".join(parts)


def _tool_calls_from_content(content: Any) -> list[dict[str, Any]]:
    """从 assistant message.content 抽 ``type == "toolCall"`` 块。"""
    if not isinstance(content, list):
        return []
    calls: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "toolCall":
            calls.append(block)
    return calls


class _PrimeTurnSummary:
    """从单次 ``prime-agent --mode json`` stdout 归纳出的一轮观测数据。"""

    def __init__(self) -> None:
        self.generations: list[dict[str, Any]] = []
        self.tools: list[dict[str, Any]] = []
        self.tool_calls: list[dict[str, Any]] = []
        self.tool_names: list[str] = []
        self.messages: list[Any] = []
        self.final_text: str = ""


def _summarize_stream(stdout_text: str) -> _PrimeTurnSummary:
    """解析事件流：assistant round-trip 到 generation，toolCall 到 tool。

    只吃 ``message_end``（每次 LLM round-trip 触发一次、usage 为**本轮**增量）作为
    generation 源；``agent_end.messages`` 在 ``-r`` 续接时只含**当前轮**消息（已实测），
    故不会跨轮重复计数。tool 输出从 ``tool_execution_end`` 事件回填。
    """
    summary = _PrimeTurnSummary()
    tool_outputs: dict[str, Any] = {}

    # 先扫一遍 tool_execution_end 收集 toolCallId 到输出，供 toolCall 回填。
    for obj in _iter_json_lines(stdout_text):
        if obj.get("type") == "tool_execution_end":
            call_id = obj.get("toolCallId")
            if isinstance(call_id, str):
                tool_outputs[call_id] = obj.get("result")

    for obj in _iter_json_lines(stdout_text):
        if obj.get("type") != "message_end":
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue

        summary.messages.append(msg)

        usage_details = _usage_details_from_prime(msg.get("usage"))
        text = _text_from_content(msg.get("content"))
        if usage_details is not None:
            summary.generations.append(
                {
                    "model": msg.get("model"),
                    "output": text,
                    "usage_details": usage_details,
                }
            )
        if text:
            summary.final_text = text

        for call in _tool_calls_from_content(msg.get("content")):
            name = call.get("name") or "tool"
            call_id = call.get("id")
            args = call.get("arguments")
            summary.tool_names.append(str(name))
            summary.tool_calls.append(
                {"name": str(name), "arguments": args, "id": call_id}
            )
            summary.tools.append(
                {
                    "name": str(name),
                    "arguments": args,
                    "output": tool_outputs.get(call_id) if isinstance(call_id, str) else None,
                }
            )

    return summary


def _dedup_preserve_order(names: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _get_langfuse_client() -> Any | None:
    """按 CONFIG 构造 Langfuse SDK v4 client；未配置或导入失败返回 None。"""
    if not CONFIG.langfuse_credentials_present:
        return None
    try:
        from langfuse import Langfuse  # type: ignore[import]
    except ImportError:
        LOGGER.warning("[prime_agent langfuse] langfuse SDK not installed")
        return None
    try:
        return Langfuse(
            public_key=CONFIG.langfuse_public_key,
            secret_key=CONFIG.langfuse_secret_key,
            host=CONFIG.langfuse_base_url,
        )
    except Exception:  # noqa: BLE001
        LOGGER.exception("[prime_agent langfuse] failed to init Langfuse client")
        return None


def push_prime_agent_plugin_trace(
    *,
    session_id: str,
    user_message: str,
    stdout_text: str,
    run_tag: str,
) -> None:
    """把本轮 chat 的 usage/transcript 补成一条 ``prime-agent-plugin`` trace。

    参数：
        session_id: LIFT/Langfuse 维度 session id（与 pre-chat span 一致），用于
            post-process greedy pairing 挂回 PhaseRun。
        user_message: 本轮 chat 入参（作为 root observation.input）。
        stdout_text: ``prime-agent --mode json`` 的完整 stdout（JSON 事件流）。
        run_tag: LIFT 评测 run id（Langfuse tag，与 pre-chat span 一致）。

    失败仅 warning，绝不影响 chat 主路径。
    """
    if not CONFIG.langfuse_credentials_present:
        return

    summary = _summarize_stream(stdout_text)
    if not summary.generations:
        # 没有任何可落库的 usage（例如 agent 秒崩 / 空回复），静默跳过。
        LOGGER.debug(
            "[prime_agent langfuse] no generations parsed session_id=%s", session_id
        )
        return

    client = _get_langfuse_client()
    if client is None:
        return

    try:
        from langfuse import propagate_attributes  # type: ignore[import]
    except ImportError:
        LOGGER.warning("[prime_agent langfuse] propagate_attributes not importable")
        return

    tags: list[str] = []
    if run_tag:
        tags.append(run_tag)
    if session_id:
        tags.append(session_id)

    tool_names = _dedup_preserve_order(summary.tool_names)
    metadata = LangfusePluginTraceMetadata(
        success=True,
        message_count=len(summary.messages),
        tool_roundtrips=len(summary.tool_calls),
        tool_call_blocks=len(summary.tool_calls),
        tool_names_distinct=",".join(tool_names) or None,
        messages=summary.messages,
    ).model_dump(by_alias=True)

    final_output = summary.final_text or ""

    try:
        with propagate_attributes(session_id=session_id, tags=tags):
            root_cm = client.start_as_current_observation(
                name=PRIME_AGENT_PLUGIN_TRACE_NAME,
                as_type="agent",
                input=user_message,
                metadata=metadata,
            )
            root = root_cm.__enter__()
            try:
                for gen in summary.generations:
                    obs = client.start_observation(
                        name="llm.chat",
                        as_type="generation",
                        input=None,  # transcript 已在 root metadata.messages
                        model=gen.get("model"),
                    )
                    obs.update(
                        output=str(gen.get("output") or "")[:_MAX_TEXT_CHARS],
                        usage_details=gen.get("usage_details"),
                    )
                    obs.end()

                for tool in summary.tools:
                    args = tool.get("arguments")
                    obs = client.start_observation(
                        name=str(tool.get("name") or "tool"),
                        as_type="tool",
                        input=args,
                    )
                    output_val = tool.get("output")
                    if isinstance(output_val, str) and len(output_val) > _MAX_TEXT_CHARS:
                        output_val = output_val[:_MAX_TEXT_CHARS]
                    obs.update(output=output_val)
                    obs.end()

                # 统一观测契约：root output 带 tool_calls 列表，供后处理校准 toolCallBlocks。
                if summary.tool_calls:
                    root.update(
                        output={"content": final_output, "tool_calls": summary.tool_calls}
                    )
                else:
                    root.update(output=final_output)
            finally:
                root_cm.__exit__(None, None, None)
        client.flush()
    except Exception:  # noqa: BLE001
        LOGGER.exception(
            "[prime_agent langfuse] push failed session_id=%s", session_id
        )


def push_prime_agent_plugin_trace_safe(
    *,
    session_id: str,
    user_message: str,
    stdout_text: str,
    run_tag: str,
) -> None:
    """薄封装：调 ``push_prime_agent_plugin_trace``，任何异常仅 warning。"""
    try:
        push_prime_agent_plugin_trace(
            session_id=session_id,
            user_message=user_message,
            stdout_text=stdout_text,
            run_tag=run_tag,
        )
    except Exception:  # noqa: BLE001
        LOGGER.exception("[prime_agent langfuse] unexpected error, swallowed")


__all__ = [
    "PRIME_AGENT_PLUGIN_TRACE_NAME",
    "push_prime_agent_plugin_trace",
    "push_prime_agent_plugin_trace_safe",
]
