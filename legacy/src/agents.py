from __future__ import annotations
import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import shutil
import subprocess
import time
from abc import ABC, abstractmethod
from pathlib import Path

import os
from pydantic import BaseModel
from src.config import CONFIG, LOGGER, _PROJECT_ROOT
from src.report.langfuse_reporting import emit_pre_chat_state
from src.models import CustomTags
from src.utils import short_id

GMT_PLUS_8 = timezone(timedelta(hours=8), name="GMT+8")
WEEKDAY_ABBR = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


# 与 hermes-helper/hermes_runner.py 中的协议哨兵保持一致。
_HERMES_RUNNER_TASK_END = "__evo_task_end__"
_HERMES_RUNNER_MSG_END = "__evo_msg_end__"
_HERMES_RUNNER_RESP_END = "__evo_resp_end__"




def _upsert_env_var(file_path: str | Path, key: str, value: str) -> None:
    path = Path(file_path).expanduser().resolve()
    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()

    target_prefix = f"{key}="
    replaced = False
    for idx, line in enumerate(lines):
        if line.startswith(target_prefix):
            lines[idx] = f"{target_prefix}{value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{target_prefix}{value}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _parse_json_loose(raw: str) -> dict:
    """
    `openclaw ... --json` may still print a non-JSON prefix line.
    Parse the last JSON object from output robustly.
    """
    # LOGGER.info("Raw output: %s", raw)
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _format_message_timestamp() -> str:
    now = datetime.now(GMT_PLUS_8)
    weekday = WEEKDAY_ABBR[now.weekday()]
    return f"[{weekday} {now.strftime('%Y-%m-%d %H:%M:%S')} GMT+8]"


def _resolve_project_path(path_value: str | Path) -> Path:
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    return path.resolve()


def copy_skill_dir_to(workspace_dir: Path, skill_path: str | Path) -> Path:
    """将 skills 目录拷贝到 ``workspace_dir`` 下，OpenClaw / Hermes 共用。"""
    source_dir = _resolve_project_path(skill_path)
    if not source_dir.exists():
        raise ValueError(f"Skill path does not exist: {source_dir}")
    if not source_dir.is_dir():
        raise ValueError(f"Skill path is not a directory: {source_dir}")
    if source_dir.name != "skills":
        raise ValueError(f"skills directory must be named 'skills', but got {source_dir.name}")
    destination_dir = workspace_dir / source_dir.name
    shutil.copytree(source_dir, destination_dir, dirs_exist_ok=True)
    LOGGER.info("Copied skill directory to workspace: %s -> %s", source_dir, destination_dir)
    return destination_dir


def copy_material_dir_to(workspace_dir: Path, material_path: str | Path) -> Path:
    """将 materials 目录拷贝到 ``workspace_dir`` 下，OpenClaw / Hermes 共用。"""
    source_dir = _resolve_project_path(material_path)
    if not source_dir.exists():
        raise ValueError(f"Material path does not exist: {source_dir}")
    if not source_dir.is_dir():
        raise ValueError(f"Material path is not a directory: {source_dir}")
    destination_dir = workspace_dir / source_dir.name
    shutil.copytree(source_dir, destination_dir, dirs_exist_ok=True)
    LOGGER.info("Copied material directory to workspace: %s -> %s", source_dir, destination_dir)
    return destination_dir


class Agent(ABC):
    @property
    @abstractmethod
    def env_file_path(self) -> str | None:
        """返回agent的env文件路径"""
        pass

    @abstractmethod
    async def _restart_gateway(self) -> None:
        """重启agent的gateway"""
        pass
    
    @staticmethod
    @abstractmethod
    async def evolve(session_id: str) -> None:
        """启动agent进化"""
        pass

    @staticmethod
    def disable_evolve() -> None:
        pass

    @staticmethod
    def enable_evolve() -> None:
        pass
    
    @staticmethod
    @abstractmethod
    def reset_evolve(run_id: str, category: str, repeat_index: int) -> None:
        """重置当前 benchmark 闭环后的进化状态，并按 run/category 归档备份。"""
        pass

    @abstractmethod
    async def chat(
        self,
        msg: str,
        session_id: str,
        tags: CustomTags,
        response_schema: BaseModel | None = None,
        *,
        chat_role: str = "work_agent",
    ) -> str:
        pass

    async def end_session(self, session_id: str) -> None:
        """通知 agent 当前 session 已经结束，可以释放该 session 关联的资源
        （例如 Hermes 模式下需要关闭对应的 hermes_runner 子进程，并触发 review）。

        默认实现为 no-op，对不需要资源释放的 agent（如 OpenClaw）保持兼容。
        """
        _ = session_id


