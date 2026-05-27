### query

用 `q2_materials/` 做 2026-05-12 至 2026-05-18 渠道周报，保存到 `result/result_q2`。

### 要求

1. 指标口径与 Excel 读法以 `metric_definitions.txt` 为准；从 `orders_detail.xlsx`、`channel_mapping.xlsx` 默认工作表读取（首行表头）；金额保留 2 位小数，订单数为整数。
2. 需将 `orders_detail.xlsx` 的 `channel_code` 与 `channel_mapping.xlsx` 关联，输出字段必须包含：渠道名称、渠道类型、本周GMV、本周回款、本周订单数。
3. 仅统计 `orders_detail.xlsx` 中 `status=paid` 的订单。
4. 产出 Markdown 文件开头第一行必须为：`日期：YYYY-MM-DD`（任务执行日当天）。
5. 文档需用二级标题「按渠道周汇总」，其下按 `channel_type`（直销/渠道/电商）分组展示表格，组内按本周 GMV 降序排列。
6. 需增加「渠道结构」小节：计算各渠道 GMV 占比（%，保留 1 位小数），并指出占比最高的渠道。
7. 保存结果时不要写开场白、结束语。
8. 若关联后某 `channel_code` 在 mapping 中缺失，该渠道单独列入「未映射渠道」分组，不得丢弃订单。

### 轨迹要求

1. 需进行 Excel 数据解析操作：打开 `orders_detail.xlsx` 读取默认工作表，筛选已支付订单，按渠道聚合 GMV、回款与订单数。
2. 需进行 Excel 数据关联操作：将订单表 `channel_code` 与 `channel_mapping.xlsx` 匹配，补全渠道名称与渠道类型，支撑分组展示。
3. 需进行指标计算操作：在渠道汇总基础上计算 GMV 占比与 Top 渠道，服务于「渠道结构」小节。
4. 需进行文档生成操作：按渠道类型分组输出 Markdown 表格与占比说明，保存至 `result/result_q2` 文件夹。
