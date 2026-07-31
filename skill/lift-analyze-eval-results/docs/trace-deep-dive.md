# Trace 深挖取证指南

当出现需要**归因**的情形（good/bad 差但 profound 好、trials/tool_use_num 显著变化、怀疑 skill 过拟合或系统性报错），要从产物里下沉到工具调用与 skill 内容层面取证。本文件说明**去哪读、读什么、怎么判**。

---

## 1. 两条取证路径

### 路径 A：HTML 轨迹图（快速目视）

`{run_id}_metrics_report.html` 每个任务行下有"Show evolved / Show baseline trajectory"按钮，展开是 snake-layout 流程图：
- 节点色：蓝=user、绿=assistant、橙=tool。
- 点节点看 reasoning / content / tool arguments。
- **并排展开 evolved 与 baseline**，直接目视对比"同一任务两次的工具链形态差异"。

适合：快速看出"evolved 是不是工具链更短 / 换了工具 / 多了报错循环"。

### 路径 B：backfilled JSON（逐字取证，权威）

`{run_id}_backfilled.json`。定位路径：

```
runs[r].suites[s].tasks[t].{baseline|evolved}
  ├── success / content_score / turns / tool_calls / workspace_dir
  └── langfuse.work_analytics
        ├── all_messages      # 完整对话消息数组（取证主力）
        ├── trace_chain[]     # 每轮 {turn_index, input, output, latency_seconds}
        ├── chat_turns[]      # eval 轮（trials = 其长度）
        └── global_stats      # 5 字段 token + tool_call_blocks / tool_observation_count
```

> HTML 轨迹图正是从 `all_messages` 渲染的（`report_html.py::build_trajectory_nodes`）。要看**原文**（skill 正文、报错全文、参数全值）就读 backfilled JSON，HTML 只是可视化。

---

## 2. `all_messages` 消息结构（怎么读）

数组元素按 role 分（实测于 OpenClaw，其它 runtime 契约一致）：

- **`role: "user"`**：任务 prompt / judge 回传。`content` 可为字符串或 `[{type:"text", text:...}]`。
- **`role: "assistant"`**：`content` 是块数组，块 `type` ∈：
  - `text`：给用户的话；
  - `thinking`：推理（`thinking` 字段）；
  - `toolCall`：**工具调用**，字段 `name`（工具名）、`arguments`（参数对象）、`id`。
- **`role: "toolResult"` / `"tool"`**：**工具返回**（HTML 轨迹图会 drop 这类节点，但取证时**必须读**——系统报错都在这里）。

示例（assistant 发起的一次工具调用块）：

```json
{ "type": "toolCall", "id": "call...", "name": "read",
  "arguments": { "path": "/root/.openclaw/workspace/q5_materials/final_eval_context.md" } }
```

> 注意 OpenClaw 自进化 signal（`exec` 命令含 `http://127.0.0.1:18090`）在指标里被过滤、也不进轨迹图；取证时看到可忽略，别当成任务工具调用。

---

## 3. 四类取证点（对应 SKILL Step 4）

### 3.1 工具调用正确性
逐个 `toolCall` 看 `name` + `arguments`：工具选得对不对、路径/参数对不对、有没有明显调错（如该 read 却 write、路径拼错）。evolved vs baseline 对比：evolved 是否更精准。

### 3.2 工具返回的系统 / 环境错误
在 `toolResult` / `tool` 返回里搜：`Error` / `error` / `Traceback` / `not found` / `No such file` / `permission denied` / `timeout` / `command not found` / 非零退出。
- **大量系统性报错**会拖垮全局 good/bad（多数任务失败），却可能不影响个别侥幸成功任务的 profound——这正是"good/bad 差但 profound 好"的常见根因之一。
- 区分：**环境/系统错误**（不该归咎于进化，属评测环境问题）vs **agent 用错工具导致的错误**（属进化质量问题）。报告要分开说。

### 3.3 skill 调用是否精准
看 evolved 是否在**不该用的任务类型**上加载 / 套用 warmup 学到的 skill（trace 里 read / load skill 文件，或调用 skill 相关工具）。用错场景 = 泛化差。

### 3.4 skill 内容是否合理、是否与 content 要求相悖（过拟合判定）
从 trace 里 read 出的 **skill 正文**判断：
- skill 是否把**某个特定 warmup 任务的答案 / 套路写死**？
- 在通用 / 其它 holdout 任务上照搬时，是否与该任务的 **content 要求相悖**（如任务要 A 方案，skill 硬塞 warmup 的 B 方案）？

若是 → **判定 skill 过拟合**：进化"学偏了"，把特例当通则。这是必须点名的问题，即使个别任务因此 profound，也要指出其代价（全局退化）。

> 交叉核对 `outcome/run-{i}/{phase}/{suite}/{task}/` 下 evolved 实际写出的产物，看内容是否真满足 content 要求，而非只看 judge 分。

---

## 4. trials / tool_use_num 变化的特征提取（Step 5）

对比 evolved vs baseline 的 `all_messages`，提取**结构性变化**：

| 观察到的变化 | 通常判定 | 佐证方式 |
|---|---|---|
| 多步工具链整合为更少组合（如 3×read+grep → 1×grep 定位） | 好：更高效 | 数两侧 toolCall 序列 [证据] |
| 换用更合适的工具（裸 shell 拼接 → 专用检索/编辑工具） | 好：更精准 | 对比工具 name [证据] |
| tool_use_num 降但 content_score 不降/上升 | 好的进化 | 交叉 content_score [证据] |
| tool_use_num 升，且伴随 toolResult 报错重试 | 坏：绕路/试错 | 数报错次数 [证据] |
| trials 升，且 judge 反复打回（content 未达标） | 坏 | 看 trace_chain 逐轮 judge 反馈 [证据] |

**潜在副作用**（结合其它指标，标清推断）：
- tool_use_num↓ 但 `output_tokens`↑ → 把工具活儿塞进单条长回复 [推断，需读 assistant content 佐证]；
- trials↓ 但 `cache_hit_ratio`↓ → prompt 结构变得不利缓存 [推断]。

**每条结论标 `[证据]`（trace 原文/计数可复核）或 `[推断]`（指标相关性猜测，给出进一步验证方式）。**

---

## 5. 取证操作提示

- backfilled JSON 可能较大：优先用 jq / python 定位到 `runs[r].suites[s].tasks[t]`，只提所需 phase 的 `all_messages`，不要整文件塞进上下文。
- 逐任务取证时，先用 comparison CSV 锁定"变化最大/最矛盾"的任务名，再回 backfilled JSON 精读，避免盲目通读。
- 跨 runtime 取证：不同 runtime 的工具集 / 消息格式细节不同（OpenAI-style `tool_calls` vs OpenClaw block-style `toolCall`），`build_trajectory_nodes` 已兼容两者；人工读时注意字段位置差异。
