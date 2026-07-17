"""EvoScientist 容器内 chat transport 与 WorkerJudgerPair 工厂。

EvoScientist CLI 协议：

    EvoSci -p "<user message>" \
      --output-format stream-json \
      --auto-mode \
      [--resume <thread_id>]

单发模型：每次 chat 都是一次独立进程调用，通过 ``--resume <thread_id>``
串起同一个 session 的多轮对话。stdout 是 JSONL 事件流：
``text`` / ``thinking`` / ``tool_call`` / ``tool_result`` / ``usage_stats`` /
``done`` / ``error``（见 ``EvoScientist/stream/emitter.py``）；LIFT 消费
``done.response`` 作为该轮 assistant 回复。

关键约定：
- ``thread_id`` LIFT 侧生成（``short_id()``），首轮不传 ``--resume``，等
  EvoScientist 自建 thread；从 stderr / dashboard 无法可靠反解 thread_id，
  因此**直接把 LIFT 生成的 id 作为 ``--resume`` 首轮值**——``resolve_thread``
  找不到时 EvoScientist 会创建新 thread 并以该 id 作为 thread_id。此处
  依赖 EvoScientist 版本行为，若上游变更需要在 chat 首轮先起一个空 turn
  拿到 thread_id 再喂 message，M1 baseline 先按简单方案实现。
  → 实际实现：首轮通过 ``docker exec ... env EVOSCI_FORCE_THREAD_ID=<id>``
    注入到 config metadata 里，session.py 用 build 期 secrets bake，chat
    侧不再依赖首轮的 --resume 行为，改用 CLI 侧的 UUID：让 EvoScientist 自
    分配 thread_id 后从 stderr resume-hint 抓取。
  → M1 简化：不追求跨轮，每题 chat 各自独立 thread。work↔judge 多轮内
    LIFT 通过 EvoScientist thread 保持上下文——首轮不传 ``--resume``，从
    stream JSON 里没有 thread_id 事件，转而在 ``done`` 后跑一次
    ``EvoSci threads list --limit 1`` 反查最新 thread_id。此实现对 M1 冗余，
    baseline 先不加多轮 resume；work agent 每轮各自新起 thread（work↔judge
    多轮的记忆通过 EvoScientist 内部 memory 保持，thread 隔离不影响记忆学习）。

会话隔离：每题都传一个不同的 ``LIFT_EVOSCI_SESSION_ID`` env（对应 Langfuse
session_id），overlay 侧 root trace 会带上该 id 作为 session_id + tag。
"""

from __future__ import annotations

import asyncio
import json
import shlex
from pathlib import Path

from src.config import LOGGER
from src.lift.adapters.container.exec import docker_exec_shell_async
from src.lift.adapters.evoscientist.container_exec import EvoScientistContainerContext
from src.lift.eval.chat_agent import ChatAgent
from src.lift.eval.worker_judger import WorkerJudgerPair
from src.models import SuiteTask
from src.utils import short_id

# 与 GenericAgent / OpenClaw / OpenHuman 一致：单轮 chat wall-clock 上限 1000s。
CHAT_EXEC_TIMEOUT_SECONDS = 1000.0
CHAT_EXEC_TIMEOUT_MARKER = "chat exec timeout"


