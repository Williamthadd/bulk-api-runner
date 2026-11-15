import time

import pytest

from app.sandbox import CallFailed, DryRunBlocked, run_script
from tests.helpers import WAREHOUSE_XLSX


def run(src, **kw):
    kw.setdefault("timeout_s", 20)
    return run_script(src, kw.pop("xlsx", None), **kw)


def test_basic_items_logs_skip_warn():
    src = '''
def transform(rows, ctx):
    ctx.log("hello")
    print("printed", 1)
    ctx.skip("row 9", "bad")
    ctx.warn("x", "careful")
    return [{"label": "x", "body": {"a": 1}, "query": {"q": 1}}, {"body": None}]
'''
    r = run(src)
    assert r.ok, r.error
    assert r.items == [{"label": "x", "body": {"a": 1}, "query": {"q": 1}}, {"label": "item 2", "body": None}]
    assert r.logs == ["hello", "printed 1"]
    assert r.skipped == [{"label": "row 9", "reason": "bad"}]
    assert r.warnings == [{"label": "x", "msg": "careful"}]


def test_rows_from_xlsx_have_native_types():
    src = '''
def transform(rows, ctx):
    r = rows[0]
    return [{"label": type(r["latitude"]).__name__, "body": {"row": r["_row"]}}]
'''
    r = run(src, xlsx=str(WAREHOUSE_XLSX))
    assert r.ok and r.rows_read == 1
    assert r.items[0] == {"label": "float", "body": {"row": 2}}


def test_exception_reports_script_line():
    src = "def transform(rows, ctx):\n    x = 1\n    return 1 / 0\n"
    r = run(src)
    assert not r.ok
    assert r.error.startswith("ZeroDivisionError")
    assert r.line == 3
    assert 'line 3' in r.traceback and "return 1 / 0" in r.traceback
    assert "worker.py" not in r.traceback


def test_syntax_error_line():
    r = run("def transform(rows, ctx):\n    return [\n\nx = (\n")
    assert not r.ok and r.error.startswith("SyntaxError") and r.line is not None


def test_missing_transform_and_bad_return():
    assert "transform(rows, ctx)" in run("x = 1").error
    assert "must return a list" in run("def transform(rows, ctx):\n    return {}").error
    r = run("import datetime\ndef transform(rows, ctx):\n    return [{'body': {'d': datetime.date.today()}}]")
    assert not r.ok and "not JSON serializable" in r.error
    assert "unknown keys" in run("def transform(rows, ctx):\n    return [{'bdy': 1}]").error


def test_timeout_kills_process():
    t0 = time.monotonic()
    r = run("def transform(rows, ctx):\n    while True: pass\n", timeout_s=2)
    assert not r.ok and "timed out" in r.error
    assert time.monotonic() - t0 < 10


def test_hard_exit_is_reported():
    r = run("import os\ndef transform(rows, ctx):\n    os._exit(7)\n")
    assert not r.ok and "exited unexpectedly" in r.error and "code 7" in r.error


def test_vars_read_only():
    src = '''
def transform(rows, ctx):
    try:
        ctx.vars["x"] = 2
    except TypeError:
        return [{"label": ctx.vars["x"], "body": None}]
'''
    r = run(src, variables={"x": "1"})
    assert r.ok and r.items[0]["label"] == "1"


def test_ctx_call_rpc_roundtrip():
    calls = []

    def handler(name, query, body, path):
        calls.append((name, query, body, path))
        if name == "fail":
            raise CallFailed("HTTP 500: boom")
        return {"data": {"items": [{"id": "abc"}]}}

    src = '''
def transform(rows, ctx):
    a = ctx.call("lookup", query={"value": "x"}, path="7")
    try:
        ctx.call("fail")
    except CallError as e:
        ctx.log(f"caught {e}")
    return [{"label": "l", "body": {"id": a["data"]["items"][0]["id"]}}]
'''
    r = run(src, call_handler=handler)
    assert r.ok, r.error
    assert r.items[0]["body"] == {"id": "abc"}
    assert calls[0] == ("lookup", {"value": "x"}, None, "7")
    assert "caught HTTP 500: boom" in r.logs
    assert any(l.startswith("[ctx.call] lookup") and l.endswith("-> ok") for l in r.logs)


def test_dry_run_blocked_surfaces_as_exception():
    def handler(name, query, body, path):
        raise DryRunBlocked(f"'{name}' uses POST; only GET/HEAD are allowed during a dry run")

    r = run("def transform(rows, ctx):\n    ctx.call('Create thing', body={})\n    return []\n",
            call_handler=handler)
    assert not r.ok and r.error.startswith("DryRunBlocked") and r.line == 2
    r = run("def transform(rows, ctx):\n    try:\n        ctx.call('x')\n    except DryRunBlocked:\n"
            "        return [{'label':'blocked','body':None}]\n", call_handler=handler)
    assert r.ok and r.items[0]["label"] == "blocked"
