### query

请用 q2_materials/dip_cases.csv 试做一次心内科 DIP 数据月度对比分析，重点看 2025 与 2026 年初的费用和住院天数变化。结果保存到 result/result_q2。

### 要求

1. 最终产物包含 dip_analysis_report.md、monthly_metrics.csv、missingness_report.csv、figures/trend_total_cost.png。
2. 必须区分 total_cost、dip_paid_amount、prepay_total 三个费用字段，不允许混用；如果字段缺失严重，要单独说明不能用于主结论。
3. monthly_metrics.csv 至少包含 year_month、case_count、avg_total_cost、median_total_cost、avg_length_of_stay、missing_dip_paid_rate、missing_disease_code_rate。
4. 报告必须先做缺失情况诊断，再给趋势结论；不能在字段缺失明显时直接比较。
5. 如果 2026 只有 1-2 月数据，结论必须写成“探索性分析”，不得写成政策效果结论。
6. 所有代码和中间结果要可复现，建议输出 analysis.py 或 notebook。

### 轨迹要求

1. 缺失诊断：检查费用、病种代码、初始分值、住院天数字段的缺失比例，并输出 missingness_report.csv。
2. 指标计算：按 year_month 聚合病例数、次均费用、中位费用、平均住院天数和关键缺失率。
3. 趋势可视化：绘制 total_cost 月度趋势图，并避免把缺失字段作为主趋势。
4. 结论约束：根据样本月份和缺失情况限定结论强度，明确探索性分析边界。
