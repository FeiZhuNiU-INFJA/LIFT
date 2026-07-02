"""Hermes 容器内 chat transport 与 WorkerJudgerPair 工厂。

设计（见 .trae/documents/hermes_runtime_integration_plan.md）：

- 每个 ``session_id`` 对应一个**常驻** ``docker exec -i`` runner 子进程
  （容器内 ``hermes_runner.py``），跨多轮 chat 复用以维持会话记忆；
- work / judge 各自独立 runner；
- runner 通过 stdin/stdout sentinel 协议驱动：
  发送 ``msg + "\n" + __evo_msg_end__``，读到 ``__evo_resp_end__`` 为止；
- **review 时机**：work session 在 warmup 阶段结束时发 ``__evo_task_end__`` 触发
  background review（写入 ``/opt/data`` memory/skills），再由 ``docker commit`` 带入
  delta；judge session 与 holdout work session 不 review；
- **Langfuse tag 桥接**：runner 以 ``-e SESSION_ID=<lift session>`` /
  ``-e EVOBENCH_RUN_ID=<run_id>`` 启动，容器内 langfuse 插件据此把 LIFT 的
  work/judge session id 写进 ``Hermes turn`` trace 的 tags，供后处理
  ``_stitch_hermes`` 配对。
"""

from __future__ import annotations

import asyncio

from src.config import LOGGER
from src.lift.adapters.container.session import ContainerSession
from src.lift.adapters.hermes.container_exec import (
    HERMES_HOME_DIR,
    HERMES_RUNNER_PATH,
    HERMES_TASK_CWD,
    HermesContainerContext,
    HermesRunnerParams,
    hermes_runner_params,
)
from src.lift.eval.chat_agent import ChatAgent
from src.lift.eval.worker_judger import WorkerJudgerPair
from src.models import SuiteTask
from src.utils import short_id

# runner 协议哨兵（与 agent-runtimes/hermes/hermes-helper/hermes_runner.py 一致）。
_TASK_END = "__evo_task_end__"
_MSG_END = "__evo_msg_end__"
_RESP_END = "__evo_resp_end__"

# 单轮 chat 宿主侧 wall-clock 上限；超时返回 provider-error marker 走重试通道。
CHAT_EXEC_TIMEOUT_SECONDS = 600.0
CHAT_EXEC_TIMEOUT_MARKER = "chat exec timeout"

# ContainerSession.metadata 中存放 session_id -> HermesContainerAgent 的注册表 key。
_RUNNERS_META_KEY = "hermes_runners"


def register_runner_registry(session: ContainerSession) -> dict[str, "HermesContainerAgent"]:
    """取（或初始化）容器上的 runner 注册表，供 evolve_after_task / cleanup 查找。"""
    reg = session.metadata.get(_RUNNERS_META_KEY)
    if reg is None:
        reg = {}
        session.metadata[_RUNNERS_META_KEY] = reg
    return reg


async def end_all_runners(session: ContainerSession) -> None:
    """容器 cleanup 前收尾所有残留 runner（best-effort，不触发 review）。"""
    reg = session.metadata.get(_RUNNERS_META_KEY)
    if not reg:
        return
    for agent in list(reg.values()):
        try:
            await agent.end_session(review=False)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Failed ending hermes runner %s: %r", agent.agent_name, exc)
    reg.clear()


