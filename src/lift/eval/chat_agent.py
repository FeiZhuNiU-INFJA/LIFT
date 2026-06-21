"""评测内核与 agent runtime 之间的 chat 抽象（与具体 agent 产品无关）。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone

from src.models import SuiteTask

_GMT_PLUS_8 = timezone(timedelta(hours=8), name="GMT+8")
_WEEKDAY_ABBR = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def format_outbound_message(message: str) -> str:
    """为发往 agent 的消息添加时间戳前缀（OpenClaw 等 runtime 的通用约定）。"""
    now = datetime.now(_GMT_PLUS_8)
    weekday = _WEEKDAY_ABBR[now.weekday()]
    stamp = f"[{weekday} {now.strftime('%Y-%m-%d %H:%M:%S')} GMT+8]"
    return f"{stamp}\n{message}"


class ChatAgent(ABC):
    """单轮 message 往返：框架负责 tags / Langfuse，子类只负责 transport。

    子类**必须**实现 ``agent_name`` 与 ``chat``；``initialize`` / ``activate_session`` /
    ``augment_*`` 有默认实现（OpenClaw 等无状态 runtime 可直接继承不重写）。
    """

    @property
    @abstractmethod
    def agent_name(self) -> str:
        """runtime 内注册的 agent 名（写入 Langfuse tags）。"""

    async def initialize(self) -> None:
        """单题构建时的一次性 setup（如容器内注册 agent）；默认 no-op。

        声明为 async 是为了让 transport 内的同步阻塞 IO（``docker exec`` 等）能用
        ``asyncio`` 原生路径，不会卡住事件循环导致并发协程被串行化。
        """

    async def activate_session(self, session_id: str) -> None:
        """chat 前切换 session；有状态 runtime（如 Hermes）覆写，无状态路径默认 no-op。"""
        _ = session_id  # OpenClaw 在 chat() 的 --session-id 传入，此处 intentionally 空实现

    def augment_work_prompt(self, task: SuiteTask, prompt: str) -> str:
        """可选：为 work agent 追加 prompt 后缀。"""
        _ = task
        return prompt

    def augment_judge_user_prompt(self, task: SuiteTask, prompt: str) -> str:
        """可选：为 judge 侧用户 prompt 追加内容。"""
        _ = task
        return prompt

    @abstractmethod
    async def chat(self, message: str, *, session_id: str) -> str:
        """发送一条消息并返回 assistant 文本（不含 Langfuse / judge schema 逻辑）。"""
