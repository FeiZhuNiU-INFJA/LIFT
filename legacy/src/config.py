import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

RESET = "\033[0m"
LEVEL_COLORS = {
    logging.DEBUG: "\033[36m",
    logging.INFO: "\033[32m",
    logging.WARNING: "\033[33m",
    logging.ERROR: "\033[31m",
    logging.CRITICAL: "\033[35m",
}

def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class AppConfig:
    hermes_api_key: str | None
    hermes_env_file: str | None
    hermes_dir: str | None
    hermes_model_name: str | None
    hermes_api_url: str | None
    hermes_max_tokens: int
    openclaw_env_file: str | None
    openclaw_model_name: str | None
    eval_max_turns: int
    log_file: str
    langfuse_pre_chat: bool
    langfuse_public_key: str | None
    langfuse_secret_key: str | None
    langfuse_base_url: str | None
    openai_api_key: str | None
    openai_base_url: str | None
    firecrawl_api_key: str | None
    api_server_enabled: bool
    api_server_key: str | None
    trajectory_judge_model: str
    do_trajectory_judge: bool

    @property
    def langfuse_credentials_present(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)


def load_config() -> AppConfig:
    return AppConfig(
        hermes_api_key=os.getenv("HERMES_API_KEY"),
        hermes_env_file=os.getenv("HERMES_ENV_FILE"),
        hermes_dir=os.getenv("HERMES_DIR"),
        hermes_model_name=os.getenv("HERMES_MODEL_NAME"),
        hermes_api_url=os.getenv("HERMES_API_URL"),
        hermes_max_tokens=int(os.getenv("HERMES_MAX_TOKENS", "102400")),
        openclaw_env_file=os.getenv("OPENCLAW_ENV_FILE"),
        openclaw_model_name=os.getenv("OPENCLAW_MODEL_NAME"),
        eval_max_turns=int(os.getenv("EVAL_MAX_TURNS", "2")),
        log_file=os.getenv("EVAL_LOG_FILE", str(_default_log_file())),
        langfuse_pre_chat=_env_flag("EVAL_LANGFUSE_PRE_CHAT", default=True),
        langfuse_public_key=os.getenv("LANGFUSE_PUBLIC_KEY"),
        langfuse_secret_key=os.getenv("LANGFUSE_SECRET_KEY"),
        langfuse_base_url=os.getenv("LANGFUSE_BASE_URL"),
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        openai_base_url=os.getenv("OPENAI_BASE_URL"),
        firecrawl_api_key=os.getenv("FIRECRAWL_API_KEY"),
        api_server_enabled=_env_flag("API_SERVER_ENABLED", default=False),
        api_server_key=os.getenv("API_SERVER_KEY"),
        trajectory_judge_model=os.getenv("TRAJECTORY_JUDGE_MODEL", "gpt-4o-mini"),
        do_trajectory_judge=_env_flag("DO_TRAJECTORY_JUDGE", default=False),
    )


class ColorFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        original_levelname = record.levelname
        color = LEVEL_COLORS.get(record.levelno, "")
        if color:
            record.levelname = f"{color}{original_levelname}{RESET}"
        try:
            return super().format(record)
        finally:
            record.levelname = original_levelname


_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _default_log_file() -> Path:
    return _PROJECT_ROOT / "evolve_eval.log"


def setup_logging() -> None:
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    log_path = Path(CONFIG.log_file).expanduser()
    if not log_path.is_absolute():
        log_path = (_PROJECT_ROOT / log_path).resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    plain_formatter = logging.Formatter(_LOG_FORMAT)
    color_formatter = ColorFormatter(_LOG_FORMAT)

    def _has_stream_handler() -> bool:
        return any(
            isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
            for h in root_logger.handlers
        )

    def _has_file_handler_for_path(path: Path) -> bool:
        for h in root_logger.handlers:
            if isinstance(h, logging.FileHandler):
                base = getattr(h, "baseFilename", None)
                if base and Path(base).resolve() == path.resolve():
                    return True
        return False

    if root_logger.handlers:
        for handler in root_logger.handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(
                handler, logging.FileHandler
            ):
                handler.setFormatter(color_formatter)
            elif isinstance(handler, logging.FileHandler):
                handler.setFormatter(plain_formatter)
        if not _has_file_handler_for_path(log_path):
            file_handler = logging.FileHandler(log_path, encoding="utf-8")
            file_handler.setFormatter(plain_formatter)
            root_logger.addHandler(file_handler)
        return

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(color_formatter)
    root_logger.addHandler(stream_handler)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(plain_formatter)
    root_logger.addHandler(file_handler)


CONFIG = load_config()
setup_logging()
LOGGER = logging.getLogger(__name__)
