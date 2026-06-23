"""清理自部署 Langfuse 中超过 N 天的 trace（含 observations / scores 级联删除）。

走 Langfuse 公共 REST API（``GET/DELETE /api/public/traces``），HTTP Basic Auth
使用 ``.env`` 的 ``LANGFUSE_PUBLIC_KEY`` + ``LANGFUSE_SECRET_KEY``。凭据是项目级的，
天然把删除范围限制在当前 project，不会跨项目误删。

默认 dry-run，只列出要删的数量；显式带 ``--execute`` 才真删。

Examples:
    # 预演：看 30 天前有多少 trace 待清理
    python scripts/cleanup_langfuse_traces.py --older-than-days 30

    # 真删 + 调小批量（langfuse server 顶不住时）
    python scripts/cleanup_langfuse_traces.py --older-than-days 30 --execute --batch-size 50
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv


LOGGER = logging.getLogger("cleanup_langfuse_traces")

# 顶层超时：list 翻页 + delete 批量都用同一个 client，60s 与 trace_backfill 对齐。
_HTTP_TIMEOUT_SECONDS = 60
# 列表 API 单页最大条数（Langfuse 公共 API 默认上限 100）。
_LIST_PAGE_SIZE = 100


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--older-than-days",
        type=int,
        required=True,
        help="删除 timestamp 早于 now-N 天的 trace（N>=1）。",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="真正执行删除；不带此参数时仅 dry-run。",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="批量删除的每批大小（默认 100，Langfuse server 压力大时调小）。",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="Langfuse base URL，默认读 LANGFUSE_BASE_URL。",
    )
    return parser.parse_args()


def _load_credentials(cli_base_url: str | None) -> tuple[str, str, str]:
    """读取 ``.env`` 中的 Langfuse 凭据，缺一即报错退出。"""
    load_dotenv()
    public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
    secret_key = os.getenv("LANGFUSE_SECRET_KEY")
    base_url = cli_base_url or os.getenv("LANGFUSE_BASE_URL")
    missing = [
        name
        for name, val in [
            ("LANGFUSE_PUBLIC_KEY", public_key),
            ("LANGFUSE_SECRET_KEY", secret_key),
            ("LANGFUSE_BASE_URL", base_url),
        ]
        if not val
    ]
    if missing:
        LOGGER.error("缺少环境变量：%s。请先在 .env 中配置后重试。", ", ".join(missing))
        sys.exit(2)
    return public_key, secret_key, base_url.rstrip("/")


def _list_trace_ids_before(client: httpx.Client, cutoff_iso: str) -> list[str]:
    """分页拉取 ``timestamp <= cutoff`` 的所有 trace id。"""
    ids: list[str] = []
    page = 1
    while True:
        resp = client.get(
            "/api/public/traces",
            params={"toTimestamp": cutoff_iso, "limit": _LIST_PAGE_SIZE, "page": page},
        )
        resp.raise_for_status()
        payload = resp.json()
        rows = payload.get("data") or []
        if not rows:
            break
        ids.extend(str(row["id"]) for row in rows if row.get("id"))
        meta = payload.get("meta") or {}
        total_pages = int(meta.get("totalPages") or 0)
        LOGGER.info(
            "扫描第 %d/%s 页，本页 %d 条，累计 %d 条。",
            page,
            total_pages or "?",
            len(rows),
            len(ids),
        )
        if total_pages and page >= total_pages:
            break
        page += 1
    return ids


def _delete_batch(client: httpx.Client, batch: list[str]) -> None:
    """调用批量删除接口；Langfuse 会异步级联清理 observations / scores。"""
    resp = client.request(
        "DELETE",
        "/api/public/traces",
        json={"traceIds": batch},
    )
    resp.raise_for_status()


def main() -> int:
    logging.basicConfig(
        level=os.getenv("CLEANUP_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    args = _parse_args()
    if args.older_than_days < 1:
        LOGGER.error("--older-than-days 必须 >= 1（防止误删近期数据）。")
        return 2
    if args.batch_size < 1:
        LOGGER.error("--batch-size 必须 >= 1。")
        return 2

    public_key, secret_key, base_url = _load_credentials(args.base_url)
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=args.older_than_days)
    cutoff_iso = cutoff_dt.isoformat()
    mode = "EXECUTE" if args.execute else "DRY-RUN"
    LOGGER.info(
        "[%s] base=%s cutoff=%s (older than %d days)",
        mode, base_url, cutoff_iso, args.older_than_days,
    )

    with httpx.Client(
        base_url=base_url,
        auth=(public_key, secret_key),
        timeout=_HTTP_TIMEOUT_SECONDS,
    ) as client:
        ids = _list_trace_ids_before(client, cutoff_iso)
        if not ids:
            LOGGER.info("没有需要清理的 trace。")
            return 0
        LOGGER.info("共找到 %d 条过期 trace。", len(ids))

        if not args.execute:
            LOGGER.info("dry-run 结束；如需真删，加 --execute。")
            return 0

        deleted = 0
        for i in range(0, len(ids), args.batch_size):
            batch = ids[i : i + args.batch_size]
            try:
                _delete_batch(client, batch)
            except httpx.HTTPError as exc:
                LOGGER.error(
                    "批量删除失败（offset=%d size=%d）：%s。剩余 trace 跳过本批继续。",
                    i, len(batch), exc,
                )
                continue
            deleted += len(batch)
            LOGGER.info("已提交删除 %d/%d。", deleted, len(ids))
        LOGGER.info(
            "完成：提交删除 %d 条 trace（Langfuse 后台异步级联清理 observations/scores）。",
            deleted,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