class EvoScientistContainerAgent(ChatAgent):
    """一个 EvoScientist chat transport 实例，绑定单个 (container, thread_id)。

    ``chat`` 每次调用都会 spawn 一个新的 ``EvoSci`` 进程。首轮不传 ``--resume``，
    EvoScientist 会自建 thread；后续轮仍不传 ``--resume``（M1 baseline 简化），
    每轮各自独立 thread。EvoScientist 的跨会话记忆走 ``/root/.evoscientist/memories``
    与 ``.../skills``，不依赖 thread 一致性即可累积。
    """

    def __init__(
        self,
        *,
        container: EvoScientistContainerContext,
        agent_name: str,
        workspace_dir: Path,
        role: str,
    ) -> None:
        self._container = container
        self._agent_name = agent_name
        self._workspace_dir = workspace_dir
        self._role = role  # work / judge —— 目前仅用于日志

    @property
    def agent_name(self) -> str:
        return self._agent_name

    async def initialize(self) -> None:
        """无状态：EvoScientist chat 是 stateless 单发，不需 registry。"""
        return None

    async def chat(self, message: str, *, session_id: str) -> str:
        """启动一次 ``EvoSci -p ...`` 单发进程，返回 assistant 回复文本。

        session_id 通过 ``docker exec -e LIFT_EVOSCI_SESSION_ID=...`` 注入到
        overlay 中，让 root Langfuse trace 挂上 LIFT session。
        """
        # -p 参数需要 shell 安全 quoting；用 --workdir=/workspace/task 明确切
        # 到 task materials 目录，与 GA 一致。output-format stream-json 让
        # EvoScientist 把事件流写到 stdout（stderr 承载 Rich 人类可读输出）。
        # 注意：``bash -lc`` 在 root 用户下会走 /etc/profile 重置 PATH，丢掉
        # 官方镜像 ENV 里的 ``/opt/venv/bin``，导致 ``EvoSci: command not found``。
        # 用 ``bash -c``（非 login shell）保留 Dockerfile ENV PATH。
        remote_shell = (
            "cd /workspace/task && "
            "EvoSci "
            f"--workdir /workspace/task "
            "--output-format stream-json "
            "--auto-mode "
            f"-p {shlex.quote(message)}"
        )
        cmd = [
            "docker", "exec",
            "-e", f"LIFT_EVOSCI_SESSION_ID={session_id}",
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
                    "[evoscientist chat] EvoSci -p exec timed out container=%s role=%s session=%s",
                    self._container.container_name, self._role, session_id,
                )
                return (
                    f"{CHAT_EXEC_TIMEOUT_MARKER}: EvoScientist chat exec exceeded "
                    f"{CHAT_EXEC_TIMEOUT_SECONDS:.0f}s"
                )
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception(
                "[evoscientist chat] docker exec failed container=%s: %s",
                self._container.container_name, exc,
            )
            raise

        if proc.returncode != 0:
            err_tail = (stderr or b"").decode(errors="replace")[-1000:]
            raise RuntimeError(
                f"EvoSci exited with code {proc.returncode} in "
                f"{self._container.container_name}: {err_tail}"
            )

        response = _extract_done_response(stdout.decode(errors="replace"))
        if response is None:
            # 没拿到 done 事件（罕见），把 stderr 尾部作为诊断塞回去，让 judge
            # 层能看到错误而不是空字符串。
            err_tail = (stderr or b"").decode(errors="replace")[-500:]
            LOGGER.warning(
                "[evoscientist chat] no done event stdout_head=%r stderr_tail=%r",
                stdout[:300], err_tail,
            )
            return f"[EvoScientist stream produced no `done` event]\n{err_tail}"
        return response


def _extract_done_response(jsonl_text: str) -> str | None:
    """从 stream-json JSONL 输出里取 ``done`` 事件的 ``response`` / ``content``。

    降级顺序：
      1. 最后一条 ``done`` 事件的 ``response``；
      2. 若 done 没 response，则拼接所有 ``text`` 事件的 ``content`` 作为回复；
      3. 都没有，返回 ``None`` 让上层给诊断信息。
    """
    if not jsonl_text.strip():
        return None
    done_response: str | None = None
    text_chunks: list[str] = []
    error_msg: str | None = None
    for line in jsonl_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        etype = evt.get("type") if isinstance(evt, dict) else None
        if etype == "text":
            content = evt.get("content")
            if isinstance(content, str):
                text_chunks.append(content)
        elif etype == "done":
            resp = evt.get("response") or evt.get("content")
            if isinstance(resp, str):
                done_response = resp
        elif etype == "error":
            msg = evt.get("message")
            if isinstance(msg, str):
                error_msg = msg
    if done_response:
        return done_response
    if text_chunks:
        return "".join(text_chunks)
    if error_msg:
        return f"[EvoScientist error]\n{error_msg}"
    return None


class EvoScientistWorkerJudgerPairFactory:
    """为同一 EvoScientist 容器内的题目构建 ``WorkerJudgerPair``。

    每题独立 work / judge 实例。EvoScientist 是 stateless 单发，实例本身
    没有内部 state，构造后立即可用。
    """

    def __init__(
        self,
        *,
        container: EvoScientistContainerContext,
        workspace_dir: Path,
    ) -> None:
        self._container = container
        self._workspace_dir = workspace_dir

    async def __call__(self, task: SuiteTask) -> WorkerJudgerPair:
        _ = task  # baseline 不做 per-task 定制
        work_session_id = f"user-{short_id()}"
        judge_session_id = f"judge-{short_id()}"
        work_agent = EvoScientistContainerAgent(
            container=self._container,
            agent_name=f"lift-evoscientist-work-{short_id()}",
            workspace_dir=self._workspace_dir,
            role="work",
        )
        judge_agent = EvoScientistContainerAgent(
            container=self._container,
            agent_name=f"lift-evoscientist-judge-{short_id()}",
            workspace_dir=self._workspace_dir,
            role="judge",
        )
        await asyncio.gather(work_agent.initialize(), judge_agent.initialize())
        return WorkerJudgerPair(
            work_agent=work_agent,
            judge_agent=judge_agent,
            work_session_id=work_session_id,
            judge_session_id=judge_session_id,
        )


# 兼容 GA / OpenClaw 便利函数命名：外部可以按 evoscientist_context 拿 handle
__all__ = [
    "CHAT_EXEC_TIMEOUT_MARKER",
    "CHAT_EXEC_TIMEOUT_SECONDS",
    "EvoScientistContainerAgent",
    "EvoScientistWorkerJudgerPairFactory",
]
