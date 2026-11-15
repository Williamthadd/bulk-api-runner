"""Runner against a fake local HTTP server (no real API)."""
import asyncio

import pytest

from app import http_engine as he
from app.runner import DONE, FAILED, PAUSED, STOPPED, RunManager, RunState
from tests.fake_server import FakeServer


class MemBackend:
    def __init__(self, spec, variables, secret_keys=("bearer_token",)):
        self.spec, self.vars, self.secret_keys = spec, variables, secret_keys
        self.items: dict[int, dict] = {}
        self.runs = []

    def prepare(self, item):
        secrets = [self.vars.get(k, "") for k in self.secret_keys]
        return he.prepare(self.spec, self.vars, item=item), secrets

    def persist_run(self, st):
        self.runs.append(st.status)
        return None

    def persist_item(self, st, row):
        self.items[row["seq"]] = dict(row)

    def after_finish(self, st):
        pass


def make(server_url, n=3, token="tok", **settings):
    spec = he.RequestSpec(id=1, name="r", method="POST", url="{{base_url}}/api/x",
                          token_template="{{bearer_token}}", settings=he.normalize_settings(settings))
    backend = MemBackend(spec, {"base_url": server_url, "bearer_token": token})
    items = [{"seq": i + 1, "label": f"user{i + 1}@x", "body": {"n": i + 1}} for i in range(n)]
    st = RunState(request_id=1, request_name="r", environment_name="Dev", mode="send", method="POST",
                  url_display=f"{server_url}/api/x", items=items, settings=spec.settings)
    return st, backend


async def until(cond, timeout=10):
    for _ in range(int(timeout / 0.02)):
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


async def run_to_end(st, backend, mgr=None):
    mgr = mgr or RunManager()
    await mgr.start(st, backend)
    await asyncio.wait_for(st.task, 20)
    return mgr


async def test_success_sends_bearer_and_body():
    with FakeServer() as srv:
        st, b = make(srv.url)
        await run_to_end(st, b)
    assert st.status == DONE and st.success_count == 3 and st.failed_count == 0
    assert [r["json"] for r in srv.requests] == [{"n": 1}, {"n": 2}, {"n": 3}]
    assert all(r["headers"]["authorization"] == "Bearer tok" for r in srv.requests)
    # JSON body -> Content-Type: application/json; no Accept of our own (httpx's default "*/*" is curl's too)
    assert all(r["headers"].get("accept") == "*/*" and r["headers"]["content-type"] == "application/json"
               for r in srv.requests)
    row = b.items[1]
    assert row["http_status"] == 200 and row["message"] == "ok" and row["success"]
    assert "authorization" not in row["request_body"].lower()


async def test_code_not_200_is_failure():
    with FakeServer(lambda req: (200, {"code": 400, "msg": "employee already scheduled"})) as srv:
        st, b = make(srv.url, n=2)
        await run_to_end(st, b)
    assert st.status == DONE and st.failed_count == 2
    assert b.items[1]["message"] == "code 400 != 200: employee already scheduled"
    assert b.items[1]["http_status"] == 200 and not b.items[1]["success"]


async def test_503_retried_with_backoff_then_accepted():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] <= 2:
            return 503, {"code": 503, "msg": "busy"}
        return 202, {"code": 202, "msg": "accepted", "data": {"upload_id": "u1", "schedule_count": 31, "batch_count": 1}}

    with FakeServer(handler) as srv:
        st, b = make(srv.url, n=1, retry={"max_retries": 3, "on_status": [503], "backoff_ms": 10},
                     success={"require_2xx": True, "code_field": "code", "code_values": [200, 202]})
        await run_to_end(st, b)
    assert len(srv.requests) == 3
    row = b.items[1]
    assert row["success"] and row["http_status"] == 202 and row["attempts"] == 3
    assert "upload_id" in row["response_body"]


async def test_503_without_retry_and_4xx_never_retried():
    with FakeServer(lambda req: (503, {"msg": "busy"})) as srv:
        st, b = make(srv.url, n=1)
        await run_to_end(st, b)
    assert len(srv.requests) == 1 and st.failed_count == 1
    with FakeServer(lambda req: (409, {"msg": "dup"})) as srv:
        st, b = make(srv.url, n=1, retry={"max_retries": 3, "on_status": [409, 503], "backoff_ms": 0})
        await run_to_end(st, b)
    assert len(srv.requests) == 1


