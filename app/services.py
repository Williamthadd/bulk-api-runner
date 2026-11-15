"""Domain logic on top of the ORM: collection tree, secrets, environments, uploads, export/import,
seed data, retention and the DB-backed run backend."""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from . import crypto, oauth
from . import http_engine as he
from .config import ROOT, settings
from .db import session_scope
from .models import (AuthProfile, Environment, EnvironmentVariable, Folder, Request, Run, RunItem, Upload, utcnow)
from .templating import is_pure_reference

log = logging.getLogger("bulk_runner.services")

EXPORT_FORMAT = "bulk-api-runner/collection"
SEED_DIR = ROOT / "seed"


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


class Invalid(Exception):
    pass


# --------------------------------------------------------------------------- auth configs (secrets)
def store_auth(existing: dict | None, incoming: dict | None) -> dict:
    """Merge an auth update coming from the UI into the stored (encrypted) config.

    incoming = {"type": "inherit"|"bearer"|"profile", "token": str|None, "profile_id": int|None}.
    A missing/None token keeps the stored one. The profile id is checked by check_auth_profile().
    """
    existing = dict(existing or {})
    if incoming is None:
        return existing or {"type": "inherit"}
    typ = incoming.get("type") or existing.get("type") or "inherit"
    if typ not in he.AUTH_TYPES:
        raise Invalid("Auth type must be 'inherit', 'bearer' or 'profile'")
    out: dict = {"type": typ}
    token = incoming.get("token", None)
    if token is None:
        if existing.get("token"):
            out["token"] = existing["token"]
    elif token.strip():
        out["token"] = crypto.encrypt(token.strip())
    pid = incoming.get("profile_id", existing.get("profile_id"))
    if pid not in (None, ""):
        try:
            out["profile_id"] = int(pid)
        except (TypeError, ValueError):
            raise Invalid("profile_id must be a number") from None
    if typ == "profile" and not out.get("profile_id"):
        raise Invalid("Choose an auth profile")
    return out


def check_auth_profile(s: Session, cfg: dict) -> dict:
    if cfg.get("type") == "profile" and not s.get(AuthProfile, cfg.get("profile_id")):
        raise Invalid(f"Auth profile {cfg.get('profile_id')} does not exist")
    return cfg


def plain_auth(stored: dict | None) -> dict:
    """Decrypted auth config for internal use."""
    stored = stored or {}
    out = {"type": stored.get("type") or "inherit"}
    if stored.get("profile_id"):
        out["profile_id"] = stored["profile_id"]
    if stored.get("token"):
        out["token"], ok = crypto.try_decrypt(stored["token"])
        out["unreadable"] = not ok
    return out


def public_auth(stored: dict | None) -> dict:
    """Auth config for the UI: literal tokens are never sent back, variable references are."""
    p = plain_auth(stored)
    tok = p.get("token") or ""
    return {
        "type": p["type"],
        "token": tok if is_pure_reference(tok) else None,
        "has_token": bool(tok),
        "token_is_reference": is_pure_reference(tok),
        "token_unreadable": bool(p.get("unreadable")),
        "profile_id": p.get("profile_id"),
    }


# --------------------------------------------------------------------------- serialization
def folder_dict(f: Folder) -> dict:
    return {"id": f.id, "parent_id": f.parent_id, "name": f.name, "sort_order": f.sort_order,
            "auth": public_auth(f.auth_config)}


def request_dict(r: Request) -> dict:
    return {
        "id": r.id, "folder_id": r.folder_id, "name": r.name, "method": r.method, "url": r.url,
        "headers": r.headers or [], "params": r.params or [], "auth": public_auth(r.auth_config),
        "data_source": {"sheet": None, "header_row": 1, **(r.data_source or {})},
        "script": r.script or "", "settings": he.normalize_settings(r.settings), "sort_order": r.sort_order,
        "updated_at": r.updated_at.isoformat(timespec="milliseconds") + "Z" if r.updated_at else None,
    }


def tree(s: Session) -> list[dict]:
    folders = s.scalars(select(Folder).order_by(Folder.sort_order, Folder.id)).all()
    reqs = s.scalars(select(Request).order_by(Request.sort_order, Request.id)).all()
    nodes = {f.id: {**folder_dict(f), "kind": "folder", "folders": [], "requests": []} for f in folders}
    roots = []
    for f in folders:
        (nodes[f.parent_id]["folders"] if f.parent_id in nodes else roots).append(nodes[f.id])
    for r in reqs:
        if r.folder_id in nodes:
            nodes[r.folder_id]["requests"].append({"kind": "request", "id": r.id, "name": r.name,
                                                   "method": r.method, "folder_id": r.folder_id})
    return roots


def get_folder(s: Session, fid: int) -> Folder:
    f = s.get(Folder, fid)
    if not f:
        raise NotFound(f"Folder {fid} not found")
    return f


def get_request(s: Session, rid: int) -> Request:
    r = s.get(Request, rid)
    if not r:
        raise NotFound(f"Request {rid} not found")
    return r


