"""Prime Agent 容器内 chat transport 与 WorkerJudgerPair 工厂。

Prime Agent CLI（bin ``prime-agent``）headless 协议（JSON 事件流单发）：

    prime-agent --mode json [-r <session_id>] "<user message>"

``--mode json`` 让 ``prime-agent`` 把整段 session 以 **JSON Lines** 打到 stdout
（见 upstream ``docs/json.md`` / ``dist/modes/print-mode.js``）：

  - 第一行是 session header ``{"type":"session","version":N,"id":"<uuid>",...}``；
    LIFT 从这里拿回 Prime Agent 的 conversation session id，用于后续 ``-r`` 续接。
  - 之后是事件流：``agent_start`` / ``turn_start`` / ``message_start`` /
    ``message_update`` / ``message_end`` / ``turn_end`` / ``agent_end``。
    LIFT 取终态 assistant 文本作为该轮回复：优先 ``agent_end.messages`` 里最后
    一条 ``role=="assistant"`` 的 ``content[].text``，回退到最后一条
    ``message_end`` 的 assistant message。

为什么用 JSON 模式而非 ``-p``（print 纯文本）：``-p`` 把回复直接打到 stdout
好解析，但**不回传 session id**，多轮 ``-r`` 续接就没法拿到 conversation id。
JSON 模式一举拿到 id + 结构化回复，与 EvoScientist ``--output-format stream-json``
同构，鲁棒性更好。

多轮续接：首轮不传 ``-r``，从 header 捕获 session id 存到 agent 实例上；后续轮
追加 ``-r <id>``（``-r <string>`` 是显式 session selector，非交互模式下不会被
``shouldRejectNonInteractiveBareResume`` 拒绝——那只拦 bare ``--resume``）。状态
存在 agent 实例上，因此同一 warmup 容器内多题并发不会串同一 conversation。

会话隔离：每题一个不同的 LIFT session id（对应 Langfuse session_id），通过
``docker exec -e`` 注入 overlay，让 root trace 挂上 LIFT session。注意该
LIFT session id 用于观测关联，与 Prime Agent 自身的 conversation session id 是
两个维度——本 transport 用后者（header 回传的 uuid）做多轮续接。
"""

from __future__ import annotations

import asyncio
import json
import shlex
from pathlib import Path

from src.config import LOGGER
from src.lift.adapters.prime_agent.container_exec import PrimeAgentContainerContext
from src.lift.adapters.prime_agent.langfuse_usage import (
    push_prime_agent_plugin_trace_safe,
)
from src.lift.eval.chat_agent import ChatAgent
from src.lift.eval.worker_judger import WorkerJudgerPair
from src.models import SuiteTask
from src.utils import short_id

# 与 EvoScientist / GenericAgent / OpenHuman 一致：单轮 chat wall-clock 上限 1000s。
CHAT_EXEC_TIMEOUT_SECONDS = 1000.0
CHAT_EXEC_TIMEOUT_MARKER = "chat exec timeout"


