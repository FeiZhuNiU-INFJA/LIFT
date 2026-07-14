"""容器内 Langfuse endpoint 复写工具。

宿主机 ``.env`` 中的 ``LANGFUSE_BASE_URL`` 常配 ``http://localhost:PORT``；
容器内进程要访问宿主 Langfuse 需把 host 段改写为 ``host.docker.internal``
（``ContainerSession`` 已通过 ``--add-host host.docker.internal:host-gateway``
把这个名字映射到宿主机 gateway）。scheme / 端口 / 路径原样保留。
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

CONTAINER_LANGFUSE_HOST = "host.docker.internal"

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1"})


def rewrite_langfuse_base_url_for_container(raw: str | None) -> str | None:
    """把 ``localhost`` / ``127.0.0.1`` 的 Langfuse URL host 段改写为
    ``host.docker.internal``，保留 scheme / 端口 / 路径 / query / fragment。

    - 空 / 全空白输入 → 返回 ``None``（由调用方决定是否 fallback）
    - 非 loopback host（例如生产环境的真实域名）→ 原样返回（strip 后）
    """
    if not raw or not raw.strip():
        return None
    stripped = raw.strip()
    parts = urlsplit(stripped)
    host = (parts.hostname or "").lower()
    if host not in _LOOPBACK_HOSTS:
        return stripped
    port = parts.port
    netloc = CONTAINER_LANGFUSE_HOST if port is None else f"{CONTAINER_LANGFUSE_HOST}:{port}"
    return urlunsplit((parts.scheme or "http", netloc, parts.path, parts.query, parts.fragment))