def folder_chain(s: Session, folder_id: int | None) -> list[Folder]:
    """Nearest first."""
    chain, seen = [], set()
    while folder_id is not None and folder_id not in seen:
        seen.add(folder_id)
        f = s.get(Folder, folder_id)
        if not f:
            break
        chain.append(f)
        folder_id = f.parent_id
    return chain


def root_folder_id(s: Session, folder_id: int) -> int:
    chain = folder_chain(s, folder_id)
    return chain[-1].id if chain else folder_id


def descendant_folder_ids(s: Session, fid: int) -> set[int]:
    rows = s.execute(select(Folder.id, Folder.parent_id)).all()
    kids: dict[int | None, list[int]] = {}
    for i, p in rows:
        kids.setdefault(p, []).append(i)
    out, stack = set(), [fid]
    while stack:
        cur = stack.pop()
        if cur in out:
            continue
        out.add(cur)
        stack.extend(kids.get(cur, []))
    return out


# --------------------------------------------------------------------------- folder / request CRUD
def _next_order(s: Session, model, **where) -> int:
    q = select(func.coalesce(func.max(model.sort_order), -1))
    for k, v in where.items():
        col = getattr(model, k)
        q = q.where(col.is_(None) if v is None else col == v)
    return int(s.scalar(q)) + 1


def create_folder(s: Session, name: str, parent_id: int | None = None, auth: dict | None = None) -> Folder:
    name = (name or "").strip()
    if not name:
        raise Invalid("Folder name is required")
    if parent_id is not None:
        get_folder(s, parent_id)
    f = Folder(name=name[:200], parent_id=parent_id, sort_order=_next_order(s, Folder, parent_id=parent_id),
               auth_config=check_auth_profile(s, store_auth(None, auth or {"type": "inherit"})))
    s.add(f)
    s.flush()
    return f


def update_folder(s: Session, fid: int, data: dict) -> Folder:
    f = get_folder(s, fid)
    if "name" in data:
        name = (data["name"] or "").strip()
        if not name:
            raise Invalid("Folder name is required")
        f.name = name[:200]
    if "auth" in data:
        f.auth_config = check_auth_profile(s, store_auth(f.auth_config, data["auth"]))
    f.updated_at = utcnow()
    s.flush()
    return f


def delete_folder(s: Session, fid: int) -> None:
    get_folder(s, fid)
    s.execute(delete(Folder).where(Folder.id == fid))  # DB cascades to sub-folders and requests


def _unique_request_name(s: Session, folder_id: int, name: str, exclude_id: int | None = None) -> str:
    existing = set(s.scalars(select(Request.name).where(Request.folder_id == folder_id,
                                                        Request.id != (exclude_id or 0))).all())
    if name not in existing:
        return name
    base = name
    for i in range(2, 1000):
        cand = f"{base} ({i})"
        if cand not in existing:
            return cand
    raise Conflict("Too many requests with the same name")


def _check_name_free(s: Session, folder_id: int, name: str, exclude_id: int | None = None) -> None:
    q = select(Request.id).where(Request.folder_id == folder_id, Request.name == name)
    if exclude_id:
        q = q.where(Request.id != exclude_id)
    if s.scalar(q):
        raise Conflict(f"A request named '{name}' already exists in this folder")


def _clean_kv(rows: Any, what: str) -> list[dict]:
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        key = str(r.get("key") or "").strip()
        val = "" if r.get("value") is None else str(r.get("value"))
        if not key and not val:
            continue
        out.append({"key": key, "value": val, "enabled": bool(r.get("enabled", True))})
    return out


def create_request(s: Session, folder_id: int, name: str, **fields) -> Request:
    get_folder(s, folder_id)
    name = (name or "").strip() or "New request"
    _check_name_free(s, folder_id, name)
    r = Request(folder_id=folder_id, name=name[:200], method="GET", url="", headers=[], params=[],
                auth_config={"type": "inherit"}, data_source={"sheet": None, "header_row": 1}, script=DEFAULT_SCRIPT,
                settings=he.normalize_settings({}), sort_order=_next_order(s, Request, folder_id=folder_id))
    s.add(r)
    s.flush()
    if fields:
        update_request(s, r.id, fields)
    return r


