"""End-to-end through the HTTP API (real MySQL + fake upstream server)."""
import time

import pytest
from fastapi.testclient import TestClient

from app import main
from app import db as appdb
from app import services as svc
from tests.fake_server import FakeServer
from tests.helpers import EMPLOYEE_XLSX

pytestmark = pytest.mark.db

IDS = {"william.thaddeus@moladin.com": "88032f68-0302-4c2a-8fcb-4c47898cbb6e",
       "jason.adiwijaya@moladin.ai": "22222222-2222-2222-2222-222222222222",
       "bryan.lakaoni@moladin.ai": "11111111-1111-1111-1111-111111111111"}


def fake_eams(req):
    p = req["path"]
    if p == "/api/v1/employee/list":
        email = req["query"].get("value", "")
        items = [{"id": IDS[email], "employee_email": email}] if email in IDS else []
        return 200, {"code": 200, "msg": "success", "data": {"items": items, "page": 1}}
    if p == "/api/v1/attendance/schedule/bulk":
        n = sum(len(g["schedules"]) for g in req["json"])        # the body is an array of groups
        return 202, {"code": 202, "msg": "accepted", "data": {"upload_id": "up-1", "schedule_count": n, "batch_count": 1}}
    if p == "/api/v1/employee/create":
        if "dup" in (req["json"] or {}).get("employee_email", ""):
            return 200, {"code": 409, "msg": "employee email already exists", "data": None}
        return 200, {"code": 200, "msg": "employee created", "data": {"id": "e1"}}
    return 404, {"code": 404, "msg": "not found"}


@pytest.fixture
def client(db):
    main.DRY_RUNS.clear()
    main.manager.runs.clear()
    with appdb.session_scope() as s:
        svc.seed_if_empty(s)
    with FakeServer(fake_eams) as srv, TestClient(main.app) as c:
        env = c.get("/api/environments").json()["environments"][0]
        c.put(f"/api/environments/{env['id']}/variables",
              json={"variables": [{"key": "base_url", "value": srv.url},
                                  {"key": "bearer_token", "value": None, "is_secret": True}]})
        c.srv, c.env_id = srv, env["id"]
        c.req = {r["name"]: r["id"] for r in c.get("/api/tree").json()["tree"][0]["requests"]}
        yield c


def set_token(c, value):
    c.put(f"/api/environments/{c.env_id}/variables/bearer_token", json={"value": value})


def wait_run(c, run_id, timeout=15):
    for _ in range(int(timeout / 0.05)):
        d = c.get(f"/api/runs/{run_id}").json()
        if d["run"]["status"] not in ("Running", "Paused"):
            return d
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def test_bulk_schedule_is_one_request_with_an_array_body_and_202(client):
    c, rid = client, client.req["Attendance - bulk schedule"]
    set_token(c, "tok-123456")
    up = c.post(f"/api/requests/{rid}/use-sample").json()
    assert up["preview"]["types"] == {"email": "text", "warehouse": "text", "date": "date",
                                      "start_time": "time", "end_time": "time"}
    dr = c.post(f"/api/requests/{rid}/dry-run", json={"upload_id": up["upload"]["id"]}).json()
    assert dr["ok"], dr
    assert len(dr["items"]) == 1 and dr["items"][0]["label"] == "3 employee schedule group(s)"
    groups = dr["items"][0]["body"]
    assert [(g["email"], len(g["schedules"])) for g in groups] == [
        ("william.thaddeus@moladin.com", 31), ("jason.adiwijaya@moladin.ai", 25), ("bryan.lakaoni@moladin.ai", 31)]
    assert not [r for r in c.srv.requests if r["path"] == "/api/v1/employee/list"]    # no lookups any more
    curl = c.post(f"/api/requests/{rid}/curl", json={"index": 0}).json()["curl"]
    assert "tok-123456" not in curl and "Bearer ****" in curl and "--data '[{" in curl

    pf = c.get(f"/api/requests/{rid}/send-check").json()
    assert pf["ok"] and pf["count"] == 1 and pf["environment"] == "Development"
    run = c.post(f"/api/requests/{rid}/send", json={"mode": "send", "fingerprint": dr["fingerprint"]}).json()
    d = wait_run(c, run["id"])
    assert d["run"]["status"] == "Done" and d["run"]["success_count"] == 1
    assert d["items"][0]["http_status"] == 202
    assert d["items"][0]["accepted"] == {"upload_id": "up-1", "schedule_count": 87, "batch_count": 1}
    sent = [r for r in c.srv.requests if r["path"] == "/api/v1/attendance/schedule/bulk"]
    assert len(sent) == 1 and isinstance(sent[0]["json"], list) and len(sent[0]["json"]) == 3
    assert sent[0]["headers"]["content-type"] == "application/json"
    # history comes back from MySQL once the run is no longer in memory
    main.manager.runs.clear()
    hist = c.get(f"/api/runs?request_id={rid}").json()["runs"]
    assert hist[0]["id"] == run["id"] and hist[0]["status"] == "Done"
    d2 = c.get(f"/api/runs/{run['id']}").json()
    assert len(d2["items"]) == 1 and d2["items"][0]["accepted"]["schedule_count"] == 87
    full = c.get(f"/api/runs/{run['id']}/items/1").json()
    assert '"schedules"' in full["request_body"] and "tok-123456" not in full["request_body"]
    csv = c.get(f"/api/runs/{run['id']}/export.csv").text
    assert csv.splitlines()[0].startswith("time_utc,item,label") and len(csv.splitlines()) == 2


