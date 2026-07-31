"""OpenHuman transcript → Langfuse ``openhuman-plugin`` trace 推送。

OpenHuman core 本身**不集成** Langfuse SDK / OTel exporter（binary 里仅有
``"langfuse"`` 字面量，无 push 通路）。为让 LIFT 后处理侧的 trace 拼装
（``langfuse_trace_stitch._stitch_by_session_id`` + ``LANGFUSE_PLUGIN_TRACE_NAMES``
白名单里的 ``openhuman-plugin``）能正常工作，我们在**宿主端** chat 完成后：

1. ``docker exec`` 读容器内 ``~/.openhuman/users/local/workspace/session_raw/*.jsonl``，
   按 ``_meta.thread_id == session_id`` 过滤出本轮的 orchestrator + subagent transcript；
2. （可选）把拉到的**原始 jsonl 留档到本机** ``dump_dir``，便于离线查看 / 分析
   OpenHuman 那套与 OpenAI 有出入的 schema（``usage={input,output,cached_input}``、
   ``tool_calls`` 藏在 ``extra_metadata.openhuman_turn_usage``、独立 ``reasoning_content``
   等）；
3. 把 messages **归一化成与 GenericAgent 一致的最小形状**（``{role, content}`` +
   assistant 的 OpenAI 风格 ``tool_calls=[{id,type,function:{name,arguments}}]``），
   用 Langfuse Python SDK v4 push 一条 name = ``openhuman-plugin`` 的
   ``as_type='agent'`` root observation，附带：
     - ``propagate_attributes(session_id=..., tags=[run_tag, session_id])`` — 与
       LIFT pre-chat span 走同一 ``session_id`` / ``tags``，后处理据此配对
     - ``metadata`` 按 ``LangfusePluginTraceMetadata`` 形状写入（success /
       message_count / tool_roundtrips / tool_call_blocks / tool_names_distinct /
       messages），messages 即上面归一化后的 GA 风格数组
     - 每个 assistant iteration 挂一个 ``as_type='generation'`` 子 observation，
       ``usage_details`` 从 ``openhuman_turn_usage.usage`` 取（input / output /
       cached_input）
     - 每个 ``tool_calls`` 条目 + 后续 ``role=tool`` 结果挂一个 ``as_type='tool'``
       子 observation

OpenHuman 每题（每 thread_id）所有轮次的对话都累积在同一批 ``session_raw/*.jsonl``
里，因此每次 chat 后**全量重读**得到的就是"截至当前轮的整段会话"——与 GA 修复后
"每轮 push 一条含全量 transcript 的 root trace"语义一致，后处理 ``TranscriptChampion``
取最晚一条即完整对话。

这份模块是**尽力而为**的：Langfuse 未配置或 push 失败都只 warning，不影响 chat 主路径。
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.config import CONFIG, LOGGER

# 容器内 transcript 目录（默认 user = local）。openhuman-core 会在这里按
# ``<epoch>_<agent>[__<epoch>_<agent>_<taskid>].jsonl`` 组织每个 agent 的对话
# 记录，_meta 首行含 ``thread_id``，我们据此过滤当前会话。
_CONTAINER_SESSION_RAW_DIR = "/root/.openhuman/users/local/workspace/session_raw"

# 单个 transcript 文件大小硬上限，防止极端超长会话把内存打爆。10MB 内存串
# 化足够表达一个 phase 的所有 iteration。
_MAX_TRANSCRIPT_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class ParsedTranscript:
    """从容器内单个 ``session_raw/*.jsonl`` 拉出来的最小结构。"""

    path: str
    meta: dict[str, Any]  # 首行 ``_meta``
    messages: list[dict[str, Any]]  # 其余每行一个 role message
    raw_text: str = ""  # 原始 jsonl 全文（留档到本机用）


def _docker_exec_capture(container_name: str, argv: list[str]) -> str:
    """``docker exec`` 执行命令并 UTF-8 decode stdout（stderr 走日志）。"""
    cmd = ["docker", "exec", container_name, *argv]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, timeout=30, check=False
        )
    except subprocess.TimeoutExpired:
        LOGGER.warning("[openhuman langfuse] docker exec timeout: %s", " ".join(cmd))
        return ""
    if proc.returncode != 0:
        # 目录不存在等情况 ls 会 exit=2；只 debug 一下，不 warning。
        LOGGER.debug(
            "[openhuman langfuse] docker exec rc=%d cmd=%s stderr=%s",
            proc.returncode, " ".join(cmd),
            proc.stderr.decode("utf-8", errors="replace")[:400],
        )
        return ""
    return proc.stdout.decode("utf-8", errors="replace")


def _list_container_transcripts(container_name: str) -> list[str]:
    """列出容器内当前所有 session_raw 文件绝对路径。"""
    out = _docker_exec_capture(
        container_name,
        ["sh", "-c", f"ls -1 {_CONTAINER_SESSION_RAW_DIR}/*.jsonl 2>/dev/null || true"],
    )
    return [line.strip() for line in out.splitlines() if line.strip()]


def _read_container_file(container_name: str, path: str) -> str:
    """``docker exec cat`` 读一个文件内容。"""
    out = _docker_exec_capture(container_name, ["cat", path])
    if len(out) > _MAX_TRANSCRIPT_BYTES:
        LOGGER.warning(
            "[openhuman langfuse] transcript %s exceeds %d bytes; truncating",
            path, _MAX_TRANSCRIPT_BYTES,
        )
        out = out[:_MAX_TRANSCRIPT_BYTES]
    return out


def _parse_transcript_text(path: str, raw: str) -> ParsedTranscript | None:
    """解析单个 jsonl 文本：首行 ``_meta``，其余每行一个 role 消息。"""
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return None
    try:
        meta_obj = json.loads(lines[0])
    except json.JSONDecodeError:
        LOGGER.debug("[openhuman langfuse] non-JSON first line in %s", path)
        return None
    meta = meta_obj.get("_meta") if isinstance(meta_obj, dict) else None
    if not isinstance(meta, dict):
        return None
    messages: list[dict[str, Any]] = []
    for ln in lines[1:]:
        try:
            row = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("role"):
            messages.append(row)
    messages = _coalesce_assistant_pairs(messages)
    return ParsedTranscript(path=path, meta=meta, messages=messages, raw_text=raw)


def _unwrap_dumped_content(content: Any) -> Any:
    """若 assistant 顶层 ``content`` 被写成整条 message 的 JSON dump，解出内层干净文本。

    OpenHuman 对**带 tool_call 的 assistant 轮次**的"完整版"记录，会把顶层 ``content``
    写成 ``{"content":"<干净文本>","reasoning_content":...,"tool_calls":[...]}`` 的
    JSON 字符串（导致 HTML 轨迹里出现嵌套 content）。这里尝试 ``json.loads`` 并取内层
    ``content``；非该形态则原样返回。
    """
    if not isinstance(content, str):
        return content
    s = content.lstrip()
    if not s.startswith("{") or '"content"' not in s[:60]:
        return content
    try:
        obj = json.loads(content)
    except json.JSONDecodeError:
        return content
    if isinstance(obj, dict) and isinstance(obj.get("content"), str):
        return obj["content"]
    return content


def _tool_call_id_from_content(content: Any) -> str | None:
    """从 tool message 的 JSON 串 content 里提取 ``tool_call_id``（若有）。"""
    if not isinstance(content, str):
        return None
    s = content.lstrip()
    if not s.startswith("{"):
        return None
    try:
        obj = json.loads(content)
    except json.JSONDecodeError:
        return None
    cid = obj.get("tool_call_id") if isinstance(obj, dict) else None
    return cid if isinstance(cid, str) else None


def _coalesce_assistant_pairs(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """折叠 OpenHuman 对同一 assistant 轮次写出的"预览版 + 完整版"两条记录。

    OpenHuman 对带 tool_call 的 assistant 会连续写两行：
      - **预览版**：仅 ``{role, content}``，content 是干净思考文本，但**无 tool_calls**；
      - **完整版**：紧随其后，带 ``extra_metadata`` / ``ts`` / ``tool_calls``，但顶层
        ``content`` 是整条 message 的 JSON dump（嵌套、脏）。

    二者是同一轮 iteration（完整版内层 content == 预览版 content）。不折叠会导致：
    HTML 轨迹里同一轮重复出现两次，且完整版那条显示为嵌套 JSON。

    折叠规则（满足"不重复、不嵌套、内容尽量完善"）：
      - 识别"前一条 assistant 无 extra_metadata/ts（预览版），后一条 assistant 有
        （完整版）"的相邻对 → 合并为**一条**；
      - 合并后保留完整版的全部元信息（tool_calls / usage / ts / extra_metadata），
        但把顶层 ``content`` 覆盖为**干净文本**（优先预览版 content，回退完整版内层
        解包 content）。
    """
    out: list[dict[str, Any]] = []
    i = 0
    n = len(messages)
    while i < n:
        cur = messages[i]
        nxt = messages[i + 1] if i + 1 < n else None
        is_cur_assist = str(cur.get("role") or "") == "assistant"
        is_nxt_assist = nxt is not None and str(nxt.get("role") or "") == "assistant"
        cur_is_preview = is_cur_assist and not (cur.get("extra_metadata") or cur.get("ts"))
        nxt_is_full = is_nxt_assist and bool(nxt.get("extra_metadata") or nxt.get("ts"))
        if cur_is_preview and nxt_is_full:
            merged = dict(nxt)  # 保留完整版元信息（tool_calls/usage/ts/...）
            clean = cur.get("content")
            if not (isinstance(clean, str) and clean.strip()):
                clean = _unwrap_dumped_content(nxt.get("content"))
            merged["content"] = clean
            out.append(merged)
            i += 2
            continue
        # 未配对的完整版单条：也把嵌套 content 解包，避免 HTML 嵌套显示。
        if is_cur_assist and (cur.get("extra_metadata") or cur.get("ts")):
            fixed = dict(cur)
            fixed["content"] = _unwrap_dumped_content(cur.get("content"))
            out.append(fixed)
            i += 1
            continue
        out.append(cur)
        i += 1
    return out


def collect_session_transcripts(
    container_name: str, session_id: str
) -> list[ParsedTranscript]:
    """拉取容器内所有 ``_meta.thread_id == session_id`` 的 transcript。

    返回按容器内路径字典序排序的列表；orchestrator（无 ``__<sub>``）通常排在其
    subagent 之前，符合 chat 时序。
    """
    paths = _list_container_transcripts(container_name)
    hits: list[ParsedTranscript] = []
    for path in sorted(paths):
        raw = _read_container_file(container_name, path)
        parsed = _parse_transcript_text(path, raw)
        if parsed is None:
            continue
        if parsed.meta.get("thread_id") != session_id:
            continue
        hits.append(parsed)
    return hits


def archive_transcripts_to_host(
    transcripts: list[ParsedTranscript],
    dump_dir: Path,
    session_id: str,
) -> None:
    """把拉到的**原始 jsonl** 留档到本机 ``dump_dir/<session_id>/``。

    OpenHuman 的 session_raw jsonl 是"真源数据"，schema 与 OpenAI 有诸多出入
    （usage 键名、tool_calls 藏在 extra_metadata、独立 reasoning_content 等），
    落一份到本机方便离线人肉查看 / 二次分析，不依赖容器还活着。文件名沿用容器内
    basename（含 orchestrator / subagent 区分与 epoch 前缀）。best-effort，失败仅
    warning。
    """
    if not transcripts:
        return
    try:
        target = dump_dir / session_id
        target.mkdir(parents=True, exist_ok=True)
        for t in transcripts:
            name = Path(t.path).name or f"{session_id}.jsonl"
            (target / name).write_text(t.raw_text, encoding="utf-8")
        LOGGER.info(
            "[openhuman langfuse] archived %d transcript(s) for session=%s -> %s",
            len(transcripts), session_id, target,
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning(
            "[openhuman langfuse] failed to archive transcripts for session=%s: %r",
            session_id, exc,
        )


_USER_TS_LINE_RE = re.compile(
    r"^\[(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+\d{4}-\d{2}-\d{2}[^\]]*\]\s*$",
    re.MULTILINE,
)


def _clean_user_content(content: Any) -> Any:
    """把 OpenHuman user message 里的编排脚手架剥掉，只留真实用户请求。

    OpenHuman 会给每条 user message 包裹大量上下文脚手架：
      - ``Current Date & Time: ...`` 头
      - ``## Agent context status`` / ``## Prepared context`` 段
      - ``[context_bundle] ... [/context_bundle]`` 块
      - ``[Context]`` / ``[Request]`` / ``[Orchestrator tools]`` 分节
      - 巨长的可用工具清单

    真实请求恒在一行 ``[<Weekday> YYYY-MM-DD HH:MM:SS GMT+N]`` 时间戳之后
    （LIFT 侧 chat 注入的 ``[Fri ... GMT+8]\\n<query>``；judge 复跑轮则是只有返回的 reason"）。提取规则（按优先级）：

    1. 若存在 ``[Request]`` 分节，取其正文（截到下一个 ``[Section]`` 或
       ``## Heading`` 前），再对正文套规则 2 去时间戳头。
    2. 取最后一个 ``[<Weekday> ... GMT±N]`` 时间戳行之后、下一个 ``[Section]`` /
       ``## Heading`` 之前的正文。
    3. 都不命中则原样返回（避免误伤 sub-agent 的非脚手架内容）。
    """
    if not isinstance(content, str) or not content.strip():
        return content
    text = content

    # 下一分节标题：方括号标题（如 [Orchestrator tools]）或 markdown 标题 (## ...)。
    # **排除** [Weekday ... GMT±N] 时间戳行——它同样是 [..] 形态但正是我们要保留的锚点。
    def _next_section(s: str):
        for mm in re.finditer(r"^(\[[^\]]+\]|##\s.*)\s*$", s, re.MULTILINE):
            if _USER_TS_LINE_RE.match(mm.group(0)):
                continue
            return mm
        return None

    # 1) 优先 [Request] 分节
    m = re.search(r"^\[Request\]\s*$", text, re.MULTILINE)
    if m:
        rest = text[m.end():]
        stop = _next_section(rest)
        body = rest[: stop.start()] if stop else rest
        text = body

    # 2) 取最后一个 [Weekday ... GMT±N] 时间戳行之后的内容
    ts_matches = list(_USER_TS_LINE_RE.finditer(text))
    if ts_matches:
        last = ts_matches[-1]
        rest = text[last.end():]
        stop = _next_section(rest)
        body = rest[: stop.start()] if stop else rest
        cleaned = body.strip()
        if cleaned:
            return cleaned

    # [Request] 命中但无时间戳时，返回 [Request] 正文
    if m:
        cleaned = text.strip()
        if cleaned:
            return cleaned

    # 2.5) sub-agent 委托简报形态：``[Context]\nCurrent Date & Time: ...\n\n<正文>``
    #      （无 [Fri...GMT+8] 时间戳、无 [Request]）。去掉 [Context] 头 + 紧随的
    #      ``Current Date & Time:`` 行，保留 Task/Objective 等真实委托正文。
    ctx_m = re.match(r"^\[Context\]\s*$", content.lstrip().splitlines()[0]) if content.strip() else None
    if ctx_m is not None:
        body = content
        body = re.sub(r"^\[Context\]\s*$", "", body, count=1, flags=re.MULTILINE)
        body = re.sub(
            r"^Current Date & Time:.*$", "", body, count=1, flags=re.MULTILINE
        )
        cleaned = body.strip()
        if cleaned:
            return cleaned

    # 3) 兜底：原样返回
    return content


def _extract_tool_calls_from_assistant(
    msg: dict[str, Any],
) -> list[dict[str, Any]]:
    """从 assistant message 中提取 ``tool_calls``（openhuman 特有 schema）。"""
    extra = msg.get("extra_metadata") or {}
    if not isinstance(extra, dict):
        return []
    usage_block = extra.get("openhuman_turn_usage") or {}
    if not isinstance(usage_block, dict):
        return []
    calls = usage_block.get("tool_calls") or []
    if not isinstance(calls, list):
        return []
    return [c for c in calls if isinstance(c, dict)]


def _normalize_tool_calls_ga_style(
    calls: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """把 OpenHuman ``{name, arguments, id}`` tool_calls 归一成 GA/OpenAI 风格。

    目标形状与 GenericAgent overlay ``_assistant_message_from_response`` 完全一致：
    ``{"id":..., "type":"function", "function":{"name":..., "arguments":...}}``，
    让后处理 ``report_html.build_trajectory_nodes`` 能识别成 tool 节点。
    """
    out: list[dict[str, Any]] = []
    for c in calls:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        if not name:
            continue
        out.append(
            {
                "id": c.get("id"),
                "type": "function",
                "function": {"name": name, "arguments": c.get("arguments")},
            }
        )
    return out


def _normalize_message_ga_style(
    msg: dict[str, Any], agent_name: str
) -> dict[str, Any]:
    """把一条 OpenHuman message 规约成与 GenericAgent 一致的最小形状。

    GA transcript 里每条只有 ``{role, content}``，assistant 额外带 OpenAI 风格
    ``tool_calls``。OpenHuman 原始 message 混入了 ``reasoning_content`` / ``provider``
    / ``model`` / ``usage`` / ``iteration`` / ``ts`` / ``extra_metadata`` 等大量
    非对话字段，且 tool_calls 藏在 ``extra_metadata``——这里统一剥成 GA 形状，只额外
    保留 ``_agent`` 标注（便于 UI 区分 orchestrator / subagent），与 GA 侧 champion
    消费口径对齐。
    """
    role = str(msg.get("role") or "assistant")
    content = msg.get("content")
    if not isinstance(content, (str, list, type(None))):
        content = str(content)
    out: dict[str, Any] = {
        "role": role,
        "content": content if content is not None else "",
        "_agent": agent_name,
    }
    if role == "user":
        # 剥掉 OpenHuman 编排脚手架（[Context]/[Orchestrator tools]/工具清单等），
        # 只留真实用户请求；首轮=task query，复跑轮=judge reason+重试语。
        out["content"] = _clean_user_content(content)
    elif role == "assistant":
        # 兜底解包：极端情况下未被 _coalesce_assistant_pairs 处理到的完整版，
        # 顶层 content 仍是 JSON dump，这里再解一次内层干净文本，杜绝嵌套。
        out["content"] = _unwrap_dumped_content(out["content"])
        calls = _normalize_tool_calls_ga_style(
            _extract_tool_calls_from_assistant(msg)
        )
        if calls:
            out["tool_calls"] = calls
    elif role == "tool":
        # tool result 的 content 是 ``{"content":"<真实输出>","tool_call_id":...}``
        # JSON 串；解出内层输出文本，避免 all_messages 里残留嵌套 JSON。同时透传
        # ``tool_call_id`` 供 report_html.build_trajectory_nodes 识别 / drop。
        out["content"] = _unwrap_dumped_content(out["content"])
        cid = _tool_call_id_from_content(content)
        if cid:
            out["tool_call_id"] = cid
    return out


def _usage_details_from_assistant(msg: dict[str, Any]) -> dict[str, int] | None:
    """``role=assistant`` 的 ``usage`` → Langfuse ``usage_details``。

    OpenHuman schema：``usage = {input, output, cached_input, cost_usd, ...}``；
    映射到 Langfuse 标准键 ``input`` / ``output`` / ``total`` / ``cache_read_input_tokens``
    （参考 ``langfuse_trace_fetch._usage_breakdown`` 的兼容读取逻辑）。
    """
    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return None
    inp = int(usage.get("input") or 0)
    out = int(usage.get("output") or 0)
    cached = int(usage.get("cached_input") or 0)
    if inp == 0 and out == 0 and cached == 0:
        return None
    details: dict[str, int] = {
        "input": inp,
        "output": out,
        "total": inp + out,
    }
    if cached:
        details["cache_read_input_tokens"] = cached
    return details


@dataclass
class _PluginTraceSummary:
    """从 transcripts 聚合出来的 plugin trace 骨架。"""

    messages: list[dict[str, Any]]
    tool_roundtrips: int
    tool_call_blocks: int
    tool_names_distinct: list[str]
    generations: list[dict[str, Any]]  # 每个 assistant iteration
    tools: list[dict[str, Any]]  # 每次 tool 调用（含 input args + tool result）
    final_response: str
    # 统一观测契约：同 session 跨轮累积的 OpenAI 风格 tool_calls 列表 → root
    # output.tool_calls。OpenHuman 每轮 chat 后全量重读所有 session_raw jsonl，因此
    # 这里天然是"截至当前轮"的累积列表；长度 == tool_call_blocks（含 subagent 调用）。
    tool_calls: list[dict[str, Any]]


def _dedup_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            ordered.append(it)
    return ordered


def _is_subagent(t: ParsedTranscript) -> bool:
    """``_meta.agent_type == 'subagent'`` 判定（root/orchestrator 之外的委托代理）。"""
    return str(t.meta.get("agent_type") or "").lower() == "subagent"


def _root_epoch_from_path(path: str) -> str:
    """从 transcript 文件名取父 root 轮次的 epoch 前缀。

    - root 文件名：``<epoch>_orchestrator.jsonl``
    - subagent 文件名：``<epoch>_orchestrator__<childepoch>_..._<subagent>_....jsonl``
      （``__`` 之前是发起它的 root 轮次同款前缀）

    两者都取第一个 ``_`` 之前的 epoch，即可把 subagent 归到其父 root 轮次。多个
    orchestrator（多轮对话）会得到不同 epoch，据此按轮拆分。
    """
    name = Path(path).name
    head = name.split("__", 1)[0]  # 去掉 subagent 后缀，只留父 root 段
    return head.split("_", 1)[0]


def _child_epoch_from_path(path: str) -> str | None:
    """从 subagent 文件名取 child epoch（``__`` 之后的第一个 epoch 段）。

    subagent 文件名：``<parentepoch>_orchestrator__<childepoch>_<rand>_<subagent>_....jsonl``。
    这个 child epoch（秒级）**恰好等于** 发起它的 root assistant 那条 tool_call 的
    ``ts`` 取整秒（实测对齐），用于把 subagent 精确内联到触发它的 tool 调用之后。
    root 文件（无 ``__``）返回 None。
    """
    name = Path(path).name
    if "__" not in name:
        return None
    tail = name.split("__", 1)[1]
    epoch = tail.split("_", 1)[0]
    return epoch or None


def _assistant_call_epoch(msg: dict[str, Any]) -> str | None:
    """把 root assistant 的 ``ts`` 转成整秒字符串，用于匹配 subagent child epoch。

    OpenHuman assistant 顶层 ``ts`` 形如 ``2026-07-15T06:30:57.895...+00:00``；取
    ``int(timestamp())`` 得到秒级 epoch，与 subagent 文件名的 child epoch 对齐。
    解析失败返回 None（退化为轮末兜底追加）。
    """
    ts = msg.get("ts")
    if not isinstance(ts, str) or not ts.strip():
        return None
    from datetime import datetime

    try:
        return str(int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()))
    except (ValueError, OSError):
        return None


def _subagent_middle_messages(t: ParsedTranscript) -> list[dict[str, Any]]:
    """把一个 subagent transcript 折叠成"并入父 root 轮次"的中间执行痕迹。

    按约定：subagent 的**首个（连续）user**（orchestrator 下发的委托简报）与**末尾
    （连续）纯文本 assistant**（子代理最终汇报，root 层 tool result 里通常已含）都属于
    "子代理内部交接"，剔除；中间剩下的才是真正的执行痕迹（tool_call assistant + tool
    result），并入发起它的 root 轮次，让最终 messages 看起来像单 agent。

    - 末尾剔除只针对**无 tool_calls** 的 assistant：一旦末尾 assistant 带 tool_calls
      （说明是一次真实工具调用，即便无结果也算调用），就停止剔除并保留它，满足
      "subagent 的调用也算调用"的计数要求。
    - read-only 预检类子代理若无任何 tool（如 hello 场景的 context_scout：仅
      system+user+assistant），折叠后中间为空 → 等价整段丢弃，不污染主轨迹。
    """
    convo = [m for m in t.messages if str(m.get("role") or "") != "system"]
    if not convo:
        return []
    # 剔除开头连续 user（委托简报，通常一条）
    start = 0
    while start < len(convo) and str(convo[start].get("role")) == "user":
        start += 1
    # 剔除末尾连续的"无 tool_calls 的 assistant"（子代理最终汇报）
    end = len(convo)
    while end > start:
        last = convo[end - 1]
        if str(last.get("role")) == "assistant" and not _extract_tool_calls_from_assistant(last):
            end -= 1
        else:
            break
    return convo[start:end]


def summarize_transcripts(transcripts: list[ParsedTranscript]) -> _PluginTraceSummary:
    """把 orchestrator + subagent transcripts 聚合成 plugin trace payload。

    metadata 目标 schema（``LangfusePluginTraceMetadata``）：

    - ``messages``：**折叠成单 agent 主线**后的对话消息，归一化成与 GenericAgent
      一致的 GA 风格最小形状（``{role, content, _agent}`` + assistant 的 OpenAI 风格
      ``tool_calls``）。subagent（``agent_type=='subagent'``）不作为独立轮次展示：
      剔除其委托简报（首个 user）与最终汇报（末尾纯文本 assistant），中间执行痕迹
      （tool 调用等）并入发起它的 root 轮次之后。
    - ``tool_roundtrips``：进入主线的 ``role=tool`` 消息数
    - ``tool_call_blocks``：进入主线的 assistant 消息中 tool_calls 条目总数（**含
      subagent 的工具调用**）
    - ``tool_names_distinct``：去重后的工具名列表

    多轮：``session_raw`` 里每个 root orchestrator = 用户主动发起的一轮对话。按 root
    epoch 升序排列构成主线；每个 root 轮次处理完后，紧接着插入"父 epoch == 该 root
    epoch"的 subagent 中间消息。因此最终 ``messages`` 里 ``role=user`` 的条数 == root
    orchestrator 轮数 == ``chat_turns`` 长度（每轮 root 首条即真实用户 query）。
    """
    # ── 1) 分离 root 主线与 subagent，subagent 按父 root epoch 归组 ──
    roots: list[ParsedTranscript] = []
    subs_by_parent: dict[str, list[ParsedTranscript]] = {}
    for t in transcripts:
        if _is_subagent(t):
            subs_by_parent.setdefault(_root_epoch_from_path(t.path), []).append(t)
        else:
            roots.append(t)
    roots.sort(key=lambda t: t.path)  # 路径含 epoch 前缀，字典序≈时间序
    for lst in subs_by_parent.values():
        lst.sort(key=lambda t: t.path)

    # ── 2) 组装有序的 (agent_name, 原始 msg dict) 主线序列 ──
    # 每个 root 轮次内：root 消息按序进入；遇到带 tool_call 的 root assistant，其
    # ``ts``（整秒）对应触发的 subagent child epoch —— 在紧随的 root tool result 之后
    # 内联插入该 subagent 的中间执行痕迹，实现"像单 agent 一样"的内联展开。无法按
    # child epoch 匹配的 subagent（如框架预检 context_scout：child epoch == root
    # epoch，无对应 tool_call）在轮末兜底追加，不丢数据。
    ordered: list[tuple[str, dict[str, Any]]] = []
    for rt in roots:
        rt_agent = str(rt.meta.get("agent") or "orchestrator")
        epoch = _root_epoch_from_path(rt.path)
        subs = subs_by_parent.get(epoch, [])
        # child epoch -> subagent transcript（用于精确内联匹配）
        subs_by_child: dict[str, ParsedTranscript] = {}
        for sub in subs:
            ce = _child_epoch_from_path(sub.path)
            if ce is not None:
                subs_by_child.setdefault(ce, sub)
        consumed: set[int] = set()  # 已内联的 subagent id()，避免轮末重复追加

        # 记录"上一条 root assistant tool_call 的 ts→epoch"，在其 tool result 后内联
        pending_child_epoch: str | None = None
        rt_msgs = [m for m in rt.messages if str(m.get("role") or "") != "system"]
        for msg in rt_msgs:
            role = str(msg.get("role") or "")
            ordered.append((rt_agent, msg))
            if role == "assistant" and _extract_tool_calls_from_assistant(msg):
                # 该 assistant 触发了工具调用；记下它的 ts 秒，等 tool result 出现后内联。
                pending_child_epoch = _assistant_call_epoch(msg)
            elif role == "tool" and pending_child_epoch is not None:
                sub = subs_by_child.get(pending_child_epoch)
                if sub is not None and id(sub) not in consumed:
                    consumed.add(id(sub))
                    sub_agent = str(sub.meta.get("agent") or "subagent")
                    for mid in _subagent_middle_messages(sub):
                        ordered.append((sub_agent, mid))
                pending_child_epoch = None

        # 轮末兜底：未被内联匹配的 subagent（如 context_scout 预检），按 path 顺序追加。
        for sub in subs:
            if id(sub) in consumed:
                continue
            sub_agent = str(sub.meta.get("agent") or "subagent")
            for mid in _subagent_middle_messages(sub):
                ordered.append((sub_agent, mid))

    # 兜底：无任何 root（异常）时平铺所有 transcript，避免整体丢数据。
    if not roots and transcripts:
        for t in transcripts:
            agent_name = str(t.meta.get("agent") or "unknown")
            for msg in t.messages:
                if str(msg.get("role") or "") == "system":
                    continue
                ordered.append((agent_name, msg))

    messages: list[dict[str, Any]] = []
    tool_names: list[str] = []
    tool_call_blocks = 0
    tool_roundtrips = 0
    generations: list[dict[str, Any]] = []
    tools: list[dict[str, Any]] = []
    tool_calls_list: list[dict[str, Any]] = []
    final_response = ""

    pending_tool_call_id_to_name: dict[str, str] = {}
    for agent_name, msg in ordered:
        role = str(msg.get("role") or "")
        # 归一化成 GA 风格最小形状（root/subagent 统一处理，tool_calls 转 OpenAI 风格）。
        messages.append(_normalize_message_ga_style(msg, agent_name))

        if role == "assistant":
            calls = _extract_tool_calls_from_assistant(msg)
            if calls:
                # 工具调用计数：root 与 subagent 的调用都计入（满足"subagent 调用也算"）。
                tool_call_blocks += len(calls)
                # 同源收集 OpenAI 风格 tool_calls → root output.tool_calls；用与
                # tool_call_blocks 相同的 calls 源 + GA 风格归一化，保证 len 一致。
                tool_calls_list.extend(_normalize_tool_calls_ga_style(calls))
                for c in calls:
                    name = str(c.get("name") or "")
                    if name:
                        tool_names.append(name)
                        call_id = c.get("id")
                        if isinstance(call_id, str):
                            pending_tool_call_id_to_name[call_id] = name
                        tools.append(
                            {
                                "name": name,
                                "call_id": c.get("id"),
                                "arguments": c.get("arguments"),
                                "agent": agent_name,
                            }
                        )
            usage_details = _usage_details_from_assistant(msg)
            if usage_details is not None:
                generations.append(
                    {
                        "agent": agent_name,
                        "iteration": (
                            msg.get("iteration")
                            or (msg.get("extra_metadata") or {})
                            .get("openhuman_turn_usage", {})
                            .get("iteration")
                        ),
                        "content": msg.get("content") or "",
                        "usage_details": usage_details,
                        "model": msg.get("model"),
                    }
                )
            # 最后一条 orchestrator 层的、非 tool_call 结果 assistant 就是最终回复
            if agent_name == "orchestrator" and not calls:
                txt = msg.get("content")
                if isinstance(txt, str) and txt.strip():
                    final_response = txt

        elif role == "tool":
            tool_roundtrips += 1
            content = msg.get("content")
            call_id2: str | None = None
            if isinstance(content, str):
                try:
                    payload = json.loads(content)
                    if isinstance(payload, dict):
                        cid = payload.get("tool_call_id")
                        if isinstance(cid, str):
                            call_id2 = cid
                except json.JSONDecodeError:
                    pass
            name = pending_tool_call_id_to_name.get(call_id2 or "") if call_id2 else None
            if tools and name is None:
                name = tools[-1].get("name")
            if tools and name:
                for entry in reversed(tools):
                    if entry.get("name") == name and "output" not in entry:
                        entry["output"] = content
                        break

    return _PluginTraceSummary(
        messages=messages,
        tool_roundtrips=tool_roundtrips,
        tool_call_blocks=tool_call_blocks,
        tool_names_distinct=_dedup_preserve_order(tool_names),
        generations=generations,
        tools=tools,
        final_response=final_response,
        tool_calls=tool_calls_list,
    )


def _get_langfuse_client() -> Any | None:
    """按 CONFIG 构造 Langfuse SDK v4 client；未配置或导入失败返回 None。"""
    if not CONFIG.langfuse_credentials_present:
        return None
    try:
        from langfuse import Langfuse  # type: ignore[import]
    except ImportError:
        LOGGER.warning("[openhuman langfuse] langfuse SDK not installed")
        return None
    try:
        return Langfuse(
            public_key=CONFIG.langfuse_public_key,
            secret_key=CONFIG.langfuse_secret_key,
            host=CONFIG.langfuse_base_url,
        )
    except Exception:  # noqa: BLE001
        LOGGER.exception("[openhuman langfuse] failed to init Langfuse client")
        return None


def push_openhuman_plugin_trace(
    *,
    container_name: str,
    session_id: str,
    user_message: str,
    reply_text: str,
    run_tag: str,
    dump_dir: Path | None = None,
) -> None:
    """将本轮 chat 的 transcript 上传到 Langfuse ``openhuman-plugin``。

    参数：
        container_name: docker exec 目标容器（openhuman-core 所在）
        session_id: LIFT 侧 work/judge session_id（== openhuman thread_id）
        user_message: 本轮 chat 入参消息（作为 root observation.input）
        reply_text: 本轮 chat 回复（作为 root observation.output 兜底；若能从
            transcript 里拿到 orchestrator 最终回复则以后者为准）
        run_tag: LIFT 评测 run id（作为 Langfuse tag，与 pre-chat span 一致）
        dump_dir: 若给定，把拉到的**原始 jsonl 留档到本机** ``dump_dir/<session_id>/``，
            便于离线查看 OpenHuman 原始 schema。best-effort，不影响 push。

    失败仅 warning，不影响 chat 主路径。
    """
    if not CONFIG.langfuse_credentials_present and dump_dir is None:
        return

    transcripts = collect_session_transcripts(container_name, session_id)
    if not transcripts:
        LOGGER.debug(
            "[openhuman langfuse] no transcripts for session_id=%s in %s",
            session_id, container_name,
        )
        return

    # 留档原始 jsonl 到本机（在归一化 / push 之前，保证拿到的是原样源数据）。
    if dump_dir is not None:
        archive_transcripts_to_host(transcripts, dump_dir, session_id)

    if not CONFIG.langfuse_credentials_present:
        return

    summary = summarize_transcripts(transcripts)
    final_output = summary.final_response or reply_text

    client = _get_langfuse_client()
    if client is None:
        return

    try:
        from langfuse import propagate_attributes  # type: ignore[import]
    except ImportError:
        LOGGER.warning("[openhuman langfuse] propagate_attributes not importable")
        return

    tags: list[str] = []
    if run_tag:
        tags.append(run_tag)
    if session_id:
        tags.append(session_id)

    # LangfusePluginTraceMetadata 形状：字段名跟 pydantic model 一致，后处理
    # (LangfusePluginTraceMetadata.from_langfuse_dict) 会兼容 camel/snake case。
    metadata = {
        "success": True,
        "message_count": len(summary.messages),
        "tool_roundtrips": summary.tool_roundtrips,
        "tool_call_blocks": summary.tool_call_blocks,
        "tool_names_distinct": ",".join(summary.tool_names_distinct) or None,
        "messages": summary.messages,
    }

    try:
        with propagate_attributes(session_id=session_id, tags=tags):
            root_cm = client.start_as_current_observation(
                name="openhuman-plugin",
                as_type="agent",
                input=user_message,
                metadata=metadata,
            )
            root = root_cm.__enter__()
            try:
                # 每个 assistant iteration → generation observation
                for gen in summary.generations:
                    obs = client.start_observation(
                        name="llm.chat",
                        as_type="generation",
                        input=None,  # transcript messages 已在 root metadata 里
                        model=gen.get("model"),
                    )
                    obs.update(
                        output=str(gen.get("content") or "")[:20000],
                        usage_details=gen.get("usage_details"),
                    )
                    obs.end()

                # 每次 tool 调用 → tool observation
                for tool in summary.tools:
                    args = tool.get("arguments")
                    # arguments 是 JSON 字符串，尽力解析出更可读的形态
                    parsed_args: Any = args
                    if isinstance(args, str):
                        try:
                            parsed_args = json.loads(args)
                        except json.JSONDecodeError:
                            parsed_args = args
                    obs = client.start_observation(
                        name=str(tool.get("name") or "tool"),
                        as_type="tool",
                        input=parsed_args,
                    )
                    output_val = tool.get("output")
                    if isinstance(output_val, str) and len(output_val) > 20000:
                        output_val = output_val[:20000]
                    obs.update(output=output_val)
                    obs.end()

                # 统一观测契约：root output 带"同 session 跨轮累积"的 tool_calls 列表
                # （与 Hermes/OpenClaw/GA/EvoScientist 对齐，供人工检查 + 后处理
                # _tool_call_count_from_output 校准 toolCallBlocks）。无工具调用时退回纯文本。
                if summary.tool_calls:
                    root.update(output={"content": final_output, "tool_calls": summary.tool_calls})
                else:
                    root.update(output=final_output)
            finally:
                root_cm.__exit__(None, None, None)
        client.flush()
    except Exception:  # noqa: BLE001
        LOGGER.exception(
            "[openhuman langfuse] push failed session_id=%s container=%s",
            session_id, container_name,
        )


def push_openhuman_plugin_trace_safe(
    *,
    container_name: str,
    session_id: str,
    user_message: str,
    reply_text: str,
    run_tag: str,
    dump_dir: Path | None = None,
) -> None:
    """薄封装：调 ``push_openhuman_plugin_trace``，任何异常仅 warning。"""
    try:
        push_openhuman_plugin_trace(
            container_name=container_name,
            session_id=session_id,
            user_message=user_message,
            reply_text=reply_text,
            run_tag=run_tag,
            dump_dir=dump_dir,
        )
    except Exception:  # noqa: BLE001
        LOGGER.exception("[openhuman langfuse] unexpected error, swallowed")
