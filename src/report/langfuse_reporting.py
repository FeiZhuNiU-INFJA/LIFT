"""
Pre-chat Langfuse (SDK v4) reporting for evolve_eval.

Credentials: LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST / LANGFUSE_BASE_URL
(typically set in .env; loaded via src.config load_dotenv).

Uses the same ``session_id`` as OpenClaw / Hermes ``conversation`` / ``--session-id`` so traces
group under the same Langfuse session as the langfuse-tracer plugin.

Disable with EVAL_LANGFUSE_PRE_CHAT=0|false.
"""

from __future__ import annotations

from src.config import CONFIG, LOGGER
from src.models import FornaxUdfTags


def _langfuse_credentials_present() -> bool:
    return CONFIG.langfuse_credentials_present


def _truncate_str(value: str, max_len: int = 200) -> str:
    """Langfuse v4 propagated metadata: dict[str, str], value length limit 200."""
    if len(value) <= max_len:
        return value
    return value[: max_len - 15] + "...(truncated)"


def _tags_to_full_payload(tags: FornaxUdfTags) -> dict[str, str]:
    return {
        "run": tags.run,
        "task": tags.task,
        "task_query": tags.task_query,
        "is_final_task": tags.is_final_task,
        "is_evolve_turn": tags.is_evolve_turn,
        "is_ended": tags.is_ended,
        "content_reqs": tags.content_reqs,
        "trajectory_reqs": tags.trajectory_reqs,
        "content_score": tags.content_score,
    }


def emit_pre_chat_state(
    *,
    session_id: str,
    tags: FornaxUdfTags,
    chat_role: str,
) -> None:
    """
    Emit one short-lived span before the agent HTTP/CLI call.

    - ``propagate_attributes(session_id=...)`` matches OpenClaw sessionKey / plugin session.
    - Full dynamic fields live on span ``input`` (JSON-serializable dict).
    - ``propagate_attributes(metadata=...)`` only carries short string dimensions (v4 limit).
    """
    if not CONFIG.langfuse_pre_chat or not _langfuse_credentials_present():
        return

    try:
        from langfuse import get_client, propagate_attributes
    except ImportError:
        LOGGER.warning("langfuse not installed; skip pre-chat reporting")
        return

    client = get_client()
    if not getattr(client, "_tracing_enabled", True):
        return

    payload = _tags_to_full_payload(tags)
    # short_meta: dict[str, str] = {
    #     "source": "evolve_eval",
    #     "eval_run": _truncate_str(tags.run),
    #     "eval_task": _truncate_str(tags.task),
    #     "session_id": _truncate_str(session_id),
    #     "chat_role": _truncate_str(chat_role),
    #     "is_final_task": "true" if tags.is_final_task else "false",
    #     "is_evolve_turn": "true" if tags.is_evolve_turn else "false",
    #     "is_ended": "true" if tags.is_ended else "false",
    #     "content_score": _truncate_str(str(tags.content_score)),
    # }
    # trace_name = _truncate_str(f"evolve_eval:{tags.task}")

    try:
        with propagate_attributes(
            session_id=session_id,
            user_id=_truncate_str(tags.run),
            # trace_name=trace_name,
            # metadata=short_meta,
            tags=[chat_role, tags.run, tags.task, tags.agent_name],
        ):
            with client.start_as_current_observation(
                as_type="span",
                name=f"{chat_role}",
                input=payload,
            ):
                pass
        client.flush()
    except Exception:
        LOGGER.exception(
            "Langfuse pre-chat reporting failed (session_id=%s, chat_role=%s)",
            session_id,
            chat_role,
        )