def test_ctx_call_reaches_another_request_with_the_folder_token(client):
    """ctx.call through the HTTP engine (the seed no longer ships a helper request, so the test creates one)."""
    c, rid = client, client.req["Employee - create"]
    set_token(c, "tok-123456")
    folder_id = c.get(f"/api/requests/{rid}").json()["folder_id"]
    h = c.post("/api/requests", json={"folder_id": folder_id, "name": "Employee - find by email"}).json()
    c.put(f"/api/requests/{h['id']}", json={"url": "{{base_url}}/api/v1/employee/list",
                                            "params": [{"key": "page", "value": "1", "enabled": True}]})
    c.put(f"/api/requests/{rid}", json={"script": (
        "def transform(rows, ctx):\n"
        "    r = ctx.call('Employee - find by email', query={'filter_keys': 'employee_email', 'value': 'william.thaddeus@moladin.com'})\n"
        "    return [{'label': 'x', 'body': {'id': r['data']['items'][0]['id']}}]\n")})
    dr = c.post(f"/api/requests/{rid}/dry-run", json={"upload_id": None}).json()
    assert dr["ok"], dr
    assert dr["items"][0]["body"] == {"id": "88032f68-0302-4c2a-8fcb-4c47898cbb6e"}
    look = [r for r in c.srv.requests if r["path"] == "/api/v1/employee/list"][0]
    assert look["query"] == {"page": "1", "filter_keys": "employee_email", "value": "william.thaddeus@moladin.com"}
    assert look["headers"]["authorization"] == "Bearer tok-123456"
    assert any(l.startswith("[ctx.call] Employee - find by email") for l in dr["logs"])


def test_empty_token_blocks_send(client):
    c, rid = client, client.req["Employee - create"]
    up = c.post(f"/api/requests/{rid}/use-sample").json()
    dr = c.post(f"/api/requests/{rid}/dry-run", json={"upload_id": up["upload"]["id"]}).json()
    assert dr["ok"] and len(dr["items"]) == 1
    assert dr["items"][0]["body"]["phone_number"] == "08174949338"
    pf = c.get(f"/api/requests/{rid}/send-check").json()
    assert not pf["ok"] and any("Bearer token is empty" in b for b in pf["blockers"])
    r = c.post(f"/api/requests/{rid}/send", json={"mode": "send"})
    assert r.status_code == 409
    assert not [x for x in c.srv.requests if x["path"] == "/api/v1/employee/create"]


