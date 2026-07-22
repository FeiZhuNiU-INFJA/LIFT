"""LIFT-aware Langfuse tracing overlay for EvoScientist.

EvoScientist 不像 GenericAgent 那样有 plugin hook 系统。整段事件流由
``EvoScientist.stream.events.stream_agent_events`` 这一个 async generator
产生（thinking / text / tool_call / tool_result / usage_stats / done / error），
CLI 在 ``--output-format stream-json`` 模式下把每条事件序列化为 JSONL 到
stdout（``EvoScientist/stream/json_sink.py``）。

LIFT 的 tracing 策略：**wrap ``stream_agent_events``**——保持原有事件顺序
逐条向 CLI 输出，同时旁路累积一份结构化 transcript / usage / tool_calls，
在 ``done`` / ``error`` 事件到达时打一条名为 ``evoscientist-plugin`` 的
Langfuse trace，metadata 走 OpenClaw camelCase schema（``messages`` /
``messageCount`` / ``toolCallBlocks`` / ``toolRoundtrips`` / ``toolNamesDistinct``）
以便 LIFT 后处理 ``langfuse_trace_stitch.py`` 1:1 配对 pre-chat
``work_agent`` / ``judge_agent`` span。

关键约定：
  - ``session_id`` 从 ``LIFT_EVOSCI_SESSION_ID`` env 读，LIFT chat_agent
    在每轮 ``docker exec`` 时通过 ``-e`` 注入（每轮 turn 都不同）。
  - ``tags`` 至少包含 ``LIFT_EVAL_RUN_TAG`` 与 session_id 本身。
  - ``usage_details`` 走 Anthropic-style key（``input`` /
    ``output`` / ``cache_read_input_tokens`` / ``cache_creation_input_tokens``
    / ``reasoning_tokens``），LIFT 后处理 ``_usage_breakdown`` 认得这批 key。
    ``usage_stats`` 事件只带 input/output 两字段，cache/reasoning 需要
    从 langchain callback 或 LangGraph event 里补齐；M1 baseline 先只
    覆盖 input/output，后续观察再决定是否用 langchain callback 补 cache。
  - overlay 通过 sitecustomize.py 在每个 Python 进程启动时被 import；
    如果 Langfuse SDK 或 EvoScientist 未装（例如 dev 期跑单元测试），
    自适应 no-op 不抛异常。
"""
from __future__ import annotations

import contextvars
import os
import threading
from typing import Any

__version__ = "0.2.0"

_LIFT_TRACE_NAME = "evoscientist-plugin"

# 每一次 ``stream_agent_events`` 调用打开一个 usage bucket；同一进程内
# ``OpenAICompatContentMixin._astream`` wrapper 会把 chunk-level usage_metadata
# 累加到这里。ContextVar 保证 async task 之间互不串扰。
# bucket schema: 5 字段口径 —— input 含 cached；由后处理侧统一按
# ``input - cache_read`` 折算 "fresh"。
_USAGE_BUCKET: contextvars.ContextVar[dict[str, int] | None] = contextvars.ContextVar(
    "_lift_evosci_usage_bucket", default=None,
)


def _lift_tags() -> list[str]:
    tags: list[str] = []
    run_tag = (os.environ.get("LIFT_EVAL_RUN_TAG") or "").strip()
    sid = (os.environ.get("LIFT_EVOSCI_SESSION_ID") or "").strip()
    if run_tag:
        tags.append(run_tag)
    if sid:
        tags.append(sid)
    return tags


def _lift_session_id() -> str | None:
    sid = (os.environ.get("LIFT_EVOSCI_SESSION_ID") or "").strip()
    return sid or None


