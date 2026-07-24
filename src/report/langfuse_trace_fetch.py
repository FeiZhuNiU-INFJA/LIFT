"""Fetch full Langfuse trace details (``trace.get``) and map to ``LangfuseTraceRef``."""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Lock
from typing import Any, Callable

import httpx

from src.config import LOGGER
from src.report.langfuse_trace_parse import structure_trace_payload, is_plugin_trace
from src.models import (
    LangfuseObservationBrief,
    LangfuseTraceDetailRecord,
    LangfuseTraceRef,
    LangfuseTraceTokens,
)


# 单个 phase 内 ``trace.get`` 的线程池上限。Langfuse SDK 同步客户端底层是
# httpx.Client，连接池天然线程安全，可直接喂 ThreadPoolExecutor。注意外层
# ``backfill_report`` 已并发 N phase（``EVAL_BACKFILL_WORKERS``），总并发
# = N × _TRACE_GET_WORKERS，所以这里默认压到 4 防止打挂自托管 Langfuse。
_TRACE_GET_WORKERS_ENV = "EVAL_BACKFILL_TRACE_GET_WORKERS"
_TRACE_GET_WORKERS_DEFAULT = 4


def _resolve_trace_get_workers() -> int:
    raw = os.environ.get(_TRACE_GET_WORKERS_ENV)
    if raw is None:
        return _TRACE_GET_WORKERS_DEFAULT
    try:
        return max(1, int(raw))
    except ValueError:
        return _TRACE_GET_WORKERS_DEFAULT


# ``trace.get`` 走 httpx 同步池，跑大规模 backfill 时偶发瞬态网络错误
# （连接池 socket 提前关的 [Errno 9] Bad file descriptor / RemoteProtocolError /
# ReadTimeout），单条挂会把整个 phase 顶到 ``backfill_phase`` 的 except 分支变成
# "keeping phase unchanged"。``trace.get`` 是幂等 GET，本地做一层小重试即可
# 覆盖 99% 的瞬态抖动；总次数 = 1 + _TRACE_GET_RETRIES。
_TRACE_GET_RETRIES = 3
_TRACE_GET_RETRY_BACKOFF_SECONDS = 0.5
_TRACE_GET_RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    httpx.ReadError,
    httpx.ConnectError,
    httpx.ReadTimeout,
    httpx.ConnectTimeout,
    httpx.RemoteProtocolError,
    httpx.PoolTimeout,
)


def _trace_get_with_retry(client: Any, tid: str) -> Any:
    """Call ``client.api.trace.get(tid)`` with bounded retries on transient network errors.

    非重试类异常（例如 4xx 语义错误）直接抛出；只针对 httpx 网络瞬态错误。
    """
    last_exc: BaseException | None = None
    for attempt in range(_TRACE_GET_RETRIES + 1):
        try:
            return client.api.trace.get(tid)
        except _TRACE_GET_RETRYABLE_EXCEPTIONS as exc:
            last_exc = exc
            if attempt == _TRACE_GET_RETRIES:
                break
            delay = _TRACE_GET_RETRY_BACKOFF_SECONDS * (2 ** attempt)
            LOGGER.warning(
                "trace.get(%s) transient %s: %s — retrying in %.1fs (attempt %d/%d)",
                tid, type(exc).__name__, exc, delay,
                attempt + 1, _TRACE_GET_RETRIES,
            )
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


def _latency_seconds(raw: Any) -> float | None:
    """Coerce Langfuse latency field to float seconds, or None."""
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _iso(ts: Any) -> str | None:
    """Format a timestamp object or value as an ISO string."""
    if ts is None:
        return None
    if hasattr(ts, "isoformat"):
        return ts.isoformat()
    return str(ts)


def _first_int(d: dict[str, Any], *keys: str) -> int:
    """按顺序取第一个非空整数字段；用于跨 provider 的 usage 字段兜底。"""
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        try:
            iv = int(v)
        except (TypeError, ValueError):
            continue
        if iv:
            return iv
    return 0