def test_changes_after_dry_run_require_new_dry_run(client):
    c, rid = client, client.req["Employee - create"]
    set_token(c, "tok-abcdef")
    up = c.post(f"/api/requests/{rid}/use-sample").json()
    dr = c.post(f"/api/requests/{rid}/dry-run", json={"upload_id": up["upload"]["id"]}).json()
    assert c.get(f"/api/requests/{rid}").json()["dry_run"]["valid"] is True
    script = c.get(f"/api/requests/{rid}").json()["script"]
    c.put(f"/api/requests/{rid}", json={"script": script + "\n# edited\n"})
    assert c.get(f"/api/requests/{rid}").json()["dry_run"]["valid"] is False
    r = c.post(f"/api/requests/{rid}/send", json={"mode": "send", "fingerprint": dr["fingerprint"]})
    assert r.status_code == 409 and "dry run again" in r.json()["detail"]
    # a different file also invalidates it
    c.put(f"/api/requests/{rid}", json={"script": script})
    assert c.get(f"/api/requests/{rid}").json()["dry_run"]["valid"] is True
    up2 = c.post(f"/api/requests/{rid}/upload", files={"file": ("x.xlsx", open(EMPLOYEE_XLSX, "rb").read() + b"")}).json()
    assert up2["upload"]["id"] != up["upload"]["id"]
    dr2 = c.post(f"/api/requests/{rid}/dry-run", json={"upload_id": up2["upload"]["id"]}).json()
    run = c.post(f"/api/requests/{rid}/send", json={"mode": "send_first", "fingerprint": dr2["fingerprint"]}).json()
    d = wait_run(c, run["id"])
    assert d["run"]["mode"] == "send_first" and d["run"]["total"] == 1 and d["run"]["success_count"] == 1


def test_skipped_items_need_send_valid_only(client):
    c, rid = client, client.req["Warehouse - create"]
    set_token(c, "tok-abcdef")
    script = c.get(f"/api/requests/{rid}").json()["script"]
    # add an extra valid item so there is something to send
    c.put(f"/api/requests/{rid}", json={"script": script.replace(
        "    return items", "    items.append({'label': 'extra', 'body': {'warehouse_name': 'x'}})\n    return items")})
    up = c.post(f"/api/requests/{rid}/use-sample").json()
    dr = c.post(f"/api/requests/{rid}/dry-run", json={"upload_id": up["upload"]["id"]}).json()
    assert dr["skipped"][0]["reason"].startswith("latitude -12839392.0") and len(dr["items"]) == 1
    pf = c.get(f"/api/requests/{rid}/send-check").json()
    assert not pf["ok"] and "send valid items only" in pf["blockers"][0]
    assert c.get(f"/api/requests/{rid}/send-check?send_valid_only=true").json()["ok"]


def test_dry_run_blocks_non_get_ctx_call(client):
    c, rid = client, client.req["Employee - create"]
    set_token(c, "tok-abcdef")
    c.put(f"/api/requests/{rid}", json={"script": (
        "def transform(rows, ctx):\n"
        "    ctx.call('Warehouse - create', body={'x': 1})\n"
        "    return []\n")})
    up = c.post(f"/api/requests/{rid}/use-sample").json()
    dr = c.post(f"/api/requests/{rid}/dry-run", json={"upload_id": up["upload"]["id"]}).json()
    assert not dr["ok"] and dr["error"].startswith("DryRunBlocked") and dr["line"] == 2
    assert not [x for x in c.srv.requests if x["method"] == "POST"]


def test_manual_authorization_header_rejected(client):
    c, rid = client, client.req["Employee - create"]
    r = c.put(f"/api/requests/{rid}", json={"headers": [{"key": "Authorization", "value": "Bearer x"}]})
    assert r.status_code == 400 and "Auth tab" in r.json()["detail"]
    assert c.put(f"/api/requests/{rid}", json={"headers": [{"key": "X-Trace", "value": "1"}]}).status_code == 200


def test_request_override_token_is_sent(client):
    c, rid = client, client.req["Employee - create"]
    set_token(c, "folder-token")
    c.put(f"/api/requests/{rid}", json={"auth": {"type": "bearer", "token": "override-token-1"}})
    d = c.get(f"/api/requests/{rid}").json()
    assert d["auth"]["token"] is None and d["auth"]["has_token"]       # literal never echoed back
    assert d["effective_auth"]["source"] == "request"
    up = c.post(f"/api/requests/{rid}/use-sample").json()
    dr = c.post(f"/api/requests/{rid}/dry-run", json={"upload_id": up["upload"]["id"]}).json()
    run = c.post(f"/api/requests/{rid}/send", json={"mode": "send", "fingerprint": dr["fingerprint"]}).json()
    wait_run(c, run["id"])
    sent = [x for x in c.srv.requests if x["path"] == "/api/v1/employee/create"]
    assert sent[-1]["headers"]["authorization"] == "Bearer override-token-1"


