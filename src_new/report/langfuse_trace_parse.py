"""Parse Langfuse trace raw input/output/metadata into typed models."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ValidationError

from src_new.models import LANGFUSE_PLUGIN_TRACE_NAMES, LangfuseAgentTraceInput, LangfusePluginTraceMetadata


def is_plugin_trace(name: str | None) -> bool:
    return (name or "") in LANGFUSE_PLUGIN_TRACE_NAMES


def is_agent_trace(name: str | None) -> bool:
    n = name or ""
    return n.endswith("_agent")


def _coerce_dict(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def parse_agent_input(raw: Any) -> LangfuseAgentTraceInput | None:
    data = _coerce_dict(raw)
    if not data:
        return None
    try:
        return LangfuseAgentTraceInput.model_validate(data)
    except ValidationError:
        return None


def parse_plugin_metadata(raw: Any) -> LangfusePluginTraceMetadata | None:
    data = _coerce_dict(raw)
    if not data:
        return None
    try:
        return LangfusePluginTraceMetadata.from_langfuse_dict(data)
    except ValidationError:
        return None


def _as_optional_str(raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    return str(raw)


class StructuredTracePayload(BaseModel):
    agent_input: LangfuseAgentTraceInput | None = None
    plugin_prompt: str | None = None
    plugin_response: str | None = None
    plugin_metadata: LangfusePluginTraceMetadata | None = None


def structure_trace_payload(
    name: str | None,
    raw_input: Any,
    raw_output: Any,
    raw_metadata: Any,
) -> StructuredTracePayload:
    if is_plugin_trace(name):
        meta = parse_plugin_metadata(raw_metadata)
        if meta is None and isinstance(raw_metadata, dict) and raw_metadata:
            meta = LangfusePluginTraceMetadata.from_langfuse_dict(raw_metadata)
        return StructuredTracePayload(
            plugin_prompt=_as_optional_str(raw_input),
            plugin_response=_as_optional_str(raw_output),
            plugin_metadata=meta,
        )
    if is_agent_trace(name):
        return StructuredTracePayload(agent_input=parse_agent_input(raw_input))
    return StructuredTracePayload()
