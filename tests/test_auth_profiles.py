"""Auth profiles: tokens fetched from a (fake, local) OAuth2 token endpoint."""
import asyncio
import base64
import json
import time
from urllib.parse import parse_qs

import pytest
from fastapi.testclient import TestClient

from app import db as appdb
from app import http_engine as he
from app import main, oauth
from app import services as svc
from app.runner import DONE, PAUSED, RunManager, RunState
from tests.fake_server import FakeServer

SECRET = "test-client-secret-123"


def jwt(exp_in=300, n=0):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{enc({'alg': 'none'})}.{enc({'exp': int(time.time()) + exp_in, 'n': n})}.sig"


class TokenEndpoint:
    """Fake token endpoint + API. Issues token #1, #2, ... and accepts only the latest one (optionally)."""

    def __init__(self, expires_in=300, reject_first_token=False, api_status=None):
        self.issued: list[str] = []
        self.forms: list[dict] = []
        self.expires_in, self.reject_first, self.api_status = expires_in, reject_first_token, api_status

    def __call__(self, req):
        if req["path"] == "/token":
            form = {k: v[0] for k, v in parse_qs(req["json"] if isinstance(req["json"], str) else "").items()}
            self.forms.append({**form, "_ctype": req["headers"].get("content-type", "")})
            if form.get("client_secret") != SECRET:
                return 401, {"error": "unauthorized_client", "error_description": "Invalid client or Invalid client credentials"}
            tok = jwt(n=len(self.issued) + 1)
            self.issued.append(tok)
            return 200, {"access_token": tok, "expires_in": self.expires_in, "token_type": "Bearer"}
        auth = req["headers"].get("authorization", "")
        if self.api_status:
            return self.api_status, {"code": self.api_status, "msg": "nope"}
        if not self.issued or auth != f"Bearer {self.issued[-1]}" or (self.reject_first and len(self.issued) == 1):
            return 401, {"code": 401, "msg": "token is malformed", "data": None}
        return 200, {"code": 200, "msg": "ok", "data": {}}


@pytest.fixture(autouse=True)
def _fresh_cache():
    oauth.clear()
    yield
    oauth.clear()


def profile(url, secret=SECRET, pid=1):
    return oauth.ProfileSpec(id=pid, name="EAMS-Auth", token_url=f"{url}/token", client_id="eams-admin",
                             client_secret=secret)


# --------------------------------------------------------------------------- oauth module
def test_fetches_with_client_credentials_form_and_caches():
    ep = TokenEndpoint()
    with FakeServer(ep) as srv:
        a = oauth.get_token(profile(srv.url), {})
        b = oauth.get_token(profile(srv.url), {})
        c = oauth.get_token(profile(srv.url), {}, force=True)
    assert a.ok and not a.from_cache and b.from_cache and b.token == a.token
    assert c.token != a.token and len(ep.forms) == 2
    f = ep.forms[0]
    assert f["grant_type"] == "client_credentials" and f["client_id"] == "eams-admin" and f["client_secret"] == SECRET
    assert f["_ctype"].startswith("application/x-www-form-urlencoded")
    st = oauth.cached_status(1)
    assert st["length"] == len(c.token) and 250 < st["expires_in_s"] <= 300


def test_token_is_refreshed_before_it_expires():
    ep = TokenEndpoint(expires_in=2)   # margin = 10% of 2 s -> refresh after ~1.8 s
    with FakeServer(ep) as srv:
        oauth.get_token(profile(srv.url), {})
        time.sleep(1.9)
        r = oauth.get_token(profile(srv.url), {})
    assert not r.from_cache and len(ep.issued) == 2


def test_bad_secret_error_is_readable_and_masked():
    with FakeServer(TokenEndpoint()) as srv:
        r = oauth.get_token(profile(srv.url, secret="wrong-secret-xyz"), {})
    assert not r.ok and "HTTP 401" in r.error and "Invalid client" in r.error and "wrong-secret-xyz" not in r.error
    assert oauth.cached_status(1) is None


def test_variables_and_missing_fields():
    p = oauth.ProfileSpec(id=2, name="P", token_url="{{auth_url}}", client_id="", client_secret="")
    r = oauth.get_token(p, {})
    assert "auth_url" in r.error and "client_id is empty" in r.error and "client_secret is empty" in r.error