def test_retry_failed_only_resends_failed_items(client):
    c, rid = client, client.req["Employee - create"]
    set_token(c, "tok-abcdef")
    c.put(f"/api/requests/{rid}", json={"script": (
        "def transform(rows, ctx):\n"
        "    return [{'label': e, 'body': {'employee_email': e}} for e in ('a@x', 'dup@x', 'b@x')]\n")})
    dr = c.post(f"/api/requests/{rid}/dry-run", json={"upload_id": None}).json()
    assert dr["ok"] and len(dr["items"]) == 3 and dr["upload"] is None
    run = c.post(f"/api/requests/{rid}/send", json={"mode": "send", "fingerprint": dr["fingerprint"]}).json()
    d = wait_run(c, run["id"])
    assert d["run"]["success_count"] == 2 and d["run"]["failed_count"] == 1 and d["can_retry_failed"]
    failed = [i for i in d["items"] if not i["success"]][0]
    assert failed["http_status"] == 200 and failed["message"] == "code 409 != 200: employee email already exists"
    assert c.get(f"/api/runs/{run['id']}/export.csv").text.count("employee email already exists") == 1
    retry = c.post(f"/api/runs/{run['id']}/retry-failed").json()
    assert retry["mode"] == "retry_failed" and retry["total"] == 1
    d2 = wait_run(c, retry["id"])
    assert [i["label"] for i in d2["items"]] == ["dup@x"]
    sent = [x["json"]["employee_email"] for x in c.srv.requests if x["path"] == "/api/v1/employee/create"]
    assert sent == ["a@x", "dup@x", "b@x", "dup@x"]


def test_tree_crud_and_move(client):
    c = client
    top = c.post("/api/folders", json={"name": "Other"}).json()
    sub = c.post("/api/folders", json={"name": "Sub", "parent_id": top["id"]}).json()
    rq = c.post("/api/requests", json={"folder_id": sub["id"], "name": "R"}).json()
    assert c.post("/api/requests", json={"folder_id": sub["id"], "name": "R"}).json()["name"] == "R (2)"
    dup = c.post(f"/api/requests/{rq['id']}/duplicate").json()
    assert dup["name"] == "R copy"
    assert c.post("/api/move", json={"kind": "folder", "id": top["id"], "parent_id": sub["id"]}).status_code == 400
    assert c.post("/api/move", json={"kind": "request", "id": rq["id"], "parent_id": top["id"], "index": 0}).status_code == 200
    t = {f["name"]: f for f in c.get("/api/tree").json()["tree"]}
    assert t["Other"]["requests"][0]["name"] == "R"
    assert c.patch(f"/api/folders/{sub['id']}", json={"name": "Renamed"}).json()["name"] == "Renamed"
    c.delete(f"/api/folders/{top['id']}")
    assert "Other" not in {f["name"] for f in c.get("/api/tree").json()["tree"]}


def test_upload_limits(client):
    c, rid = client, client.req["Employee - create"]
    big = b"PK" + b"0" * (10 * 1024 * 1024)
    assert c.post(f"/api/requests/{rid}/upload", files={"file": ("big.xlsx", big)}).status_code == 413
    r = c.post(f"/api/requests/{rid}/upload", files={"file": ("x.csv", b"a,b\n")})
    assert r.status_code == 400 and "xlsx" in r.json()["detail"]


def test_header_row_beyond_end_is_reset_to_1_on_upload(client):
    c, rid = client, client.req["Employee - create"]
    assert c.put(f"/api/requests/{rid}", json={"data_source": {"header_row": 100}}).status_code == 200
    up = c.post(f"/api/requests/{rid}/upload",
                files={"file": ("e.xlsx", EMPLOYEE_XLSX.read_bytes())}).json()
    assert up["preview"]["header_row"] == 1 and up["preview"]["header_row_reset_from"] == 100
    assert up["preview"]["total_rows"] > 0
    assert c.get(f"/api/requests/{rid}").json()["data_source"]["header_row"] == 1   # saved on the request
    # typing a too-large header row later: the preview falls back and saves 1 again
    c.put(f"/api/requests/{rid}", json={"data_source": {"header_row": 100}})
    pv = c.get(f"/api/uploads/{up['upload']['id']}/preview",
               params={"header_row": 100, "request_id": rid}).json()["preview"]
    assert pv["header_row"] == 1 and pv["header_row_reset_from"] == 100
    assert c.get(f"/api/requests/{rid}").json()["data_source"]["header_row"] == 1
    dr = c.post(f"/api/requests/{rid}/dry-run", json={"upload_id": up["upload"]["id"]}).json()
    assert dr["ok"]