def _install_overlay() -> bool:
    """Monkey-patch ``stream_agent_events``。成功返回 True。

    失败原因通常是：
      - langfuse SDK 未装（``import langfuse`` fail）
      - EvoScientist 未装（``import EvoScientist.stream.events`` fail）
      - Langfuse 环境变量缺失（``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY``）
    任一失败 → overlay 静默降级，不影响 EvoSci 主流程。

    可通过 ``LIFT_EVOSCI_OVERLAY=0`` 完全关闭 overlay（用于隔离调试）；
    ``LIFT_EVOSCI_TOKEN_PATCH=0`` 只关闭 _astream token 抓取 patch。
    """
    if os.environ.get("LIFT_EVOSCI_OVERLAY", "1") == "0":
        return False
    try:
        from langfuse import Langfuse, propagate_attributes
    except Exception:  # noqa: BLE001
        return False

    try:
        from EvoScientist.stream import events as evosci_events
    except Exception:  # noqa: BLE001
        return False

    # 顺带 patch langchain-openai BaseChatOpenAI —— EvoScientist 的
    # ``custom-openai`` provider 经 ``models.py`` 路由后实际走 provider="openai"
    # + 自定义 base_url，最终建的是 langchain-openai ``ChatOpenAI`` 实例；
    # 由于 base_url 非默认，langchain 的 ``__init__`` 会跳过 stream_usage 自动
    # 开启，导致请求体不带 ``stream_options.include_usage``，chunk 全无 usage。
    # 我们强制打开并把每 chunk 的 usage_metadata 累积到 ContextVar bucket。
    # 注意：``EvoScientist.llm.openai_compat.OpenAICompatContentMixin`` **只**
    # 给 DeepSeek/kimi 类 provider 用，custom-openai 不经过它——所以必须 patch
    # 底层 BaseChatOpenAI 才有效。
    # 通过 LIFT_EVOSCI_TOKEN_PATCH=0 关闭此 patch，用于隔离调试。
    if os.environ.get("LIFT_EVOSCI_TOKEN_PATCH", "1") != "0":
        try:
            from langchain_openai.chat_models.base import BaseChatOpenAI
            from langchain_openai import ChatOpenAI
            _patch_openai_compat_usage(BaseChatOpenAI)
            # ChatOpenAI 子类 override 了 ``_get_request_payload`` (rename
            # ``max_tokens`` -> ``max_completion_tokens``),而 EvoScientist
            # custom-openai 走的是 ChatOpenAI 实例;必须再对子类重复 patch,
            # 才能在子类 override 的 rename 之后把值再 rename 回 ``max_tokens``。
            _patch_openai_compat_usage(ChatOpenAI)
        except Exception:  # noqa: BLE001
            # patch 失败不阻断 tracing 主流程;只是没有 token 5 字段而已。
            pass

    pub = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
    sec = os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
    host = (
        os.environ.get("LANGFUSE_HOST", "").strip()
        or os.environ.get("LANGFUSE_BASE_URL", "").strip()
    )
    if not pub or not sec:
        return False

    lf = Langfuse(public_key=pub, secret_key=sec, host=host or None)
    _orig_stream = evosci_events.stream_agent_events

    if getattr(_orig_stream, "_lift_wrapped", False):
        return True  # 已装载（幂等）

    async def _wrapped_stream(
        agent: Any,
        message: Any,
        thread_id: str,
        metadata: dict | None = None,
        media: list[str] | None = None,
        events: object = None,
        **extra_kwargs,
    ):
        """Wrapped ``stream_agent_events``：透传事件 + 累积 transcript + 打 trace。

        签名必须与 upstream ``stream_agent_events`` 完全兼容——尤其是
        ``events: ToolSelectionView | None`` 参数：``gateway/local.py`` 会以
        keyword 形式传 ``events=self.events``；wrapper 若少此参数会抛
        ``TypeError: unexpected keyword argument 'events'``，导致 stream_json
        早退、CLI 打印 ``Goodbye!`` 后 exit(1)。``**extra_kwargs`` 兜住未来新增
        参数，防止签名漂移再次炸掉。
        """
        import sys as _sys
        _debug = os.environ.get("LIFT_EVOSCI_OVERLAY_DEBUG", "") == "1"
        if _debug:
            print(f"[lift overlay] stream ENTRY thread_id={thread_id!r}", file=_sys.stderr, flush=True)
        sid = _lift_session_id()
        tags = _lift_tags()
        attr_kwargs: dict[str, Any] = {}
        if sid:
            attr_kwargs["session_id"] = sid
        if tags:
            attr_kwargs["tags"] = tags

        # 单轮 turn 的累积状态。EvoScientist 一次 ``EvoSci -p`` 只跑一轮
        # ``stream_agent_events``，所以每次 wrap 各自持有独立状态。
        transcript: list[dict[str, Any]] = []
        tool_names: list[str] = []
        tool_call_blocks = 0
        # 用户消息：由 CLI 侧传入的 message 参数
        user_content = _user_message_text(message)
        if user_content:
            transcript.append({"role": "user", "content": user_content})
        # 累积 assistant 文本 + 当前 tool_call blocks（tool_call 事件先入
        # transcript.tool_calls，tool_result 事件配对成 tool message）
        assistant_content: list[str] = []
        pending_tool_calls: dict[str, dict[str, Any]] = {}
        finalized_tool_calls: list[dict[str, Any]] = []
        input_tokens = 0
        output_tokens = 0
        error_msg: str | None = None
        done_response: str = ""

        # 开启 usage bucket：``_astream`` patch 会把 chunk 的 usage_metadata
        # 累加到这里。turn 结束后从 bucket 读 5 字段口径。
        bucket: dict[str, int] = {
            "input": 0,
            "output": 0,
            "cache_read_input_tokens": 0,
            "reasoning_tokens": 0,
        }
        bucket_token = _USAGE_BUCKET.set(bucket)

        # 起 propagate_attributes 上下文 + root trace observation。
        # 用手动 __enter__/__exit__ 以适配 async generator 生命周期。
        attr_cm = propagate_attributes(**attr_kwargs) if attr_kwargs else None
        if attr_cm is not None:
            attr_cm.__enter__()
        obs_cm = lf.start_as_current_observation(
            name=_LIFT_TRACE_NAME,
            as_type="agent",
            input={"user_input": user_content or ""},
        )
        obs = obs_cm.__enter__()

        # 用一个 llm.chat generation 承载 usage 汇总（EvoScientist 只在
        # ``usage_stats`` 事件里给 total tokens，无逐次 LLM 调用粒度；对
        # LIFT 后处理来说 root-generation 单条就够了）。
        gen = None
        try:
            gen = lf.start_observation(
                name="llm.chat",
                as_type="generation",
                input=user_content or "",
            )
        except Exception:  # noqa: BLE001
            gen = None

        try:
            async for event in _orig_stream(
                agent,
                message,
                thread_id,
                metadata=metadata,
                media=media,
                events=events,
                **extra_kwargs,
            ):
                if _debug:
                    etype_dbg = event.get("type") if isinstance(event, dict) else type(event).__name__
                    print(f"[lift overlay] stream EVENT type={etype_dbg}", file=_sys.stderr, flush=True)
                _accumulate(
                    event,
                    assistant_content=assistant_content,
                    pending_tool_calls=pending_tool_calls,
                    finalized_tool_calls=finalized_tool_calls,
                    tool_names=tool_names,
                )
                etype = event.get("type") if isinstance(event, dict) else None
                if etype == "usage_stats":
                    input_tokens = int(event.get("input_tokens") or 0)
                    output_tokens = int(event.get("output_tokens") or 0)
                elif etype == "tool_call":
                    tool_call_blocks += 1
                elif etype == "done":
                    done_response = str(event.get("response") or event.get("content") or "")
                elif etype == "error":
                    error_msg = str(event.get("message") or "unknown error")
                yield event
        except BaseException as exc:  # noqa: BLE001
            error_msg = error_msg or repr(exc)
            if _debug:
                print(f"[lift overlay] stream EXC {type(exc).__name__}: {exc!r}", file=_sys.stderr, flush=True)
            raise
        finally:
            if _debug:
                print(f"[lift overlay] stream FINALLY error={error_msg!r}", file=_sys.stderr, flush=True)
            # Assemble final assistant message
            final_text = "".join(assistant_content) or done_response
            assistant_msg: dict[str, Any] = {"role": "assistant", "content": final_text}
            if finalized_tool_calls:
                assistant_msg["tool_calls"] = finalized_tool_calls
            if final_text or finalized_tool_calls:
                transcript.append(assistant_msg)

            try:
                if gen is not None:
                    # 优先用 ``_astream`` patch 累积到的 bucket；如果为空
                    # （patch 未生效 / 非流式路径），回落到 usage_stats event 的
                    # input/output（cache/reasoning 会缺）。
                    usage: dict[str, int] = {}
                    b_in = int(bucket.get("input") or 0)
                    b_out = int(bucket.get("output") or 0)
                    b_cache = int(bucket.get("cache_read_input_tokens") or 0)
                    b_reason = int(bucket.get("reasoning_tokens") or 0)
                    if b_in or b_out:
                        # 5 字段口径 —— key 名对齐 Anthropic-style，
                        # 后处理 ``_usage_breakdown`` 认这批。``input`` 含 cached，
                        # 走 ``input - cache_read`` 折算 fresh。
                        usage["input"] = b_in
                        usage["output"] = b_out
                        if b_cache:
                            usage["cache_read_input_tokens"] = b_cache
                        if b_reason:
                            usage["reasoning_tokens"] = b_reason
                    elif input_tokens or output_tokens:
                        usage["input"] = input_tokens
                        usage["output"] = output_tokens
                    gen.update(
                        output=final_text[:20000] if final_text else "",
                        usage_details=usage or None,
                    )
                    gen.end()
            except Exception:  # noqa: BLE001
                pass

            distinct_names = _distinct(tool_names)
            try:
                obs.update(
                    metadata={
                        "success": error_msg is None,
                        "error": error_msg,
                        "messages": transcript,
                        "messageCount": len(transcript),
                        "toolCallBlocks": tool_call_blocks,
                        "toolRoundtrips": tool_call_blocks,
                        "toolNamesDistinct": (
                            ",".join(distinct_names) if distinct_names else None
                        ),
                    },
                    output=final_text or done_response,
                )
            except Exception:  # noqa: BLE001
                pass
            try:
                obs_cm.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
            if attr_cm is not None:
                try:
                    attr_cm.__exit__(None, None, None)
                except Exception:  # noqa: BLE001
                    pass
            try:
                lf.flush()
            except Exception:  # noqa: BLE001
                pass
            try:
                _USAGE_BUCKET.reset(bucket_token)
            except Exception:  # noqa: BLE001
                pass

    _wrapped_stream._lift_wrapped = True  # type: ignore[attr-defined]
    evosci_events.stream_agent_events = _wrapped_stream  # type: ignore[assignment]
    return True


