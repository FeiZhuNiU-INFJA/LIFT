# 后处理类型同步 + Langfuse Trace 拼装

> [`SKILL.md`](../SKILL.md) 的第 4、5 步深化文档。本文件覆盖:`AgentSource` 单点收敛、**统一观测契约下的 runtime-agnostic 指标提取**、trace name 白名单、`stitch_phase_langfuse_traces` dispatch、dashboard tools 兜底、宿主侧 transcript push 反哺路径。
>
> **⚠️ 前置必读**:后处理的所有读法都建立在 [统一观测契约](../../../docs/langfuse-unified-observation-contract.md) 之上——每个 eval turn 一条 root span,root 的 `metadata.messages` / `metadata.toolCallBlocks` / `output.tool_calls` 都是"同 session 跨轮累积"值。**接入新 runtime 前先读那份契约**,否则看不懂为什么 token 跨轮 SUM 而工具计数跨轮 take-max。
>
> 5 字段 token 落库口径与"cache/reasoning=0 排障"见 [`token-observability.md`](token-observability.md)。

## 4. 后处理类型同步(`AgentSource` 收敛到单点)

`AgentSource` 的合法值域**唯一事实源**是 [`SUPPORTED_RUNTIMES`](../../../src/lift/adapters/registry.py#L12) tuple;后处理侧只是一个 `TypeAlias = str` 语义标记。**新增 runtime 只改 registry 一处即可**,argparse choices / dispatch tuple 全部从这里派生。

| 文件 | 作用 |
|---|---|
| [`src/lift/adapters/registry.py`](../../../src/lift/adapters/registry.py#L12) | `SUPPORTED_RUNTIMES` tuple —— **唯一事实源**,CLI `--agent-runtime` choices / argparse / dispatch tuple 全部引它 |
| [`src/postprocess/extract.py`](../../../src/postprocess/extract.py#L17) | `AgentSource: TypeAlias = str` —— 语义标记,被下游 4 处 import |
| `src/postprocess/{trace_backfill,run_post_process,report_html}.py` + `src/report/langfuse_trace_stitch.py` | 都改成 `from src.postprocess.extract import AgentSource`(stitch 内部再 alias 一次也可) |

**为什么不用 `Literal[...]`**:本仓库未接入 mypy/Pyright,Literal 的静态 narrowing 收益为 0;反倒是"5 处漂移"曾造成 `multi_user_openclaw` 后处理直接 `raise ValueError`(历史漏同步)。改成 `TypeAlias = str` + 单点 tuple 后,argparse choices / dispatch 都从 `SUPPORTED_RUNTIMES` 派生,天然一致。

### 4.0 新增 runtime 的后处理 checklist

1. `registry.py` 的 `SUPPORTED_RUNTIMES` tuple 加名字 → CLI `--agent-runtime` / `--agent-source` choices 自动包含
2. `langfuse_trace_stitch.stitch_phase_langfuse_traces` 的 dispatch 已经用 `agent_source in SUPPORTED_RUNTIMES` 兜底走 OpenClaw layout;如果新 runtime **不复用** sid-only 布局(例如 hermes 那种要 tag 才能配对),需要在这里新开一条 `if agent_source == "<runtime>"` 分支,参考 `_stitch_hermes`
3. **指标提取无需任何 runtime 分支**——见 §4.1

### 4.1 指标提取:runtime-agnostic 单函数(`_make_metric_row`)⚠️

> **历史演进**:早期每个 runtime 有各自的 `_make_row_openclaw` / `_make_row_hermes` / `_make_row_openhuman`,按 `agent_source` dispatch 处理不同的 usage schema。**统一观测契约落地后这些分支已全部删除**,合并成单个 [`_make_metric_row`](../../../src/postprocess/extract.py#L103)。接入新 runtime **不需要**再写任何 `_make_row_<runtime>`。

后处理 CSV 里 `total_tokens` / `tool_use_num` 这些列由 [`src/postprocess/extract.py`](../../../src/postprocess/extract.py) 里的 `_make_metric_row(work_analytics)` 统一组装,对所有 runtime 用同一套读法:

| 列 | 读法 | 说明 |
|---|---|---|
| `trials` | `len(work_analytics["chat_turns"])` | = 该 session 内 eval turn 数;不数 `all_messages`(context compaction 后会丢早期消息) |
| `tool_use_num` | `global_stats.tool_call_blocks`,为 0 时回退 `tool_observation_count` | 由 [`build_work_analytics`](../../../src/report/langfuse_work_analytics.py#L70) 对各轮 root span 的**跨轮累积计数取 max**(权威来源是各插件写入 root `output.tool_calls` 的长度) |
| token 5 字段 | `global_stats`(归一后) | provider 差异已在 [`_usage_breakdown`](../../../src/report/langfuse_trace_fetch.py#L140) 消化,`extract` 这层拿到的已是同构 5 字段 |

**为什么不再需要 `_make_row_<runtime>`**:曾经 OpenHuman 那种"usage schema 不含 `totalTokens`"(`{input, output, cached_input}`)要专门分支,现在这层差异**已下沉到 Langfuse 归一层** `_usage_breakdown`(用 `_first_int` 多命名兼容,例如 OpenHuman 的 `cached_input` → `cache_read_tokens`)。到 `extract.py` 时 `global_stats` 已是同构 5 字段,一把梭即可。

**接入新 runtime 时**:只要 overlay / push 侧把 usage 塞进 GENERATION 子 span 的 `usage_details`,`_usage_breakdown` 就能归一;若出现新的 provider usage key 命名,去 `_usage_breakdown` 的 `_first_int(...)` 候选名单**补一个别名**,而**不是**回去写 runtime 分支。

> **验证**:跑完 pipeline 后 `head -2 results/lift-runid-<run_id>/*_comparison_metrics.csv`,`baseline_total_tokens` / `evolved_total_tokens` / `tool_use_num` 应非 0;若 `total_tokens` 为 0,回 `*_backfilled.json` 看 `global_stats.total_tokens` 有没有值——有则是 extract 读法问题(基本不会,已 runtime-agnostic),没有则查 langfuse GENERATION observation 的 `usage_details` 是否正确挂了 `{input, output, total}`(overlay/push 侧的锅,回 [`token-observability.md`](token-observability.md))。若 `tool_use_num` 异常,按 [统一观测契约 §5](../../../docs/langfuse-unified-observation-contract.md) 的验收命令检查跨轮 take-max 一致性。

---

## 5. Langfuse Trace 拼装(`models.py` + `langfuse_trace_stitch.py`)

### 5.1 `src/models.py` 加 trace name

[`LANGFUSE_PLUGIN_TRACE_NAMES`](../../../src/models.py#L97-L103) 元组加 `"<runtime>-plugin"`:

```python
LANGFUSE_PLUGIN_TRACE_NAMES: tuple[str, ...] = (
    "openclaw-plugin",
    "Hermes turn",
    "genericagent-plugin",
    "openhuman-plugin",
    "evoscientist-plugin",
    "<runtime>-plugin",  # 新加
)
```

这个元组与 `langfuse_tracing_overlay.py:_LIFT_TRACE_NAME` 是一对镜像 — overlay 写入的 trace name 必须出现在元组里,否则 [`langfuse_trace_parse.py:15`](../../../src/report/langfuse_trace_parse.py#L15) `is_plugin_trace` 会丢掉 trace。

### 5.2 `src/report/langfuse_trace_stitch.py` 加 dispatch

`stitch_phase_langfuse_traces` 末尾按 `agent_source` 选 `_stitch_openclaw` / `_stitch_hermes`:
- 走 **OpenClaw 拼装**(基于 `session_id` 直接 list trace):runtime 输出 trace 已经写了 `session_id` = `user-*` / `judge-*` → 加进 `if agent_source in ("openclaw", ..., "<runtime>"): return _stitch_openclaw(...)`。GA / OpenHuman / EvoScientist 走这条。
- 走 **Hermes 拼装**(基于 `eval_run_tag` + 后置匹配 sid in tags):runtime 自身 SDK 不支持设 session_id,只能写 tag → 走 `_stitch_hermes`。

> **判断标准**:你的 `langfuse_tracing_overlay.py` 是否能在 root span 上设 `session_id`。能 → OpenClaw 路线(推荐,简单);不能 → Hermes 路线。

### 5.3 dashboard tools 列:tool_calls 通用兜底机制

dashboard 的 **tools 列**读 [`PhaseRun.tool_calls`](../../../src/models.py)(int / None)。这个字段有两条数据源,按下列优先级填:

| 优先级 | 数据源 | 触发位置 | 适用 runtime |
|---|---|---|---|
| 1(精确) | adapter override `count_tool_calls(env, task, result, ctx)` | holdout 题末 [`base.py:317`](../../../src/lift/adapters/base.py#L317) | OpenClaw(容器内 docker exec 读 `trajectory.jsonl`) |
| 2(兜底) | Langfuse `type=TOOL` observation 数 | 后处理 [`trace_backfill.py:55-58`](../../../src/postprocess/trace_backfill.py#L55-L58) | 任何 runtime(只要 overlay 给每次工具调用挂 `as_type='tool'` span) |

**兜底链路**(runtime-agnostic):

```
runtime overlay 每次 tool 调用 → langfuse `as_type='tool'` span (type=TOOL)
  → langfuse_trace_fetch.count_tool_observations(detail)         # 数 type=TOOL
  → LangfuseTraceRef.tool_observation_count                      # 写入 plugin trace ref
  → langfuse_trace_merge 把字段从 plugin ref 搬运到 agent ref     # _orphan_plugin_ref + merge_plugin_into_agent
  → LangfuseTokenToolStats.tool_observation_count (work-analytics 全局聚合)
  → trace_backfill.backfill_phase: phase.tool_calls=None 时填上   # 不覆盖 OpenClaw 已有的精确值
  → run_post_process.build_phase_tool_calls_from_report          # 从 backfilled JSON 抽 (r,s,task,phase)→tool_calls
  → tracker.set_phase_tool_calls(bundle)                         # 回写到 RunStateTracker,供静态 dashboard 渲染
  → dashboard tools 列显示
```

**接入新 runtime 时不需要做任何额外工作** —— 只要 `langfuse_tracing_overlay.py` 在每次工具调用 / 每个 plugin 子操作上挂了 `as_type='tool'` 的 span,dashboard 就自动有数。如果你的 runtime 能像 OpenClaw 那样从容器内拿到精确轮次(`trajectory.jsonl` 之类),可以 override `count_tool_calls` 拿到比 observation count 更稳的值;不 override 也不会显示空 — 兜底链路接住。

> **GA 注意**:GA 的 plugin 函数都是 generator(`def do_xxx(self, args, response): yield ...; return StepOutcome(...)`),如果你给 plugin 包了 langfuse decorator 但忘了 `as_type='tool'`,observation 会落到 `type=DEFAULT`,count 仍是 0。验证手段:langfuse UI 上挑一条 `genericagent-plugin` trace,展开 observation 列表,看每次 tool 调用是不是 `tool` 类型。

> **⚠️ 两个"工具数"别混淆**:dashboard tools 列读的 `PhaseRun.tool_calls` 走**本节兜底链路**——`type=TOOL` 子 span 计数(`tool_observation_count`,后处理跨轮 **SUM**);而 CSV 的 `tool_use_num`(§4.1)走 root `metadata.toolCallBlocks` / `output.tool_calls`(跨轮**累积** + 后处理 **take-max**)。两者数据源与聚合方式不同,含义上都是"这个 phase 调了多少次工具",正常情况下应当接近或相等。若你的 runtime 只挂了 root 的 `output.tool_calls` 而没挂 `as_type='tool'` 子 span,dashboard tools 列会走不到兜底而显示空,但 CSV `tool_use_num` 仍正确——**两条都要满足**才算观测完整。口径的权威定义见 [统一观测契约](../../../docs/langfuse-unified-observation-contract.md) §0/§2。

#### 5.3.1 dashboard 实时 vs 静态导出 — 为什么要回写 tracker

dashboard 实际有**两种形态**,两个都吃同一棵 `RunStateTracker.snapshot()` 状态树:

| 形态 | URL / 文件 | 数据来源 | 何时生效 |
|---|---|---|---|
| 运行期实时 | `http://<host>:<port>` | tracker `/snapshot` + SSE,跟着事件总线刷新 | run 进行中;run 结束 HTTP server 关停 |
| 静态导出 | `results/<run_id>/dashboard.html` | `build_static_dashboard_html(tracker.snapshot())` 把当前 snapshot 序列化嵌入 HTML | run 结束 + 后处理跑完,[`lift_main.py:347`](../../../src/cli/lift_main.py#L347) 自动导出 |

**关键事实**:A 路径(adapter `count_tool_calls`)是**运行期 phase 结束时**通过 `StageEvent(tool_calls=N)` 实时落 tracker;B 路径(langfuse 兜底)是**后处理阶段**才能拿到值,必须显式回写 tracker,否则:

- backfilled JSON 里有 `tool_calls=N` ✅
- 但 tracker.snapshot() 里 phase node 仍是 `tool_calls: None`
- 静态 dashboard 嵌入的 snapshot tools 列 → 显示 "—"

**回写位点**:[`run_post_process.py`](../../../src/postprocess/run_post_process.py) 在写完 backfilled JSON 后调 `build_phase_tool_calls_from_report` 抽出 `(repeat, suite, task, phase) → tool_calls` bundle,再调 `tracker.set_phase_tool_calls(bundle)`。这套机制是 runtime-agnostic 的:

- OpenClaw(A 路径):运行期 tracker 已有精确值,回写值通常等于运行期值(noop 但无害)
- GA / Hermes / 任何走兜底的 runtime:回写让静态 dashboard 第一次看到数

> **对运行期实时 dashboard 的影响**:B 路径 runtime 的 tools 列在 run 进行中**注定显示空**——langfuse 是 async upload,trace 还没完整 flush,count 不出来;要看 tools 必须等后处理跑完打开静态 dashboard。如果新 runtime 想要实时 tools,得自己 override `count_tool_calls`(容器内累加 counter / 读日志文件)。

### 5.4 上游无 Python plugin 系统时:宿主侧 transcript → push 反哺路径 ⚠️

overlay 路线的前提是 runtime 上游有 **Python plugin loader**——把
`langfuse_tracing_overlay.py` bake 进容器,让 LLM 引擎在每次 chat / tool 调用
自动 emit Langfuse span。**如果上游是 Rust / Go 二进制 / 闭源实现,或它的
Langfuse 集成走自家私有 HTTP 反代路径(LIFT 拦不到)**,overlay 走不通。

**判断标准**:

| 条件 | 走哪条路径 |
|---|---|
| 上游有 Python plugin loader + 能在 root span 设 `session_id` | overlay 路线(§5.3 + §5.1 dispatch,推荐) |
| 上游是 Python 但 SDK 不允许自设 session_id,只能写 tag | overlay + Hermes 拼装(见 §5.2) |
| 上游是二进制 / 私有 Langfuse 反代 / **完全没有 hook 点** | **transcript → push 反哺**(本节) |

**核心思路**:

1. runtime 侧只需要落 transcript——warmup / holdout 阶段 agent 把每轮 chat 的
   完整对话(system / user / assistant / tool)以 JSONL 落容器某处。绝大多数 agent
   上游都自带这能力,无需改上游。
2. LIFT adapter 在 `chat()` 返回前 hook:`docker exec cat` transcript → 宿主解析
   → Langfuse SDK v4 直接 push 一条 `<runtime>-plugin` root trace,把 iteration /
   tool call 挂成 GENERATION / TOOL observation。
3. push 的 metadata 必须与 [`LangfusePluginTraceMetadata`](../../../src/models.py#L122-L171)
   对齐,否则 backfill 时 `is_plugin_trace` 拿不到值。

**参考实现**:OpenHuman(`openhuman-core` 是 Rust 二进制,自家 Langfuse 走
`/telemetry/langfuse/ingestion` proxy 到上游账号,LIFT 拦不到)。约束清单、
hook 位点、异步 push 骨架、验收命令等**全部细节**已下沉到
[agent-runtimes/openhuman/README.md#transcript-push](../../../agent-runtimes/openhuman/README.md#transcript-push)。

新 runtime 若也无 hook 点,参照 OpenHuman 结构:
- adapter `worker_judger_factory` 透传 `ctx.run_id → run_tag`
- chat_agent 用 `asyncio.to_thread(push_<runtime>_plugin_trace_safe, …)` 异步 push
- 每轮 chat 后**全量重读** transcript(OpenHuman 每次 chat 重读所有 `session_raw/*.jsonl`),
  这样 root `metadata.messages` / `toolCallBlocks` / `output.tool_calls` 天然是"截至当前轮的
  累积值",满足 [统一观测契约](../../../docs/langfuse-unified-observation-contract.md) §1 规则 B/C
- root `output` 携带跨轮累积的完整 `tool_calls` 列表(`{content, tool_calls:[...]}`),
  且 `len(output.tool_calls) == metadata.toolCallBlocks`(同源:从 assistant 消息的 `tool_calls`
  同处累加,含 subagent 调用)
- generation observation `usage_details` 只填 `{input, output, total,
  cache_read_input_tokens}`(Langfuse SDK v4 只认这四个 key)—— provider 原始 schema
  (如 OpenHuman `cached_input`)由后处理 `_usage_breakdown` 归一,**无需**再写 `_make_row_<runtime>`
- tool observation 挂 `as_type='tool'`(不是 `'default'`),让 dashboard tools 列的兜底链路(§5.3)有数

