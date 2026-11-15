"""FastAPI application: JSON API + static UI."""
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, FastAPI, File, Query, Request as HttpRequest, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select

from . import crypto, db, oauth
from . import http_engine as he
from . import services as svc
from .config import ROOT, settings
from .models import Run, RunItem, utcnow
from .runner import ACTIVE, RunManager, RunState, public_row, row_summary
from .sandbox import CallFailed, DryRunBlocked, run_script
from .templating import mask_text, render
from .xlsx_loader import XlsxError, preview

log = logging.getLogger("bulk_runner")
STATIC = ROOT / "static"

manager = RunManager()
# request_id -> last dry-run result (kept in memory; a restart requires a new dry run)
DRY_RUNS: dict[int, dict] = {}


# --------------------------------------------------------------------------- helpers
def fingerprint(upload_sha: str, script: str, sheet: Any, header_row: Any, env_id: Any) -> str:
    raw = json.dumps([upload_sha, script, sheet, header_row, env_id], default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def current_fingerprint(s, r, upload_id: int | None) -> str | None:
    sha = ""  # a dry run without a file (scripts that only use ctx.call) is allowed
    if upload_id:
        try:
            u, _ = svc.upload_path(s, upload_id)
        except svc.NotFound:
            return None
        sha = u.sha256
    env = svc.active_env(s)
    ds = r.data_source or {}
    return fingerprint(sha, r.script or "", ds.get("sheet"), int(ds.get("header_row") or 1), env.id if env else None)


def dry_run_status(s, r) -> dict | None:
    d = DRY_RUNS.get(r.id)
    if not d:
        return None
    cur = current_fingerprint(s, r, d.get("upload_id"))
    return {"ok": d["ok"], "fingerprint": d["fingerprint"], "valid": bool(d["ok"] and cur == d["fingerprint"]),
            "upload_id": d.get("upload_id"), "items": len(d.get("items") or []),
            "skipped": len(d.get("skipped") or []), "created_at": d.get("created_at")}


def effective_auth(s, r) -> dict:
    spec, _ = svc.build_spec(s, r)
    env, variables, _, unreadable = svc.env_runtime(s)
    tmpl = spec.token_template
    source_name = None
    if spec.token_source.startswith("folder"):
        for f in svc.folder_chain(s, r.folder_id):
            if svc.plain_auth(f.auth_config).get("type") in ("bearer", "profile"):
                source_name = f.name
                break
    if spec.auth_profile is not None:
        ap = spec.auth_profile
        _, problems = oauth.resolve(ap, variables)
        # no network call here: only what is already cached (the token is fetched when it is needed)
        return {"kind": "profile", "profile": {"id": ap.id, "name": ap.name}, "profile_problems": problems,
                "token_info": oauth.cached_status(ap.id), "source": spec.token_source, "source_name": source_name,
                "display": f"auth profile '{ap.name}'", "configured": True, "empty": bool(problems),
                "missing_vars": [], "unreadable_vars": unreadable}
    resolved, missing = render(tmpl or "", variables)
    resolved = he.normalize_token(resolved)
    return {
        "kind": "bearer" if tmpl is not None else "none",
        "token_info": he.token_info(resolved) if resolved and not missing else None,
        "source": spec.token_source, "source_name": source_name,
        "display": tmpl if tmpl and svc.is_pure_reference(tmpl) else ("••••••" if tmpl else None),
        "configured": tmpl is not None, "empty": not resolved.strip() or bool(missing),
        "missing_vars": sorted(missing), "unreadable_vars": unreadable,
    }


def make_call_handler(near_folder_id: int, env_id: int | None, dry_run: bool = True):
    def handler(name: str, query: Any, body: Any, path: Any) -> Any:
        with db.session_scope() as s:
            target = svc.find_request_by_name(s, str(name), near_folder_id)
            spec, tok_secrets = svc.build_spec(s, target)
            _, variables, secrets, _ = svc.env_runtime(s, env_id)
        secrets = secrets + tok_secrets
        if dry_run and spec.method not in ("GET", "HEAD"):
            raise DryRunBlocked(f"'{name}' is a {spec.method} request; only GET/HEAD requests may be called "
                                "during a dry run")
        if query is not None and not isinstance(query, dict):
            raise CallFailed("query must be a dict")
        p = svc.prepare_request(spec, variables, query=query, body=body, path=path)
        if not p.ok:
            raise CallFailed(f"'{name}': " + "; ".join(p.errors))
        res = he.send_sync(p, spec.settings["timeout_s"])
        ok, reason = he.evaluate(res, spec.settings["success"])
        if not ok:
            msg = he.response_message(res)
            raise CallFailed(mask_text(f"'{name}' failed ({reason}): {msg}", secrets + [p.token]))
        return res.json if res.json is not None else res.text
    return handler


def _err(status: int, msg: str, **extra) -> JSONResponse:
    return JSONResponse({"detail": msg, **extra}, status_code=status)


# --------------------------------------------------------------------------- app
async def _housekeeping() -> None:
    while True:
        try:
            def work():
                with db.session_scope() as s:
                    svc.cleanup_uploads(s)
            await asyncio.to_thread(work)
        except Exception as e:  # noqa: BLE001
            log.warning("upload cleanup failed: %s", type(e).__name__)
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    crypto.init()
    try:
        with db.session_scope() as s:
            n = svc.mark_interrupted_runs(s)
            if n:
                log.info("marked %d interrupted run(s) as Stopped", n)
    except db.DB_ERRORS:
        log.warning("database unavailable at startup")
    task = asyncio.create_task(_housekeeping())
    yield
    task.cancel()
    for st in list(manager.runs.values()):
        if st.status in ACTIVE:
            manager.stop(st.id)


app = FastAPI(title="Bulk API Runner", lifespan=lifespan, docs_url="/api/docs", redoc_url=None)


@app.exception_handler(svc.NotFound)
async def _nf(_, e):
    return _err(404, str(e))


@app.exception_handler(svc.Conflict)
async def _cf(_, e):
    return _err(409, str(e))


@app.exception_handler(svc.Invalid)
async def _inv(_, e):
    return _err(400, str(e))


@app.exception_handler(XlsxError)
async def _xl(_, e):
    return _err(400, str(e))


@app.exception_handler(crypto.SecretUnreadable)
async def _sec(_, e):
    return _err(409, "A stored secret cannot be decrypted (encryption key changed or lost). Re-enter it.")


for _exc in db.DB_ERRORS:
    @app.exception_handler(_exc)
    async def _dberr(_, e):  # noqa: F811
        return _err(503, "Database unavailable. Is MySQL running? (docker compose up -d db)", db_down=True)


# --------------------------------------------------------------------------- health / static
@app.get("/api/health")
async def health():
    ok = await asyncio.to_thread(db.ping)
    return {"db": ok, "location": settings.db_location()}


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")


# --------------------------------------------------------------------------- collection tree
class FolderIn(BaseModel):
    name: str
    parent_id: int | None = None


class RequestIn(BaseModel):
    folder_id: int
    name: str = "New request"
    method: str = "GET"


class MoveIn(BaseModel):
    kind: str
    id: int
    parent_id: int | None = None
    index: int | None = None


@app.get("/api/tree")
def get_tree():
    with db.session_scope() as s:
        return {"tree": svc.tree(s)}


@app.post("/api/folders")
def create_folder(body: FolderIn):
    with db.session_scope() as s:
        f = svc.create_folder(s, body.name, body.parent_id,
                              auth={"type": "bearer" if body.parent_id is None else "inherit",
                                    "token": "{{bearer_token}}" if body.parent_id is None else None})
        return svc.folder_dict(f)


@app.get("/api/folders/{fid}")
def get_folder(fid: int):
    with db.session_scope() as s:
        f = svc.get_folder(s, fid)
        d = svc.folder_dict(f)
        d["path"] = [x.name for x in reversed(svc.folder_chain(s, f.id))]
        parent_tok, parent_profile = None, None
        if f.parent_id:
            cfg, _ = he.resolve_auth({}, [svc.plain_auth(x.auth_config) for x in svc.folder_chain(s, f.parent_id)])
            if cfg and cfg["type"] == "bearer":
                tmpl = cfg.get("token") or ""
                parent_tok = tmpl if tmpl and svc.is_pure_reference(tmpl) else ("••••••" if tmpl else None)
            elif cfg and cfg["type"] == "profile":
                ap = s.get(svc.AuthProfile, cfg.get("profile_id"))
                parent_profile = ap.name if ap else None
        d["inherited_token_display"] = parent_tok
        d["inherited_profile"] = parent_profile
        return d


@app.patch("/api/folders/{fid}")
def patch_folder(fid: int, body: dict = Body(...)):
    with db.session_scope() as s:
        return svc.folder_dict(svc.update_folder(s, fid, body))


@app.delete("/api/folders/{fid}")
def delete_folder(fid: int):
    with db.session_scope() as s:
        svc.delete_folder(s, fid)
    return {"ok": True}


@app.post("/api/folders/{fid}/duplicate")
def duplicate_folder(fid: int):
    with db.session_scope() as s:
        return svc.folder_dict(svc.duplicate_folder(s, fid))


@app.get("/api/folders/{fid}/export")
def export_folder(fid: int):
    with db.session_scope() as s:
        doc = svc.export_folder(s, fid)
    name = "".join(c if c.isalnum() or c in "-_" else "_" for c in doc["folder"]["name"]) or "collection"
    return Response(json.dumps(doc, indent=2, ensure_ascii=False), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{name}.collection.json"'})


@app.post("/api/import")
def import_collection(doc: dict = Body(...)):
    with db.session_scope() as s:
        return svc.folder_dict(svc.import_collection(s, doc))


@app.post("/api/move")
def move(body: MoveIn):
    with db.session_scope() as s:
        svc.move(s, body.kind, body.id, body.parent_id, body.index)
    return {"ok": True}


# --------------------------------------------------------------------------- requests
@app.post("/api/requests")
def create_request(body: RequestIn):
    with db.session_scope() as s:
        name = svc._unique_request_name(s, body.folder_id, (body.name or "New request").strip() or "New request")
        r = svc.create_request(s, body.folder_id, name, method=body.method)
        return svc.request_dict(r)


@app.get("/api/requests/{rid}")
def get_request(rid: int):
    with db.session_scope() as s:
        r = svc.get_request(s, rid)
        d = svc.request_dict(r)
        d["effective_auth"] = effective_auth(s, r)
        d["path"] = [x.name for x in reversed(svc.folder_chain(s, r.folder_id))]
        d["dry_run"] = dry_run_status(s, r)
        d["default_script_timeout_s"] = settings.script_timeout_s
        return d


@app.put("/api/requests/{rid}")
def put_request(rid: int, body: dict = Body(...)):
    with db.session_scope() as s:
        r = svc.update_request(s, rid, body)
        d = svc.request_dict(r)
        d["effective_auth"] = effective_auth(s, r)
        d["dry_run"] = dry_run_status(s, r)
        return d


@app.delete("/api/requests/{rid}")
def delete_request(rid: int):
    with db.session_scope() as s:
        svc.delete_request(s, rid)
    DRY_RUNS.pop(rid, None)
    return {"ok": True}


@app.post("/api/requests/{rid}/duplicate")
def duplicate_request(rid: int):
    with db.session_scope() as s:
        return svc.request_dict(svc.duplicate_request(s, rid))


# --------------------------------------------------------------------------- data (uploads)
def _preview_payload(s, u, path, sheet, header_row, r=None) -> dict:
    p = preview(str(path), sheet or None, int(header_row or 1))
    if p.get("header_row_reset_from") and r is not None:
        # the saved header row does not exist in this file: store the fallback (row 1) on the request
        r.data_source = {**(r.data_source or {}), "header_row": p["header_row"]}
    return {"upload": svc.upload_dict(u), "preview": p}


@app.post("/api/requests/{rid}/upload")
async def upload(rid: int, file: UploadFile = File(...)):
    limit = settings.upload_max_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        return _err(413, f"File is larger than {settings.upload_max_mb} MB")

    def work():
        with db.session_scope() as s:
            r = svc.get_request(s, rid)
            u = svc.save_upload(s, rid, file.filename or "upload.xlsx", data)
            _, path = svc.upload_path(s, u.id)
            ds = r.data_source or {}
            try:
                payload = _preview_payload(s, u, path, ds.get("sheet"), ds.get("header_row") or 1, r)
            except XlsxError:
                # the stored sheet may not exist in the new file: fall back to the first sheet
                r.data_source = {**ds, "sheet": None}
                payload = _preview_payload(s, u, path, None, ds.get("header_row") or 1, r)
            return payload
    return await asyncio.to_thread(work)


@app.post("/api/requests/{rid}/use-sample")
async def use_sample(rid: int):
    def work():
        with db.session_scope() as s:
            r = svc.get_request(s, rid)
            rel = (r.data_source or {}).get("sample_file")
            if not rel:
                raise svc.NotFound("This request has no sample file")
            p = svc.sample_path(rel)
            u = svc.save_upload(s, rid, p.name, p.read_bytes())
            _, path = svc.upload_path(s, u.id)
            ds = r.data_source or {}
            return _preview_payload(s, u, path, ds.get("sheet"), ds.get("header_row") or 1, r)
    return await asyncio.to_thread(work)


@app.get("/api/uploads/{uid}/preview")
async def upload_preview(uid: int, sheet: str | None = None, header_row: int = 1, request_id: int | None = None):
    def work():
        with db.session_scope() as s:
            u, path = svc.upload_path(s, uid)
            r = svc.get_request(s, request_id) if request_id else None
            return _preview_payload(s, u, path, sheet, header_row, r)
    return await asyncio.to_thread(work)


# --------------------------------------------------------------------------- dry run / curl
class DryRunIn(BaseModel):
    upload_id: int | None = None


@app.post("/api/requests/{rid}/dry-run")
async def dry_run(rid: int, body: DryRunIn):
    def load():
        with db.session_scope() as s:
            r = svc.get_request(s, rid)
            u, path = svc.upload_path(s, body.upload_id) if body.upload_id else (None, None)
            env = svc.active_env(s)
            ds = r.data_source or {}
            sett = he.normalize_settings(r.settings)
            return dict(script=r.script or "", path=str(path) if path else None, sheet=ds.get("sheet"),
                        header_row=int(ds.get("header_row") or 1), folder_id=r.folder_id,
                        env_id=env.id if env else None, env_name=env.name if env else None,
                        variables=svc.script_vars(s, env.id if env else None),
                        timeout=float(sett.get("script_timeout_s") or settings.script_timeout_s),
                        fp=current_fingerprint(s, r, body.upload_id),
                        upload=svc.upload_dict(u) if u else None)
    ctx = await asyncio.to_thread(load)
    res = await asyncio.to_thread(
        run_script, ctx["script"], ctx["path"], sheet=ctx["sheet"], header_row=ctx["header_row"],
        variables=ctx["variables"], call_handler=make_call_handler(ctx["folder_id"], ctx["env_id"], dry_run=True),
        timeout_s=ctx["timeout"], memory_mb=settings.script_memory_mb, dry_run=True)
    out = res.to_dict()
    for i, it in enumerate(out["items"], start=1):
        it["seq"] = i
    out.update(fingerprint=ctx["fp"], upload_id=body.upload_id, upload=ctx["upload"], env_id=ctx["env_id"],
               environment=ctx["env_name"], created_at=utcnow().isoformat(timespec="milliseconds") + "Z")
    DRY_RUNS[rid] = out
    return out


@app.get("/api/requests/{rid}/dry-run")
def last_dry_run(rid: int):
    d = DRY_RUNS.get(rid)
    if not d:
        return {"result": None}
    with db.session_scope() as s:
        r = svc.get_request(s, rid)
        status = dry_run_status(s, r)
    return {"result": d, "status": status}


class CurlIn(BaseModel):
    index: int


@app.post("/api/requests/{rid}/curl")
def curl(rid: int, body: CurlIn):
    d = DRY_RUNS.get(rid)
    if not d or not d.get("ok"):
        raise svc.Invalid("Run a successful dry run first")
    items = d.get("items") or []
    if not 0 <= body.index < len(items):
        raise svc.Invalid("Item index out of range")
    with db.session_scope() as s:
        r = svc.get_request(s, rid)
        spec, tok_secrets = svc.build_spec(s, r)
        _, variables, secrets, _ = svc.env_runtime(s)
    p = svc.prepare_request(spec, variables, item=items[body.index])
    return {"curl": he.to_curl(p, secrets + tok_secrets + ([p.token] if p.token else [])),
            "errors": p.errors, "warnings": p.warnings}


# --------------------------------------------------------------------------- send
def _preflight(s, rid: int, mode: str, send_valid_only: bool, fp: str | None) -> dict:
    r = svc.get_request(s, rid)
    blockers: list[str] = []
    warnings: list[str] = []
    d = DRY_RUNS.get(rid)
    env, variables, secrets, unreadable = svc.env_runtime(s)
    spec, tok_secrets = svc.build_spec(s, r)
    items = (d or {}).get("items") or []
    skipped = (d or {}).get("skipped") or []
    if not d:
        blockers.append("Run a dry run first.")
    elif not d.get("ok"):
        blockers.append("The last dry run failed. Fix the script and run it again.")
    else:
        cur = current_fingerprint(s, r, d.get("upload_id"))
        if cur != d["fingerprint"] or (fp and fp != d["fingerprint"]):
            blockers.append("The file, script, sheet/header row or environment changed since the last dry run. "
                            "Run the dry run again.")
        if not items:
            blockers.append("The dry run produced no items to send.")
        if skipped and mode == "send" and not send_valid_only:
            blockers.append(f"{len(skipped)} item(s) were skipped. Tick 'send valid items only' to send the rest.")
    if mode not in ("send", "send_first"):
        blockers.append("Unknown mode")
    if env is None:
        blockers.append("No active environment. Create or select one in the top bar.")
    if unreadable:
        blockers.append(f"Secret variable(s) {', '.join(unreadable)} cannot be decrypted (key changed). Re-enter them.")
    # with an auth profile this fetches (or reuses) a token, so bad credentials show up before sending
    p = svc.prepare_request(spec, variables, item=items[0] if items else {"body": None})
    info = he.token_info(p.token) if p.token and spec.auth_profile is None else {}
    if info.get("expired"):
        warnings.append(f"The Bearer token expired at {info['exp']} (UTC). The server will most likely answer 401; "
                        "paste a fresh token first.")
    elif info.get("expires_in_s") is not None and info["expires_in_s"] < 300:
        warnings.append(f"The Bearer token expires in {max(1, info['expires_in_s'] // 60)} minute(s).")
    for e in p.errors:
        if e not in blockers:
            blockers.append(e)
    warnings.extend(dict.fromkeys(p.warnings))
    if d and d.get("warnings"):
        warnings.append(f"The dry run reported {len(d['warnings'])} warning(s); those items are still sent.")
    if not spec.settings.get("idempotent") and spec.method in ("POST", "PATCH"):
        warnings.append("This request is not idempotent: sending the same items twice may create duplicates.")
    url, _ = render(spec.url, variables)
    count = (1 if items else 0) if mode == "send_first" else len(items)
    return {"ok": not blockers, "blockers": blockers, "warnings": warnings, "count": count,
            "skipped": len(skipped), "method": spec.method, "url": mask_text(url, secrets + tok_secrets),
            "environment": env.name if env else None, "token_source": spec.token_source,
            "auth_label": (f"auth profile '{spec.auth_profile.name}' (from {spec.token_source}, fetched automatically)"
                           if spec.auth_profile else f"Bearer token (from {spec.token_source})"),
            "request_name": r.name, "fingerprint": (d or {}).get("fingerprint"),
            "settings": spec.settings}


@app.get("/api/requests/{rid}/send-check")
def send_check(rid: int, mode: str = "send", send_valid_only: bool = False):
    with db.session_scope() as s:
        return _preflight(s, rid, mode, send_valid_only, None)


class SendIn(BaseModel):
    mode: str = "send"
    fingerprint: str | None = None
    send_valid_only: bool = False


@app.post("/api/requests/{rid}/send")
async def send(rid: int, body: SendIn):
    def check():
        with db.session_scope() as s:
            pf = _preflight(s, rid, body.mode, body.send_valid_only, body.fingerprint)
            env = svc.active_env(s)
            return pf, (env.id if env else None)
    pf, env_id = await asyncio.to_thread(check)
    if not pf["ok"]:
        return _err(409, pf["blockers"][0], blockers=pf["blockers"])
    if manager.active_run():
        return _err(409, "Another run is still in progress. Stop it or wait for it to finish.")
    d = DRY_RUNS[rid]
    items = [dict(it) for it in d["items"]]
    if body.mode == "send_first":
        items = items[:1]
    st = RunState(request_id=rid, request_name=pf["request_name"], environment_name=pf["environment"] or "",
                  mode=body.mode, method=pf["method"], url_display=pf["url"], items=items,
                  settings=pf["settings"], skipped_count=pf["skipped"] if body.mode == "send" else 0)
    await manager.start(st, svc.DbRunBackend(rid, env_id))
    return st.header()


# --------------------------------------------------------------------------- runs
def _live_rows(st: RunState) -> list[dict]:
    return [row_summary(st.rows[seq]) for seq in sorted(st.rows)]


@app.get("/api/runs")
def list_runs(request_id: int | None = None, limit: int = Query(50, le=500)):
    with db.session_scope() as s:
        q = select(Run).order_by(Run.started_at.desc(), Run.id.desc()).limit(limit)
        if request_id:
            q = q.where(Run.request_id == request_id)
        runs = [svc.run_dict(r) for r in s.scalars(q).all()]
    for r in runs:
        st = manager.runs.get(r["id"])
        if st:
            r.update(st.header())
    return {"runs": runs}


@app.get("/api/runs/{run_id}")
def get_run(run_id: int):
    st = manager.runs.get(run_id)
    if st:
        return {"run": st.header(), "items": _live_rows(st),
                "can_retry_failed": st.status not in ACTIVE and st.failed_count > 0}
    with db.session_scope() as s:
        run = s.get(Run, run_id)
        if not run:
            raise svc.NotFound(f"Run {run_id} not found")
        items = s.scalars(select(RunItem).where(RunItem.run_id == run_id).order_by(RunItem.seq)).all()
        return {"run": svc.run_dict(run), "items": [svc.item_dict(i) for i in items], "can_retry_failed": False}


@app.get("/api/runs/{run_id}/items/{seq}")
def get_run_item(run_id: int, seq: int):
    st = manager.runs.get(run_id)
    if st and seq in st.rows:
        row = public_row(st.rows[seq])
        row["accepted"] = svc.accepted_data(row.get("http_status"), row.get("response_body"))
        return row
    with db.session_scope() as s:
        it = s.scalar(select(RunItem).where(RunItem.run_id == run_id, RunItem.seq == seq))
        if not it:
            raise svc.NotFound("Item not found")
        return svc.item_dict(it, full=True)


@app.post("/api/runs/{run_id}/{action}")
async def run_action(run_id: int, action: str):
    st = manager.runs.get(run_id)
    if not st:
        return _err(404, "This run is not active in this app session")
    if action == "pause":
        manager.pause(run_id)
    elif action == "resume":
        manager.resume(run_id)
    elif action == "stop":
        manager.stop(run_id)
    elif action == "retry-failed":
        if st.status in ACTIVE:
            return _err(409, "The run is still active")
        if manager.active_run():
            return _err(409, "Another run is still in progress")
        failed = {seq for seq, row in st.rows.items() if not row.get("success")}
        items = [dict(it) for it in st.items if it["seq"] in failed]
        if not items:
            return _err(400, "There are no failed items to retry")

        def fresh():
            with db.session_scope() as s:
                r = svc.get_request(s, st.request_id) if st.request_id else None
                if not r:
                    raise svc.NotFound("The request of this run no longer exists")
                return he.normalize_settings(r.settings)
        sett = await asyncio.to_thread(fresh)
        new = RunState(request_id=st.request_id, request_name=st.request_name,
                       environment_name=st.environment_name, mode="retry_failed", method=st.method,
                       url_display=st.url_display, items=items, settings=sett)
        env_id = getattr(st.backend, "env_id", None)
        await manager.start(new, svc.DbRunBackend(st.request_id, env_id))
        return new.header()
    else:
        return _err(404, "Unknown action")
    return st.header()


@app.get("/api/runs/{run_id}/export.csv")
def export_csv(run_id: int):
    st = manager.runs.get(run_id)
    if st:
        rows = [public_row(st.rows[k]) for k in sorted(st.rows)]
        name = st.request_name
    else:
        with db.session_scope() as s:
            run = s.get(Run, run_id)
            if not run:
                raise svc.NotFound(f"Run {run_id} not found")
            name = run.request_name
            rows = [svc.item_dict(i) for i in
                    s.scalars(select(RunItem).where(RunItem.run_id == run_id).order_by(RunItem.seq)).all()]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time_utc", "item", "label", "method", "url", "http_status", "duration_ms", "success",
                "attempts", "message"])
    for r in rows:
        w.writerow([r.get("created_at"), r.get("seq"), r.get("label"), r.get("method"), r.get("url"),
                    r.get("http_status"), r.get("duration_ms"), r.get("success"), r.get("attempts"),
                    r.get("message")])
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="run-{run_id}-{safe}.csv"'})


