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
from src.config import CONFIG, LOGGER, _PROJECT_ROOT
from src.report.langfuse_reporting import emit_pre_chat_state
from src.models import FornaxUdfTags
from src.utils import short_id

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
    
    async def _prepare_chat_env(
        self,
        tags: FornaxUdfTags,
    ) -> None:
        env_file_path = self.env_file_path
        if not env_file_path:
            raise ValueError("Missing agent env file path in config.py")

        tags_value = tags.to_env_value()
        LOGGER.info("Updating FORNAX_UDF_TAGS, env_file_path=%s", env_file_path)
        LOGGER.info("FORNAX_UDF_TAGS=%s", tags_value)
        LOGGER.debug("FORNAX_UDF_TAGS length=%d", len(tags_value))
        await asyncio.to_thread(_upsert_env_var, env_file_path, "FORNAX_UDF_TAGS", tags_value)
        LOGGER.info("FORNAX_UDF_TAGS updated successfully")

        LOGGER.info("Restarting gateway")
        await self._restart_gateway()
        LOGGER.info("Gateway restarted successfully")

    @abstractmethod
    async def chat(
        self,
        msg: str,
        session_id: str,
        tags: FornaxUdfTags,
        response_schema: BaseModel | None = None,
        *,
        chat_role: str = "work_agent",
    ) -> str:
        pass


