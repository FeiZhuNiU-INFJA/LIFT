import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

RESET = "\033[0m"
LEVEL_COLORS = {
    logging.DEBUG: "\033[36m",
    logging.INFO: "\033[32m",
    logging.WARNING: "\033[33m",
    logging.ERROR: "\033[31m",
    logging.CRITICAL: "\033[35m",
}

@dataclass(frozen=True)
class AppConfig:
    hermes_api_key: str | None
    hermes_env_file: str | None
    openclaw_env_file: str | None
    eval_max_turns: int
    model: str


def load_config() -> AppConfig:
    return AppConfig(
        hermes_api_key=os.getenv("HERMES_API_KEY"),
        hermes_env_file=os.getenv("HERMES_ENV_FILE"),
        openclaw_env_file=os.getenv("OPENCLAW_ENV_FILE"),
        eval_max_turns=int(os.getenv("EVAL_MAX_TURNS", "2")),
        model=os.getenv("MODEL_NAME", 'unknown'),
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

    log_path = Path(
        os.getenv("EVAL_LOG_FILE", str(_default_log_file())),
    ).expanduser()
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
