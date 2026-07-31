---
name: "lift-analyze-eval-results"
description: "分析 / 解读 LIFT 后处理产物（backfilled JSON + HTML + CSV），判定 agent 自进化到底有没有变好、好在哪、坏在哪。以 good/bad 任务计数为主、相近时看 profound 程度；当 good/bad 差但 profound 好时深入 trace 归因（工具调用正确性、系统报错、skill 精准度与是否过拟合）；重点归因 trials / tool_use_num 变化的原因与好坏，区分推断与实证，并处理 impr>=2 的离群排除口径。用户说\"分析评测结果 / 对比结果 / 分析进化效果 / 解读后处理结果 / 这次进化好不好\"时使用。"
---

# LIFT: 分析与对比评测结果

这份 skill 用于**读懂 LIFT 后处理已经生成的产物**，给出一份"这次自进化到底有没有变好、好/坏在哪、为什么"的分析报告。它**不负责生成**报告产物（那是评测流水线 `-e` / `--evaluate-only` 或 `evolution-report` skill 的事），只负责**消费并解读**。

核心范式回顾：每个 holdout 任务跑两次——`baseline`（干净基线镜像）与 `evolved`（warmup+evolve 后 `docker commit` 的 delta 镜像）。后处理把两次配对，算 `impr`（相对改进）与 `diff`（绝对差）。本 skill 就是在这份配对结果上做判断。

> **一句话方法论**：先看 **good/bad**（有多少任务变好/变坏）定大盘；相近时比 **profound** 程度定深度；出现"good/bad 差但 profound 好"这种矛盾信号，**必须下沉到 trace 归因**，并严格区分「有实际证据」与「属于推断」。

---

## 何时使用

- 用户说"分析这次评测结果 / 对比结果 / 进化效果好不好 / 解读后处理产物"
- 拿到一个 `results/{run_id}/` 目录，想知道 evolved 相对 baseline 是进步还是退化
- 想搞清楚"为什么 trials/tool_use_num 变了、是好是坏"
- 怀疑 evolved 学到的 skill 过拟合、或引入了系统性副作用，需要证据

---

## 第一原则：数据从哪里读（三层优先级）

所有产物都在 **`results/{run_id}/`** 下（`run_id` 见 `report.json` 的 `run_id` 字段）。文件名前缀 = `run_id`：

| 层级 | 文件 | 定位 | 提供什么 |
|---|---|---|---|
| **主力** | `{run_id}_metrics_report.html` | **数据以它为准** | Global/Suite 汇总表、成功率、**good/bad 堆叠条形图（已剔除离群）**、**profound（★）标记与计数**、每任务 evolved(baseline) 明细、逐任务可点击的 evolved vs baseline 轨迹图 |
| **兜底/取证** | `{run_id}_backfilled.json` | **最详细的检查与深挖** | 每任务 `{baseline,evolved}` 的 `success/content_score/turns/tool_calls/workspace_dir` + `langfuse.work_analytics.{all_messages, trace_chain, global_stats, chat_turns}`——工具调用、skill 内容、系统报错都在这里 |
| **辅助** | `{run_id}_comparison_metrics.csv`、`{run_id}_summary_metrics.csv` | **只用来快速查数值** | comparison=逐任务每指标的 evolved/baseline/impr/diff；summary=按 suite + global 的聚合。**注意：CSV 里没有 profound，也没有 good/bad 计数**（这两个只在 HTML 生成），所以判定必须回到 HTML |

配套（深挖用）：
- **实际产物工作区**：`results/{run_id}/outcome/run-{i}/{baseline|evolved}/{suite}/{task}/`——agent 真正写出的文件，用来核对"内容要求是否真被满足"。
- **执行期 report**：`results/{run_id}/report.json`——只有结论（success/score），trace 在 backfilled 里。

> **优先级铁律**：**结论与数值以 HTML 为准，CSV 只是让你少翻 HTML 表格、方便批量查数，backfilled JSON 是所有"为什么"的最终取证来源**。三者对不上时，以 HTML + backfilled JSON 为准（CSV 是从同一 DataFrame 落盘，一般一致）。

