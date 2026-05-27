### query

请使用 q2_materials/topic_brief.md 中的研究主题，做一份近三年学术论文调研，帮助判断这个方向是否值得做成评测集任务。最终产物保存到工作区 result/result_q2。

### 要求

1. 最终产物包含 literature_landscape.md、paper_matrix.csv、search_log.md。
2. paper_matrix.csv 至少包含 title、year、venue_or_source、problem、method、dataset_or_material、metric、main_finding、limitation、url_or_doi 字段。
3. 优先检索论文、预印本、会议/期刊页面、官方数据集页面；博客只能作为补充，不得作为主要依据。
4. literature_landscape.md 必须输出：研究问题拆解、主流方法谱系、数据集/评测指标、可迁移到 Agent 评测集的任务点、不可验证或难评测的点。
5. 如果论文之间结论冲突，需要单独列出“冲突与可能原因”，不能强行平均。
6. 引用时不要只给链接，要说明该来源支撑哪一个判断。

### 轨迹要求

1. 学术检索：基于 topic_brief.md 提炼英文检索式，并优先检索近三年论文和数据集页面。
2. 结构化阅读：逐篇抽取问题、方法、数据、指标、结论、局限，写入 paper_matrix.csv。
3. 综合归纳：把论文证据转化为可评测任务点，并标记哪些点难以客观验证。
4. 冲突处理：遇到结论不一致时，比较数据集、设置、时间和样本差异，写入报告。