def update_request(s: Session, rid: int, data: dict) -> Request:
    r = get_request(s, rid)
    if "name" in data:
        name = (data["name"] or "").strip()
        if not name:
            raise Invalid("Request name is required")
        _check_name_free(s, r.folder_id, name, exclude_id=r.id)
        r.name = name[:200]
    if "method" in data:
        m = (data["method"] or "").upper()
        if m not in he.METHODS:
            raise Invalid(f"Method must be one of {', '.join(he.METHODS)}")
        r.method = m
    if "url" in data:
        r.url = (data["url"] or "").strip()[:2000]
    if "headers" in data:
        hdrs = _clean_kv(data["headers"], "header")
        errs = he.check_headers(hdrs)
        if errs:
            raise Invalid(errs[0])
        r.headers = hdrs
    if "params" in data:
        r.params = _clean_kv(data["params"], "param")
    if "auth" in data:
        r.auth_config = check_auth_profile(s, store_auth(r.auth_config, data["auth"]))
    if "data_source" in data:
        ds = dict(r.data_source or {})
        inc = data["data_source"] or {}
        if "sheet" in inc:
            ds["sheet"] = inc["sheet"] or None
        if "header_row" in inc:
            try:
                hr = int(inc["header_row"] or 1)
            except (TypeError, ValueError):
                raise Invalid("Header row must be a number") from None
            if hr < 1:
                raise Invalid("Header row must be >= 1")
            ds["header_row"] = hr
        r.data_source = ds
    if "script" in data:
        r.script = data["script"] or ""
    if "settings" in data:
        r.settings = he.normalize_settings(data["settings"])
    r.updated_at = utcnow()
    s.flush()
    return r


def delete_request(s: Session, rid: int) -> None:
    get_request(s, rid)
    s.execute(delete(Request).where(Request.id == rid))


def duplicate_request(s: Session, rid: int) -> Request:
    src = get_request(s, rid)
    name = _unique_request_name(s, src.folder_id, f"{src.name} copy")
    r = Request(folder_id=src.folder_id, name=name, method=src.method, url=src.url,
                headers=list(src.headers or []), params=list(src.params or []),
                auth_config=dict(src.auth_config or {}), data_source=dict(src.data_source or {}),
                script=src.script, settings=dict(src.settings or {}),
                sort_order=_next_order(s, Request, folder_id=src.folder_id))
    s.add(r)
    s.flush()
    return r


def duplicate_folder(s: Session, fid: int, parent_id: int | None = ..., name: str | None = None) -> Folder:  # type: ignore[assignment]
    src = get_folder(s, fid)
    target_parent = src.parent_id if parent_id is ... else parent_id
    f = Folder(name=name or f"{src.name} copy", parent_id=target_parent, auth_config=dict(src.auth_config or {}),
               sort_order=_next_order(s, Folder, parent_id=target_parent))
    s.add(f)
    s.flush()
    for r in s.scalars(select(Request).where(Request.folder_id == src.id).order_by(Request.sort_order)).all():
        s.add(Request(folder_id=f.id, name=r.name, method=r.method, url=r.url, headers=list(r.headers or []),
                      params=list(r.params or []), auth_config=dict(r.auth_config or {}),
                      data_source=dict(r.data_source or {}), script=r.script, settings=dict(r.settings or {}),
                      sort_order=r.sort_order))
    for child in s.scalars(select(Folder).where(Folder.parent_id == src.id).order_by(Folder.sort_order)).all():
        duplicate_folder(s, child.id, parent_id=f.id, name=child.name)
    s.flush()
    return f


def _reorder(items: list, moved, index: int | None) -> None:
    items = [i for i in items if i.id != moved.id]
    idx = len(items) if index is None else max(0, min(index, len(items)))
    items.insert(idx, moved)
    for n, it in enumerate(items):
        it.sort_order = n


def move(s: Session, kind: str, item_id: int, parent_id: int | None, index: int | None = None) -> None:
    if kind == "request":
        r = get_request(s, item_id)
        if parent_id is None:
            raise Invalid("A request must be inside a folder")
        get_folder(s, parent_id)
        if parent_id != r.folder_id:
            _check_name_free(s, parent_id, r.name, exclude_id=r.id)
            r.folder_id = parent_id
            s.flush()
        sibs = s.scalars(select(Request).where(Request.folder_id == parent_id).order_by(Request.sort_order, Request.id)).all()
        _reorder(list(sibs), r, index)
    elif kind == "folder":
        f = get_folder(s, item_id)
        if parent_id is not None:
            get_folder(s, parent_id)
            if parent_id in descendant_folder_ids(s, f.id):
                raise Invalid("Cannot move a folder into itself or one of its sub-folders")
        f.parent_id = parent_id
        s.flush()
        q = select(Folder).where(Folder.parent_id.is_(None) if parent_id is None else Folder.parent_id == parent_id)
        sibs = s.scalars(q.order_by(Folder.sort_order, Folder.id)).all()
        _reorder(list(sibs), f, index)
    else:
        raise Invalid("kind must be 'folder' or 'request'")
    s.flush()


# --------------------------------------------------------------------------- environments
def env_dict(e: Environment) -> dict:
    out = []
    for v in e.variables:
        if v.is_secret:
            plain, ok = crypto.try_decrypt(v.value)
            out.append({"id": v.id, "key": v.key, "value": None, "is_secret": True,
                        "has_value": bool(plain), "unreadable": not ok})
        else:
            out.append({"id": v.id, "key": v.key, "value": v.value or "", "is_secret": False,
                        "has_value": bool(v.value), "unreadable": False})
    return {"id": e.id, "name": e.name, "is_active": e.is_active, "variables": out}