指标定义、方向、代码出处、排除口径 → 见 [docs/metric-reference.md](./docs/metric-reference.md)（权威契约）。
如何下沉 trace 做工具/skill 归因 → 见 [docs/trace-deep-dive.md](./docs/trace-deep-dive.md)。

---

## 纳入分析的指标（及本 skill 明确排除的两个）

只在下列维度上做判断（方向见 metric-reference）。

### ⭐ 头号核心指标：`trials`（越低越好）

**`trials` = 达成满分 content_score 所需的 work↔judge 交互轮数**，是**与进化效率提升最相关的核心指标，分析与对比时必须重点关注、优先归因**。

原理：LIFT 的任务本就**靠多轮 judge 提醒来收敛**——judge 每轮把"还差什么"的 reason 反馈给 work agent 当作下一轮 prompt，逼它逐步补齐所有 `content_reqs`（见 [../../src/lift/eval/run_task.py](../../src/lift/eval/run_task.py) 的 work↔judge 循环）。因此**真正的进化红利体现在"少提醒几轮就能满分"**：baseline 要 N 轮才补齐的要求，evolved 一上来就想到、几轮内搞定 → `trials` 下降。这是自进化"学会了怎么把事一次做对"的最直接度量。

→ 报告里 `trials` 的变化要放在最前面、给出**为什么变、好还是坏、为什么好/坏**，并下沉 trace 找出"evolved 少走了哪几轮、省掉了 baseline 的哪些返工"（见 Step 5）。

### 关于 `content_score`（Outcome Score）：它是"是否达标"的门槛，不是"进化好坏"的主指标

- **content_score ≈ 1（满分）是框架设计的预期结果，不是异常、更不是"评测集太简单/天花板效应"**。因为多轮提醒机制就是设计来把任务推到满分的；只要 `max_conversation_turns` 给够，绝大多数任务最终都应满分。
- ⚠️ **反过来才是警报**：若某任务 content_score **持续偏低 / 拿不到满分**，优先怀疑是**评测设计或启动配置问题**，而非进化差——最常见的是**启动脚本的 `max_conversation_turns` 设得比该任务 `content_reqs` 的要求条数还少**，导致轮数耗尽也补不齐所有要求（`success=False`，score 为最后一轮分）。这类要点名指出是"配置/设计问题"，与进化效果解耦。
- 因此：**content_score 主要用来确认"两个 phase 是否都达到了可比的满分基线"**。只有在**同为满分**的前提下，`trials`/`tool_use_num`/token 的对比才是干净的"效率进化"信号。若 evolved 满分而 baseline 未满分（或反之），要先说明这是效果差异、再谈效率。

**其余指标（都要深入，不得因为 trials 是头号就略过）**：

- `tool_use_num`（越低越好）：达成同等满分所用的工具调用数，与 trials 并列的效率信号。
- `trajectory_score`：**默认 mock=1.0，先确认 `DO_TRAJECTORY_JUDGE=true` 才有意义**，否则不解读。
- 成功率 `baseline/evolved_success_rate`：`success` 布尔均值；结合 content_score 看是否有任务未达标。
- Token 类：`input_tokens`↓、`cache_read_tokens`↑、`output_tokens`↓、`reasoning_tokens`↓、`total_tokens`↓、`cache_hit_ratio`↑。

**明确排除、不纳入进化好坏判断**：
- ❌ `total_latency_seconds`——受网络/负载/并发/provider 抖动干扰太大，**与进化效果没有稳定因果关联**，不作为结论依据（Hermes 本就无此数据）。
- ❌ `cache_write_tokens`——太小众（OpenAI 家恒 0），无分析价值，HTML 本就默认隐藏。

---

## 分析工作流

按顺序推进。每一步先落"事实/数值"，再落"判断"，判断必须标注是**证据**还是**推断**。

### Step 1. 数据有效性核查（先算清楚有多少数据可信）

从 HTML 的 success badges / summary CSV 读三个字段：`task_count`、`task_count_aggregated`、`task_count_excluded`。

