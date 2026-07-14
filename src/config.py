"""应用配置与日志初始化：从环境变量加载 ``AppConfig``，并配置彩色控制台 + 文件日志。"""

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from src.paths import PROJECT_ROOT

load_dotenv()

LOGGER = logging.getLogger(__name__)
"""本模块 logger（``src.config``）。"""

RESET = "\033[0m"
"""ANSI 重置色码。"""

LEVEL_COLORS = {
    logging.DEBUG: "\033[36m",
    logging.INFO: "\033[32m",
    logging.WARNING: "\033[33m",
    logging.ERROR: "\033[31m",
    logging.CRITICAL: "\033[35m",
}
"""日志级别到 ANSI 前景色的映射。"""


def _env_flag(name: str, default: bool = False) -> bool:
    """解析环境变量为布尔值（``1``/``true``/``yes``/``y``/``on`` 为 True）。"""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _read_model_name() -> str:
    """读取共享 ``MODEL_NAME``，并尽早提示格式契约。"""
    model = os.getenv("MODEL_NAME", "unknown")
    if model not in {"", "unknown"} and not (
        model.startswith("custom/") and len(model) > len("custom/")
    ):
        LOGGER.warning(
            "MODEL_NAME must be 'custom/model_id' (e.g. custom/ep-xxxx); got %r.",
            model,
        )
    return model


@dataclass(frozen=True)
class AppConfig:
    """从环境变量加载的不可变应用配置。"""

    container_network_mode: str | None
    """所有容器 runtime 的 ``docker run --network`` 模式（``CONTAINER_NETWORK_MODE``）。

    默认 None（不覆盖，用 Docker 默认 bridge）。在纯 IPv6/NAT64 等 bridge 容器无法
    出网的宿主机上，设为 ``host`` 让容器复用宿主机网络栈（继承其 DNS/路由）。
    OpenClaw 依赖 Docker 端口发布回填 gateway / FastAPI 端口，host 模式会让 Docker
    忽略 ``-p`` 并导致 ``docker inspect`` 没有 HostPort；只在明确确认风险后使用。
    """
    model: str
    """agent 使用的模型名（``MODEL_NAME``，形如 ``custom/model_id``，provider 前缀恒为 ``custom``）。"""
    max_tokens: int
    """单轮 work/judge chat 的最大输出 token（``MAX_TOKENS``，默认 51200）。"""
    reasoning_effort: str
    """全 runtime 统一的 seed 模型思维链强度（``REASONING_EFFORT``，默认 ``medium``）。

    - OpenClaw：作为 ``agent --thinking <level>`` 参数传入，同时 ``models.fragment.json``
      的 ``reasoning: true`` 让 gateway 视模型为可推理。
    - Hermes：由 ``hermes_runner.py`` 转成 ``reasoning_config={"enabled": True, "effort": <level>}``
      注入 AIAgent；ARK ``volces.com`` 已在 ``install-in-image.sh`` 中加入白名单。
    - GenericAgent：镜像构建期 sed 到 ``mykey.py`` 的 ``native_oai_config`` 上，
      再由 ``llmcore.py`` 作为顶层 ``reasoning_effort`` 透传到 OpenAI 兼容请求体。
    - OpenHuman：Rust 二进制不支持该字段，服务端默认已 thinking，此处配置无效果。

    值为空字符串时各 runtime 走各自的默认（Hermes 走 upstream 默认，OpenClaw 不追加
    ``--thinking``，GenericAgent 不写入 ``reasoning_effort`` key）。
    """
    log_file: str
    """日志文件路径（``EVAL_LOG_FILE``，默认项目根下 ``evolve_eval.log``）。"""
    langfuse_pre_chat: bool
    """是否在 chat 前上报 Langfuse pre-chat span（``EVAL_LANGFUSE_PRE_CHAT``）。"""
    langfuse_public_key: str | None
    """Langfuse public key（``LANGFUSE_PUBLIC_KEY``）。"""
    langfuse_secret_key: str | None
    """Langfuse secret key（``LANGFUSE_SECRET_KEY``）。"""
    langfuse_base_url: str | None
    """Langfuse API base URL（``LANGFUSE_BASE_URL``）。"""
    work_openai_api_key: str | None
    """Work agent 的 OpenAI 兼容 API 密钥（``WORK_OPENAI_API_KEY``）。"""
    work_openai_base_url: str | None
    """Work agent 的 OpenAI 兼容 base URL（``WORK_OPENAI_BASE_URL``）。"""
    trajectory_judge_openai_api_key: str | None
    """轨迹评判 LLM 的 API 密钥（``TRAJECTORY_JUDGE_OPENAI_API_KEY``）。"""
    trajectory_judge_openai_base_url: str | None
    """轨迹评判 LLM 的 base URL（``TRAJECTORY_JUDGE_OPENAI_BASE_URL``）。"""
    firecrawl_api_key: str | None
    """Firecrawl API 密钥（``FIRECRAWL_API_KEY``）。"""
    api_server_enabled: bool
    """Hermes 是否启用 API server（``API_SERVER_ENABLED``）。"""
    api_server_key: str | None
    """Hermes API server 密钥（``API_SERVER_KEY``）。"""
    trajectory_judge_model: str
    """轨迹评判使用的模型名（``TRAJECTORY_JUDGE_MODEL``，默认 gpt-4o-mini）。"""
    do_trajectory_judge: bool
    """是否启用轨迹评判（``DO_TRAJECTORY_JUDGE``）。"""

    @property
    def langfuse_credentials_present(self) -> bool:
        """Langfuse public/secret key 是否均已配置。"""
        return bool(self.langfuse_public_key and self.langfuse_secret_key)


