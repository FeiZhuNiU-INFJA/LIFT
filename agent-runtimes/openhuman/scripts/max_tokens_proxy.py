#!/usr/bin/env python3
"""LIFT max_tokens 注入代理 —— 让 openhuman-core 走到实际 ARK / OpenAI-兼容
   endpoint 的 chat completions / responses 请求上带上 ``max_tokens``。

背景:
  openhuman-core (Rust binary) 的 JSON-RPC 只暴露 ``message`` / ``thread_id`` /
  ``model_override`` 等参数,config.toml 也没有 ``max_tokens`` 键;binary 内部
  构造 ``ChatCompletionRequest`` 时对 ``max_tokens`` 走 provider-router 硬编码
  (bin 内可见 ``chat() for model=X ignores max_tokens=Y — this provider does
  not``),外部无覆盖入口。上游默认 ``max_tokens=4096``,长产出被静默截断。

架构:
  openhuman-core --> http://127.0.0.1:${LIFT_PROXY_PORT}/v3  --> ARK ...

  代理在:
    - 请求侧: 读 JSON body,若是 chat/completions / responses / embeddings 请求
      且 body 缺 ``max_tokens`` / ``max_completion_tokens`` / ``max_output_tokens``,
      按 endpoint 类型注入。
    - 响应侧: 完全透明字节流转发,支持 SSE(chunked / stream=true)。

Env:
  LIFT_PROXY_LISTEN_HOST       监听 host(默认 127.0.0.1)
  LIFT_PROXY_PORT              监听端口(默认 7787)
  LIFT_PROXY_UPSTREAM          上游 base URL(如 https://ark.cn-beijing.volces.com/api/v3)
                               代理会把 ``/v3/...`` 后的 path 拼到该 URL 后面
  MAX_TOKENS                   注入值(默认 51200)
  LIFT_PROXY_DEBUG             1 时打印每次请求 patch 摘要

启动:
  python3 max_tokens_proxy.py
"""
from __future__ import annotations

import json
import logging
import os
import socket
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, urlunparse

LOG = logging.getLogger("lift_max_tokens_proxy")


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        LOG.warning("env %s=%r not an int; using default %d", name, raw, default)
        return default


