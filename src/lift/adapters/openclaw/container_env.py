"""OpenClaw 容器运行时环境变量与 bind mount 权限回收脚本。"""

from __future__ import annotations

import os

CONTAINER_LANGFUSE_BASE_URL = "http://host.docker.internal:3000"  # 容器内访问宿主机 Langfuse


def _normalize_langfuse_base_url(raw: str | None) -> str:
    """将 localhost Langfuse URL 映射为 ``host.docker.internal``。"""
    if not raw or not raw.strip():
        return CONTAINER_LANGFUSE_BASE_URL
    lowered = raw.strip().lower()
    if "127.0.0.1" in lowered or "localhost" in lowered:
        return CONTAINER_LANGFUSE_BASE_URL
    return raw.strip()


def host_user_ids() -> tuple[int, int]:
    """宿主机 uid/gid，用于容器销毁后 reclaim bind mount 文件所有权。"""
    import os

    return os.getuid(), os.getgid()


def container_reclaim_ownership_script(uid: int, gid: int) -> str:
    """在容器内以 root 执行，将 volume 目录 chown 回宿主机用户。"""
    return f"""
for d in /workspace/task /workspace/outcome; do
  if [[ -d "$d" ]]; then
    chown -R {uid}:{gid} "$d" 2>/dev/null || true
  fi
done
""".strip()


def container_runtime_env() -> dict[str, str]:
    """传入 OpenClaw 容器的环境变量（覆盖/补充 repo ``.env``）。"""
    env: dict[str, str] = {}
    ark_key = os.environ.get("ARK_API_KEY")
    if ark_key:
        env["ARK_API_KEY"] = ark_key
    # 工具/插件凭据：宿主机环境若有则透传给容器（OpenClaw 启动时按 env 自动选 web_search /
    # web_fetch provider，无需写 openclaw.json）。
    for key in (
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "FIRECRAWL_API_KEY",
    ):
        val = os.environ.get(key)
        if val:
            env[key] = val
    env["LANGFUSE_BASE_URL"] = _normalize_langfuse_base_url(
        os.environ.get("LANGFUSE_BASE_URL")
    )
    return env
