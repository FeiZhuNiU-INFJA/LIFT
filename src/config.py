import os
import logging
from dataclasses import dataclass

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


def setup_logging() -> None:
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    if root_logger.handlers:
        for handler in root_logger.handlers:
            handler.setFormatter(ColorFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        return

    handler = logging.StreamHandler()
    handler.setFormatter(ColorFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root_logger.addHandler(handler)


CONFIG = load_config()
setup_logging()
LOGGER = logging.getLogger(__name__)
