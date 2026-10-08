"""Dev server for the explorer frontend: `python frontend/serve.py [--port 8080] [--api http://127.0.0.1:8000]
[--host-page gpa.example.com=/biggpa ...]`.

Proxies /api/* (GET, POST, PUT, PATCH and DELETE) to the API, so the page can call it same-origin without the
API needing CORS, serves the standalone pages in PAGES (e.g. /biggpa), and serves index.html for every
other path (so `/<source URL>` links reach the page's link resolver). `--host-page` serves a standalone
page at / for requests to that Host, so one server can back a second domain. Stdlib only.
"""

from __future__ import annotations

import argparse
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
INDEX = HERE / "index.html"
# Standalone pages, outside the explorer's hash router. Re-read per request, like index.html.
PAGES = {"/biggpa": HERE / "biggpa.html"}


def make_handler(api: str, host_pages: dict[str, str]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self._proxy_write("POST")

        def do_PUT(self) -> None:
            self._proxy_write("PUT")

        def do_PATCH(self) -> None:
            self._proxy_write("PATCH")

        def do_DELETE(self) -> None:
            self._proxy_write("DELETE")

        def _proxy_write(self, method: str) -> None:
            if not self.path.startswith("/api/"):
                self._send(405, "text/plain", b"Method not allowed")
                return
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self._proxy(self.path[len("/api"):], method, body)

        def do_GET(self) -> None:
            if self.path.startswith("/api/"):
                target = self.path[len("/api"):]
            elif self.path == "/favicon.ico":
                self._send(404, "text/plain", b"Not found")
                return
            elif page := PAGES.get(self.path.split("?", 1)[0].rstrip("/") or self._host_page()):
                self._send(200, "text/html; charset=utf-8", page.read_bytes())
                return
            else:
                # Every other path gets the page, which handles `/<source URL>` links itself
                # (e.g. /https://classroom.google.com/u/1/c/.../a/.../details).
                self._send(200, "text/html; charset=utf-8", INDEX.read_bytes())
                return
            self._proxy(target)

        def _host_page(self) -> str:
            """The page --host-page maps this request's Host (port ignored) to, else ""."""
            host = (self.headers.get("Host") or "").partition(":")[0].lower()
            return host_pages.get(host, "")

        def _proxy(self, target: str, method: str = "GET", body: bytes | None = None) -> None:
            req = urllib.request.Request(api + target, data=body, method=method)
            # Tells the API it's mounted at /api, so its docs and OpenAPI servers point here.
            req.add_header("X-Forwarded-Prefix", "/api")
            if ctype := self.headers.get("Content-Type"):
                req.add_header("Content-Type", ctype)
            if auth := self.headers.get("Authorization"):
                req.add_header("Authorization", auth)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    self._send(resp.status, resp.headers.get("Content-Type", ""), resp.read())
            except urllib.error.HTTPError as e:
                self._send(e.code, e.headers.get("Content-Type", ""), e.read())
            except OSError as e:
                self._send(502, "application/json", f'{{"detail": "API unreachable at {api}: {e}"}}'.encode())

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
    parser.add_argument("--host-page", action="append", default=[], metavar="HOST=PAGE",
                        help=f"serve PAGE at / for requests to HOST (repeatable); PAGE is one of {', '.join(PAGES)}")
    args = parser.parse_args()
    host_pages = {}
    for spec in args.host_page:
        host, _, page = spec.partition("=")
        if not host or page not in PAGES:
            parser.error(f"--host-page {spec!r}: expected HOST=PAGE with PAGE one of {', '.join(PAGES)}")
        host_pages[host.lower()] = page
    server = ThreadingHTTPServer((args.host, args.port), make_handler(args.api.rstrip("/"), host_pages))
    print(f"Explorer on http://{args.host}:{args.port}  (proxying /api/* -> {args.api})")
    for host, page in host_pages.items():
        print(f"  {host}/ serves {page}")
    server.serve_forever()


if __name__ == "__main__":
    main()
