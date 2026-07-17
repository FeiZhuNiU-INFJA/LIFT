# EvoScientist 不适合作为 LIFT 通用 Agent 评测对象的分析

本文记录 EvoScientist runtime 接入 LIFT 后，在通用个人助理型评测中的适配性问题。
结论不是“EvoScientist 能力弱”，而是：**EvoScientist 的产品形态和行为先验更接近科研/项目执行型垂直 Agent，不适合作为 LIFT 中的通用 Agent 基线来承载泛个人助理类 holdout 评测。**

## 背景

LIFT 的核心评测目标是观察 agent 在 warmup 后是否能把偏好、技能或策略迁移到 holdout。对于通用 Agent，典型 suite 会包含：

- 邮件、日程、清单、代码示例等轻量个人助理任务
- 多轮 judge 对 work agent 输出做增量纠错
- holdout 中要求主动复用 warmup 学到的偏好
- baseline 与 evolved 对比成功率、轮数、工具调用、token 和 latency

EvoScientist 已完成 LIFT runtime 接入、Langfuse trace 拼接、Token 5 字段观测、Firecrawl MCP 配置与 delta materialization。技术接入链路本身可工作。但在 `assets/benchmarks_demo/integration_check.json` 这类个人助理偏好迁移 suite 上，它表现出系统性的行为不匹配。

## 实证观察

### integration_check 首版：任务画像不匹配导致 warmup 长时间拉扯

早期 `integration_check.json` 同时包含邮件、家庭购物清单、出差日程、代码示例 4 个 warmup，并在 holdout 中交叉引用多个 warmup 偏好。

在 EvoScientist 上，warmup 长时间卡住，典型问题包括：

- 把“改一下”“补进去”理解成要修改已有文件
- 只声明“已记住规则”，不重新输出完整答案
- 对个人助理类任务生成项目化/模板化内容
- 对轻量格式约束执行不稳定

这说明原 suite 对 EvoScientist 来说不是单纯“偏好记忆”测试，而混入了大量与其默认工作模式冲突的通用助理行为要求。

### integration_check 简化版：可以跑通，但仍暴露行为偏差

后续将 suite 收敛为 2 个 warmup + 2 个 holdout：

- `W1 -> H1`：邮件偏好迁移
- `W2 -> H2`：日程偏好迁移

`evosci-integrate-c4` 端到端跑通：

- `baseline_success_rate=1.0`
- `evolved_success_rate=1.0`
- `H1`: baseline/evolved 均成功
- `H2`: baseline/evolved 均成功

但用时仍接近 1 小时。关键不是最终能否通过，而是通过路径暴露出 EvoScientist 的默认行为与通用 Agent 评测假设不一致。

#### H1 邮件：收发件人语义与文件修改倾向

H1 要求：

> 帮我起草一封给客户的道歉邮件，因为我们的项目交付延期了一周。

沿用 W1 偏好：中文、正文出现“老周”、末尾署名 `Zhou`、无 emoji。

实际拉扯点：

1. **“老周”语义被误解为收件人称呼**

   EvoScientist 多次写成：

   ```text
   老周：您好。...
   Zhou
   ```

   但任务语义是“我作为老周，给客户写道歉邮件”。judge 因此追问：

   ```text
   你搞反收发件人啦，我是让你帮我（老周）写我发给客户的道歉邮件，不是别人发给我的...
   ```

   这说明“正文出现对我的称呼老周”对通用 chat agent 也有歧义，但 EvoScientist 在邮件语境下更容易按模板邮件收件人处理。

2. **增量纠错触发文件修改模式**

   judge 说：

   ```text
   你这封邮件正文里没加上我的称呼“老周”，记得补进去哦。
   ```

   EvoScientist 回：

   ```text
   未在当前工作目录中找到对应的邮件草稿文件，请提供邮件文件的具体路径或文件名...
   ```

   这不是能力问题，而是产品先验问题：EvoScientist 把“补进去/修改”解释成 workspace 文件编辑任务，而 LIFT judge 期望的是直接在对话中给出修正版。

3. **模板化、多版本、占位符倾向**

   EvoScientist 常输出：

   - `正式版 / 简洁版`
   - `[项目名称]`
   - `[原交付日期]`
   - `[你的姓名/项目团队名称]`

   这些对真实项目协作可能有用，但对 LIFT 的“直接给出最终答案”判定会增加 judge 拉扯。

#### H2 日程：垂直领域先验压过用户场景

H2 要求：

> 帮我列一下下周去杭州出差三天的日程提纲。

沿用 W2 偏好：按日期或天数组织、北京时间 UTC+8、YYYY-MM-DD。

实际拉扯点：

1. **baseline 没有主动复用具体日期格式**

   baseline 首轮写 `Day1/Day2/Day3`，未给每天具体 `YYYY-MM-DD` 日期，judge 追问后又触发“请提供日程文件路径”的文件修改模式。

2. **内容漂移到科研/项目执行场景**

   一轮输出变成：

   ```text
   项目周会
   核心实验调试
   文献阅读
   数据预处理
   保存模型 checkpoint
   ```

   这些内容明显不是“杭州出差三天”，而是科研/实验工作流。说明 EvoScientist 的领域先验会把模糊日程任务拉回科研项目执行场景。

3. **范围控制不稳**

   它曾加入“行前准备”和“返程日”，把三天出差扩成五天安排。judge 需要继续追问：

   ```text
   你这列了五天的安排呀，我要的是出差三天的日程...
   ```

