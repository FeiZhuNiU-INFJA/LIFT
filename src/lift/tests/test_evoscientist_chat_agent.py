"""EvoScientist chat transport helpers."""

from __future__ import annotations

from src.lift.adapters.evoscientist.chat_agent import (
    _extract_done_response,
    _parse_resume_thread_id,
)


def test_parse_resume_thread_id_plain_stderr() -> None:
    stderr = """
Loading agent...
Goodbye!

Resume this session with:
EvoSci --resume 3f4a9c2d
"""
    assert _parse_resume_thread_id(stderr) == "3f4a9c2d"


def test_parse_resume_thread_id_strips_ansi() -> None:
    stderr = "\x1b[36mEvoSci --resume abcdef12\x1b[0m\n"
    assert _parse_resume_thread_id(stderr) == "abcdef12"


def test_parse_resume_thread_id_returns_none_without_hint() -> None:
    assert _parse_resume_thread_id("Goodbye!\n") is None


def test_extract_done_response_prefers_done_response() -> None:
    jsonl = (
        '{"type":"text","content":"partial"}\n'
        '{"type":"done","response":"final answer"}\n'
    )
    assert _extract_done_response(jsonl) == "final answer"

