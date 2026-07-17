# Token 观测:5 字段落库排障

> 本文档是 [`SKILL.md`](../SKILL.md) 的深化子文档,聚焦"接入后 cache_read / reasoning 为 0 / NaN"这类落库问题。
> 接入新 runtime 时按 §"接入检查清单"提前对齐口径可以避免 90% 的坑。

LIFT 每个 runtime 都要把 5 字段 token 落到 Langfuse 且被 post-process 读回:`input_fresh + cache_write + cache_read = 完整 prompt`、`output`、`reasoning`。**任何一层断掉都会看到 `cache_read=0` / `reasoning=0` / `NaN`,且不会报错**。

---

## 0. LIFT 口径(重要,读完再排障)

**保底**:`output_tokens` 必须有值(这是评估必需)。
**尽力**:`reasoning_tokens` 能抽就抽,抽不到不算 bug。

具体关系:
- 主流 provider(OpenAI Chat Completions / Doubao / Ark) 口径:`completion_tokens = 全部 assistant 输出 = 可见文本 + reasoning`
- LIFT 内所有 runtime 的 `output_tokens` 都对齐这个口径(**含 reasoning**),再把 `reasoning_tokens` 作为独立可观测字段单独统计一份
- **`reasoning` 是 `output` 的子集,不是 sibling** —— 所以 `output + reasoning` 相加是 double count;`total = input + output + cache_read`,reasoning 不入 total(已验证)
- Langfuse dashboard 按 key 名聚合(`*output*` / `*input*`),`reasoning_tokens` 名字不含这些前缀,不会被 double sum

**推论**:
1. reasoning=0 但 output 正常 —— 合法(冷启动无 thinking / 上游 schema 不吐 reasoning 数字都可能),不影响业务
2. output=0 —— **必查**,是 A 层断了
3. 不同 runtime 之间的 output 量级不可直接比 —— OH 的 output 含 reasoning 且不再抽出,其他 runtime 的 output 也含 reasoning 但另抽了一份

---

## 1. 接入检查清单(新 runtime 必对齐)

接入新 runtime 时,按下列顺序确认 5 字段能全链路走通:

1. **provider 归一层**:确认 provider 返回的 `usage` 里 cache / reasoning 细分字段在哪(见 §3.A.2)
2. **agent 侧 accumulator**:overlay / plugin 能拿到并累加(见 §3.A)
3. **写入 Langfuse**:`usage_details=` 参数(Python SDK)或 `usageDetails` sibling(REST)必须传细分字段(见 §4.B.1)
4. **命名对齐**:使用 Anthropic 风格 key(`cache_read_input_tokens` / `cache_creation_input_tokens` / `reasoning_tokens`),Langfuse dashboard 才能按前缀聚合(见 §4.B.2)
5. **backfill 兼容**:确认 `_usage_breakdown` 能读 camelCase + snake_case 两套 key(见 §5.C.1)
6. **验证**:必跑 `integration_check.json`(至少含多 turn 任务),按 §7 三层交叉验证

---

## 2. 排障断层图(先按这张图定位在哪一层)

```
① runtime 内部:LLM 响应 → provider 归一 → agent 侧 accumulator/usage
         ↓
② runtime → Langfuse:plugin/overlay 把 usage 写进 ingestion API
         ↓
③ Langfuse 持久化:observation.usage / observation.usageDetails
         ↓
④ backfill 读回:src/report/langfuse_trace_fetch.py::_usage_breakdown()
         ↓
⑤ CSV / dashboard:src/models.py::PhaseRun.tokens
```

每一层都可能默默丢字段。**从 ① 开始,往下一层一层贴证据**,不要跳步。

详细的三层交叉验证方法见 [`three-layer-verification.md`](three-layer-verification.md)。

---

## 3. §断点 A 修复:agent 侧 accumulator 拿不到 cache_read

### A.1 判定证据

**证据文件**:plugin / overlay 的 append log。OpenClaw 走 `LANGFUSE_TRACER_LOG_FILE` 环境变量,落到 host bind mount:`results/{run_id}/outcome/**/langfuse-tracer.log`。

**判定**:找 `agent_end usage source: accumulated=...` 行

```
accumulated={"input":9789,"output":2236,"cacheRead":36464,"cacheWrite":0,"reasoning":1026,"calls":1}
```

- `cacheRead > 0` / `reasoning > 0` → **A 层 OK**,继续查 B
- `accumulated=(none)` 或 `cacheRead=0 && reasoning=0` → **A 层断**,查下方 A.1 / A.2 / A.3

