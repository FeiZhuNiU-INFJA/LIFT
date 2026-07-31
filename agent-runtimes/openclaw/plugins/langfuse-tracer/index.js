/**
 * langfuse-tracer — OpenClaw plugin
 *
 * Sends an agent trace + LLM generation to Langfuse after every agent turn.
 * Uses the Langfuse REST API directly (no npm packages required).
 *
 * Required env vars in the openclaw-gateway container:
 *   LANGFUSE_PUBLIC_KEY   — project public key  (same as LANGFUSE_INIT_PROJECT_PUBLIC_KEY)
 *   LANGFUSE_SECRET_KEY   — project secret key  (same as LANGFUSE_INIT_PROJECT_SECRET_KEY)
 *   LANGFUSE_BASE_URL     — e.g. http://172.21.0.1:3050 (Docker host gateway to Langfuse)
 *
 * OpenClaw (non-bundled plugins): `agent_end` is a conversation hook. You must set in
 * ~/.openclaw/openclaw.json under plugins.entries["langfuse-tracer"]:
 *   "hooks": { "allowConversationAccess": true }
 * Otherwise `api.on("agent_end", ...)` is never registered (see registry.ts
 * registerTypedHook + CONVERSATION_HOOK_NAMES) — you will see before_agent_start
 * logs but never agent_end / Langfuse traces.
 *
 * File log (append): `langfuse-tracer-plugin.log` next to this plugin (override via LANGFUSE_TRACER_LOG_FILE).
 * Override with LANGFUSE_TRACER_LOG_FILE=/abs/path.log or a path relative to repo root.
 *
 * Optional: LANGFUSE_TRACER_DEBUG_MESSAGES=1 — append full messages JSON (truncated) to file log.
 * Full ``event.messages`` is written to Langfuse ``metadata.messages`` (no global length cap).
 * Only per-message sanitization when a message is not JSON-serializable or contains binary payloads.
 *
 * Each `before_agent_start` / `agent_end` writes hook `event` (1st) and `ctx` (2nd) to the file
 * (JSON, size caps). agent_end omits `messages` body unless LANGFUSE_TRACER_DEBUG_MESSAGES=1.
 */

