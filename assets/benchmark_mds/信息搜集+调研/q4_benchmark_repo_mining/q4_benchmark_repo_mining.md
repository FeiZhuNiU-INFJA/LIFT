### query

请基于 q4_materials/benchmark_seeds.md 中列出的线索，搜集可用于“Agent 技能学习/偏好迁移评测”的开源 benchmark 或数据集，并整理成可复用任务灵感库。最终产物保存到 result/result_q4。

### 要求

1. 最终产物包含 benchmark_inventory.md、benchmark_inventory.csv、task_idea_bank.md。
2. benchmark_inventory.csv 字段至少包含 name、repo_or_dataset_url、license、task_format、materials_format、evaluation_method、strength、weakness、reuse_idea、last_activity_date。
3. 必须检查仓库或数据集的 license/使用限制；不清楚时标注 unknown，不得默认可商用。
4. task_idea_bank.md 需要按信息搜集、数据分析、代码自动化、办公文档四类输出可改造任务点。
5. 每个可改造任务点必须说明：可验证产物、可隐藏到历史要求中的偏好、最终测试可如何模糊化。
6. 不要只罗列链接，要解释它们为什么适合或不适合本评测集。

### 轨迹要求

1. 开源检索：从 benchmark_seeds.md 出发检索 GitHub、HuggingFace、论文页面或官方项目页。
2. 元数据抽取：记录 license、任务格式、材料格式、评测方法、最后活跃时间等可复用信息。
3. 任务转化：把 benchmark 的结构转化为本项目可用的任务灵感，并说明可验证性。
4. 合规检查：对 license 和使用限制做显式记录，避免默认可复用。