def _usage_breakdown(usage: Any) -> tuple[int, int, int, int, int]:
    """从 Langfuse usage 归一到 ``(input_fresh, cache_write, cache_read, output, reasoning)``。

    覆盖当前生产链路里出现的三种 provider 命名口径：
    - **Langfuse 标准 / OpenAI / Ark**：``input`` / ``output``（``prompt_tokens`` 系列自动
      归一到这两个键），``prompt_tokens_details.cached_tokens`` → ``cache_read_tokens``。
      OpenAI 家的 ``input`` **含** cached，故这里做 ``input - cached`` 拿"新增"；
      ``cache_write`` 恒 0（不区分）。
    - **Anthropic**：``cache_creation_input_tokens`` → ``cache_write``；``cache_read_input_tokens``
      → ``cache_read``；``input_tokens``（Anthropic 定义为**不含** cache）→ ``input``。
    - **OpenHuman transcript_langfuse 归一后**：``input`` / ``output`` / ``cache_read_input_tokens``；
      语义同 OpenAI 家（``input`` 含 cached），透过 ``input - cache_read`` 拿新增。
    - ``usage_details`` 嵌套结构：Langfuse observation.usage_details 里同样支持上述字段。

    返回值恒满足 ``fresh + write + read`` = 原始 prompt 总量。
    """
    if usage is None:
        return 0, 0, 0, 0, 0
    if hasattr(usage, "model_dump"):
        d = usage.model_dump()
    elif isinstance(usage, dict):
        d = usage
    else:
        return 0, 0, 0, 0, 0

    # observation.usage 顶层与 usage_details 二者都可能出现；合并读，details 优先。
    # OpenAI 家把 cached / reasoning 嵌在 prompt_tokens_details / completion_tokens_details
    # 里，因此把这两个子 dict 也一并 flatten 进合并视图，让 _first_int 直接命中。
    # Langfuse SDK 同时暴露 snake_case (`usage_details`) 和 camelCase (`usageDetails`)。
    details = d.get("usage_details") if isinstance(d.get("usage_details"), dict) else {}
    details_camel = d.get("usageDetails") if isinstance(d.get("usageDetails"), dict) else {}
    prompt_details = (
        d.get("prompt_tokens_details") if isinstance(d.get("prompt_tokens_details"), dict) else {}
    )
    completion_details = (
        d.get("completion_tokens_details")
        if isinstance(d.get("completion_tokens_details"), dict)
        else {}
    )
    merged = {**d, **prompt_details, **completion_details, **details, **details_camel}

    # output
    out = _first_int(merged, "output", "output_tokens", "completion_tokens")

    # cache read / write（多种命名）
    cache_read = _first_int(
        merged,
        "cache_read_input_tokens",  # Anthropic / Langfuse 标准
        "cachedInputTokens",
        "cached_input",             # OpenHuman transcript raw
        "cache_read",
        "cacheRead",                # OpenClaw plugin
        "cached_tokens",            # OpenAI prompt_tokens_details.cached_tokens
        "prompt_cache_hit_tokens",  # DeepSeek
        "cachedContentTokenCount",  # Gemini
    )
    cache_write = _first_int(
        merged,
        "cache_creation_input_tokens",  # Anthropic
        "cacheWrite",                   # OpenClaw plugin
        "cache_write",
    )

    # Prompt 总量（不同 provider 语义不一）：
    # - Anthropic ``input_tokens`` 定义为**不含** cache（"fresh"），加上 cache_write/read 才是完整 prompt。
    # - OpenAI/Ark ``input`` / ``prompt_tokens`` 定义为**含** cache 的完整 prompt。
    # 归一策略：先取一个"标注为 input 的值"，再和 cache_read+cache_write 比较：
    # 如果 >= 二者之和，说明它是"含 cache 的完整 prompt"，扣掉 cache 部分得到 fresh；否则本身就是 fresh。
    raw_input = _first_int(merged, "input", "input_tokens", "prompt_tokens", "promptTokenCount")
    cache_total = cache_read + cache_write
    if raw_input >= cache_total and cache_total > 0:
        fresh = raw_input - cache_total
    else:
        fresh = raw_input

    # reasoning：OpenAI 家 completion_tokens_details.reasoning_tokens 已在上面被 flatten 进
    # merged；OpenClaw plugin 用 reasoningTokens 键在顶层。
    reasoning = _first_int(merged, "reasoning_tokens", "reasoningTokens")

    return fresh, cache_write, cache_read, out, reasoning


