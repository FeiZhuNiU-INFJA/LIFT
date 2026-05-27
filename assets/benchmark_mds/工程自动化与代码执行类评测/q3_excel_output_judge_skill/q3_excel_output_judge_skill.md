### query

请在 q3_materials/examples/ 的基础上写一个 Excel 产物裁判 skill，用来判断 Agent 生成的表格是否满足用户 query。最终结果保存到 result/result_q3。

### 要求

1. 最终产物包含 skill.md、judge.py、rubric.json、README.md、tests/test_cases.json。
2. 裁判不能依赖隐藏真值文件，只能根据 query、输入表结构、产出表结构和产出内容判断是否符合意图。
3. 评分维度至少包含：意图满足度、字段/格式正确性、计算逻辑合理性、数据完整性、异常处理、可解释性。
4. judge.py 需要输出 JSON，字段包括 passed、score、dimension_scores、issues、suggested_fix。
5. 必须设计至少 5 个测试用例，覆盖正确产物、字段缺失、公式错误、格式错误、过度生成。
6. skill.md 要写清楚裁判原则：先理解 query，再检查结构，再抽查计算，不确定时降置信度而不是武断通过。

### 轨迹要求

1. 需求归纳：从 Excel Agent 产物评测场景中提炼不依赖真值的判定维度。
2. 规则实现：将 rubric.json 的维度映射到 judge.py 的评分逻辑，并输出结构化 JSON。
3. 测试构造：在 tests/test_cases.json 中覆盖正确和典型错误场景。
4. 文档化：在 skill.md 和 README 中说明裁判流程、输入输出格式和局限性。
