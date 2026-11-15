"""Turns a stored request + item into an HTTP call: variables, Bearer auth, success rules."""
from __future__ import annotations

import base64
import copy
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from .templating import MASK, mask_text, render

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
NEVER_RETRY = {400, 401, 403, 404, 409}

DEFAULT_SETTINGS: dict[str, Any] = {
    "delay_ms": 0,
    "timeout_s": 30,
    "stop_on_error": False,
    "pause_on_401": True,
    "idempotent": False,
    "script_timeout_s": None,  # None = server default (SCRIPT_TIMEOUT_SECONDS)
    "retry": {"max_retries": 0, "on_status": [503], "backoff_ms": 1000},
    "success": {"require_2xx": True, "code_field": "code", "code_values": [200]},
}


def normalize_settings(s: dict | None) -> dict:
    out = copy.deepcopy(DEFAULT_SETTINGS)
    s = s or {}
    for k, v in s.items():
        if k in ("retry", "success") and isinstance(v, dict):
            out[k].update(v)
        elif k in out:
            out[k] = v
    out["delay_ms"] = max(0, int(out["delay_ms"] or 0))
    out["timeout_s"] = max(1, float(out["timeout_s"] or 30))
    r = out["retry"]
    r["max_retries"] = max(0, min(10, int(r.get("max_retries") or 0)))
    r["backoff_ms"] = max(0, int(r.get("backoff_ms") or 0))
    r["on_status"] = sorted({int(x) for x in (r.get("on_status") or []) if str(x).strip().isdigit()} - NEVER_RETRY)
    sc = out["success"]
    if "code_value" in sc:  # legacy single value
        legacy = sc.pop("code_value")
        if "code_values" not in ((s.get("success") or {}) if isinstance(s.get("success"), dict) else {}):
            sc["code_values"] = [legacy]
    vals = sc.get("code_values")
    if not isinstance(vals, (list, tuple)):
        vals = [vals]
    sc["code_values"] = [float(v) if not float(v).is_integer() else int(float(v))
                         for v in vals if str(v).strip().lstrip("-").replace(".", "", 1).isdigit()] or [200]
    sc["require_2xx"] = bool(sc.get("require_2xx", True))
    sc["code_field"] = str(sc.get("code_field") or "").strip()
    return out


class AuthError(Exception):
    pass


@dataclass
class RequestSpec:
    """Everything needed to call one stored request (secrets already decrypted)."""
    id: int | None
    name: str
    method: str
    url: str
    headers: list[dict] = field(default_factory=list)   # [{"key","value","enabled"}]
    params: list[dict] = field(default_factory=list)
    token_template: str | None = None                   # None = no Bearer token configured anywhere
    token_source: str = "none"
    auth_profile: Any = None                            # oauth.ProfileSpec when the token comes from a profile
    settings: dict = field(default_factory=lambda: normalize_settings({}))


@dataclass
class Prepared:
    method: str
    url: str                      # full URL including query
    headers: dict[str, str]
    body: Any = None
    has_body: bool = False
    token: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    token_problem: bool = False   # empty/undefined token -> run pauses like a 401
    auth_profile_name: str | None = None

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass
class HttpResult:
    status: int | None
    duration_ms: int
    text: str = ""
    json: Any = None
    error: str | None = None
    headers: dict = field(default_factory=dict)


_AUTH_PREFIX = re.compile(r"^(authorization\s*:\s*)", re.I)


def normalize_token(token: str | None) -> str:
    """Clean a pasted token: quotes, 'Authorization:' / 'Bearer ' prefixes and whitespace/line breaks
    (Bearer tokens never contain whitespace, but copy/paste often adds it)."""
    t = (token or "").strip().strip("'\"").strip()
    t = _AUTH_PREFIX.sub("", t).strip()
    while t[:7].lower() == "bearer ":
        t = t[7:].strip()
    return re.sub(r"\s+", "", t)