@dataclass
class _RunnerHandle:
    """单个 hermes_runner 子进程的句柄。

    一个 ``session_id`` 对应一个独立子进程，跨多次 chat 复用以维持多轮对话记忆。
    work_agent runner 在结束时会触发 background review；judge_agent 不会。
    """
    proc: asyncio.subprocess.Process
    session_id: str
    chat_role: str
    enable_review: bool
    stderr_task: asyncio.Task


class HermesAgent(Agent):
    _next_agent_id: int = 0
    # ``hermes profile create --clone`` 在并发执行时可能竞争 ~/.hermes 下的
    # 全局清单文件。用类级 asyncio.Lock 保证 profile 创建串行化。
    _profile_create_lock: asyncio.Lock | None = None

    @classmethod
    def _get_profile_create_lock(cls) -> asyncio.Lock:
        if cls._profile_create_lock is None:
            cls._profile_create_lock = asyncio.Lock()
        return cls._profile_create_lock

    @property
    def env_file_path(self) -> str | None:
        return CONFIG.hermes_env_file

    def __init__(
        self,
        workspace_path: str | Path,
    ) -> None:
        self._workspace_path = Path(workspace_path)
        self._agent_id = HermesAgent._next_agent_id
        HermesAgent._next_agent_id += 1
        self._profile_name = f"hermes-{self._agent_id}"
        # session_id -> _RunnerHandle。session_id 由调用方生成（带毫秒时间戳 + uuid），
        # 不同 session 必然不同 key，因此无需额外锁；同一 session 的 chat 又由 run_task
        # 内部串行 await 调用，也不会自我并发。
        self._runners: dict[str, _RunnerHandle] = {}
        # 当 task 间并发执行时，多个 task 可能同时调用 copy_task_assets 往同一
        # workspace 拷 skills / materials；用实例级 asyncio.Lock 串行化，避免
        # shutil.copytree 在 dirs_exist_ok=True 下的并发写竞态。
        self._assets_copy_lock = asyncio.Lock()

    @property
    def workspace_dir(self) -> Path:
        return self._workspace_path

    def copy_task_assets(
        self,
        skill_path: str | Path | None,
        material_path: str | Path | None,
    ) -> None:
        """在每个 task 执行前，把 task 级 skills / materials 拷贝到当前 phase workspace。

        ⚠️ 仅用于串行执行场景；task 间并发时请使用 ``copy_task_assets_async``，
        否则多个协程并发 copytree 同一 workspace 会出现竞态。
        """
        self._workspace_path.mkdir(parents=True, exist_ok=True)
        if skill_path:
            copy_skill_dir_to(self._workspace_path, skill_path)
        if material_path:
            copy_material_dir_to(self._workspace_path, material_path)

    async def copy_task_assets_async(
        self,
        skill_path: str | Path | None,
        material_path: str | Path | None,
    ) -> None:
        """``copy_task_assets`` 的并发安全版本，用 instance 锁串行化文件拷贝。

        拷贝是同步且通常很快，全部塞进 lock 内部不会拖累 task 并发的 LLM 等待时间。
        """
        async with self._assets_copy_lock:
            await asyncio.to_thread(self.copy_task_assets, skill_path, material_path)

    @classmethod
    async def create(
        cls,
        workspace_path: str | Path,
    ) -> HermesAgent:
        instance = cls(workspace_path)
        await instance._ensure_profile()
        instance._init_env()
        return instance

    async def aclose(self) -> None:
        """关闭当前 HermesAgent 持有的所有 hermes_runner 子进程。

        必须在每个 benchmark_path 运行完成（无论成功或异常）时调用，
        否则 hermes_runner 子进程会作为孤儿进程残留在系统里。
        """
        for sid in list(self._runners.keys()):
            try:
                await self.end_session(sid)
            except Exception:
                LOGGER.exception("Failed to end runner for session %s during aclose", sid)

    async def _ensure_profile(self) -> None:
        async with self._get_profile_create_lock():
            try:
                await self._run_cmd_checked(
                    ["hermes", "profile", "create", self._profile_name, "--clone"]
                )
            except subprocess.CalledProcessError:
                await self._run_cmd_checked(
                    ["hermes", "profile", "delete", self._profile_name, "-y"]
                )
                await self._run_cmd_checked(
                    ["hermes", "profile", "create", self._profile_name, "--clone"]
                )

    def _init_env(self) -> None:
        env = self._env_file
        if CONFIG.langfuse_public_key:
            _upsert_env_var(env, "HERMES_LANGFUSE_PUBLIC_KEY", CONFIG.langfuse_public_key)
        if CONFIG.langfuse_secret_key:
            _upsert_env_var(env, "HERMES_LANGFUSE_SECRET_KEY", CONFIG.langfuse_secret_key)
        if CONFIG.langfuse_base_url:
            _upsert_env_var(env, "HERMES_LANGFUSE_BASE_URL", CONFIG.langfuse_base_url)
        if CONFIG.firecrawl_api_key:
            _upsert_env_var(env, "HERMES_FIRECRAWL_API_KEY", CONFIG.firecrawl_api_key)

    @property
    def _env_file(self) -> Path:
        return Path(CONFIG.hermes_dir).expanduser().resolve() / "profiles" / self._profile_name / ".env"

    def reset_pre_chat_state(self) -> None:
        # 已无内部状态需要重置；保留方法以向后兼容外部调用方（如 hermes_main 中的旧调用点）。
        return None

    @staticmethod
    async def evolve(session_id: str) -> None:
        """Hermes无需主动触发进化"""
        _ = session_id
        pass

    @staticmethod
    def disable_evolve() -> None:
        pass

    @staticmethod
    def enable_evolve() -> None:
        pass

    @staticmethod
    def reset_evolve(run_id: str, category: str, repeat_index: int) -> None:
        _ = (run_id, category, repeat_index)
        pass

    async def _run_cmd_checked(self, args: list[str]) -> None:
        LOGGER.info("Running command: %s", " ".join(args))
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(
                proc.returncode,
                args,
                output=stdout,
                stderr=stderr,
            )
        LOGGER.info("Command succeeded: %s", " ".join(args))

    async def _restart_gateway(self) -> None:
        """新链路下不再使用 gateway 子进程；保留方法以满足 Agent 抽象基类签名，无操作。"""
        return None

    async def _spawn_runner(
        self,
        session_id: str,
        chat_role: str,
        enable_review: bool,
    ) -> _RunnerHandle:
        """为 ``session_id`` 拉起一个常驻 hermes_runner 子进程。

        - work_agent + 非 evolve 阶段 ⇒ 附 ``--enable-review``，结束时跑 background review。
        - work_agent + evolve 阶段 ⇒ 不附；evolve 之后不再需要 review。
        - judge_agent ⇒ 不附；结束时直接退出。
        - stdin/stdout/stderr 全部用 PIPE，stderr 起后台 task 转给 LOGGER，
          防止子进程因 PIPE 缓冲写满而阻塞。
        """
        hermes_dir = Path(CONFIG.hermes_dir).expanduser().resolve()
        hermes_agent_dir = hermes_dir / "hermes-agent"
        profile_home = hermes_dir / "profiles" / self._profile_name
        runner_script = _PROJECT_ROOT / "hermes-helper" / "hermes_runner.py"
        # 使用 hermes-agent 仓库自带 venv 中的 python，确保依赖环境与 hermes 保持一致。
        python_exe = hermes_agent_dir / "venv" / "bin" / "python"

        cmd: list[str] = [
            str(python_exe),
            str(runner_script),
            "--hermes-agent-dir", str(hermes_agent_dir),
            "--profile-home", str(profile_home),
            "--workspace", str(self._workspace_path),
            "--model", CONFIG.hermes_model_name or "",
            "--base-url", (CONFIG.hermes_api_url or "").strip(),
            "--api-key", CONFIG.hermes_api_key or "",
            "--session-id", session_id,
            "--max-tokens", str(CONFIG.hermes_max_tokens),
        ]
        if enable_review:
            cmd.append("--enable-review")

        env = os.environ.copy()
        env["HERMES_HOME"] = str(profile_home)
        env["TERMINAL_CWD"] = str(self._workspace_path)

        LOGGER.info(
            "Spawning hermes_runner: profile=%s session=%s role=%s enable_review=%s",
            self._profile_name,
            session_id,
            chat_role,
            enable_review,
        )
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(self._workspace_path),
        )

        async def _drain_stderr() -> None:
            assert proc.stderr is not None
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                LOGGER.info(
                    "[hermes_runner stderr session=%s] %s",
                    session_id,
                    line.decode("utf-8", errors="replace").rstrip(),
                )

        stderr_task = asyncio.create_task(_drain_stderr())

        handle = _RunnerHandle(
            proc=proc,
            session_id=session_id,
            chat_role=chat_role,
            enable_review=enable_review,
            stderr_task=stderr_task,
        )
        self._runners[session_id] = handle
        return handle

    async def _send_and_recv(self, handle: _RunnerHandle, msg: str) -> str:
        """向 runner 发送一条 user message，并阻塞读取直到 ``__evo_resp_end__``。

        协议：父进程一次性写入 ``msg + "\n" + __evo_msg_end__ + "\n"`` 表示这条
        user message 结束；runner 处理完后写出 final_response，然后写一行
        ``__evo_resp_end__`` 作为本轮回复终止哨兵。
        """
        proc = handle.proc
        if proc.stdin is None or proc.stdout is None:
            raise RuntimeError(
                f"hermes_runner stdin/stdout not piped for session {handle.session_id}"
            )
        if proc.returncode is not None:
            raise RuntimeError(
                f"hermes_runner already exited (rc={proc.returncode}) for session {handle.session_id}"
            )

        payload = msg + "\n" + _HERMES_RUNNER_MSG_END + "\n"
        proc.stdin.write(payload.encode("utf-8"))
        await proc.stdin.drain()

        chunks: list[str] = []
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                # 子进程提前退出。读完 stderr task 已经写了 LOGGER。
                rc = proc.returncode
                raise RuntimeError(
                    f"hermes_runner stdout closed unexpectedly (rc={rc}) "
                    f"for session {handle.session_id}"
                )
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if line == _HERMES_RUNNER_RESP_END:
                break
            chunks.append(line)
        return "\n".join(chunks)

    async def end_session(self, session_id: str) -> None:
        """通知对应 runner 结束会话：写入 ``__evo_task_end__``、关闭 stdin，等待退出。

        - work_agent 的 runner 会在退出前跑 background review，因此此处的 await
          会一直阻塞到 review 完成；这正是希望的语义。
        - judge_agent 的 runner 不跑 review，几乎立即退出。
        """
        handle = self._runners.pop(session_id, None)
        if handle is None:
            return
        proc = handle.proc

        try:
            if proc.stdin is not None and proc.returncode is None:
                try:
                    proc.stdin.write((_HERMES_RUNNER_TASK_END + "\n").encode("utf-8"))
                    LOGGER.info("Sent task end sentinel to runner session=%s", session_id)
                    await proc.stdin.drain()
                    proc.stdin.close()
                    LOGGER.info("Closed stdin for runner session=%s", session_id)
                except Exception:
                    LOGGER.exception(
                        "Failed to send task end sentinel to runner session=%s",
                        session_id,
                    )
            try:
                await proc.wait()
            except Exception:
                LOGGER.exception(
                    "Failed waiting for runner exit session=%s",
                    session_id,
                )
                if proc.returncode is None:
                    try:
                        proc.kill()
                        await proc.wait()
                        LOGGER.info("Killed runner session=%s", session_id)
                    except ProcessLookupError:
                        pass
        finally:
            handle.stderr_task.cancel()
            try:
                await handle.stderr_task
            except (asyncio.CancelledError, Exception):
                pass

    async def chat(
        self,
        msg: str,
        session_id: str,
        tags: CustomTags,
        response_schema: BaseModel | None = None,  # hermes agent 对该参数无原生支持，下面用 prompt 兜底
        *,
        chat_role: str = "work_agent",
    ) -> str:
        if chat_role in ("work_agent", "judge_agent"):
            emit_pre_chat_state(session_id=session_id, tags=tags, chat_role=chat_role)

        msg = f"{_format_message_timestamp()}\n{msg}"
        if response_schema is not None:
            # hermes_runner.py 不支持原生 JSON schema 约束输出，把 schema 作为
            # 指令追加到 user message，最大努力等价。
            schema_text = json.dumps(response_schema.model_json_schema(), ensure_ascii=False)
            msg = (
                f"{msg}\n\n请严格按照以下 JSON Schema 输出一个 JSON 对象，"
                f"不要包含任何额外说明文字：\n{schema_text}"
            )

        # 同一 session_id 复用同一 runner 进程，跨多轮对话保留记忆；
        # 不同 session_id 必然不同进程（满足 work / judge 分离要求）。
        # review 触发条件（三者必须同时满足）：
        #   1. chat_role == "work_agent"（judge 永远不 review）
        #   2. 非 evolve 阶段（evolve 完成后再 review 会污染下一轮 baseline 起点）
        #   3. tags.enable_review 未被显式关闭（如 exam test_baseline 这种"对照组"场景）
        handle = self._runners.get(session_id)
        if handle is None:
            enable_review = (
                chat_role == "work_agent"
                and not bool(getattr(tags, "is_evolve_turn", False))
                and bool(getattr(tags, "enable_review", True))
            )
            handle = await self._spawn_runner(session_id, chat_role, enable_review)

        return await self._send_and_recv(handle, msg)


