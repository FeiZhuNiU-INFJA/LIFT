from __future__ import annotations

import os

CONTAINER_LANGFUSE_BASE_URL = "http://host.docker.internal:3000"


def _normalize_langfuse_base_url(raw: str | None) -> str:
    if not raw or not raw.strip():
        return CONTAINER_LANGFUSE_BASE_URL
    lowered = raw.strip().lower()
    if "127.0.0.1" in lowered or "localhost" in lowered:
        return CONTAINER_LANGFUSE_BASE_URL
    return raw.strip()


def container_runtime_env() -> dict[str, str]:
    """Env vars passed into OpenClaw containers (overrides repo .env where needed)."""
    env: dict[str, str] = {}
    ark_key = os.environ.get("ARK_API_KEY")
    if ark_key:
        env["ARK_API_KEY"] = ark_key
    for key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        val = os.environ.get(key)
        if val:
            env[key] = val
    env["LANGFUSE_BASE_URL"] = _normalize_langfuse_base_url(
        os.environ.get("LANGFUSE_BASE_URL")
    )
    return env
