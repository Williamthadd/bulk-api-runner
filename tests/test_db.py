"""Database behaviour against a real MySQL (db-test service)."""
import asyncio
import json
from datetime import timedelta

import pytest
from sqlalchemy import inspect, select, text

from app import crypto, services as svc
from app import db as appdb
from app.models import Environment, EnvironmentVariable, Folder, Request, Run, RunItem, utcnow
from app.runner import RunManager, RunState
from tests.fake_server import FakeServer

pytestmark = pytest.mark.db


def test_migrations_upgrade_and_downgrade_from_empty(mysql_url):
    from tests.conftest import _reset_schema
    eng = appdb.make_engine(mysql_url)
    try:
        appdb.downgrade(mysql_url, "base")
        assert set(inspect(eng).get_table_names()) <= {"alembic_version"}
        appdb.migrate(mysql_url)
        names = set(inspect(eng).get_table_names())
        assert {"folders", "requests", "environments", "environment_variables", "runs", "run_items", "uploads", "auth_profiles"} <= names
        cols = {c["name"]: c for c in inspect(eng).get_columns("run_items")}
        assert "MEDIUMTEXT" in str(cols["response_body"]["type"]).upper()
        assert "DATETIME" in str(cols["created_at"]["type"]).upper()
        with eng.connect() as c:
            coll = c.execute(text("SELECT TABLE_COLLATION FROM information_schema.TABLES "
                                  "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME='requests'")).scalar()
            assert coll == "utf8mb4_0900_ai_ci"
            assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0002_auth_profiles"
    finally:
        eng.dispose()
        _reset_schema(mysql_url)


def test_seed_loaded_only_once(db):
    with appdb.session_scope() as s:
        assert svc.seed_if_empty(s) is True
    with appdb.session_scope() as s:
        tree = svc.tree(s)
        assert [f["name"] for f in tree] == ["employee-attendance-management-system"]
        root = tree[0]
        assert [r["name"] for r in root["requests"]] == [
            "Attendance - bulk schedule", "Warehouse - create", "Employee - create"]
        assert root["auth"]["type"] == "bearer" and root["auth"]["token"] == "{{bearer_token}}"
        reqs = s.scalars(select(Request)).all()
        assert all(r.auth_config["type"] == "inherit" for r in reqs)
        bulk = next(r for r in reqs if r.name == "Attendance - bulk schedule")
        assert "def transform" in bulk.script and bulk.settings["retry"]["max_retries"] == 3
        assert "ALLOW_MULTIPLE_SHIFTS_PER_DATE" in bulk.script            # the new bulk-schedule script
        wh = next(r for r in reqs if r.name == "Warehouse - create")
        assert "VALID_TIMEZONES" in wh.script and '"timezone": tz' in wh.script
        env, variables, secrets, _ = svc.env_runtime(s)
        assert env.name == "Development" and variables == {"base_url": "https://eams.development.mofi.id", "bearer_token": ""}
        bt = s.scalar(select(EnvironmentVariable).where(EnvironmentVariable.key == "bearer_token",
                                                        EnvironmentVariable.environment_id == env.id))
        assert bt.is_secret
    with appdb.session_scope() as s:
        # even after the user deletes every request, an existing folder prevents re-seeding
        for r in s.scalars(select(Request)).all():
            s.delete(r)
    with appdb.session_scope() as s:
        assert svc.seed_if_empty(s) is False
        assert s.scalar(select(Request.id)) is None
        assert len(s.scalars(select(Folder)).all()) == 1