def load_config() -> AppConfig:
    """从当前环境变量加载 ``AppConfig``。"""
    return AppConfig(
        container_network_mode=(os.getenv("CONTAINER_NETWORK_MODE") or "").strip()
        or None,
        model=_read_model_name(),
        max_tokens=int(os.getenv("MAX_TOKENS", "51200")),
        reasoning_effort=(os.getenv("REASONING_EFFORT", "medium") or "").strip(),
        log_file=os.getenv("EVAL_LOG_FILE", str(_default_log_file())),
        langfuse_pre_chat=_env_flag("EVAL_LANGFUSE_PRE_CHAT", default=True),
        langfuse_public_key=os.getenv("LANGFUSE_PUBLIC_KEY"),
        langfuse_secret_key=os.getenv("LANGFUSE_SECRET_KEY"),
        langfuse_base_url=os.getenv("LANGFUSE_BASE_URL"),
        work_openai_api_key=os.getenv("WORK_OPENAI_API_KEY"),
        work_openai_base_url=os.getenv("WORK_OPENAI_BASE_URL"),
        trajectory_judge_openai_api_key=os.getenv("TRAJECTORY_JUDGE_OPENAI_API_KEY"),
        trajectory_judge_openai_base_url=os.getenv("TRAJECTORY_JUDGE_OPENAI_BASE_URL"),
        firecrawl_api_key=os.getenv("FIRECRAWL_API_KEY"),
        api_server_enabled=_env_flag("API_SERVER_ENABLED", default=False),
        api_server_key=os.getenv("API_SERVER_KEY"),
        trajectory_judge_model=os.getenv("TRAJECTORY_JUDGE_MODEL", "gpt-4o-mini"),
        do_trajectory_judge=_env_flag("DO_TRAJECTORY_JUDGE", default=False),
    )


class ColorFormatter(logging.Formatter):
    """为控制台日志的 levelname 添加 ANSI 颜色。"""

    def format(self, record: logging.LogRecord) -> str:
        """格式化日志记录，仅对 levelname 着色，不影响文件 handler。"""
        original_levelname = record.levelname
        color = LEVEL_COLORS.get(record.levelno, "")
        if color:
            record.levelname = f"{color}{original_levelname}{RESET}"
        try:
            return super().format(record)
        finally:
            record.levelname = original_levelname


_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
"""根 logger 使用的日志格式字符串。"""

_PROJECT_ROOT = PROJECT_ROOT


def _default_log_file() -> Path:
    """默认日志文件路径（项目根下 ``evolve_eval.log``）。"""
    return PROJECT_ROOT / "evolve_eval.log"


def setup_logging() -> None:
    """配置根 logger：默认 INFO，可由 ``EVAL_LOG_LEVEL`` 覆盖（DEBUG/WARNING/ERROR 等）；
    彩色 stdout、plain 文件 handler（幂等追加）。"""
    root_logger = logging.getLogger()
    level_name = os.getenv("EVAL_LOG_LEVEL", "INFO").upper()
    level = logging.getLevelName(level_name)
    if not isinstance(level, int):
        level = logging.INFO
    root_logger.setLevel(level)

    log_path = Path(os.getenv("EVAL_LOG_FILE", str(_default_log_file()))).expanduser()
    if not log_path.is_absolute():
        log_path = (PROJECT_ROOT / log_path).resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    plain_formatter = logging.Formatter(_LOG_FORMAT)
    color_formatter = ColorFormatter(_LOG_FORMAT)

    def _has_stream_handler() -> bool:
        """根 logger 是否已有 stdout/stderr StreamHandler。"""
        return any(
            isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
            for h in root_logger.handlers
        )

    def _has_file_handler_for_path(path: Path) -> bool:
        """根 logger 是否已绑定指向 ``path`` 的 FileHandler。"""
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
            file_handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
            file_handler.setFormatter(plain_formatter)
            root_logger.addHandler(file_handler)
        return

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(color_formatter)
    root_logger.addHandler(stream_handler)

    file_handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    file_handler.setFormatter(plain_formatter)
    root_logger.addHandler(file_handler)


setup_logging()

CONFIG = load_config()
"""模块加载时初始化的全局配置实例。"""