import { appendFileSync } from 'node:fs';
import { dirname, isAbsolute, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const PLUGIN_ROOT = dirname(fileURLToPath(import.meta.url));

export function register(api) {
  const publicKey = process.env.LANGFUSE_PUBLIC_KEY?.trim();
  const secretKey = process.env.LANGFUSE_SECRET_KEY?.trim();
  const baseUrl = (process.env.LANGFUSE_BASE_URL?.trim() ?? 'http://172.21.0.1:3050').replace(/\/$/, '');

  if (!publicKey || !secretKey) {
    api.logger.info('[langfuse-tracer] LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY not set — tracing disabled');
    return;
  }

  const rawLogFile = process.env.LANGFUSE_TRACER_LOG_FILE?.trim();
  const logFilePath = rawLogFile
    ? isAbsolute(rawLogFile)
      ? rawLogFile
      : resolve(PLUGIN_ROOT, rawLogFile)
    : join(PLUGIN_ROOT, 'langfuse-tracer-plugin.log');

  const appendLog = (body) => {
    const stamp = new Date().toISOString();
    const text = typeof body === 'string' ? body : String(body);
    try {
      appendFileSync(logFilePath, `[${stamp}]\n${text}\n${'─'.repeat(72)}\n`, 'utf8');
    } catch (err) {
      api.logger.warn?.(`[langfuse-tracer] file log failed (${logFilePath}): ${String(err)}`);
    }
  };

  const authHeader = 'Basic ' + Buffer.from(`${publicKey}:${secretKey}`).toString('base64');
  api.logger.info(`[langfuse-tracer] Langfuse tracing enabled → ${baseUrl}`);
  appendLog(`[langfuse-tracer] Langfuse tracing enabled → ${baseUrl}\nfile: ${logFilePath}`);

  // Capture the prompt text before the turn starts so we have a clean "input"
  const pendingPrompts = new Map();

  // llm_output usage accumulator keyed by sessionKey. OpenClaw's `agent_end` fires
  // before the current turn's assistant.usage.cacheRead / cacheWrite / reasoningTokens
  // land on message.usage (they are patched on the *next* turn's history rewrite), so
  // reading turnSlice.msg.usage at agent_end always sees cacheRead=0 for the just-
  // produced assistant. `llm_output` fires per model call inside the turn with the
  // provider-normalized `usage` payload — accumulating those gives the true per-turn
  // usage. See LIFT docs: openclaw cache_read=0 root cause.
  const pendingUsage = new Map();

  api.on('llm_output', (event, ctx) => {
    const key = ctx?.sessionKey ?? ctx?.agentId ?? 'default';
    let usagePreview = '(no usage)';
    try {
      usagePreview = event && typeof event === 'object' && event.usage
        ? JSON.stringify(event.usage).slice(0, 400)
        : '(no usage)';
    } catch {}
    appendLog(`[langfuse-tracer] hit: llm_output sessionKey=${key} usage=${usagePreview}`);
    const u = event?.usage;
    if (!u || typeof u !== 'object') return;
    const inp = num(u.input);
    const out = num(u.output);
    const cr = num(u.cacheRead);
    const cw = num(u.cacheWrite);
    const reasoning = num(u.reasoningTokens);
    if (inp === 0 && out === 0 && cr === 0 && cw === 0 && reasoning === 0) return;
    const acc = pendingUsage.get(key) ?? {
      input: 0,
      output: 0,
      cacheRead: 0,
      cacheWrite: 0,
      reasoning: 0,
      calls: 0,
    };
    acc.input += inp;
    acc.output += out;
    acc.cacheRead += cr;
    acc.cacheWrite += cw;
    acc.reasoning += reasoning;
    acc.calls += 1;
    pendingUsage.set(key, acc);
  });

  api.on('before_agent_start', (event, ctx) => {
    api.logger.info(
      `[langfuse-tracer] hit: before_agent_start sessionKey=${ctx?.sessionKey ?? '-'} agentId=${ctx?.agentId ?? '-'}`,
    );
    appendLog(
      `[langfuse-tracer] hit: before_agent_start sessionKey=${ctx?.sessionKey ?? '-'} agentId=${ctx?.agentId ?? '-'}`,
    );
    logOpenClawHookContext(appendLog, 'before_agent_start', event, ctx, {
      omitMessages: false,
      maxEvent: 48_000,
    });
    const key = ctx.sessionKey ?? ctx.agentId ?? 'default';
    pendingPrompts.set(key, {
      prompt: event.prompt ?? '',
      startedAt: Date.now(),
    });
  });

  api.on('agent_end', async (event, ctx) => {
    api.logger.info(
      `[langfuse-tracer] hit: agent_end sessionKey=${ctx?.sessionKey ?? '-'} agentId=${ctx?.agentId ?? '-'} eventKeys=${event && typeof event === 'object' ? Object.keys(event).join(',') : String(event)}`,
    );
    appendLog(
      `[langfuse-tracer] hit: agent_end sessionKey=${ctx?.sessionKey ?? '-'} agentId=${ctx?.agentId ?? '-'} eventKeys=${event && typeof event === 'object' ? Object.keys(event).join(',') : String(event)}`,
    );
    logOpenClawHookContext(appendLog, 'agent_end', event, ctx, {
      omitMessages: true,
      maxEvent: 96_000,
    });
    const { agentId, sessionKey } = ctx;
    const runId = pickTrimmedString(ctx?.runId);
    const shortSessionId = pickTrimmedString(ctx?.sessionId);
    /** Langfuse `sessionId`: short id from ctx, else full sessionKey. */
    const langfuseSessionId = shortSessionId ?? pickTrimmedString(sessionKey);
    const { messages, success, durationMs, error } = event;
    const safeMessages = Array.isArray(messages) ? messages : [];

    if (process.env.LANGFUSE_TRACER_DEBUG_MESSAGES === '1') {
      const MESSAGES_JSON_LOG_MAX = 120_000;
      try {
        const serialized = JSON.stringify(safeMessages.length ? safeMessages : null, null, 2);
        const suffix =
          serialized.length > MESSAGES_JSON_LOG_MAX
            ? `\n... [truncated: ${serialized.length} chars total]`
            : '';
        appendLog(
          `[langfuse-tracer] agent_end messages JSON (count=${safeMessages.length}, len=${serialized.length}):\n${serialized.slice(0, MESSAGES_JSON_LOG_MAX)}${suffix}`,
        );
      } catch (stringifyErr) {
        appendLog(`[langfuse-tracer] agent_end messages JSON.stringify failed: ${String(stringifyErr)}`);
        api.logger.warn(
          `[langfuse-tracer] agent_end messages JSON.stringify failed: ${String(stringifyErr)}`,
        );
      }
    }

    const key = sessionKey ?? agentId ?? 'default';
    const pending = pendingPrompts.get(key);
    pendingPrompts.delete(key);

    const now = new Date().toISOString();
    const startedAt = pending?.startedAt ?? (durationMs ? Date.now() - durationMs : Date.now());
    const startTime = new Date(startedAt).toISOString();

    // --- Extract input: prefer captured prompt, fall back to last user message ---
    let input = pending?.prompt ?? '';
    if (!input) {
      for (let i = safeMessages.length - 1; i >= 0; i--) {
        const msg = safeMessages[i];
        if (msg?.role === 'user') {
          input = extractText(msg.content, 2000);
          break;
        }
      }
    }

    // --- Extract output: last assistant message text ---
    let output = '';
    for (let i = safeMessages.length - 1; i >= 0; i--) {
      const msg = safeMessages[i];
      if (msg?.role === 'assistant') {
        output = extractText(msg.content, 4000);
        break;
      }
    }

    const lastUserIndex = findLastIndex(safeMessages, (m) => m?.role === 'user');
    const turnSlice = lastUserIndex >= 0 ? safeMessages.slice(lastUserIndex + 1) : safeMessages;

    // Primary source: llm_output-accumulated usage for this session. Fallback to
    // scanning turnSlice.assistant.usage when llm_output didn't fire (defensive:
    // subagent embedded runs, or hook subscription failing to bind).
    //
    // OpenClaw fires `llm_output` synchronously *before* `agent_end` (see
    // run-attempt.runCodexAppServerAttempt), but both hooks are dispatched via
    // an internal `runVoidHook` that schedules handlers on the microtask queue.
    // Empirically, this agent_end handler runs *before* the llm_output handler
    // gets to update pendingUsage, so a naive read here returns undefined. Yield
    // one macrotask via setImmediate to drain pending microtasks — after this
    // resolves, any llm_output handler that fired before us has completed and
    // pendingUsage carries the true per-turn usage (cacheRead / reasoningTokens
    // included). Without this yield, cache_read/reasoning silently land as 0.
    await new Promise((resolve) => setImmediate(resolve));
    const accumulated = pendingUsage.get(key);
    pendingUsage.delete(key);
    appendLog(
      `[langfuse-tracer] agent_end usage source: accumulated=${
        accumulated ? JSON.stringify(accumulated) : '(none)'
      }`,
    );
    const usage = usageFromAccumulator(accumulated) ?? aggregateAssistantUsage(turnSlice);
    // Langfuse ingestion API: `usage` only recognizes input/output/total/unit; the
    // fine-grained cache_read_input_tokens / cache_creation_input_tokens /
    // reasoning_tokens keys must live under `usageDetails` (Anthropic-style names),
    // otherwise they are silently dropped. Mirror the payload so both legacy and
    // typed consumers see the numbers. See LIFT lesson: openclaw ingestion drop.
    const usageDetails = usageDetailsFromUsage(usage);
    // 统一观测契约（见 docs/langfuse-unified-observation-contract）：
    // toolCallBlocks / toolRoundtrips 必须是"同 session **跨轮累积**"值。OpenClaw 的
    // `agent_end` 每个 eval turn 触发一次、各发一条 openclaw-plugin root trace，且
    // `event.messages`(safeMessages) 是**累积**的完整会话历史；因此统计工具调用要走
    // 全量 safeMessages，而不是仅当轮的 turnSlice —— 否则每轮 root 只带当轮增量，后处理
    // 取 max 会严重少算。计数与 trajectory.jsonl 的 last model.completed 快照口径一致。
    const toolStats = summarizeTools(safeMessages);
    // 跨轮累积的完整工具调用列表 → root output.tool_calls（与 Hermes 对齐，供人工在
    // 报告里直接检查，也让后处理 `_tool_call_count_from_output` 校准 toolCallBlocks）。
    const toolCallsList = collectToolCalls(safeMessages);
    const messagesPayload = serializeMessagesForMetadata(safeMessages, appendLog);

    const traceTags = buildTraceTags({
      agentId,
      runId,
      sessionId: shortSessionId,
    });

    const traceId = randomId();
    const generationId = randomId();
    const batchItemId1 = randomId();
    const batchItemId2 = randomId();

    const batch = [
      {
        id: batchItemId1,
        type: 'trace-create',
        timestamp: now,
        body: {
          id: traceId,
          name: 'openclaw-plugin',
          sessionId: langfuseSessionId,
          userId: agentId ?? 'unknown',
          tags: traceTags,
          input: input.slice(0, 2000) || undefined,
          // root output 携带"同 session 跨轮累积"的完整 tool_calls 列表（统一观测契约）。
          // 有工具调用时用 {content, tool_calls} 对象形态（与 Hermes root output 对齐，
          // 供人工检查 + 后处理 `_tool_call_count_from_output` 校准）；无则退回纯文本。
          output: toolCallsList.length > 0
            ? { content: output.slice(0, 4000) || null, tool_calls: toolCallsList }
            : (output.slice(0, 4000) || undefined),
          metadata: {
            success,
            error: error ?? undefined,
            messageCount: safeMessages.length,
            messages: messagesPayload.messages,
            messagesTruncated: messagesPayload.anyMessageTruncated,
            messagesSanitizedCount: messagesPayload.sanitizedCount,
            messagesSerializedChars: messagesPayload.serializedChars,
            ...toolStats,
          },
          timestamp: startTime,
        },
      },
      {
        id: batchItemId2,
        type: 'generation-create',
        timestamp: now,
        body: {
          id: generationId,
          traceId,
          name: 'llm',
          startTime,
          endTime: now,
          input: input.slice(0, 2000) || undefined,
          output: output.slice(0, 4000) || undefined,
          level: success ? 'DEFAULT' : 'ERROR',
          statusMessage: error ?? undefined,
          usage,
          usageDetails,
          // 统一观测契约：全量 transcript 只挂在 root trace 的 metadata.messages，
          // GENERATION 子节点不再重复写 messages（避免每个 LLM 节点携带 N 份完整历史）。
          // 保留轻量工具计数字段（toolStats）+ 观测计数，供 UI 快速查看。
          metadata: {
            durationMs,
            messageCount: safeMessages.length,
            ...toolStats,
          },
        },
      },
    ];

    try {
      const res = await fetch(`${baseUrl}/api/public/ingestion`, {
        method: 'POST',
        headers: {
          Authorization: authHeader,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ batch }),
      });
      if (!res.ok) {
        const text = await res.text().catch(() => '');
        const msg = `[langfuse-tracer] Ingestion failed ${res.status}: ${text.slice(0, 200)}`;
        appendLog(msg);
        api.logger.warn(msg);
      }
    } catch (err) {
      const msg = `[langfuse-tracer] Fetch error: ${String(err)}`;
      appendLog(msg);
      api.logger.warn(msg);
    }
  });
}

