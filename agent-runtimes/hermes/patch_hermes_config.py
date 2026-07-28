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


def _openspace_enabled() -> bool:
    """OpenSpace MCP server 是否需要注册进 config.yaml。

    由镜像 ENV ``OPENSPACE_ENABLED``（Dockerfile 在 INSTALL_OPENSPACE=true 时置 true）
    驱动；作为兜底，若 /opt/openspace-venv 存在也视为启用。
    """
    flag = os.environ.get("OPENSPACE_ENABLED", "").strip().lower()
    if flag in {"1", "true", "yes"}:
        return True
    if flag in {"0", "false", "no"}:
        return False
    return Path("/opt/openspace-venv/bin/openspace-mcp").exists()


def _agentmemory_enabled() -> bool:
    """agentmemory memory provider 是否需要写进 config.yaml 的 ``memory.provider``。

    由镜像 ENV ``AGENTMEMORY_ENABLED``（Dockerfile 在 INSTALL_AGENTMEMORY=true 时置 true）
    驱动；作为兜底，若 ``$HERMES_HOME/plugins/agentmemory`` 插件目录存在也视为启用。
    """
    flag = os.environ.get("AGENTMEMORY_ENABLED", "").strip().lower()
    if flag in {"1", "true", "yes"}:
        return True
    if flag in {"0", "false", "no"}:
        return False
    hermes_home = os.environ.get("HERMES_HOME", "/opt/hermes-state").strip() or "/opt/hermes-state"
    return Path(hermes_home, "plugins", "agentmemory").exists()


def _openspace_model() -> str:
    """``OPENSPACE_MODEL``：把 ``MODEL_NAME`` 的 ``custom/`` 前缀重映射为 ``openai/``。

    OpenSpace 用 **litellm** 路由模型，而 litellm 不认 ``custom`` 这个 provider——
    实测 ``model=custom/ep-xxx`` 会让 litellm 无法正确应用 api_key/api_base，触发
    ``AuthenticationError: API key or AK/SK ... missing or invalid``，进而 agent 误
    以为要走 OpenSpace cloud 登录。本项目 ``custom/`` 约定表示"OpenAI 兼容自定义端点"
    （provider 恒为 custom，见 config.py），对 litellm 而言等价于 ``openai/<model_id>``
    + 显式 api_base + api_key。因此这里把 ``custom/`` 换成 ``openai/``。

    - 显式 ``OPENSPACE_MODEL`` 优先，且**原样透传**（用户若要指定 litellm 原生 provider
      前缀，如 ``volcengine/`` / ``openrouter/``，直接设 ``OPENSPACE_MODEL`` 即可）。
    - ``MODEL_NAME=custom/<id>`` → 返回 ``openai/<id>``。
    - 其它非 ``custom/`` 前缀（已是 litellm 可识别 provider）原样返回。
    """
    explicit = os.environ.get("OPENSPACE_MODEL", "").strip()
    if explicit:
        return explicit
    model_name = os.environ.get("MODEL_NAME", "").strip()
    if not model_name:
        return ""
    if model_name.startswith("custom/") and len(model_name) > len("custom/"):
        return "openai/" + model_name[len("custom/"):]
    return model_name