**离群排除口径（必须在报告里写清楚）**：当某任务满足

$$\text{impr\_trials} \ge 2.0 \quad \lor \quad \text{impr\_tool\_use\_num} \ge 2.0$$

（即 evolved 的 trials 或 tool_use_num $\ge 3\times$ baseline，暴涨 $\ge 200\%$），该任务被判为**退化过强的离群点**，从 suite/global 的 `mean_impr`/`mean_diff` 聚合与 **good/bad 图**中剔除，但**仍保留在逐任务明细表**。代码见 [../../src/postprocess/metrics.py](../../src/postprocess/metrics.py)（`SUMMARY_IMPR_OUTLIER_METRICS` / `SUMMARY_IMPR_OUTLIER_THRESHOLD` / `_outlier_mask`）。

📌 **报告开头必须声明**：本次共 N 个任务对，有效纳入聚合 `task_count_aggregated` 个，因离群剔除 `task_count_excluded` 个（并逐一列出被剔除的任务名——它们恰恰是进化反噬最严重的点，要单独审）。

### Step 2. 定大盘：good / bad 任务计数

看 HTML 每个 suite + global 的 **"Better / Worse Task Counts"** 图（绿=变好、灰=持平、红=变坏，逐指标一条）。这是主判据：

- 若某指标 **绿 ≫ 红** → 进化在该维度整体正收益（证据）。
- 若 **红 ≫ 绿** → 整体退化。
- **主判据是 `trials` 的 good/bad**（达成满分所需轮数降没降），其次 `tool_use_num` 与 token —— 这才是"进化让 agent 更高效没有"的直接答案。
- `content_score` 的 good/bad **不是主判据**：它多半几乎全 tie（都收敛到满分，符合设计预期）。看它是为了**门槛校验**——确认两个 phase 是否都满分、有没有任务掉出满分（掉出的先按 Step 2.5 排查是不是配置/设计问题，再谈进化）。

> good/bad 图已剔除离群（与 summary 口径一致）。若要自己从 `comparison_metrics.csv` 复算，需对每任务的 `diff_{metric}` 按方向判正负，且**手动套用同一排除规则**，否则口径不一致。

### Step 2.5. content_score 门槛校验（判断效率对比是否"干净"）

在把 trials/token 当"效率进化"解读之前，先校验 content_score：

- **两个 phase 都满分**（最常见）→ 效率对比干净，可放心用 trials 归因进化。
- **有任务未满分 / 偏低**：先分流原因，不要直接归咎进化——
  - 若 **baseline 与 evolved 同样拿不到满分** → 高度怀疑 **`max_conversation_turns` < 该任务 content_reqs 条数**（轮数耗尽也补不齐要求），或任务设计本身有问题。**点名为"配置/设计问题"**，从进化好坏判断里剔除该任务。
  - 若 **一方满分、另一方不满分** → 这是真实的**效果差异**，先说明效果差异，再谈效率（此时 trials 不可比，因为一方是"没做完"而非"更快做完"）。
- 报告需明确写出：本次 content_score 分布（baseline/evolved 各有多少满分）、是否存在配置/设计导致的未满分任务。

### Step 3. 相近时比 profound（★）程度

当两组（或多个 run / runtime 内部）good/bad **接近、难分高下**时，用 profound 破平：

- **profound 定义**：evolved 不只赢过同 run 的 baseline，还赢过**该任务跨所有 run 的最优 baseline**（方向感知：成本类取 min baseline，效果类取 max baseline）。见 [../../src/postprocess/report_html.py](../../src/postprocess/report_html.py) `build_profound_flags`。
- **只在 HTML 有**：good/bad 图里绿段上的赭色斜纹 + 左侧 `★N`，以及明细表任务名/`Impr` 单元格前的 ★。CSV 里没有，必须看 HTML。
- profound 多 = 进化不是"微弱普涨"而是"实打实拉到历史最好之上"，深度更强。

### Step 4. 处理矛盾信号：good/bad 差，但 profound 好

