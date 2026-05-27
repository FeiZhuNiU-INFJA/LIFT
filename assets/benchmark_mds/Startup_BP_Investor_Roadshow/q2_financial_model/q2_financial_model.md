### query

用工作区 `q2_materials/` 整理 BP 的财务预测与资金用途，并写第 5–7 页幻灯片要点，保存到 `result/result_q2`。

### 要求

1. 口径与 Excel 读法以 `metric_definitions.txt` 为准；从 `financial_model.xlsx` 的 assumptions、monthly_projection 读取，勿编造。
2. 写作与幻灯片风格分别见 `output_style_guide.txt`、`deck_style_guide.txt`。
3. **BP 文稿**（Markdown）：第一行 `日期：YYYY-MM-DD`；二级标题顺序：「关键假设」「月度预测」「Runway 与资金用途」「财务小结」；关键假设表含 assumptions 全部参数行；月度预测表列：月份 | MRR | ARR | Burn | Net cash flow；runway 与资金用途见 funding_use.txt 三项比例；财务小结 3–5 条 bullet。
4. **幻灯片要点**：产出 `slide_pages_05-07.txt`，按 `deck_pages_q2.txt` 填写第 5–7 页，格式同 Q1 的 `---页码---` 块，数字与 Excel 一致。
5. 本任务不生成 `.pptx`。
6. 无开场白/结束语。

### 轨迹要求

1. 需进行 Excel 数据解析与指标计算操作：读取假设与预测表，计算 runway。
2. 需进行规则对齐操作：统一万元精度与幻灯片字数限制。
3. 需进行文档生成操作：输出财务 Markdown 与 `slide_pages_05-07.txt`，保存至 `result/result_q2` 文件夹。
