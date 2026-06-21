"""容器内读 OpenClaw ``trajectory.jsonl`` 计 work agent 的 tool 调用次数。

OpenClaw 把每条 chat 的运行轨迹写到
``~/.openclaw/agents/<agent>/sessions/<session>.trajectory.jsonl``。每行是一条
事件，``type=="model.completed"`` 的记录里 ``data.messagesSnapshot`` 是当轮 chat
结束时的完整消息序列；最后一条 ``model.completed`` 即整段 session 的最终快照，
其 ``messagesSnapshot[].content[].type == "toolCall"`` 的 block 数即整段 session
work agent 的 tool 调用总次数（已与 plugin metadata / Langfuse trace 对账）。

设计：

- 入口 ``count_session_tool_calls(container_name, work_session_id)`` 在容器里跑一
  段 python，``find`` 出 ``<sid>.trajectory.jsonl`` 后聚合最后一条 model.completed
  的 toolCall 数。多个匹配（理论上不会，session_id 是 short_id 唯一）取最后修改
  的那个。
- 失败仅返回 None：trajectory 是 OpenClaw 内部产物，找不到 / 解析失败时不影响
  hold-out 主链路；上层 ``count_tool_calls`` 也只在异常时 warning。
"""

from __future__ import annotations

import shlex

from src.config import LOGGER
from src.lift.adapters.container.exec import docker_exec_shell_async


_TRAJECTORY_BASE = "/root/.openclaw/agents"


_COUNT_SCRIPT_TEMPLATE = """
SID=__SID__
BASE=__BASE__
python3 - "$SID" "$BASE" <<'PY'
import json, os, sys
sid, base = sys.argv[1], sys.argv[2]
target = sid + ".trajectory.jsonl"
hits = []
for root, _dirs, files in os.walk(base):
    if target in files:
        hits.append(os.path.join(root, target))
if not hits:
    print("__lift_tool_calls__:none")
    sys.exit(0)
hits.sort(key=lambda p: os.path.getmtime(p))
path = hits[-1]
last_completed = None
with open(path, "r", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("type") == "model.completed":
            last_completed = rec
if last_completed is None:
    print("__lift_tool_calls__:none")
    sys.exit(0)
snap = ((last_completed.get("data") or {}).get("messagesSnapshot")) or []
count = 0
for m in snap:
    for b in (m.get("content") or []):
        if isinstance(b, dict) and b.get("type") == "toolCall":
            count += 1
print("__lift_tool_calls__:" + str(count))
PY
""".strip()


_MARKER = "__lift_tool_calls__:"


async def count_session_tool_calls(
    container_name: str,
    work_session_id: str,
) -> int | None:
    """在容器内统计 ``work_session_id`` 对应 trajectory 的 toolCall 块数。

    失败返回 None；成功返回非负整数（找不到 trajectory / 没 model.completed 时
    都返回 None 以便上层显示 "—"）。
    """
    if not work_session_id:
        return None
    script = (
        _COUNT_SCRIPT_TEMPLATE
        .replace("__SID__", shlex.quote(work_session_id))
        .replace("__BASE__", shlex.quote(_TRAJECTORY_BASE))
    )
    try:
        out = await docker_exec_shell_async(container_name, script)
    except Exception as exc:  # noqa: BLE001 — adapter 自报通道，不能拖垮 hold-out
        LOGGER.warning(
            "count_session_tool_calls: docker exec failed (%s, sid=%s): %r",
            container_name, work_session_id, exc,
        )
        return None
    for line in (out or "").splitlines():
        line = line.strip()
        if not line.startswith(_MARKER):
            continue
        payload = line[len(_MARKER):]
        if payload == "none":
            return None
        try:
            return int(payload)
        except ValueError:
            return None
    return None
