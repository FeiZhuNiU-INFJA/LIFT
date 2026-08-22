#!/usr/bin/env bash
# Prime Agent 镜像分层 —— L4 轻量层（配置 / 渲染 / 补丁，秒级）。
#
# 职责：
#   1) 渲染 Prime Agent 自定义 provider（models.json）+ 默认模型（settings.json），
#      把 Work LLM（WORK_OPENAI_* + MODEL_NAME）接成 OpenAI 兼容后端。
#   2) 在 settings.json 声明 Firecrawl 远程 MCP（mcpServers.firecrawl，静态 bearer
#      token → FIRECRAWL_API_KEY），配套 skills/firecrawl 让 kernel 能联网抓取/搜索。
#   3) Langfuse env 落地（见文末说明，容器内 plugin trace 目前不注入）。
#
# 输入（build-args → docker ENV）：
#   WORK_OPENAI_API_KEY / WORK_OPENAI_BASE_URL / MODEL_NAME
#   LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST
#   REASONING_EFFORT
#   PRIME_AGENT_CODING_AGENT_DIR（状态根目录，Dockerfile 已 ENV 钉死）
#
# 配置契约（对齐 prime-agent 0.7.x 出厂文档 docs/models.md + docs/settings.md）：
#   - getAgentDir() = ${PRIME_AGENT_CODING_AGENT_DIR}（默认 ~/.prime/agent）。
#   - 自定义 provider：${AGENT_DIR}/models.json → { "providers": { "<name>": {...} } }。
#     每次打开 /model 会重载；apiKey 字段值若命中同名环境变量则取环境变量值，
#     否则按字面量使用（resolveConfigValueUncached）。故此处写环境变量名
#     "WORK_OPENAI_API_KEY"（镜像已 ENV 承载），密钥不落进 JSON 明文。
#   - openai-completions 走 OpenAI SDK client，apiKey 自动带 Authorization: Bearer，
#     标准端点无需 authHeader:true。
#   - 默认模型：${AGENT_DIR}/settings.json → defaultProvider / defaultModel /
#     defaultThinkingLevel（非交互 print/json 模式据此选型）。
set -euo pipefail

AGENT_DIR="${PRIME_AGENT_CODING_AGENT_DIR:-/root/.prime/agent}"
mkdir -p "${AGENT_DIR}"
echo "==> Prime Agent state dir: ${AGENT_DIR}"

# LIFT 约定的自定义 provider 名（与 openclaw 的 provider="custom" 语义对齐）。
PRIME_AGENT_PROVIDER="${PRIME_AGENT_PROVIDER:-custom}"

# ─────────────────────────────────────────────────────────────────────
# 1) 渲染 models.json（自定义 provider）+ settings.json（默认选型）
# ─────────────────────────────────────────────────────────────────────
# LIFT 约定：MODEL_NAME 传进来时已由 build-image.sh 剥掉 custom/ 前缀，此处直接用。
PRIME_AGENT_PROVIDER="${PRIME_AGENT_PROVIDER}" python3 - <<'PYEOF'
import json
import os
from pathlib import Path

agent_dir = Path(os.environ.get("PRIME_AGENT_CODING_AGENT_DIR", "/root/.prime/agent"))
agent_dir.mkdir(parents=True, exist_ok=True)

provider_name = os.environ.get("PRIME_AGENT_PROVIDER", "custom")
model_id = os.environ.get("MODEL_NAME", "").strip()
base_url = os.environ.get("WORK_OPENAI_BASE_URL", "").strip() \
    or "https://ark.cn-beijing.volces.com/api/v3"

# thinking level：由 REASONING_EFFORT 驱动，落在 prime-agent 允许集合内，否则回退 high。
allowed_levels = {"off", "minimal", "low", "medium", "high", "xhigh", "max"}
thinking_level = os.environ.get("REASONING_EFFORT", "high").strip().lower()
if thinking_level not in allowed_levels:
    thinking_level = "high"

# compat 开关：多数 OpenAI 兼容自建端点不认 developer role（用 system 代替）；
# reasoning_effort 是否支持按需切（默认放行，端点若拒绝可 rebuild 关闭）。
def _bool_env(name, default):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}

supports_developer_role = _bool_env("PRIME_AGENT_SUPPORTS_DEVELOPER_ROLE", False)
supports_reasoning_effort = _bool_env("PRIME_AGENT_SUPPORTS_REASONING_EFFORT", True)
reasoning = _bool_env("PRIME_AGENT_REASONING", True)

model_entry = {
    "id": model_id,
    "name": model_id or provider_name,
    "reasoning": reasoning,
    "input": ["text"],
    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
    "contextWindow": int(os.environ.get("PRIME_AGENT_CONTEXT_WINDOW", "128000")),
    "maxTokens": int(os.environ.get("PRIME_AGENT_MAX_TOKENS", "16384")),
    "compat": {
        "supportsDeveloperRole": supports_developer_role,
        "supportsReasoningEffort": supports_reasoning_effort,
    },
}

