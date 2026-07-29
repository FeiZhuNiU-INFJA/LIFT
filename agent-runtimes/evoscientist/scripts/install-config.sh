#!/usr/bin/env bash
# EvoScientist 镜像分层 —— L4 轻量层（配置 / 渲染 / 补丁，秒级）。
#
# 从旧 install-in-image.sh 拆出：
#   1) 渲染 EvoScientist config.yaml —— WORK_OPENAI_* + MODEL_NAME + REASONING_EFFORT
#   2) 注册 firecrawl-search MCP server（`--env-ref FIRECRAWL_API_KEY`，key 运行期读）
#   3) 部署 langfuse tracing overlay 到 site-packages，走 sitecustomize.py 全局注入
#
# 输入：build-args → docker ENV（同上游 install-in-image.sh，未变）：
#   WORK_OPENAI_API_KEY / WORK_OPENAI_BASE_URL / MODEL_NAME
#   LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST
#   REASONING_EFFORT
#
# firecrawl-mcp 的 npm 包 pre-warm 已迁移到 scripts/install-heavy.sh (L2)。
set -euo pipefail

# EvoScientist config path 遵循 XDG 约定：``get_config_dir()`` 优先读
# XDG_CONFIG_HOME，官方镜像预设为 ``/home/evosci/.evoscientist/.config`` —— 即使
# 我们以 root 运行也**不会**回落到 ``/root/.config/evoscientist``。因此这里直接
# 通过 python 反查 EvoScientist 侧的 config path 并写入。
CONFIG_FILE="$(python3 -c 'from EvoScientist.config.settings import get_config_path; print(get_config_path())')"
CONFIG_DIR="$(dirname "${CONFIG_FILE}")"
mkdir -p "${CONFIG_DIR}"
echo "==> EvoScientist config path (from XDG): ${CONFIG_FILE}"

# ─────────────────────────────────────────────────────────────────────
# 1) 渲染 EvoScientist config.yaml
# ─────────────────────────────────────────────────────────────────────
# 用 Python 生成 config.yaml 避免手工 YAML 转义（value 里出现 :、# 等特殊
# 字符会踩坑）。字段严格对齐 EvoScientistConfig dataclass（settings.py）：
#   - provider="custom-openai"
#   - model=<纯 model id，例如 ep-xxxx>
#   - custom_openai_api_key / custom_openai_base_url
#   - reasoning_effort、auto_mode（stream-json 默认会开，显式写更稳）
#   - enable_async_subagents=False（M1 baseline 不用 langgraph dev sidecar）
python3 <<PYEOF
import os
import yaml

config = {
    "provider": "custom-openai",
    "model": os.environ.get("MODEL_NAME", ""),
    "custom_openai_api_key": os.environ.get("WORK_OPENAI_API_KEY", ""),
    "custom_openai_base_url": os.environ.get(
        "WORK_OPENAI_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"
    ),
    "reasoning_effort": os.environ.get("REASONING_EFFORT", "high"),
    "auto_mode": True,
    "auto_approve": True,
    "enable_ask_user": False,
    "enable_async_subagents": False,
    "show_thinking": True,
    "log_level": "info",
    "ui_backend": "cli",
    # 兜底 default_workdir，防止 EVOSCIENTIST_WORKSPACE_DIR ENV 被子进程意外
    # 清掉后回落到 Path.cwd()——LIFT 挂载在 /workspace/task，父级 /workspace
    # 是官方 base image 的烘焙默认，若命中会导致 sub-agent 双嵌套写产物。
    "default_workdir": "/workspace/task",
}
config_file = "${CONFIG_FILE}"
with open(config_file, "w", encoding="utf-8") as f:
    yaml.safe_dump(config, f, sort_keys=False, allow_unicode=True)
print(f"Rendered {config_file}")
print("  model      :", config["model"])
print("  provider   :", config["provider"])
print("  base_url   :", config["custom_openai_base_url"])
print("  reasoning  :", config["reasoning_effort"])
PYEOF

# 用 EvoScientist 侧 load_config() 交叉验证—— provider/model/key 必须匹配写入值。
python3 <<'PYEOF_VERIFY'
from EvoScientist.config.settings import load_config
c = load_config()
assert c.provider == "custom-openai", f"provider mismatch: {c.provider!r}"
assert bool(c.custom_openai_api_key), "custom_openai_api_key empty after load"
assert bool(c.model), "model empty after load"
print(f"  load_config OK: provider={c.provider} model={c.model} key_len={len(c.custom_openai_api_key)}")
PYEOF_VERIFY