@app.get("/api/events")
async def events(request: HttpRequest):
    q = manager.subscribe()

    async def gen():
        try:
            yield "retry: 2000\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(ev, default=str)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            manager.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# --------------------------------------------------------------------------- auth profiles
@app.get("/api/auth-profiles")
def list_profiles():
    with db.session_scope() as s:
        return {"profiles": svc.list_profiles(s)}


@app.post("/api/auth-profiles")
def create_profile(body: dict = Body(...)):
    with db.session_scope() as s:
        return svc.profile_dict(s, svc.create_profile(s, body))


@app.patch("/api/auth-profiles/{pid}")
def update_profile(pid: int, body: dict = Body(...)):
    with db.session_scope() as s:
        return svc.profile_dict(s, svc.update_profile(s, pid, body))


@app.delete("/api/auth-profiles/{pid}")
def delete_profile(pid: int):
    with db.session_scope() as s:
        svc.delete_profile(s, pid)
    return {"ok": True}


@app.post("/api/auth-profiles/{pid}/test")
def test_profile(pid: int):
    """Fetch a fresh token with the saved settings. Only facts about the token are returned, never the token."""
    with db.session_scope() as s:
        spec = svc.profile_spec(svc.get_profile(s, pid))
        _, variables, _, _ = svc.env_runtime(s)
    tr = oauth.get_token(spec, variables, force=True)
    if not tr.ok:
        return {"ok": False, "error": tr.error}
    info = he.token_info(tr.token)
    return {"ok": True, "token": {**(oauth.cached_status(pid) or {}), "jwt": info.get("jwt", False)}}