这是最需要小心的情形——**整体退化，但少数任务被拉到极强**。往往意味着进化学到的东西"过拟合"到某些任务、却损害了其它任务。**必须下沉 trace 取证**（详见 [docs/trace-deep-dive.md](./docs/trace-deep-dive.md)），逐条核查：

1. **工具调用正确性**：evolved 的工具名/参数是否合理？是否调错工具、传错路径/参数？
2. **工具返回是否大量系统/环境错误**：backfilled JSON 的 `all_messages` 里 `role=toolResult` / `role=tool` 的返回是否充斥 `Error` / `not found` / 权限/网络失败？大量系统性报错会拖垮 good/bad，却不影响个别侥幸成功任务的 profound。
3. **skill 调用是否精准**：evolved 是否在**不该用的任务上**硬套 warmup 学到的 skill？
4. **skill 内容是否合理、是否与任务 content 要求相悖**：从 trace 里 read/加载 skill 的内容看，若 skill 把某个特定任务的答案/套路写死，并在通用任务上照搬 → **判定为 skill 过拟合**，这是必须点名指出的问题（进化"学偏了"）。

> 结论范式："task X/Y/Z 上 evolved profound（证据：content_score 0.4→1.0），但全局 good/bad 为红（证据：8 任务中 5 个 content_score 下降），下沉 trace 发现 evolved 在 A 类任务反复调用 warmup 习得的 `skill-foo`，而 `skill-foo` 内容含写死的 B 任务答案（证据：trace turn 3 read 出的 skill 正文），与 A 类 content 要求相悖 → **skill 过拟合**（判定）。"

### Step 5. 头号归因：trials（其次 tool_use_num）为什么变、是好是坏

**`trials` 是本 skill 的头号归因对象，永远最先分析、写在结论最前面。** 对每个显著变化，回答三问：**为什么变？好还是坏？为什么好/坏？**

从 HTML 轨迹图 + backfilled JSON 的 `all_messages` / `trace_chain` 提取**变化特征**（怎么读见 [docs/trace-deep-dive.md](./docs/trace-deep-dive.md)）：

- **trials 变少（进化的核心红利）**：baseline 要 N 轮被 judge 反复提醒才补齐 content_reqs，evolved 几轮内就满分 → 进化"学会了一次把事做对"。**必须下沉 trace 找出 evolved 具体省掉了 baseline 的哪几轮返工**（对比两侧 trace_chain 每轮 judge reason：baseline 第 2/3/4 轮被要求补的东西，evolved 第 1 轮就做了），并核对 evolved 仍是满分（否则见 Step 2.5）。这是最有说服力的证据。
- **trials 变多**：通常坏——被 judge 反复打回（某要求反复不达标）或啰嗦绕路。查 trace_chain 逐轮 judge reason 看卡在哪个 content_req。
- **tool_use_num 变少但仍满分**：多半是**好的进化特征**，常见形态：
  - 把多步工具调用链**整合成更少的工具组合**（如先前 3 次 read+1 次 grep，evolved 一次 grep 定位）；
  - **选了更合适的工具**（如用专用检索代替裸 shell 拼接）。
- **tool_use_num 变多**：可能坏（绕路、试错、报错重试），也可能好（把一次糊弄拆成扎实的多步）——**必须结合 trials 与 content_score 判断**，不能只看数字。

**潜在副作用**：tool_use_num 降但 output_tokens 暴涨（把工具活儿塞进单条长回复）、或 trials 降但 cache_hit_ratio 降（轮数骤减→prompt 重放变少→可复用 KV cache 机会减少，属副产品而非退化，需结合绝对 cache_read/total_tokens 判断是否真省）等。发现时结合其它指标说明，并**标清哪些是 trace 实证、哪些是指标相关性推断**。

### Step 6. 跨 runtime 对比要极其小心

不同架构的 agent（OpenClaw / Hermes / GA / OpenHuman / EvoScientist）**工具集、轮次语义、token 归一、是否有 per-turn latency 都不同**。因此：