4. **evolved 可明显改善，但改善来自任务收敛而非通用性**

   `H2 evolved` 一轮通过，说明 warmup 后偏好确实进入 delta。但这并不证明 EvoScientist 适合通用评测；它只说明在强约束、低歧义、单一偏好的日程任务上，warmup 记忆能发挥作用。

## 根本原因

### 1. EvoScientist 是垂直工作流 Agent，不是通用聊天 Agent

从行为看，EvoScientist 更偏向：

- 科研任务拆解
- 项目/实验执行
- 工作区文件读写
- 结构化模板产出
- sub-agent / tool / memory 驱动的复杂流程

而通用个人助理 suite 偏向：

- 直接回答
- 轻量文本生成
- 对上一轮输出做对话内修正
- 不要求文件路径
- 不主动扩展复杂项目背景

二者默认交互协议不同。

### 2. LIFT judge 的自然语言纠错会触发 EvoScientist 的文件操作模式

LIFT 的 work-judge loop 常用类似反馈：

- “改一下”
- “补进去”
- “加上这个”
- “把日期改成 YYYY-MM-DD”

通用 chat agent 通常会理解为“直接重写答案”。EvoScientist 则容易理解为“修改已有文件”，开始索要文件路径。这个行为会显著增加 turns、latency 和 token 消耗。

### 3. EvoScientist 的输出倾向和 LIFT 成功判据冲突

EvoScientist 偏好输出模板、占位符、多版本方案和项目化上下文；LIFT judge 则常要求：

- 直接给最终答案
- 少占位符
- 不额外询问
- 严格满足少量显式 content requirements

这会造成“内容看似专业，但不符合判据”的反复追问。

### 4. Stream transport 限制会污染格式类评测

EvoScientist 的 `--output-format stream-json` 输出是归一化事件流，不是底层 LLM 原始 delta。其 stream 层会丢弃 whitespace-only chunk，导致部分换行在 `done.response` 前已经丢失。

这意味着 benchmark 不应把“必须真实换行”“Markdown 列表必须逐行”等作为 EvoScientist runtime 的硬性判据。否则会把 transport 限制误判为 agent 偏好迁移失败。

## 对 LIFT 评测的影响

如果把 EvoScientist 作为通用 Agent 纳入与 OpenClaw、GenericAgent 等同类对比，结果会混入大量非目标变量：

- 评测到的是“是否适应个人助理对话协议”，而不只是“是否自我演化”
- turns / latency / token 受文件修改模式强烈影响
- 内容成功率受垂直领域先验影响
- judge 反馈措辞会显著改变结果
- 格式类要求可能被 stream transport 限制污染

因此，EvoScientist 不适合作为 LIFT 的“通用 Agent baseline/evolved runtime”与通用助理任务直接横向比较。

## 更合理的定位

建议将 EvoScientist 在 LIFT 中定位为：

> **科研/项目执行型垂直 Agent runtime，用于评测垂直工作流 Agent 在 warmup 后的记忆、工具使用、实验流程复用和项目执行改进。**

适合它的 suite 应该围绕：

- 文献/网页调研
- 实验计划制定
- 研究假设拆解
- 数据处理脚本
- benchmark 结果分析
- 项目报告生成
- 多步骤工具调用
- memory / AutoSkills / MCP 能力复用

不建议作为主评测集的 suite 类型：

- 个人邮件风格偏好
- 家庭购物清单
- 轻量日程安排
- 强“不要问问题，直接给最终答案”的短文本生成
- 依赖换行保真的 Markdown 格式检查

## Suite 设计建议

如果仍希望保留 EvoScientist 的 integration check，应采用专门 suite，而不是复用通用 personal assistant suite。

建议规则：

1. **每个 holdout 只对应一个 warmup 偏好**

   避免 H1 同时引用 W1/W4、H2 同时引用 W2/W3 这类交叉偏好。

2. **避免“改一下/补进去/修改”措辞**

   judge 或 expected_result 中尽量使用：

   ```text
   请直接重新给出完整答案，不需要修改文件，也不需要索要文件路径。
   ```

3. **减少占位符敏感判据**

   如果需要最终答案，应在 query 中明确：

   ```text
   不要使用占位符，信息不确定时使用合理假设。
   ```

4. **避免换行保真判据**

   不使用“每条单独一行”“必须 Markdown 列表真实分行”等要求。

5. **贴近科研/项目执行场景**

   例如：

   - warmup：记住某类实验记录格式
   - holdout：生成另一个实验的记录模板
   - warmup：记住文献调研摘要结构
   - holdout：对新论文/网页做同结构摘要
   - warmup：记住数据处理脚本风格
   - holdout：生成新数据集处理脚本

## 结论

EvoScientist 在 LIFT 中可以作为一个有价值的垂直 runtime，但不适合作为通用 Agent 评测对象。

它的失败或拉扯并不等同于“agent 不会做任务”，而是反映出：

- runtime 交互协议偏 workspace/project execution
- 产品先验偏科研与项目流程
- 输出风格偏模板化和多版本方案
- 对 judge 增量纠错的解释方式不同于通用 chat agent
- stream-json transport 对格式类任务存在限制

因此，LIFT 中对 EvoScientist 的评测应采用专门的垂直 suite，并在报告中避免把它与通用个人助理型 runtime 做不加区分的横向比较。
