#!/usr/bin/env python3
# LIFT Hermes container runner.
#
# Copied from legacy/hermes-helper/hermes_runner.py into the self-contained
# Hermes build context (see .trae/documents/hermes_runtime_integration_plan.md
# §A). No host-path coupling: all paths (--hermes-agent-dir, --profile-home,
# --workspace, --model, --base-url, --api-key, --session-id, --max-tokens) come
# via args. Inside the LIFT image it is launched by docker exec with the
# discovered Hermes venv python; HERMES_HOME defaults to /opt/data.
from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path


END_SENTINEL = "__evo_task_end__"
MSG_END_SENTINEL = "__evo_msg_end__"
RESP_END_SENTINEL = "__evo_resp_end__"
NONE_RESPONSE_FALLBACK = (
    "Agent did not return a final response. "
    "It may have encountered an internal error, output truncation, "
    "or an incomplete tool-calling state."
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run Hermes AIAgent as a long-lived multi-turn process. "
            "Each user message is read line-by-line from stdin until a line "
            "equal to '__evo_msg_end__' is encountered. Send '__evo_task_end__' "
            "as a single line to trigger optional review and exit."
        )
    )
    parser.add_argument("--hermes-agent-dir", required=True, help="Path to the hermes-agent repo.")
    parser.add_argument("--model", required=True, help="Model passed to AIAgent.")
    parser.add_argument("--base-url", required=True, help="Base URL passed to AIAgent.")
    parser.add_argument("--api-key", required=True, help="API key passed to AIAgent.")
    parser.add_argument("--session-id", required=True, help="Session ID passed to AIAgent.")
    parser.add_argument("--max-tokens", type=int, required=True, help="max_tokens passed to AIAgent.")
    parser.add_argument(
        "--profile-home",
        default="",
        help="Optional explicit HERMES_HOME/profile path. If omitted, current environment is used.",
    )
    parser.add_argument(
        "--workspace",
        default="",
        help="Optional workspace path. If set, chdir into it before creating AIAgent.",
    )
    parser.add_argument(
        "--no-review-memory",
        action="store_true",
        help="Disable memory review. Default: enabled.",
    )
    parser.add_argument(
        "--no-review-skills",
        action="store_true",
        help="Disable skill review. Default: enabled.",
    )
    parser.add_argument(
        "--enable-review",
        action="store_true",
        help="Enable skill or memory review on session end. Default: disabled.",
    )
    return parser.parse_args()


def emit_response(content: str) -> None:
    sys.stdout.write(content)
    if not content.endswith("\n"):
        sys.stdout.write("\n")
    # 响应结束哨兵：父进程读到此行即知本轮回复完整，仅作检测，不进入实际消息内容。
    sys.stdout.write(RESP_END_SENTINEL + "\n")
    sys.stdout.flush()


# read_user_message 的返回标记
_READ_EOF = object()
_READ_TASK_END = object()


def read_user_message():
    """阻塞读取一条完整的 user message。

    协议：调用方按行写入 stdin，写完 ``__evo_msg_end__`` 一行表示该消息结束；
    任意时刻写入 ``__evo_task_end__`` 表示整个会话结束。

    返回值：
    - ``str``：完整的 user message（多行用 ``\\n`` 拼接）。
    - ``_READ_TASK_END``：收到任务终止哨兵。
    - ``_READ_EOF``：stdin 被关闭。
    """
    lines: list[str] = []
    while True:
        raw = sys.stdin.readline()
        if raw == "":
            return _READ_EOF
        line = raw.rstrip("\r\n")
        stripped = line.strip()
        if stripped == END_SENTINEL:
            return _READ_TASK_END
        if stripped == MSG_END_SENTINEL:
            return "\n".join(lines)
        lines.append(line)


