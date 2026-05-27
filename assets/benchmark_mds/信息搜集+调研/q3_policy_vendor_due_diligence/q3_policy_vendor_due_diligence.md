### query

q3_materials/vendor_candidates.csv 里列了几个模型平台候选项。请做一份企业接入前的合规与采购预调研，重点看数据安全、工具调用、价格和 SLA。最终产物保存到 result/result_q3。

### 要求

1. 最终产物包含 vendor_due_diligence.md、vendor_compare.xlsx 或 vendor_compare.csv、risk_register.md。
2. 对每个平台至少覆盖：模型能力、工具/插件生态、私有化或专有网络支持、数据保留/训练声明、价格口径、限流/SLA、文档成熟度、接入风险。
3. 价格、SLA、限流、数据保留政策必须引用官方文档或合同/帮助中心页面；若没有公开信息，标为“未公开”。
4. risk_register.md 需使用 risk_id、risk、evidence、impact、likelihood、mitigation、owner 字段。
5. 报告必须区分“公开可证实事实”和“基于资料的采购判断”，不得把主观推荐写成事实。
6. 输出一个最终建议：推荐优先试点、谨慎试点、不建议当前接入，并说明触发条件。

### 轨迹要求

1. 资料检索：以 vendor_candidates.csv 为候选清单，优先查官方文档、价格页、安全/隐私政策、开发者文档。
2. 证据校验：对价格、SLA、限流和数据政策类信息必须二次确认来源时间，并在表格中标注更新时间。
3. 风险登记：把不确定、缺失或可能影响上线的信息转化为 risk_register.md。
4. 采购判断：基于证据表形成分级推荐，显式说明判断前提。
