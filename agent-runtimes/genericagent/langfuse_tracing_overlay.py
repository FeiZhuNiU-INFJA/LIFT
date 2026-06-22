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
    from langfuse import Langfuse
    # 上游 cfg key 名兼容 langfuse SDK：public_key / secret_key / host
    _lf = Langfuse(**{k: v for k, v in _cfg.items() if k in ('public_key', 'secret_key', 'host')}) if _cfg else None
except Exception:
    _lf = None

_LIFT_TRACE_NAME = "genericagent-plugin"


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

    # ── Agent root trace ─────────────────────────────────────────
    @hooks.register('agent_before')
    def _on_agent_before(ctx):
        try:
            obs = _lf.start_observation(
                name=_LIFT_TRACE_NAME, as_type='agent',
                input={'user_input': ctx.get('user_input', '')},
            )
            # 上游 SDK 暴露 update_trace 接口：把 sessionId / tags 写到 trace 根
            try:
                sid = _lift_session_id()
                tags = _lift_tags()
                update_kwargs = {}
                if sid:
                    update_kwargs['session_id'] = sid
                if tags:
                    update_kwargs['tags'] = tags
                if update_kwargs and hasattr(obs, 'update_trace'):
                    obs.update_trace(**update_kwargs)
            except Exception:
                pass
            _tls.trace_obs = obs
        except Exception:
            _tls.trace_obs = None

    @hooks.register('agent_after')
    def _on_agent_after(ctx):
        try:
            obs = getattr(_tls, 'trace_obs', None)
            if obs:
                obs.update(output=ctx.get('exit_reason'))
                obs.end()
                _tls.trace_obs = None
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
