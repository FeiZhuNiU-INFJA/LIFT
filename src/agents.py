from __future__ import annotations
import asyncio
from datetime import datetime, timedelta, timezone
import json
import shutil
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from openai import AsyncOpenAI
import os
import uuid
from pydantic import BaseModel
from src.config import CONFIG, LOGGER
from src.benchmark_schema import BenchmarkTask

GMT_PLUS_8 = timezone(timedelta(hours=8), name="GMT+8")
WEEKDAY_ABBR = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _bool_to_tag(value: bool | None) -> str:
    if value is None:
        return ""
    return "1" if value else "0"


def _sanitize_tag_value(value: str) -> str:
    # FORNAX_UDF_TAGS uses comma-separated key=value pairs.
    return value.replace(",", " ").replace("\n", " ").strip()


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
    LOGGER.info("Raw output: %s", raw)
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


@dataclass
class FornaxUdfTags:
    run: str
    task: str
    task_query: str
    is_final_task: bool
    is_evolve_turn: bool
    is_ended: bool
    content_reqs: str
    trajectory_reqs: str
    content_score: float

    @classmethod
    def init_tags(cls, task: BenchmarkTask, run_id: str) -> FornaxUdfTags:
        return cls(
            run=run_id,
            task=f"{task.category_name}_{task.name}",
            task_query=task.query,
            is_final_task=False,
            is_evolve_turn=False,
            is_ended=False,
            content_reqs=task.expected_result.content_reqs,
            trajectory_reqs=task.expected_result.trajectory_reqs,
            content_score=0.0,
        )

    def to_env_value(self) -> str:
        fields_in_order = [
            ("run", self.run),
            ("task", self.task),
            ("task_query", self.task_query),
            ("is_final_task", _bool_to_tag(self.is_final_task)),
            ("is_evolve_turn", _bool_to_tag(self.is_evolve_turn)),
            ("is_ended", _bool_to_tag(self.is_ended)),
            ("content_reqs", self.content_reqs),
            ("trajectory_reqs", self.trajectory_reqs),
            ("content_score", self.content_score),
        ]
        return ",".join(
            f"{key}={_sanitize_tag_value(str(value))}" for key, value in fields_in_order
        )

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
    
    @abstractmethod
    async def evolve(self, session_id: str) -> None:
        """启动agent进化"""
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

    async def evolve(self, session_id: str) -> None:
        """Hermes无需主动触发进化"""
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
    ) -> str:
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
    def __init__(self, run_id: str, task_id: str, skills_dir: str | None = None) -> None:
        
        self.run_id = run_id
        self.task_id = task_id
        # Agent名字一定以evobench开头，方便后续删除Agent
        self.agent_name = "evobench-" + str(uuid.uuid4())[:16] + '-' + CONFIG.model
        self.workspace_dir = self._mk_workspace()
        if skills_dir:
                self.copy_skill_dir(skills_dir)
        # Start Fornax trace plugins
        LOGGER.info("Running command: openclaw plugins enable openclaw-fornax-trace")
        subprocess.run(["openclaw", "plugins", "enable", "openclaw-fornax-trace"], check=True)
        LOGGER.info("Running command: openclaw gateway start")
        subprocess.run(["openclaw", "gateway", "start"], check=True)
        
        self._create_agent()
        
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
        subprocess.run(["openclaw", "agents", "add", self.agent_name, "--model", CONFIG.model, "--workspace", self.workspace_dir], check=True)
        if not chk_existance():
            raise ValueError(f"Failed to create agent {self.agent_name}")
        LOGGER.info(f"Agent {self.agent_name} created successfully")
        
    
    @property
    def env_file_path(self) -> str | None:
        return CONFIG.openclaw_env_file
    
    def _mk_workspace(self) -> Path:
        """
        Create workspace directory at /tmp/<run_id>/<task_id>
        If task id starts with "baseline-", then it is a baseline run.
        If it starts with "evolved-", then it is an evolved run.
        If it already exists, do nothing.
        """
        workspace_dir = Path("/tmp") / str(self.run_id) / str(self.task_id)
        workspace_dir.mkdir(parents=True, exist_ok=True)
        return workspace_dir

    def copy_skill_dir(self, skill_path: str | Path) -> Path:
        source_dir = Path(skill_path).expanduser().resolve()
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

    async def _run_cmd_checked_capture(self, args: list[str]) -> str:
        LOGGER.info("Running command: %s", " ".join(args))
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
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

    async def evolve(self, session_id: str) -> None:
        # evolve command
        # openclaw gateway call chat.send --params '{"sessionKey": "{session_id}", "message": "/learn review --approve", "idempotencyKey": "review-{session_id}"}'
        # loop until message is not empty list
        # openclaw gateway call chat.history --params '{"sessionKey": "session_id"}'
        send_params = {
            "sessionKey": session_id,
            "message": "/learn review --approve",
            "idempotencyKey": f"review-{session_id}",
        }
        send_cmd = ["openclaw", "gateway", "call", "chat.send", "--params", json.dumps(send_params), "--json"]
        await self._run_cmd_checked_capture(send_cmd)

        start_ts = time.monotonic()
        warned = False

        history_cmd = [
            "openclaw",
            "gateway",
            "call",
            "chat.history",
            "--params",
            json.dumps({"sessionKey": session_id}),
            "--json",
        ]
        while True:
            LOGGER.debug("Polling command: %s", " ".join(history_cmd))
            stdout = await self._run_cmd_checked_capture(history_cmd)
            payload = _parse_json_loose(stdout)
            messages = payload.get("messages")
            if isinstance(messages, list) and len(messages) > 0:
                LOGGER.info("Evolve review finished: chat.history messages=%d", len(messages))
                return

            elapsed = time.monotonic() - start_ts
            if elapsed >= 300 and not warned:
                warned = True
                LOGGER.warning(
                    "Evolve review still running after %.1fs (>=300s). Continue polling without raising.",
                    elapsed,
                )

            await asyncio.sleep(5.0)

    async def chat(
        self,
        msg: str,
        session_id: str,
        tags: FornaxUdfTags,
        response_schema: BaseModel | None = None,
    ) -> str:
        await self._prepare_chat_env(tags)
        _ = tags
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
    