// ── helpers ──────────────────────────────────────────────────────────────────

function pickTrimmedString(v) {
  if (typeof v !== 'string') return undefined;
  const t = v.trim();
  return t ? t : undefined;
}

/** Langfuse trace tags: fixed markers + ctx.runId / ctx.sessionId when present (raw values, no key prefix). */
function buildTraceTags({ agentId, runId, sessionId }) {
  const tags = ['openclaw', agentId ?? 'unknown'];
  const evalRunTag = pickTrimmedString(process.env.LIFT_EVAL_RUN_TAG);
  if (evalRunTag) tags.push(evalRunTag);
  if (runId) tags.push(runId);
  if (sessionId && sessionId !== runId) tags.push(sessionId);
  return tags;
}

/** Log OpenClaw hook args: `event` is 1st param, `ctx` is 2nd (conversation / session context). */
function logOpenClawHookContext(appendLog, phase, event, ctx, opts = {}) {
  const maxCtx = opts.maxCtx ?? 16_384;
  const maxEvent = opts.maxEvent ?? 96_000;
  const omitMessages = opts.omitMessages === true;

  let ctxText;
  try {
    ctxText = JSON.stringify(ctx ?? null, null, 2);
  } catch (err) {
    ctxText = `[ctx JSON.stringify failed: ${String(err)}]`;
  }
  if (ctxText.length > maxCtx) {
    const total = ctxText.length;
    ctxText = `${ctxText.slice(0, maxCtx)}\n... [ctx truncated, ${total} chars total]`;
  }
  appendLog(`[langfuse-tracer] ${phase} ctx (hook 2nd arg):\n${ctxText}`);

  const eventPayload =
    omitMessages && event && typeof event === 'object'
      ? summarizeAgentEndEventForLog(event)
      : event;
  let eventText;
  try {
    eventText = JSON.stringify(eventPayload, null, 2);
  } catch (err) {
    eventText = `[event JSON.stringify failed: ${String(err)}]`;
  }
  if (eventText.length > maxEvent) {
    const total = eventText.length;
    eventText = `${eventText.slice(0, maxEvent)}\n... [event truncated, ${total} chars total]`;
  }
  appendLog(`[langfuse-tracer] ${phase} event (hook 1st arg):\n${eventText}`);
}