LISTEN_HOST = os.environ.get("LIFT_PROXY_LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = _env_int("LIFT_PROXY_PORT", 7787)
UPSTREAM = (os.environ.get("LIFT_PROXY_UPSTREAM") or "").rstrip("/")
MAX_TOKENS = _env_int("MAX_TOKENS", 51200)
DEBUG = (os.environ.get("LIFT_PROXY_DEBUG") or "0").strip() == "1"

# 转发时剥掉的逐跳 hop-by-hop headers(RFC 7230 §6.1)+ Host / Content-Length。
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


def _should_inject(path: str) -> str | None:
    """返回 payload 语义类型: ``chat`` / ``responses`` / None(不动)。

    - ``.../chat/completions`` : chat completions,注入 ``max_tokens``
    - ``.../responses``        : responses API,注入 ``max_output_tokens``
    - 其它(embeddings / files / models 等)               : 不动
    """
    p = path.lower()
    if p.endswith("/chat/completions") or "/chat/completions?" in p:
        return "chat"
    if p.endswith("/responses") or "/responses?" in p:
        return "responses"
    return None


def _maybe_patch_body(body: bytes, kind: str) -> tuple[bytes, dict | None]:
    """按 endpoint kind 注入 max_tokens。返回 (new_body, patch_summary_or_None)。

    仅当 JSON 顶层是 dict 且缺任一 ``max_*tokens`` 字段时才补;显式配置一律尊重。
    非 JSON body 一律原样透传。
    """
    try:
        obj = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body, None
    if not isinstance(obj, dict):
        return body, None

    has_any = any(
        k in obj for k in ("max_tokens", "max_completion_tokens", "max_output_tokens")
    )
    if has_any:
        return body, None

    if kind == "chat":
        obj["max_tokens"] = MAX_TOKENS
        summary = {"injected": "max_tokens", "value": MAX_TOKENS}
    elif kind == "responses":
        obj["max_output_tokens"] = MAX_TOKENS
        summary = {"injected": "max_output_tokens", "value": MAX_TOKENS}
    else:
        return body, None

    new_body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    return new_body, summary


def _forward_url(request_path: str) -> str:
    """把入站 request path 拼到 UPSTREAM 后面。

    例: UPSTREAM=https://ark.cn-beijing.volces.com/api/v3, request=/v3/chat/completions
    => https://ark.cn-beijing.volces.com/api/v3/chat/completions

    我们的约定:openhuman-core config.toml ``inference_url`` 指向
    ``http://127.0.0.1:${LIFT_PROXY_PORT}/v3``,即入站 path 一律以 ``/v3`` 开头,
    代理去掉 ``/v3`` 前缀后拼到 UPSTREAM。若未以 ``/v3`` 开头则整段拼接(兜底)。
    """
    if not UPSTREAM:
        raise RuntimeError("LIFT_PROXY_UPSTREAM env is not set")
    parsed = urlparse(UPSTREAM)
    if not parsed.scheme or not parsed.netloc:
        raise RuntimeError(f"LIFT_PROXY_UPSTREAM not a full URL: {UPSTREAM!r}")

    # 保留 UPSTREAM 自带的 path (e.g. /api/v3),把 request_path 附加。
    upstream_path = parsed.path.rstrip("/")  # /api/v3

    inbound = request_path
    if inbound.startswith("/v3/"):
        inbound = inbound[len("/v3"):]  # 剩下 /chat/completions
    elif inbound == "/v3":
        inbound = ""

    joined = upstream_path + inbound
    return urlunparse(parsed._replace(path=joined))


class ProxyHandler(BaseHTTPRequestHandler):
    server_version = "LIFTMaxTokensProxy/1.0"

    # noqa: N802 —— overriding stdlib
    def log_message(self, fmt: str, *args) -> None:  # type: ignore[override]
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle("PATCH")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._handle("OPTIONS")

    def _handle(self, method: str) -> None:
        try:
            upstream_url = _forward_url(self.path)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("upstream url build failed: %r", exc)
            self._send_gateway_error(f"upstream url build failed: {exc}")
            return

        content_length = int(self.headers.get("Content-Length") or 0)
        raw_body = self.rfile.read(content_length) if content_length > 0 else b""

        kind = _should_inject(self.path)
        patch_summary: dict | None = None
        body = raw_body
        if kind is not None and raw_body:
            body, patch_summary = _maybe_patch_body(raw_body, kind)

        if DEBUG or patch_summary is not None:
            LOG.info(
                "[%s] %s -> %s (kind=%s, len=%d->%d, patched=%s)",
                method,
                self.path,
                upstream_url,
                kind,
                len(raw_body),
                len(body),
                patch_summary,
            )

        headers: dict[str, str] = {}
        for k in self.headers.keys():
            if k.lower() in _HOP_BY_HOP:
                continue
            headers[k] = self.headers[k]
        if body:
            headers["Content-Length"] = str(len(body))

        req = urllib.request.Request(
            url=upstream_url,
            data=body if method != "GET" else None,
            method=method,
            headers=headers,
        )
        try:
            # 大超时:SSE 流可能持续很久;由 openhuman-core / LIFT 层的超时兜底。
            resp = urllib.request.urlopen(req, timeout=1200)
        except urllib.error.HTTPError as e:
            # 上游返回 4xx / 5xx —— 把 status / headers / body 透传,不改语义。
            self._forward_response(e, is_error=True)
            return
        except (urllib.error.URLError, socket.error, ConnectionError) as e:
            LOG.warning("upstream transport error to %s: %r", upstream_url, e)
            self._send_gateway_error(f"upstream transport error: {e}")
            return

        self._forward_response(resp, is_error=False)

    def _forward_response(self, resp, is_error: bool) -> None:
        try:
            status = resp.status if not is_error else resp.code
            self.send_response_only(status, None)
            for k, v in resp.headers.items():
                if k.lower() in _HOP_BY_HOP:
                    continue
                self.send_header(k, v)
            self.end_headers()
            # 流式转发,支持 SSE / chunked。urllib response 是 file-like。
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)
                try:
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass

    def _send_gateway_error(self, msg: str) -> None:
        body = json.dumps({"error": {"message": msg, "type": "lift_proxy_error"}}).encode()
        try:
            self.send_response_only(502, None)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s [max_tokens_proxy] %(levelname)s %(message)s",
    )

    if not UPSTREAM:
        LOG.error("LIFT_PROXY_UPSTREAM env is not set; refusing to start")
        return 2

    LOG.info(
        "listening on %s:%d -> %s (MAX_TOKENS=%d, debug=%s)",
        LISTEN_HOST,
        LISTEN_PORT,
        UPSTREAM,
        MAX_TOKENS,
        DEBUG,
    )

    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), ProxyHandler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
