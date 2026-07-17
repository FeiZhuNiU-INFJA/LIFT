# Debug Session: evosci-stream-format

Status: OPEN

## Symptom

`integration_check.json` 的 W3 在 work/judge 循环中卡住。Judge 反复反馈日程 Markdown 无序列表“所有条目挤在同一行，`-` 后没有空格”，work agent 反复确认记住格式或重新生成日程，但输出仍被 judge 判为格式错误。

## Scope

Runtime: `evoscientist`

Observed run: `lift-runid-evosci-integrate-c2`

Relevant code path:

- `src/lift/adapters/evoscientist/chat_agent.py::_extract_done_response`
- `src/lift/eval/run_task.py` work result -> judge input

## Hypotheses

1. `done` event 没有顶层 `response` / `content`，导致 `_extract_done_response` 回落到 `text_chunks`。
2. `text` event 是 token / fragment 级事件，片段本身不含必要空格或换行，当前 `"".join(text_chunks)` 造成粘连。
3. 完整回复存在于嵌套字段（如 `done.data.response` / `done.result.content` / message-like payload），现有解析漏取。
4. Stream 中存在更合适的 final / assistant event，应优先读取该事件而不是拼 `text`。
5. 原始 EvoScientist stream 本身已经粘连，则问题在 EvoScientist CLI 输出层，不在 LIFT parser。

## Evidence Plan

1. 不修改业务逻辑，直接运行 EvoScientist CLI 采集原始 stream JSONL。
2. 分析 event type、字段结构、`text` chunk 边界、`done` event payload。
3. 基于证据选择最小修复。
4. 通过单测和一次最小 runtime prompt 验证 pre/post 差异。

## Evidence: Raw Stream Probe

Command output files:

- `logs/debug-evosci-stream-format/raw.jsonl`
- `logs/debug-evosci-stream-format/raw.stderr`

Probe prompt:

> 请用 Markdown 无序列表列出下周去上海出差三天的日程。要求每条以 - 加空格开头，每条单独一行。

Observed event types:

- `tool_selection`: 1
- `text`: 202
- `usage_stats`: 1
- `done`: 1

Key evidence:

- First text events are `"-"`, `"7"`, `"月"`, `"2"`, `"1"`; no `"- "` text event appears.
- The final `done` event contains both `content` and `response`, but both are already glued:
  `-7月21日（周一）上午...-7月21日（周一）下午...`
- No `final` / `assistant` / `message` / `agent_end` event exists in the JSONL stream.
- `stderr` contains startup / warning / resume-hint only; it does not contain a formatted assistant answer.

Hypothesis status:

1. `done` missing `response/content`: rejected. `done.response` exists.
2. `text` chunks fallback causes gluing: rejected for this run. Gluing is already present in `done.response`.
3. Complete reply exists in nested field: rejected for this stream. No nested complete message was present.
4. Better final / assistant event exists: rejected for this stream.
5. Raw EvoScientist stream itself is glued: confirmed.

## Evidence: Whitespace Preservation Probe

Probe prompt:

> 请只输出下面三行，不要解释：
> A B
> - item one
> - item two

Observed result:

- Text chunks: `['A', ' B', '-', ' item', ' one', '-', ' item', ' two']`
- Joined / done response: `A B- item one- item two`

Interpretation:

- Ordinary spaces are preserved by the stream (`' B'`, `' item'`).
- Newlines were not produced in the raw stream / final response.
- Therefore W3's `-2026...` pattern is not caused by `_extract_done_response` stripping spaces. The model / EvoScientist run generated a compact answer without valid Markdown line breaks, and the judge correctly rejects it.

## Notes

No business logic modifications have been made in this debugging session yet.