/** Shallow copy of agent_end `event` with messages replaced by length only (avoids huge logs). */
function summarizeAgentEndEventForLog(event) {
  if (!event || typeof event !== 'object') {
    return { _nonObjectEvent: String(event) };
  }
  const { messages, ...rest } = event;
  return {
    ...rest,
    messages: Array.isArray(messages) ? { _omitted: true, length: messages.length } : messages,
  };
}

function findLastIndex(arr, pred) {
  if (!Array.isArray(arr)) return -1;
  for (let i = arr.length - 1; i >= 0; i--) {
    if (pred(arr[i], i)) return i;
  }
  return -1;
}

/**
 * Aggregate per-turn assistant usage.
 *
 * OpenClaw's transcript assistant.usage shape:
 *   { input, output, cacheRead, cacheWrite, reasoningTokens, totalTokens, cost }
 *
 * We pass all 5 tokens forward to Langfuse under Langfuse-convention keys so
 * ``prompt_tokens_details.cached_tokens``-style dashboards / downstream extract
 * can see the full breakdown. Historically this only forwarded input / output,
 * silently dropping cacheRead (which for Ark can be >90% of prompt) and
 * reasoningTokens (relevant when REASONING_EFFORT=high).
 */
function aggregateAssistantUsage(turnMessages) {
  let inputSum = 0;
  let outputSum = 0;
  let cacheReadSum = 0;
  let cacheWriteSum = 0;
  let reasoningSum = 0;
  let hasAny = false;
  for (const msg of turnMessages) {
    if (msg?.role !== 'assistant' || !msg.usage || typeof msg.usage !== 'object') continue;
    const u = msg.usage;
    const inp = num(u.input_tokens ?? u.input);
    const out = num(u.output_tokens ?? u.output);
    const cr = num(u.cacheRead ?? u.cache_read_input_tokens ?? u.cached_tokens);
    const cw = num(u.cacheWrite ?? u.cache_creation_input_tokens);
    const reasoning = num(u.reasoningTokens ?? u.reasoning_tokens);
    if (inp > 0 || out > 0 || cr > 0 || cw > 0 || reasoning > 0) hasAny = true;
    inputSum += inp;
    outputSum += out;
    cacheReadSum += cr;
    cacheWriteSum += cw;
    reasoningSum += reasoning;
  }
  if (!hasAny) {
    return undefined;
  }
  // Langfuse convention: keys containing "input" contribute to the dashboard input
  // total, so we use ``cache_read_input_tokens`` / ``cache_creation_input_tokens``
  // (Anthropic-style names) — LIFT's ``_usage_breakdown`` already recognizes both
  // these and OpenClaw's raw ``cacheRead`` / ``cacheWrite`` keys.
  const usage = { unit: 'TOKENS' };
  if (inputSum > 0) usage.input = inputSum;
  if (outputSum > 0) usage.output = outputSum;
  if (cacheReadSum > 0) usage.cache_read_input_tokens = cacheReadSum;
  if (cacheWriteSum > 0) usage.cache_creation_input_tokens = cacheWriteSum;
  if (reasoningSum > 0) usage.reasoning_tokens = reasoningSum;
  return usage;
}

