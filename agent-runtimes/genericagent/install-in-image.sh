#!/usr/bin/env bash
# Run inside Docker build (after `git clone` + dep install).
# 1) Render mykey.py.template into /opt/GenericAgent/mykey.py
# 2) Overlay /opt/GenericAgent/plugins/langfuse_tracing.py with our LIFT-aware version
set -euo pipefail

GA_DIR="/opt/GenericAgent"

# 1) Render mykey.py.template -> mykey.py
escape_sed() {
  printf '%s' "${1:-}" | sed -e 's/[\/&]/\\&/g' -e ':a;N;$!ba;s/\n/\\n/g'
}

WORK_OPENAI_API_KEY_ESC="$(escape_sed "${WORK_OPENAI_API_KEY:-}")"
WORK_OPENAI_BASE_URL_ESC="$(escape_sed "${WORK_OPENAI_BASE_URL:-https://ark.cn-beijing.volces.com/api/v3}")"
MODEL_NAME_ESC="$(escape_sed "${MODEL_NAME:-}")"
LANGFUSE_PUBLIC_KEY_ESC="$(escape_sed "${LANGFUSE_PUBLIC_KEY:-}")"
LANGFUSE_SECRET_KEY_ESC="$(escape_sed "${LANGFUSE_SECRET_KEY:-}")"
LANGFUSE_HOST_ESC="$(escape_sed "${LANGFUSE_HOST:-http://host.docker.internal:3000}")"
FIRECRAWL_API_KEY_ESC="$(escape_sed "${FIRECRAWL_API_KEY:-}")"
# LIFT 约定：全 runtime 统一以 REASONING_EFFORT 环境变量控制 seed 模型思维链强度。
# GA ``llmcore.py`` 会把 ``reasoning_effort`` 顶层透传到 OpenAI 兼容请求体，Ark
# doubao-seed 端点已实测接受该字段；未显式设置则默认 high 与 OpenClaw / Hermes 对齐。
REASONING_EFFORT_ESC="$(escape_sed "${REASONING_EFFORT:-high}")"

sed \
  -e "s/__WORK_OPENAI_API_KEY__/${WORK_OPENAI_API_KEY_ESC}/g" \
  -e "s/__WORK_OPENAI_BASE_URL__/${WORK_OPENAI_BASE_URL_ESC}/g" \
  -e "s/__MODEL_NAME__/${MODEL_NAME_ESC}/g" \
  -e "s/__LANGFUSE_PUBLIC_KEY__/${LANGFUSE_PUBLIC_KEY_ESC}/g" \
  -e "s/__LANGFUSE_SECRET_KEY__/${LANGFUSE_SECRET_KEY_ESC}/g" \
  -e "s/__LANGFUSE_HOST__/${LANGFUSE_HOST_ESC}/g" \
  -e "s/__FIRECRAWL_API_KEY__/${FIRECRAWL_API_KEY_ESC}/g" \
  -e "s/__REASONING_EFFORT__/${REASONING_EFFORT_ESC}/g" \
  /tmp/mykey.py.template > "${GA_DIR}/mykey.py"

# 2) Overlay langfuse_tracing.py — strict overwrite. GA 自带版本可能在不同 ref 下漂移，
#    我们直接用 LIFT 自带版本固化 trace name + session_id + tags 行为。
mkdir -p "${GA_DIR}/plugins"
if [[ -f "${GA_DIR}/plugins/langfuse_tracing.py" ]]; then
  cp -a "${GA_DIR}/plugins/langfuse_tracing.py" "${GA_DIR}/plugins/langfuse_tracing.py.upstream.bak"
fi
cp /tmp/langfuse_tracing_overlay.py "${GA_DIR}/plugins/langfuse_tracing.py"

# 3) Patch GA 让工具 cwd 指向 LIFT 容器内 task 目录 /workspace/task
#    GA 上游把 ``GenericAgentHandler.cwd`` 与 system prompt 里的 ``cwd = ...``
#    都硬编码成 ``os.path.join(script_dir, 'temp')`` ，导致 LLM 的 ``code_run`` /
#    ``file_read`` 工具默认看到的是 GA 源码目录下 temp，看不到 LIFT 挂在
#    /workspace/task 的 task materials。这里直接改源码两处把根目录指向
#    /workspace/task：
#      a) agentmain.py:Handler 构造参数
#      b) ga.py:system prompt 里告知 LLM 的 cwd 字符串
#    GA 自身的 memory / model_responses / temp/<iodir> 还是用 ``script_dir``
#    绝对路径推导，与 Handler.cwd 无关，所以本 patch 不破坏 LIFT I/O 协议。
#    用 python 做 in-place 字符串替换，避免 sed 多重转义。
python - <<'PYEOF'
import re
import sys

GA_DIR = "/opt/GenericAgent"
LIFT_TASK_CWD = "/workspace/task"

def patch_file(path: str, old: str, new: str) -> None:
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    if old not in text:
        sys.stderr.write(f"ERROR: pattern not found in {path}\n  pattern: {old!r}\n")
        sys.exit(1)
    if text.count(old) > 1:
        sys.stderr.write(f"ERROR: pattern is not unique in {path}\n  pattern: {old!r}\n")
        sys.exit(1)
    text = text.replace(old, new, 1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"  patched: {path}")

# a) Handler.cwd → /workspace/task
patch_file(
    f"{GA_DIR}/agentmain.py",
    "GenericAgentHandler(self, self.history, os.path.join(script_dir, 'temp'))",
    f"GenericAgentHandler(self, self.history, '{LIFT_TASK_CWD}')",
)

# b) system prompt cwd 字符串 → /workspace/task
patch_file(
    f"{GA_DIR}/ga.py",
    "prompt += f'cwd = {os.path.join(script_dir, \"temp\")} (./)\\n'",
    f"prompt += f'cwd = {LIFT_TASK_CWD} (./)\\n'",
)

# c) memory 路径全部改为绝对路径 /opt/GenericAgent/memory
#    背景：GA 引擎读 memory 用 script_dir 拼绝对路径（进 delta 镜像 FS 层），
#    但系统提示告诉 LLM ``cwd = /workspace/task`` + ``[Memory] (../memory)``；
#    LLM 会把 ``../memory`` 解释为 ``/workspace/memory``（不在 bind mount 内，
#    也不在容器 FS 层被 docker commit），或把 ``memory/xxx`` 解释为
#    ``/workspace/task/memory/xxx``（在 bind mount 内 → 不进 delta）。
#    三点错位导致进化产物无法持久化。这里把 LLM 侧看到的 memory 路径也统一
#    锚定到 ``/opt/GenericAgent/memory``，让读、写、docker commit 都指向同一位置。
patch_file(
    f"{GA_DIR}/ga.py",
    "path = './memory/memory_management_sop.md'",
    f"path = '{GA_DIR}/memory/memory_management_sop.md'",
)
patch_file(
    f"{GA_DIR}/ga.py",
    'prompt += f"\\n[Memory] (../memory)\\n"',
    f'prompt += f"\\n[Memory] ({GA_DIR}/memory)\\n"',
)
patch_file(
    f"{GA_DIR}/ga.py",
    "prompt += structure + '\\n../memory/global_mem_insight.txt:\\n'",
    f"prompt += structure + '\\n{GA_DIR}/memory/global_mem_insight.txt:\\n'",
)
PYEOF

# 4) Install firecrawl plugin + register tools schema
#    GA 通过 ``plugins.hooks.discover_and_load`` 自动 import 所有 plugins/*.py，
#    本插件 import 时直接 monkey-patch ``GenericAgentHandler`` 加 ``do_firecrawl_*``。
#    LLM 侧需要 ``assets/tools_schema*.json`` 同步声明，否则 LLM 看不到工具。
cp /tmp/firecrawl_plugin.py "${GA_DIR}/plugins/firecrawl_plugin.py"

python - <<'PYEOF'
import json
import os

GA_DIR = "/opt/GenericAgent"

# 工具 schema：英文、中文两套，与 GA 加载逻辑（agentmain.py:load_tool_schema(suffix)）对齐。
SCHEMAS = {
    "tools_schema.json": [
        {
            "type": "function",
            "function": {
                "name": "firecrawl_search",
                "description": (
                    "Web search via Firecrawl Cloud API. Returns a list of "
                    "results with title/url/description. Use whenever you need "
                    "fresh information from the internet. Prefer this over "
                    "web_scan/web_execute_js (no browser inside the eval container)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"},
                        "limit": {"type": "integer", "description": "Max results", "default": 5},
                        "lang": {"type": "string", "description": "ISO language code, e.g. zh / en", "default": "zh"},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "firecrawl_scrape",
                "description": (
                    "Fetch a single URL via Firecrawl and return the page main "
                    "content as markdown. Combine with firecrawl_search: search "
                    "first, then scrape the most relevant URL for full text."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Target page URL (http/https)"},
                        "only_main_content": {
                            "type": "boolean",
                            "description": "Strip nav/footer/sidebar; keep article body only",
                            "default": True,
                        },
                    },
                    "required": ["url"],
                },
            },
        },
    ],
    "tools_schema_cn.json": [
        {
            "type": "function",
            "function": {
                "name": "firecrawl_search",
                "description": (
                    "通过 Firecrawl 云 API 进行联网搜索，返回结果列表（标题/URL/摘要）。"
                    "需要联网获取最新信息时使用；评测容器内无浏览器，"
                    "应优先于 web_scan / web_execute_js。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索关键词"},
                        "limit": {"type": "integer", "description": "返回结果数", "default": 5},
                        "lang": {"type": "string", "description": "ISO 语言代码，可选 zh / en", "default": "zh"},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "firecrawl_scrape",
                "description": (
                    "通过 Firecrawl 抓取单个 URL，返回 markdown 正文。"
                    "通常先用 firecrawl_search 找到目标页，再用本工具读全文。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "目标页面 URL（http/https）"},
                        "only_main_content": {
                            "type": "boolean",
                            "description": "是否仅保留正文（去除导航/页脚/侧栏）",
                            "default": True,
                        },
                    },
                    "required": ["url"],
                },
            },
        },
    ],
}

for fname, new_entries in SCHEMAS.items():
    path = os.path.join(GA_DIR, "assets", fname)
    with open(path, "r", encoding="utf-8") as f:
        tools = json.load(f)
    existing = {t.get("function", {}).get("name") for t in tools if isinstance(t, dict)}
    for entry in new_entries:
        name = entry["function"]["name"]
        if name in existing:
            print(f"  skip (already present): {name} in {fname}")
            continue
        tools.append(entry)
        print(f"  appended: {name} -> {fname}")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(tools, f, ensure_ascii=False, indent=2)
        f.write("\n")
PYEOF

# 5) Sanity check: agentmain.py importable
cd "${GA_DIR}"
python -c "import sys; sys.path.insert(0, '${GA_DIR}'); import agentmain" || {
  echo "WARN: agentmain import failed during build; check GA upstream compatibility." >&2
  # 不退出失败：build 期可能因缺少 mykey 字段 / 网络资源而 import 失败，运行期再验证。
}

echo "GenericAgent baked at ${GA_DIR}; mykey.py + langfuse_tracing overlay applied."
