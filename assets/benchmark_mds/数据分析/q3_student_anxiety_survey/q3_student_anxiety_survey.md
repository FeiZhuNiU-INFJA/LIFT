### query

q3_materials/survey_sample.csv 是研究生焦虑风险调查的小样本试算数据。请做一份描述性分析和建模前数据质量评估，结果放到 result/result_q3。

### 要求

1. 最终产物包含 survey_qc_report.md、descriptive_stats.csv、risk_group_table.csv、figures/gad7_distribution.png。
2. 必须说明 GAD-7 风险分层规则：0-4 最小，5-9 轻度，10-14 中度，15-21 重度；同时计算每组人数和占比。
3. 描述性统计需覆盖人口学变量、睡眠、运动、导师支持、经济压力、GAD-7、PHQ-9。
4. 报告必须区分“描述性发现”和“因果解释”；小样本不得给出确定性因果结论。
5. 建模前评估要说明：样本量是否足够、类别是否稀疏、是否存在共线性风险、是否需要扩大样本。
6. 输出至少 3 条后续正式调查问卷或采样策略建议。

### 轨迹要求

1. 数据质控：检查量表取值范围、缺失、重复 student_id 和明显异常值。
2. 分层统计：根据 GAD-7 规则生成人数和占比，输出 risk_group_table.csv。
3. 描述性分析：按变量类型生成描述性统计，并输出 descriptive_stats.csv。
4. 建模可行性评估：从样本量、类别稀疏、共线性和因果限制角度写入报告。