/**
 * Turn `pendingUsage` accumulator entry into a Langfuse `usage` object.
 *
 * `llm_output` fires per model call inside a turn with the provider-normalized
 * payload `{ input, output, cacheRead, cacheWrite, reasoningTokens }`. This is
 * the only place where OpenClaw exposes cache_read at the moment the turn ends —
 * `assistant.usage.cacheRead` is patched onto the message only during the *next*
 * turn's history rewrite (ensureAssistantUsageSnapshots), which is after
 * `agent_end` has already fired. Returns undefined when nothing was accumulated
 * so the caller can fall back to `aggregateAssistantUsage`.
 */
function usageFromAccumulator(acc) {
  if (!acc || acc.calls === 0) return undefined;
  const usage = { unit: 'TOKENS' };
  if (acc.input > 0) usage.input = acc.input;
  if (acc.output > 0) usage.output = acc.output;
  if (acc.cacheRead > 0) usage.cache_read_input_tokens = acc.cacheRead;
  if (acc.cacheWrite > 0) usage.cache_creation_input_tokens = acc.cacheWrite;
  if (acc.reasoning > 0) usage.reasoning_tokens = acc.reasoning;
  return usage;
}

/** Turn a `usage` object into Langfuse `usageDetails` — the ingestion API only
 * recognises input/output/total/unit inside `usage`; fine-grained cache/reasoning
 * fields must live under `usageDetails` to persist. Numbers are copied verbatim
 * so dashboard input totals aggregate the cache_*_input_tokens correctly. */
