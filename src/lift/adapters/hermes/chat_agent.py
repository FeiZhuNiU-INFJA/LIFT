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
CHAT_EXEC_TIMEOUT_SECONDS = 1200.0
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
    """容器 cleanup 前收尾所有残留 runner（best-effort）。

    统一发 task_end 终止；是否 review 由各 runner spawn 时的 ``--enable-review``
    决定（judge / holdout 本就不带，warmup work 若还没被 evolve_after_task 收掉则会
    补跑一次 review）。正常路径下 warmup work 已在 evolve_after_task 里收尾，这里主要
    兜底 judge 与异常残留。
    """
    reg = session.metadata.get(_RUNNERS_META_KEY)
    if not reg:
        return
    for agent in list(reg.values()):
        try:
            await agent.end_session()
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

    async def end_session(self) -> bool:
        """结束 runner 子进程：发送 ``__evo_task_end__`` 并关闭 stdin，等待退出。

        与 legacy ``HermesAgent.end_session``（``legacy/src/agents.py`` L462-506）一致：
        **无论是否 review，都必须发 task_end 哨兵来终止 runner**。runner 侧收到哨兵后
        是否真跑 background review，**完全**由该 runner spawn 时是否带 ``--enable-review``
        决定（见 ``hermes_runner.py::run_review_if_enabled``），不由结束流程控制——因此
        这里没有 ``review`` 参数。

        本类的 review 契约由构造参数 ``enable_review`` 在 spawn 时锁定：
          - work agent（warmup）→ ``enable_review=True`` → 结束时跑 review 写 ``/opt/data``；
          - judge agent / holdout work → ``enable_review=False`` → 结束时直接退出。

        work runner 的 review 可能耗时，故这里的 ``await proc.wait()`` 会阻塞到 review
        完成（正是期望语义）；超时兜底 kill。

        Returns:
            ``True`` 表示 runner 收到 task_end 后**干净退出**（rc==0，即 review 若启用
            也已跑完）；``False`` 表示超时被 kill 或非零退出（review 未确认完成）。
            上层（``evolve_after_task``）据此对 work runner 做硬保证。
            若 runner 本就不存在（已被收尾），返回 ``True``（无需再收尾）。
        """
        proc = self._proc
        if proc is None:
            return True
        clean = False
        try:
            if proc.stdin is not None and proc.returncode is None:
                try:
                    proc.stdin.write((_TASK_END + "\n").encode("utf-8"))
                    await proc.stdin.drain()
                    proc.stdin.close()
                except Exception:  # noqa: BLE001
                    LOGGER.exception("Failed sending task_end session=%s", self._session_id)
            try:
                # work runner 的 review 可能耗时；给足超时，超时则 kill。
                await asyncio.wait_for(proc.wait(), timeout=CHAT_EXEC_TIMEOUT_SECONDS)
                clean = proc.returncode == 0
                if not clean:
                    LOGGER.warning(
                        "hermes runner session=%s exited rc=%s (review may be incomplete)",
                        self._session_id, proc.returncode,
                    )
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                clean = False
                if proc.returncode is None:
                    LOGGER.warning(
                        "hermes runner session=%s wait timed out; killing (review incomplete)",
                        self._session_id,
                    )
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
        return clean


class HermesWorkerJudgerPairFactory:
    """为同一 Hermes 容器内的题目构建 ``WorkerJudgerPair``。

    **review 契约（spawn 时锁定，不可变）**——对齐 legacy
    ``HermesAgent.chat`` 的 ``enable_review = (chat_role=="work_agent") and
    not is_evolve_turn`` 规则：

    - **work agent runner**：``enable_review=self._warmup``——warmup 阶段一定带
      ``--enable-review``（每题结束跑 review 写 ``/opt/data``）；holdout（测量阶段，
      对应 legacy 的 evolve/baseline turn）一定不带。
    - **judge agent runner**：``enable_review=False``——judge **永远**不 review。

    end_session 不再有 review 参数：终止时统一发 task_end 哨兵，是否 review 只看这里
    spawn 时的 ``--enable-review``。
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
            enable_review=self._warmup,  # 契约：warmup work 一定 review，holdout work 一定不
            registry=self._registry,
        )
        judge_agent = HermesContainerAgent(
            container=self._container,
            agent_name=f"lift-hermes-judge-{short_id()}",
            run_id=self._run_id,
            params=params,
            enable_review=False,  # 契约：judge 永远不 review
            registry=self._registry,
        )
        await asyncio.gather(work_agent.initialize(), judge_agent.initialize())
        return WorkerJudgerPair(
            work_agent=work_agent,
            judge_agent=judge_agent,
            work_session_id=work_session_id,
            judge_session_id=judge_session_id,
        )
