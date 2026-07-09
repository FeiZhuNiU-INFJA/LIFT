"""OpenHuman 容器内 chat transport 与 WorkerJudgerPair 工厂。

OpenHuman 的 JSON-RPC 2.0 契约（``openhuman-core serve``，端口 7788）：

- HTTP path：``POST /rpc``
- 请求：``{"jsonrpc":"2.0","id":<int>,"method":"openhuman.agent_chat",
          "params":{"message":"...", "thread_id":"...", "model_override":...}}``
- 成功响应：``{"jsonrpc":"2.0","id":<int>,"result":<value>}``
  当 ``agent_chat`` 通过 ``RpcOutcome::single_log`` 返回时，``result`` 是
  ``{"result":"<reply text>", "logs":["agent chat completed"]}`` 两层嵌套。
- 错误响应仍返回 HTTP 200，但 body 含 ``"error"`` 字段。

thread_id 沿用 LIFT 的 ``user-*`` / ``judge-*`` session_id，一次会话（一题）
的多轮 chat 都用同一 thread_id，OpenHuman 依赖它组织 conversation history。

Chat 完成后，宿主端从容器 ``~/.openhuman/users/local/workspace/session_raw/``
读 orchestrator + subagent transcript，转发一条 ``openhuman-plugin`` root
trace 到 Langfuse（openhuman-core 本身不集成 Langfuse SDK / OTel exporter）。
详见 ``transcript_langfuse.py``。
"""

from __future__ import annotations

import asyncio
import itertools
import json
from pathlib import Path

import urllib.error
import urllib.request

from src.config import LOGGER
from src.lift.adapters.openhuman.container_exec import OpenHumanContainerContext
from src.lift.adapters.openhuman.transcript_langfuse import (
    push_openhuman_plugin_trace_safe,
)
from src.lift.eval.chat_agent import ChatAgent
from src.lift.eval.worker_judger import WorkerJudgerPair
from src.models import SuiteTask
from src.utils import short_id

# 单轮 chat wall-clock 上限，与 GA / OpenClaw 一致（长产出留足预算）。超时返回
# 带 marker 的字符串，走 ``_looks_like_provider_error`` 重试通道。
CHAT_EXEC_TIMEOUT_SECONDS = 1000.0
CHAT_EXEC_TIMEOUT_MARKER = "chat exec timeout"

# JSON-RPC 方法名遵循 OpenHuman ``openhuman.<namespace>_<function>`` 规约。
_METHOD_AGENT_CHAT = "openhuman.agent_chat"


