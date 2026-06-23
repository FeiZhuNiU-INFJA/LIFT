"""把 LIFT dashboard 截成 PNG（供设计自查 / 分享）。

需要先在**有快速网络的机器**上装好 Playwright：

    pip install playwright
    playwright install chromium          # 下载 Chromium（约 150MB）

然后用例：

    # 1) 截正在跑的 HTTP dashboard（评测运行时）：
    python scripts/screenshot_dashboard.py --url http://localhost:8765 -o run.png

    # 2) 截一份静态导出 HTML（--evaluate-only 后生成的脱机快照）：
    python scripts/screenshot_dashboard.py --html path/to/dashboard.html -o final.png

可选：--width / --height 控制视口（默认 1680×2400，full_page 会截整页）。
"""

from __future__ import annotations

import argparse
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser(description="Screenshot the LIFT dashboard to a PNG.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--url", help="dashboard HTTP URL, e.g. http://localhost:8765")
    src.add_argument("--html", help="path to a static dashboard HTML file")
    ap.add_argument("-o", "--out", default="dashboard.png", help="output PNG path")
    ap.add_argument("--width", type=int, default=1680)
    ap.add_argument("--height", type=int, default=2400)
    ap.add_argument("--wait", type=float, default=1500,
                    help="ms to wait after load for JS render (default 1500)")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit(
            "playwright 未安装。在有快速网络的机器上执行：\n"
            "  pip install playwright && playwright install chromium"
        )

    if args.html:
        target = Path(args.html).resolve().as_uri()
        wait_until = "load"
    else:
        target = args.url
        # dashboard 有 SSE 长连接，networkidle 永不触发，用 domcontentloaded 即可
        wait_until = "domcontentloaded"

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            viewport={"width": args.width, "height": args.height},
            device_scale_factor=2,
        )
        page.goto(target, wait_until=wait_until)
        page.wait_for_timeout(int(args.wait))  # 让 refreshSnapshot + render() 跑一轮
        page.screenshot(path=args.out, full_page=True)
        browser.close()
    print(f"saved {args.out}")


if __name__ == "__main__":
    main()
