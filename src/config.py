import os
import logging
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

@dataclass(frozen=True)
class AppConfig:
    hermes_api_key: str | None
    hermes_env_file: str | None
    openclaw_env_file: str | None


def load_config() -> AppConfig:
    return AppConfig(
        hermes_api_key=os.getenv("HERMES_API_KEY"),
        hermes_env_file=os.getenv("HERMES_ENV_FILE"),
        openclaw_env_file=os.getenv("OPENCLAW_ENV_FILE"),
    )


CONFIG = load_config()
LOGGER = logging.getLogger(__name__)
