"""OpenClaw warmup 后 evolve 钩子：``openclaw learn review``。"""

from __future__ import annotations

import json
import shlex

from src.config import LOGGER
from src.lift.adapters.container.exec import docker_exec_shell_async
from src.lift.adapters.openclaw.container_exec import (
    OpenClawContainerContext,
    exec_openclaw_async,
)


# 容器内 self-evolving-plugin-pro 写状态的位置（与 src/runtimeManager.js 对齐）
_RUNTIME_STATE_PATH = "/root/.openclaw/evolution-runtime/runtime-state.json"
_PLUGIN_SERVICE_ENDPOINT = "http://127.0.0.1:18090"


# self-evolving-plugin-pro `/instances/onboard` 要求 workspace_root 是 git repo
# （git_root == workspace_root）且至少有一个 HEAD commit；agent workspace 是镜像内
# ``/root/.openclaw/workspace``（参与 docker commit，承载 SOUL/memory 等持久态），
# 没初始化过，需要在调用 plugin 任何 ensureReady 入口（包括 bootstrap 走的
# ``learn status``）之前 git init + 一次空 commit，否则 onboard 会被 plugin 拒为 400。
_PREPARE_WORKSPACE_GIT_SCRIPT = """
mkdir -p /root/.openclaw/workspace
git config --global --add safe.directory /root/.openclaw/workspace
git config --global user.email "lift@local"
git config --global user.name "lift"
if [[ ! -d /root/.openclaw/workspace/.git ]]; then
  git -C /root/.openclaw/workspace init -q
  git -C /root/.openclaw/workspace add -A
  git -C /root/.openclaw/workspace commit -q --allow-empty -m "lift: warmup baseline"
fi
""".strip()