class PrimeAgentContainerAgent(ChatAgent):
    """一个 Prime Agent chat transport 实例，绑定单个 (container, session)。

    ``chat`` 每次调用都会 spawn 一个新的 ``prime-agent --mode json`` 进程。首轮
    不传 ``-r``；从 session header 解析回 Prime Agent 的 conversation session id，
    后续轮追加 ``-r <id>``。该状态存在 agent 实例上，因此同一 warmup 容器内多个
    task 并发时不会共享 conversation。
    """

    def __init__(
        self,
        *,
        container: PrimeAgentContainerContext,
        agent_name: str,
        workspace_dir: Path,
        role: str,
        run_tag: str = "",
    ) -> None:
        self._container = container
        self._agent_name = agent_name
        self._workspace_dir = workspace_dir
        self._role = role  # work / judge —— 目前仅用于日志
        self._run_tag = run_tag  # LIFT 评测 run id；Langfuse plugin trace 用作 tag
        self._session_id: str | None = None  # Prime Agent 侧 conversation session

    @property
    def agent_name(self) -> str:
        return self._agent_name

    async def initialize(self) -> None:
        """无状态：Prime Agent JSON 模式是单发，不需 registry。"""
        return None

    async def chat(self, message: str, *, session_id: str) -> str:
        """启动一次 ``prime-agent --mode json ...`` 单发进程，返回 assistant 回复文本。

        ``session_id``（LIFT/Langfuse 维度）通过 ``docker exec -e`` 注入 overlay；
        Prime Agent 侧的 conversation 续接用 ``self._session_id``（``-r``）。
        """
        resume_arg = (
            f"-r {shlex.quote(self._session_id)} "
            if self._session_id
            else ""
        )
        # bash -c（非 login shell）保留 Dockerfile ENV PATH（含 prime-agent 所在
        # bin），与 EvoScientist 一样避免 -lc 走 /etc/profile 重置 PATH。
        remote_shell = (
            "cd /workspace/task && "
            "prime-agent --mode json "
            f"{resume_arg}"
            f"-- {shlex.quote(message)}"
        )
        cmd = [
            "docker", "exec",
            "-e", f"LIFT_PRIME_AGENT_SESSION_ID={session_id}",
            self._container.container_name,
            "bash", "-c", remote_shell,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=CHAT_EXEC_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                LOGGER.warning(
                    "[prime_agent chat] prime-agent --mode json exec timed out "
                    "container=%s role=%s session=%s",
                    self._container.container_name, self._role, session_id,
                )
                return (
                    f"{CHAT_EXEC_TIMEOUT_MARKER}: Prime Agent chat exec exceeded "
                    f"{CHAT_EXEC_TIMEOUT_SECONDS:.0f}s"
                )
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception(
                "[prime_agent chat] docker exec failed container=%s: %s",
                self._container.container_name, exc,
            )
            raise

        stdout_text = (stdout or b"").decode(errors="replace")
        stderr_text = (stderr or b"").decode(errors="replace")

        if proc.returncode != 0:
            raise RuntimeError(
                f"prime-agent --mode json exited with code {proc.returncode} in "
                f"{self._container.container_name}: {stderr_text[-1000:]}"
            )

        # 首轮从 session header 捕获 conversation session id 供后续 -r 续接。
        parsed_session = _parse_session_id(stdout_text)
        if parsed_session and self._session_id is None:
            self._session_id = parsed_session
            LOGGER.info(
                "[prime_agent chat] captured session=%s container=%s role=%s",
                parsed_session, self._container.container_name, self._role,
            )

        # Prime Agent 无容器内 Langfuse 插件——host 侧从 stdout 解析 usage 补写
        # prime-agent-plugin trace，让 post-process 能挂回 5 字段 token（失败仅 warning）。
        push_prime_agent_plugin_trace_safe(
            session_id=session_id,
            user_message=message,
            stdout_text=stdout_text,
            run_tag=self._run_tag,
        )

        reply = _extract_reply(stdout_text)
        if reply is None:
            LOGGER.warning(
                "[prime_agent chat] empty reply stdout_head=%r stderr_tail=%r",
                stdout_text[:300], stderr_text[-500:],
            )
            return f"[Prime Agent produced no parseable reply]\n{stderr_text[-500:]}"
        return reply


def _iter_json_lines(text: str):
    """逐行解析 JSON Lines，跳过非 JSON 行（Rich 人类可读输出可能混入）。"""
    for line in text.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(obj, dict):
            yield obj


def _parse_session_id(text: str) -> str | None:
    """从 JSON 事件流的 session header 解析 conversation session id。

    header 形如 ``{"type":"session","version":N,"id":"<uuid>",...}``（``json.md``）。
    """
    for obj in _iter_json_lines(text):
        if obj.get("type") == "session":
            sid = obj.get("id")
            if isinstance(sid, str) and sid:
                return sid
    return None


def _assistant_text(message: object) -> str | None:
    """从一条 assistant message 抽取拼接后的 ``content[].text``。"""
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return None
    content = message.get("content")
    if not isinstance(content, list):
        return None
    parts = [
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]
    joined = "".join(parts).strip()
    return joined or None


def _extract_reply(text: str) -> str | None:
    """从 JSON 事件流抽取终态 assistant 回复文本。

    优先级（对齐 upstream ``selectHeadlessTerminalResult`` 的“取末尾 assistant”）：
      1. 最后一个 ``agent_end`` 事件的 ``messages`` 里最后一条 assistant message；
      2. 回退到最后一条 ``message_end`` 的 assistant message；
      3. 都没有则返回 None，让上层给诊断信息。
    """
    last_agent_end_reply: str | None = None
    last_message_end_reply: str | None = None
    for obj in _iter_json_lines(text):
        etype = obj.get("type")
        if etype == "agent_end":
            messages = obj.get("messages")
            if isinstance(messages, list):
                for msg in reversed(messages):
                    reply = _assistant_text(msg)
                    if reply is not None:
                        last_agent_end_reply = reply
                        break
        elif etype == "message_end":
            reply = _assistant_text(obj.get("message"))
            if reply is not None:
                last_message_end_reply = reply
    return last_agent_end_reply or last_message_end_reply


class PrimeAgentWorkerJudgerPairFactory:
    """为同一 Prime Agent 容器内的题目构建 ``WorkerJudgerPair``。

    每题独立 work / judge 实例。JSON 模式单发是无状态构造，构造后立即可用。
    """

    def __init__(
        self,
        *,
        container: PrimeAgentContainerContext,
        workspace_dir: Path,
        judge_container: PrimeAgentContainerContext | None = None,
        run_tag: str = "",
    ) -> None:
        self._container = container
        self._judge_container = judge_container or container
        self._workspace_dir = workspace_dir
        self._run_tag = run_tag

    async def __call__(self, task: SuiteTask) -> WorkerJudgerPair:
        _ = task  # baseline 不做 per-task 定制
        work_session_id = f"user-{short_id()}"
        judge_session_id = f"judge-{short_id()}"
        work_agent = PrimeAgentContainerAgent(
            container=self._container,
            agent_name=f"lift-prime-agent-work-{short_id()}",
            workspace_dir=self._workspace_dir,
            role="work",
            run_tag=self._run_tag,
        )
        judge_agent = PrimeAgentContainerAgent(
            container=self._judge_container,
            agent_name=f"lift-prime-agent-judge-{short_id()}",
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


__all__ = [
    "CHAT_EXEC_TIMEOUT_MARKER",
    "CHAT_EXEC_TIMEOUT_SECONDS",
    "PrimeAgentContainerAgent",
    "PrimeAgentWorkerJudgerPairFactory",
]
