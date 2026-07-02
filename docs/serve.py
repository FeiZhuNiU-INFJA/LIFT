#!/usr/bin/env python3
"""
Local server for editing the deck's speaker notes and saving them back to the
HTML source file. Stdlib only.

Usage:
    python3 docs/serve.py            # http://127.0.0.1:8000
    python3 docs/serve.py 8957       # custom port

Then open:
    http://127.0.0.1:8000/lift-framework-slides.html
press E to open the notes drawer, edit, and click "保存到源文件" — the edits are
written straight back to docs/lift-framework-slides.html (a .bak of the previous
version is kept beside it). "导出 HTML" still works as a no-server fallback.

What it does:
  • Serves this directory (like `python -m http.server`), so the deck and all
    its assets load normally.
  • Accepts  POST /save?file=<name>.html  with the full document HTML in the
    body, and atomically overwrites that file under this directory.
      - only files ending in .html/.htm, resolved to a bare basename (no path
        traversal), inside this directory;
      - bound to 127.0.0.1 only (never exposed to the network);
      - 8 MB body cap; previous version backed up to <file>.bak before write.
"""
import os
import sys
import json
import shutil
from urllib.parse import urlparse, parse_qs
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
MAX_BYTES = 8 * 1024 * 1024


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def do_POST(self):  # noqa: N802 (http.server API)
        u = urlparse(self.path)
        if u.path != "/save":
            return self._json(404, {"ok": False, "error": "not found"})

        name = (parse_qs(u.query).get("file") or [""])[0]
        name = os.path.basename(name.strip())  # strip any path components
        if not name or not name.lower().endswith((".html", ".htm")):
            return self._json(400, {"ok": False, "error": "invalid file name"})

        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > MAX_BYTES:
            return self._json(413, {"ok": False, "error": "body too large"})

        body = self.rfile.read(length)
        try:
            data = body.decode("utf-8")
        except UnicodeDecodeError:
            return self._json(400, {"ok": False, "error": "body not utf-8"})

        target = os.path.abspath(os.path.join(ROOT, name))
        if not (target == os.path.join(ROOT, name) and target.startswith(ROOT + os.sep)):
            return self._json(400, {"ok": False, "error": "outside root"})

        try:
            if os.path.exists(target):
                shutil.copy2(target, target + ".bak")
            tmp = target + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp, target)  # atomic on same filesystem
        except Exception as e:  # noqa: BLE001
            return self._json(500, {"ok": False, "error": str(e)})

        return self._json(200, {"ok": True, "file": name, "bytes": len(body)})

    def _json(self, code, obj):
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        sys.stderr.write("[serve] " + (fmt % args) + "\n")


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"[serve] docs root : {ROOT}")
    print(f"[serve] open      : http://127.0.0.1:{port}/lift-framework-slides.html")
    print(f"[serve] save ep   : POST /save?file=<name>.html  (writes under docs/, keeps .bak)")
    print("[serve] Ctrl+C to stop")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[serve] stopped")


if __name__ == "__main__":
    main()
