### query

请使用 q1_materials/call_records.csv 做一份客服热线质检数据分析，输出核心指标、异常线索和一张汇总图表。最终产物保存到工作区 result/result_q1。

### 要求

1. 最终产物至少包含 analysis_report.md、metrics_table.csv、cleaned_call_records.csv、figures/summary.png。
2. 核心指标必须包含：总通话量、总通话时长、平均通话时长、平均情感分、负面通话占比、转派工单率、各业务域占比、各接线员接听量与平均情感分。
3. 所有时长在报告中显示为 HH:MM:SS，同时在 metrics_table.csv 中保留秒数原始字段，便于复核。
4. 负面通话定义为 sentiment_score <= 0.3；中性为 0.3 < score < 0.7；正面为 score >= 0.7。
5. 报告必须写清楚数据清洗规则、缺失值处理、异常通话判断逻辑；不能只给结果。
6. 图表要能独立读懂，标题、坐标轴、图例或注释完整。

### 轨迹要求

1. 数据读取：读取 call_records.csv，检查字段、类型、缺失值和重复 Business_Number。
2. 指标计算：按要求计算整体、业务域、接线员三个层级指标，并生成 metrics_table.csv。
3. 可视化：生成至少一张能展示业务域或情感分布的 summary.png。
4. 清洗记录：将清洗后的明细保存为 cleaned_call_records.csv，并在报告中解释清洗逻辑。
