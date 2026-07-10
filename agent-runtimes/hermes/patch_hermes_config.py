#!/usr/bin/env python3
"""Patch Hermes config.yaml model block from environment variables.

Runs at container startup (see hermes-entrypoint.sh), NOT at build time, so
secrets never get baked into image layers.

Model block written (per plan §A.1 / §D.12):

    model:
      default:  <suffix of MODEL_NAME after "/">
      provider: custom                             (forced)
      base_url: <WORK_OPENAI_BASE_URL>
      api_key:  <WORK_OPENAI_API_KEY>
      api_mode: chat_completions
      max_tokens: <MAX_TOKENS>                     (output cap; default 51200)

Existing config.yaml keys are preserved; only the `model` block is upserted.
If PyYAML is unavailable, a plaintext fallback replaces ONLY the top-level
`model:` block (keeping every other key) and logs that it took the fallback.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("HERMES_CONFIG_PATH", "/opt/hermes-state/config.yaml"))


def _model_default() -> str:
    model_name = os.environ.get("MODEL_NAME", "").strip()
    if not model_name:
        return ""
    if not (model_name.startswith("custom/") and len(model_name) > len("custom/")):
        print(
            "[patch-hermes-config] WARN: MODEL_NAME must be 'custom/model_id' "
            f"(e.g. custom/ep-xxxx); got {model_name!r}."
        )
        return model_name
    return model_name.split("/", 1)[1]


def _base_url() -> str:
    return os.environ.get("WORK_OPENAI_BASE_URL", "").strip()


def _api_key() -> str:
    return os.environ.get("WORK_OPENAI_API_KEY", "").strip()


def _max_tokens() -> int:
    """model.max_tokens：读 ``MAX_TOKENS``（与 runner ``--max-tokens`` 同源），默认 51200。"""
    raw = os.environ.get("MAX_TOKENS", "").strip()
    if not raw:
        return 51200
    try:
        return int(raw)
    except ValueError:
        return 51200


def _model_block() -> dict:
    return {
        "default": _model_default(),
        "provider": "custom",
        "base_url": _base_url(),
        "api_key": _api_key(),
        "api_mode": "chat_completions",
        "max_tokens": _max_tokens(),
    }


def _patch_with_yaml(model_block: dict) -> bool:
    try:
        import yaml  # type: ignore
    except Exception:
        return False

    data = {}
    if CONFIG_PATH.exists():
        try:
            loaded = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}

    existing = data.get("model")
    if isinstance(existing, dict):
        existing.update(model_block)
        data["model"] = existing
    else:
        data["model"] = model_block

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return True


def _render_model_block(model_block: dict) -> str:
    """Render the top-level ``model:`` YAML block (2-space indented children)."""
    lines = ["model:"]
    for key, value in model_block.items():
        lines.append(f"  {key}: {value}")
    return "\n".join(lines) + "\n"


def _patch_plaintext(model_block: dict) -> None:
    """Fallback used ONLY when PyYAML is unavailable.

    PyYAML is installed + verified into the Hermes venv at image build time
    (install-in-image.sh), and the entrypoint runs this script with that venv's
    python, so this path should not trigger in a correctly built image. It exists
    so a degraded image still configures Hermes instead of silently misbehaving.

    Behaviour: replace ONLY the top-level ``model:`` block and keep every other
    key. The top-level block spans from a line starting with ``model:`` up to the
    next top-level key (a non-indented, non-blank line). If no ``model:`` block
    exists, the new block is appended. Never rewrites the file to model-only.
    """
    new_block = _render_model_block(model_block)

    if not CONFIG_PATH.exists():
        # No existing config (unusual for a real Hermes image): create minimal.
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(new_block, encoding="utf-8")
        print(f"[patch-hermes-config] fallback(no-yaml): created new {CONFIG_PATH} with model block only")
        return

    original = CONFIG_PATH.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)

    start: int | None = None
    end: int = len(lines)
    for i, line in enumerate(lines):
        if re.match(r"^model\s*:", line):
            start = i
            # Find the end: next top-level key (non-indented, non-blank, not a comment-only continuation).
            for j in range(i + 1, len(lines)):
                nxt = lines[j]
                if nxt.strip() == "":
                    continue
                if not nxt[:1].isspace():
                    end = j
                    break
            else:
                end = len(lines)
            break

    if start is None:
        # No model block present: append one (ensure trailing newline separation).
        sep = "" if original.endswith("\n") or original == "" else "\n"
        CONFIG_PATH.write_text(original + sep + new_block, encoding="utf-8")
        print(f"[patch-hermes-config] fallback(no-yaml): appended model block to {CONFIG_PATH} (other keys preserved)")
        return

    patched = "".join(lines[:start]) + new_block + "".join(lines[end:])
    CONFIG_PATH.write_text(patched, encoding="utf-8")
    print(f"[patch-hermes-config] fallback(no-yaml): replaced model block in {CONFIG_PATH} (other keys preserved)")


def main() -> None:
    model_block = _model_block()
    if not model_block["default"]:
        print("[patch-hermes-config] WARN: model.default empty (MODEL_NAME unset).")
    if not model_block["api_key"]:
        print("[patch-hermes-config] WARN: WORK_OPENAI_API_KEY empty; Hermes will have no api_key.")
    if not model_block["base_url"]:
        print("[patch-hermes-config] WARN: WORK_OPENAI_BASE_URL empty; Hermes has no base_url.")

    if _patch_with_yaml(model_block):
        print(f"[patch-hermes-config] patched model block via PyYAML in {CONFIG_PATH}")
    else:
        print("[patch-hermes-config] WARN: PyYAML unavailable in this interpreter; using plaintext fallback.")
        _patch_plaintext(model_block)

    print(f"[patch-hermes-config] model block now in {CONFIG_PATH}:")
    for key, value in model_block.items():
        shown = value if key != "api_key" else ("<set>" if value else "<empty>")
        print(f"    {key}: {shown}")


if __name__ == "__main__":
    main()
