#!/usr/bin/env bash
# OpenHuman ENTRYPOINT 包装脚本。
#
# 镜像 ENTRYPOINT 为 `tini -- <本脚本> <CMD>`（CMD 默认 openhuman-core run --host ... --port 7788）。
#
# 职责(顺序):
#   1) max_tokens 代理(base 镜像默认启用): 在 127.0.0.1:${LIFT_PROXY_PORT} 上起一个
#      透明反向代理,请求 body 缺 max_tokens 时按 endpoint 类型注入。openhuman-core
#      的 ``inference_url`` 已被 install-in-image.sh 改指向 http://127.0.0.1:${PORT}/v3,
#      流量必经此代理。上游真正的 base URL 通过 LIFT_PROXY_UPSTREAM 传入。
#      LIFT_MAX_TOKENS_PROXY_ENABLED=false 关闭(直连 upstream)。
#   2) agentmemory 变体(AGENTMEMORY_ENABLED=true): 先在容器内后台拉起 agentmemory server
#      (:3111,离线本地嵌入),等 :3111 就绪后再 exec openhuman-core。
#      为什么必须先起 server:OpenHuman 的 agentmemory backend 无自动回退 SQLite —— openhuman-core
#      启动时若 daemon 不可达,memory trait 调用会直接报错。
#   3) exec CMD(openhuman-core run ...)。
#
# base 镜像(AGENTMEMORY_ENABLED 非 true 且 max_tokens 代理默认已启)时依旧透明:代理挂 127.0.0.1
# 后台守护,ENTRYPOINT 主进程仍是 tini -> openhuman-core。
set -euo pipefail

_start_max_tokens_proxy() {
  local enabled="${LIFT_MAX_TOKENS_PROXY_ENABLED:-true}"
  if [[ "${enabled}" != "true" && "${enabled}" != "1" ]]; then
    echo "[openhuman-entrypoint] LIFT_MAX_TOKENS_PROXY_ENABLED=${enabled}: skip max_tokens proxy" >&2
    return 0
  fi
  local upstream="${LIFT_PROXY_UPSTREAM:-}"
  if [[ -z "${upstream}" ]]; then
    echo "[openhuman-entrypoint] LIFT_PROXY_UPSTREAM not set; skip max_tokens proxy" >&2
    return 0
  fi
  local port="${LIFT_PROXY_PORT:-7787}"
  local script="/opt/lift/max_tokens_proxy.py"
  if [[ ! -f "${script}" ]]; then
    echo "[openhuman-entrypoint] WARN: ${script} missing; skip max_tokens proxy" >&2
    return 0
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    echo "[openhuman-entrypoint] WARN: python3 missing; skip max_tokens proxy" >&2
    return 0
  fi
  local log="/workspace/task/max-tokens-proxy.log"
  mkdir -p /workspace/task 2>/dev/null || true
  echo "[openhuman-entrypoint] starting max_tokens proxy on 127.0.0.1:${port} -> ${upstream} (MAX_TOKENS=${MAX_TOKENS:-51200})"
  ( python3 "${script}" >"${log}" 2>&1 & ) || {
    echo "[openhuman-entrypoint] WARN: failed to spawn max_tokens proxy" >&2
    return 0
  }
  local ready="false"
  local i
  for i in $(seq 1 30); do
    # 用 TCP 连通性探活:代理不实现 /healthz;真正的 HTTP 请求会转发到上游,
    # 探活时不能真发到 ARK 触发计费。用 bash /dev/tcp 直接 3-way handshake。
    if (echo > "/dev/tcp/127.0.0.1/${port}") 2>/dev/null; then
      ready="true"
      break
    fi
    sleep 0.2
  done
  if [[ "${ready}" == "true" ]]; then
    echo "[openhuman-entrypoint] max_tokens proxy ready on :${port}"
  else
    echo "[openhuman-entrypoint] WARN: max_tokens proxy did not become ready in time; openhuman-core may fail to reach inference_url" >&2
    cat "${log}" 2>/dev/null | tail -20 || true
  fi
}

_start_agentmemory() {
  if [[ "${AGENTMEMORY_ENABLED:-false}" != "true" ]]; then
    return 0
  fi
  export CI=1
  export HOME="${HOME:-/root}"
  local _am_log="/workspace/task/agentmemory-server.log"
  mkdir -p /workspace/task 2>/dev/null || true
  if ! command -v agentmemory >/dev/null 2>&1; then
    echo "[openhuman-entrypoint] WARN: AGENTMEMORY_ENABLED=true but 'agentmemory' not on PATH; skipping server start." >&2
    return 0
  fi
  echo "[openhuman-entrypoint] starting agentmemory server on :3111 ..."
  ( agentmemory >"${_am_log}" 2>&1 & ) || echo "[openhuman-entrypoint] WARN: failed to spawn agentmemory server" >&2
  local _ready="false"
  local _i
  for _i in $(seq 1 60); do
    if curl -fsS http://localhost:3111/agentmemory/livez >/dev/null 2>&1 \
       || curl -fsS http://localhost:3111/agentmemory/health >/dev/null 2>&1; then
      _ready="true"; break
    fi
    sleep 1
  done
  if [[ "${_ready}" == "true" ]]; then
    echo "[openhuman-entrypoint] agentmemory server ready on :3111"
  else
    # backend 无回退,server 未就绪则 openhuman-core 的 memory 调用会失败。仍继续 exec,
    # 让 openhuman-core 自身的错误信息暴露问题,而不是在 wrapper 里静默悬挂。
    echo "[openhuman-entrypoint] WARN: agentmemory server not ready within timeout; continuing (OpenHuman memory ops may fail)." >&2
    cat "${_am_log}" 2>/dev/null || true
  fi
}

_start_max_tokens_proxy
_start_agentmemory

exec "$@"
