"""Run Prime Agent global ``/refine`` as LIFT's explicit per-task evolve hook.

自进化落点：**每道 warmup 题完成后**、``docker commit`` 之前，在仍存活的 warmup
容器里对**刚结束那题的会话轨迹**触发一次 **global** ``/refine``，把证据支撑的增量
写回 global harness（``PRIME_AGENT_STATE_DIR/harness/harness_state.json``），从而
被 commit 带入 delta、holdout 新 session 可读。逐题触发（挂在 ``evolve_after_task``）
而非 suite 级一次性触发，是因为 warmup ``serial_single`` 下每题是独立 conversation
session，一次 ``-c`` 只能续接最近一题 → 只逐题触发才能把每题证据都提升到 global。

为什么必须 global：Prime Agent 默认 refine 写 local(session) harness，绑定
当次 conversation session id；LIFT holdout 每题是全新容器 + 全新 session，读不到
local harness → 进化增益归零。``/refine --global`` 显式提升到 session 无关的持久
层：upstream ``parseRefineCommandOptions`` 解析 ``--global`` 置位；global harness
落在 ``getGlobalHarnessStateDir()`` == ``~/.prime/agent/harness/``（被
``PRIME_AGENT_CODING_AGENT_DIR`` 钉死到 ``PRIME_AGENT_STATE_DIR``），docker commit
整体带走。

触发形态：``prime-agent -c -p "/refine --global [instructions]"``。

  - **为什么必须 ``-c``**：upstream ``/refine`` 是“对**当前会话轨迹**做一次定向复盘”
    （``rlm-runtime.md``: *runs a dedicated review over the current trajectory*）。不带
    ``-c`` 直接 ``prime-agent -p "/refine ..."`` 会新开一个**空会话**，refine 复盘空
    轨迹 → 产不出 harness 编辑，active evolve 退化成 no-op。``-c``(``--continue``) 走
    upstream ``SessionManager.continueRecent()`` 续接**最近一次会话**；本 hook 挂在
    ``evolve_after_task``，每题一结束即触发，此刻“最近会话”恰是**刚结束的那道 warmup
    题**，refine 从而拿到该题真实轨迹做证据。``-c`` 不受
    ``shouldRejectNonInteractiveBareResume`` 限制（那只拦 ``resume===true`` 的 bare
    ``--resume``）。
  - ``/refine`` 是 session slash command（upstream ``SESSION_SLASH_COMMAND_NAMES``
    含 ``refine``），print 模式下作为 slash 输入被 ``_normalizeSubmission`` 解析成
    session_command 执行；``parseRefineCommandOptions`` 用正则 ``/^--global(?=\\s|$)/``
    解析行首 ``--global`` 提升作用域。
  - **关键**：print / json / rpc 模式 upstream 置 ``serializedRefine=true``
    （``appMode !== "interactive" && appMode !== "daemon"``），refine 在回合边界
    **同步**跑完 LLM pass 才退出——正是同步 evolve 钩子需要的语义（不会在
    ``docker commit`` 前提早返回、丢掉尚未落盘的 harness 编辑）。

用 ``-p``（print 纯文本）而非 ``--mode json``：evolve 只需要“跑完 + 退出码/输出”
做成败判定，不需要结构化回复；print 模式最少依赖。失败（非零退出 / 超时）由
``exec_prime_agent_async`` 抛出，交给 base 的 ``evolve_after_task`` 重试预算处理。
"""

from __future__ import annotations

import shlex

from src.config import LOGGER
from src.lift.adapters.prime_agent.container_exec import (
    PrimeAgentContainerContext,
    exec_prime_agent_async,
)

# /refine 会跑一次 LLM pass，远超普通响应超时；给足 wall-clock 预算。
REFINE_EVOLVE_TIMEOUT_SECONDS = 1800.0


def _global_refine_shell(instructions: str | None) -> str:
    """构造在容器内触发 global ``/refine`` 的命令（continue + slash-through-print）。

    ``prime-agent -c -p -- "/refine --global [instructions]"``：``-c``(``--continue``)
    先续接**最近一次会话**（warmup ``serial_single`` 下 == 最后一道 warmup 题），让
    refine 复盘真实 warmup 轨迹而非空会话；``-p`` print 模式 serialized-refine 生效，
    slash ``/refine`` 被解析（``parseRefineCommandOptions`` 用 ``/^--global(?=\\s|$)/``
    提升作用域）。用 ``bash -c`` 保留 Dockerfile ENV PATH（含 ``prime-agent``）。用
    ``--`` 终止选项解析，避免 slash 文本里的 ``--global`` 被 CLI 顶层 argv parser
    抢先吞掉。
    """
    slash = "/refine --global"
    if instructions:
        slash = f"{slash} {instructions}"
    return (
        "cd /workspace/task && "
        f"prime-agent -c -p -- {shlex.quote(slash)}"
    )


async def run_global_refine_evolve(
    *,
    container: PrimeAgentContainerContext,
    session_id: str,
    instructions: str | None = None,
) -> str:
    """在 warmup 容器触发 global ``/refine`` 并等待完成，返回 stdout。

    失败（非零退出 / 超时）由 ``exec_prime_agent_async`` 抛出，交给 base 的
    ``evolve_after_task`` 重试预算（``_EVOLVE_HOOK_ATTEMPTS`` = 3）处理。
    """
    LOGGER.info(
        "[prime_agent_active_evolve] global /refine start container=%s session=%s",
        container.container_name, session_id,
    )
    stdout = await exec_prime_agent_async(
        container,
        ["bash", "-c", _global_refine_shell(instructions)],
        env={"LIFT_PRIME_AGENT_SESSION_ID": session_id},
        label="prime_agent global refine evolve",
        timeout_seconds=REFINE_EVOLVE_TIMEOUT_SECONDS,
    )
    LOGGER.info(
        "[prime_agent_active_evolve] global /refine done container=%s stdout_tail=%r",
        container.container_name, stdout[-500:],
    )
    return stdout


__all__ = [
    "REFINE_EVOLVE_TIMEOUT_SECONDS",
    "run_global_refine_evolve",
    "_global_refine_shell",
]