# ─────────────────────────────────────────────────────────────────────
# 2) 注册 Firecrawl MCP server
# ─────────────────────────────────────────────────────────────────────
# EvoScientist 通过 MCP 接外部工具（不像 OpenClaw 的原生插件系统）。firecrawl-mcp
# 是 Mendable 官方 MCP flavor，通过 `npx -y firecrawl-mcp` 启动 stdio subprocess，
# 从环境变量读 FIRECRAWL_API_KEY 完成鉴权。
#
# 暴露给 main + research-agent：auto-mode 单 shot 时，如果 top-level 决定用
# research-agent 处理联网任务，也能命中此 MCP。
#
# `--env-ref FIRECRAWL_API_KEY` 会把 key 写成 `${FIRECRAWL_API_KEY}` 占位符,
# runtime 期 (每次 EvoSci 启动 MCP subprocess) 从进程环境读取; 因此 build 期
# 不需要真实 key。
if EvoSci mcp list 2>/dev/null | grep -q "firecrawl-search"; then
  EvoSci mcp remove firecrawl-search 2>/dev/null || true
fi

EvoSci mcp add firecrawl-search npx \
  --env-ref FIRECRAWL_API_KEY \
  -e main,research-agent \
  -- -y firecrawl-mcp

# 交叉验证：mcp.yaml 应含 firecrawl-search 条目
python3 <<'PYEOF_VERIFY_MCP'
from pathlib import Path
import os, yaml
xdg = os.environ.get("XDG_CONFIG_HOME", "")
if xdg:
    cfg = Path(xdg) / "evoscientist" / "mcp.yaml"
else:
    cfg = Path.home() / ".config" / "evoscientist" / "mcp.yaml"
assert cfg.exists(), f"mcp.yaml not created at {cfg}"
data = yaml.safe_load(cfg.read_text()) or {}
assert "firecrawl-search" in str(data), f"firecrawl-search missing in {cfg}: {data}"
print(f"  mcp.yaml OK at {cfg}, firecrawl-search registered")
PYEOF_VERIFY_MCP

# ─────────────────────────────────────────────────────────────────────
# 3) 部署 langfuse tracing overlay
#
# EvoScientist 没有 GA 那样的 plugin hook 系统，事件流由
# ``EvoScientist.stream.events.stream_agent_events`` 生成。
# LIFT 通过 monkey-patch 该 async generator，在每轮 turn 结束时把
# aggregated transcript + usage 写成一条 name=``evoscientist-plugin`` 的
# Langfuse trace（session_id / tags 从 LIFT env 读）。
#
# 另外附带 patch langchain-openai ``BaseChatOpenAI._astream`` +
# ``_should_stream_usage``：强制打开 ``stream_options.include_usage=True``，
# 让 judge / 短 turn 也能吐出 5 字段 usage_metadata（详见 README§Token 5 字段）。
#
# 具体实现见同目录 langfuse_tracing_overlay.py，通过 sitecustomize.py
# 全局注入到 site-packages。
# ─────────────────────────────────────────────────────────────────────
OVERLAY_SRC="/opt/lift/evoscientist_langfuse_overlay.py"
if [[ ! -f "${OVERLAY_SRC}" ]]; then
  echo "ERROR: overlay source ${OVERLAY_SRC} not found" >&2
  exit 1
fi

SITE_PACKAGES="$(python3 -c 'import site; print(site.getsitepackages()[0])')"
echo "Site-packages: ${SITE_PACKAGES}"

# 落到 site-packages 让 sitecustomize.py 能 import
cp "${OVERLAY_SRC}" "${SITE_PACKAGES}/lift_evoscientist_overlay.py"

# sitecustomize.py 每次 Python 进程启动都会被 site.py 自动 import。
# 若已存在，则幂等追加；否则新建。
SITECUSTOMIZE="${SITE_PACKAGES}/sitecustomize.py"
INJECT_STANZA=$'\n# LIFT — auto-load EvoScientist Langfuse overlay in every Python process\ntry:\n    import lift_evoscientist_overlay  # noqa: F401\nexcept Exception as _exc:  # never let overlay break EvoSci startup\n    import sys\n    print(f"[lift_evoscientist_overlay] load skipped: {_exc}", file=sys.stderr)\n'

if [[ -f "${SITECUSTOMIZE}" ]]; then
  if ! grep -q "lift_evoscientist_overlay" "${SITECUSTOMIZE}"; then
    printf "%s" "${INJECT_STANZA}" >> "${SITECUSTOMIZE}"
  fi
else
  printf "%s" "${INJECT_STANZA}" > "${SITECUSTOMIZE}"
fi

echo "Installed EvoScientist langfuse overlay via sitecustomize:"
echo "  overlay      : ${SITE_PACKAGES}/lift_evoscientist_overlay.py"
echo "  sitecustomize: ${SITECUSTOMIZE}"

# Sanity: overlay import + EvoScientist stream module accessible
python3 -c "import lift_evoscientist_overlay; print('overlay OK,', getattr(lift_evoscientist_overlay, '__version__', 'unversioned'))"
python3 -c "from EvoScientist.stream import events; print('EvoScientist.stream.events OK')"

echo "EvoScientist config + Langfuse overlay installed successfully."
