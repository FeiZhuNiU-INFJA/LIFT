"""LIFT-aware Langfuse tracing for GenericAgent (replaces upstream version).

Forces every agent.task root span to carry:
  - name        = ``genericagent-plugin``  (LIFT_LANGFUSE_PLUGIN_TRACE_NAMES)
  - session_id  = $LIFT_GA_SESSION_ID      (set per-chat by LIFT chat_agent)
  - tags        ⊇ {LIFT_EVAL_RUN_TAG, LIFT_GA_SESSION_ID}
  - input/output preserved from upstream behavior

This lets ``src/report/langfuse_trace_stitch.py`` 1:1 pair LIFT pre-chat
``work_agent`` / ``judge_agent`` spans with the GA-side root trace.

Self-activates on import if ``langfuse_config`` exists in ``mykey.py``.
"""
import os
import threading

try:
    from llmcore import _load_mykeys
    _cfg = _load_mykeys().get('langfuse_config') or {}
    from langfuse import Langfuse, propagate_attributes
    # 上游 cfg key 名兼容 langfuse SDK：public_key / secret_key / host
    _lf = Langfuse(**{k: v for k, v in _cfg.items() if k in ('public_key', 'secret_key', 'host')}) if _cfg else None
except Exception:
    _lf = None
    propagate_attributes = None  # type: ignore[assignment]

_LIFT_TRACE_NAME = "genericagent-plugin"


def _normalize_message(msg):
    """把 GA ``agent_loop`` 里的一条 message dict 规约成 backfill 友好的最小形状。

    LIFT 后处理 (``report_html.build_trajectory_nodes``) 只认 ``role`` / ``content``
    以及 assistant 的 ``tool_calls``（``function.name`` / ``function.arguments``）。
    GA 的 ``messages`` 每轮是 ``{role, content, tool_results?}``，直接透传即可；
    这里只做浅拷贝 + content 转字符串，避免把不可序列化对象塞进 metadata。
    """
    if not isinstance(msg, dict):
        return {"role": "assistant", "content": str(msg)}
    role = msg.get("role") or "assistant"
    content = msg.get("content")
    if not isinstance(content, (str, list, type(None))):
        content = str(content)
    out = {"role": role, "content": content if content is not None else ""}
    return out


def _assistant_message_from_response(response):
    """把 GA ``response`` 对象（含 ``content`` / ``tool_calls``）转成 assistant message。

    ``tool_calls`` 归一到 OpenAI 风格 ``{id, type, function:{name, arguments}}``，让
    ``build_trajectory_nodes`` 能识别出 tool 节点；同时返回本轮的工具名列表用于计数。
    """
    content = getattr(response, "content", "") or ""
    tool_calls_raw = getattr(response, "tool_calls", None) or []
    tool_calls = []
    tool_names = []
    for tc in tool_calls_raw:
        fn = getattr(tc, "function", None)
        name = getattr(fn, "name", None) if fn is not None else None
        args = getattr(fn, "arguments", None) if fn is not None else None
        if name:
            tool_names.append(name)
        tool_calls.append(
            {
                "id": getattr(tc, "id", None),
                "type": "function",
                "function": {"name": name, "arguments": args},
            }
        )
    msg = {"role": "assistant", "content": content if isinstance(content, str) else str(content)}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg, tool_names


def _lift_tags():
    tags = []
    run_tag = os.environ.get("LIFT_EVAL_RUN_TAG", "").strip()
    sid = os.environ.get("LIFT_GA_SESSION_ID", "").strip()
    if run_tag:
        tags.append(run_tag)
    if sid:
        tags.append(sid)
    return tags


def _lift_session_id():
    return os.environ.get("LIFT_GA_SESSION_ID", "").strip() or None