def _openspace_env() -> dict[str, str]:
    """构造 ``mcp_servers.openspace.env``：workspace / skill dirs + LLM 凭据。

    MCP stdio 子进程**不会**继承容器任意环境变量（python ``mcp`` SDK 只透传
    ``PATH``/``HOME`` 等安全白名单 + 本 ``env`` 块），因此 OpenSpace 需要的 LLM 配置
    必须显式写进这里：

    - ``OPENSPACE_MODEL``    ← ``MODEL_NAME``（保留 ``custom/`` 前缀）
    - ``OPENSPACE_LLM_API_KEY``  ← ``WORK_OPENAI_API_KEY``
    - ``OPENSPACE_LLM_API_BASE`` ← ``WORK_OPENAI_BASE_URL``

    仅注入非空值：留空时 OpenSpace 自行走其默认解析（provider-native env / 宿主
    agent config / ``openspace/.env`` 兜底），不写入空串污染其解析优先级。
    """
    hermes_home = os.environ.get("HERMES_HOME", "/opt/hermes-state").strip() or "/opt/hermes-state"
    workspace = os.environ.get("OPENSPACE_WORKSPACE", "/opt/OpenSpace").strip() or "/opt/OpenSpace"
    skill_dirs = os.environ.get("OPENSPACE_HOST_SKILL_DIRS", f"{hermes_home}/skills").strip()
    env: dict[str, str] = {
        "OPENSPACE_WORKSPACE": workspace,
        "OPENSPACE_HOST_SKILL_DIRS": skill_dirs,
    }
    model = _openspace_model()
    if model:
        env["OPENSPACE_MODEL"] = model
    api_key = _api_key()
    if api_key:
        env["OPENSPACE_LLM_API_KEY"] = api_key
    api_base = _base_url()
    if api_base:
        env["OPENSPACE_LLM_API_BASE"] = api_base
    return env


def _openspace_server_block() -> dict:
    """``mcp_servers.openspace`` 的内容（stdio transport，见 OpenSpace host_skills/README）。"""
    return {
        "command": "openspace-mcp",
        "env": _openspace_env(),
    }


def _patch_openspace_with_yaml(data: dict) -> None:
    """把 ``mcp_servers.openspace`` upsert 进已加载的 config dict（原地改）。

    保留 mcp_servers 下其它 server 与 openspace 里未覆盖的既有键。
    """
    servers = data.get("mcp_servers")
    if not isinstance(servers, dict):
        servers = {}
    existing = servers.get("openspace")
    block = _openspace_server_block()
    if isinstance(existing, dict):
        existing.update(block)
        servers["openspace"] = existing
    else:
        servers["openspace"] = block
    data["mcp_servers"] = servers


def _patch_memory_provider_with_yaml(data: dict) -> None:
    """把 ``memory.provider = agentmemory`` upsert 进已加载的 config dict（原地改）。

    保留 memory 块下其它既有键（如 agentmemory backend 的可选调优字段）。
    """
    memory = data.get("memory")
    if not isinstance(memory, dict):
        memory = {}
    memory["provider"] = "agentmemory"
    data["memory"] = memory


def _agentmemory_mcp_server_block() -> dict:
    """``mcp_servers.agentmemory`` 的内容（stdio transport，@agentmemory/mcp）。

    与 provider plugin 叠加使用（上游 integrations/hermes README「6-hook plugin on top
    of the MCP server」）。MCP shim 用 ``AGENTMEMORY_URL`` 连正在运行的 :3111 server 时
    代理全部 53 个记忆工具（memory_save / memory_smart_search 等），补上 provider plugin
    未覆盖的"显式写入"通道。stdio 子进程不继承容器任意 env，故 ``AGENTMEMORY_URL`` 必须
    写进 env 块（留空时 shim 默认同址 http://localhost:3111，这里显式写更稳）。
    """
    return {
        "command": "agentmemory-mcp",
        "env": {"AGENTMEMORY_URL": "http://localhost:3111"},
    }


def _patch_agentmemory_mcp_with_yaml(data: dict) -> None:
    """把 ``mcp_servers.agentmemory`` upsert 进已加载的 config dict（原地改）。

    保留 mcp_servers 下其它 server（如 openspace）与 agentmemory 里未覆盖的既有键。
    """
    servers = data.get("mcp_servers")
    if not isinstance(servers, dict):
        servers = {}
    existing = servers.get("agentmemory")
    block = _agentmemory_mcp_server_block()
    if isinstance(existing, dict):
        existing.update(block)
        servers["agentmemory"] = existing
    else:
        servers["agentmemory"] = block
    data["mcp_servers"] = servers


