### query

请基于 q1_materials/source_docs/ 里的少量示例文档，写一个“样本包构造器”脚本，把原始资料抽样成多个代表性 sample package。最终代码和输出保存到 result/result_q1。

### 要求

1. 最终产物包含 sample_package_builder.py、README.md、config.example.json、至少 2 个生成出来的 sample_package 示例。
2. 脚本必须保留原始目录层级，但每个文件只抽取部分内容；不得直接完整复制原文件充当抽样结果。
3. 抽样策略不能固定只取前三段，需要结合文件长度、标题、关键词或随机种子做可解释抽样。
4. 所有原始文件应尽量在多个 sample package 中被覆盖到；若未覆盖，需在 coverage_report.json 中说明。
5. 脚本必须支持 CLI 参数：--source_dir、--output_dir、--num_packages、--seed、--max_chars_per_file。
6. README.md 要写明运行方式、输出结构、抽样策略、已知限制。

### 轨迹要求

1. 文件遍历：递归读取 materials/source_docs/，记录原始目录结构和文件长度。
2. 抽样实现：按可解释策略对每个文件抽取片段，生成多个 sample package，避免完整复制。
3. 覆盖统计：计算每个原始文件是否被抽中，输出 coverage_report.json。
4. CLI 与文档：实现命令行参数和 README，保证可复现实验。
