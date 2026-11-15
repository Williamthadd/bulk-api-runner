"""Tiny threaded HTTP server for tests. Never talks to the real API."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


class FakeServer:
    def __init__(self, handler=None):
        self.requests: list[dict] = []
        # handler(req) -> (status, body(dict|str|None))
        self.handler = handler or (lambda req: (200, {"code": 200, "msg": "ok", "data": {}}))
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def _do(self):
                parts = urlsplit(self.path)
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = raw.decode()
                req = {"method": self.command, "path": parts.path,
                       "query": {k: v[0] for k, v in parse_qs(parts.query).items()},
                       "headers": {k.lower(): v for k, v in self.headers.items()}, "json": body}
                outer.requests.append(req)
                status, out = outer.handler(req)
                data = out if isinstance(out, (bytes, str)) else json.dumps(out)
                data = data.encode() if isinstance(data, str) else data
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _do

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()
