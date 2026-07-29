#!/usr/bin/env bash
# 构建期第 4 层（轻量 · 频繁变动）：只做 config.toml 渲染 + [autonomy]/[cost] 预烘焙。
# 独立成脚本以便与 Node/agentmemory warmup 解耦：改任何 config 相关字段只 bust 本层，
# 不用重跑几分钟的 npm/warmup。
#
# 依赖上层的 env：API_KEY / API_URL / INFERENCE_URL / DEFAULT_MODEL / MAX_TOKENS proxy 三件套 /
#                  INSTALL_AGENTMEMORY。
set -euo pipefail

OPENHUMAN_HOME="/root/.openhuman"
mkdir -p "${OPENHUMAN_HOME}"

escape_sed() {
  printf '%s' "${1:-}" | sed -e 's/[\/&]/\\&/g' -e ':a;N;$!ba;s/\n/\\n/g'
}

# LIFT max_tokens 代理:openhuman-core Rust binary 不暴露任何 max_tokens 覆盖入口,
# 我们在容器 127.0.0.1:${LIFT_PROXY_PORT} 起一个透明反向代理,注入 MAX_TOKENS 后
# 再转发到真实上游 (INFERENCE_URL)。config.toml 的 inference_url 改指向 proxy。
# 通过 LIFT_MAX_TOKENS_PROXY_ENABLED=false 关闭代理(此时 inference_url 直连 upstream)。
LIFT_MAX_TOKENS_PROXY_ENABLED="${LIFT_MAX_TOKENS_PROXY_ENABLED:-true}"
LIFT_PROXY_PORT="${LIFT_PROXY_PORT:-7787}"
UPSTREAM_INFERENCE_URL="${INFERENCE_URL:-https://ark.cn-beijing.volces.com/api/v3}"
if [[ "${LIFT_MAX_TOKENS_PROXY_ENABLED}" == "true" || "${LIFT_MAX_TOKENS_PROXY_ENABLED}" == "1" ]]; then
  EFFECTIVE_INFERENCE_URL="http://127.0.0.1:${LIFT_PROXY_PORT}/v3"
  echo "==> max_tokens proxy enabled; inference_url=${EFFECTIVE_INFERENCE_URL}, upstream=${UPSTREAM_INFERENCE_URL}"
else
  EFFECTIVE_INFERENCE_URL="${UPSTREAM_INFERENCE_URL}"
  echo "==> max_tokens proxy disabled; inference_url directly points to upstream ${EFFECTIVE_INFERENCE_URL}"
fi

API_KEY_ESC="$(escape_sed "${API_KEY:-}")"
API_URL_ESC="$(escape_sed "${API_URL:-}")"
INFERENCE_URL_ESC="$(escape_sed "${EFFECTIVE_INFERENCE_URL}")"
DEFAULT_MODEL_ESC="$(escape_sed "${DEFAULT_MODEL:-}")"

sed \
  -e "s/__API_KEY__/${API_KEY_ESC}/g" \
  -e "s/__API_URL__/${API_URL_ESC}/g" \
  -e "s/__INFERENCE_URL__/${INFERENCE_URL_ESC}/g" \
  -e "s/__DEFAULT_MODEL__/${DEFAULT_MODEL_ESC}/g" \
  /tmp/config.toml.template > "${OPENHUMAN_HOME}/config.toml"

echo "==> Rendered ${OPENHUMAN_HOME}/config.toml:"
sed -e 's/api_key = ".*"/api_key = "<redacted>"/' "${OPENHUMAN_HOME}/config.toml"