- **绝对值不可直接比**（如 A runtime 的 tool_use_num=3 与 B 的 =8 不代表 A 更省——工具粒度不同）。
- **可比的是"各自 evolved 相对各自 baseline 的改进方向与幅度"**（即 impr / good-bad / profound 这些**相对量**）。
- Hermes 隐藏 latency、OpenAI 家 cache_write 恒 0——本就不在我们分析范围，跨 runtime 时更不必纠结。
- 报告里跨 runtime 结论一律加限定语，避免把架构差异误读成进化差异。

### Step 7. 证据纪律（贯穿全程）

- **证据**：直接来自 HTML 数值、CSV 数值、backfilled JSON trace 原文、outcome 实际产物文件。
- **推断**：由指标相关性、经验模式得出的因果猜测。
- 报告中每条因果判断都要标 `[证据]` 或 `[推断]`；`[推断]` 需给出"可如何进一步验证"。

---

## 报告产出模板

产出一份 Markdown 分析报告（用户偏好 markdown 深度推理 + 必要处用 LaTeX；数值汇总可附简表）。建议结构：

```markdown
# LIFT 评测结果分析：{run_id}（runtime = {agent_source}）

## 0. 数据有效性
- 任务对总数 N；纳入聚合 {aggregated}；离群剔除 {excluded}
- 被剔除任务（impr_trials 或 impr_tool_use_num ≥ 2.0）：<列表 + 各自 impr 值>
- trajectory_judge 是否开启：{是/否——否则 trajectory_score 恒 1.0，不解读}

## 1. content_score 门槛校验（先确认效率对比是否干净）
- content_score 分布：baseline 满分 {x}/N，evolved 满分 {y}/N
- 是否有未满分任务？若有：是配置/设计问题（max_conversation_turns < content_reqs 条数）还是真实效果差异？逐一说明
- 结论：本次是否为"两 phase 同满分"的干净效率对比

## 2. 头号结论：trials（达成满分所需交互轮数）
- trials good/tie/bad 计数 + mean_diff；一句话定性（进化是否让 agent 更快满分）
- 代表性任务下沉 trace：evolved 省掉了 baseline 的哪几轮返工 [证据]

## 3. 其余大盘（good/bad）
- tool_use_num / token 各指标 good/tie/bad 计数（表）；一句话定性

## 4. 深度（profound）
- 各指标 profound 计数（尤其 trials）；相近对比时的破平结论

## 5. 矛盾信号与归因（若有 good/bad 差 & profound 好）
- 逐任务 trace 取证：工具正确性 / 系统报错 / skill 精准度 / skill 过拟合
- 每条标 [证据]/[推断]

## 6. tool_use_num 与 Token 经济性
- tool_use_num 变化特征（工具链整合 / 换工具 / 副作用）
- input / cache_read / output / reasoning / total / cache_hit_ratio 的方向与解读（注意 cache 比例下降可能是 trials 骤降的副产品）

## 7. 跨 runtime 说明（若涉及）
- 只比相对改进；架构差异限定语

## 8. 总体判定与风险
- 进化是否有效（以 trials 效率红利为主）、主要收益点、主要风险（尤其 skill 过拟合 / 系统性报错 / 配置导致的未满分）
```

---

## docs 索引

| 文档 | 何时看 |
|---|---|
| [docs/metric-reference.md](./docs/metric-reference.md) | 需要指标定义、方向、代码出处、离群排除口径的权威说明 |
| [docs/trace-deep-dive.md](./docs/trace-deep-dive.md) | Step 4/5；要从 backfilled JSON `all_messages` / `trace_chain` 取证工具调用、系统报错、skill 内容与过拟合 |

## 相关代码（取证时对照）

- 指标与聚合/排除：[../../src/postprocess/metrics.py](../../src/postprocess/metrics.py)
- 指标提取（token 5 字段、trials、tool_use_num 来源）：[../../src/postprocess/extract.py](../../src/postprocess/extract.py)
- 方向 / profound / good-bad / 轨迹渲染：[../../src/postprocess/report_html.py](../../src/postprocess/report_html.py)
- trajectory_judge：[../../src/postprocess/judge.py](../../src/postprocess/judge.py)
- 数据模型（PhaseRun / token schema）：[../../src/models.py](../../src/models.py)