def list_envs(s: Session) -> list[dict]:
    return [env_dict(e) for e in s.scalars(select(Environment).order_by(Environment.id)).all()]


def get_env(s: Session, eid: int) -> Environment:
    e = s.get(Environment, eid)
    if not e:
        raise NotFound(f"Environment {eid} not found")
    return e


def active_env(s: Session) -> Environment | None:
    return s.scalar(select(Environment).where(Environment.is_active.is_(True)).limit(1))


def create_env(s: Session, name: str) -> Environment:
    name = (name or "").strip()
    if not name:
        raise Invalid("Environment name is required")
    if s.scalar(select(Environment.id).where(Environment.name == name)):
        raise Conflict(f"Environment '{name}' already exists")
    e = Environment(name=name[:100], is_active=not bool(s.scalar(select(func.count(Environment.id)))))
    s.add(e)
    s.flush()
    return e


def rename_env(s: Session, eid: int, name: str) -> Environment:
    e = get_env(s, eid)
    name = (name or "").strip()
    if not name:
        raise Invalid("Environment name is required")
    if s.scalar(select(Environment.id).where(Environment.name == name, Environment.id != eid)):
        raise Conflict(f"Environment '{name}' already exists")
    e.name = name[:100]
    s.flush()
    return e


def delete_env(s: Session, eid: int) -> None:
    e = get_env(s, eid)
    was_active = e.is_active
    s.delete(e)
    s.flush()
    if was_active:
        first = s.scalar(select(Environment).order_by(Environment.id).limit(1))
        if first:
            first.is_active = True


def activate_env(s: Session, eid: int) -> None:
    get_env(s, eid)
    s.execute(update(Environment).values(is_active=False))
    s.execute(update(Environment).where(Environment.id == eid).values(is_active=True))


def set_env_variables(s: Session, eid: int, variables: list[dict]) -> Environment:
    """Replace the variable set. For a secret, value=None keeps the stored value."""
    e = get_env(s, eid)
    old = {v.key: v for v in e.variables}
    seen: set[str] = set()
    new_vars = []
    for raw in variables or []:
        key = str(raw.get("key") or "").strip()
        if not key:
            continue
        if key in seen:
            raise Invalid(f"Duplicate variable '{key}'")
        seen.add(key)
        is_secret = bool(raw.get("is_secret"))
        val = raw.get("value")
        prev = old.get(key)
        if is_secret:
            if val is None:
                if prev is None:
                    stored = ""
                elif prev.is_secret:
                    stored = prev.value or ""
                else:
                    stored = crypto.encrypt(prev.value or "")
            else:
                stored = crypto.encrypt(str(val))
        else:
            if val is None and prev is not None:
                val = crypto.try_decrypt(prev.value)[0] if prev.is_secret else prev.value
            stored = "" if val is None else str(val)
        new_vars.append((key, stored, is_secret))
    for v in list(e.variables):
        e.variables.remove(v)
    s.flush()
    for key, stored, is_secret in new_vars:
        e.variables.append(EnvironmentVariable(key=key[:200], value=stored, is_secret=is_secret))
    s.flush()
    return e


def set_env_variable(s: Session, eid: int, key: str, value: str) -> None:
    """Update one variable's value (keeps its secret flag; creates it as non-secret if new)."""
    e = get_env(s, eid)
    for v in e.variables:
        if v.key == key:
            v.value = crypto.encrypt(value) if v.is_secret else value
            s.flush()
            return
    e.variables.append(EnvironmentVariable(key=key, value=value, is_secret=False))
    s.flush()


def env_runtime(s: Session, env_id: int | None = None) -> tuple[Environment | None, dict[str, str], list[str], list[str]]:
    """(env, all_vars_plaintext, secret_values, unreadable_keys)."""
    e = get_env(s, env_id) if env_id else active_env(s)
    if not e:
        return None, {}, [], []
    variables, secrets, unreadable = {}, [], []
    for v in e.variables:
        if v.is_secret:
            plain, ok = crypto.try_decrypt(v.value)
            if not ok:
                unreadable.append(v.key)
            variables[v.key] = plain
            if plain:
                secrets.append(plain)
        else:
            variables[v.key] = v.value or ""
    return e, variables, secrets, unreadable


def script_vars(s: Session, env_id: int | None) -> dict[str, str]:
    """Variables exposed to scripts as ctx.vars: secrets are left out."""
    e = get_env(s, env_id) if env_id else active_env(s)
    return {v.key: v.value or "" for v in (e.variables if e else []) if not v.is_secret}


