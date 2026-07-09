"""OpenHuman 容器最小上下文（包含 HTTP JSON-RPC 端点）。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OpenHumanContainerContext:
    """OpenHuman 容器 chat 所需的最小坐标。

    ``container_name`` 用于 ``docker exec`` 兜底诊断（读容器日志 / ready marker）；
    ``rpc_endpoint`` 是宿主机可达的 HTTP URL（如 ``http://127.0.0.1:32871``），
    LIFT 通过 ``POST {rpc_endpoint}/rpc`` 调 ``agent.chat``。端口在 ``docker run``
    时用 ``-p 0:7788`` 由 Docker 分配，宿主侧解析后写入本字段。
    ``rpc_token`` 是 openhuman-core 要求的 ``Authorization: Bearer <token>``
    值（``OPENHUMAN_CORE_TOKEN``）；由启动方生成并同时通过 ``-e`` 注入容器与
    保留在 adapter 侧使用。
    """

    container_name: str
    rpc_endpoint: str
    rpc_token: str
