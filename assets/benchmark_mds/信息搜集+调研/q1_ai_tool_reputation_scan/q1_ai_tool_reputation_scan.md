### query

请基于工作区 q1_materials/seed_keywords.md 中给出的关键词，调研“AI 表格/Excel Agent 工具”的公开口碑与核心能力，整理一份简洁报告，并将所有结果保存到工作区 result/result_q1 文件夹。

### 要求

1. 最终产物至少包含：research_report.md、evidence_table.csv、sources.json 三个文件。
2. research_report.md 需要分为：结论摘要、工具能力对比、用户高频表扬点、用户高频抱怨点、适合/不适合场景、风险与不确定性。
3. evidence_table.csv 至少包含 source_type、source_name、url_or_identifier、publish_or_update_date、claim、sentiment、confidence、why_relevant 字段。
4. 信息源必须混合使用官方资料、开发者社区/论坛、产品评测或博客，不允许只用单一来源；每条关键结论都要能在 evidence_table.csv 中找到证据。
5. 涉及价格、功能可用性、产品名称、版本发布时间等可能变化的信息，必须标注检索日期；无法确认时明确写“未确认”，不得猜测。
6. 报告语言要短句、直接、偏产品经理风格；不要堆砌原文，不要照搬营销话术。

### 轨迹要求

1. 信息检索：围绕 materials/seed_keywords.md 的关键词扩展同义词和竞品词，检索官方、社区、评测三类来源。
2. 证据抽取：从每个来源提取可验证 claim，并写入 evidence_table.csv，避免只有结论没有证据。
3. 可信度判断：根据来源类型、发布时间、是否一手资料给出 confidence，并在报告中区分事实、评论和推断。
4. 结果落盘：把报告、证据表和来源 JSON 分别保存到 result/result_q1。
