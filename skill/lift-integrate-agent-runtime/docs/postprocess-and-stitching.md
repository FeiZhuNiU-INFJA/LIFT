# 后处理类型同步 + Langfuse Trace 拼装

> [`SKILL.md`](../SKILL.md) 的第 4、5 步深化文档。本文件覆盖:`AgentSource` 单点收敛、`_make_row_<runtime>` usage schema 分支、trace name 白名单、`stitch_phase_langfuse_traces` dispatch、dashboard tools 兜底、宿主侧 transcript push 反哺路径。
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
3. 是否需要 `_make_row_<runtime>`:见 §4.1

### 4.1 usage schema 分支(`_make_row_<runtime>`)⚠️

后处理 CSV 里 `total_tokens` / `cached_token` / `tool_use_num` 这些列由 [`src/postprocess/extract.py`](../../../src/postprocess/extract.py) 里的 `_make_row_openclaw` / `_make_row_hermes` / `_make_row_<runtime>` 组装,dispatch 在 `_make_row_side` 里按 `agent_source` 选。

**默认走 `_make_row_openclaw`**,它按 **OpenClaw transcript 的 usage schema** 累加:从 `work_analytics.all_messages[*].usage.totalTokens` 求和。

**新 runtime 何时必须新增 `_make_row_<runtime>` 分支**:

| runtime transcript 里 usage 的 key 集合 | 处理方式 |
|---|---|
| 含 `totalTokens`(OpenClaw、Hermes、GA — 因为 overlay 是 LIFT 自己写的,会强制拉齐 schema) | 复用 `_make_row_openclaw`,无需新增 |
| 不含 `totalTokens`(例:OpenHuman `{input, output, cached_input}`;上游用 OpenAI SDK 原生 usage schema `{prompt_tokens, completion_tokens, total_tokens}`) | 必须新增 `_make_row_<runtime>`,从 `global_stats.total_tokens` 取(由 Langfuse GENERATION observation `usage_details` 累加得到),不能依赖 messages 里的字段 |

**症状**:CSV / dashboard 里所有 phase `total_tokens=0`,但 `*_backfilled.json` 里 `work_analytics.global_stats.total_tokens` 有值。

**修法参考 OpenHuman**:[`extract.py::_make_row_openhuman`](../../../src/postprocess/extract.py) —

```python
def _make_row_openhuman(side, work_analytics):
    global_stats = work_analytics.get("global_stats") or {}
    total_tokens = int_value(global_stats.get("total_tokens"))
    # cached_input 也不在 OpenClaw schema,从 assistant messages usage.cached_input 累加
    cached_tokens = _aggregate_openhuman_cached_tokens(work_analytics.get("all_messages") or [])
    ...
```

同时在 `_make_row_side` 里加一个 `elif agent_source == "<runtime>": metric_row = _make_row_<runtime>(...)` 分支即可。

> **验证**:跑完 pipeline 后 `head -2 results/lift-runid-<run_id>/*_comparison_metrics.csv`,`baseline_total_tokens` / `evolved_total_tokens` 应非 0;若仍为 0,回 `*_backfilled.json` 里看 `global_stats.total_tokens` 有没有值 —— 有则新增分支,没有则查 langfuse GENERATION observation 的 `usage_details` 是否正确挂了 `{input, output, total}`(overlay/push 侧的锅,回 [`token-observability.md`](token-observability.md))。

---

## 5. Langfuse Trace 拼装(`models.py` + `langfuse_trace_stitch.py`)

### 5.1 `src/models.py:96-100` 加 trace name

[`LANGFUSE_PLUGIN_TRACE_NAMES`](../../../src/models.py#L96-L100) 元组加 `"<runtime>-plugin"`:

```python
LANGFUSE_PLUGIN_TRACE_NAMES: tuple[str, ...] = (
    "openclaw-plugin",
    "Hermes turn",
    "genericagent-plugin",
    "<runtime>-plugin",  # 新加
)
```

这个元组与 `langfuse_tracing_overlay.py:_LIFT_TRACE_NAME` 是一对镜像 — overlay 写入的 trace name 必须出现在元组里,否则 [`langfuse_trace_parse.py:15`](../../../src/report/langfuse_trace_parse.py#L15) `is_plugin_trace` 会丢掉 trace。

### 5.2 `src/report/langfuse_trace_stitch.py` 加 dispatch

`stitch_phase_langfuse_traces` 末尾按 `agent_source` 选 `_stitch_openclaw` / `_stitch_hermes`:
- 走 **OpenClaw 拼装**(基于 `session_id` 直接 list trace):runtime 输出 trace 已经写了 `session_id` = `user-*` / `judge-*` → 加进 `if agent_source in ("openclaw", ..., "<runtime>"): return _stitch_openclaw(...)`。GA 走这条。
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
- generation observation `usage_details` 只填 `{input, output, total,
  cache_read_input_tokens}`(Langfuse SDK v4 只认这四个 key)
- tool observation 挂 `as_type='tool'`(不是 `'default'`)
- **配套** §4.1:新增 `_make_row_<runtime>`,从 `global_stats.total_tokens` 累加
  (transcript-push 路线通常伴随非 `totalTokens` 的原始 schema)