### A.1.1 OpenClaw:microtask 竞争(历史根因,已修)

**症状**:`accumulated=(none)`,或 `cacheRead=0` 即便 provider 明明返回了。

**根因**:OpenClaw `runVoidHook` 内部把 handler 排到 microtask 队列。语法上 `llm_output` 先 fire、`agent_end` 后 await;但 async 函数首次调用会**立即 return pending Promise 并把 handler body 排入 microtask**,如果 agent_end handler 用 `await` 让出,handler 反而先跑,accumulator 是空的。

**修复**:agent_end handler 里让一个 macrotask,让 microtask 队列排空:

```javascript
// agent-runtimes/openclaw/plugins/langfuse-tracer/index.js
api.on('agent_end', async (event, ctx) => {
  // ... existing code ...
  await new Promise((resolve) => setImmediate(resolve));  // <-- KEY FIX
  const accumulated = pendingUsage.get(key);
  // 现在读到的 accumulated 是完整的
});
```

**验证**:手动起容器 + 3 turn 复现,`accumulated={"cacheRead":19256, "reasoning":65, ...}` ≠ `(none)`。

### A.2 provider 归一层丢字段(Hermes / GA 常见)

**取证**:直接看 provider response 原始 `usage` 或 `completion_tokens_details`

- OpenAI 家:`prompt_tokens_details.cached_tokens` / `completion_tokens_details.reasoning_tokens`
- Anthropic 家:`cache_creation_input_tokens` / `cache_read_input_tokens`
- Doubao / Ark:`usage.prompt_tokens_details.cached_tokens` / `usage.completion_tokens_details.reasoning_tokens`

如果 upstream normalize_usage 丢字段,加 `_fallback_extract_from_raw_usage` 兜底(Hermes 已修复过一次,参考 [`agent-runtimes/hermes/langfuse-hermes/__init__.py`](../../../agent-runtimes/hermes/langfuse-hermes/__init__.py))。

### A.3 overlay 覆盖不生效(GA 场景)

**症状**:改了 [`agent-runtimes/genericagent/langfuse_tracing_overlay.py`](../../../agent-runtimes/genericagent/langfuse_tracing_overlay.py) 但线上仍然拿 0。

**根因**:overlay 是构建时 `COPY` 进镜像的,**必须 rebuild** 镜像才能生效。

**修复**:改动 overlay 后重跑 `bash agent-runtimes/genericagent/build-image.sh`。

### A.4 monkey-patch 打在错误的类上(EvoScientist 场景)

**症状**:overlay 的 `_astream` wrapper 已 patch 且 `_lift_patched=True`,但 chunk 里 `usage_metadata=None`,langfuse 里 `usage_details={}`。

**根因**:runtime 内部有多套 LLM 类,直觉猜测的类不是 caller 实际用的类。