def test_prepare_uses_fetched_token_or_explains():
    spec = he.RequestSpec(id=1, name="r", method="POST", url="http://h/x", auth_profile=profile("http://h"))
    p = he.prepare(spec, {}, item={"body": {"a": 1}}, fetched_token=oauth.TokenResult(error="HTTP 401: nope"))
    assert not p.ok and p.token_problem and "auth profile 'EAMS-Auth'" in p.errors[0]
    p = he.prepare(spec, {}, item={"body": {"a": 1}}, fetched_token=oauth.TokenResult(token="abc"))
    assert p.ok and p.headers == {"Authorization": "Bearer abc", "Content-Type": "application/json"}
    cfg, src = he.resolve_auth({"type": "inherit"}, [{"type": "inherit"}, {"type": "profile", "profile_id": 3}])
    assert cfg["profile_id"] == 3 and src == "folder (+1 up)"


# --------------------------------------------------------------------------- runner
class ProfileBackend:
    def __init__(self, spec):
        self.spec, self.rows = spec, {}

    def prepare(self, item):
        return svc.prepare_request(self.spec, {}, item=item), [SECRET]

    def on_unauthorized(self):
        oauth.invalidate(self.spec.auth_profile.id)
        return True

    def persist_run(self, st):
        return None

    def persist_item(self, st, row):
        self.rows[row["seq"]] = dict(row)

    def after_finish(self, st):
        pass


def _run(url, n=2):
    spec = he.RequestSpec(id=1, name="r", method="POST", url=f"{url}/api/x", auth_profile=profile(url),
                          settings=he.normalize_settings({}))
    items = [{"seq": i + 1, "label": f"i{i + 1}", "body": {"n": i + 1}} for i in range(n)]
    st = RunState(request_id=1, request_name="r", environment_name="Dev", mode="send", method="POST",
                  url_display=url, items=items, settings=spec.settings)
    return st, ProfileBackend(spec)


async def test_run_fetches_once_and_refreshes_after_401():
    ep = TokenEndpoint(reject_first_token=True)
    with FakeServer(ep) as srv:
        st, b = _run(srv.url, n=3)
        await RunManager().start(st, b)
        await asyncio.wait_for(st.task, 20)
    assert st.status == DONE and st.success_count == 3
    assert len(ep.issued) == 2                       # first token rejected -> one refresh, then reused
    assert b.rows[1]["attempts"] == 2 and b.rows[2]["attempts"] == 1
    sent = [r["headers"]["authorization"] for r in srv.requests if r["path"] == "/api/x"]
    assert sent == [f"Bearer {ep.issued[0]}"] + [f"Bearer {ep.issued[1]}"] * 3


async def test_run_pauses_when_fresh_token_is_still_rejected():
    ep = TokenEndpoint(api_status=401)
    with FakeServer(ep) as srv:
        st, b = _run(srv.url, n=1)
        mgr = RunManager()
        await mgr.start(st, b)
        for _ in range(500):
            if st.status == PAUSED:
                break
            await asyncio.sleep(0.02)
        assert st.status == PAUSED and "EAMS-Auth" in st.banner and len(ep.issued) == 2
        mgr.stop(st.id)
        await asyncio.wait_for(st.task, 10)


async def test_run_pauses_when_token_cannot_be_fetched():
    with FakeServer(TokenEndpoint()) as srv:
        st, b = _run(srv.url, n=1)
        b.spec.auth_profile.client_secret = "wrong"
        mgr = RunManager()
        await mgr.start(st, b)
        for _ in range(500):
            if st.status == PAUSED:
                break
            await asyncio.sleep(0.02)
        assert st.pause_reason == "token" and "auth profile 'EAMS-Auth'" in st.banner
        assert "wrong" not in json.dumps(st.header()) and not [r for r in srv.requests if r["path"] == "/api/x"]
        mgr.stop(st.id)
        await asyncio.wait_for(st.task, 10)


# --------------------------------------------------------------------------- API (MySQL)
@pytest.fixture
def client(db):
    main.DRY_RUNS.clear()
    main.manager.runs.clear()
    with appdb.session_scope() as s:
        svc.seed_if_empty(s)
    ep = TokenEndpoint()
    with FakeServer(ep) as srv, TestClient(main.app) as c:
        env = c.get("/api/environments").json()["environments"][0]
        c.put(f"/api/environments/{env['id']}/variables",
              json={"variables": [{"key": "base_url", "value": srv.url},
                                  {"key": "bearer_token", "value": None, "is_secret": True}]})
        c.srv, c.ep = srv, ep
        c.tree = c.get("/api/tree").json()["tree"][0]
        c.req = {r["name"]: r["id"] for r in c.tree["requests"]}
        yield c


