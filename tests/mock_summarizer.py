"""Mock OpenAI-compatible /chat/completions server for summarizer tests.

Echoes 'MOCK <sid> summary' for every sid mentioned in the user prompt.
Set fail_times > 0 to return HTTP 500 for the first N requests.
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class MockSummarizer:
    def __init__(self, fail_times: int = 0) -> None:
        outer = self
        self.requests: list[dict] = []
        self.fail_remaining = fail_times

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args) -> None:
                pass

            def do_GET(self) -> None:  # /models for doctor
                self._reply({"data": []})

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append(body)
                if outer.fail_remaining > 0:
                    outer.fail_remaining -= 1
                    self.send_response(500)
                    self.end_headers()
                    return
                user = next(
                    (m["content"] for m in body.get("messages", []) if m["role"] == "user"), ""
                )
                sids = sorted(set(re.findall(r"\b(s\d+) type=", user)), key=lambda s: int(s[1:]))
                rows = [{"sid": sid, "summary": f"MOCK {sid} summary"} for sid in sids]
                self._reply(
                    {"choices": [{"message": {"role": "assistant", "content": json.dumps(rows)}}]}
                )

            def _reply(self, payload: dict) -> None:
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}/v1"

    def __enter__(self) -> MockSummarizer:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
