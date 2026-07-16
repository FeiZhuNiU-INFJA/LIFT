# 2026-07-16 · Token 5 字段落库全链路修复

## TL;DR

修复了 4 个 runtime 在 Langfuse 上 `cache_read` / `reasoning` 全部落 0 的问题,
把 LIFT 的 token 观测口径统一为 **5 字段**(`input_fresh` / `cache_write` /
`cache_read` / `output` / `reasoning`)。修复涉及 **plugin/overlay 层、Langfuse
ingestion 契约、backfill 反序列化**三层,并把知识固化到
[`skill/lift-integrate-agent-runtime/docs/token-observability.md`](../../skill/lift-integrate-agent-runtime/docs/token-observability.md)
与 4 个 runtime README。

## 背景

排查 evolve 收益时发现 CSV 里 `cache_read_tokens` / `reasoning_tokens` **对所有
runtime 全部为 0**,与 provider(Ark / Doubao)后台的真实计费数据不符。初步
grep 显示:

- Langfuse dashboard 里 `observation.usage` 只写了 `{input, output, total}`
- `_backfilled.json` 里对应字段也是 0

**代价**:LIFT 的核心指标 `impr_total_tokens` 会低估 cache 开销、忽略 reasoning
成本,横向对比失去意义。

## 断层分析

问题不在一个点,而是**三层同时都有洞**。按数据流从上到下:

### Layer A · Agent 侧 accumulator 拿不到 cache_read

- **OpenClaw**:插件 `langfuse-tracer` 的 `agent_end` handler 在 `runVoidHook`
  内被排到 **microtask 队列**,与 `llm_output` handler 存在竞态 —— `agent_end`
  读 accumulator 时,`llm_output` 还没把这一轮的 usage 写进去
- **GenericAgent**:overlay 的 `langfuse_tracing_overlay.py` 只 wrap 了 SSE
  parser (`_parse_claude_sse` / `_parse_openai_sse`),non-stream 路径
  (`sess.stream=False`) 完全没被拦截 → `_tls._usage=None`
- **Hermes**:上游 `agent.usage_pricing.normalize_usage` 在 OpenAI-compatible /
  Ark 路径下**丢弃** `prompt_tokens_details.cached_tokens` 与
  `completion_tokens_details.reasoning_tokens`

### Layer B · Langfuse ingestion 承载错位

即便 Layer A 拿到了数据,`generation-create` body 的 **`usage` 字段只识别
`input / output / total / unit`**,其他 key(`cache_read_input_tokens`、
`reasoning_tokens` 等 Anthropic-style 名字)必须写在 sibling **`usageDetails`**
字段,否则被 Langfuse 静默丢弃 —— 这是 REST API 的隐藏契约。

### Layer C · Backfill 侧 camelCase 兼容缺失

Langfuse Python SDK 返回的字段名是 **`usageDetails` (camelCase)**,而
[`src/report/langfuse_trace_fetch.py::_usage_breakdown()`](../../src/report/langfuse_trace_fetch.py)
只读 **`usage_details` (snake_case)** → 即使 Layer B 写对了,backfill 也认不出。

## 修复策略

### Layer A 各 runtime 差异化修复

| Runtime | 根因 | 修复要点 |
|---|---|---|
| OpenClaw | microtask 竞态 | agent_end handler 里 `await new Promise(r => setImmediate(r))` 强行让一个 macrotask,排空 microtask 队列 |
| GenericAgent | overlay 只覆盖 stream 路径 | 直接 wrap llmcore 公共汇聚点 `_record_usage(usage, api_mode)` —— 该函数被 messages / chat_completions / responses 3 种 api_mode 的 SSE + JSON parser 共 7 处调用,比 wrap 所有 parser 更根本 |
| Hermes | 上游 normalize_usage 丢字段 | 加 `_fallback_extract_from_raw_usage()` 直接从 raw usage 兜底提取,取 `canonical.cache_read_tokens or fb_cache_read` |
| OpenHuman | 上游 schema 无 reasoning | 上游 `MessageUsage` 只有 `{input, output, cached_input}`,reasoning 隐式并入 output —— 决定**不改**,声明 `reasoning=0` 合规 |

**关键取舍 · OpenClaw 为什么用 `setImmediate` 不用锁**:

microtask 竞态本质是 handler 执行顺序问题,加锁会让 `agent_end` 阻塞等 `llm_output`,
反而放大延迟且需要修改 OpenClaw runtime 契约。`setImmediate` 只让一次 macrotask
(< 1ms),让原有的 microtask 队列自然排完,是最小侵入。

**关键取舍 · GenericAgent 为什么 wrap `_record_usage` 不 wrap 各 parser**:

wrap parser 需要枚举 6 处(3 api_mode × stream/non-stream),GA 未来加新 api_mode
时会漏。wrap `_record_usage` 是 GA 内部的**单一 usage 汇聚点**,一次拦截兜住所有
未来路径。

