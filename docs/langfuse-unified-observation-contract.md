# LIFT 统一观测契约（Langfuse Unified Observation Contract）

> **一句话**：所有 agent runtime 的 Langfuse 插件 trace 必须遵循同一套口径 —— **每个 eval
> turn 一条 root span；root span 上的 `metadata.messages` / `metadata.toolCallBlocks` /
> `output.tool_calls` 三者都是"同一个 session 内、跨越所有历史 eval turn 的累积值"**。后处理
> 侧因此 **runtime-agnostic**：token 5 字段跨轮 **SUM**，工具计数跨轮 **take-max**，不再有任何
> `_make_row_<runtime>` / `if agent_source == ...` 分叉。
>
> 本文件是 [`skill/lift-integrate-agent-runtime`](../skill/lift-integrate-agent-runtime/SKILL.md)
> 第 4 步（后处理 + trace 拼装）与镜像 overlay（第 1 步）共同依赖的**权威口径定义**。
> 源码里 `src/postprocess/extract.py`、`src/report/langfuse_trace_fetch.py`、
> `src/report/langfuse_work_analytics.py` 的 docstring 都反向引用本文件。

---

## 0. 名词与坐标系（先对齐"轮"的定义）

集成时最容易踩的坑是把"轮"混为一谈。LIFT 里有**三个不同粒度的"轮"**，务必分清：

| 名词 | 含义 | 谁产生 |
|---|---|---|
| **eval turn** | LIFT work↔judge 循环里的一次 `chat()` 调用（judge 让 work 重做一次 = 多一个 eval turn） | `src/lift/eval/run_task.py` |
| **root span** | Langfuse 上一条 `as_type='agent'` 的顶层 observation | 各 runtime 的 overlay / 宿主 push |
| **LLM iteration** | runtime 内部一次 agent loop 里的单次 LLM 请求（一个 eval turn 内可能有几十次） | runtime 上游 |

**契约的核心映射**：

```
1 个 session（work-<sid> 或 judge-<sid>）
   = N 个 eval turn（judge 复跑会增加）
   = N 条 root span（每个 eval turn 落 1 条）
        每条 root span 底下挂当轮的子 span：
          - GENERATION 子 span：当轮每次 LLM iteration 的 usage（**当轮增量**）
          - TOOL 子 span（可选）：当轮每次工具调用（**当轮增量**）
        但 root span 自己的三个累积字段是【跨所有历史 eval turn 累积】：
          - metadata.messages        —— 截至当前 turn 的全量 transcript
          - metadata.toolCallBlocks  —— 截至当前 turn 的累积工具调用总数
          - output.tool_calls        —— 截至当前 turn 的累积工具调用完整列表
```

一句话记牢：**子 span 记"当轮"，root span 记"累积"。** 这个非对称是整个契约的关键。

---

## 1. 三条必守规则（root span 上的字段）

### 规则 A：每个 eval turn 落一条 root span，绝不合并成"整 session 一条"

- 容器会被 `docker rm -f`（SIGKILL）强杀，`atexit` / 信号 handler **不执行**。若把整个
  session 做成"进程启动建一条 root span、退出才 end"，则 span 永远 flush 不出去，Langfuse 上
  Input / Output 全是 `undefined`。
- 因此**每个 eval turn 结束时同步 `__exit__` + `flush`**，保证每一轮都是一条**完整落库**的 trace。

### 规则 B：`metadata.messages` = 同 session 跨轮累积的全量 transcript

- 第 N 轮的 root span 里，`messages` 必须包含**从第 1 轮到第 N 轮**的所有对话
  （system 可省 / user / assistant + `tool_calls` / tool 结果）。
- 归一化成最小形状 `{role, content, tool_calls?}`（OpenAI 风格
  `tool_calls=[{id,type,function:{name,arguments}}]`），让后处理 / dashboard 统一识别。
- **子 span（GENERATION）不放 messages**（严格模式）。全量 transcript 只在 root
  `metadata.messages` 一处，避免双写与歧义。
- 后处理 `TranscriptChampion` 按 timestamp **取最晚一条** work transcript，因此"最后一轮
  root span 天然含整段会话"就是正确结果。

### 规则 C：`toolCallBlocks` 与 `output.tool_calls` 同口径、同源，都是跨轮累积