def token_info(token: str | None) -> dict:
    """Non-secret facts about a token: length, JWT parts, expiry. `problem` is set when it cannot be valid."""
    t = token or ""
    info: dict[str, Any] = {"length": len(t), "jwt": False, "segments": len(t.split(".")) if t else 0}
    if not t.startswith("eyJ"):
        return info  # opaque token: nothing to check
    parts = t.split(".")
    if len(parts) != 3 or not all(parts):
        info["problem"] = (f"The Bearer token looks incomplete: {len(t)} characters and {len([p for p in parts if p])} "
                           "of the 3 JWT parts. It was probably cut off when it was copied. Copy the whole token again.")
        return info
    try:
        raw = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(raw))
    except Exception:  # noqa: BLE001
        info["problem"] = "The Bearer token is not a readable JWT (the middle part cannot be decoded). Copy it again."
        return info
    info["jwt"] = True
    exp = payload.get("exp") if isinstance(payload, dict) else None
    if isinstance(exp, (int, float)):
        info["exp"] = datetime.fromtimestamp(exp, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        info["expires_in_s"] = int(exp - time.time())
        info["expired"] = info["expires_in_s"] <= 0
    return info


AUTH_TYPES = ("inherit", "bearer", "profile")


def resolve_auth(request_auth: dict | None, folder_auths: list[dict | None]) -> tuple[dict | None, str]:
    """Nearest wins: the request's own auth (Bearer token or auth profile), else the closest folder that
    sets one. Returns (auth config or None, source)."""
    ra = request_auth or {}
    if ra.get("type") in ("bearer", "profile"):
        return ra, "request"
    for i, fa in enumerate(folder_auths):
        fa = fa or {}
        if fa.get("type") in ("bearer", "profile"):
            return fa, "folder" if i == 0 else f"folder (+{i} up)"
    return None, "none"


def resolve_token_template(request_auth: dict | None, folder_auths: list[dict | None]) -> tuple[str | None, str]:
    """Like resolve_auth, for a Bearer token: (token template or None, source)."""
    cfg, source = resolve_auth(request_auth, folder_auths)
    if cfg is None or cfg.get("type") != "bearer":
        return None, source
    return cfg.get("token") or "", source


def is_authorization_header(key: str | None) -> bool:
    return (key or "").strip().lower() == "authorization"


def check_headers(headers: list[dict] | None) -> list[str]:
    return [f"Header '{h.get('key')}' is not allowed: the Bearer token comes from the Auth tab only."
            for h in (headers or []) if is_authorization_header(h.get("key"))]


def _scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return "" if v is None else str(v)


def prepare(spec: RequestSpec, variables: dict[str, str], *, item: dict | None = None,
            query: dict | None = None, body: Any = None, path: str | None = None,
            fetched_token: Any = None) -> Prepared:
    """fetched_token: an oauth.TokenResult, required when the spec uses an auth profile."""
    errors: list[str] = []
    warnings: list[str] = []
    method = spec.method.upper()
    url, missing = render(spec.url, variables)
    if missing:
        errors.append(f"URL uses undefined variable(s): {', '.join(sorted(missing))}")
    if path:
        url = url.rstrip("/") + "/" + str(path).lstrip("/")
    if not url.lower().startswith(("http://", "https://")):
        errors.append(f"URL must start with http:// or https:// (got '{url[:80]}')")

    # The app adds only "Authorization: Bearer <token>" and, for requests with a JSON body,
    # "Content-Type: application/json". No Accept header. Anything else comes from the Headers tab.
    headers: dict[str, str] = {}
    for h in spec.headers or []:
        if not h.get("enabled", True) or not (h.get("key") or "").strip():
            continue
        if is_authorization_header(h.get("key")):
            warnings.append("Manual Authorization header ignored; use the Auth tab.")
            continue
        v, miss = render(h.get("value") or "", variables)
        if miss:
            errors.append(f"Header '{h['key']}' uses undefined variable(s): {', '.join(sorted(miss))}")
        headers[h["key"].strip()] = v
    for k, v in ((item or {}).get("headers") or {}).items():
        if is_authorization_header(k):
            warnings.append("Authorization header from the script ignored; use the Auth tab.")
            continue
        headers[str(k)] = _scalar(v)

    params: list[tuple[str, str]] = []
    for p in spec.params or []:
        if not p.get("enabled", True) or not (p.get("key") or "").strip():
            continue
        v, miss = render(p.get("value") or "", variables)
        if miss:
            errors.append(f"Param '{p['key']}' uses undefined variable(s): {', '.join(sorted(miss))}")
        params.append((p["key"].strip(), v))
    override = dict((item or {}).get("query") or {})
    override.update(query or {})
    if override:
        params = [(k, v) for k, v in params if k not in override]
        for k, v in override.items():
            for one in (v if isinstance(v, (list, tuple)) else [v]):
                params.append((str(k), _scalar(one)))

    token = ""
    token_problem = False
    profile_name = spec.auth_profile.name if spec.auth_profile is not None else None
    if spec.auth_profile is not None:
        tr = fetched_token
        if tr is None or not getattr(tr, "token", ""):
            why = getattr(tr, "error", None) or "no token was fetched"
            errors.append(f"Could not get a Bearer token from auth profile '{profile_name}': {why}")
            token_problem = True
        else:
            token = normalize_token(tr.token)
    elif spec.token_template is None:
        errors.append("No Bearer token configured. Set a token or an auth profile on the request or a parent "
                      "folder (Auth tab).")
        token_problem = True
    else:
        token, miss = render(spec.token_template, variables)
        token = normalize_token(token)
        problem = token_info(token).get("problem") if token else None
        if problem:
            errors.append(problem)
            token_problem = True
            token = ""
        if miss:
            errors.append(f"Bearer token uses undefined variable(s): {', '.join(sorted(miss))}")
            token_problem = True
        elif not token and not problem:
            errors.append("Bearer token is empty. Paste a token into the 'bearer_token' environment variable "
                          "(or the Auth tab) first.")
            token_problem = True
    if token:
        headers["Authorization"] = f"Bearer {token}"

    payload = body if body is not None else (item or {}).get("body")
    has_body = payload is not None and method not in ("GET", "HEAD")
    if payload is not None and method in ("GET", "HEAD"):
        warnings.append(f"{method} requests are sent without a body; the item body was ignored.")
    if has_body and not any(k.lower() == "content-type" for k in headers):
        headers["Content-Type"] = "application/json"

    full_url = url
    if not errors or url.lower().startswith(("http://", "https://")):
        try:
            full_url = str(httpx.URL(url).copy_merge_params(params)) if params else url
        except Exception as e:  # noqa: BLE001
            errors.append(f"Invalid URL: {e}")
    return Prepared(method=method, url=full_url, headers=headers, body=payload if has_body else None,
                    has_body=has_body, token=token, errors=errors, warnings=warnings, token_problem=token_problem,
                    auth_profile_name=profile_name)


def _result_from_response(resp: httpx.Response, t0: float) -> HttpResult:
    text = resp.text
    parsed = None
    if text:
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
    return HttpResult(status=resp.status_code, duration_ms=int((time.monotonic() - t0) * 1000),
                      text=text, json=parsed, headers=dict(resp.headers))


def _error_text(e: Exception, timeout_s: float) -> str:
    if isinstance(e, httpx.TimeoutException):
        return f"Timeout after {timeout_s:g}s"
    if isinstance(e, httpx.ConnectError):
        return f"Connection error: {e}"
    return f"{type(e).__name__}: {e}"


def _kwargs(p: Prepared) -> dict:
    kw: dict = {"headers": p.headers}
    if p.has_body:
        kw["content"] = json.dumps(p.body, ensure_ascii=False).encode("utf-8")
    return kw


async def send_async(client: httpx.AsyncClient, p: Prepared, timeout_s: float) -> HttpResult:
    t0 = time.monotonic()
    try:
        resp = await client.request(p.method, p.url, timeout=timeout_s, **_kwargs(p))
        return _result_from_response(resp, t0)
    except Exception as e:  # noqa: BLE001
        return HttpResult(status=None, duration_ms=int((time.monotonic() - t0) * 1000), error=_error_text(e, timeout_s))


def send_sync(p: Prepared, timeout_s: float) -> HttpResult:
    t0 = time.monotonic()
    try:
        with httpx.Client(follow_redirects=False) as c:
            resp = c.request(p.method, p.url, timeout=timeout_s, **_kwargs(p))
        return _result_from_response(resp, t0)
    except Exception as e:  # noqa: BLE001
        return HttpResult(status=None, duration_ms=int((time.monotonic() - t0) * 1000), error=_error_text(e, timeout_s))


def evaluate(res: HttpResult, success: dict) -> tuple[bool, str]:
    """Apply the success criteria. Returns (ok, reason_if_failed)."""
    if res.status is None:
        return False, res.error or "No response"
    if success.get("require_2xx", True) and not 200 <= res.status < 300:
        return False, f"HTTP {res.status}"
    field_name = (success.get("code_field") or "").strip()
    if field_name and isinstance(res.json, dict) and field_name in res.json:
        code = res.json[field_name]
        if isinstance(code, (int, float)) and not isinstance(code, bool):
            expected = success.get("code_values")
            if expected is None:
                expected = [success.get("code_value", 200)]
            allowed = {float(v) for v in expected}
            if float(code) not in allowed:
                exp = "/".join(f"{v:g}" for v in sorted(allowed))
                return False, f"{field_name} {code:g} != {exp}"
    return True, ""


def response_message(res: HttpResult, limit: int = 300) -> str:
    if res.status is None:
        return res.error or "No response"
    if isinstance(res.json, dict):
        for k in ("msg", "message", "error", "detail"):
            v = res.json.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()[:limit]
            if v is not None and not isinstance(v, str):
                return json.dumps(v, ensure_ascii=False)[:limit]
    snippet = " ".join((res.text or "").split())
    return snippet[:limit] if snippet else f"HTTP {res.status} (empty body)"


def should_retry(res: HttpResult, retry: dict, attempt: int) -> bool:
    """attempt = number of attempts already made (1 after the first)."""
    if attempt > int(retry.get("max_retries") or 0):
        return False
    if res.status is None:
        return bool(retry.get("on_network_error"))
    return res.status in set(retry.get("on_status") or []) and res.status not in NEVER_RETRY


def backoff_seconds(retry: dict, attempt: int) -> float:
    base = max(0, int(retry.get("backoff_ms") or 0)) / 1000
    return min(30.0, base * (2 ** max(0, attempt - 1)))


def display_headers(p: Prepared, secrets: list[str]) -> dict[str, str]:
    """Headers safe to show/store: no Authorization, secrets masked."""
    return {k: mask_text(v, secrets) for k, v in p.headers.items() if not is_authorization_header(k)}


def to_curl(p: Prepared, secrets: list[str]) -> str:
    """curl command for this request; the token and secret values are masked."""
    def q(v: str) -> str:
        return "'" + v.replace("'", "'\"'\"'") + "'"
    parts = [f"curl --request {p.method}", f"  --url {q(mask_text(p.url, secrets))}"]
    for k, v in p.headers.items():
        v = f"Bearer {MASK}" if is_authorization_header(k) else mask_text(v, secrets)
        parts.append(f"  --header {q(f'{k}: {v}')}")
    if p.has_body:
        compact = json.dumps(p.body, ensure_ascii=False, separators=(",", ":"))
        parts.append(f"  --data {q(mask_text(compact, secrets))}")
    return " \\\n".join(parts)


def accepted_data(http_status: int | None, response_body: str | None) -> dict | None:
    """For 202 Accepted responses: the `data` object of the envelope (upload_id, schedule_count, ...)."""
    if http_status != 202 or not response_body:
        return None
    try:
        data = json.loads(response_body).get("data")
    except (ValueError, AttributeError):
        return None
    return data if isinstance(data, dict) else None