class OpenHumanContainerAgent(ChatAgent):
    """单个 OpenHuman thread 的 HTTP chat transport。

    每实例对应容器内 openhuman-core 的一个 thread（thread_id = ``session_id``），
    通过 ``POST {rpc_endpoint}/rpc`` 发起 ``openhuman.agent_chat`` 调用。同一
    实例的多轮 chat 共享 thread_id，openhuman 用它维护 conversation history。
    """

    _RPC_ID_ITER = itertools.count(1)  # 进程级 JSON-RPC id，仅诊断用

    def __init__(
        self,
        *,
        container: OpenHumanContainerContext,
        agent_name: str,
        workspace_dir: Path,
        role: str,
        run_tag: str = "",
    ) -> None:
        self._container = container
        self._agent_name = agent_name
        self._workspace_dir = workspace_dir
        self._role = role  # work / judge — 目前仅用于日志区分
        self._run_tag = run_tag  # LIFT 评测 run id；Langfuse push 用作 tag

    @property
    def agent_name(self) -> str:
        return self._agent_name

    async def initialize(self) -> None:
        """OpenHuman thread 无须显式注册，首次 ``agent.chat`` 用 thread_id 即建。"""
        self._workspace_dir.mkdir(parents=True, exist_ok=True)

    async def chat(self, message: str, *, session_id: str) -> str:
        """单轮 chat：POST /rpc → openhuman.agent_chat。"""
        rpc_id = next(self._RPC_ID_ITER)
        payload = {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": _METHOD_AGENT_CHAT,
            "params": {
                "message": message,
                "thread_id": session_id,
            },
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        url = f"{self._container.rpc_endpoint}/rpc"

        try:
            response_bytes = await asyncio.wait_for(
                asyncio.to_thread(
                    _post_rpc, url, body, self._container.rpc_token
                ),
                timeout=CHAT_EXEC_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            LOGGER.warning(
                "[openhuman chat] RPC timed out container=%s role=%s session=%s",
                self._container.container_name, self._role, session_id,
            )
            return (
                f"{CHAT_EXEC_TIMEOUT_MARKER}: openhuman /rpc timed out "
                f"after {CHAT_EXEC_TIMEOUT_SECONDS:.0f}s"
            )
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            LOGGER.warning(
                "[openhuman chat] RPC transport error container=%s: %r",
                self._container.container_name, exc,
            )
            return (
                f"{CHAT_EXEC_TIMEOUT_MARKER}: openhuman /rpc transport error: {exc!r}"
            )

        try:
            resp = json.loads(response_bytes.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            LOGGER.warning(
                "[openhuman chat] non-JSON response container=%s: %r",
                self._container.container_name, exc,
            )
            return (
                f"{CHAT_EXEC_TIMEOUT_MARKER}: openhuman /rpc returned non-JSON: {exc!r}"
            )

        if isinstance(resp, dict) and "error" in resp:
            err = resp["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            LOGGER.warning(
                "[openhuman chat] RPC error container=%s: %s",
                self._container.container_name, msg,
            )
            # 借用 timeout marker 走 provider-error 重试
            return f"{CHAT_EXEC_TIMEOUT_MARKER}: openhuman /rpc error: {msg}"

        reply_text = _extract_reply_text(resp)

        # Best-effort：从容器 ``session_raw/`` 拉 transcript → push 一条
        # ``openhuman-plugin`` root trace 到 Langfuse。失败仅 warning。
        # 放在成功返回路径下（错误 / 超时不 push），避免污染 trace 序列。
        try:
            await asyncio.to_thread(
                push_openhuman_plugin_trace_safe,
                container_name=self._container.container_name,
                session_id=session_id,
                user_message=message,
                reply_text=reply_text,
                run_tag=self._run_tag,
            )
        except Exception:  # noqa: BLE001
            LOGGER.exception(
                "[openhuman chat] langfuse push failed container=%s session=%s",
                self._container.container_name, session_id,
            )

        return reply_text


def _post_rpc(url: str, body: bytes, token: str) -> bytes:
    """同步 HTTP POST（在线程池里跑）。用 urllib 避免额外 aiohttp 依赖。

    openhuman-core 强制要求 ``Authorization: Bearer <OPENHUMAN_CORE_TOKEN>``。
    """
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    # 单请求超时兜底；``CHAT_EXEC_TIMEOUT_SECONDS`` 由外层 asyncio.wait_for 强制。
    with urllib.request.urlopen(req, timeout=CHAT_EXEC_TIMEOUT_SECONDS) as resp:
        return resp.read()


def _extract_reply_text(resp: object) -> str:
    """从 JSON-RPC response 中提取 agent 回复文本。

    OpenHuman ``agent.chat`` 走 ``RpcOutcome::single_log``，实际形状：
    ``{"jsonrpc":"2.0","id":N,"result":{"result":"<reply>", "logs":[...]}}``

    偶尔也可能是 no-log 通路，此时 ``result`` 直接是字符串。两种都要能吃。
    """
    if not isinstance(resp, dict):
        return str(resp)
    outer = resp.get("result")
    # 双层 envelope：single_log 情形
    if isinstance(outer, dict) and "result" in outer:
        inner = outer["result"]
        if isinstance(inner, str):
            return inner
        return json.dumps(inner, ensure_ascii=False)
    # 单层 envelope：no-log 情形（或 openhuman 未来改动）
    if isinstance(outer, str):
        return outer
    if outer is None:
        return ""
    return json.dumps(outer, ensure_ascii=False)


class OpenHumanWorkerJudgerPairFactory:
    """为同一 OpenHuman 容器内的题目构建 ``WorkerJudgerPair``。

    每题独立 work / judge 实例（两条 thread），Langfuse session_id 与 OpenClaw
    一致采用 ``user-*`` / ``judge-*`` 前缀，方便 trace 后处理沿用既有规则。
    """

    def __init__(
        self,
        *,
        container: OpenHumanContainerContext,
        workspace_dir: Path,
        run_tag: str = "",
    ) -> None:
        self._container = container
        self._workspace_dir = workspace_dir
        self._run_tag = run_tag

    async def __call__(self, task: SuiteTask) -> WorkerJudgerPair:
        _ = task
        work_session_id = f"user-{short_id()}"
        judge_session_id = f"judge-{short_id()}"
        work_agent = OpenHumanContainerAgent(
            container=self._container,
            agent_name=f"lift-openhuman-work-{short_id()}",
            workspace_dir=self._workspace_dir,
            role="work",
            run_tag=self._run_tag,
        )
        judge_agent = OpenHumanContainerAgent(
            container=self._container,
            agent_name=f"lift-openhuman-judge-{short_id()}",
            workspace_dir=self._workspace_dir,
            role="judge",
            run_tag=self._run_tag,
        )
        await asyncio.gather(work_agent.initialize(), judge_agent.initialize())
        return WorkerJudgerPair(
            work_agent=work_agent,
            judge_agent=judge_agent,
            work_session_id=work_session_id,
            judge_session_id=judge_session_id,
        )
