### query

请根据 q2_materials/jobs.jsonl 写一个批量请求大模型的调度脚本。这里不需要真的调用外部 API，可以用 mock_client 模拟。结果保存到 result/result_q2。

### 要求

1. 最终产物包含 batch_runner.py、mock_client.py、README.md、outputs/predictions.jsonl、logs/run_summary.json。
2. 脚本必须支持并发执行、失败重试、指数退避、QPS 限制、断点续跑。
3. 输入 jobs.jsonl 每行一个任务，输出 predictions.jsonl 必须保留 job_id、input、output、status、attempts、error、latency_ms。
4. 断点续跑时不得重复处理已经成功的 job_id；重复 job_id 需要记录 warning。
5. README.md 要说明如何配置 concurrency、qps、max_retries、resume。
6. 代码要模块化，核心逻辑不能全部堆在 main 函数里。

### 轨迹要求

1. 任务读取：解析 jobs.jsonl，校验 job_id 唯一性并记录重复项。
2. 调度实现：实现并发、QPS 限制、失败重试和指数退避，并用 mock_client 模拟 API。
3. 断点续跑：读取已有 predictions.jsonl，跳过已成功 job_id。
4. 运行汇总：输出 run_summary.json，包含成功数、失败数、平均延迟、总耗时和 warnings。
