# 2026-07-26 · GenericAgent 1800s 超时根因分析

## TL;DR

`genericagent-full` run(10 repeat × 14 suite × 2 phase = 560 task-phase)累计
**235 次 `chat exec timeout: GA wait output timed out after 1800s`**,分布在
**14 个 work session + 42 个 judge session**。

**根因**:一次 GA `put_task` 内部是 `LLM ↔ tool` 的 loop;长上下文场景下单步
`llm.chat` 上探到 60-120s,15-30 步就撞穿 [`CHAT_EXEC_TIMEOUT_SECONDS=1800s`](../../src/lift/adapters/genericagent/chat_agent.py#L36)
的墙钟。**不是 tool 卡住、不是网络代理、也不是 GA `max_turns=180` 打满**——
langfuse trace 里 observation 数从未接近 180,最多 35。

paper 中比较 GenericAgent 与其它 runtime 时,必须标注:1800s 超时是运行时约束,
不代表 agent 能力上限。

## 归因公式

```
一次 put_task 墙钟 ≈ Σ(单步耗时)
                  ≈ N_step × 单次 llm.chat 延时 (本地 tool ≈ 0s, firecrawl ≈ 3-10s)

长上下文场景: N_step = 20-30, llm.chat = 60-120s
  → 一次 put_task = 1200-3600s → 撞穿 1800s
```

关键杠杆:**单步 `llm.chat` 延时**。它跟 prefill 尺寸强相关——`reasoning_effort=high`
+ `context_win=85000` + 多轮工具产出累加,让第 8 轮 prompt 之后的 prefill 上探
到 150k+ tokens,单请求耗时从 20s 涨到 90-120s。

## 三层"上限"的语义澄清

排查中反复混淆的三个上限,paper 里**必须分开表述**:

| 层 | 参数 | 含义 | 触发 fail 时的日志特征 |
|---|---|---|---|
| GA 内部 | `max_turns=180`([`agentmain.py`](../../agent-runtimes/genericagent/)) | 单次 `put_task` 内 LLM ↔ tool 的最大回合数 | GA 输出里带 "reached max turns";**实际生产中从未观察到打满** |
| LIFT 单轮墙钟 | [`CHAT_EXEC_TIMEOUT_SECONDS=1800`](../../src/lift/adapters/genericagent/chat_agent.py#L36) | 单次 `chat` 等待 GA 交答案的 wall-clock | `wait output timed out after 1800s` warning,后接 provider retry;retry 用尽则 phase raise |
| LIFT 对话 | `--max-conversation-turns=36` | work↔judge 反复拉扯的最大轮数 | task 完成但 `success=False`,`turns` 打满,`content_score` 通常已 ~1.0 |

三者**独立触发**——1800s 超时和 `max_conversation_turns` 打满是**不同的 fail 路径**。

## 关键证据(session `user-a2bbbad6`)

该 work session 从 LIFT log 看:第 8 轮 prompt 发出后 4 次 retry 全部 1800s
超时后死掉。**卡死那次的 trace 未被 langfuse flush**(进程被 SIGKILL 前 export
没落库),但**之前 6 个成功 trace 的步长趋势已足够反推撞穿机制**。

**Trace 6(第 7 轮 prompt · 212.9s · 18 obs)——即将撞墙前的最后一次成功:**

```
+   0.0s  llm.chat                    90.0s ← 长上下文首轮生成
+  90.0s  file_patch                   0.0s
+  90.0s  llm.chat                    21.6s
+ 111.6s  file_patch                   0.0s
+ 111.6s  llm.chat                     7.2s
+ 118.8s  file_read                    0.0s
+ 118.8s  llm.chat                    21.6s
+ 140.4s  file_patch                   0.0s
+ 140.4s  llm.chat                     6.9s
+ 147.3s  file_read                    0.0s
+ 147.3s  llm.chat                    21.5s
+ 168.8s  file_patch                   0.0s
+ 168.8s  llm.chat                    20.3s
+ 189.1s  file_patch                   0.0s
+ 189.1s  llm.chat                    12.7s
+ 201.8s  update_working_checkpoint    0.0s
+ 201.8s  llm.chat                    11.1s
+ 212.9s  no_tool                      0.0s
```

**Session 汇总(6 trace · 57 次 llm.chat · 12 次 firecrawl):**

| 工具 | 次数 | 平均 | 中位数 | 最大 |
|---|---:|---:|---:|---:|
| `llm.chat` | 57 | **13.9s** | 9.0s | **90.0s** |
| `firecrawl_search` | 12 | 3.9s | 3.4s | 10.0s |
| `firecrawl_scrape` | 2 | 4.9s | 4.9s | 5.8s |
| `code_run` | 3 | 1.0s | 1.0s | 1.0s |
| `file_patch` / `file_read` / `checkpoint` / `file_write` | 34 | 0.0s | 0.0s | 0.0s |

**推论**:

1. 本地 tool 全部 0.0s → **"tool 卡住"假设排除**
2. `firecrawl` 平均 3.9s、最大 10s → **"网络卡住"假设排除**
3. **`llm.chat` 是唯一时间池**——最大 90s 已发生在**还没撞墙的第 7 轮**;第 8
   轮 prompt 触发新一轮大规模抓取后,`llm.chat` 有理由继续上探到 120-180s
4. **1800s 撞穿的算式**:15 步 × 120s = 1800s,或 20 步 × 90s = 1800s——GA
   一次交答卷通常需要 8-30 步,任何不利组合都能撞到

## 数据附录:`genericagent-full` run 超时分布

| 场景 | 频次 | 备注 |
|---|---|---|
| work-side 1800s 超时(unique session) | 14 | 集中在长上下文任务(旅游/亲子游/多轮补充需求)|
| judge-side 1800s 超时(unique session) | 42 | Baseline 输出过长 → judge 单次评分 prefill 巨大,单步耗时高 |
| 累计 warning 事件(含 provider retry) | 235 | 单 session 最多 4 条(1 原 + 3 retry)|
| resume 前完全失败的 cell | 8 | 全部在 `健康零食购物攻略测评` + 1 个 `股票投资决策`;**resume 后全部跑通** |
| `success=False` 但 cell 完整的 task | 1 | r1 s11 `猫粮::Q6` baseline turns=36 |
| **最终 raise 的 task-phase** | **0** | 全被 provider retry 挽救,最终 report 完整 |

## paper 中标注要求

陈述 GenericAgent 结果时至少标注:

1. 累计 1800s 超时占 task-phase 的比例(本次 = 235 / 560 warning 覆盖率)
2. 因超时被 provider retry 挽救(转 success)的比例(本次 ≈ 100%)
3. 因超时用尽 retry 而 raise 的 task 数(本次 = 0)

## 参考文件

- LIFT 墙钟配置: [`src/lift/adapters/genericagent/chat_agent.py`](../../src/lift/adapters/genericagent/chat_agent.py#L36)
- GA 模型 & 裁剪配置: [`agent-runtimes/genericagent/mykey.py.template`](../../agent-runtimes/genericagent/mykey.py.template)
- Firecrawl 插件: 镜像内 `/opt/GenericAgent/plugins/firecrawl_plugin.py`
- 项目 project_memory 已固化 `context_win=85000` + `CHAT_EXEC_TIMEOUT_SECONDS=1800s` 组合结论
