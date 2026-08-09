"""agentmemory active-evolve 的两个原语：LLM provider 点火 env + warmup 后蒸馏触发。

**点火（ignition）**：agentmemory server 在 boot 时用 ``loadConfig()`` 一次性构造
provider——没有任何 LLM key 时落到零-LLM ``NoopProvider``，``consolidate`` /
``reflect`` 会被 ``isConsolidationEnabled()`` 门控为 no-op。因此必须在
``agentmemory`` 进程 **启动前** 就让 ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL`` /
``OPENAI_MODEL`` 出现在容器 env 里（prelaunch 子 shell 继承容器 env）。我们复用
被测 agent 同源的 ``WORK_OPENAI_*``，使记忆蒸馏与主链路 token 口径统一。

**蒸馏（distill）**：warmup 全部题跑完、``docker commit`` 之前，在容器内 curl
本地 :3111，按 agentmemory 自带 session-end hook 的规范序列触发：
``POST /agentmemory/crystals/auto {olderThanDays:0}`` 后
``POST /agentmemory/consolidate-pipeline {tier:"all",force:true}``（后者内部串起
semantic-merge → reflect → procedural 三层）。没有配置 ``AGENTMEMORY_SECRET``，
路由无鉴权，裸 curl 即可。
"""

from __future__ import annotations

import os

from src.config import LOGGER
from src.lift.adapters.container.exec import docker_exec_async
from src.lift.adapters.openclaw.container_exec import OpenClawContainerContext

# 容器内 agentmemory server 的固定监听地址（prelaunch 脚本以 :3111 起）。
_AGENTMEMORY_URL = "http://localhost:3111"

# consolidate-pipeline 内部要走 LLM（semantic-merge + reflect + procedural），
# 每层可能各发一次 provider 调用。给足宿主侧 wall-clock，避免正常蒸馏被误杀。
_DISTILL_TIMEOUT_SECONDS = 600.0


def build_ignition_env() -> dict[str, str]:
    """把宿主机 ``WORK_OPENAI_*`` / ``MODEL_NAME`` 映射成 agentmemory 认的 OpenAI
    provider 点火 env。

    - ``OPENAI_API_KEY`` ← ``WORK_OPENAI_API_KEY``：与被测 agent 同 key，token 口径统一。
    - ``OPENAI_BASE_URL`` ← ``WORK_OPENAI_BASE_URL``：agentmemory 的 ``appendOpenAIRoute``
      会在已含 path 的 base（如 ARK ``.../api/v3``）后直接补 ``/chat/completions``，
      故无需手动拼 ``/v1``。
    - ``OPENAI_MODEL`` ← ``MODEL_NAME`` 去掉 ``custom/`` 前缀：agentmemory 默认
      ``gpt-4o-mini`` 在 ARK 等自建端点上不存在，**必须**显式指定真实 model id，
      否则蒸馏调用会 404。

    缺失关键变量时返回空 dict——调用方据此判定"点火不可用"，退回被动行为并 WARNING，
    绝不静默把没有 provider 的容器当作已点火（那会让 consolidate 全程 no-op）。
    """
    api_key = (os.environ.get("WORK_OPENAI_API_KEY") or "").strip()
    base_url = (os.environ.get("WORK_OPENAI_BASE_URL") or "").strip()
    model_name = (os.environ.get("MODEL_NAME") or "").strip()
    if not api_key or not base_url or not model_name:
        return {}
    # MODEL_NAME 约定为 ``provider/model_id``（provider 固定 custom）；agentmemory
    # 只认裸 model id，去掉第一个 "/" 之前的前缀。
    model_id = model_name.split("/", 1)[1] if "/" in model_name else model_name
    if not model_id:
        return {}
    env: dict[str, str] = {
        "OPENAI_API_KEY": api_key,
        "OPENAI_BASE_URL": base_url,
        "OPENAI_MODEL": model_id,
        # 显式打开 consolidation，语义与 hasLLMProviderConfigured 门控一致；
        # 即便未来 provider 探测逻辑变动，force+此开关也能保证蒸馏被执行。
        "CONSOLIDATION_ENABLED": "true",
    }
    # 与主链路 work/judge 的思考深度对齐（ARK doubao 端点原生支持 reasoning_effort）。
    reasoning = (os.environ.get("WORK_OPENAI_REASONING_EFFORT") or "").strip()
    if reasoning:
        env["OPENAI_REASONING_EFFORT"] = reasoning
    return env


async def agentmemory_distill(container: OpenClawContainerContext) -> None:
    """warmup 结束后在容器内触发一次 agentmemory 蒸馏（crystals + consolidate-pipeline）。

    序列与 agentmemory 自带 ``session-end`` hook 在 ``CONSOLIDATION_ENABLED=true``
    时的行为一致：先 auto-crystallize，再 tier=all 的 consolidate-pipeline（内部
    semantic-merge → reflect → procedural）。``force:true`` 绕过 enabled 门控，但
    provider 仍需为真正的 OpenAI provider（靠 ``build_ignition_env`` 点火保证），
    否则 ``provider.summarize`` 会抛错、pipeline 各 tier 记为 error。

    所有 HTTP 结果打进日志便于事后核账；非 2xx 或 curl 失败时抛
    ``RuntimeError`` 交给上层 ``_run_evolve_after_warmup_with_retry`` 重试。
    """
    # 用 python 解析每个响应的 http_code（容器内 agentmemory 镜像自带 python3），
    # 逐个 endpoint 断言 2xx；``consolidate-pipeline`` 的 body 里各 tier 的
    # skipped/error 会一起 echo 出来，供人工核对是否真的走了 LLM。
    script = f"""
set -o pipefail
AM_URL={_AGENTMEMORY_URL!r}
_post() {{
  local route="$1"; local payload="$2"
  local code
  code=$(curl -sS -o /tmp/_am_resp.json -w '%{{http_code}}' \\
    -X POST "${{AM_URL}}${{route}}" \\
    -H 'Content-Type: application/json' \\
    -d "${{payload}}" 2>/tmp/_am_err.txt || true)
  echo "[distill] POST ${{route}} -> http_code=${{code}}"
  cat /tmp/_am_resp.json 2>/dev/null || true
  echo
  if [[ ! "${{code}}" =~ ^2 ]]; then
    echo "[distill] non-2xx for ${{route}}; curl stderr:"
    cat /tmp/_am_err.txt 2>/dev/null || true
    return 1
  fi
}}
# 与 session-end hook 规范序列一致：先 crystallize，再 tier=all consolidate。
_post /agentmemory/crystals/auto '{{"olderThanDays":0}}'
_post /agentmemory/consolidate-pipeline '{{"tier":"all","force":true}}'
""".strip()
    try:
        out = await docker_exec_async(
            container.container_name,
            ["bash", "-lc", script],
            label="agentmemory distill",
            timeout_seconds=_DISTILL_TIMEOUT_SECONDS,
        )
    except Exception:
        LOGGER.exception(
            "agentmemory distill failed (%s)", container.container_name
        )
        raise
    if out.strip():
        LOGGER.info(
            "agentmemory distill (%s):\n%s",
            container.container_name,
            out.strip(),
        )
