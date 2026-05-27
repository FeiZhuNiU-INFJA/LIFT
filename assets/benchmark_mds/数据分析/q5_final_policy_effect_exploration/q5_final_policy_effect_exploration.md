### query

我现在想看看新流程上线后到底有没有变好。q5_materials 里有一份汇总表，帮我做一版能给老师看的探索性分析，结果放 result/result_q5。

### 要求

1. 最终产物包含 final_exploratory_report.md、analysis_dataset.csv、metric_summary.csv、missingness_report.csv、figures/monthly_trends.png、analysis.py。
2. 必须先检查字段含义和缺失率，尤其是费用、住院天数、负面通话率、工单转派率、缺失支付字段比例；缺失严重的指标不能作为主结论。
3. 报告必须区分探索性对比、趋势线索和政策效果结论；当前数据不足时不能宣称因果效果。
4. 指标至少包括：case_count、avg_total_cost、avg_length_of_stay、negative_call_rate、ticket_rate，并按 pre/post 分组汇总。
5. 图表至少展示月度趋势，并在图中或图注标记上线前后阶段。
6. 必须说明是否适合做 ITS、PSM+DID、混合效应模型；若样本月份少或个体级变量不足，要解释为什么暂不适合。
7. 所有时长/费用/比例字段的单位要在报告和表格中写清楚，脚本可复现，输出路径固定到 result/result_q5。

### 轨迹要求

1. 数据质控：读取 summary_monthly.csv，检查字段含义、单位、缺失率和 period 分组是否合理。
2. 探索性汇总：按 pre/post 计算病例量、费用、住院天数、负面通话率、工单率的均值或总量。
3. 趋势可视化：绘制月度趋势图并标记上线前后，避免用缺失严重字段支撑主结论。
4. 方法适配判断：根据样本月份、是否有个体级协变量、是否有对照组，判断 ITS、PSM+DID、混合效应模型是否适合。
5. 复现落盘：保存清洗数据、指标表、缺失报告、图表和 analysis.py 到 result/result_q5。