@pytest.mark.db
def test_profile_crud_never_returns_secret(client):
    c = client
    body = {"name": "EAMS-Auth", "token_url": f"{c.srv.url}/token", "client_id": "eams-admin",
            "grant_type": "client_credentials", "client_secret": SECRET}
    p = c.post("/api/auth-profiles", json=body).json()
    assert p["has_secret"] and SECRET not in json.dumps(p)
    assert c.post("/api/auth-profiles", json=body).status_code == 409                  # name taken
    assert c.post("/api/auth-profiles", json={**body, "name": "X", "client_secret": ""}).status_code == 400
    assert c.post("/api/auth-profiles", json={**body, "name": "Y", "grant_type": "password"}).status_code == 400
    assert SECRET not in c.get("/api/auth-profiles").text
    # empty secret on update keeps the saved one
    p2 = c.patch(f"/api/auth-profiles/{p['id']}", json={"client_secret": "", "client_id": "eams-admin"}).json()
    assert p2["has_secret"]
    t = c.post(f"/api/auth-profiles/{p['id']}/test").json()
    assert t["ok"] and t["token"]["jwt"] and t["token"]["length"] > 10 and c.ep.issued[0] not in json.dumps(t)
    with appdb.session_scope() as s:
        from app.models import AuthProfile
        assert s.get(AuthProfile, p["id"]).client_secret.startswith("enc:v1:")
    bad = c.patch(f"/api/auth-profiles/{p['id']}", json={"client_secret": "nope-123"}).json()
    t = c.post(f"/api/auth-profiles/{bad['id']}/test").json()
    assert not t["ok"] and "HTTP 401" in t["error"] and "nope-123" not in t["error"]


@pytest.mark.db
def test_folder_profile_is_used_for_sending(client):
    c = client
    p = c.post("/api/auth-profiles", json={"name": "EAMS-Auth", "token_url": f"{c.srv.url}/token",
                                           "client_id": "eams-admin", "client_secret": SECRET}).json()
    fid = c.tree["id"]
    assert c.patch(f"/api/folders/{fid}", json={"auth": {"type": "profile", "profile_id": 999}}).status_code == 400
    assert c.patch(f"/api/folders/{fid}", json={"auth": {"type": "profile", "profile_id": p["id"]}}).status_code == 200
    rid = c.req["Employee - create"]
    d = c.get(f"/api/requests/{rid}").json()
    ea = d["effective_auth"]
    assert ea["kind"] == "profile" and ea["profile"]["name"] == "EAMS-Auth" and ea["source"] == "folder"
    assert not c.ep.forms                                   # opening a request does not fetch a token
    c.put(f"/api/requests/{rid}", json={"script": (
        "def transform(rows, ctx):\n    return [{'label': e, 'body': {'employee_email': e}} for e in ('a@x', 'b@x')]\n")})
    dr = c.post(f"/api/requests/{rid}/dry-run", json={"upload_id": None}).json()
    chk = c.get(f"/api/requests/{rid}/send-check").json()
    assert chk["ok"] and "EAMS-Auth" in chk["auth_label"] and len(c.ep.issued) == 1
    curl = c.post(f"/api/requests/{rid}/curl", json={"index": 0}).json()["curl"]
    assert "Bearer ****" in curl and c.ep.issued[0] not in curl
    run = c.post(f"/api/requests/{rid}/send", json={"mode": "send", "fingerprint": dr["fingerprint"]}).json()
    for _ in range(300):
        rd = c.get(f"/api/runs/{run['id']}").json()
        if rd["run"]["status"] not in ("Running", "Paused"):
            break
        time.sleep(0.05)
    assert rd["run"]["status"] == "Done" and rd["run"]["success_count"] == 2
    assert len(c.ep.issued) == 1                            # cached token reused for every item
    assert c.ep.issued[0] not in json.dumps(rd)
    # in use -> cannot delete; export carries only the profile name
    assert c.delete(f"/api/auth-profiles/{p['id']}").status_code == 409
    doc = c.get(f"/api/folders/{fid}/export").json()
    assert doc["folder"]["auth"] == {"type": "profile", "profile": "EAMS-Auth"}
    assert SECRET not in json.dumps(doc) and c.srv.url + "/token" not in json.dumps(doc)
    imp = c.post("/api/import", json=doc).json()
    assert imp["auth"]["type"] == "profile" and imp["auth"]["profile_id"] == p["id"]
