"""GenericAgent 容器内 chat transport 与 WorkerJudgerPair 工厂。

GA 进程协议（``agentmain.py --task <iodir> --nobg``）：

1. 宿主向 ``/opt/GenericAgent/temp/<iodir>/input.txt`` 写入首条消息；
2. ``docker exec -d`` 起 GA 进程，``--nobg`` 强制阻塞模式（不再 fork 后台子进程）；
3. GA 完成首轮 chat 后写 ``output.txt`` 并以 ``[ROUND END]`` 结尾；
4. 后续轮：宿主写 ``reply.txt`` → GA 写 ``output<N>.txt``（N=1, 2, ...）。

GA 自带 1200s ``queue.get`` 与 300×2s reply 轮询超时，10 分钟内宿主未续 reply
GA 主循环就 ``break`` 退出。容器 cleanup 时 docker rm -f 会一并 SIGKILL 残留 GA。
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path

from src.config import LOGGER
from src.lift.adapters.container.exec import docker_exec_shell_async
from src.lift.adapters.genericagent.container_exec import GenericAgentContainerContext
from src.lift.eval.chat_agent import ChatAgent
from src.lift.eval.worker_judger import WorkerJudgerPair
from src.models import SuiteTask
from src.utils import short_id

# 单轮 chat 在宿主侧的 wall-clock 上限：与 OpenClaw 对齐 600s。GA 内部 reply 等待
# 上限是 600s（300 × 2s），与外层一致。超时返回带 ``CHAT_EXEC_TIMEOUT_MARKER`` 前缀
# 的字符串走 ``_looks_like_provider_error`` 重试通道。
CHAT_EXEC_TIMEOUT_SECONDS = 600.0
CHAT_EXEC_TIMEOUT_MARKER = "chat exec timeout"

_GA_TEMP_ROOT = "/opt/GenericAgent/temp"
_ROUND_END_MARKER = "[ROUND END]"
_POLL_INTERVAL_SECONDS = 2.0


class GenericAgentContainerAgent(ChatAgent):
    """单个 GenericAgent 实例的 chat transport。

    每实例对应容器内一个独立 GA 进程（独立 ``iodir``）。``initialize`` 仅准备
    iodir 路径；GA 进程在首次 ``chat`` 调用时通过 ``docker exec -d`` 拉起。
    后续轮 ``chat`` 写 ``reply.txt`` 让既有 GA 主循环继续。

    LIFT factory 每题新建 work / judge 各一对实例，所以同一 ``GenericAgentContainerAgent``
    只服务一个 ``session_id``，无须切换。
    """

    def __init__(
        self,
        *,
        container: GenericAgentContainerContext,
        agent_name: str,
        workspace_dir: Path,
        role: str,
    ) -> None:
        self._container = container
        self._agent_name = agent_name
        self._workspace_dir = workspace_dir
        self._role = role  # work / judge — 仅用于 iodir 命名
        self._iodir: str = ""
        self._round: int = 0  # 已完成的对话轮次；0 表示尚未发起首轮
        self._started: bool = False

    @property
    def agent_name(self) -> str:
        return self._agent_name

    async def initialize(self) -> None:
        """准备 iodir：宿主 workspace mkdir + 容器内 iodir mkdir。

        GA 进程不在此处启动——首次 ``chat`` 才把 input.txt 写好后再 ``docker exec -d``
        拉起，避免 GA 在没有 input 的情况下立即 ``while True`` 报 file-not-found。
        """
        self._iodir = f"lift-{self._role}-{short_id()}"
        self._workspace_dir.mkdir(parents=True, exist_ok=True)
        await docker_exec_shell_async(
            self._container.container_name,
            f"mkdir -p {_GA_TEMP_ROOT}/{self._iodir}",
        )

    async def chat(self, message: str, *, session_id: str) -> str:
        """单轮 chat：首轮启动 GA + 写 input.txt；后续轮写 reply.txt。"""
        if not self._started:
            return await self._first_chat(message, session_id=session_id)
        return await self._reply_chat(message)

    async def _first_chat(self, message: str, *, session_id: str) -> str:
        await self._write_text("input.txt", message)
        await self._launch_ga(session_id=session_id)
        self._started = True
        return await self._wait_output(self._round)

    async def _reply_chat(self, message: str) -> str:
        await self._write_text("reply.txt", message)
        self._round += 1
        return await self._wait_output(self._round)

    async def _write_text(self, name: str, content: str) -> None:
        """把 ``content`` 以 base64 安全写入容器内 ``temp/<iodir>/<name>``。"""
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        path = f"{_GA_TEMP_ROOT}/{self._iodir}/{name}"
        # base64 字符串只含 [A-Za-z0-9+/=]，shell quote 安全；printf '%s' 避免
        # echo 自带换行污染。
        script = f"printf '%s' {encoded} | base64 -d > {path}"
        await docker_exec_shell_async(self._container.container_name, script)

    async def _launch_ga(self, *, session_id: str) -> None:
        """``docker exec -d`` 启动 GA 进程。

        ``-d`` 让 docker exec 立即返回，不等待 GA 主循环；GA 自身 stdout / stderr
        重定向到容器内日志文件，避免 docker exec 通信通道堵塞。
        ``--nobg`` 强制阻塞主路径，不再走 GA 自己 fork ``subprocess.Popen`` 派生
        子进程那条分支（那条分支会让父 docker exec 立即退出但子进程在 docker
        exec 进程树外，反而难以追踪 / cleanup）。

        cwd 切到 ``/workspace/task``：LIFT 把 task materials bind 挂到这里，
        与 OpenClaw work agent 一致。GA 工具 ``code_run`` / ``file_read`` 的
        ``Handler.cwd`` 默认 ``./temp``（相对路径），跟随进程 cwd，所以切到
        ``/workspace/task`` 后这些工具就能直接看到 task materials。
        ``agentmain.py`` 内部用 ``script_dir = os.path.dirname(os.path.abspath(__file__))``
        推 ``iodir`` / ``memory`` 绝对路径，与进程 cwd 无关，因此 LIFT 文件
        I/O 协议（input.txt / output*.txt / reply.txt）不受影响。
        """
        log_dir = f"{_GA_TEMP_ROOT}/{self._iodir}"
        cmd = [
            "docker", "exec", "-d",
            "-e", f"LIFT_GA_SESSION_ID={session_id}",
            self._container.container_name,
            "bash", "-lc",
            (
                f"mkdir -p /workspace/task && "
                f"cd /workspace/task && "
                f"python /opt/GenericAgent/agentmain.py --task {self._iodir} --nobg "
                f"</dev/null "
                f">>{log_dir}/ga.stdout.log "
                f"2>>{log_dir}/ga.stderr.log"
            ),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                "docker exec -d failed launching GA in "
                f"{self._container.container_name}: "
                f"{stderr.decode(errors='replace')}"
            )

    async def _wait_output(self, round_idx: int) -> str:
        """轮询容器内 ``output<N>.txt``，``[ROUND END]`` 出现即返回去哨兵后的内容。

        ``round_idx == 0`` 时文件名为 ``output.txt``（GA agentmain.py 中 ``nround = ''``）。
        cat 失败（文件未生成）走 ``true`` 兜底，避免 docker exec 抛错。
        """
        suffix = "" if round_idx == 0 else str(round_idx)
        path = f"{_GA_TEMP_ROOT}/{self._iodir}/output{suffix}.txt"
        loop = asyncio.get_event_loop()
        deadline = loop.time() + CHAT_EXEC_TIMEOUT_SECONDS
        last_text = ""
        while True:
            if loop.time() >= deadline:
                LOGGER.warning(
                    "[genericagent chat] wait output timeout container=%s iodir=%s round=%d",
                    self._container.container_name, self._iodir, round_idx,
                )
                preview = last_text or "<no output captured>"
                # 首行包含 marker，``_looks_like_provider_error`` 取首行作为摘要
                return (
                    f"{CHAT_EXEC_TIMEOUT_MARKER}: GA wait output timed out "
                    f"after {CHAT_EXEC_TIMEOUT_SECONDS:.0f}s\n{preview}"
                )
            try:
                text = await docker_exec_shell_async(
                    self._container.container_name,
                    f"cat {path} 2>/dev/null || true",
                )
            except Exception:  # noqa: BLE001 — 诊断态，docker exec 抖动不能拖垮 chat
                text = ""
            if text:
                last_text = text
            if _ROUND_END_MARKER in text:
                # GA 末尾形如 "...content...\n\n[ROUND END]\n"，去掉哨兵保留对话文本
                return text.replace(_ROUND_END_MARKER, "").rstrip("\n").rstrip() + "\n"
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)


class GenericAgentWorkerJudgerPairFactory:
    """为同一 GA 容器内的题目构建 ``WorkerJudgerPair``。

    每题独立 work / judge 实例（即两个独立 GA 进程，iodir 隔离），LangFuse
    session_id 与 OpenClaw 一致采用 ``user-*`` / ``judge-*`` 前缀，方便 trace
    后处理沿用既有过滤规则。
    """

    def __init__(
        self,
        *,
        container: GenericAgentContainerContext,
        workspace_dir: Path,
    ) -> None:
        self._container = container
        self._workspace_dir = workspace_dir

    async def __call__(self, task: SuiteTask) -> WorkerJudgerPair:
        _ = task  # 当前 GA baseline 不依赖 task 做 per-task 定制
        work_session_id = f"user-{short_id()}"
        judge_session_id = f"judge-{short_id()}"
        work_agent = GenericAgentContainerAgent(
            container=self._container,
            agent_name=f"lift-genericagent-work-{short_id()}",
            workspace_dir=self._workspace_dir,
            role="work",
        )
        judge_agent = GenericAgentContainerAgent(
            container=self._container,
            agent_name=f"lift-genericagent-judge-{short_id()}",
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