def main() -> int:
    args = parse_args()

    if args.profile_home.strip():
        os.environ["HERMES_HOME"] = str(Path(args.profile_home).expanduser().resolve())

    if args.workspace.strip():
        workspace = Path(args.workspace).expanduser().resolve()
        os.chdir(workspace)
        os.environ["TERMINAL_CWD"] = str(workspace)

    hermes_agent_dir = Path(args.hermes_agent_dir).expanduser().resolve()
    if str(hermes_agent_dir) not in sys.path:
        sys.path.insert(0, str(hermes_agent_dir))

    from run_agent import AIAgent
    from agent.background_review import spawn_background_review_thread

    agent = AIAgent(
        model=args.model,
        base_url=args.base_url.strip(),
        provider='custom',
        api_key=args.api_key,
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=False,
        max_iterations=192,
        session_id=args.session_id,
        max_tokens=args.max_tokens,
        disabled_toolsets=["delegation"],
    )

    # Disable the built-in cadence-triggered review; this script triggers it manually on session end.
    agent._memory_nudge_interval = 0
    agent._skill_nudge_interval = 0

    # Suppress agent's own prints so they do not pollute the stdout protocol.
    agent._print_fn = lambda *a, **k: None
    agent.background_review_callback = None

    # full_history：跨多轮 conversation 累计的完整 message 历史。
    # - 每轮 chat 前作为 ``conversation_history`` 注入，保证多轮记忆。
    # - 每轮 chat 后以 ``result["messages"]``（run_conversation 返回的更新后 transcript）覆盖，
    #   它已经包含本轮 user / assistant / tool 全部消息。
    # - 任务结束触发 review 时，把整个 full_history 一并交给 review_launcher。
    full_history: list = []

    def run_review_if_enabled() -> None:
        if not args.enable_review:
            return
        review_memory = not args.no_review_memory
        review_skills = not args.no_review_skills
        if not (review_memory or review_skills):
            return
        review_launcher = AIAgent(
            model=args.model,
            base_url=args.base_url.strip(),
            api_key=args.api_key,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=False,
            max_iterations=16,
            session_id=f"review-{args.session_id}",
            max_tokens=args.max_tokens,
            disabled_toolsets=["delegation"],
        )
        review_launcher._memory_nudge_interval = 0
        review_launcher._skill_nudge_interval = 0
        review_launcher._print_fn = lambda *a, **k: None
        review_launcher.background_review_callback = None
        try:
            target, _prompt = spawn_background_review_thread(
                review_launcher,
                full_history,
                review_memory=review_memory,
                review_skills=review_skills,
            )
            thread = threading.Thread(
                target=target,
                daemon=False,
                name="manual-bg-review",
            )
            thread.start()
            thread.join()
        finally:
            try:
                review_launcher.shutdown_memory_provider()
            except Exception:
                pass
            try:
                review_launcher.close()
            except Exception:
                pass

    try:
        while True:
            user_message = read_user_message()
            if user_message is _READ_EOF:
                # stdin 被关闭，直接结束（不跑 review，避免误触发）。
                break
            if user_message is _READ_TASK_END:
                run_review_if_enabled()
                break
            if not user_message:
                continue

            result = agent.run_conversation(
                user_message=user_message,
                conversation_history=full_history,
            )

            # 用本轮 run_conversation 返回的完整 transcript 覆盖 full_history，
            # 它已经累计了所有历史 user / assistant / tool 消息。退化时回退到
            # ``_session_messages``，最后一档保留旧值，避免下一轮 / review 拿不到上下文。
            messages_snapshot = list(
                result.get("messages")
                or getattr(agent, "_session_messages", [])
                or full_history
            )
            full_history = messages_snapshot

            final_response = result.get("final_response")
            if final_response is None:
                error = str(result.get("error") or "").strip()
                turn_exit_reason = str(result.get("turn_exit_reason") or "").strip()
                fallback = NONE_RESPONSE_FALLBACK
                details: list[str] = []
                if turn_exit_reason:
                    details.append(f"reason={turn_exit_reason}")
                if error:
                    details.append(f"error={error}")
                if details:
                    fallback = f"{fallback} ({'; '.join(details)})"
                emit_response(fallback)
                continue

            emit_response(str(final_response))
        return 0
    finally:
        try:
            agent.shutdown_memory_provider()
        except Exception:
            pass
        try:
            agent.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
