### query

请分析 q4_materials/agent_request_logs.csv，找出 Agent 调用链路里的延迟和错误异常，并输出一个工程排查报告。结果保存到 result/result_q4。

### 要求

1. 最终产物包含 log_anomaly_report.md、endpoint_metrics.csv、anomaly_cases.csv、figures/latency_by_endpoint.png。
2. endpoint_metrics.csv 至少包含 endpoint、request_count、success_rate、p50_latency_ms、p95_latency_ms、avg_retry_count、top_error_type。
3. 异常规则至少包括：status_code 非 2xx、latency_ms 超过整体 P95、retry_count >= 2；命中的请求写入 anomaly_cases.csv。
4. 报告必须给出工程排查顺序：先看限流/超时/服务端错误，再看是否集中在某 endpoint 或时间段。
5. 如果样本太少，必须说明统计指标不稳定，只能作为排查线索。
6. 不要只写“优化性能”，要给出可执行动作，如重试退避、限流保护、缓存、超时参数、报警阈值。

### 轨迹要求

1. 日志解析：读取请求日志并校验时间、状态码、延迟、重试次数字段类型。
2. 指标聚合：按 endpoint 计算成功率、延迟分位数、平均重试次数和主要错误类型。
3. 异常检测：按非 2xx、超过 P95、retry_count >= 2 三类规则筛选异常请求。
4. 工程归因：根据错误类型、endpoint 和时间集中性提出排查顺序与可执行优化动作。
