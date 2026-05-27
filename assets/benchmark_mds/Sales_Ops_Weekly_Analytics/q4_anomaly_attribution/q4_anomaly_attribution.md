### query

帮我看下 2026-05-12 到 2026-05-18 这周销售有没有明显异常，产品线和大区都查一下，有问题的说清原因。材料在 `q4_materials/`，保存到 `result/result_q4`。

### 要求

1. 指标口径与 Excel 读法以 `metric_definitions.txt` 为准；业务表均为 `.xlsx`，默认工作表、首行表头；异常判定阈值以 `anomaly_thresholds.txt` 为准。
2. 产品线维度：用 `sales_daily.xlsx` 汇总周 GMV，与 `weekly_baseline.xlsx` 中对应 `product_line` 的 `baseline_gmv` 对比；超过 ±15% 记为异常。
3. 大区维度：用 `sales_by_dept.xlsx` 汇总周 GMV，与 `weekly_baseline.xlsx` 中对应 `department` 的 `baseline_gmv` 对比；超过 ±12% 记为异常。
4. 产出 Markdown 文件开头第一行必须为：`日期：YYYY-MM-DD`（任务执行日当天）。
5. 文档结构须包含二级标题：「异常概览」「产品线异常明细」「大区异常明细」「综合判断」。
6. 每条异常须包含：对象名称、本周实际 GMV、对比基准、偏差百分比（保留 1 位小数）、可能原因（1-2 条，仅基于材料推断）。
7. 若无任何维度触发异常，在「异常概览」中明确写「本周未发现达到阈值的异常」。
8. 金额保留 2 位小数；保存结果时不要写开场白、结束语。
9. 「综合判断」需用 3-5 句话归纳：异常集中在哪些维度、对下周运营关注的 1 条建议（建议须与异常分析结论一致）。

### 轨迹要求

1. 需进行 Excel 数据解析操作：分别从 `sales_daily.xlsx`、`sales_by_dept.xlsx` 读取默认工作表并聚合周 GMV，作为异常检测输入。
2. 需进行 Excel 基线对比操作：读取 `weekly_baseline.xlsx` 默认工作表，结合 `anomaly_thresholds.txt` 计算相对 baseline 的偏差并标记异常项。
3. 需进行归因分析操作：对每条异常结合材料字段给出可能原因，避免无依据的外推。
4. 需进行文档生成操作：按固定四段结构输出报告，保存至 `result/result_q4` 文件夹。
