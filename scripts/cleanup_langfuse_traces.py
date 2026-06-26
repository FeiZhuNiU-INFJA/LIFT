"""清理自部署 Langfuse 中超过 N 天的 trace（含 observations / scores 级联删除）。

走 Langfuse 公共 REST API（``GET/DELETE /api/public/traces``），HTTP Basic Auth
使用 ``.env`` 的 ``LANGFUSE_PUBLIC_KEY`` + ``LANGFUSE_SECRET_KEY``。凭据是项目级的，
天然把删除范围限制在当前 project，不会跨项目误删。

实测自托管 Langfuse：list ``limit`` 硬上限 100、DELETE 单批硬上限 1000；list 加
``fields=core`` 可让单页响应快 ~40×。两端都用 asyncio 并发以接近 server 吞吐上限。

默认 dry-run，只列出要删的数量；显式带 ``--execute`` 才真删。

Examples:
    # 预演：看 3 天前有多少 trace 待清理
    python scripts/cleanup_langfuse_traces.py --older-than-days 3

    # 真删
    python scripts/cleanup_langfuse_traces.py --older-than-days 3 --execute
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv


LOGGER = logging.getLogger("cleanup_langfuse_traces")

# Langfuse server 端硬约束（自托管 v3 实测）：
_LIST_PAGE_SIZE = 100      # GET /api/public/traces 限制 limit<=100
_DELETE_BATCH_SIZE = 1000  # DELETE /api/public/traces 限制 traceIds.length<=1000

# 并发参数：list 端 8 并发吞吐就饱和（~35 req/s），delete 端 4 并发够用且不打挂 server。
_LIST_CONCURRENCY = 8
_DELETE_CONCURRENCY = 4

_HTTP_TIMEOUT_SECONDS = 60


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


async def _fetch_page(client: httpx.AsyncClient, cutoff_iso: str, page: int) -> tuple[int, list[str], int]:
    """拉单页。返回 (page, ids, total_pages)。"""
    resp = await client.get(
        "/api/public/traces",
        params={
            "toTimestamp": cutoff_iso,
            "limit": _LIST_PAGE_SIZE,
            "page": page,
            "fields": "core",  # 关键：只要 core 字段，单页响应快 ~40×
        },
    )
    resp.raise_for_status()
    payload = resp.json()
    rows = payload.get("data") or []
    ids = [str(row["id"]) for row in rows if row.get("id")]
    total_pages = int((payload.get("meta") or {}).get("totalPages") or 0)
    return page, ids, total_pages


async def _list_trace_ids(client: httpx.AsyncClient, cutoff_iso: str) -> list[str]:
    """先取第 1 页拿 totalPages，再并发抓剩余页。"""
    _, page1_ids, total_pages = await _fetch_page(client, cutoff_iso, 1)
    LOGGER.info("第 1 页：%d 条，totalPages=%d。", len(page1_ids), total_pages)
    if total_pages <= 1:
        return page1_ids

    sem = asyncio.Semaphore(_LIST_CONCURRENCY)

    async def bounded(p: int):
        async with sem:
            return await _fetch_page(client, cutoff_iso, p)

    t0 = time.time()
    results = await asyncio.gather(*[bounded(p) for p in range(2, total_pages + 1)])
    LOGGER.info("剩余 %d 页并发拉完，耗时 %.1fs。", total_pages - 1, time.time() - t0)

    all_ids = list(page1_ids)
    for _, ids, _ in sorted(results, key=lambda x: x[0]):
        all_ids.extend(ids)
    return all_ids


async def _delete_batch(client: httpx.AsyncClient, batch: list[str]) -> int:
    resp = await client.request("DELETE", "/api/public/traces", json={"traceIds": batch})
    resp.raise_for_status()
    return len(batch)


async def _delete_all(client: httpx.AsyncClient, ids: list[str]) -> int:
    """按 batch_size 切片并发删除，返回成功删除条数。"""
    batches = [ids[i : i + _DELETE_BATCH_SIZE] for i in range(0, len(ids), _DELETE_BATCH_SIZE)]
    sem = asyncio.Semaphore(_DELETE_CONCURRENCY)
    deleted = 0
    lock = asyncio.Lock()

    async def bounded(batch: list[str], idx: int):
        nonlocal deleted
        async with sem:
            try:
                n = await _delete_batch(client, batch)
            except httpx.HTTPError as exc:
                LOGGER.error("批 #%d（%d 条）失败：%s。", idx, len(batch), exc)
                return
            async with lock:
                deleted += n
                if deleted % 5000 < _DELETE_BATCH_SIZE:  # 每 ~5k 条打一次进度
                    LOGGER.info("已删除 %d / %d。", deleted, len(ids))

    await asyncio.gather(*[bounded(b, i) for i, b in enumerate(batches)])
    return deleted


async def _run(args: argparse.Namespace) -> int:
    public_key, secret_key, base_url = _load_credentials(args.base_url)
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=args.older_than_days)
    cutoff_iso = cutoff_dt.isoformat()
    mode = "EXECUTE" if args.execute else "DRY-RUN"
    LOGGER.info(
        "[%s] base=%s cutoff=%s (older than %d days)",
        mode, base_url, cutoff_iso, args.older_than_days,
    )

    async with httpx.AsyncClient(
        base_url=base_url,
        auth=(public_key, secret_key),
        timeout=_HTTP_TIMEOUT_SECONDS,
        limits=httpx.Limits(max_connections=max(_LIST_CONCURRENCY, _DELETE_CONCURRENCY) * 2),
    ) as client:
        t_list = time.time()
        ids = await _list_trace_ids(client, cutoff_iso)
        LOGGER.info("List 完成：%d 条过期 trace，耗时 %.1fs。", len(ids), time.time() - t_list)
        if not ids:
            return 0

        if not args.execute:
            LOGGER.info("dry-run 结束；如需真删，加 --execute。")
            return 0

        t_del = time.time()
        deleted = await _delete_all(client, ids)
        LOGGER.info(
            "Delete 完成：%d / %d 条，耗时 %.1fs（observations/scores 由 Langfuse 后台异步级联清理）。",
            deleted, len(ids), time.time() - t_del,
        )
    return 0


def main() -> int:
    logging.basicConfig(
        level=os.getenv("CLEANUP_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    args = _parse_args()
    if args.older_than_days < 1:
        LOGGER.error("--older-than-days 必须 >= 1（防止误删近期数据）。")
        return 2
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