if _lf:
    import plugins.hooks as hooks
    import llmcore
    _tls = threading.local()

    # ── 进程/会话级 transcript 累积器 ─────────────────────────────
    # 关键：GA 的多轮对话（LIFT 每写一次 ``reply.txt``）会各自重新调用一次
    # ``agent_runner_loop``，因此 ``agent_before`` / ``agent_after`` 每轮都会触发，
    # 而 ``agent_runner_loop`` 内的 ``messages`` 每轮从新 user 重建（历史存在
    # llmcore backend 里）。若累积器绑在单次 loop 上，每轮 root span 只含本轮
    # 增量，无法还原整段会话。
    #
    # 方案：transcript / 工具名 累积器提升到**进程级全局**（``_STATE``，进程与
    # session 一一对应），跨轮持续追加；但 root span 仍**每轮一条**（agent_before
    # 建、agent_after 关闭并 flush），每轮把「截至当前轮的全量 transcript」写进
    # 该轮 root span 的 metadata。这样：
    #   - 每轮都有一条完整落库的 trace（Input/Output 不为 undefined）；
    #   - 最后一轮的 trace 天然含整段会话，后处理 ``TranscriptChampion``
    #     取「最晚一条」即完整对话。
    #
    # 注意工具计数口径：``messages`` 走 champion「取最晚一条」（全量），而后处理
    # ``build_work_analytics`` 对 ``toolCallBlocks`` 是**按轮 SUM**。因此 root
    # metadata 里的 ``toolCallBlocks`` 写**本轮** per-round 增量
    # （``_tls.round_tool_call_blocks``），SUM 后正好是总数；``toolNamesDistinct``
    # 用全局累积去重（展示用，不参与 SUM）。
    _STATE = {
        "transcript": [],   # 跨轮累积的全量 message 列表
        "tool_names": [],   # 跨轮累积的工具名（全局 distinct 展示用）
    }
    _STATE_LOCK = threading.Lock()

    def _distinct(names):
        seen = set()
        out = []
        for n in names:
            if n and n not in seen:
                seen.add(n)
                out.append(n)
        return out

    # ── Agent root trace（每轮一条）──────────────────────────────
    @hooks.register('agent_before')
    def _on_agent_before(ctx):
        """每轮开启 propagate_attributes（写 session_id/tags）+ root agent span。

        Langfuse Python SDK 4.x 起，``session_id`` / ``tags`` 通过
        ``propagate_attributes`` 上下文管理器传播到当前 OTel context 下所有 span。
        我们手动 ``__enter__``（在 agent_after ``__exit__``）——GA hook 分散在多个
        回调里，没有 ``with`` 包住整轮的位置。root span 每轮新建、当轮关闭，但
        transcript 从进程级 ``_STATE`` 跨轮累积。
        """
        try:
            sid = _lift_session_id()
            tags = _lift_tags()
            attr_kwargs = {}
            if sid:
                attr_kwargs['session_id'] = sid
            if tags:
                attr_kwargs['tags'] = tags
            attr_cm = propagate_attributes(**attr_kwargs) if attr_kwargs else None
            if attr_cm is not None:
                attr_cm.__enter__()
            obs_cm = _lf.start_as_current_observation(
                name=_LIFT_TRACE_NAME, as_type='agent',
                input={'user_input': ctx.get('user_input', '')},
            )
            obs = obs_cm.__enter__()
            _tls.attr_cm = attr_cm
            _tls.obs_cm = obs_cm
            _tls.trace_obs = obs
            _tls.round_tool_call_blocks = 0  # 本轮 per-round 工具计数
            # 播种本轮 user message（跳过 system prompt）到进程级 transcript。
            # seeded 让 llm_before 不重复追加 agent_before 已播种的这批 messages。
            _tls.seeded = False
            try:
                seed = ctx.get('messages')
                if isinstance(seed, list):
                    with _STATE_LOCK:
                        for m in seed:
                            if isinstance(m, dict) and m.get('role') == 'system':
                                continue
                            _STATE["transcript"].append(_normalize_message(m))
                    _tls.seeded = True
            except Exception:
                pass
        except Exception:
            _tls.attr_cm = None
            _tls.obs_cm = None
            _tls.trace_obs = None

    @hooks.register('agent_after')
    def _on_agent_after(ctx):
        """当轮结束：把「截至当前轮的全量 transcript」+ 本轮工具计数写进 root
        metadata，再反序退出 obs_cm → attr_cm，最后 flush。"""
        try:
            obs = getattr(_tls, 'trace_obs', None)
            if obs:
                # 从进程级 _STATE 读全量 transcript（含此前所有轮）。键名走 OpenClaw
                # camelCase schema，见 LangfusePluginTraceMetadata。
                try:
                    with _STATE_LOCK:
                        transcript = list(_STATE["transcript"])
                        distinct = _distinct(_STATE["tool_names"])
                    round_tcb = int(getattr(_tls, 'round_tool_call_blocks', 0) or 0)
                    obs.update(metadata={
                        "messages": transcript,
                        "messageCount": len(transcript),
                        # 本轮 per-round 工具数：后处理按轮 SUM，跨轮累加得总数。
                        "toolCallBlocks": round_tcb,
                        "toolRoundtrips": round_tcb,
                        "toolNamesDistinct": ",".join(distinct) if distinct else None,
                    })
                except Exception:
                    pass
                obs.update(output=ctx.get('exit_reason'))
            obs_cm = getattr(_tls, 'obs_cm', None)
            if obs_cm is not None:
                obs_cm.__exit__(None, None, None)
            attr_cm = getattr(_tls, 'attr_cm', None)
            if attr_cm is not None:
                attr_cm.__exit__(None, None, None)
            _tls.trace_obs = None
            _tls.obs_cm = None
            _tls.attr_cm = None
            _lf.flush()
        except Exception:
            pass

    # ── LLM generation span ──────────────────────────────────────
    @hooks.register('llm_before')
    def _on_llm_before(ctx):
        try:
            _tls.gen = _lf.start_observation(
                name='llm.chat', as_type='generation',
                input=str(ctx.get('messages', ''))[:20000],
            )
            _tls._usage = None
        except Exception:
            _tls.gen = None
        # 累积当轮新 user message 到进程级 transcript：agent_before 已播种当轮
        # 首批 messages（seeded=True）则跳过一次；同一轮内后续 llm_before（GA
        # while 循环多 turn）把新 user（承载 tool_results / next_prompt）追加。
        try:
            if getattr(_tls, 'seeded', False):
                _tls.seeded = False  # 当轮已由 agent_before 播种，本次不重复追加
            else:
                msgs = ctx.get('messages')
                if isinstance(msgs, list):
                    with _STATE_LOCK:
                        for m in msgs:
                            _STATE["transcript"].append(_normalize_message(m))
        except Exception:
            pass

    @hooks.register('llm_after')
    def _on_llm_after(ctx):
        try:
            gen = getattr(_tls, 'gen', None)
            if gen:
                gen.update(
                    output=str(ctx.get('response', ''))[:20000],
                    usage_details=getattr(_tls, '_usage', None),
                )
                gen.end()
                _tls.gen = None
        except Exception:
            pass
        # 追加本轮 assistant message（含归一化 tool_calls）到进程级 transcript，
        # 并累计工具计数：全局 tool_names（distinct 展示）+ 本轮 round 计数（SUM）。
        try:
            response = ctx.get('response')
            if response is not None:
                assistant_msg, tool_names = _assistant_message_from_response(response)
                with _STATE_LOCK:
                    _STATE["transcript"].append(assistant_msg)
                    if tool_names:
                        _STATE["tool_names"].extend(tool_names)
                if tool_names:
                    _tls.round_tool_call_blocks = (
                        int(getattr(_tls, 'round_tool_call_blocks', 0) or 0) + len(tool_names)
                    )
        except Exception:
            pass

    # ── Tool spans ───────────────────────────────────────────────
    @hooks.register('tool_before')
    def _on_tool_before(ctx):
        try:
            name = ctx.get('tool_name', '?')
            args = {k: v for k, v in (ctx.get('args') or {}).items() if not k.startswith('_')}
            if not hasattr(_tls, 'tstack'):
                _tls.tstack = []
            _tls.tstack.append(_lf.start_observation(name=name, as_type='tool', input=args))
        except Exception:
            pass

    @hooks.register('tool_after')
    def _on_tool_after(ctx):
        try:
            stack = getattr(_tls, 'tstack', [])
            if stack:
                sp = stack.pop()
                ret = ctx.get('ret')
                out = {
                    'data': getattr(ret, 'data', None),
                    'next_prompt': getattr(ret, 'next_prompt', None),
                    'should_exit': getattr(ret, 'should_exit', None),
                } if ret else None
                sp.update(output=out)
                sp.end()
        except Exception:
            pass

    # ── Usage tracking: tee SSE for token counts (上游同款实现) ──
    #
    # 抽 5 字段：input / output / cache_read_input_tokens / cache_creation_input_tokens /
    # reasoning_tokens。key 名对齐 Langfuse Anthropic-style 约定，使 dashboard 输入
    # 面板能自动把 cache 计入 input 汇总。LIFT 侧 ``_usage_breakdown`` 也识别这批 key。
    def _extract_usage(buf):
        u = {}
        import json as _j
        for line in buf:
            s = line.decode('utf-8', 'replace') if isinstance(line, (bytes, bytearray)) else line
            if not s or not s.startswith('data:'):
                continue
            ds = s[5:].lstrip()
            if ds == '[DONE]':
                continue
            try:
                evt = _j.loads(ds)
            except Exception:
                continue
            if evt.get('type') == 'message_start':
                us = evt.get('message', {}).get('usage', {}) or {}
                u['input'] = us.get('input_tokens', u.get('input', 0))
                if us.get('cache_creation_input_tokens'):
                    u['cache_creation_input_tokens'] = us['cache_creation_input_tokens']
                if us.get('cache_read_input_tokens'):
                    u['cache_read_input_tokens'] = us['cache_read_input_tokens']
            elif evt.get('type') == 'message_delta':
                ot = (evt.get('usage') or {}).get('output_tokens')
                if ot:
                    u['output'] = ot
            elif evt.get('type') == 'response.completed':
                us = evt.get('response', {}).get('usage', {}) or {}
                if us.get('input_tokens'):
                    u['input'] = us['input_tokens']
                if us.get('output_tokens'):
                    u['output'] = us['output_tokens']
                cr = (us.get('input_tokens_details') or {}).get('cached_tokens')
                if cr:
                    u['cache_read_input_tokens'] = cr
                # OpenAI Responses API：reasoning 计入 output_tokens_details。
                rt = (us.get('output_tokens_details') or {}).get('reasoning_tokens')
                if rt:
                    u['reasoning_tokens'] = rt
            else:
                us = evt.get('usage')
                if us:
                    if us.get('prompt_tokens'):
                        u['input'] = us['prompt_tokens']
                    if us.get('completion_tokens'):
                        u['output'] = us['completion_tokens']
                    cr = (us.get('prompt_tokens_details') or {}).get('cached_tokens')
                    if cr:
                        u['cache_read_input_tokens'] = cr
                    # OpenAI Chat Completions：reasoning 计入 completion_tokens_details。
                    rt = (us.get('completion_tokens_details') or {}).get('reasoning_tokens')
                    if rt:
                        u['reasoning_tokens'] = rt
        return u or None

    def _wrap_parser(orig):
        def wrapped(resp_lines, *a, **kw):
            buf = []

            def tee():
                for ln in resp_lines:
                    buf.append(ln)
                    yield ln

            ret = yield from orig(tee(), *a, **kw)
            try:
                _tls._usage = _extract_usage(buf)
            except Exception:
                pass
            return ret
        return wrapped

    try:
        llmcore._parse_claude_sse = _wrap_parser(llmcore._parse_claude_sse)
    except Exception:
        pass
    try:
        llmcore._parse_openai_sse = _wrap_parser(llmcore._parse_openai_sse)
    except Exception:
        pass

    # non-stream JSON 路径不经过 _wrap_parser 的 SSE line 累积，直接由 llmcore 内
    # 部把 provider 归一后的 usage 传给 `_record_usage(usage, api_mode)`。为让 non-
    # stream 也能拿到 usage_details，直接 wrap `_record_usage` —— 这是 SSE / JSON /
    # streaming 三条 parser 的公共汇聚点（见 llmcore.py 中 7 处调用）。
    #
    # api_mode 取值：
    #   * 'messages'         → Anthropic Messages
    #   * 'chat_completions' → OpenAI Chat Completions（reasoning 在 completion_tokens_details）
    #   * 'responses'        → OpenAI Responses API（reasoning 在 output_tokens_details）
    try:
        _orig_record_usage = llmcore._record_usage

        def _wrapped_record_usage(usage, api_mode):
            try:
                _orig_record_usage(usage, api_mode)
            finally:
                try:
                    if not usage:
                        return
                    u: dict[str, int] = {}
                    if api_mode == 'messages':
                        if usage.get('input_tokens'):
                            u['input'] = usage['input_tokens']
                        if usage.get('output_tokens'):
                            u['output'] = usage['output_tokens']
                        if usage.get('cache_creation_input_tokens'):
                            u['cache_creation_input_tokens'] = usage['cache_creation_input_tokens']
                        if usage.get('cache_read_input_tokens'):
                            u['cache_read_input_tokens'] = usage['cache_read_input_tokens']
                    elif api_mode == 'responses':
                        if usage.get('input_tokens'):
                            u['input'] = usage['input_tokens']
                        if usage.get('output_tokens'):
                            u['output'] = usage['output_tokens']
                        cr = (usage.get('input_tokens_details') or {}).get('cached_tokens')
                        if cr:
                            u['cache_read_input_tokens'] = cr
                        rt = (usage.get('output_tokens_details') or {}).get('reasoning_tokens')
                        if rt:
                            u['reasoning_tokens'] = rt
                    else:  # chat_completions
                        if usage.get('prompt_tokens'):
                            u['input'] = usage['prompt_tokens']
                        if usage.get('completion_tokens'):
                            u['output'] = usage['completion_tokens']
                        cr = (usage.get('prompt_tokens_details') or {}).get('cached_tokens')
                        if cr:
                            u['cache_read_input_tokens'] = cr
                        rt = (usage.get('completion_tokens_details') or {}).get('reasoning_tokens')
                        if rt:
                            u['reasoning_tokens'] = rt
                    if u:
                        _tls._usage = u
                except Exception:
                    pass

        llmcore._record_usage = _wrapped_record_usage
    except Exception:
        pass
