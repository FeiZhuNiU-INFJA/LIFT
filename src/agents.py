from __future__ import annotations
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from openai import OpenAI
from pydantic import BaseModel

from src.config import CONFIG, LOGGER
from src.benchmark_schema import BenchmarkTask


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
        pass

    @abstractmethod
    def _restart_gateway(self) -> None:
        pass

    def _prepare_chat_env(
        self,
        tags: FornaxUdfTags,
    ) -> None:
        env_file_path = self.env_file_path
        if not env_file_path:
            raise ValueError("Missing agent env file path in config.py")

        tags_value = tags.to_env_value()
        LOGGER.info("Updating FORNAX_UDF_TAGS, env_file_path=%s", env_file_path)
        LOGGER.debug("FORNAX_UDF_TAGS length=%d", len(tags_value))
        _upsert_env_var(env_file_path, "FORNAX_UDF_TAGS", tags_value)
        LOGGER.info("FORNAX_UDF_TAGS updated successfully")

        LOGGER.info("Restarting gateway")
        self._restart_gateway()
        LOGGER.info("Gateway restarted successfully")

    @abstractmethod
    def chat(
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
        self.client = OpenAI(
            base_url="http://localhost:8642/v1",
            api_key=CONFIG.hermes_api_key,
        )

    def _restart_gateway(self) -> None:
        LOGGER.info("Running command: hermes gateway restart")
        subprocess.run(["hermes", "gateway", "restart"], check=True)
        LOGGER.info("Command succeeded: hermes gateway restart")
        # 要使用hermes API server， 必须要要运行hermes gateway (启动很慢)
        LOGGER.info("Running command: hermes gateway")
        subprocess.run(["hermes", "gateway"], check=True)
        LOGGER.info("Command succeeded: hermes gateway")

    def chat(
        self,
        msg: str,
        session_id: str,
        tags: FornaxUdfTags,
        response_schema: BaseModel | None = None, # hermes agent对这个参数无感
    ) -> str:
        self._prepare_chat_env(tags)
        if response_schema is None:
            response = self.client.responses.create(
                model="hermes-agent",
                input=msg,
                conversation=session_id,
            )
        else:
            response = self.client.responses.create(
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
    @property
    def env_file_path(self) -> str | None:
        return CONFIG.openclaw_env_file

    def _restart_gateway(self) -> None:
        raise NotImplementedError(
            "OpenClawAgent gateway restart is not implemented yet."
        )

    def chat(
        self,
        msg: str,
        session_id: str,
        tags: FornaxUdfTags,
        response_schema: BaseModel | None = None,
    ) -> str:
        raise NotImplementedError("OpenClawAgent.chat is not implemented yet.")