def _user_message_text(message: Any) -> str:
    """从 CLI 传给 ``stream_agent_events`` 的 message 参数里抽用户文本。

    ``message`` 可能是 str（``EvoSci -p "..."`` 单发）或 ``Command``（resume
    interrupt）；本 overlay 只关心 str 分支，其它降级为 ``str(message)``。
    """
    if isinstance(message, str):
        return message
    return str(message)


def _accumulate(
    event: Any,
    *,
    assistant_content: list[str],
    pending_tool_calls: dict[str, dict[str, Any]],
    finalized_tool_calls: list[dict[str, Any]],
    tool_names: list[str],
) -> None:
    """把单条 stream event 归入累积状态。事件 schema 见 ``stream/emitter.py``。"""
    if not isinstance(event, dict):
        return
    etype = event.get("type")
    if etype == "text":
        content = event.get("content")
        if isinstance(content, str):
            assistant_content.append(content)
    elif etype == "tool_call":
        tid = str(event.get("id") or "")
        name = str(event.get("name") or "")
        args = event.get("args") or {}
        if name:
            tool_names.append(name)
        entry = {
            "id": tid or None,
            "type": "function",
            "function": {"name": name, "arguments": args},
        }
        finalized_tool_calls.append(entry)
        if tid:
            pending_tool_calls[tid] = entry
    # tool_result / subagent_* / usage_stats / done / error / thinking：
    # 不写进 assistant transcript 主体，仅用于 tool_call 计数与 metadata。


