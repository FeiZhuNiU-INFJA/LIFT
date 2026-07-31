# 指标参考（权威契约）

本文件是分析 LIFT 后处理结果时的**指标权威定义**：每个指标的物理意义、方向（越高/越低越好）、数据来源、代码出处，以及离群排除口径。所有判断以此为准。

代码出处：[../../../src/postprocess/metrics.py](../../../src/postprocess/metrics.py)、[../../../src/postprocess/extract.py](../../../src/postprocess/extract.py)、[../../../src/postprocess/report_html.py](../../../src/postprocess/report_html.py)、[../../../src/models.py](../../../src/models.py)。

---

## 0. 基本量：impr 与 diff

对每个指标，后处理都算两个衍生量（`build_comparison_dataframe`）：

- **`impr_{metric}`**（相对改进）：$\dfrac{\text{evolved} - \text{baseline}}{\text{baseline}}$。baseline 为 0 时无法定义 → NaN。
- **`diff_{metric}`**（绝对差）：$\text{evolved} - \text{baseline}$。

小 baseline 值（如 trials=1）会让 `impr` 剧烈放大 → **优先看 `diff` 判稳健性，`impr` 看幅度**。

聚合口径（`build_summary_row`）：
- **`mean_impr_{metric}`** 用**先求和再算比例**：$\dfrac{\sum \text{evolved} - \sum \text{baseline}}{\sum \text{baseline}}$，不是逐样本 impr 取平均（避免小 baseline 放大失真）。
- **`mean_diff_{metric}`** 是逐样本绝对差的算术平均。

---

## 1. 纳入分析的指标（方向 + 含义 + 来源）

方向定义见 `report_html.py::_METRIC_LOWER_IS_BETTER`。

### ⭐ 头号效率指标：`trials`（越低越好）

| 指标 | HTML 列名 | 含义 | 来源 |
|---|---|---|---|
| `trials` | Trials | **达成满分 content_score 所需的 work↔judge 交互轮数**（成功那轮的序号；超限时 = `max_conversation_turns`） | `len(work_analytics.chat_turns)`（extract.py）。用 chat_turns 而非 all_messages，避免 context 压缩丢早期 message 误算 |

**为什么 trials 是与进化效率最相关的核心指标**：LIFT 的任务靠**多轮 judge 提醒**收敛——[../../../src/lift/eval/run_task.py](../../../src/lift/eval/run_task.py) 的循环里，judge 每轮把 `reason`（还差哪些 content_reqs）反馈给 work agent 当作下一轮 prompt，直到 `success` 或耗尽 `max_conversation_turns`。所以 baseline 常要 N 轮才被提醒补齐，而进化后的 evolved **一上来就想到、几轮内满分** → `trials` 下降就是"自进化学会了一次把事做对"的最直接度量。**分析与对比时永远最先看 trials。**

### content_score / trajectory_score / 成功率（效果类，越高越好）

| 指标 | HTML 列名 | 含义 | 来源 |
|---|---|---|---|
| `content_score` | Outcome Score | Judge 打的内容分 0~1 = 满足要求数/总要求数；success 时为 1 | `PhaseRun.content_score`，缺失回退 `agent_input.content_score`（extract.py `make_row`）。Judge 判定逻辑见 [../../../src/lift/eval/run_task.py](../../../src/lift/eval/run_task.py) |
| `trajectory_score` | Trajectory Score | Judge LLM 对**执行路径质量**的打分 0~1，只看轨迹不看内容 | 后处理另起 judge 评（judge.py）。**`DO_TRAJECTORY_JUDGE=false` 时恒返回 1.0（mock）** |
| 成功率 | Baseline/Evolved Success Rate | `success` 布尔（Judge 判定"全部要求满足"）在该 scope 的均值 | `PhaseRun.success` |

> ⚠️ **content_score ≈ 1 是设计预期，不是天花板效应**：多轮提醒机制本就是把任务推向满分，`max_conversation_turns` 给够时绝大多数任务都应满分。所以 content_score 的 good/bad **不是进化好坏的主判据**，它只用于**门槛校验**（确认两 phase 是否同满分，从而 trials 对比是否干净）。
> 反过来，**content_score 持续偏低才是警报**：最常见原因是**启动脚本 `max_conversation_turns` < 该任务 content_reqs 条数**（`content_reqs` 见 `models.py`，是 judge 脑中的要求清单），轮数耗尽也补不齐 → `success=False`。这属**配置/设计问题**，须与进化好坏解耦、单独点名。
> ⚠️ **trajectory_score 陷阱**：默认 mock=1.0（judge.py `judge_trajectory_with_mock`），全列为 1、无区分度。解读前**先确认 `.env` 的 `DO_TRAJECTORY_JUDGE`**，否则此维度是噪声，不得作为结论。

### 其余成本 / 效率类（越低越好）