class OpenClawAgent(Agent):
    def __init__(
        self,
        run_id: str,
        task_id: str,
        agent_name: str,
        skills_dir: str | None = None,
        material_dir: str | None = None,
        workspace_dir: str | Path | None = None,
    ) -> None:
        self.run_id = run_id
        self.task_id = task_id
        self.agent_name = agent_name
        self._skills_dir = skills_dir
        self._material_dir = material_dir
        self._workspace_dir_arg = workspace_dir
        self.workspace_dir: Path | None = None

    def initialize(self) -> None:
        self.workspace_dir = self._mk_workspace(self._workspace_dir_arg)
        if self._skills_dir:
            self.copy_skill_dir(self._skills_dir)
        if self._material_dir:
            self.copy_material_dir(self._material_dir)
        self._create_agent()

    def _openclaw_env(self, extra_env: dict[str, str] | None = None) -> dict[str, str]:
        env = os.environ.copy()
        env_file_path = self.env_file_path
        if env_file_path:
            env_path = Path(env_file_path).expanduser().resolve()
            if env_path.exists():
                for raw_line in env_path.read_text(encoding="utf-8").splitlines():
                    line = raw_line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    env[key] = value
        if extra_env:
            env.update(extra_env)
        return env

    def _create_agent(self, max_retries: int = 3) -> None:
        def chk_existance():
            result = subprocess.run(["openclaw", "agents", "list"], check=False, capture_output=True)
            if result.returncode == 1:
                LOGGER.info(f"Error when executing openclaw agents list: {result.stderr.decode()}")
                raise ValueError("Failed to list agents")
            result = result.stdout.decode(encoding="utf-8")
            return result.find(self.agent_name) != -1

        for attempt in range(max_retries):
            if chk_existance():
                LOGGER.info(f"Agent {self.agent_name} already exists")
                return
            try:
                subprocess.run(
                    [
                        "openclaw",
                        "agents",
                        "add",
                        self.agent_name,
                        "--model",
                        CONFIG.openclaw_model_name or "",
                        "--workspace",
                        str(self.workspace_dir),
                    ],
                    check=True,
                    env=self._openclaw_env(),
                )
                if chk_existance():
                    LOGGER.info(f"Agent {self.agent_name} created successfully, model={CONFIG.openclaw_model_name}")
                    return
            except subprocess.CalledProcessError:
                pass
            LOGGER.warning(
                "Failed to create agent %s (attempt %d/%d), retrying with new name...",
                self.agent_name,
                attempt + 1,
                max_retries,
            )
            time.sleep(1)
            self.agent_name = "evobench-agent_name-" + short_id()

        raise ValueError(f"Failed to create agent after {max_retries} retries")
        
    
    @property
    def env_file_path(self) -> str | None:
        return CONFIG.openclaw_env_file
    
    def _mk_workspace(self, workspace_dir: str | Path | None = None) -> Path:
        """
        Create workspace directory at ``<cwd>/results/<run_id>/outcome/<task_id>``.
        If task id starts with "baseline-", then it is a baseline run.
        If it starts with "evolved-", then it is an evolved run.
        If it already exists, do nothing.
        """
        if workspace_dir is None:
            workspace_path = (
                Path.cwd() / "results" / str(self.run_id) / "outcome" / str(self.task_id)
            )
        else:
            workspace_path = Path(workspace_dir).expanduser().resolve()
        workspace_path.mkdir(parents=True, exist_ok=True)
        return workspace_path

    def _resolve_project_path(self, path_value: str | Path) -> Path:
        return _resolve_project_path(path_value)

    def copy_skill_dir(self, skill_path: str | Path) -> Path:
        return copy_skill_dir_to(self.workspace_dir, skill_path)

    def copy_material_dir(self, material_path: str | Path) -> Path:
        return copy_material_dir_to(self.workspace_dir, material_path)

    async def _run_cmd_checked_capture(self, args: list[str]) -> str:
        LOGGER.info("Running command: %s", " ".join(args))
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._openclaw_env(),
        )
        stdout, stderr = await proc.communicate()
        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        if proc.returncode != 0:
            LOGGER.error("Command failed: %s", " ".join(args))
            LOGGER.error("stdout: %s", stdout_text)
            LOGGER.error("stderr: %s", stderr_text)
            raise subprocess.CalledProcessError(
                proc.returncode,
                args,
                output=stdout_text,
                stderr=stderr_text,
            )
        LOGGER.info("Command succeeded: %s", " ".join(args))
        return stdout_text

    @staticmethod
    def _openclaw_config_path() -> Path:
        return Path("~/.openclaw/openclaw.json").expanduser().resolve()

    @staticmethod
    def _load_openclaw_json(path: Path) -> dict:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("top-level JSON value must be an object")
        return data

    @staticmethod
    def _dump_openclaw_json(path: Path, data: dict) -> None:
        with path.open("w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")

    @staticmethod
    def _add_openclaw_config_fields(data: dict) -> tuple[dict, dict[str, int]]:
        patched = deepcopy(data)
        stats = {
            "model_compat_added": 0,
            "langfuse_hooks_added": 0,
        }

        providers = patched.get("models", {}).get("providers", {})
        if isinstance(providers, dict):
            for provider_cfg in providers.values():
                if not isinstance(provider_cfg, dict):
                    continue
                models = provider_cfg.get("models", [])
                if not isinstance(models, list):
                    continue
                for model_cfg in models:
                    if not isinstance(model_cfg, dict):
                        continue
                    compat = model_cfg.get("compat")
                    if not isinstance(compat, dict):
                        model_cfg["compat"] = {"supportsUsageInStreaming": True}
                        stats["model_compat_added"] += 1
                    elif "supportsUsageInStreaming" not in compat:
                        compat["supportsUsageInStreaming"] = True
                        stats["model_compat_added"] += 1

        plugin_entries = patched.get("plugins", {}).get("entries", {})
        if isinstance(plugin_entries, dict):
            langfuse_cfg = plugin_entries.get("langfuse-tracer")
            if isinstance(langfuse_cfg, dict):
                hooks = langfuse_cfg.get("hooks")
                if not isinstance(hooks, dict):
                    langfuse_cfg["hooks"] = {"allowConversationAccess": True}
                    stats["langfuse_hooks_added"] += 1
                elif "allowConversationAccess" not in hooks:
                    hooks["allowConversationAccess"] = True
                    stats["langfuse_hooks_added"] += 1

        return patched, stats

    @staticmethod
    def _remove_openclaw_config_fields(data: dict) -> tuple[dict, dict[str, int]]:
        patched = deepcopy(data)
        stats = {
            "model_compat_removed": 0,
            "langfuse_hooks_removed": 0,
        }

        providers = patched.get("models", {}).get("providers", {})
        if isinstance(providers, dict):
            for provider_cfg in providers.values():
                if not isinstance(provider_cfg, dict):
                    continue
                models = provider_cfg.get("models", [])
                if not isinstance(models, list):
                    continue
                for model_cfg in models:
                    if not isinstance(model_cfg, dict):
                        continue
                    compat = model_cfg.get("compat")
                    if isinstance(compat, dict) and "supportsUsageInStreaming" in compat:
                        del compat["supportsUsageInStreaming"]
                        stats["model_compat_removed"] += 1
                        if not compat:
                            del model_cfg["compat"]

        plugin_entries = patched.get("plugins", {}).get("entries", {})
        if isinstance(plugin_entries, dict):
            langfuse_cfg = plugin_entries.get("langfuse-tracer")
            if isinstance(langfuse_cfg, dict):
                hooks = langfuse_cfg.get("hooks")
                if isinstance(hooks, dict) and "allowConversationAccess" in hooks:
                    del hooks["allowConversationAccess"]
                    stats["langfuse_hooks_removed"] += 1
                    if not hooks:
                        del langfuse_cfg["hooks"]

        return patched, stats

    @staticmethod
    def initialize_environment(
        *,
        ensure_config_fields: bool = True,
        restart_gateway: bool = True,
        rebuild_runtime: bool = True,
    ) -> None:
        if rebuild_runtime:
            OpenClawAgent.rebuild_evolution_runtime()

        if ensure_config_fields:
            OpenClawAgent.add_fields_to_openclaw_config()

        LOGGER.info("Running command: openclaw plugins enable langfuse-tracer")
        subprocess.run(["openclaw", "plugins", "enable", "langfuse-tracer"], check=True)

        subprocess.run(["openclaw", "plugins", "enable", "self-evolving-plugin-pro"], check=True)
        
        if restart_gateway:
            LOGGER.info("Running command: openclaw gateway restart")
            subprocess.run(["openclaw", "gateway", "restart"], check=True)

    @staticmethod
    def remove_fields_from_openclaw_config() -> dict[str, int]:
        config_path = OpenClawAgent._openclaw_config_path()
        LOGGER.info("Loading OpenClaw config: %s", config_path)
        data = OpenClawAgent._load_openclaw_json(config_path)
        patched, stats = OpenClawAgent._remove_openclaw_config_fields(data)
        if patched != data:
            OpenClawAgent._dump_openclaw_json(config_path, patched)
            LOGGER.info("Removed fields from OpenClaw config: %s", stats)
        else:
            LOGGER.info("OpenClaw config already has fields removed: %s", stats)
        return stats

    @staticmethod
    def add_fields_to_openclaw_config() -> dict[str, int]:
        config_path = OpenClawAgent._openclaw_config_path()
        LOGGER.info("Loading OpenClaw config: %s", config_path)
        data = OpenClawAgent._load_openclaw_json(config_path)
        patched, stats = OpenClawAgent._add_openclaw_config_fields(data)
        if patched != data:
            OpenClawAgent._dump_openclaw_json(config_path, patched)
            LOGGER.info("Added fields to OpenClaw config: %s", stats)
        else:
            LOGGER.info("OpenClaw config already has fields added: %s", stats)
        return stats

    @staticmethod
    def rebuild_evolution_runtime() -> None:
        script_path = _PROJECT_ROOT / "scripts" / "rebuild-evolution-runtime.sh"
        LOGGER.info("Running command: bash %s", script_path)
        subprocess.run(["bash", str(script_path)], check=True)

    @staticmethod
    def _run_review_command() -> None:
        LOGGER.info("Running command: openclaw learn review")
        subprocess.run(["openclaw", "learn", "review"], check=True)

    @staticmethod
    async def evolve(session_id: str) -> None:
        _ = session_id
        await asyncio.to_thread(OpenClawAgent.remove_fields_from_openclaw_config)
        try:
            await asyncio.to_thread(OpenClawAgent._run_review_command)
        finally:
            await asyncio.to_thread(OpenClawAgent.add_fields_to_openclaw_config)

    @staticmethod
    def disable_evolve() -> None:
        subprocess.run(["openclaw", "plugins", "disable", "self-evolving-plugin-pro"], check=True)
        LOGGER.info("Running command: openclaw plugins disable self-evolving-plugin-pro")
        subprocess.run(["openclaw", "gateway", "restart"], check=True)
        LOGGER.info("Running command: openclaw gateway restart")

    @staticmethod
    def enable_evolve() -> None:
        subprocess.run(["openclaw", "plugins", "enable", "self-evolving-plugin-pro"], check=True)
        LOGGER.info("Running command: openclaw plugins enable self-evolving-plugin-pro")
        subprocess.run(["openclaw", "gateway", "restart"], check=True)
        LOGGER.info("Running command: openclaw gateway restart")
    
    @staticmethod
    def reset_evolve(run_id: str, category: str, repeat_index: int) -> None:
        script_path = _PROJECT_ROOT / "scripts" / "reset-evolution.sh"
        LOGGER.info(
            "Running command: bash %s %s %s %s",
            script_path,
            run_id,
            category,
            repeat_index,
        )
        subprocess.run(
            ["bash", str(script_path), run_id, category, str(repeat_index)],
            check=True,
        )

    async def chat(
        self,
        msg: str,
        session_id: str,
        tags: CustomTags,
        response_schema: BaseModel | None = None,
        *,
        chat_role: str = "work_agent",
    ) -> str:
        tags.agent_name = self.agent_name
        emit_pre_chat_state(session_id=session_id, tags=tags, chat_role=chat_role)
        _ = response_schema  # OpenClaw CLI mode currently ignores schema output control.
        msg = f"{_format_message_timestamp()}\n{msg}"
        command = [
            "openclaw",
            "agent",
            "--agent",
            self.agent_name,
            "--message",
            msg,
            "--session-id",
            session_id,
            "--json",
            "--local"
        ]
        stdout = (await self._run_cmd_checked_capture(command)).strip()
        if not stdout:
            raise ValueError("OpenClaw returned empty stdout")

        payload = _parse_json_loose(stdout)
        if not payload:
            LOGGER.error("Failed to parse OpenClaw output as JSON: %s", stdout)
            raise ValueError("OpenClaw returned invalid JSON")
        # non-local mode
        # payloads = payload.get("result").get("payloads") 
        # local mode
        payloads = payload.get("payloads")
        if not isinstance(payloads, list):
            raise ValueError("OpenClaw JSON missing payloads list")

        texts = []
        for item in payloads:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str) and text:
                texts.append(text)
                
        # 暂时不确定mediaUrls这里会放些什么先暂存
        mediaUrls = []
        for item in payloads:
            if not isinstance(item, dict):
                continue
            mediaUrl = item.get("mediaUrl")
            if isinstance(mediaUrl, str) and mediaUrl:
                mediaUrls.append(mediaUrl)
        LOGGER.info("mediaUrls: %s", '\n'.join(mediaUrls))
        # return "\n".join(texts) + f"\nmediaUrls: \n{'\n'.join(mediaUrls)}"
        return "\n".join(texts)

    async def _restart_gateway(self) -> None:
        await self._run_cmd_checked_capture(["openclaw", "gateway", "restart"])
    