# ── sandbox 边界放行 /workspace/task（关键修复）────────────────────────────
# openhuman-core 除 acting-tool sandbox 的 action_dir 外还有第二层 autonomy 边界：
#   workspace_only=true + forbidden_paths 含 /root/tmp/opt → 即便 OPENHUMAN_ACTION_DIR
#   注入到 /workspace/task，agent 访问 /workspace/task/qN_materials/xxx 仍会被判
#   "Resolved path escapes workspace"。真实边界由 ~/.openhuman/users/local/config.toml
#   的 [autonomy] 段控制，字段与 CLI ``config update_autonomy_settings`` 一一对应。
#
# 直接在 build 期预烘焙 users/local/config.toml，避免运行期每容器 exec 一次的竞态。
# workspace_only=false + trusted_roots=/workspace/task(rw) 打开 LIFT 挂载点；
# forbidden_paths 只保留凭据目录，不再涵盖 /root/tmp/opt（否则会把 agentmemory
# 的 /root/.agentmemory 一起拦掉）。
OPENHUMAN_USER_HOME="${OPENHUMAN_HOME}/users/local"
mkdir -p "${OPENHUMAN_USER_HOME}"
cat > "${OPENHUMAN_USER_HOME}/config.toml" <<'AUTONOMY_EOF'
# openhuman-core 的 [autonomy] 有两层语义:
#   1. path 边界: workspace_only + trusted_roots + forbidden_paths → 决定路径拦不拦
#   2. approval tier: level + require_* + auto_approve → 决定 tool call 需不需要人点批准
# 只放开路径边界不够。默认 level 兜到 "supervised" + require_task_plan_approval
# + require_approval_for_medium_risk 会让 headless 容器里的 agent 在没人点批准时把
# medium-risk tool (file_write / execute_shell / node_exec / npm_exec) 都视为被拦，
# 然后按内建 policy-blocked 模板对用户回复"权限不足,请去 Settings -> Agent access"。
# LIFT 评测里没人 approval,必须把整个 approval loop 关掉。
#
# 字段名必须精确匹配 openhuman-core Rust binary 的期望:
#   level (NOT autonomy_level) — 之前烘焙时用了 autonomy_level 被 schema migration 直接扔掉
#   auto_approve 里 file_write 必须显式列出 — 否则 schema init 会移除
[autonomy]
level = "autonomous"
require_task_plan_approval = false
require_approval_for_medium_risk = false
block_high_risk_commands = false
allow_tool_install = true
max_actions_per_hour = 100000
max_cost_per_day_cents = 100000000
auto_approve = [
  "file_read", "file_write", "file_edit", "file_delete", "file_search",
  "memory_search", "memory_write", "memory_tree_drill_down", "memory_tree_query_source",
  "web_search_tool", "web_fetch", "browser",
  "shell", "node_exec", "npm_exec", "schedule",
  "git_operations", "skills",
  "glob", "grep",
]

workspace_only = false
forbidden_paths = ["/etc", "/root/.ssh", "/root/.gnupg", "/root/.aws", "/root/.config"]

[[autonomy.trusted_roots]]
path = "/workspace/task"
access = "readwrite"

# LIFT 评测场景已预付费到上游 provider，openhuman-core 内建的 daily_limit_usd=10
# 会在长上下文任务里累积到上限后拒 chat（"cost budget exceeded"）。评测无成本内约，
# 直接关掉 cost tracking；无 env 覆盖，只能落 config.toml。
[cost]
enabled = false
AUTONOMY_EOF
echo "==> Wrote ${OPENHUMAN_USER_HOME}/config.toml (autonomy: level=autonomous, approval loop disabled, trusted_roots=/workspace/task rw; cost.enabled=false)"

# agentmemory 已在上一层安装/warmup 完毕，此处只做 config.toml 的 [memory] 段追加。
INSTALL_AGENTMEMORY="${INSTALL_AGENTMEMORY:-false}"
if [[ "${INSTALL_AGENTMEMORY}" == "true" ]]; then
  if ! grep -q '^\[memory\]' "${OPENHUMAN_HOME}/config.toml" 2>/dev/null; then
    {
      echo ""
      echo "[memory]"
      echo 'backend = "agentmemory"'
      echo 'agentmemory_url = "http://localhost:3111"'
    } >> "${OPENHUMAN_HOME}/config.toml"
    echo "==> Appended [memory] backend=agentmemory to config.toml"
  else
    echo "==> [memory] block already present in config.toml; skip append"
  fi
fi

# Sanity check：确认 openhuman-core 可执行。--help 不同版本行为不一致，容忍失败。
if command -v openhuman-core >/dev/null 2>&1; then
  openhuman-core --version 2>/dev/null \
    || openhuman-core help >/dev/null 2>&1 \
    || true
else
  echo "WARN: openhuman-core binary not found on PATH; check .deb layout" >&2
fi

echo "OpenHuman baked; config at ${OPENHUMAN_HOME}/config.toml"