EvoScientist 案例:类名带 `OpenAICompatContentMixin` 极具迷惑性,但 `grep -rn OpenAICompatContentMixin` 只出现在 `deepseek.py` —— 是 DeepSeek/kimi 专用 mixin,`custom-openai` provider 完全不经过它。真正被建的是 langchain-openai 的 `ChatOpenAI`(经 [`models.py`](file:///root/workspace/agent_evolve_evaluation/agent-runtimes/evoscientist) `_OPENAI_ROUTED_PROVIDERS` 路由后 provider 被改写为 `openai` + 自定义 `base_url`)。

**定位方法**(三步反查):

1. **静态 grep**:`grep -rn <ProviderName>\|<ClassNameGuess>` 覆盖 runtime 源代码 —— 找出真正被继承 / 实例化的类
2. **运行时反查**:在活容器里 `inspect.getsourcefile(instance._astream)` + `type(instance).__mro__` —— MRO 给出**真实**继承链
3. **路由函数追根**:很多 runtime 有 `create_llm(provider=...)` 或 `_OPENAI_ROUTED_PROVIDERS` 之类的路由表,把用户设置的 provider "改名" 成 langchain 底层 provider —— 追进去看最终 `init_chat_model(provider=..., **kwargs)` 传的是什么

**修复**:patch 底层 langchain 基类。EvoScientist 是 `BaseChatOpenAI._astream` + `_should_stream_usage`(双管齐下强制 `stream_options.include_usage=True`),见 [langfuse_tracing_overlay.py:_patch_openai_compat_usage](file:///root/workspace/agent_evolve_evaluation/agent-runtimes/evoscientist/langfuse_tracing_overlay.py#L383-L448)。

**verify**:容器里跑 `python3 -c "from langchain_openai.chat_models.base import BaseChatOpenAI; print(getattr(BaseChatOpenAI._astream, '_lift_patched', False))"` 应为 `True`;实际请求后 `curl <langfuse>/api/public/observations?name=llm.chat` 的 `usageDetails` 应含 5 字段而非 `{}`。

---

## 4. §断点 B 修复:Langfuse 存了但字段丢失

### B.1 usage 字段承载错位(OpenClaw v5 的根因)

**症状**:插件 log 里 accumulator 明明有 `cacheRead=36464`,Langfuse observation.usage 里只剩 `{input, output, total}`。

**根因**:Langfuse `/api/public/ingestion` 的 `usage` 字段**只识别 `input / output / total / unit`**,其他 key(`cache_read_input_tokens` / `reasoning_tokens` 等 Anthropic-style 名字)**必须写在 sibling `usageDetails` 字段**,否则被静默丢弃。

**修复**:generation-create body 里 `usage` 和 `usageDetails` **同时**写:

```javascript
// agent-runtimes/openclaw/plugins/langfuse-tracer/index.js
{
  type: 'generation-create',
  body: {
    // ...
    usage,             // 承载 total 显示
    usageDetails,      // 承载 cache/reasoning 细分,必须放这里才落库
    // ...
  }
}

function usageDetailsFromUsage(usage) {
  // 复制 input / output / total / cache_read_input_tokens /
  // cache_creation_input_tokens / reasoning_tokens
}
```

Hermes / GA 用 Python SDK 通常是走 `usage_details=` 参数,SDK 内部会写到 `usageDetails` 字段,一般不会踩这个坑。但要确认它们真的传了细分字段。

### B.2 命名对齐:Anthropic-style 优先

Langfuse dashboard 靠 key 前后缀聚合:

- 含 `input` 的 key → dashboard 输入总和
- 含 `output` 的 key → dashboard 输出总和
- 用 `cache_read_input_tokens` / `cache_creation_input_tokens`(Anthropic 命名)而不是 `cache_read` / `cache_write`,可以让 cache 自动汇总到 input 总量,与业务口径一致。

---

## 5. §断点 C 修复:backfill 侧读不到

### C.1 usageDetails camelCase vs snake_case

**症状**:Langfuse observation 里明明有 `usageDetails: {cache_read_input_tokens: 36464}`,`backfilled.json` 里 `cache_read_tokens=0`。

**根因**:[`src/report/langfuse_trace_fetch.py::_usage_breakdown()`](../../../src/report/langfuse_trace_fetch.py) 只读 `usage_details`(snake_case),但 Langfuse SDK 返回的字段是 `usageDetails`(camelCase)。

**修复**:两个都读并合并:

```python
# src/report/langfuse_trace_fetch.py
details = d.get("usage_details") if isinstance(d.get("usage_details"), dict) else {}
details_camel = d.get("usageDetails") if isinstance(d.get("usageDetails"), dict) else {}
merged = {**d, **prompt_details, **completion_details, **details, **details_camel}
```

同时改 `observation_briefs` 调用侧,把 sibling 的 `usageDetails` 合到传给 `_usage_breakdown` 的 payload 里(它签名只吃单个 usage dict):

```python
usage_payload = {}
raw_usage = d.get("usage")
if isinstance(raw_usage, dict):
    usage_payload.update(raw_usage)
for details_key in ("usage_details", "usageDetails"):
    details_val = d.get(details_key)
    if isinstance(details_val, dict):
        usage_payload[details_key] = details_val
fresh, cw, cr, out_t, reasoning = _usage_breakdown(usage_payload)
```

**验证**:直接 `python -m src.cli.lift_main -r <runtime> --evaluate-only --run_id <run>` 重跑 backfill,不需要 rerun 整个 IC。

---

## 6. Runtime 状态矩阵

各 runtime 的具体病史(microtask 竞争、overlay 覆盖点、schema 缺字段等)已下沉
到各自 README 的 "Token 5 字段落库状态" 小节。本节只给横向速查:

| Runtime | 状态 | 主要关注点 | 详情 |
|---|---|---|---|
| OpenClaw / with-evolve | ✅ 全 5 字段齐 | microtask 竞争(已修) + `usageDetails` 承载(已修) + `allowConversationAccess` 前提 | [openclaw/README.md#token-5-fields](../../../agent-runtimes/openclaw/README.md#token-5-fields) |
| Hermes | ⚠️ 依赖 LIFT overlay | 上游 `normalize_usage` 在 Ark 路径丢字段;overlay 与 upstream 失同步会**静默变 0** | [hermes/README.md#token-5-fields](../../../agent-runtimes/hermes/README.md#token-5-fields) |
| GenericAgent / GA active evolve | ✅ 全 5 字段齐 | overlay wrap `_record_usage` 公共汇聚点(而非各 parser);改动后必须 rebuild 镜像 | [genericagent/README.md#token-5-fields](../../../agent-runtimes/genericagent/README.md#token-5-fields) |
| OpenHuman | ⚠️ `reasoning=0` 合规 | 上游 `MessageUsage` schema 无独立 reasoning 字段,已隐式并入 output | [openhuman/README.md#token-5-fields](../../../agent-runtimes/openhuman/README.md#token-5-fields) |
| EvoScientist / active evolve | ✅ 全 5 字段齐 | `custom-openai` 走原生 langchain-openai `ChatOpenAI` + otel autoinstrumentation,原生已能吐 usage_metadata;overlay `_should_stream_usage`/`_astream` 双管齐下强制 `stream_options.include_usage=True`,把 judge / 短 turn 的 usage 覆盖率从 44% 拉满到 100%;active 变体复用同一镜像和 overlay,只额外触发 AutoSkills evolve hook | [evoscientist/README.md#token-5-fields](../../../agent-runtimes/evoscientist/README.md#token-5-fields) |

排障时:先按 §2 断层图定位在 A/B/C 哪一层,再按 §3-§5 通用修法尝试;若怀疑
runtime 特定问题,再翻对应 README 的历史病史。

---

## 7. 常用取证脚本模板

### 7.1 dump 每 trace 的 tokens(判定 C 层)

```python
# scripts/dump_backfill_tokens.py
import json, pathlib, sys
p = pathlib.Path(sys.argv[1])  # <run_id>_backfilled.json
d = json.loads(p.read_text())
for run in d.get('runs', []):
    for suite in run.get('suites', []):
        for t in suite.get('tasks', []):
            for phase in ('baseline','evolved'):
                pr = t.get(phase, {})
                lf = pr.get('langfuse') or {}
                for i, tr in enumerate(lf.get('work_agent_traces') or []):
                    tk = tr.get('tokens') or {}
                    print(f"{t.get('task_name')} {phase} tr[{i}] "
                          f"in={tk.get('input_tokens')} out={tk.get('output_tokens')} "
                          f"cr={tk.get('cache_read_tokens')} rs={tk.get('reasoning_tokens')}")
```

### 7.2 查 Langfuse observation(判定 B 层)

```python
# scripts/fetch_trace.py
import os, json, urllib.request, base64
from dotenv import load_dotenv
load_dotenv('/root/workspace/agent_evolve_evaluation/.env')
pk = os.environ['LANGFUSE_PUBLIC_KEY']; sk = os.environ['LANGFUSE_SECRET_KEY']
base = os.environ['LANGFUSE_BASE_URL'].rstrip('/')
auth = 'Basic ' + base64.b64encode(f'{pk}:{sk}'.encode()).decode()
req = urllib.request.Request(f'{base}/api/public/traces/{TRACE_ID}', headers={'Authorization': auth})
data = json.loads(urllib.request.urlopen(req, timeout=30).read())
for obs in data['observations']:
    print(obs['name'], 'usage=', obs.get('usage'), 'usageDetails=', obs.get('usageDetails'))
```

### 7.3 grep plugin log(判定 A 层)

```bash
# OpenClaw
grep -E 'hit: llm_output|usage source' \
  results/<run_id>/outcome/run-0/evolved/<suite>/<task>/langfuse-tracer.log

# Hermes / GA:overlay/plugin 的 log 落地位置待确认(§6 补充)
```

---

## 8. 反面教训清单

1. **hello.json 冷启动 cache_read=0 是真实的**,不要因此判定"没修好"。真实验证需要 `integration_check.json` 的 H2(多 turn),第 2+ turn 才会命中 cache。
2. **rebuild 镜像要跑对 flag**:OpenClaw with evolve 用 `bash agent-runtimes/openclaw/build-image.sh --with-evolve`,漏掉 `--with-evolve` 会去测 base image。
3. **不要在 fetch API 未响应时就断言插件失效**。断网 / URL 错时插件 append log 仍会显示 accumulator 有值 —— 说明 A 层 OK,只是 B 层没通。
4. **plugin log 默认落在插件目录内(会随容器一起清理)**,必须 `LANGFUSE_TRACER_LOG_FILE` 挂到 host bind mount 才能事后诊断。
5. **backfill 用 `--evaluate-only` 重跑**,不需要重新跑 IC。仅当改动在 A / B 层(agent 或 plugin)才需要 rerun IC。