async def test_stop_on_first_error():
    with FakeServer(lambda req: (400, {"code": 400, "msg": "bad"})) as srv:
        st, b = make(srv.url, n=3, stop_on_error=True)
        await run_to_end(st, b)
    assert st.status == FAILED and len(srv.requests) == 1 and "first error" in st.stop_reason


async def test_401_pauses_and_resume_resends_only_failed_item():
    valid = {"old"}

    def handler(req):
        tok = req["headers"]["authorization"].split(" ", 1)[1]
        if tok not in valid:
            return 401, {"code": 401, "msg": "token expired"}
        if req["json"]["n"] == 1:
            valid.clear(); valid.add("new")   # token "expires" after the first item
        return 200, {"code": 200, "msg": "ok"}

    with FakeServer(handler) as srv:
        st, b = make(srv.url, n=3, token="old")
        mgr = RunManager()
        await mgr.start(st, b)
        await until(lambda: st.status == PAUSED)
        assert st.pause_reason == "401" and "Token expired or invalid" in st.banner
        assert st.success_count == 1 and st.failed_count == 0
        assert b.items[2]["http_status"] == 401
        b.vars["bearer_token"] = "new"           # user pastes a fresh token
        mgr.resume(st.id)
        await asyncio.wait_for(st.task, 10)
    assert st.status == DONE and st.success_count == 3 and st.failed_count == 0
    sent = [(r["json"]["n"], r["headers"]["authorization"]) for r in srv.requests]
    assert sent == [(1, "Bearer old"), (2, "Bearer old"), (2, "Bearer new"), (3, "Bearer new")]
    assert b.items[2]["attempts"] == 2


async def test_401_counts_as_failure_when_pause_disabled():
    with FakeServer(lambda req: (401, {"msg": "unauthorized"})) as srv:
        st, b = make(srv.url, n=2, pause_on_401=False)
        await run_to_end(st, b)
    assert st.status == DONE and st.failed_count == 2 and len(srv.requests) == 2


async def test_stop_while_paused_on_401():
    with FakeServer(lambda req: (401, {"msg": "unauthorized"})) as srv:
        st, b = make(srv.url, n=3)
        mgr = RunManager()
        await mgr.start(st, b)
        await until(lambda: st.status == PAUSED)
        mgr.stop(st.id)
        await asyncio.wait_for(st.task, 10)
    assert st.status == STOPPED and st.failed_count == 1 and len(srv.requests) == 1


async def test_empty_token_never_sends():
    with FakeServer() as srv:
        st, b = make(srv.url, n=2, token="", pause_on_401=False)
        await run_to_end(st, b)
    assert srv.requests == [] and st.failed_count == 2
    assert b.items[1]["message"].startswith("Not sent: Bearer token is empty")


async def test_user_pause_resume_and_delay():
    with FakeServer() as srv:
        st, b = make(srv.url, n=4, delay_ms=150)
        mgr = RunManager()
        await mgr.start(st, b)
        await until(lambda: st.success_count >= 1)
        mgr.pause(st.id)
        await until(lambda: st.status == PAUSED)
        n = len(srv.requests)
        await asyncio.sleep(0.4)
        assert len(srv.requests) == n            # nothing sent while paused
        mgr.resume(st.id)
        await asyncio.wait_for(st.task, 10)
    assert st.status == DONE and st.success_count == 4


async def test_secrets_masked_in_stored_rows():
    with FakeServer(lambda req: (200, {"code": 200, "msg": "echo tok-SECRET-123", "data": req["json"]})) as srv:
        st, b = make(srv.url, n=1, token="tok-SECRET-123")
        st.items[0]["body"] = {"note": "tok-SECRET-123"}
        await run_to_end(st, b)
    row = b.items[1]
    for k in ("message", "request_body", "response_body", "url"):
        assert "tok-SECRET-123" not in row[k]
    assert "****" in row["response_body"]


async def test_only_one_active_run():
    with FakeServer() as srv:
        st, b = make(srv.url, n=2, delay_ms=300)
        mgr = RunManager()
        await mgr.start(st, b)
        st2, b2 = make(srv.url, n=1)
        with pytest.raises(RuntimeError):
            await mgr.start(st2, b2)
        mgr.stop(st.id)
        await asyncio.wait_for(st.task, 10)
