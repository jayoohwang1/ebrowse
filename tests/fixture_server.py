"""Tiny static HTTP server for fixture pages (stdlib only).

Usage in tests:
    with FixtureServer() as srv:
        page.goto(srv.url("form.html"))

Manual: python -m tests.fixture_server [port]
"""

from __future__ import annotations

import http.server
import sys
import threading
from functools import partial
from pathlib import Path

PAGES_DIR = Path(__file__).parent / "fixtures" / "pages"


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        pass

    def do_GET(self) -> None:
        if self.path == "/redirect-to-localhost":
            port = self.server.server_address[1]
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{port}/form.html")
            self.end_headers()
            return
        super().do_GET()


class FixtureServer:
    def __init__(self, port: int = 0) -> None:
        handler = partial(_QuietHandler, directory=str(PAGES_DIR))
        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def url(self, page: str = "") -> str:
        return f"http://127.0.0.1:{self.port}/{page}"

    def __enter__(self) -> FixtureServer:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8901
    with FixtureServer(port) as srv:
        print(f"serving {PAGES_DIR} at {srv.url()}")
        threading.Event().wait()