def observation_briefs(observations: list[Any]) -> list[LangfuseObservationBrief]:
    """Map raw Langfuse observation objects to ``LangfuseObservationBrief`` models."""
    briefs: list[LangfuseObservationBrief] = []
    for o in observations or []:
        d = o.model_dump() if hasattr(o, "model_dump") else {}
        # observation.usage 与 observation.usageDetails 是同级字段。usageDetails 承载
        # 精细化 token 分项（cache_read_input_tokens / reasoning_tokens 等），必须一并
        # 传入 _usage_breakdown，否则 OpenClaw / Hermes 通过 Langfuse ingestion 写入的
        # 细分字段会被忽略。
        usage_payload: dict[str, Any] = {}
        raw_usage = d.get("usage")
        if isinstance(raw_usage, dict):
            usage_payload.update(raw_usage)
        for details_key in ("usage_details", "usageDetails"):
            details_val = d.get(details_key)
            if isinstance(details_val, dict):
                usage_payload[details_key] = details_val
        fresh, cw, cr, out_t, reasoning = _usage_breakdown(usage_payload)
        briefs.append(
            LangfuseObservationBrief(
                id=str(d.get("id") or ""),
                type=str(d.get("type") or "").upper(),
                name=str(d["name"]) if d.get("name") is not None else None,
                input_tokens=fresh,
                cache_write_tokens=cw,
                cache_read_tokens=cr,
                output_tokens=out_t,
                reasoning_tokens=reasoning,
            )
        )
    return briefs


def _hermes_tool_call_count_from_output(raw_output: Any) -> int | None:
    """Hermes ``Hermes turn`` chain 的 output 形如 ``{content, reasoning, tool_calls: [...]}``。

    ``tool_calls`` 由插件在 ``_finish_trace`` 时通过 ``_merge_trace_output`` 注入，
    覆盖整轮累计的工具调用（包括被上下文压缩遮蔽的早期调用），是 Hermes 工具调用数
    的权威来源。返回 ``None`` 表示无法解析（保留旧 fallback 行为）。
    """
    data: Any = raw_output
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    tool_calls = data.get("tool_calls")
    if not isinstance(tool_calls, list):
        return None
    return len(tool_calls)


def trace_detail_from_api(full: Any) -> LangfuseTraceDetailRecord:
    """Convert a Langfuse ``trace.get`` response into a ``LangfuseTraceDetailRecord``."""
    d = full.model_dump()
    obs_raw = getattr(full, "observations", None) or []
    name = d.get("name")
    structured = structure_trace_payload(
        name,
        d.get("input"),
        d.get("output"),
        d.get("metadata") if isinstance(d.get("metadata"), dict) else {},
    )
    # Hermes 现行链路：
    # - trace 顶层 metadata.messages 包含全量 transcript（由插件
    #   `_publish_messages_to_root` 写入）。当前插件已经不再在 GENERATION 子节点
    #   metadata 中保存 messages，所以这里没有回退路径——顶层缺失就是真的缺失。
    # - tool_call_blocks 权威来源：trace 顶层 output.tool_calls 的长度。插件在
    #   `_finish_trace` 中把整轮累计 tool_calls 写入 root output，不受上下文压缩影响；
    #   下游 `_make_row_hermes` 通过 `global_stats.tool_call_blocks` 读取。
    if name == "Hermes turn" and structured.plugin_metadata is not None:
        tc_count = _hermes_tool_call_count_from_output(d.get("output"))
        if tc_count is not None:
            structured = structured.model_copy(
                update={
                    "plugin_metadata": structured.plugin_metadata.model_copy(
                        update={"tool_call_blocks": tc_count}
                    )
                }
            )
    return LangfuseTraceDetailRecord(
        id=str(d["id"]),
        name=name,
        timestamp=_iso(d.get("timestamp")),
        session_id=d.get("session_id"),
        tags=[str(x) for x in (d.get("tags") or [])],
        agent_input=structured.agent_input,
        plugin_prompt=structured.plugin_prompt,
        plugin_response=structured.plugin_response,
        plugin_metadata=structured.plugin_metadata,
        latency_seconds=_latency_seconds(d.get("latency")),
        observations=observation_briefs(obs_raw),
    )


