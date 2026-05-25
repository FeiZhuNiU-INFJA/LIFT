import os

from dotenv import load_dotenv

load_dotenv()


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL")
JUDGE_MODEL_NAME = os.getenv("JUDGE_MODEL_NAME", "gpt-4o-mini")
USE_JUDGE = env_flag("USE_JUDGE", default=False)