def _distinct(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _patch_openai_compat_usage(cls: Any) -> None:
    """Wrap ``BaseChatOpenAI._astream`` + ``_should_stream_usage``：强制启用
    stream_usage 并累积 chunk-level usage_metadata。

    EvoScientist 的 ``custom-openai`` 经 ``models.py`` 路由后走 provider="openai"
    + 自定义 base_url，最终建的是 langchain-openai ``ChatOpenAI`` 实例（
    ``EvoScientist.llm.openai_compat.OpenAICompatContentMixin`` 只服务 DeepSeek 等
    provider，custom-openai 完全不经过它）。由于 base_url 非默认，langchain 的
    ``__init__`` 会跳过 stream_usage 自动开启（详见 ``langchain_openai.chat_models
    .base`` 的 "Enable stream_usage by default if using default base URL" 段落），
    最终请求体不带 ``stream_options={"include_usage": True}``，chunk 全无 usage。

    双管齐下 patch：
      1. ``_should_stream_usage`` 兜底：无视 caller，只要 caller/kwargs 没显式
         关掉，就返回 True——让 langchain 自己在 ``_stream``/``_astream`` 里把
         ``stream_options.include_usage`` 塞进请求体；
      2. ``_astream`` wrapper：把每 chunk ``ChatGenerationChunk.message.
         usage_metadata`` 累加到 ``_USAGE_BUCKET`` ContextVar。overlay
         ``_wrapped_stream`` 会在 turn 结束时读 bucket 打 5 字段 usage。

    幂等：``_lift_patched`` 标记防止双装载。
    """
    orig_astream = getattr(cls, "_astream", None)
    if orig_astream is not None and not getattr(orig_astream, "_lift_patched", False):
        async def _lift_astream(
            self: Any,
            messages: Any,
            stop: Any = None,
            run_manager: Any = None,
            *,
            stream_usage: bool | None = None,
            **kwargs: Any,
        ):
            effective_usage = True if stream_usage is None else stream_usage
            async for chunk in orig_astream(
                self,
                messages,
                stop=stop,
                run_manager=run_manager,
                stream_usage=effective_usage,
                **kwargs,
            ):
                _accumulate_chunk_usage(chunk)
                yield chunk

        _lift_astream._lift_patched = True  # type: ignore[attr-defined]
        cls._astream = _lift_astream

    orig_should = getattr(cls, "_should_stream_usage", None)
    if orig_should is not None and not getattr(orig_should, "_lift_patched", False):
        def _lift_should_stream_usage(
            self: Any,
            stream_usage: bool | None = None,
            **kwargs: Any,
        ) -> bool:
            # caller 显式 False 才关；否则一律 True。langchain 后续会自己塞
            # stream_options.include_usage 到请求体。
            if stream_usage is False:
                return False
            opts = kwargs.get("stream_options") or {}
            if isinstance(opts, dict) and opts.get("include_usage") is False:
                return False
            return True

        _lift_should_stream_usage._lift_patched = True  # type: ignore[attr-defined]
        cls._should_stream_usage = _lift_should_stream_usage

    # ─────────────────────────────────────────────────────────────────
    # 3) 强制注入 max_tokens 到请求 payload
    #
    # EvoScientist 主 flow ``get_chat_model`` 走 ``init_chat_model``,不会把
    # config.yaml 里的 max_tokens (甚至 EvoScientistConfig 里根本没这字段)
    # 传给 langchain-openai。custom-openai 走 provider=openai + 自定义 base_url,
    # ``BaseChatOpenAI`` 默认 ``max_tokens=None`` → payload 缺 max_tokens →
    # 火山方舟 doubao 端点按服务端默认(通常 <=8k)截断长产出。
    #
    # LIFT 契约:所有 runtime 共用 ``MAX_TOKENS`` 环境变量;wrap
    # ``_get_request_payload`` —— 这是 langchain-openai 建请求前最后一个汇总点,
    # 已完成 legacy_token_param rename / responses API 转换。返回后:
    #   1) 若已带 max_output_tokens (responses API):不动。
    #   2) 若已带 max_completion_tokens (ChatOpenAI 子类对 chat completions 的
    #      默认 rename):value 转移回 max_tokens,兼容非 o-系列 endpoint
    #      (火山方舟 doubao / ARK 官方文档明确列 max_tokens 字段)。
    #   3) 若什么都没有:按 payload 结构判断 responses vs chat completions,
    #      分别兜底 max_output_tokens / max_tokens = MAX_TOKENS env。
    # 关闭开关:``LIFT_EVOSCI_MAXTOKENS_PATCH=0``。
    # ─────────────────────────────────────────────────────────────────
    if os.environ.get("LIFT_EVOSCI_MAXTOKENS_PATCH", "1") != "0":
        try:
            _max_tokens_env = int((os.environ.get("MAX_TOKENS") or "0").strip() or "0")
        except (TypeError, ValueError):
            _max_tokens_env = 0
        orig_get_payload = getattr(cls, "_get_request_payload", None)
        if (
            _max_tokens_env > 0
            and orig_get_payload is not None
            and not getattr(orig_get_payload, "_lift_patched", False)
        ):
            def _lift_get_request_payload(self: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
                payload = orig_get_payload(self, *args, **kwargs)
                is_responses = "input" in payload  # responses API payload 特征
                if is_responses:
                    if "max_output_tokens" not in payload:
                        payload["max_output_tokens"] = _max_tokens_env
                else:
                    # chat completions:兜底 + 兼容非 o-系列 endpoint
                    if "max_completion_tokens" in payload and "max_tokens" not in payload:
                        payload["max_tokens"] = payload.pop("max_completion_tokens")
                    elif "max_tokens" not in payload:
                        payload["max_tokens"] = _max_tokens_env
                return payload

            _lift_get_request_payload._lift_patched = True  # type: ignore[attr-defined]
            cls._get_request_payload = _lift_get_request_payload


def _accumulate_chunk_usage(chunk: Any) -> None:
    """把 ChatGenerationChunk.message.usage_metadata 累加到 ContextVar bucket。

    langchain 只在最后一个 chunk 上带 usage_metadata；但也可能因中间聚合
    出现在多个 chunk（Responses API 分次 accumulate）。我们对每个 chunk 做
    加法累计，同时以后 chunk 的值不会重复回退（langchain 的 delta 累积语义
    是 "此 chunk 新增"，不是全量）。
    """
    bucket = _USAGE_BUCKET.get()
    if bucket is None:
        return
    try:
        message = getattr(chunk, "message", None) or chunk
        um = getattr(message, "usage_metadata", None)
    except Exception:  # noqa: BLE001
        return
    if not um:
        return
    try:
        inp = int(um.get("input_tokens") or 0)
        outp = int(um.get("output_tokens") or 0)
        input_details = um.get("input_token_details") or {}
        output_details = um.get("output_token_details") or {}
        cache_read = int(input_details.get("cache_read") or 0)
        reasoning = int(output_details.get("reasoning") or 0)
    except Exception:  # noqa: BLE001
        return
    if inp:
        bucket["input"] += inp
    if outp:
        bucket["output"] += outp
    if cache_read:
        bucket["cache_read_input_tokens"] += cache_read
    if reasoning:
        bucket["reasoning_tokens"] += reasoning


# 触发装载（幂等）。site-packages/sitecustomize.py 会 import 本模块。
_INSTALL_LOCK = threading.Lock()
_INSTALLED = False


def install() -> bool:
    """Public entry：主动触发一次 overlay 装载。适合单元测试和 REPL 手动调用。"""
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return True
        _INSTALLED = _install_overlay()
        return _INSTALLED


# module import 时自动装载
install()