| 指标 | HTML 列名 | 含义 | 来源 |
|---|---|---|---|
| `tool_use_num` | Tool Use Num | 工具调用块总数 | `global_stats.tool_call_blocks`，为 0 时回退 `tool_observation_count`（extract.py `_make_metric_row`）。OpenClaw 自进化 signal 调用（`exec` 到 `127.0.0.1:18090`）被 `_should_ignore_tool_call_block` 过滤，不计入 |

### Token 类（5 字段 schema + 2 派生）

Token schema（`models.py::LangfuseTokenToolStats`）三段 input 互斥：`input + cache_write + cache_read` = 真正进模型的完整 prompt。

| 指标 | HTML 列名 | 方向 | 含义 |
|---|---|---|---|
| `input_tokens` | Input (fresh) | ↓ 越低越好 | 新增输入，**不含 cache** |
| `cache_read_tokens` | Cache Read | ↑ **越高越好** | 命中 cache 读取，越多越省钱，与命中率同向 |
| `output_tokens` | Output | ↓ 越低越好 | 总输出，**包含** reasoning |
| `reasoning_tokens` | Reasoning | ↓ 越低越好 | 思维链 token，$\subset$ output_tokens |
| `total_tokens`（派生） | Total Tokens | ↓ 越低越好 | $input + cache\_write + cache\_read + output$。**reasoning 不再加**（已在 output 内，否则双计） |
| `cache_hit_ratio`（派生） | Cache Hit Ratio | ↑ **越高越好** | $\dfrac{cache\_read}{input + cache\_write + cache\_read}$，prompt 复用 KV cache 的比例，跨 provider 一致 |

---

## 2. 本 skill 明确排除的两个指标

| 指标 | 为什么排除 |
|---|---|
| `total_latency_seconds` | 受网络 / VM 负载 / 并发容器数 / provider 抖动干扰太大，**与进化效果无稳定因果**。Hermes 上游本就不上报（HTML 隐藏）。**不作为进化好坏依据** |
| `cache_write_tokens` | 太小众：OpenAI / Ark / DeepSeek / Gemini 家恒 0，只有 Anthropic 风格才非 0。HTML 默认隐藏（`_HTML_HIDDEN_METRICS_BASE`），无分析价值 |

---

## 3. 离群排除口径（关键，报告必须声明）

代码：`metrics.py` 的 `SUMMARY_IMPR_OUTLIER_METRICS = ("trials", "tool_use_num")`、`SUMMARY_IMPR_OUTLIER_THRESHOLD = 2.0`、`_outlier_mask`。

**规则**：某任务若满足

$$\text{impr\_trials} \ge 2.0 \quad \lor \quad \text{impr\_tool\_use\_num} \ge 2.0$$

即 evolved 的 trials 或 tool_use_num $\ge 3\times$ baseline（暴涨 $\ge 200\%$），判为**退化过强的离群点**：

- **剔除**：不参与 suite / global 的 `mean_impr` / `mean_diff` 聚合，也不进 **good/bad 图**（`good_bad_chart_html` 里 `scope_df.loc[~_outlier_mask(...)]`）。
- **保留**：仍出现在逐任务明细表（Run Blocks），带原始颜色。

summary 里三个计数字段：
- `task_count`：该 scope 全部任务对数；
- `task_count_aggregated`：纳入聚合的数（= 总数 − 离群）；
- `task_count_excluded`：被剔除的离群数。

📌 **报告要点**：开头声明"共 N 对，有效 aggregated 个，剔除 excluded 个"，并**逐一列出被剔除任务**——它们是进化反噬最严重的点，要在明细里单独审，不能因为被排除就忽略。

---

## 4. profound（★）与 good/bad —— 只在 HTML

| 概念 | 定义 | 代码 | 在哪看 |
|---|---|---|---|
| **good / tie / bad** | 对每指标，按方向把每任务的 `diff_{metric}` 判为变好/持平/变坏并计数 | `report_html.py::_good_bad_counts`、`good_bad_chart_html` | HTML "Better/Worse Task Counts" 堆叠条（绿/灰/红），**已剔离群** |
| **profound（★）** | evolved 不只赢同 run baseline，还赢**该任务跨所有 run 的最优 baseline**（成本类取 min、效果类取 max） | `report_html.py::build_profound_flags` | HTML 绿段上赭色斜纹 + 左侧 `★N`；明细表任务名 / `Impr` 单元格前 ★ |

⚠️ **CSV 里既没有 good/bad 计数，也没有 profound**——这两个是 HTML 渲染时才算的。所以主判据必须回到 HTML；CSV 仅用于快速查具体 impr/diff 数值。

---

## 5. HTML 单元格着色（快速读图）

`report_html.py::_value_color_class`：按指标方向给 `diff`/`impr` 上色——绿=优于 baseline、红=劣于、黑=持平、灰=NaN（baseline 缺失/为 0，无法定义相对改进）。图例见报告顶部 Legend 卡片。