这是**用户反复强调、必须严格遵守的原则**：

1. `metadata.toolCallBlocks`（int）与 `output.tool_calls`（list）都表示"截至当前 eval turn，
   这个 session 内累积发生的全部工具调用"，二者 **`len(output.tool_calls) == toolCallBlocks`**
   恒成立。
2. 二者必须**同源**：从**每一次 LLM 响应里独立计数**并 append 到累积器，**不要**从
   `messages`/transcript 反向数 —— transcript 可能被上下文压缩（context compaction）截断，
   反向数会漏掉早期调用。
3. `output.tool_calls` 里放**完整的调用列表**（含子 agent / subagent 的调用），形态与 Hermes
   root output 对齐（`{content, tool_calls:[...]}`），方便用户**直接在报告 / Langfuse UI 里检查**
   本次评测到底调了哪些工具。
4. `output` 形态：有工具调用时写 `{"content": <最终回复>, "tool_calls": [...]}`；无工具调用时
   可退化为纯文本 `output=<最终回复>`。

> **为什么用累积而不是当轮增量**：后处理对工具计数走 **take-max**（见 §2）。累积值天然单调不减，
> max = 最末轮累积值 = session 总量，且对 trace 时间排序抖动、provider 重试导致的配对缺口
> （某轮 `toolCallBlocks=0`）都健壮。若 root 只放当轮增量，后处理就必须 SUM，一旦某轮 trace 因
> 重试重复上报就会翻倍高估 —— 这正是旧口径踩过的坑。

---

## 2. 后处理侧的读法（runtime-agnostic，不再分叉）

统一契约让后处理彻底摆脱 `agent_source` 特判。三层聚合口径如下：

### 2.1 单条 trace：工具计数权威来源 = `output.tool_calls` 长度