class HermesContainerAgent(ChatAgent):
    """容器内 Hermes chat：常驻 ``docker exec -i`` runner + sentinel 协议。"""

    def __init__(
        self,
        *,
        container: HermesContainerContext,
        agent_name: str,
        run_id: str,
        params: HermesRunnerParams,
        enable_review: bool,
        registry: dict[str, "HermesContainerAgent"] | None = None,
    ) -> None:
        self._container = container
        self._agent_name = agent_name
        self._run_id = run_id
        self._params = params
        self._enable_review = enable_review
        self._registry = registry
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task | None = None
        self._session_id: str = ""

    @property
    def agent_name(self) -> str:
        return self._agent_name

    async def initialize(self) -> None:
        """无一次性 setup：runner 在首次 chat（拿到 session_id）时惰性启动。"""

    def _build_exec_cmd(self, session_id: str) -> list[str]:
        params = self._params
        cmd = [
            "docker", "exec", "-i",
            # Langfuse tag 桥接：插件读 SESSION_ID / EVOBENCH_RUN_ID 写进 trace tags。
            "-e", f"SESSION_ID={session_id}",
            "-e", f"EVOBENCH_RUN_ID={self._run_id}",
            "-e", f"HERMES_HOME={HERMES_HOME_DIR}",
            "-e", f"TERMINAL_CWD={HERMES_TASK_CWD}",
            self._container.container_name,
            self._container.venv_py,
            HERMES_RUNNER_PATH,
            "--hermes-agent-dir", self._container.src_dir,
            "--profile-home", HERMES_HOME_DIR,
            "--workspace", HERMES_TASK_CWD,
            "--model", params.model,
            "--base-url", params.base_url,
            "--api-key", params.api_key,
            "--session-id", session_id,
            "--max-tokens", str(params.max_tokens),
        ]
        if self._enable_review:
            cmd.append("--enable-review")
        return cmd

    async def _spawn(self, session_id: str) -> None:
        cmd = self._build_exec_cmd(session_id)
        LOGGER.info(
            "Spawning hermes runner: container=%s agent=%s session=%s review=%s",
            self._container.container_name, self._agent_name, session_id, self._enable_review,
        )
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._proc = proc
        self._session_id = session_id

        async def _drain_stderr() -> None:
            assert proc.stderr is not None
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                LOGGER.info(
                    "[hermes runner stderr session=%s] %s",
                    session_id, line.decode("utf-8", errors="replace").rstrip(),
                )

        self._stderr_task = asyncio.create_task(_drain_stderr())
        if self._registry is not None:
            self._registry[session_id] = self

    async def chat(self, message: str, *, session_id: str) -> str:
        """发送一条消息并读回 assistant 文本；首次调用惰性起 runner。"""
        if self._proc is None:
            await self._spawn(session_id)
        try:
            return await asyncio.wait_for(
                self._send_and_recv(message), timeout=CHAT_EXEC_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            LOGGER.warning(
                "[hermes chat] timeout container=%s session=%s",
                self._container.container_name, self._session_id,
            )
            return f"{CHAT_EXEC_TIMEOUT_MARKER}: hermes runner timed out after {CHAT_EXEC_TIMEOUT_SECONDS:.0f}s"

    async def _send_and_recv(self, msg: str) -> str:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdout is None:
            raise RuntimeError(f"hermes runner not piped for session {self._session_id}")
        if proc.returncode is not None:
            raise RuntimeError(
                f"hermes runner already exited (rc={proc.returncode}) session {self._session_id}"
            )

        payload = msg + "\n" + _MSG_END + "\n"
        proc.stdin.write(payload.encode("utf-8"))
        await proc.stdin.drain()

        chunks: list[str] = []
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                rc = proc.returncode
                raise RuntimeError(
                    f"hermes runner stdout closed unexpectedly (rc={rc}) session {self._session_id}"
                )
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if line == _RESP_END:
                break
            chunks.append(line)
        return "\n".join(chunks)

    async def end_session(self, *, review: bool) -> None:
        """结束 runner：写 ``__evo_task_end__``（触发 review，若 runner 起时带 --enable-review），
        关闭 stdin 并等待退出。work session 在 warmup 结束时应传 ``review=True``。

        ``review`` 仅影响是否等待 runner 完整跑完 review；实际是否 review 由 spawn
        时的 ``--enable-review`` 决定。judge / holdout 传 ``review=False``。
        """
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.stdin is not None and proc.returncode is None:
                try:
                    proc.stdin.write((_TASK_END + "\n").encode("utf-8"))
                    await proc.stdin.drain()
                    proc.stdin.close()
                except Exception:  # noqa: BLE001
                    LOGGER.exception("Failed sending task_end session=%s", self._session_id)
            try:
                # review 可能耗时；给足超时，超时则 kill。
                await asyncio.wait_for(proc.wait(), timeout=CHAT_EXEC_TIMEOUT_SECONDS)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                if proc.returncode is None:
                    try:
                        proc.kill()
                        await proc.wait()
                    except ProcessLookupError:
                        pass
        finally:
            if self._stderr_task is not None:
                self._stderr_task.cancel()
                try:
                    await self._stderr_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            if self._registry is not None:
                self._registry.pop(self._session_id, None)
            self._proc = None


class HermesWorkerJudgerPairFactory:
    """为同一 Hermes 容器内的题目构建 ``WorkerJudgerPair``。

    ``enable_review`` 由 ``run_phase`` 决定：warmup 阶段 work agent 带 review，
    holdout 阶段一律不 review（baseline/evolved 只测量）。
    """

    def __init__(
        self,
        *,
        container: HermesContainerContext,
        session: ContainerSession,
        run_id: str,
        warmup: bool,
    ) -> None:
        self._container = container
        self._registry = register_runner_registry(session)
        self._run_id = run_id
        self._warmup = warmup

    async def __call__(self, task: SuiteTask) -> WorkerJudgerPair:
        _ = task
        params = hermes_runner_params()
        work_session_id = f"user-{short_id()}"
        judge_session_id = f"judge-{short_id()}"

        work_agent = HermesContainerAgent(
            container=self._container,
            agent_name=f"lift-hermes-work-{short_id()}",
            run_id=self._run_id,
            params=params,
            enable_review=self._warmup,  # 仅 warmup work session review
            registry=self._registry,
        )
        judge_agent = HermesContainerAgent(
            container=self._container,
            agent_name=f"lift-hermes-judge-{short_id()}",
            run_id=self._run_id,
            params=params,
            enable_review=False,
            registry=self._registry,
        )
        await asyncio.gather(work_agent.initialize(), judge_agent.initialize())
        return WorkerJudgerPair(
            work_agent=work_agent,
            judge_agent=judge_agent,
            work_session_id=work_session_id,
            judge_session_id=judge_session_id,
        )