# --------------------------------------------------------------------------- request specs
def build_spec(s: Session, r: Request) -> tuple[he.RequestSpec, list[str]]:
    """RequestSpec with decrypted token / auth profile; also returns secrets for masking."""
    chain = folder_chain(s, r.folder_id)
    cfg, source = he.resolve_auth(plain_auth(r.auth_config), [plain_auth(f.auth_config) for f in chain])
    tmpl, profile, secrets = None, None, []
    if cfg and cfg["type"] == "bearer":
        tmpl = cfg.get("token") or ""
        if tmpl and not is_pure_reference(tmpl):
            secrets.append(tmpl)
    elif cfg and cfg["type"] == "profile":
        ap = s.get(AuthProfile, cfg.get("profile_id"))
        if ap is None:
            source = "none"  # the profile was deleted: treated as "no auth configured"
        else:
            profile = profile_spec(ap)
            secrets.extend(x for x in (profile.client_secret,) if x and not is_pure_reference(x))
    return he.RequestSpec(id=r.id, name=r.name, method=r.method, url=r.url, headers=r.headers or [],
                          params=r.params or [], token_template=tmpl, token_source=source, auth_profile=profile,
                          settings=he.normalize_settings(r.settings)), secrets


def prepare_request(spec: he.RequestSpec, variables: dict[str, str], *, force_refresh: bool = False,
                    **kw) -> he.Prepared:
    """he.prepare(), fetching (or reusing) the auth profile token first when the request uses one."""
    tr = oauth.get_token(spec.auth_profile, variables, force=force_refresh) if spec.auth_profile else None
    return he.prepare(spec, variables, fetched_token=tr, **kw)


# --------------------------------------------------------------------------- auth profiles
def profile_spec(ap: AuthProfile) -> oauth.ProfileSpec:
    secret, ok = crypto.try_decrypt(ap.client_secret) if ap.client_secret else ("", True)
    return oauth.ProfileSpec(id=ap.id, name=ap.name, token_url=ap.token_url or "", client_id=ap.client_id or "",
                             client_secret=secret, grant_type=ap.grant_type or "client_credentials",
                             scope=ap.scope or "", secret_unreadable=not ok)


def profile_users(s: Session, pid: int) -> list[str]:
    """Names of folders/requests whose Auth is set to this profile."""
    out = []
    for f in s.scalars(select(Folder)).all():
        if (f.auth_config or {}).get("type") == "profile" and (f.auth_config or {}).get("profile_id") == pid:
            out.append(f"folder '{f.name}'")
    for r in s.scalars(select(Request)).all():
        if (r.auth_config or {}).get("type") == "profile" and (r.auth_config or {}).get("profile_id") == pid:
            out.append(f"request '{r.name}'")
    return out


def profile_dict(s: Session, ap: AuthProfile) -> dict:
    """Profile for the UI: the client secret is never sent back."""
    secret_ok = True
    if ap.client_secret:
        _, secret_ok = crypto.try_decrypt(ap.client_secret)
    return {"id": ap.id, "name": ap.name, "token_url": ap.token_url or "", "client_id": ap.client_id or "",
            "grant_type": ap.grant_type or "client_credentials", "scope": ap.scope or "",
            "has_secret": bool(ap.client_secret), "secret_unreadable": not secret_ok,
            "used_by": profile_users(s, ap.id),
            "token": oauth.cached_status(ap.id)}


def list_profiles(s: Session) -> list[dict]:
    return [profile_dict(s, ap) for ap in s.scalars(select(AuthProfile).order_by(AuthProfile.name)).all()]


def get_profile(s: Session, pid: int) -> AuthProfile:
    ap = s.get(AuthProfile, pid)
    if not ap:
        raise NotFound(f"Auth profile {pid} not found")
    return ap


def _apply_profile(s: Session, ap: AuthProfile, data: dict) -> None:
    if "name" in data:
        name = (data.get("name") or "").strip()
        if not name:
            raise Invalid("Profile name is required")
        other = s.scalar(select(AuthProfile).where(AuthProfile.name == name))
        if other and other.id != ap.id:
            raise Conflict(f"An auth profile named '{name}' already exists")
        ap.name = name[:100]
    if "token_url" in data:
        url = (data.get("token_url") or "").strip()
        if not (url.lower().startswith(("http://", "https://")) or url.startswith("{{")):
            raise Invalid("Token URL must start with http:// or https:// (or be a {{variable}})")
        ap.token_url = url[:2000]
    if "client_id" in data:
        cid = (data.get("client_id") or "").strip()
        if not cid:
            raise Invalid("client_id is required")
        ap.client_id = cid[:500]
    if "grant_type" in data:
        gt = (data.get("grant_type") or "client_credentials").strip()
        if gt not in oauth.GRANT_TYPES:
            raise Invalid("grant_type must be client_credentials")
        ap.grant_type = gt
    if "scope" in data:
        ap.scope = (data.get("scope") or "").strip()[:500]
    secret = data.get("client_secret")
    if isinstance(secret, str) and secret.strip():  # empty / missing = keep the saved secret
        ap.client_secret = crypto.encrypt(secret.strip())


def create_profile(s: Session, data: dict) -> AuthProfile:
    data = {"grant_type": "client_credentials", **data}
    for k in ("name", "token_url", "client_id"):
        data.setdefault(k, "")
    if not (data.get("client_secret") or "").strip():
        raise Invalid("client_secret is required")
    ap = AuthProfile(name="", token_url="", client_id="", grant_type="client_credentials", scope="")
    _apply_profile(s, ap, data)
    s.add(ap)
    s.flush()
    return ap