models_config = {
    "providers": {
        provider_name: {
            "baseUrl": base_url,
            "api": "openai-completions",
            # apiKey 写环境变量名：prime-agent 命中同名 env → 取值；否则按字面量。
            # 镜像已 ENV 承载 WORK_OPENAI_API_KEY，密钥不落 JSON 明文。
            "apiKey": "WORK_OPENAI_API_KEY",
            "models": [model_entry],
        }
    }
}

models_path = agent_dir / "models.json"
models_path.write_text(
    json.dumps(models_config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
)

# settings.json：默认 provider/model + thinking level；关掉 telemetry（评测环境
# 无需向 Prime Intellect 上报），startup 静音。
settings_path = agent_dir / "settings.json"
settings = {}
if settings_path.exists():
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        if not isinstance(settings, dict):
            settings = {}
    except Exception:
        settings = {}
settings.update(
    {
        "defaultProvider": provider_name,
        "defaultModel": model_id,
        "defaultThinkingLevel": thinking_level,
        "quietStartup": True,
        "telemetry": {"enabled": False},
    }
)

# mcpServers.firecrawl：声明 Firecrawl 官方远程 MCP（路线一：静态 bearer token）。
#   - type=http + url 指向官方 v2 端点；McpIntegration._open_session 走 streamable HTTP。
#   - bearerTokenEnvVar 引用 FIRECRAWL_API_KEY（镜像 ENV 承载），发
#     Authorization: Bearer <token>，与 Firecrawl 文档的 header 鉴权对齐；密钥不落 JSON。
#   - 用户自建 mcpServers 条目不受 auth-gate 约束：firecrawl skill 始终加载，key 缺失时
#     调用抛 NotEnabled（agent 可回退 kernel + requests）。
# 合并式写入：保留（或不覆盖）用户已有的其它 mcpServers 条目，仅更新/新增 firecrawl。
mcp_servers = settings.get("mcpServers")
if not isinstance(mcp_servers, dict):
    mcp_servers = {}
mcp_servers["firecrawl"] = {
    "type": "http",
    "url": "https://mcp.firecrawl.dev/v2/mcp",
    "bearerTokenEnvVar": "FIRECRAWL_API_KEY",
}
settings["mcpServers"] = mcp_servers
settings_path.write_text(
    json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
)

print(f"Rendered {models_path}")
print(f"  provider: {provider_name}  api: openai-completions")
print(f"  model   : {model_id or '<empty>'}")
print(f"  base_url: {base_url}")
print(f"  apiKey  : env-ref WORK_OPENAI_API_KEY ("
      f"{'set' if os.environ.get('WORK_OPENAI_API_KEY') else 'EMPTY at build'})")
print(f"  reasoning={reasoning} supportsDeveloperRole={supports_developer_role} "
      f"supportsReasoningEffort={supports_reasoning_effort}")
print(f"Rendered {settings_path}")
print(f"  defaultProvider={provider_name} defaultModel={model_id or '<empty>'} "
      f"defaultThinkingLevel={thinking_level}")
print(f"  mcpServers.firecrawl: http https://mcp.firecrawl.dev/v2/mcp "
      f"bearerTokenEnvVar=FIRECRAWL_API_KEY ("
      f"{'set' if os.environ.get('FIRECRAWL_API_KEY') else 'EMPTY at build'})")
PYEOF

# ─────────────────────────────────────────────────────────────────────
# 2) Langfuse 关联
# ─────────────────────────────────────────────────────────────────────
# Prime Agent 是 Node/TS runtime，无 EvoScientist/GA 那样的 Python sitecustomize
# hook 可注入；其自带 trace 分享面向 Prime Intellect（opt-in），非 Langfuse。
# 当前策略（选项 C 兜底）：只把 Langfuse env 落到镜像（Dockerfile ENV 承载），
# 容器内不打 plugin trace，仅靠 LIFT host 侧 pre-chat span 关联。代价：trace_backfill
# 缺容器侧 usage，5 字段 token 统计不完整。
# 后续可选升级（选项 B）：写一个 prime-agent extension，在 turn-end 打 Langfuse
# root span（trace name 需并入 src/models.py LANGFUSE_PLUGIN_TRACE_NAMES 且同步
# post-process 配对逻辑）；或从 chat 的 `--mode json` 事件流解析 message_end.usage
# 直接回填。二者均非本次 provider/调用形态改造范围。
echo "==> Langfuse env baked (LANGFUSE_HOST=${LANGFUSE_HOST:-<unset>}); in-container plugin trace not emitted (host-side span only)."

echo "[L4] Prime Agent config layer done."
