### query

帮我做一个最小自进化 Agent 工作区，不要搞太复杂，但要能跑、能改自己、能评估自己，结果放到 result/result_q5。

### 要求

1. 最终产物包含 loader.py、agent_impl.py、evaluator.py、workspace/、protocol.md、README.md、tests/、logs/example_run.log。
2. 必须采用稳定 loader + 可变 agent_impl 的结构；loader 每轮从磁盘重载 agent_impl，不能一直使用启动时内存中的旧代码。
3. agent_impl 可以提出代码修改，但只有显式输出 status=evolution_done 后，loader 才能开始下一轮并加载新版本。
4. 需要提供暂停进化、恢复进化、人工干预信息注入三个控制入口；入口形式可以是 CLI、文件信号或轻量 HTTP，但必须可运行。
5. 必须实现 evaluator.py，对每轮 agent 产物给出结构化评分 JSON，至少包含 success、score、issues、next_suggestion。
6. 必须包含安全边界：dry-run、工作区路径限制、修改前备份、异常回滚、日志记录；禁止默认执行危险 shell 命令。
7. README.md 必须给出一条从安装、运行、注入人工提示、触发进化、查看日志到运行测试的完整路径。
8. tests/ 至少覆盖：热加载生效、暂停/恢复、人工干预注入、错误回滚、evaluator 输出格式。

### 轨迹要求

1. 系统拆分：实现 loader、agent_impl、evaluator、workspace、tests 的最小闭环结构。
2. 热加载与进化协议：定义并实现 status=evolution_done 后重载新 agent_impl 的生命周期。
3. 控制接口：提供暂停、恢复、人工干预注入，并用 README 演示。
4. 自评估：实现 evaluator.py 输出结构化 JSON，并把评分反馈给下一轮可用。
5. 安全工程：加入 dry-run、路径限制、修改前备份、异常回滚和日志，确保自修改过程可控。
6. 测试验证：编写覆盖热加载、控制接口、回滚和 evaluator 格式的测试。
