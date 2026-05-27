### query

我想做一套测试 Agent 能不能自己学会历史要求的评测集。请你综合调研一下，并给我一版可直接开工的设计。材料都在 q5_materials 里，结果放到 result/result_q5。

### 要求

1. 最终产物包含 final_design_report.md、source_evidence_table.csv、task_blueprint.md、open_questions.md。
2. 必须同时覆盖公开 benchmark/数据集、学术论文、产品或社区实践三类来源；不能只做网页摘要。
3. source_evidence_table.csv 至少包含 source_type、name、url_or_identifier、date_checked、claim、supports_which_design_decision、confidence、limitation。
4. task_blueprint.md 需要给出至少 3 个任务类型，每类 Q1-Q5，且 Q5 至少复用前面任务约 50% 的要求，同时 query 更模糊。
5. final_design_report.md 必须明确：评测目标、被测能力、数据结构、任务难度递进方法、评分维度、轨迹评分方法、风险与反作弊策略。
6. 对价格、版本、仓库活跃度、论文年份等动态信息必须标注检索日期；不确定时写明不确定，不得猜测。
7. 报告风格要偏执行方案，不写空泛愿景；每个设计建议都要能回到证据表或任务蓝图。

### 轨迹要求

1. 综合检索：同时检索 benchmark/数据集、论文、产品或社区实践三类来源，并记录日期。
2. 证据组织：将所有关键 claim 写入 source_evidence_table.csv，并映射到具体设计决策。
3. 方案生成：根据证据形成评测集结构、难度递进、评分维度和反作弊策略。
4. 历史要求迁移：在 task_blueprint.md 中显式设计 Q1-Q4 的要求积累，以及 Q5 对这些要求的复用与模糊化。
5. 不确定性处理：将无法确认或需要人工决策的问题集中写入 open_questions.md。