[`langfuse_trace_fetch._tool_call_count_from_output`](../src/report/langfuse_trace_fetch.py#L232)
从 root `output.tool_calls` 读长度，用它**校准** `plugin_metadata.tool_call_blocks`
（以 output 为准兜底 metadata 缺失 / 偏差）。返回 `None` 表示该 runtime 没写 output.tool_calls，
回退到 `metadata.toolCallBlocks`。

### 2.2 跨 eval turn 聚合：token SUM、工具 take-max

[`langfuse_work_analytics.build_work_analytics`](../src/report/langfuse_work_analytics.py#L70)
遍历一个 session 的各 root span（各 eval turn），按字段语义分两类聚合：

| 字段类别 | 聚合方式 | 原因 |
|---|---|---|
| token 5 字段（input / cache_write / cache_read / output / reasoning） | **SUM** | GENERATION 子 span 记的是**当轮增量**，跨轮相加才是 session 总量 |
| `tool_observation_count`（TOOL 子 span 计数） | **SUM** | TOOL 子 span 也是当轮增量 |
| `tool_call_blocks` / `tool_roundtrips`（root metadata） | **take-max** | root 字段是**跨轮累积**，max = 最末轮累积值，SUM 会重复相加爆炸性高估 |

```python
# token：子 span 当轮增量 → 跨轮 SUM
g.input_tokens += t.stats.input_tokens
...
g.tool_observation_count += t.stats.tool_observation_count
# root 累积字段 → 跨轮 take-max
g.tool_call_blocks = max(g.tool_call_blocks, t.stats.tool_call_blocks)
g.tool_roundtrips = max(g.tool_roundtrips, t.stats.tool_roundtrips)
```

### 2.3 CSV / dashboard 出行：`_make_metric_row` 单函数

[`extract._make_metric_row`](../src/postprocess/extract.py#L103) 对所有 runtime 用同一套读法，
**没有** `_make_row_openclaw` / `_make_row_hermes` / `_make_row_openhuman` 之类分支了：

- `tool_use_num` ← `global_stats.tool_call_blocks`（缺失/0 时回退 `tool_observation_count`）
- token 5 字段 ← `global_stats`（provider 差异已在 `_usage_breakdown` 归一层消化，见
  [`token-observability.md`](../skill/lift-integrate-agent-runtime/docs/token-observability.md)）
- `trials` ← `len(chat_turns)`

> `make_row(...)` 仍保留 `agent_source` 形参，但**仅供下游 `report_html` 调整展示列**
> （如 Hermes 无 per-turn latency），指标提取本身不消费它。

---

## 3. Token 归一：为什么不再需要 `_make_row_<runtime>`

历史上 OpenHuman 那种"usage schema 不含 `totalTokens`"（`{input, output, cached_input}`）需要专门
的 `_make_row_openhuman` 从 `global_stats` 读。现在这层差异**已下沉到 Langfuse 归一层**
[`langfuse_trace_fetch._usage_breakdown`](../src/report/langfuse_trace_fetch.py#L140)：它用
`_first_int` 多命名兼容读取，把各家 provider 的 usage key 全部归一成 5 字段（例如 OpenHuman 的
`cached_input` → `cache_read_tokens`）。所以到了 `extract.py` 这一层，所有 runtime 的
`global_stats` 已经是同构 5 字段，`_make_metric_row` 一把梭即可。

**接入新 runtime 时**：只要 overlay / push 侧把 usage 塞进 GENERATION 子 span 的 `usage_details`
（Langfuse SDK v4 只认 `{input, output, total, cache_read_input_tokens}` 这几个 key），
`_usage_breakdown` 就能归一。若出现新的 provider usage key 命名，去 `_usage_breakdown` 的
`_first_int(...)` 候选名单里补一个别名，而**不是**回去写 runtime 分支。

---

## 4. 各 runtime 如何满足契约（累积机制差异表）

契约是统一的，但**"累积在哪里发生"随进程模型不同**。接入前先判断新 runtime 属于哪一类：

| runtime | 进程模型 | 累积发生在哪 | 参考实现 |
|---|---|---|---|
| **Hermes** | 常驻 runner | module 级 session 累积器 | [`agent-runtimes/hermes/langfuse-hermes/__init__.py`](../agent-runtimes/hermes/langfuse-hermes/__init__.py)（**标杆**） |
| **OpenClaw** | 每 eval turn 一次 `agent_end` → 一条 root trace | 上游 `safeMessages` 本就是累积全量历史；overlay 直接 `collectToolCalls(safeMessages)` | [`plugins/langfuse-tracer/index.js`](../agent-runtimes/openclaw/plugins/langfuse-tracer/index.js) `collectToolCalls` |
| **GenericAgent** | 首轮 `docker exec -d` 起常驻进程，后续写 `reply.txt` 复用同进程 | **进程级 `_STATE`**（`tool_calls` / `tool_call_blocks` 从每次 LLM 响应独立累加） | [`agent-runtimes/genericagent/langfuse_tracing_overlay.py`](../agent-runtimes/genericagent/langfuse_tracing_overlay.py) `_STATE` |
| **EvoScientist** | **每 eval turn 是独立 `EvoSci -p` OS 进程**（stateless 单发，`--resume` 续接） | **session_id 键控的状态文件**（`/tmp/lift_evosci_tc_<sid>.json`，原子写跨进程累积） | [`agent-runtimes/evoscientist/langfuse_tracing_overlay.py`](../agent-runtimes/evoscientist/langfuse_tracing_overlay.py) `_load/_save_cumulative_tool_calls` |
| **OpenHuman** | 宿主侧 push，每轮全量重读 `session_raw/*.jsonl` | **天然累积**（每次 chat 后重读所有 jsonl，metadata.messages / toolCallBlocks 本就是截至当前的全量） | [`src/lift/adapters/openhuman/transcript_langfuse.py`](../src/lift/adapters/openhuman/transcript_langfuse.py) `summarize_transcripts` |

**决策树**：

```
新 runtime 每个 eval turn 是不是同一个进程？
├── 是（常驻进程 / 长连接）        → 进程级累积器（GA _STATE / Hermes module 级）
├── 否，但每轮全量重读某处 transcript → 天然累积（OpenHuman，无需额外状态）
└── 否，每轮是独立短命进程         → session_id 键控的磁盘状态文件跨进程累积（EvoScientist）
```

无论哪一类，最终写进 root span 的 `toolCallBlocks` / `output.tool_calls` 必须满足 §1 规则 C
（跨轮累积、同源、`len == toolCallBlocks`）。

---

## 5. 验收：一条命令确认口径正确

跑一个 **≥2 eval turn** 的 suite（推荐 `assets/benchmarks_demo/information_search_gathering.json`
+ `--max-conversation-turns 3`；judge 复跑天然凑出多轮），后处理完成后查 backfilled JSON。

**必须同时满足两条**：

1. **per-turn `tool_call_blocks` 单调不减**（跨 eval turn 累积）。
2. **`global_stats.tool_call_blocks` == max(per-turn 值) == CSV `tool_use_num`**（take-max 一致）。

```bash
RID=lift-runid-<run_id>
JSON=results/$RID/${RID}_backfilled.json
python3 -c "
import json
d = json.load(open('$JSON', encoding='utf-8'))
for rp in d['runs']:
    for s in rp['suites']:
        for t in s['tasks']:
            for ph in ('baseline','evolved'):
                wa = (t[ph].get('langfuse') or {}).get('work_analytics') or {}
                per = [ (c.get('stats') or {}).get('tool_call_blocks') for c in (wa.get('chat_turns') or []) ]
                g   = (wa.get('global_stats') or {}).get('tool_call_blocks')
                mono = all(a is None or b is None or b>=a for a,b in zip(per,per[1:]))
                ok   = (g == max([x for x in per if x is not None] or [0]))
                print(f'{t[\"task_name\"]:5} {ph:8} per_turn={per} global={g} monotonic={mono} take_max_ok={ok}')
"
```

> **对 root `output.tool_calls` 的额外检查**：若测试在后处理前被中断（没有 backfilled JSON），可趁
> 容器还活着把 transcript / trajectory 快照到宿主，用该 runtime 的 summarize / collect 逻辑离线跑
> 一遍，断言 `len(output.tool_calls) == tool_call_blocks` 且跨轮单调递增（OpenHuman 集成时即这样
> 验证过：turn 1/2/3 = 26/36/47，len 与 blocks 完全一致）。

---

## 6. 常见反例（照着排查）

| 症状 | 根因 | 修法 |
|---|---|---|
| `tool_use_num` 约等于实际值 × 轮次（虚高） | root 写了**当轮增量**却被后处理当累积 take-max —— 或旧口径下写累积却被 SUM | 统一成 §1 规则 C：root 写**跨轮累积**，后处理 take-max |
| 某中间轮 `toolCallBlocks=0` / `None`，但结果仍正确 | provider 重试导致该轮 trace 配对缺口（`plugin_name=None`），**不是**累积 bug | take-max 对此天然健壮（`max(13,0,15)=15`），无需处理；这正是选 max 而非"取最末轮"的原因 |
| `len(output.tool_calls) != toolCallBlocks` | 二者不同源：一个从 LLM 响应数、一个从 transcript 反数 | 强制同源：同一处 `calls` 既 `blocks += len(calls)` 又 `tool_calls.extend(normalize(calls))` |
| Langfuse 上该 session 只有 1 条 trace / Input-Output 是 `undefined` | 把整 session 做成一条 root span，靠 atexit 收尾但被 SIGKILL | §1 规则 A：每个 eval turn 同步 `__exit__` + `flush` |
| CSV `total_tokens=0` 但 `global_stats.total_tokens` 有值 | GENERATION 子 span 没挂 `usage_details`，或 provider 新 usage key 没进 `_usage_breakdown` 候选名单 | 补 overlay 的 `usage_details`；或在 `_first_int` 候选名单加别名（**不要**写 runtime 分支） |

---

## 7. 关联文档

| 文档 | 关系 |
|---|---|
| [`skill/lift-integrate-agent-runtime/docs/postprocess-and-stitching.md`](../skill/lift-integrate-agent-runtime/docs/postprocess-and-stitching.md) | 后处理 + trace 拼装的操作指南；§4/§5 引用本契约 |
| [`skill/lift-integrate-agent-runtime/docs/image-scaffold.md`](../skill/lift-integrate-agent-runtime/docs/image-scaffold.md) | overlay 脚手架；§3.1 多轮 root span 生命周期引用本契约规则 A/C |
| [`skill/lift-integrate-agent-runtime/docs/token-observability.md`](../skill/lift-integrate-agent-runtime/docs/token-observability.md) | token 5 字段口径 + `_usage_breakdown` 归一层 |
| [`docs/release-notes/2026-07-16-token-5-fields-observability.md`](release-notes/2026-07-16-token-5-fields-observability.md) | token 5 字段治理的历史叙事 |
