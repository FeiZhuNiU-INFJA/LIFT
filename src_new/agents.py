from __future__ import annotations
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import shutil
import subprocess
import time
from abc import ABC, abstractmethod
from pathlib import Path

from openai import AsyncOpenAI
import os
from pydantic import BaseModel
from src_new.config import CONFIG, LOGGER, _PROJECT_ROOT
from src_new.report.langfuse_reporting import emit_pre_chat_state
from src_new.models import CustomTags
from src_new.paths import outcome_root
from src_new.utils import short_id

GMT_PLUS_8 = timezone(timedelta(hours=8), name="GMT+8")
WEEKDAY_ABBR = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")




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


class HermesAgent(Agent):
    _next_agent_id: int = 0
    # 每个 HermesAgent profile 的 API server 监听端口起点。Hermes 默认是 8642，
    # 在并发 / 串行多 profile 场景会冲突，因此为每个实例分配一个独立端口
    # ``_BASE_API_SERVER_PORT + _agent_id``。50000 起步落在 IANA 动态端口范围
    # （49152-65535）内，对千级别 agent 数仍然安全。
    _BASE_API_SERVER_PORT: int = 50000

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
        self._port = HermesAgent._BASE_API_SERVER_PORT + self._agent_id
        self.client = AsyncOpenAI(
            base_url=f"http://localhost:{self._port}/v1",
            api_key=CONFIG.hermes_api_key,
            timeout=3600.0,
            max_retries=5,
        )
        self._gateway_proc: asyncio.subprocess.Process | None = None
        self._gateway_ready: bool = False
        self.has_emitted_work_pre_span: bool = False
        self.has_emitted_judge_pre_span: bool = False

    @property
    def workspace_dir(self) -> Path:
        return self._workspace_path

    def copy_task_assets(
        self,
        skill_path: str | Path | None,
        material_path: str | Path | None,
    ) -> None:
        """在每个 task 执行前，把 task 级 skills / materials 拷贝到当前 phase workspace。"""
        self._workspace_path.mkdir(parents=True, exist_ok=True)
        if skill_path:
            copy_skill_dir_to(self._workspace_path, skill_path)
        if material_path:
            copy_material_dir_to(self._workspace_path, material_path)

    @classmethod
    async def create(
        cls,
        workspace_path: str | Path,
    ) -> HermesAgent:
        instance = cls(workspace_path)
        await instance._ensure_profile()
        instance._init_env()
        await instance._spawn_gateway_proc()
        return instance

    async def _spawn_gateway_proc(self) -> None:
        """启动当前 profile 的 ``<profile_name> gateway run`` 后台进程，并等待端口就绪。

        ``create`` 与 ``_restart_gateway`` 共用此方法，确保两条路径的启动方式完全一致。
        stdout/stderr 走 DEVNULL，避免长时间运行后 PIPE buffer 写满反压子进程。
        """
        LOGGER.info(
            "Starting command in background: %s gateway run (port=%d)",
            self._profile_name,
            self._port,
        )
        self._gateway_proc = await asyncio.create_subprocess_exec(
            self._profile_name,
            "gateway",
            "run",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await self._wait_for_tcp_ready("localhost", self._port, timeout_s=300.0)
        self._gateway_ready = True

    async def aclose(self) -> None:
        """显式关闭后台 gateway 子进程，释放端口与 profile 占用。

        必须在每个 benchmark_path 运行完成（无论成功或异常）时调用，
        否则 ``<profile_name> gateway run`` 会作为孤儿子进程持续占用端口，
        且 Python 对象 GC 不会触发其退出。
        """
        proc = self._gateway_proc
        if proc is not None and proc.returncode is None:
            LOGGER.info(
                "Closing gateway process for profile %s (pid=%s, port=%d)",
                self._profile_name,
                proc.pid,
                self._port,
            )
            try:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=15.0)
                except asyncio.TimeoutError:
                    LOGGER.warning(
                        "Gateway process %s did not exit in 15s; killing",
                        proc.pid,
                    )
                    proc.kill()
                    await proc.wait()
            except ProcessLookupError:
                pass
        self._gateway_proc = None
        self._gateway_ready = False

    async def _ensure_profile(self) -> None:
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
        if CONFIG.api_server_key:
            _upsert_env_var(env, "API_SERVER_KEY", CONFIG.api_server_key)
        if CONFIG.api_server_enabled:
            _upsert_env_var(env, "API_SERVER_ENABLED", CONFIG.api_server_enabled)
        # 每个 profile 独立监听端口，避免多 HermesAgent 之间端口冲突。
        _upsert_env_var(env, "API_SERVER_PORT", str(self._port))

    @property
    def _env_file(self) -> Path:
        return Path.home() / ".hermes" / "profiles" / self._profile_name / ".env"

    async def switch_session(self, session_id: str) -> None:
        if hasattr(self, "_current_session_id") and self._current_session_id == session_id:
            return
        self._current_session_id = session_id
        _upsert_env_var(self._env_file, "SESSION_ID", session_id)
        await self._restart_gateway()

    def reset_pre_chat_state(self) -> None:
        self.has_emitted_work_pre_span = False
        self.has_emitted_judge_pre_span = False

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

    async def _wait_for_tcp_ready(
        self, host: str, port: int, timeout_s: float = 60.0
    ) -> None:
        deadline = time.monotonic() + timeout_s
        last_exc: Exception | None = None
        delay_s = 0.2
        while time.monotonic() < deadline:
            try:
                reader, writer = await asyncio.open_connection(host, port)
                writer.close()
                await writer.wait_closed()
                return
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                await asyncio.sleep(delay_s)
                delay_s = min(delay_s * 1.5, 2.0)
        raise TimeoutError(
            f"Timed out waiting for TCP {host}:{port} to be ready"
        ) from last_exc

    async def _restart_gateway(self) -> None:
        LOGGER.info(
            "Restarting gateway for profile: %s", self._profile_name
        )
        # 注意：``<profile_name> gateway restart`` 在前台模式下不会立即返回
        # （与 ``<profile_name> gateway run`` 一样会阻塞当前进程）。
        # 因此这里不调用 restart 子命令，而是直接 terminate 掉 ``create`` 阶段
        # 由 ``_spawn_gateway_proc`` 拉起的那个 ``<profile_name> gateway run`` 子进程，
        # 再复用同一方法重新拉起，从而触发 profile env 重新加载。
        if self._gateway_proc is not None and self._gateway_proc.returncode is None:
            LOGGER.info(
                "Terminating existing gateway process for profile %s (pid=%s)",
                self._profile_name,
                self._gateway_proc.pid,
            )
            try:
                self._gateway_proc.terminate()
                try:
                    await asyncio.wait_for(self._gateway_proc.wait(), timeout=15.0)
                except asyncio.TimeoutError:
                    LOGGER.warning(
                        "Gateway process %s did not exit in 15s; killing",
                        self._gateway_proc.pid,
                    )
                    self._gateway_proc.kill()
                    await self._gateway_proc.wait()
            except ProcessLookupError:
                pass
        self._gateway_proc = None
        self._gateway_ready = False

        await self._spawn_gateway_proc()

    async def chat(
        self,
        msg: str,
        session_id: str,
        tags: CustomTags,
        response_schema: BaseModel | None = None, # hermes agent对这个参数无感
        *,
        chat_role: str = "work_agent",
    ) -> str:
        if chat_role == "work_agent" and not self.has_emitted_work_pre_span:
            emit_pre_chat_state(session_id=session_id, tags=tags, chat_role=chat_role)
            self.has_emitted_work_pre_span = True
        elif chat_role == "judge_agent" and not self.has_emitted_judge_pre_span:
            emit_pre_chat_state(session_id=session_id, tags=tags, chat_role=chat_role)
            self.has_emitted_judge_pre_span = True
        msg = f"{_format_message_timestamp()}\n{msg}"
        if response_schema is None:
            response = await self.client.responses.create(
                model="hermes-agent",
                input=msg,
                conversation=session_id,
                max_output_tokens=32768
            )
        else:
            response = await self.client.responses.create(
                model="hermes-agent",
                input=msg,
                conversation=session_id,
                max_output_tokens=32768,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "eval_judge_result",
                        "schema": response_schema.model_json_schema(),
                        "strict": True,
                    }
                },
            )
        texts = []
        for item in response.output:
            if (
                getattr(item, "type", None) == "message"
                and getattr(item, "role", None) == "assistant"
            ):
                for content in getattr(item, "content", []):
                    if getattr(content, "type", None) == "output_text":
                        texts.append(content.text)
        return "\n".join(texts)


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
                        CONFIG.model,
                        "--workspace",
                        str(self.workspace_dir),
                    ],
                    check=True,
                    env=self._openclaw_env(),
                )
                if chk_existance():
                    LOGGER.info(f"Agent {self.agent_name} created successfully")
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
            workspace_path = outcome_root(str(self.run_id)) / str(self.task_id)
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
    