def _patch_with_yaml(
    model_block: dict, add_openspace: bool = False, add_agentmemory: bool = False
) -> bool:
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

    if add_openspace:
        _patch_openspace_with_yaml(data)
    if add_agentmemory:
        _patch_memory_provider_with_yaml(data)
        _patch_agentmemory_mcp_with_yaml(data)

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
    (scripts/install-heavy.sh), and the entrypoint runs this script with that venv's
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


def _render_openspace_block() -> str:
    """Render a top-level ``mcp_servers:`` block containing only ``openspace``.

    Used only in the PyYAML-unavailable fallback. Appended when no ``mcp_servers:``
    block exists; if one already exists we leave it alone (degraded path only), to
    avoid corrupting a hand-authored multi-server block without a YAML parser.
    """
    block = _openspace_server_block()
    lines = ["mcp_servers:", "  openspace:", f"    command: {block['command']}", "    env:"]
    for key, value in block["env"].items():
        lines.append(f"      {key}: {value}")
    return "\n".join(lines) + "\n"


def _patch_openspace_plaintext() -> None:
    """Fallback OpenSpace registration when PyYAML is unavailable.

    Only appends a ``mcp_servers:`` block if none exists. If ``mcp_servers:`` is
    already present, skip (can't safely merge without a parser) and warn.
    """
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(_render_openspace_block(), encoding="utf-8")
        print(f"[patch-hermes-config] fallback(no-yaml): created {CONFIG_PATH} with mcp_servers.openspace")
        return
    original = CONFIG_PATH.read_text(encoding="utf-8")
    if re.search(r"^mcp_servers\s*:", original, flags=re.MULTILINE):
        print("[patch-hermes-config] fallback(no-yaml): mcp_servers already present; skip OpenSpace append (install PyYAML to merge).")
        return
    sep = "" if original.endswith("\n") or original == "" else "\n"
    CONFIG_PATH.write_text(original + sep + _render_openspace_block(), encoding="utf-8")
    print(f"[patch-hermes-config] fallback(no-yaml): appended mcp_servers.openspace to {CONFIG_PATH}")


def _patch_memory_provider_plaintext() -> None:
    """Fallback ``memory.provider = agentmemory`` registration when PyYAML is unavailable.

    Only appends a ``memory:`` block if none exists. If ``memory:`` is already present,
    skip (can't safely merge without a parser) and warn.
    """
    block = "memory:\n  provider: agentmemory\n"
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(block, encoding="utf-8")
        print(f"[patch-hermes-config] fallback(no-yaml): created {CONFIG_PATH} with memory.provider=agentmemory")
        return
    original = CONFIG_PATH.read_text(encoding="utf-8")
    if re.search(r"^memory\s*:", original, flags=re.MULTILINE):
        print("[patch-hermes-config] fallback(no-yaml): memory block already present; skip agentmemory append (install PyYAML to merge).")
        return
    sep = "" if original.endswith("\n") or original == "" else "\n"
    CONFIG_PATH.write_text(original + sep + block, encoding="utf-8")
    print(f"[patch-hermes-config] fallback(no-yaml): appended memory.provider=agentmemory to {CONFIG_PATH}")


def _render_agentmemory_mcp_block() -> str:
    """Render a top-level ``mcp_servers:`` block containing only ``agentmemory``.

    Used only in the PyYAML-unavailable fallback. Mirrors ``_render_openspace_block``.
    """
    block = _agentmemory_mcp_server_block()
    lines = ["mcp_servers:", "  agentmemory:", f"    command: {block['command']}", "    env:"]
    for key, value in block["env"].items():
        lines.append(f"      {key}: {value}")
    return "\n".join(lines) + "\n"


