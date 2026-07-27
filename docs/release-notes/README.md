# Release Notes

LIFT 项目重要改动的**叙事式复盘**。区别于 `git log`(记录"改了什么") ——
本目录记录:

- **背景与痛点**:为什么必须改
- **断层分析**:问题在哪一层、证据链是什么
- **修复策略**:关键取舍(为什么 A 不为什么 B)
- **影响面**:哪些 runtime / 哪些指标被影响
- **后续与监控**:未来退化风险 + sanity-check 方法

## 何时新增一篇

出现下列**任一**情况就写一篇:

1. 一次改动跨 **≥ 3 个模块**(如 skill + adapter + plugin + backfill)
2. 改动涉及**架构级决策**(引入新契约、拆分/合并模块、调整数据流)
3. 涉及**跨 runtime 差异化**的口径 / 兼容层调整
4. 埋下未来可能踩坑的**兼容/依赖点**(overlay、上游 API 版本绑定等)

**不写**的场景:
- 单文件 bugfix / typo / lint
- 纯 CI / 依赖版本升级
- 单元测试补齐

## 文件命名

```
YYYY-MM-DD-<slug>.md
```

slug 用小写连字符,概括核心主题(不必对应 commit 标题)。

## 单篇模板

```markdown
# YYYY-MM-DD · <标题>

## TL;DR
一段(≤3 句)描述改了什么、影响谁、结果状态。

## 背景
改动前的现象与痛点,列可复现证据。

## 断层分析
定位在哪一层、为什么这里断。附证据文件 / 关键代码位置的链接。

## 修复策略
每一层怎么修 + 关键取舍(为什么 A 不为什么 B)。

## 涉及文件
清单(用 file:/// 链接)。

## 后续与监控
- 什么情况下会退化
- 如何 sanity-check
- 后续待办
```

单篇建议 **≤ 200 行**;更长的技术细节下沉到 `skill/**/docs/` 或 runtime README,
release notes 只讲**故事线**并交叉引用。

## 索引

| 日期 | 标题 | 影响面 |
|---|---|---|
| 2026-07-16 | [Token 5 字段落库全链路修复](2026-07-16-token-5-fields-observability.md) | 4 runtime · 3 层(agent plugin / Langfuse ingestion / backfill) |
| 2026-07-26 | [GenericAgent 1800s 超时根因分析](2026-07-26-genericagent-1800s-timeout-analysis.md) | GenericAgent runtime · paper 结果标注要求 |