def update_profile(s: Session, pid: int, data: dict) -> AuthProfile:
    ap = get_profile(s, pid)
    _apply_profile(s, ap, data)
    ap.updated_at = utcnow()
    s.flush()
    oauth.invalidate(ap.id)
    return ap


def delete_profile(s: Session, pid: int) -> None:
    ap = get_profile(s, pid)
    users = profile_users(s, pid)
    if users:
        raise Conflict(f"'{ap.name}' is still used by {', '.join(users[:5])}"
                       f"{' and more' if len(users) > 5 else ''}. Change their Auth first.")
    s.delete(ap)
    s.flush()
    oauth.invalidate(pid)


def find_request_by_name(s: Session, name: str, near_folder_id: int | None) -> Request:
    matches = s.scalars(select(Request).where(Request.name == name)).all()
    if not matches:
        raise NotFound(f"ctx.call: no request named '{name}'")
    if len(matches) > 1 and near_folder_id is not None:
        root = root_folder_id(s, near_folder_id)
        same = [m for m in matches if root_folder_id(s, m.folder_id) == root]
        if len(same) == 1:
            return same[0]
        matches = same or matches
    if len(matches) > 1:
        raise Conflict(f"ctx.call: request name '{name}' is ambiguous ({len(matches)} matches)")
    return matches[0]