function usageDetailsFromUsage(usage) {
  if (!usage || typeof usage !== 'object') return undefined;
  const details = {};
  const passthrough = [
    'input',
    'output',
    'total',
    'cache_read_input_tokens',
    'cache_creation_input_tokens',
    'reasoning_tokens',
  ];
  for (const key of passthrough) {
    const v = usage[key];
    if (typeof v === 'number' && v > 0) details[key] = v;
  }
  return Object.keys(details).length > 0 ? details : undefined;
}

function num(v) {
  return typeof v === 'number' && !Number.isNaN(v) ? v : 0;
}

/** toolResult role + assistant content blocks type toolCall (OpenClaw transcript shape). */
function shouldIgnoreToolCallBlock(block) {
  if (block?.type !== 'toolCall' || block?.name !== 'exec') return false;
  const command = block?.arguments?.command;
  return typeof command === 'string' && command.includes('http://127.0.0.1:18090');
}

function summarizeTools(turnMessages) {
  const toolNames = new Set();
  const ignoredToolCallIds = new Set();
  let toolResultCount = 0;
  let toolCallBlockCount = 0;

  for (const msg of turnMessages) {
    if (msg?.role === 'toolResult') {
      if (msg.toolName === 'exec' && ignoredToolCallIds.has(msg.toolCallId)) {
        continue;
      }
      toolResultCount += 1;
      const n = msg.toolName;
      if (typeof n === 'string' && n.trim()) toolNames.add(n.trim());
    }
    if (msg?.role === 'assistant' && Array.isArray(msg.content)) {
      for (const block of msg.content) {
        if (block?.type === 'toolCall' && typeof block.name === 'string' && block.name.trim()) {
          if (shouldIgnoreToolCallBlock(block)) {
            if (typeof block.id === 'string' && block.id.trim()) {
              ignoredToolCallIds.add(block.id.trim());
            }
            continue;
          }
          toolCallBlockCount += 1;
          toolNames.add(block.name.trim());
        }
      }
    }
  }

  return {
    toolRoundtrips: toolResultCount,
    toolCallBlocks: toolCallBlockCount,
    toolNamesDistinct: toolNames.size > 0 ? [...toolNames].sort().join(',') : undefined,
  };
}

