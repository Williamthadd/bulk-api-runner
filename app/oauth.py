"""Auth profiles: fetch Bearer tokens from an OAuth2 token endpoint (client credentials grant)
and cache them in memory until shortly before they expire.

Tokens are never written to the database or the logs. The client secret is only used here, for the
POST to the token endpoint, and is masked in every error message.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .templating import mask_text, render

log = logging.getLogger("bulk_runner.oauth")

GRANT_TYPES = ("client_credentials",)
FETCH_TIMEOUT_S = 15.0
DEFAULT_LIFETIME_S = 300      # when the endpoint does not say how long the token lives
MAX_MARGIN_S = 30             # refresh this long before expiry (or 10% of the lifetime if shorter)


@dataclass
class ProfileSpec:
    """A decrypted auth profile, ready to use."""
    id: int
    name: str
    token_url: str
    client_id: str
    client_secret: str
    grant_type: str = "client_credentials"
    scope: str = ""
    secret_unreadable: bool = False


@dataclass
class TokenResult:
    token: str = ""
    error: str | None = None
    expires_at: float | None = None   # epoch seconds
    fetched_at: float | None = None
    from_cache: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.token) and not self.error


@dataclass
class _Entry:
    token: str
    expires_at: float
    fetched_at: float
    refresh_at: float


_cache: dict[tuple, _Entry] = {}
_latest: dict[int, tuple] = {}          # profile id -> cache key of its most recent token
_locks: dict[tuple, threading.Lock] = {}
_guard = threading.Lock()


def _lock_for(key: tuple) -> threading.Lock:
    with _guard:
        return _locks.setdefault(key, threading.Lock())


def resolve(p: ProfileSpec, variables: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """Render {{variables}} in the profile fields. Returns (values, errors)."""
    vals, errors = {}, []
    for field in ("token_url", "client_id", "client_secret", "scope"):
        v, miss = render(getattr(p, field) or "", variables)
        if miss:
            errors.append(f"{field} uses undefined variable(s): {', '.join(sorted(miss))}")
        vals[field] = v.strip()
    if p.secret_unreadable:
        errors.append("the saved client secret cannot be decrypted (encryption key changed). Enter it again.")
    if not vals["token_url"].lower().startswith(("http://", "https://")):
        errors.append("the token URL must start with http:// or https://")
    if not vals["client_id"]:
        errors.append("client_id is empty")
    if not vals["client_secret"]:
        errors.append("client_secret is empty")
    if p.grant_type not in GRANT_TYPES:
        errors.append(f"grant_type '{p.grant_type}' is not supported (only client_credentials)")
    return vals, errors


def _key(p: ProfileSpec, vals: dict[str, str]) -> tuple:
    raw = "\x00".join([vals["token_url"], vals["client_id"], vals["client_secret"], p.grant_type, vals["scope"]])
    return (p.id, hashlib.sha256(raw.encode()).hexdigest())


def _jwt_exp(token: str) -> float | None:
    from .http_engine import token_info  # local import: http_engine is the heavier module
    info = token_info(token)
    return time.time() + info["expires_in_s"] if info.get("expires_in_s") is not None else None


def _describe_error(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        data = None
    if isinstance(data, dict):
        for k in ("error_description", "error", "msg", "message", "detail"):
            v = data.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()[:300]
    snippet = " ".join((resp.text or "").split())[:300]
    return snippet or "empty response"


def _fetch(p: ProfileSpec, vals: dict[str, str], timeout_s: float) -> TokenResult:
    form = {"grant_type": p.grant_type, "client_id": vals["client_id"], "client_secret": vals["client_secret"]}
    if vals["scope"]:
        form["scope"] = vals["scope"]
    secrets = [vals["client_secret"]]
    t0 = time.time()
    try:
        with httpx.Client(follow_redirects=False) as c:
            resp = c.post(vals["token_url"], data=form, timeout=timeout_s)
    except httpx.TimeoutException:
        return TokenResult(error=f"the token endpoint did not answer within {timeout_s:g}s")
    except Exception as e:  # noqa: BLE001
        return TokenResult(error=mask_text(f"cannot reach the token endpoint: {type(e).__name__}: {e}", secrets))
    if not 200 <= resp.status_code < 300:
        log.info("auth profile '%s': token endpoint answered HTTP %s", p.name, resp.status_code)
        return TokenResult(error=mask_text(f"the token endpoint answered HTTP {resp.status_code}: "
                                           f"{_describe_error(resp)}", secrets))
    try:
        data = resp.json()
    except ValueError:
        data = None
    token = data.get("access_token") if isinstance(data, dict) else None
    if not isinstance(token, str) or not token.strip():
        return TokenResult(error="the token endpoint answered without an access_token")
    token = token.strip()
    exp_in = data.get("expires_in")
    if isinstance(exp_in, (int, float)) and not isinstance(exp_in, bool) and exp_in > 0:
        expires_at = t0 + float(exp_in)
    else:
        expires_at = _jwt_exp(token) or (t0 + DEFAULT_LIFETIME_S)
    log.info("auth profile '%s': fetched a new token (valid %ds)", p.name, int(expires_at - t0))
    return TokenResult(token=token, expires_at=expires_at, fetched_at=t0)


def get_token(p: ProfileSpec, variables: dict[str, str], *, force: bool = False,
              timeout_s: float = FETCH_TIMEOUT_S) -> TokenResult:
    """A valid token for this profile: from the cache, or freshly fetched."""
    vals, errors = resolve(p, variables)
    if errors:
        return TokenResult(error="; ".join(errors))
    key = _key(p, vals)
    with _lock_for(key):
        now = time.time()
        e = _cache.get(key)
        if e and not force and now < e.refresh_at:
            return TokenResult(token=e.token, expires_at=e.expires_at, fetched_at=e.fetched_at, from_cache=True)
        res = _fetch(p, vals, timeout_s)
        if res.ok:
            lifetime = max(1.0, res.expires_at - res.fetched_at)
            _cache[key] = _Entry(res.token, res.expires_at, res.fetched_at,
                                 refresh_at=res.expires_at - min(MAX_MARGIN_S, lifetime * 0.1))
            _latest[p.id] = key
        else:
            _cache.pop(key, None)
        return res


def invalidate(profile_id: int) -> None:
    """Forget cached tokens of a profile (after a 401, or when the profile changes)."""
    with _guard:
        for k in [k for k in _cache if k[0] == profile_id]:
            _cache.pop(k, None)
        _latest.pop(profile_id, None)


def cached_status(profile_id: int) -> dict[str, Any] | None:
    """Non-secret facts about the most recent cached token, without fetching."""
    key = _latest.get(profile_id)
    e = _cache.get(key) if key else None
    if not e:
        return None
    now = time.time()
    return {"length": len(e.token), "expires_in_s": int(e.expires_at - now), "expired": e.expires_at <= now,
            "fetched_at": _iso(e.fetched_at), "exp": _iso(e.expires_at)}


def clear() -> None:
    with _guard:
        _cache.clear()
        _latest.clear()


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