# --------------------------------------------------------------------------- environments
@app.get("/api/environments")
def list_envs():
    with db.session_scope() as s:
        return {"environments": svc.list_envs(s)}


@app.post("/api/environments")
def create_env(body: dict = Body(...)):
    with db.session_scope() as s:
        return svc.env_dict(svc.create_env(s, body.get("name", "")))


@app.patch("/api/environments/{eid}")
def rename_env(eid: int, body: dict = Body(...)):
    with db.session_scope() as s:
        return svc.env_dict(svc.rename_env(s, eid, body.get("name", "")))


@app.delete("/api/environments/{eid}")
def delete_env(eid: int):
    with db.session_scope() as s:
        svc.delete_env(s, eid)
    return {"ok": True}


@app.post("/api/environments/{eid}/activate")
def activate_env(eid: int):
    with db.session_scope() as s:
        svc.activate_env(s, eid)
    return {"ok": True}


@app.put("/api/environments/{eid}/variables")
def put_env_vars(eid: int, body: dict = Body(...)):
    with db.session_scope() as s:
        return svc.env_dict(svc.set_env_variables(s, eid, body.get("variables") or []))


@app.put("/api/environments/{eid}/variables/{key}")
def put_env_var(eid: int, key: str, body: dict = Body(...)):
    """Quick update of one value (e.g. pasting a fresh bearer_token during a paused run)."""
    with db.session_scope() as s:
        svc.set_env_variable(s, eid, key, str(body.get("value") or ""))
        return svc.env_dict(svc.get_env(s, eid))
