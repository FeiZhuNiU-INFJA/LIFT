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
    """``docker run`` 时需要相对宿主机 ``.env`` **改写**的环境变量。

    其它 secret（``ARK_API_KEY`` / ``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY`` /
    ``FIRECRAWL_API_KEY`` 等）一律走 ``--env-file``，不在这里返回——避免 secret 重复
    出现在 ``docker run -e ...`` 命令行与日志里；这些值已经写入容器 ``Config.Env``，
    后续 ``docker exec`` 会自动继承，无需再次注入。
    """
    return {
        # 容器内 host.docker.internal 访问宿主机 Langfuse；宿主机 .env 通常配 localhost
        "LANGFUSE_BASE_URL": _normalize_langfuse_base_url(
            os.environ.get("LANGFUSE_BASE_URL")
        ),
    }