def tokens_from_detail(detail: LangfuseTraceDetailRecord) -> LangfuseTraceTokens:
    """Sum GENERATION observation token counts from a trace detail record."""
    fresh = cw = cr = out = reasoning = 0
    for ob in detail.observations:
        if ob.type == "GENERATION":
            fresh += ob.input_tokens
            cw += ob.cache_write_tokens
            cr += ob.cache_read_tokens
            out += ob.output_tokens
            reasoning += ob.reasoning_tokens
    return LangfuseTraceTokens(
        input_tokens=fresh,
        cache_write_tokens=cw,
        cache_read_tokens=cr,
        output_tokens=out,
        reasoning_tokens=reasoning,
    )


def count_tool_observations(detail: LangfuseTraceDetailRecord) -> int:
    """Count ``type=TOOL`` observations in a trace detail record.

    Runtime-agnostic 工具调用兜底数：只要 runtime 的 langfuse overlay 给每次工具调用挂
    了 ``as_type='tool'`` observation（GA / OpenClaw / 任何走 LIFT trace 拼装的 plugin
    trace 都满足），本函数就能给出正确计数；OpenClaw 仍以 ``plugin_metadata.toolRoundtrips``
    为权威值，本字段作为缺失 metadata 时的兜底（dashboard 显示）。
    """
    return sum(1 for ob in detail.observations if ob.type == "TOOL")


class TranscriptChampion:
    """按 timestamp 取“最晚一条” work transcript 的线程安全单槽归约器。

    背景：Hermes 每条 ``Hermes turn`` trace 的 ``metadata.messages`` 都是当轮全量
    transcript，若把每条 trace 的 messages 都留到内存/落盘，会产生 N 份重复
    （实测单个 927MB backfilled JSON 里 work 侧 messages 就占 ~340MB）。

    本类在 ``fetch_trace_details`` 的 worker 内**流式归约**：每条 trace 解析后把
    messages ``offer`` 进来，只保留 timestamp 最大（最晚）的一份，其余当场丢弃。
    因此任意时刻内存里最多 ``worker 数 + 1`` 份 transcript，而不是全部。

    - 选择口径：**最晚**（timestamp 最大）。用户要的是“最终 messages”，即便它是
      上下文压缩后的结果，也必须是链路末端那一份，而非早期更长的那份。
    - ``predicate``：判定一条 trace 是否属于 work 侧候选（judge 侧 transcript
      下游不消费，直接不 offer）。
    """

    def __init__(self, predicate: Callable[[LangfuseTraceDetailRecord], bool]):
        self._predicate = predicate
        self._lock = Lock()
        self._best_ts: str | None = None
        self._messages: list[Any] = []

    def offer(self, detail: LangfuseTraceDetailRecord, messages: list[Any]) -> None:
        """若 *detail* 是 work 候选且 timestamp 更晚，则用 *messages* 替换当前冠军。"""
        if not messages or not self._predicate(detail):
            return
        ts = detail.timestamp or ""
        with self._lock:
            # 严格 >：并列时保留先到者即可，冠军口径是“最晚”，同 timestamp 无差别。
            if self._best_ts is None or ts > self._best_ts:
                self._best_ts = ts
                self._messages = messages

    @property
    def messages(self) -> list[Any]:
        """当前冠军 transcript（最晚一条 work trace 的 messages），无候选时为空列表。"""
        return self._messages