### Layer B · usage + usageDetails 双写

所有 runtime 的 plugin/overlay 写入 Langfuse 时:

```
{
  usage: { input, output, total, unit },        // Langfuse 只认这几个
  usageDetails: {                                // 细分字段的家
    input, output,
    cache_read_input_tokens,
    cache_creation_input_tokens,
    reasoning_tokens
  }
}
```

**命名对齐**:使用 Anthropic 风格 key(`*_input_tokens`),让 Langfuse dashboard
按前缀聚合时,cache tokens 能自动汇入 input 总量,与业务口径一致。

### Layer C · backfill 同时读 camelCase + snake_case

[`langfuse_trace_fetch.py`](../../src/report/langfuse_trace_fetch.py) 里
`_usage_breakdown()` 现在合并两套 key:

```python
details = d.get("usage_details") if isinstance(d.get("usage_details"), dict) else {}
details_camel = d.get("usageDetails") if isinstance(d.get("usageDetails"), dict) else {}
merged = {**d, **prompt_details, **completion_details, **details, **details_camel}
```

**关键**:调用侧也要把 sibling `usageDetails` 手动合到 payload 里(签名只吃单个
usage dict),否则 SDK 平铺出来后 dict 上没有嵌套。

## 涉及文件

**Agent 侧**:
- [agent-runtimes/openclaw/plugins/langfuse-tracer/index.js](../../agent-runtimes/openclaw/plugins/langfuse-tracer/index.js) — `setImmediate` 修复 + `usageDetailsFromUsage`
- [agent-runtimes/genericagent/langfuse_tracing_overlay.py](../../agent-runtimes/genericagent/langfuse_tracing_overlay.py) — wrap `_record_usage`
- [agent-runtimes/hermes/langfuse-hermes/__init__.py](../../agent-runtimes/hermes/langfuse-hermes/__init__.py) — `_fallback_extract_from_raw_usage`

**LIFT 内核侧**:
- [src/report/langfuse_trace_fetch.py](../../src/report/langfuse_trace_fetch.py) — camelCase 兼容 + sibling merge
- [src/lift/adapters/openclaw/session.py](../../src/lift/adapters/openclaw/session.py) — `LANGFUSE_TRACER_LOG_FILE` 挂到 bind mount

**文档 / 知识固化**:
- [skill/lift-integrate-agent-runtime/docs/token-observability.md](../../skill/lift-integrate-agent-runtime/docs/token-observability.md) — 5 层断层图 + 通用修复方案(§0–§5)+ runtime 状态矩阵(§6)
- [skill/lift-integrate-agent-runtime/docs/acceptance-checklist.md](../../skill/lift-integrate-agent-runtime/docs/acceptance-checklist.md) — 5 字段审计检查
- 4 个 runtime README 加 "Token 5 字段落库状态" 小节:
  - [openclaw](../../agent-runtimes/openclaw/README.md#token-5-fields)
  - [genericagent](../../agent-runtimes/genericagent/README.md#token-5-fields)
  - [hermes](../../agent-runtimes/hermes/README.md#token-5-fields)
  - [openhuman](../../agent-runtimes/openhuman/README.md#token-5-fields)

## 后续与监控

### 未来退化风险

1. **Hermes overlay 与上游脱钩**:LIFT 版 `langfuse-hermes/__init__.py` 是构建
   期覆盖上游 `observability/langfuse` 的。升 base image tag(`HERMES_BASE_IMAGE_TAG`)
   时,上游可能改 hook 名 / 参数签名,导致 overlay 静默失效
2. **OpenClaw plugin API 变更**:`runVoidHook` 语义若上游改成 sync,`setImmediate`
   fix 会变成 no-op(虽然仍然安全,但没必要)
3. **Langfuse ingestion 契约变化**:REST API 上不承诺 `usageDetails` 语义,升
   Langfuse server 时需盯

### Sanity-check 方法

每次升级 base image / plugin / Langfuse 后跑一次:

```bash
python -m src.cli.lift_main -r <runtime> \
  --benchmark_dir assets/benchmarks_demo --suite integration_check.json \
  --run_id ic-<runtime>-$(date +%Y%m%d)
```

跑完检查 `results/<run_id>/_comparison_metrics.csv`:`cache_read_tokens` /
`reasoning_tokens` **不能全为 0**(hello.json 的单 turn 是允许 0 的,必须
integration_check 的多 turn)。

详细步骤见
[`skill/lift-integrate-agent-runtime/docs/three-layer-verification.md`](../../skill/lift-integrate-agent-runtime/docs/three-layer-verification.md)。

### 后续待办

- [ ] 观察 Hermes full run(`hermes-full-r1-*`)结果,验证 fallback 兜底在
  benchmark 全量场景下的字段覆盖率
- [ ] 考虑给 OpenHuman 上游提 PR,在 `MessageUsage` schema 加
  `reasoning_tokens` 字段,以摆脱 "reasoning=0 合规" 的现状
