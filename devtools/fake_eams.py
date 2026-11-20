#!/usr/bin/env python3
"""Local fake of the EAMS endpoints, for rehearsing a bulk run without touching a real server.

    python devtools/fake_eams.py --port 8899 [--expire-after 2] [--fail-503 1]

Then set base_url = http://127.0.0.1:8899 in an environment and any non-empty bearer_token.
  --expire-after N   the first token stops working (401) after N authorised calls; paste any other token to continue
  --fail-503 K       the first K bulk-schedule calls answer 503 (to see retry with backoff)

It also has an OAuth2 token endpoint for auth profiles: POST http://127.0.0.1:8899/token
(form: grant_type=client_credentials, client_id=<any>, client_secret=<--client-secret, default local-secret>).
With --expire-after, the first fetched token expires and the app should fetch a new one by itself.
Employees whose email contains "unknown" are not found; emails containing "dup" are rejected with code 409.
"""
from __future__ import annotations

import argparse
import base64
import json
import time
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

STATE = {"first_token": None, "authorised": 0, "bulk_calls": 0}
LOCK = threading.Lock()


def make_handler(args):
    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):
            print("fake-eams:", self.command, self.path.split("?")[0], "->", a[1] if len(a) > 1 else "")

        def _send(self, status, obj):
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _auth(self) -> bool:
            h = self.headers.get("Authorization") or ""
            if not h.startswith("Bearer ") or not h[7:].strip():
                self._send(401, {"code": 401, "msg": "missing bearer token", "data": None})
                return False
            tok = h[7:].strip()
            with LOCK:
                if STATE["first_token"] is None:
                    STATE["first_token"] = tok
                if args.expire_after and tok == STATE["first_token"] and STATE["authorised"] >= args.expire_after:
                    self._send(401, {"code": 401, "msg": "token expired", "data": None})
                    return False
                STATE["authorised"] += 1
            return True

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"null") if n else None

        def do_GET(self):
            u = urlsplit(self.path)
            if not self._auth():
                return
            if u.path == "/api/v1/employee/list":
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                email = (q.get("value") or "").strip().lower()
                items = [] if (not email or "unknown" in email) else [{
                    "id": str(uuid.uuid5(uuid.NAMESPACE_URL, email)), "employee_email": email,
                    "employee_name": email.split("@")[0].replace(".", " ").title()}]
                return self._send(200, {"code": 200, "msg": "success",
                                        "data": {"items": items, "page": int(q.get("page", 1)), "total": len(items)}})
            self._send(404, {"code": 404, "msg": "not found", "data": None})

        def _token(self):
            n = int(self.headers.get("Content-Length") or 0)
            form = {k: v[0] for k, v in parse_qs((self.rfile.read(n) if n else b"").decode()).items()}
            if form.get("grant_type") != "client_credentials":
                return self._send(400, {"error": "unsupported_grant_type"})
            if not form.get("client_id") or form.get("client_secret") != args.client_secret:
                return self._send(401, {"error": "unauthorized_client",
                                        "error_description": "Invalid client or Invalid client credentials"})
            enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
            tok = ".".join([enc({"alg": "none", "typ": "JWT"}),
                            enc({"exp": int(time.time()) + args.token_ttl, "azp": form["client_id"],
                                 "jti": str(uuid.uuid4())}), "fake-signature"])
            return self._send(200, {"access_token": tok, "expires_in": args.token_ttl, "token_type": "Bearer"})

        def do_POST(self):
            u = urlsplit(self.path)
            if u.path.endswith("/token"):
                return self._token()
            if not self._auth():
                return
            body = self._body() or {}
            if u.path == "/api/v1/attendance/schedule/bulk":
                with LOCK:
                    STATE["bulk_calls"] += 1
                    n = STATE["bulk_calls"]
                if n <= args.fail_503:
                    return self._send(503, {"code": 503, "msg": "service busy, try again", "data": None})
                # the body is an array: one element per (employee, warehouse), each with its own "schedules"
                groups = body if isinstance(body, list) else [body]
                sc = sum(len((g or {}).get("schedules") or []) for g in groups)
                return self._send(202, {"code": 202, "msg": "schedule upload accepted",
                                        "data": {"upload_id": str(uuid.uuid4()), "schedule_count": sc,
                                                 "batch_count": max(1, (sc + 49) // 50)}})
            if u.path == "/api/v1/warehouse/create":
                return self._send(200, {"code": 200, "msg": "warehouse created", "data": {"id": str(uuid.uuid4())}})
            if u.path == "/api/v1/employee/create":
                if "dup" in (body.get("employee_email") or ""):
                    return self._send(200, {"code": 409, "msg": "employee email already exists", "data": None})
                return self._send(200, {"code": 200, "msg": "employee created", "data": {"id": str(uuid.uuid4())}})
            self._send(404, {"code": 404, "msg": "not found", "data": None})

    return H


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--expire-after", type=int, default=0)
    ap.add_argument("--fail-503", type=int, default=0)
    ap.add_argument("--client-secret", default="local-secret", help="client_secret accepted by /token")
    ap.add_argument("--token-ttl", type=int, default=300, help="lifetime of fetched tokens in seconds")
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(args))
    print(f"fake EAMS listening on http://127.0.0.1:{args.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