/**
 * Collect the cross-turn cumulative tool_calls list from full session messages,
 * normalized to OpenAI-style `{id, type, function:{name, arguments}}`.
 *
 * 统一观测契约：写入 root output.tool_calls，供人工检查 + 后处理
 * `_tool_call_count_from_output` 校准计数。过滤规则与 `summarizeTools` 完全一致
 * （跳过 self-evolution signal 的 exec 调用），因此返回列表长度 === toolStats.toolCallBlocks，
 * 保证 output.tool_calls.length 与 metadata.toolCallBlocks 一致。
 */
function collectToolCalls(messages) {
  const src = Array.isArray(messages) ? messages : [];
  const out = [];
  for (const msg of src) {
    if (msg?.role !== 'assistant' || !Array.isArray(msg.content)) continue;
    for (const block of msg.content) {
      if (block?.type !== 'toolCall' || typeof block.name !== 'string' || !block.name.trim()) {
        continue;
      }
      if (shouldIgnoreToolCallBlock(block)) continue;
      const name = block.name.trim();
      let args = block.arguments;
      // arguments 尽力序列化为字符串（与 OpenAI function.arguments 形态对齐）。
      if (args !== undefined && typeof args !== 'string') {
        try {
          args = JSON.stringify(args);
        } catch {
          args = String(args);
        }
      }
      out.push({
        id: typeof block.id === 'string' ? block.id : undefined,
        type: 'function',
        name,
        function: { name, arguments: args ?? '' },
      });
    }
  }
  return out;
}

function isProbablyBase64Payload(s) {
  if (typeof s !== 'string' || s.length < 256) return false;
  const sample = s.length > 4096 ? s.slice(0, 4096) : s;
  return /^[A-Za-z0-9+/=\s]+$/.test(sample);
}

function isBinaryLike(value) {
  if (value == null) return false;
  if (typeof Buffer !== 'undefined' && Buffer.isBuffer(value)) return true;
  if (value instanceof Uint8Array || value instanceof ArrayBuffer) return true;
  if (typeof value === 'object' && value.type === 'Buffer' && Array.isArray(value.data)) return true;
  return false;
}

function binaryPlaceholder(label, byteLength) {
  return { _langfuse_omitted: label, byteLength: byteLength ?? null };
}

/**
 * Deep-sanitize one value for JSON; replaces binary / huge base64 with placeholders.
 * @returns {{ value: any, truncated: boolean }}
 */