def _detach_plugin_messages(detail: LangfuseTraceDetailRecord) -> list[Any]:
    """从 *detail* 的 ``plugin_metadata`` 上摘除 ``messages`` 并原地清空，返回被摘下的列表。

    只丢 ``messages`` 列表本身；``message_count`` / ``messages_serialized_chars`` 等轻量
    观测字段保留，下游 dashboard / extract 口径不变。``detail`` 被就地改为 messages 为空，
    因此后续 ``trace_ref_from_detail`` 产出的 ref 天然不带 transcript。
    """
    meta = detail.plugin_metadata
    if meta is None or not meta.messages:
        return []
    popped = meta.messages
    meta.messages = []
    return popped


def fetch_trace_details(
    client: Any,
    trace_ids: list[str],
    cache: dict[str, LangfuseTraceDetailRecord] | None = None,
    *,
    champion: TranscriptChampion | None = None,
) -> dict[str, LangfuseTraceDetailRecord]:
    """Fetch full trace details for *trace_ids*, reusing entries in *cache* when provided.

    用线程池并发 ``trace.get``：单 phase 通常几十~上百条 trace，每条 RTT
    ~200-500ms（要拉完整 observations），串行 fetch 是 backfill 的主要瓶颈。
    并发上限通过 ``EVAL_BACKFILL_TRACE_GET_WORKERS`` 调节。

    当传入 *champion* 时启用**流式 transcript 归约**：每条 trace 解析后立即把
    ``plugin_metadata.messages`` 摘下，交给 *champion*（只保留最晚一条 work
    transcript），非冠军的 messages 随 worker 栈帧出作用域被回收。返回的 detail
    一律**不再携带** ``messages``，避免 N 份全量 transcript 同时驻留内存/落盘。
    未传 *champion* 时保持原行为（detail 携带完整 messages）。
    """
    out = cache if cache is not None else {}
    pending = [tid for tid in trace_ids if tid not in out]
    if not pending:
        return out

    def _fetch_one(tid: str) -> LangfuseTraceDetailRecord:
        detail = trace_detail_from_api(_trace_get_with_retry(client, tid))
        if champion is not None:
            messages = _detach_plugin_messages(detail)
            champion.offer(detail, messages)
        return detail

    workers = min(_resolve_trace_get_workers(), len(pending))
    if workers <= 1:
        for tid in pending:
            out[tid] = _fetch_one(tid)
        return out
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for tid, detail in zip(pending, pool.map(_fetch_one, pending)):
            out[tid] = detail
    return out


def trace_ref_from_detail(
    detail: LangfuseTraceDetailRecord,
    *,
    user_id: str | None = None,
    include_plugin_tokens: bool = True,
) -> LangfuseTraceRef:
    """Build a lightweight ``LangfuseTraceRef`` from a fetched trace detail."""
    tokens: LangfuseTraceTokens | None = None
    tool_count = 0
    if include_plugin_tokens and is_plugin_trace(detail.name):
        tokens = tokens_from_detail(detail)
        tool_count = count_tool_observations(detail)
    return LangfuseTraceRef(
        id=detail.id,
        name=detail.name,
        timestamp=detail.timestamp,
        session_id=detail.session_id,
        user_id=user_id,
        tags=list(detail.tags),
        agent_input=detail.agent_input,
        plugin_prompt=detail.plugin_prompt,
        plugin_response=detail.plugin_response,
        plugin_metadata=detail.plugin_metadata,
        tokens=tokens,
        tool_observation_count=tool_count,
        latency_seconds=detail.latency_seconds,
    )
