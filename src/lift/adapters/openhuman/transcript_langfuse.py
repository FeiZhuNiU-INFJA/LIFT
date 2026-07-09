"""OpenHuman transcript → Langfuse ``openhuman-plugin`` trace 推送。

OpenHuman core 本身**不集成** Langfuse SDK / OTel exporter（binary 里仅有
``"langfuse"`` 字面量，无 push 通路）。为让 LIFT 后处理侧的 trace 拼装
（``langfuse_trace_stitch._stitch_openclaw`` + ``LANGFUSE_PLUGIN_TRACE_NAMES``
白名单里的 ``openhuman-plugin``）能正常工作，我们在**宿主端** chat 完成后：

1. ``docker exec`` 读容器内 ``~/.openhuman/users/local/workspace/session_raw/*.jsonl``，
   按 ``_meta.thread_id == session_id`` 过滤出本轮的 orchestrator + subagent transcript；
2. 用 Langfuse Python SDK v4 push 一条 name = ``openhuman-plugin`` 的
   ``as_type='agent'`` root observation，附带：
     - ``propagate_attributes(session_id=..., tags=[run_tag, session_id])`` — 与
       LIFT pre-chat span 走同一 ``session_id`` / ``tags``，后处理据此配对
     - ``metadata`` 按 ``LangfusePluginTraceMetadata`` 形状写入（success /
       message_count / tool_roundtrips / tool_call_blocks / tool_names_distinct /
       messages）
     - 每个 assistant iteration 挂一个 ``as_type='generation'`` 子 observation，
       ``usage_details`` 从 ``openhuman_turn_usage.usage`` 取（input / output /
       cached_input）
     - 每个 ``tool_calls`` 条目 + 后续 ``role=tool`` 结果挂一个 ``as_type='tool'``
       子 observation

这份模块是**尽力而为**的：Langfuse 未配置或 push 失败都只 warning，不影响 chat 主路径。
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.config import CONFIG, LOGGER

# 容器内 transcript 目录（默认 user = local）。openhuman-core 会在这里按
# ``<epoch>_<agent>[__<epoch>_<agent>_<taskid>].jsonl`` 组织每个 agent 的对话
# 记录，_meta 首行含 ``thread_id``，我们据此过滤当前会话。
_CONTAINER_SESSION_RAW_DIR = "/root/.openhuman/users/local/workspace/session_raw"

# 单个 transcript 文件大小硬上限，防止极端超长会话把内存打爆。10MB 内存串
# 化足够表达一个 phase 的所有 iteration。
_MAX_TRANSCRIPT_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class ParsedTranscript:
    """从容器内单个 ``session_raw/*.jsonl`` 拉出来的最小结构。"""

    path: str
    meta: dict[str, Any]  # 首行 ``_meta``
    messages: list[dict[str, Any]]  # 其余每行一个 role message


def _docker_exec_capture(container_name: str, argv: list[str]) -> str:
    """``docker exec`` 执行命令并 UTF-8 decode stdout（stderr 走日志）。"""
    cmd = ["docker", "exec", container_name, *argv]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, timeout=30, check=False
        )
    except subprocess.TimeoutExpired:
        LOGGER.warning("[openhuman langfuse] docker exec timeout: %s", " ".join(cmd))
        return ""
    if proc.returncode != 0:
        # 目录不存在等情况 ls 会 exit=2；只 debug 一下，不 warning。
        LOGGER.debug(
            "[openhuman langfuse] docker exec rc=%d cmd=%s stderr=%s",
            proc.returncode, " ".join(cmd),
            proc.stderr.decode("utf-8", errors="replace")[:400],
        )
        return ""
    return proc.stdout.decode("utf-8", errors="replace")


def _list_container_transcripts(container_name: str) -> list[str]:
    """列出容器内当前所有 session_raw 文件绝对路径。"""
    out = _docker_exec_capture(
        container_name,
        ["sh", "-c", f"ls -1 {_CONTAINER_SESSION_RAW_DIR}/*.jsonl 2>/dev/null || true"],
    )
    return [line.strip() for line in out.splitlines() if line.strip()]


def _read_container_file(container_name: str, path: str) -> str:
    """``docker exec cat`` 读一个文件内容。"""
    out = _docker_exec_capture(container_name, ["cat", path])
    if len(out) > _MAX_TRANSCRIPT_BYTES:
        LOGGER.warning(
            "[openhuman langfuse] transcript %s exceeds %d bytes; truncating",
            path, _MAX_TRANSCRIPT_BYTES,
        )
        out = out[:_MAX_TRANSCRIPT_BYTES]
    return out


def _parse_transcript_text(path: str, raw: str) -> ParsedTranscript | None:
    """解析单个 jsonl 文本：首行 ``_meta``，其余每行一个 role 消息。"""
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return None
    try:
        meta_obj = json.loads(lines[0])
    except json.JSONDecodeError:
        LOGGER.debug("[openhuman langfuse] non-JSON first line in %s", path)
        return None
    meta = meta_obj.get("_meta") if isinstance(meta_obj, dict) else None
    if not isinstance(meta, dict):
        return None
    messages: list[dict[str, Any]] = []
    for ln in lines[1:]:
        try:
            row = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("role"):
            messages.append(row)
    return ParsedTranscript(path=path, meta=meta, messages=messages)


def collect_session_transcripts(
    container_name: str, session_id: str
) -> list[ParsedTranscript]:
    """拉取容器内所有 ``_meta.thread_id == session_id`` 的 transcript。

    返回按容器内路径字典序排序的列表；orchestrator（无 ``__<sub>``）通常排在其
    subagent 之前，符合 chat 时序。
    """
    paths = _list_container_transcripts(container_name)
    hits: list[ParsedTranscript] = []
    for path in sorted(paths):
        raw = _read_container_file(container_name, path)
        parsed = _parse_transcript_text(path, raw)
        if parsed is None:
            continue
        if parsed.meta.get("thread_id") != session_id:
            continue
        hits.append(parsed)
    return hits


def _extract_tool_calls_from_assistant(
    msg: dict[str, Any],
) -> list[dict[str, Any]]:
    """从 assistant message 中提取 ``tool_calls``（openhuman 特有 schema）。"""
    extra = msg.get("extra_metadata") or {}
    if not isinstance(extra, dict):
        return []
    usage_block = extra.get("openhuman_turn_usage") or {}
    if not isinstance(usage_block, dict):
        return []
    calls = usage_block.get("tool_calls") or []
    if not isinstance(calls, list):
        return []
    return [c for c in calls if isinstance(c, dict)]


def _usage_details_from_assistant(msg: dict[str, Any]) -> dict[str, int] | None:
    """``role=assistant`` 的 ``usage`` → Langfuse ``usage_details``。

    OpenHuman schema：``usage = {input, output, cached_input, cost_usd, ...}``；
    映射到 Langfuse 标准键 ``input`` / ``output`` / ``total`` / ``cache_read_input_tokens``
    （参考 ``langfuse_trace_fetch._usage_triplet`` 的兼容读取逻辑）。
    """
    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return None
    inp = int(usage.get("input") or 0)
    out = int(usage.get("output") or 0)
    cached = int(usage.get("cached_input") or 0)
    if inp == 0 and out == 0 and cached == 0:
        return None
    details: dict[str, int] = {
        "input": inp,
        "output": out,
        "total": inp + out,
    }
    if cached:
        details["cache_read_input_tokens"] = cached
    return details


@dataclass
class _PluginTraceSummary:
    """从 transcripts 聚合出来的 plugin trace 骨架。"""

    messages: list[dict[str, Any]]
    tool_roundtrips: int
    tool_call_blocks: int
    tool_names_distinct: list[str]
    generations: list[dict[str, Any]]  # 每个 assistant iteration
    tools: list[dict[str, Any]]  # 每次 tool 调用（含 input args + tool result）
    final_response: str


def _dedup_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            ordered.append(it)
    return ordered


def summarize_transcripts(transcripts: list[ParsedTranscript]) -> _PluginTraceSummary:
    """把 orchestrator + subagent transcripts 聚合成 plugin trace payload。

    metadata 目标 schema（``LangfusePluginTraceMetadata``）：

    - ``messages``：所有 transcript 里 role != system 的消息扁平化（保留原顺序）
    - ``tool_roundtrips``：出现的 ``role=tool`` 消息数
    - ``tool_call_blocks``：所有 assistant 消息中 tool_calls 条目总数
    - ``tool_names_distinct``：去重后的工具名列表
    """
    messages: list[dict[str, Any]] = []
    tool_names: list[str] = []
    tool_call_blocks = 0
    tool_roundtrips = 0
    generations: list[dict[str, Any]] = []
    tools: list[dict[str, Any]] = []
    final_response = ""

    # 按 transcript 顺序拼接。系统消息（role=system，openhuman 里通常是 tool
    # policy + orchestrator system prompt）不放进 messages，避免冗余 + 尊重
    # LangfusePluginTraceMetadata 只关心 conversation 语义。
    pending_tool_call_id_to_name: dict[str, str] = {}
    for t in transcripts:
        agent_name = str(t.meta.get("agent") or "unknown")
        for msg in t.messages:
            role = str(msg.get("role") or "")
            if role == "system":
                continue
            enriched = dict(msg)
            enriched["_agent"] = agent_name  # 标记来自哪个 sub-agent，便于 UI 阅读
            messages.append(enriched)

            if role == "assistant":
                calls = _extract_tool_calls_from_assistant(msg)
                if calls:
                    tool_call_blocks += len(calls)
                    for c in calls:
                        name = str(c.get("name") or "")
                        if name:
                            tool_names.append(name)
                            call_id = c.get("id")
                            if isinstance(call_id, str):
                                pending_tool_call_id_to_name[call_id] = name
                            tools.append(
                                {
                                    "name": name,
                                    "call_id": c.get("id"),
                                    "arguments": c.get("arguments"),
                                    "agent": agent_name,
                                }
                            )
                usage_details = _usage_details_from_assistant(msg)
                if usage_details is not None:
                    generations.append(
                        {
                            "agent": agent_name,
                            "iteration": (
                                msg.get("iteration")
                                or (msg.get("extra_metadata") or {})
                                .get("openhuman_turn_usage", {})
                                .get("iteration")
                            ),
                            "content": msg.get("content") or "",
                            "usage_details": usage_details,
                            "model": msg.get("model") or t.meta.get("model"),
                        }
                    )
                # 最后一条 orchestrator 层的、非 tool_call 结果 assistant 就是最终回复
                if agent_name == "orchestrator" and not _extract_tool_calls_from_assistant(msg):
                    txt = msg.get("content")
                    if isinstance(txt, str) and txt.strip():
                        final_response = txt

            elif role == "tool":
                tool_roundtrips += 1
                # 从 tool result 中找回对应 call_id → name
                content = msg.get("content")
                call_id: str | None = None
                if isinstance(content, str):
                    try:
                        payload = json.loads(content)
                        if isinstance(payload, dict):
                            cid = payload.get("tool_call_id")
                            if isinstance(cid, str):
                                call_id = cid
                    except json.JSONDecodeError:
                        pass
                name = pending_tool_call_id_to_name.get(call_id or "") if call_id else None
                if tools and name is None:
                    name = tools[-1].get("name")
                # 回填 tool result 到最近一次同名 tool entry
                if tools and name:
                    for entry in reversed(tools):
                        if entry.get("name") == name and "output" not in entry:
                            entry["output"] = content
                            break

    return _PluginTraceSummary(
        messages=messages,
        tool_roundtrips=tool_roundtrips,
        tool_call_blocks=tool_call_blocks,
        tool_names_distinct=_dedup_preserve_order(tool_names),
        generations=generations,
        tools=tools,
        final_response=final_response,
    )


def _get_langfuse_client() -> Any | None:
    """按 CONFIG 构造 Langfuse SDK v4 client；未配置或导入失败返回 None。"""
    if not CONFIG.langfuse_credentials_present:
        return None
    try:
        from langfuse import Langfuse  # type: ignore[import]
    except ImportError:
        LOGGER.warning("[openhuman langfuse] langfuse SDK not installed")
        return None
    try:
        return Langfuse(
            public_key=CONFIG.langfuse_public_key,
            secret_key=CONFIG.langfuse_secret_key,
            host=CONFIG.langfuse_base_url,
        )
    except Exception:  # noqa: BLE001
        LOGGER.exception("[openhuman langfuse] failed to init Langfuse client")
        return None


def push_openhuman_plugin_trace(
    *,
    container_name: str,
    session_id: str,
    user_message: str,
    reply_text: str,
    run_tag: str,
) -> None:
    """将本轮 chat 的 transcript 上传到 Langfuse ``openhuman-plugin``。

    参数：
        container_name: docker exec 目标容器（openhuman-core 所在）
        session_id: LIFT 侧 work/judge session_id（== openhuman thread_id）
        user_message: 本轮 chat 入参消息（作为 root observation.input）
        reply_text: 本轮 chat 回复（作为 root observation.output 兜底；若能从
            transcript 里拿到 orchestrator 最终回复则以后者为准）
        run_tag: LIFT 评测 run id（作为 Langfuse tag，与 pre-chat span 一致）

    失败仅 warning，不影响 chat 主路径。
    """
    if not CONFIG.langfuse_credentials_present:
        return

    transcripts = collect_session_transcripts(container_name, session_id)
    if not transcripts:
        LOGGER.debug(
            "[openhuman langfuse] no transcripts for session_id=%s in %s",
            session_id, container_name,
        )
        return

    summary = summarize_transcripts(transcripts)
    final_output = summary.final_response or reply_text

    client = _get_langfuse_client()
    if client is None:
        return

    try:
        from langfuse import propagate_attributes  # type: ignore[import]
    except ImportError:
        LOGGER.warning("[openhuman langfuse] propagate_attributes not importable")
        return

    tags: list[str] = []
    if run_tag:
        tags.append(run_tag)
    if session_id:
        tags.append(session_id)

    # LangfusePluginTraceMetadata 形状：字段名跟 pydantic model 一致，后处理
    # (LangfusePluginTraceMetadata.from_langfuse_dict) 会兼容 camel/snake case。
    metadata = {
        "success": True,
        "message_count": len(summary.messages),
        "tool_roundtrips": summary.tool_roundtrips,
        "tool_call_blocks": summary.tool_call_blocks,
        "tool_names_distinct": ",".join(summary.tool_names_distinct) or None,
        "messages": summary.messages,
    }

    try:
        with propagate_attributes(session_id=session_id, tags=tags):
            root_cm = client.start_as_current_observation(
                name="openhuman-plugin",
                as_type="agent",
                input=user_message,
                metadata=metadata,
            )
            root = root_cm.__enter__()
            try:
                # 每个 assistant iteration → generation observation
                for gen in summary.generations:
                    obs = client.start_observation(
                        name="llm.chat",
                        as_type="generation",
                        input=None,  # transcript messages 已在 root metadata 里
                        model=gen.get("model"),
                    )
                    obs.update(
                        output=str(gen.get("content") or "")[:20000],
                        usage_details=gen.get("usage_details"),
                    )
                    obs.end()

                # 每次 tool 调用 → tool observation
                for tool in summary.tools:
                    args = tool.get("arguments")
                    # arguments 是 JSON 字符串，尽力解析出更可读的形态
                    parsed_args: Any = args
                    if isinstance(args, str):
                        try:
                            parsed_args = json.loads(args)
                        except json.JSONDecodeError:
                            parsed_args = args
                    obs = client.start_observation(
                        name=str(tool.get("name") or "tool"),
                        as_type="tool",
                        input=parsed_args,
                    )
                    output_val = tool.get("output")
                    if isinstance(output_val, str) and len(output_val) > 20000:
                        output_val = output_val[:20000]
                    obs.update(output=output_val)
                    obs.end()

                root.update(output=final_output)
            finally:
                root_cm.__exit__(None, None, None)
        client.flush()
    except Exception:  # noqa: BLE001
        LOGGER.exception(
            "[openhuman langfuse] push failed session_id=%s container=%s",
            session_id, container_name,
        )


def push_openhuman_plugin_trace_safe(
    *,
    container_name: str,
    session_id: str,
    user_message: str,
    reply_text: str,
    run_tag: str,
) -> None:
    """薄封装：调 ``push_openhuman_plugin_trace``，任何异常仅 warning。"""
    try:
        push_openhuman_plugin_trace(
            container_name=container_name,
            session_id=session_id,
            user_message=user_message,
            reply_text=reply_text,
            run_tag=run_tag,
        )
    except Exception:  # noqa: BLE001
        LOGGER.exception("[openhuman langfuse] unexpected error, swallowed")