def test_secrets_encrypted_at_rest(db):
    with appdb.session_scope() as s:
        e = svc.create_env(s, "Dev")
        svc.set_env_variables(s, e.id, [{"key": "bearer_token", "value": "tok-PLAIN-123", "is_secret": True},
                                        {"key": "base_url", "value": "http://x", "is_secret": False}])
        f = svc.create_folder(s, "c", auth={"type": "bearer", "token": "literal-TOKEN-xyz"})
        fid, eid = f.id, e.id
    with appdb.get_engine().connect() as c:
        raw = c.execute(text("SELECT value FROM environment_variables WHERE `key`='bearer_token'")).scalar()
        plain = c.execute(text("SELECT value FROM environment_variables WHERE `key`='base_url'")).scalar()
        auth = json.loads(c.execute(text(f"SELECT auth_config FROM folders WHERE id={fid}")).scalar())
    assert raw != "tok-PLAIN-123" and raw.startswith("enc:v1:") and crypto.decrypt(raw) == "tok-PLAIN-123"
    assert plain == "http://x"
    assert auth["token"].startswith("enc:v1:") and crypto.decrypt(auth["token"]) == "literal-TOKEN-xyz"
    with appdb.session_scope() as s:
        d = svc.env_dict(svc.get_env(s, eid))
        sec = next(v for v in d["variables"] if v["key"] == "bearer_token")
        assert sec["value"] is None and sec["has_value"]
        assert svc.folder_dict(svc.get_folder(s, fid))["auth"]["token"] is None   # literal never returned
        # saving the table with value=None keeps the secret
        svc.set_env_variables(s, eid, [{"key": "bearer_token", "value": None, "is_secret": True},
                                       {"key": "base_url", "value": "http://y"}])
    with appdb.session_scope() as s:
        assert svc.env_runtime(s, eid)[1]["bearer_token"] == "tok-PLAIN-123"
    # lost key: values become unreadable and must be re-entered
    crypto.init(crypto.Fernet.generate_key())
    with appdb.session_scope() as s:
        d = svc.env_dict(svc.get_env(s, eid))
        assert next(v for v in d["variables"] if v["key"] == "bearer_token")["unreadable"]
        assert svc.env_runtime(s, eid)[3] == ["bearer_token"]


def test_export_excludes_secrets_and_imports(db):
    with appdb.session_scope() as s:
        f = svc.create_folder(s, "col", auth={"type": "bearer", "token": "{{bearer_token}}"})
        sub = svc.create_folder(s, "sub", f.id, auth={"type": "bearer", "token": "LITERAL-secret-1"})
        svc.create_request(s, sub.id, "r1", method="POST", url="{{base_url}}/x",
                           auth={"type": "bearer", "token": "LITERAL-secret-2"}, script="def transform(r, c): return []")
        doc = svc.export_folder(s, f.id)
    dumped = json.dumps(doc)
    assert "LITERAL-secret" not in dumped and "enc:v1" not in dumped
    assert doc["folder"]["auth"]["token"] == "{{bearer_token}}"
    with appdb.session_scope() as s:
        g = svc.import_collection(s, doc)
        assert g.name == "col (2)"
        t = [n for n in svc.tree(s) if n["id"] == g.id][0]
        assert t["folders"][0]["requests"][0]["name"] == "r1"


def test_cascade_delete_folder_to_requests(db):
    with appdb.session_scope() as s:
        f = svc.create_folder(s, "top")
        sub = svc.create_folder(s, "sub", f.id)
        r1 = svc.create_request(s, f.id, "a")
        r2 = svc.create_request(s, sub.id, "b")
        run = Run(request_id=r2.id, request_name="b", mode="send", status="Done")
        s.add(run)
        s.flush()
        s.add(RunItem(run_id=run.id, seq=1, method="POST", url="u"))
        ids = (f.id, sub.id, r1.id, r2.id, run.id)
    with appdb.session_scope() as s:
        svc.delete_folder(s, ids[0])
    with appdb.session_scope() as s:
        assert s.get(Folder, ids[1]) is None
        assert s.get(Request, ids[2]) is None and s.get(Request, ids[3]) is None
        run = s.get(Run, ids[4])
        assert run is not None and run.request_id is None and run.request_name == "b"  # history kept
        assert s.scalar(select(RunItem.id).where(RunItem.run_id == ids[4])) is not None


def test_run_retention(db):
    with appdb.session_scope() as s:
        f = svc.create_folder(s, "f")
        r = svc.create_request(s, f.id, "r")
        now = utcnow()
        for i in range(25):
            run = Run(request_id=r.id, request_name="r", mode="send", status="Done",
                      started_at=now - timedelta(minutes=i))
            s.add(run)
            s.flush()
            s.add(RunItem(run_id=run.id, seq=1, method="POST", url="u"))
        old = Run(request_id=r.id, request_name="r", mode="send", status="Done", started_at=now - timedelta(days=31))
        s.add(old)
        rid = r.id
    with appdb.session_scope() as s:
        removed = svc.apply_retention(s, rid, max_runs=20, days=30)
        assert removed == 6
    with appdb.session_scope() as s:
        runs = s.scalars(select(Run).where(Run.request_id == rid)).all()
        assert len(runs) == 20
        assert min(r.started_at for r in runs) > utcnow() - timedelta(days=1)
        assert s.scalar(text("SELECT COUNT(*) FROM run_items")) == 20   # items cascaded
    with appdb.session_scope() as s:
        svc.apply_retention(s, rid, max_runs=5, days=30)
        assert len(s.scalars(select(Run).where(Run.request_id == rid)).all()) == 5