class HermesAgent(Agent):
    @property
    def env_file_path(self) -> str | None:
        return CONFIG.hermes_env_file

    def __init__(self) -> None:
        self.client = AsyncOpenAI(
            base_url="http://localhost:8642/v1",
            api_key=CONFIG.hermes_api_key,
        )
        self._gateway_proc: asyncio.subprocess.Process | None = None
        self._gateway_ready: bool = False

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
        LOGGER.info("Running command: hermes gateway restart")
        await self._run_cmd_checked(["hermes", "gateway", "restart"])
        # 要使用hermes API server， 必须要要运行hermes gateway (启动很慢)
        if self._gateway_proc is not None and self._gateway_proc.returncode is None:
            LOGGER.info("hermes gateway already running")
            if not self._gateway_ready:
                await self._wait_for_tcp_ready("localhost", 8642, timeout_s=60.0)
                self._gateway_ready = True
            return

        LOGGER.info("Starting command in background: hermes gateway")
        self._gateway_proc = await asyncio.create_subprocess_exec(
            "hermes",
            "gateway",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._gateway_ready = False
        await self._wait_for_tcp_ready("localhost", 8642, timeout_s=300.0)
        self._gateway_ready = True

    async def chat(
        self,
        msg: str,
        session_id: str,
        tags: FornaxUdfTags,
        response_schema: BaseModel | None = None, # hermes agent对这个参数无感
        *,
        chat_role: str = "work_agent",
    ) -> str:
        emit_pre_chat_state(session_id=session_id, tags=tags, chat_role=chat_role)
        await self._prepare_chat_env(tags)
        msg = f"{_format_message_timestamp()}\n{msg}"
        if response_schema is None:
            response = await self.client.responses.create(
                model="hermes-agent",
                input=msg,
                conversation=session_id,
            )
        else:
            response = await self.client.responses.create(
                model="hermes-agent",
                input=msg,
                conversation=session_id,
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
        skills_dir: str | None = None,
        material_dir: str | None = None,
        workspace_dir: str | Path | None = None,
    ) -> None:
        
        self.run_id = run_id
        self.task_id = task_id
        # Agent名字一定以evobench开头，方便后续删除Agent
        self.agent_name = "evobench-agent_name-" + short_id()
        self.workspace_dir = self._mk_workspace(workspace_dir)
        if skills_dir:
            self.copy_skill_dir(skills_dir)
        if material_dir:
            self.copy_material_dir(material_dir)
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

    def _create_agent(self) -> None:
        def chk_existance():
            result = subprocess.run(["openclaw", "agents", "list"], check=False, capture_output=True)
            if result.returncode == 1:
                LOGGER.info(f"Error when executing openclaw agents list: {result.stderr.decode()}")
                raise ValueError("Failed to list agents")
            result = result.stdout.decode(encoding="utf-8")
            return result.find(self.agent_name) != -1
        if chk_existance():
            LOGGER.info(f"Agent {self.agent_name} already exists")
            return
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
        if not chk_existance():
            raise ValueError(f"Failed to create agent {self.agent_name}")
        LOGGER.info(f"Agent {self.agent_name} created successfully")
        
    
    @property
    def env_file_path(self) -> str | None:
        return CONFIG.openclaw_env_file
    
    def _mk_workspace(self, workspace_dir: str | Path | None = None) -> Path:
        """
        Create workspace directory at /tmp/<run_id>/<task_id>
        If task id starts with "baseline-", then it is a baseline run.
        If it starts with "evolved-", then it is an evolved run.
        If it already exists, do nothing.
        """
        if workspace_dir is None:
            workspace_path = Path("/tmp") / str(self.run_id) / str(self.task_id)
        else:
            workspace_path = Path(workspace_dir).expanduser().resolve()
        workspace_path.mkdir(parents=True, exist_ok=True)
        return workspace_path

    def _resolve_project_path(self, path_value: str | Path) -> Path:
        path = Path(path_value).expanduser()
        if not path.is_absolute():
            path = _PROJECT_ROOT / path
        return path.resolve()

    def copy_skill_dir(self, skill_path: str | Path) -> Path:
        source_dir = self._resolve_project_path(skill_path)
        if not source_dir.exists():
            raise ValueError(f"Skill path does not exist: {source_dir}")
        if not source_dir.is_dir():
            raise ValueError(f"Skill path is not a directory: {source_dir}")
        if source_dir.name != "skills":
            raise ValueError(f"skills directory must be named 'skills', but got {source_dir.name}")
        destination_dir = self.workspace_dir / source_dir.name
        shutil.copytree(source_dir, destination_dir, dirs_exist_ok=True)
        LOGGER.info("Copied skill directory to workspace: %s -> %s", source_dir, destination_dir)
        return destination_dir

    def copy_material_dir(self, material_path: str | Path) -> Path:
        source_dir = self._resolve_project_path(material_path)
        if not source_dir.exists():
            raise ValueError(f"Material path does not exist: {source_dir}")
        if not source_dir.is_dir():
            raise ValueError(f"Material path is not a directory: {source_dir}")
        destination_dir = self.workspace_dir / source_dir.name
        shutil.copytree(source_dir, destination_dir, dirs_exist_ok=True)
        LOGGER.info("Copied material directory to workspace: %s -> %s", source_dir, destination_dir)
        return destination_dir

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
        trace_plugin: str = "langfuse",
        restart_gateway: bool = True,
        rebuild_runtime: bool = True,
    ) -> None:
        if rebuild_runtime:
            OpenClawAgent.rebuild_evolution_runtime()

        if ensure_config_fields:
            OpenClawAgent.add_fields_to_openclaw_config()

        trace_plugin_name: str | None
        if trace_plugin == "fornax":
            trace_plugin_name = "openclaw-fornax-trace"
        elif trace_plugin == "langfuse":
            trace_plugin_name = "langfuse-tracer"
        else:
            raise ValueError(f"Unsupported trace plugin: {trace_plugin}")

        if trace_plugin_name:
            LOGGER.info("Running command: openclaw plugins enable %s", trace_plugin_name)
            subprocess.run(["openclaw", "plugins", "enable", trace_plugin_name], check=True)

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
        tags: FornaxUdfTags,
        response_schema: BaseModel | None = None,
        *,
        chat_role: str = "work_agent",
    ) -> str:
        tags.agent_name = self.agent_name
        emit_pre_chat_state(session_id=session_id, tags=tags, chat_role=chat_role)
        await self._prepare_chat_env(tags)
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
    
