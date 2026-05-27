### query

我想让一个自进化 Agent 每轮都能重新加载自己的代码。请你写一个最小可运行的热加载器 demo，材料里有初始说明，结果保存到 result/result_q4。

### 要求

1. 最终产物包含 loader.py、agent_impl.py、protocol.md、README.md、logs/example_run.log。
2. loader.py 必须保持稳定，不允许被 agent_impl.py 修改；每轮从文件系统重新加载 agent_impl.py。
3. agent_impl.py 每轮可以输出一个 evolution_patch 或 next_action，但只有当它显式输出 status=evolution_done 时，loader 才进入下一轮重载。
4. 需要提供暂停接口和人工干预信息注入接口，可以用 CLI 命令、文件信号或简单 HTTP 接口实现，但必须写清楚。
5. 必须有安全边界：禁止删除工作区根目录外文件；写入操作默认限制在 workspace/ 子目录；支持 dry-run。
6. README.md 要解释生命周期：加载 -> 执行 -> 产出进化信号 -> 重载 -> 下一轮。

### 轨迹要求

1. 架构设计：将稳定 loader 与可变 agent_impl 分离，定义二者通信协议。
2. 热加载实现：每轮从磁盘重新 import 或执行 agent_impl.py，避免只使用内存旧版本。
3. 控制接口：实现暂停和人工干预注入机制，并在 protocol.md 中说明。
4. 安全约束：限制文件操作范围，加入 dry-run 和日志，避免自进化误删关键文件。