# --------------------------------------------------------------------------- uploads
def _uploads_dir() -> Path:
    d = settings.uploads_dir
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_upload(s: Session, request_id: int | None, original_name: str, data: bytes) -> Upload:
    if len(data) > settings.upload_max_mb * 1024 * 1024:
        raise Invalid(f"File is larger than {settings.upload_max_mb} MB")
    if not original_name.lower().endswith((".xlsx", ".xlsm")):
        raise Invalid("Only .xlsx files are supported")
    if data[:2] != b"PK":
        raise Invalid("This does not look like an .xlsx file")
    stored = f"{uuid.uuid4().hex}.xlsx"
    (_uploads_dir() / stored).write_bytes(data)
    u = Upload(request_id=request_id, original_name=Path(original_name).name[:255], stored_name=stored,
               size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    s.add(u)
    s.flush()
    return u


def upload_path(s: Session, upload_id: int) -> tuple[Upload, Path]:
    u = s.get(Upload, upload_id)
    if not u:
        raise NotFound("Uploaded file not found (uploads are deleted after "
                       f"{settings.upload_ttl_hours} hours). Upload it again.")
    p = _uploads_dir() / u.stored_name
    if not p.exists():
        raise NotFound("Uploaded file is gone from disk. Upload it again.")
    return u, p


def upload_dict(u: Upload) -> dict:
    return {"id": u.id, "name": u.original_name, "size": u.size_bytes, "sha256": u.sha256,
            "created_at": u.created_at.isoformat(timespec="milliseconds") + "Z"}


def cleanup_uploads(s: Session) -> int:
    cutoff = utcnow() - timedelta(hours=settings.upload_ttl_hours)
    old = s.scalars(select(Upload).where(Upload.created_at < cutoff)).all()
    n = 0
    for u in old:
        try:
            (_uploads_dir() / u.stored_name).unlink(missing_ok=True)
        except OSError:
            pass
        s.delete(u)
        n += 1
    known = set(s.scalars(select(Upload.stored_name)).all())
    import time as _t
    for f in _uploads_dir().glob("*.xlsx"):
        if f.name not in known and _t.time() - f.stat().st_mtime > settings.upload_ttl_hours * 3600:
            f.unlink(missing_ok=True)
    return n


# --------------------------------------------------------------------------- export / import
def _export_auth(s: Session, stored: dict | None) -> dict:
    p = plain_auth(stored)
    out = {"type": p["type"]}
    if p["type"] == "profile":
        ap = s.get(AuthProfile, p.get("profile_id"))
        # only the profile name: its URL, client and secret stay in this app
        return {"type": "profile", "profile": ap.name} if ap else {"type": "inherit"}
    if p.get("token") and is_pure_reference(p["token"]):
        out["token"] = p["token"]  # a variable reference is not a secret; literal tokens are dropped
    return out


def export_folder(s: Session, fid: int) -> dict:
    def walk(f: Folder) -> dict:
        reqs = s.scalars(select(Request).where(Request.folder_id == f.id).order_by(Request.sort_order, Request.id)).all()
        subs = s.scalars(select(Folder).where(Folder.parent_id == f.id).order_by(Folder.sort_order, Folder.id)).all()
        return {
            "name": f.name, "auth": _export_auth(s, f.auth_config),
            "folders": [walk(c) for c in subs],
            "requests": [{"name": r.name, "method": r.method, "url": r.url, "headers": r.headers or [],
                          "params": r.params or [], "auth": _export_auth(s, r.auth_config),
                          "data_source": {k: v for k, v in (r.data_source or {}).items()},
                          "script": r.script or "", "settings": he.normalize_settings(r.settings)} for r in reqs],
        }
    return {"format": EXPORT_FORMAT, "version": 1,
            "exported_at": utcnow().isoformat(timespec="seconds") + "Z", "folder": walk(get_folder(s, fid))}


def import_collection(s: Session, doc: dict, parent_id: int | None = None, base_dir: Path | None = None) -> Folder:
    if not isinstance(doc, dict) or doc.get("format") != EXPORT_FORMAT or not isinstance(doc.get("folder"), dict):
        raise Invalid(f"Not a Bulk API Runner collection export (expected format '{EXPORT_FORMAT}')")

    def create(node: dict, parent: int | None) -> Folder:
        name = str(node.get("name") or "Imported").strip()
        if parent is None:
            taken = set(s.scalars(select(Folder.name).where(Folder.parent_id.is_(None))).all())
            base, i = name, 2
            while name in taken:
                name = f"{base} ({i})"
                i += 1
        f = create_folder(s, name, parent, auth=_import_auth(s, node.get("auth")))
        for rq in node.get("requests") or []:
            script = rq.get("script")
            if script is None and rq.get("script_file") and base_dir:
                script = (base_dir / rq["script_file"]).read_text(encoding="utf-8")
            create_request(s, f.id, str(rq.get("name") or "Request"),
                           method=rq.get("method") or "GET", url=rq.get("url") or "",
                           headers=rq.get("headers") or [], params=rq.get("params") or [],
                           auth=_import_auth(s, rq.get("auth")), script=script or "",
                           data_source=rq.get("data_source") or {}, settings=rq.get("settings") or {})
            if rq.get("data_source", {}).get("sample_file"):
                r = s.scalar(select(Request).where(Request.folder_id == f.id, Request.name == rq["name"]))
                if r:
                    r.data_source = {**(r.data_source or {}), "sample_file": rq["data_source"]["sample_file"]}
        for sub in node.get("folders") or []:
            create(sub, f.id)
        return f

    return create(doc["folder"], parent_id)


def _import_auth(s: Session, a: dict | None) -> dict:
    a = a or {}
    if a.get("type") == "profile":
        ap = s.scalar(select(AuthProfile).where(AuthProfile.name == str(a.get("profile") or "")))
        # an unknown profile name falls back to "inherit"; pick the profile in the Auth tab after importing
        return {"type": "profile", "profile_id": ap.id} if ap else {"type": "inherit"}
    typ = a.get("type") if a.get("type") in ("inherit", "bearer") else "inherit"
    return {"type": typ, "token": a.get("token") or None}


# --------------------------------------------------------------------------- seed
DEFAULT_SCRIPT = '''def transform(rows, ctx):
    """Return one item per HTTP request: {"label": ..., "body": {...}} (optional: "query", "headers")."""
    items = []
    for r in rows:
        items.append({"label": f"row {r['_row']}", "body": {k: v for k, v in r.items() if k != "_row"}})
    return items
'''


def seed_if_empty(s: Session) -> bool:
    """Load the seed collection only when there are no folders at all."""
    if s.scalar(select(func.count(Folder.id))):
        return False
    doc = json.loads((SEED_DIR / "collection.json").read_text(encoding="utf-8"))
    import_collection(s, doc, base_dir=SEED_DIR)
    if not s.scalar(select(func.count(Environment.id))):
        for env in doc.get("environments") or []:
            e = Environment(name=env["name"], is_active=bool(env.get("active")))
            s.add(e)
            s.flush()
            set_env_variables(s, e.id, [{"key": v["key"], "value": v.get("value", ""),
                                         "is_secret": bool(v.get("secret"))} for v in env.get("variables") or []])
    s.flush()
    return True


def sample_path(rel: str) -> Path:
    p = (ROOT / rel).resolve()
    if ROOT.resolve() not in p.parents or not p.exists():
        raise NotFound(f"Sample file '{rel}' not found")
    return p


# --------------------------------------------------------------------------- runs (history)
def iso(dt) -> str | None:
    return dt.isoformat(timespec="milliseconds") + "Z" if dt else None


def run_dict(r: Run) -> dict:
    return {"id": r.id, "request_id": r.request_id, "request_name": r.request_name,
            "environment_name": r.environment_name, "mode": r.mode, "status": r.status, "total": r.total,
            "success_count": r.success_count, "failed_count": r.failed_count, "skipped_count": r.skipped_count,
            "done_count": r.success_count + r.failed_count, "started_at": iso(r.started_at),
            "finished_at": iso(r.finished_at), "live": False}


accepted_data = he.accepted_data


def item_dict(i: RunItem, full: bool = False) -> dict:
    d = {"seq": i.seq, "label": i.label, "method": i.method, "url": i.url, "http_status": i.http_status,
         "duration_ms": i.duration_ms, "success": i.success, "attempts": i.attempts, "message": i.message or "",
         "created_at": iso(i.created_at), "state": "done",
         "accepted": accepted_data(i.http_status, i.response_body)}
    if full:
        d.update(request_body=i.request_body or "", response_body=i.response_body or "")
    return d


def mark_interrupted_runs(s: Session) -> int:
    res = s.execute(update(Run).where(Run.status.in_(["Running", "Paused"]))
                    .values(status="Stopped", finished_at=utcnow()))
    return res.rowcount or 0


def apply_retention(s: Session, request_id: int | None, max_runs: int | None = None, days: int | None = None) -> int:
    """Keep a run only if it is among the last `max_runs` of its request AND younger than `days`."""
    max_runs = settings.retention_max_runs if max_runs is None else max_runs
    days = settings.retention_days if days is None else days
    cutoff = utcnow() - timedelta(days=days)
    q = select(Run.id, Run.started_at).where(Run.status.notin_(["Running", "Paused"]))
    q = q.where(Run.request_id.is_(None) if request_id is None else Run.request_id == request_id)
    rows = s.execute(q.order_by(Run.started_at.desc(), Run.id.desc())).all()
    doomed = [rid for n, (rid, started) in enumerate(rows) if n >= max_runs or started < cutoff]
    if doomed:
        s.execute(delete(Run).where(Run.id.in_(doomed)))  # run_items cascade in the DB
    # runs of deleted requests: age limit only
    if request_id is not None:
        s.execute(delete(Run).where(Run.request_id.is_(None), Run.started_at < cutoff,
                                    Run.status.notin_(["Running", "Paused"])))
    return len(doomed)


class DbRunBackend:
    """RunBackend that re-reads token/variables for every item (so Resume picks up a new token)."""

    def __init__(self, request_id: int, env_id: int | None) -> None:
        self.request_id = request_id
        self.env_id = env_id
        self._cache: tuple[he.RequestSpec, dict, list[str]] | None = None

    def _load(self) -> tuple[he.RequestSpec, dict, list[str]]:
        try:
            with session_scope() as s:
                r = get_request(s, self.request_id)
                spec, tok_secrets = build_spec(s, r)
                _, variables, secrets, _ = env_runtime(s, self.env_id)
                self._cache = (spec, variables, secrets + tok_secrets)
        except Exception:  # noqa: BLE001 - DB down: keep going with the last known config
            if self._cache is None:
                raise
        return self._cache  # type: ignore[return-value]

    def prepare(self, item: dict) -> tuple[he.Prepared, list[str]]:
        spec, variables, secrets = self._load()
        p = prepare_request(spec, variables, item=item)
        return p, secrets + ([p.token] if p.token else [])

    def on_unauthorized(self) -> bool:
        """After a 401: drop the cached auth profile token so the next attempt fetches a new one.
        True when a retry with a fresh token makes sense."""
        spec = self._cache[0] if self._cache else None
        if spec is None or spec.auth_profile is None:
            return False
        oauth.invalidate(spec.auth_profile.id)
        return True

    def persist_run(self, st) -> int | None:
        with session_scope() as s:
            if st.id is not None and st.id > 0:
                run = s.get(Run, st.id)
            else:
                run = Run(request_id=st.request_id, request_name=st.request_name[:200],
                          environment_name=(st.environment_name or "")[:100], mode=st.mode,
                          started_at=st.started_at, status=st.status)
                s.add(run)
            if run is None:
                return None
            run.status, run.total = st.status, st.total
            run.success_count, run.failed_count, run.skipped_count = st.success_count, st.failed_count, st.skipped_count
            run.finished_at = st.finished_at
            s.flush()
            if st.id is None:
                st.id = run.id
            return run.id

    def persist_item(self, st, row: dict) -> None:
        if st.id is None or st.id < 0:
            return
        with session_scope() as s:
            item = s.get(RunItem, row["_db_id"]) if row.get("_db_id") else None
            if item is None:
                item = RunItem(run_id=st.id, seq=row["seq"])
                s.add(item)
            item.label = str(row.get("label") or "")[:500]
            item.method = row["method"]
            item.url = (row.get("url") or "")[:2000]
            item.http_status = row.get("http_status")
            item.duration_ms = row.get("duration_ms")
            item.success = bool(row.get("success"))
            item.attempts = int(row.get("attempts") or 0)
            item.message = row.get("message") or ""
            item.request_body = row.get("request_body") or ""     # already masked + truncated
            item.response_body = row.get("response_body") or ""   # already masked + truncated
            item.created_at = row.get("created_at") or utcnow()
            s.flush()
            row["_db_id"] = item.id
            # keep counters current so history survives a crash mid-run
            s.execute(update(Run).where(Run.id == st.id).values(
                success_count=st.success_count + (1 if row.get("success") and row.get("state") == "done" else 0),
                failed_count=st.failed_count + (1 if not row.get("success") and row.get("state") == "done" else 0),
                status=st.status))

    def after_finish(self, st) -> None:
        with session_scope() as s:
            apply_retention(s, st.request_id)