async def bootstrap_evolution_runtime(container: OpenClawContainerContext) -> None:
    """触发 self-evolving-plugin-pro 的 onboarding，使 ``runtime-state.json`` 落盘。

    plugin 写 ``runtime-state.json``（含 ``instanceId`` / ``instanceToken``）的唯一
    路径是 ``bootstrapInstance()``，而 LIFT 用 ``openclaw agent --local`` 单次 CLI
    驱动 work agent，并不会走 plugin service start 也不会触发 ``session_start``
    hook——所以前面的 ``post_signal_via_container`` 全部因
    ``runtime-state.json missing`` 短路 ``exit 0``，signal 没有真正写入插件后端。

    ``openclaw learn status`` 的 CLI handler 内部 ``await ensureReady()``，会跑通
    ``bootstrapInstance → /instances/onboard → saveRuntimeState``。warmup 容器起
    好后立刻调一次即可让后续每题的 ``post_signal`` 命中真实的 ``/signals`` 路由。

    onboard 要求 workspace_root 是 git repo 且 HEAD 有 commit，所以这里先跑
    ``_PREPARE_WORKSPACE_GIT_SCRIPT`` 把 ``/root/.openclaw/workspace`` 初始化成
    git（``learn review`` 路径里也跑一次相同脚本，幂等）。

    失败仅 ``LOGGER.warning``——bootstrap 是评测旁路，不能拖垮 warmup。
    """
    try:
        await docker_exec_shell_async(
            container.container_name, _PREPARE_WORKSPACE_GIT_SCRIPT
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning(
            "evolution runtime bootstrap (git init) failed (%s): %s",
            container.container_name, exc,
        )
        return
    try:
        out = await exec_openclaw_async(
            container, ["learn", "status"], timeout_seconds=120.0
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning(
            "evolution runtime bootstrap (learn status) failed (%s): %s",
            container.container_name, exc,
        )
        return
    if out.strip():
        LOGGER.info(
            "evolution runtime bootstrapped (%s):\n%s",
            container.container_name, out.strip(),
        )


async def post_signal_via_container(
    container: OpenClawContainerContext,
    *,
    session_id: str,
    kind: str,
    content: str,
    trust: float,
    tags: list[str] | None = None,
    source: str = "lift_eval_critique",
    classifier_note: str = "lift posted signal on behalf of agent",
    raise_on_error: bool = False,
) -> None:
    """在容器内 ``curl POST /signals``，把一条 signal 直接写入 self-evolving-plugin-pro 后端。

    self-evolving-plugin-pro 的 signal 协议要求 agent 自己用 exec 工具调 curl，但
    LIFT 评测语境里 work agent 把 critique 当工单不当反馈、并不会执行 curl。这里
    LIFT 直接代它发，绕开 agent 自上报通道，让 ``openclaw learn review`` 阶段能
    通过 SignalRecord 选出对应 session 进入 review。

    ``instance_id`` / ``instance_token`` 从容器内
    ``/root/.openclaw/evolution-runtime/runtime-state.json`` 读取——这是
    ``self-evolving-plugin`` 自身落地的权威源。

    ``raise_on_error=True`` 时将容器执行失败 / 非 2xx HTTP / runtime state 缺失
    抛给上层 retry 包装；默认仍只 ``LOGGER.warning``，保持旁路调用的宽松语义。
    """
    payload = {
        "session_id": session_id,
        "user_id": "lift",
        "kind": kind,
        "content": content,
        "tags": tags or ["lift_eval", kind],
        "trust": float(trust),
        "source": source,
        "classifier_note": classifier_note,
    }
    payload_json = json.dumps(payload, ensure_ascii=False)
    strict = "1" if raise_on_error else "0"
    # 用 python 解析 runtime-state.json + 注入 instance_id（容器里不一定装了 jq；
    # plugin runtime 自带 python，更稳）。``payload_json`` / ``endpoint`` /
    # ``state_path`` 经 ``shlex.quote`` 安全嵌入 bash。
    script = f"""
STATE_FILE={shlex.quote(_RUNTIME_STATE_PATH)}
ENDPOINT={shlex.quote(_PLUGIN_SERVICE_ENDPOINT)}
PAYLOAD_BASE={shlex.quote(payload_json)}
STRICT={strict}
if [[ ! -f "${{STATE_FILE}}" ]]; then
  echo "post_signal: runtime-state.json missing at ${{STATE_FILE}}"
  [[ "${{STRICT}}" == "1" ]] && exit 1 || exit 0
fi
read -r INSTANCE_ID INSTANCE_TOKEN < <(python3 - "${{STATE_FILE}}" <<'PY'
import json, sys
try:
    s = json.load(open(sys.argv[1]))
    print((s.get("instanceId") or "").strip(), (s.get("instanceToken") or "").strip())
except Exception:
    print("", "")
PY
)
if [[ -z "${{INSTANCE_ID}}" || -z "${{INSTANCE_TOKEN}}" ]]; then
  echo "post_signal: instance_id/token not yet provisioned"
  [[ "${{STRICT}}" == "1" ]] && exit 1 || exit 0
fi
PAYLOAD=$(IID="${{INSTANCE_ID}}" PAYLOAD_BASE="${{PAYLOAD_BASE}}" python3 - <<'PY'
import json, os
data = json.loads(os.environ["PAYLOAD_BASE"])
data["instance_id"] = os.environ["IID"]
print(json.dumps(data, ensure_ascii=False))
PY
)
if [[ -z "${{PAYLOAD}}" ]]; then
  echo "post_signal: failed to inject instance_id into payload"
  [[ "${{STRICT}}" == "1" ]] && exit 1 || exit 0
fi
HTTP_CODE=$(curl -sS -o /tmp/_lift_signal_resp.txt -w '%{{http_code}}' \\
  -X POST "${{ENDPOINT}}/signals" \\
  -H "Content-Type: application/json" \\
  -H "x-openclaw-instance-id: ${{INSTANCE_ID}}" \\
  -H "x-openclaw-instance-token: ${{INSTANCE_TOKEN}}" \\
  -d "${{PAYLOAD}}" 2>&1 || true)
echo "post_signal: http_code=${{HTTP_CODE}}"
cat /tmp/_lift_signal_resp.txt 2>/dev/null || true
echo
if [[ ! "${{HTTP_CODE}}" =~ ^2 ]]; then
  echo "post_signal: non-2xx response"
  [[ "${{STRICT}}" == "1" ]] && exit 1 || true
fi
""".strip()
    try:
        out = await docker_exec_shell_async(container.container_name, script)
    except Exception as exc:  # noqa: BLE001 — signal 上报是 warmup 旁路
        LOGGER.warning(
            "post_signal failed (%s, kind=%s, session=%s): %s",
            container.container_name, kind, session_id, exc,
        )
        if raise_on_error:
            raise
        return
    LOGGER.info(
        "post_signal (%s, kind=%s, session=%s, trust=%.2f):\n%s",
        container.container_name, kind, session_id, trust, out.strip(),
    )


async def openclaw_learn_review(container: OpenClawContainerContext) -> None:
    """warmup 题完成后在容器内执行 evolve（learn review + worker 配置）。

    所有 runtime env（``LANGFUSE_BASE_URL`` 等）已在 ``docker run`` 阶段写入容器
    ``Config.Env``，``docker exec`` 自动继承，因此这里不再显式 ``-e`` 注入。
    """
    # 预备：
    # 1. ``_PREPARE_WORKSPACE_GIT_SCRIPT``：onboard 要求的 git repo（幂等，
    #    bootstrap_evolution_runtime 已先跑过一次）。
    # 2. review worker 改 thinking=off：Ark 不支持 thinking=low，加速 warmup evolve。
    await docker_exec_shell_async(
        container.container_name,
        f"""
{_PREPARE_WORKSPACE_GIT_SCRIPT}
WORKER_JS="${{HOME}}/.openclaw/extensions/self-evolving-plugin-pro/src/review/worker.js"
if [[ -f "${{WORKER_JS}}" ]]; then
  sed -i 's/"--thinking", "low"/"--thinking", "off"/g' "${{WORKER_JS}}" || true
fi
""".strip(),
    )

    try:
        review_stdout = await exec_openclaw_async(container, ["learn", "review"])
    except Exception:
        LOGGER.exception("openclaw learn review failed (%s)", container.container_name)
        raise
    if review_stdout.strip():
        LOGGER.info(
            "openclaw learn review stdout (%s):\n%s",
            container.container_name,
            review_stdout.strip(),
        )
