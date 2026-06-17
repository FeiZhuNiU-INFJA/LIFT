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

    const usage = aggregateAssistantUsage(turnSlice);
    const toolStats = summarizeTools(turnSlice);
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
          output: output.slice(0, 4000) || undefined,
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
          metadata: {
            durationMs,
            messageCount: safeMessages.length,
            messages: messagesPayload.messages,
            messagesTruncated: messagesPayload.anyMessageTruncated,
            messagesSanitizedCount: messagesPayload.sanitizedCount,
            messagesSerializedChars: messagesPayload.serializedChars,
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

/** OpenClaw Pi: usage uses input/output (and totalTokens); some stacks use input_tokens/output_tokens. */
function aggregateAssistantUsage(turnMessages) {
  let inputSum = 0;
  let outputSum = 0;
  let hasAny = false;
  for (const msg of turnMessages) {
    if (msg?.role !== 'assistant' || !msg.usage || typeof msg.usage !== 'object') continue;
    const u = msg.usage;
    const inp = num(u.input_tokens ?? u.input);
    const out = num(u.output_tokens ?? u.output);
    if (inp > 0 || out > 0) hasAny = true;
    inputSum += inp;
    outputSum += out;
  }
  if (!hasAny && inputSum === 0 && outputSum === 0) {
    return undefined;
  }
  return {
    input: inputSum > 0 ? inputSum : undefined,
    output: outputSum > 0 ? outputSum : undefined,
    unit: 'TOKENS',
  };
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