async def test_run_items_masked_and_truncated(db):
    secret = "tok-SUPER-SECRET-9"
    big = "x" * 100_000

    def handler(req):
        return 200, {"code": 200, "msg": f"hello {secret}", "data": {"echo": secret, "pad": big}}

    with FakeServer(handler) as srv:
        with appdb.session_scope() as s:
            e = svc.create_env(s, "Dev")
            svc.set_env_variables(s, e.id, [{"key": "base_url", "value": srv.url},
                                            {"key": "bearer_token", "value": secret, "is_secret": True}])
            f = svc.create_folder(s, "c", auth={"type": "bearer", "token": "{{bearer_token}}"})
            r = svc.create_request(s, f.id, "r", method="POST", url="{{base_url}}/api?k=" + secret)
            rid, eid, sett = r.id, e.id, svc.request_dict(r)["settings"]
        st = RunState(request_id=rid, request_name="r", environment_name="Dev", mode="send", method="POST",
                      url_display=srv.url, items=[{"seq": 1, "label": "a", "body": {"note": secret}}], settings=sett)
        mgr = RunManager()
        await mgr.start(st, svc.DbRunBackend(rid, eid))
        await asyncio.wait_for(st.task, 15)
    assert srv.requests[0]["headers"]["authorization"] == f"Bearer {secret}"
    with appdb.session_scope() as s:
        item = s.scalar(select(RunItem).where(RunItem.run_id == st.id))
        run = s.get(Run, st.id)
        assert run.status == "Done" and run.success_count == 1 and run.finished_at is not None
        for col in (item.request_body, item.response_body, item.message, item.url):
            assert secret not in col
        assert "authorization" not in item.request_body.lower()
        assert len(item.response_body.encode()) <= 64 * 1024 and "truncated" in item.response_body
        assert item.message == "hello ****"


def test_db_unavailable(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    bad = "mysql+pymysql://nobody:nopass@127.0.0.1:1/none?charset=utf8mb4"
    t0 = __import__("time").monotonic()
    with pytest.raises(ConnectionError):
        appdb.wait_for_db(appdb.make_engine(bad), timeout_s=1, interval_s=0.2)
    assert __import__("time").monotonic() - t0 < 10
    appdb.init_engine(bad)
    with TestClient(app) as c:
        r = c.get("/api/tree")
        assert r.status_code == 503 and r.json()["db_down"] is True
        assert c.get("/api/health").json()["db"] is False


async def test_runner_survives_db_outage(db):
    class Broken(svc.DbRunBackend):
        def persist_item(self, st, row):
            raise appdb.OperationalError("x", {}, Exception("gone"))

    with FakeServer() as srv:
        with appdb.session_scope() as s:
            e = svc.create_env(s, "Dev")
            svc.set_env_variables(s, e.id, [{"key": "base_url", "value": srv.url},
                                            {"key": "bearer_token", "value": "t0ken", "is_secret": True}])
            f = svc.create_folder(s, "c", auth={"type": "bearer", "token": "{{bearer_token}}"})
            r = svc.create_request(s, f.id, "r", method="POST", url="{{base_url}}/x")
            rid, eid, sett = r.id, e.id, svc.request_dict(r)["settings"]
        st = RunState(request_id=rid, request_name="r", environment_name="Dev", mode="send", method="POST",
                      url_display=srv.url, items=[{"seq": i, "body": {}} for i in (1, 2)], settings=sett)
        await RunManager().start(st, Broken(rid, eid))
        await asyncio.wait_for(st.task, 15)
    assert st.status == "Done" and st.success_count == 2
    assert st.unsaved_rows == 2 and st.db_error is None   # DB "came back" for the final run update


def test_uploads_expire_after_ttl(db):
    from tests.helpers import EMPLOYEE_XLSX
    from app.config import settings as cfg
    with appdb.session_scope() as s:
        old = svc.save_upload(s, None, "old.xlsx", EMPLOYEE_XLSX.read_bytes())
        new = svc.save_upload(s, None, "new.xlsx", EMPLOYEE_XLSX.read_bytes())
        old.created_at = utcnow() - timedelta(hours=cfg.upload_ttl_hours + 1)
        old_file = cfg.uploads_dir / old.stored_name
        new_id = new.id
    assert old_file.exists()
    with appdb.session_scope() as s:
        assert svc.cleanup_uploads(s) == 1
    with appdb.session_scope() as s:
        assert not old_file.exists()
        assert svc.upload_path(s, new_id)[1].exists()
        with pytest.raises(svc.NotFound):
            svc.upload_path(s, old.id)