function sanitizeValueForJson(value) {
  if (value === null || value === undefined) {
    return { value, truncated: false };
  }
  const t = typeof value;
  if (t === 'string' || t === 'number' || t === 'boolean') {
    if (t === 'string' && isProbablyBase64Payload(value)) {
      return {
        value: `[omitted base64 payload, ${value.length} chars]`,
        truncated: true,
      };
    }
    return { value, truncated: false };
  }
  if (isBinaryLike(value)) {
    const len =
      typeof Buffer !== 'undefined' && Buffer.isBuffer(value)
        ? value.length
        : value instanceof ArrayBuffer
          ? value.byteLength
          : value instanceof Uint8Array
            ? value.length
            : Array.isArray(value?.data)
              ? value.data.length
              : null;
    return { value: binaryPlaceholder('binary', len), truncated: true };
  }
  if (Array.isArray(value)) {
    let truncated = false;
    const out = value.map((item) => {
      const r = sanitizeValueForJson(item);
      if (r.truncated) truncated = true;
      return r.value;
    });
    return { value: out, truncated };
  }
  if (t === 'object') {
    let truncated = false;
    const out = {};
    for (const [key, val] of Object.entries(value)) {
      if (
        (key === 'data' || key === 'image' || key === 'blob') &&
        typeof val === 'string' &&
        isProbablyBase64Payload(val)
      ) {
        out[key] = `[omitted ${key}, ${val.length} chars]`;
        truncated = true;
        continue;
      }
      if (
        val &&
        typeof val === 'object' &&
        typeof val.type === 'string' &&
        ['image', 'binary', 'file', 'audio', 'video'].includes(val.type)
      ) {
        out[key] = {
          type: val.type,
          ...binaryPlaceholder(val.type, null),
          ...(val.name ? { name: val.name } : {}),
        };
        truncated = true;
        continue;
      }
      const r = sanitizeValueForJson(val);
      if (r.truncated) truncated = true;
      out[key] = r.value;
    }
    return { value: out, truncated };
  }
  return { value: String(value), truncated: false };
}

/**
 * Full ``event.messages`` for Langfuse metadata — no global array truncation.
 * Per-message only when JSON clone fails or binary-like fields are stripped.
 */
function serializeMessagesForMetadata(messages, appendLog) {
  const src = Array.isArray(messages) ? messages : [];
  const out = [];
  let sanitizedCount = 0;

  for (let i = 0; i < src.length; i++) {
    const msg = src[i];
    let truncated = false;
    let cleaned = msg;

    try {
      cleaned = JSON.parse(JSON.stringify(msg));
    } catch (err) {
      appendLog?.(`[langfuse-tracer] message[${i}] JSON.stringify failed, sanitizing: ${String(err)}`);
      const r = sanitizeValueForJson(msg);
      cleaned = r.value;
      truncated = r.truncated;
    }

    if (!truncated) {
      const r = sanitizeValueForJson(cleaned);
      cleaned = r.value;
      truncated = r.truncated;
    }

    if (truncated) {
      sanitizedCount += 1;
      if (cleaned && typeof cleaned === 'object' && !Array.isArray(cleaned)) {
        cleaned = { ...cleaned, _langfuse_message_sanitized: true };
      }
    }
    out.push(cleaned);
  }

  let serializedChars = 0;
  try {
    serializedChars = JSON.stringify(out).length;
  } catch (err) {
    appendLog?.(`[langfuse-tracer] metadata.messages final stringify failed: ${String(err)}`);
  }

  return {
    messages: out,
    anyMessageTruncated: sanitizedCount > 0,
    sanitizedCount,
    serializedChars,
  };
}

function extractText(content, maxLen) {
  if (typeof content === 'string') {
    return content.slice(0, maxLen);
  }
  if (Array.isArray(content)) {
    return content
      .filter((c) => c?.type === 'text' && typeof c.text === 'string')
      .map((c) => c.text)
      .join('\n')
      .slice(0, maxLen);
  }
  return '';
}

function randomId() {
  return crypto.randomUUID();
}