def _patch_agentmemory_mcp_plaintext() -> None:
    """Fallback agentmemory MCP-server registration when PyYAML is unavailable.

    Only appends a ``mcp_servers:`` block if none exists. If ``mcp_servers:`` is
    already present (e.g. OpenSpace appended one, or a hand-authored block), skip
    and warn — can't safely merge a second server without a parser. In a correctly
    built image PyYAML is available, so this degraded path should not trigger.
    """
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(_render_agentmemory_mcp_block(), encoding="utf-8")
        print(f"[patch-hermes-config] fallback(no-yaml): created {CONFIG_PATH} with mcp_servers.agentmemory")
        return
    original = CONFIG_PATH.read_text(encoding="utf-8")
    if re.search(r"^mcp_servers\s*:", original, flags=re.MULTILINE):
        print("[patch-hermes-config] fallback(no-yaml): mcp_servers already present; skip agentmemory MCP append (install PyYAML to merge).")
        return
    sep = "" if original.endswith("\n") or original == "" else "\n"
    CONFIG_PATH.write_text(original + sep + _render_agentmemory_mcp_block(), encoding="utf-8")
    print(f"[patch-hermes-config] fallback(no-yaml): appended mcp_servers.agentmemory to {CONFIG_PATH}")


def main() -> None:
    model_block = _model_block()
    if not model_block["default"]:
        print("[patch-hermes-config] WARN: model.default empty (MODEL_NAME unset).")
    if not model_block["api_key"]:
        print("[patch-hermes-config] WARN: WORK_OPENAI_API_KEY empty; Hermes will have no api_key.")
    if not model_block["base_url"]:
        print("[patch-hermes-config] WARN: WORK_OPENAI_BASE_URL empty; Hermes has no base_url.")

    add_openspace = _openspace_enabled()
    add_agentmemory = _agentmemory_enabled()

    if _patch_with_yaml(model_block, add_openspace=add_openspace, add_agentmemory=add_agentmemory):
        print(f"[patch-hermes-config] patched model block via PyYAML in {CONFIG_PATH}")
        if add_openspace:
            print("[patch-hermes-config] registered mcp_servers.openspace via PyYAML")
        if add_agentmemory:
            print("[patch-hermes-config] set memory.provider=agentmemory + registered mcp_servers.agentmemory via PyYAML")
    else:
        print("[patch-hermes-config] WARN: PyYAML unavailable in this interpreter; using plaintext fallback.")
        _patch_plaintext(model_block)
        if add_openspace:
            _patch_openspace_plaintext()
        if add_agentmemory:
            _patch_memory_provider_plaintext()
            _patch_agentmemory_mcp_plaintext()

    print(f"[patch-hermes-config] model block now in {CONFIG_PATH}:")
    for key, value in model_block.items():
        shown = value if key != "api_key" else ("<set>" if value else "<empty>")
        print(f"    {key}: {shown}")
    if add_openspace:
        print("[patch-hermes-config] OpenSpace MCP server 'openspace' registered (command: openspace-mcp)")
        os_env = _openspace_env()
        for key in ("OPENSPACE_MODEL", "OPENSPACE_LLM_API_KEY", "OPENSPACE_LLM_API_BASE",
                    "OPENSPACE_WORKSPACE", "OPENSPACE_HOST_SKILL_DIRS"):
            value = os_env.get(key)
            if value is None:
                shown = "<empty>"
            elif key == "OPENSPACE_LLM_API_KEY":
                shown = "<set>"
            else:
                shown = value
            print(f"    openspace.env.{key}: {shown}")
        if "OPENSPACE_MODEL" not in os_env:
            print("[patch-hermes-config] WARN: OPENSPACE_MODEL empty (MODEL_NAME unset); OpenSpace falls back to its default model.")
        if "OPENSPACE_LLM_API_KEY" not in os_env:
            print("[patch-hermes-config] WARN: OPENSPACE_LLM_API_KEY empty; OpenSpace will try provider-native / host-config credentials.")


if __name__ == "__main__":
    main()
