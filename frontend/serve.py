"""Dev server for the explorer frontend: `python frontend/serve.py [--port 8080] [--api http://127.0.0.1:8000]`.

Proxies /api/* to the API, so the page can call it same-origin without the API needing
CORS, and serves index.html for every other path (so `/<source URL>` links reach the page's
link resolver). Stdlib only.
"""

from __future__ import annotations

import argparse
import shutil
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

INDEX = Path(__file__).resolve().parent / "index.html"


def make_handler(api: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.startswith("/api/"):
                target = self.path[len("/api"):]
            elif self.path == "/openapi.json":  # the API's /docs page fetches this from the root
                target = self.path
            elif self.path == "/favicon.ico":
                self._send(404, "text/plain", b"Not found")
                return
            else:
                # Every other path gets the page, which handles `/<source URL>` links itself
                # (e.g. /https://classroom.google.com/u/1/c/.../a/.../details).
                self._send(200, "text/html; charset=utf-8", INDEX.read_bytes())
                return
            self._proxy(target)

        # Attachment file requests and deletes (see lifeapi/files.py).
        def do_POST(self) -> None:
            self._api_only()

        def do_DELETE(self) -> None:
            self._api_only()

        def _api_only(self) -> None:
            if self.path.startswith("/api/"):
                self._proxy(self.path[len("/api"):])
            else:
                self._send(405, "text/plain", b"Method not allowed")

        def _proxy(self, target: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            req = urllib.request.Request(api + target, data=body, method=self.command)
            if auth := self.headers.get("Authorization"):
                req.add_header("Authorization", auth)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    self._relay(resp.status, resp)
            except urllib.error.HTTPError as e:
                self._relay(e.code, e)
            except OSError as e:
                self._send(502, "application/json", f'{{"detail": "API unreachable at {api}: {e}"}}'.encode())

        def _relay(self, status: int, resp) -> None:
            # Streamed, so downloaded attachment files don't have to fit in memory.
            self.send_response(status)
            for name in ("Content-Type", "Content-Length", "Content-Disposition"):
                if value := resp.headers.get(name):
                    self.send_header(name, value)
            self.end_headers()
            shutil.copyfileobj(resp, self.wfile)

        def _send(self, status: int, ctype: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(prog="python frontend/serve.py")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(args.api.rstrip("/")))
    print(f"Explorer on http://{args.host}:{args.port}  (proxying /api/* -> {args.api})")
    server.serve_forever()


if __name__ == "__main__":
    main()
