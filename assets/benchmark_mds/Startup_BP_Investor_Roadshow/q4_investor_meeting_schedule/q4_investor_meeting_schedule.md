### query

我下周（5/26–6/6）要开始见投资人。用工作区 `q4_materials/` 排会议日程，并写第 10 页路演周幻灯片要点，保存到 `result/result_q4`。

### 要求

1. 投资人来自 `investor_pipeline.xlsx`，6 人各至少 1 场；约束见 calendar_blackouts、meeting_rules。
2. **排期文稿**（Markdown）：第一行 `日期：YYYY-MM-DD`；二级标题：「排期总览」「会议日程表」「议程与准备」「排期说明」；日程表列：日期 | 开始 | 结束 | 投资人 | 机构 | 形式 | 优先级 | 时区备注；P0 优先 5/26–5/30 上午；议程含四段时长；会前 24h 交付 **roadshow_deck.pptx**（见 meeting_rules）。
3. **幻灯片要点**：产出 `slide_pages_10.txt`，按 `deck_pages_q4.txt` 写第 10 页，数据与本次日程表一致。
4. 本任务不生成 `.pptx`；无开场白/结束语。

### 轨迹要求

1. 需进行 Excel 数据解析与约束求解操作：生成合法会议时间表。
2. 需进行日程编排操作：为每场写议程与 pptx 会前提醒。
3. 需进行文档生成操作：输出排期 Markdown 与 `slide_pages_10.txt`，保存至 `result/result_q4` 文件夹。
