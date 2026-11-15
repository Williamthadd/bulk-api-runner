"""Sequential bulk sender with pause/resume/stop, retry and pause-on-401."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Protocol

import httpx

from . import http_engine as he
from .models import utcnow
from .templating import mask_and_truncate, mask_text

log = logging.getLogger("bulk_runner.runner")

RUNNING, PAUSED, DONE, STOPPED, FAILED = "Running", "Paused", "Done", "Stopped", "Failed"
ACTIVE = {RUNNING, PAUSED}
BANNER_401 = "Token expired or invalid, update the token and click Resume"
BANNER_TOKEN = "Bearer token is missing, set the token and click Resume"
BANNER_401_PROFILE = ("The server answered 401 even with a freshly fetched token from auth profile '{name}'. "
                      "Check the profile (client, permissions) and click Resume")
BANNER_PROFILE = "Could not get a token from auth profile '{name}'. Fix the profile and click Resume"


class RunBackend(Protocol):
    def prepare(self, item: dict) -> tuple[he.Prepared, list[str]]: ...
    def persist_run(self, state: "RunState") -> int | None: ...
    def persist_item(self, state: "RunState", row: dict) -> None: ...
    def after_finish(self, state: "RunState") -> None: ...
    # optional: def on_unauthorized(self) -> bool  (refresh an auto-fetched token after a 401)


@dataclass
class RunState:
    request_id: int | None
    request_name: str
    environment_name: str
    mode: str
    method: str
    url_display: str
    items: list[dict]                # each: {"seq", "label", "body", ...}
    settings: dict
    skipped_count: int = 0
    id: int | None = None
    status: str = RUNNING
    rows: dict[int, dict] = field(default_factory=dict)
    success_count: int = 0
    failed_count: int = 0
    started_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None
    banner: str | None = None
    pause_reason: str | None = None   # "user" | "401" | "token"
    stop_reason: str | None = None
    db_error: str | None = None
    unsaved_rows: int = 0
    current_seq: int | None = None
    _wake: asyncio.Event = field(default_factory=asyncio.Event)
    _pause_requested: bool = False
    _stop_requested: bool = False
    task: asyncio.Task | None = None
    backend: Any = None

    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def done_count(self) -> int:
        return self.success_count + self.failed_count

    def header(self) -> dict:
        return {
            "id": self.id, "request_id": self.request_id, "request_name": self.request_name,
            "environment_name": self.environment_name, "mode": self.mode, "status": self.status,
            "total": self.total, "success_count": self.success_count, "failed_count": self.failed_count,
            "skipped_count": self.skipped_count, "done_count": self.done_count,
            "started_at": iso(self.started_at), "finished_at": iso(self.finished_at),
            "banner": self.banner, "pause_reason": self.pause_reason, "stop_reason": self.stop_reason,
            "method": self.method, "url": self.url_display, "idempotent": bool(self.settings.get("idempotent")),
            "current_seq": self.current_seq, "db_error": self.db_error, "unsaved_rows": self.unsaved_rows,
            "live": True,
        }


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="milliseconds") + "Z" if dt else None


def public_row(row: dict) -> dict:
    return {k: (iso(v) if isinstance(v, datetime) else v) for k, v in row.items() if not k.startswith("_")}


def row_summary(row: dict) -> dict:
    """Row for the log table / SSE: no request/response bodies, plus the 202 `data` if any."""
    out = public_row(row)
    out.pop("request_body", None)
    out["accepted"] = he.accepted_data(row.get("http_status"), out.pop("response_body", None))
    return out


class RunManager:
    def __init__(self, client_factory: Callable[[], httpx.AsyncClient] | None = None) -> None:
        self.runs: dict[int, RunState] = {}
        self._subscribers: set[asyncio.Queue] = set()
        self._client_factory = client_factory or (lambda: httpx.AsyncClient(follow_redirects=False))
        self._local_ids = -1

    # ---- events -------------------------------------------------------------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=5000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def publish(self, event: dict) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def _run_event(self, st: RunState) -> None:
        self.publish({"type": "run", "run": st.header()})

    def _item_event(self, st: RunState, row: dict) -> None:
        self.publish({"type": "item", "run_id": st.id, "item": row_summary(row)})

    # ---- lifecycle ----------------------------------------------------------
    def active_run(self) -> RunState | None:
        for st in self.runs.values():
            if st.status in ACTIVE:
                return st
        return None

    async def start(self, st: RunState, backend: RunBackend) -> RunState:
        if self.active_run():
            raise RuntimeError("Another run is still in progress. Stop it or wait for it to finish.")
        st.backend = backend
        await self._db(st, backend.persist_run, st)
        if st.id is None:
            st.id = self._local_ids
            self._local_ids -= 1
        self.runs[st.id] = st
        st.task = asyncio.create_task(self._execute(st, backend))
        self._run_event(st)
        return st

    def pause(self, run_id: int) -> RunState:
        st = self._get(run_id)
        if st.status == RUNNING:
            st._pause_requested = True
            st.banner = "Pausing after the current item..."
            self._run_event(st)
        return st

    def resume(self, run_id: int) -> RunState:
        st = self._get(run_id)
        if st.status in ACTIVE:
            st._pause_requested = False
            st.banner = None
            st._wake.set()
        return st

    def stop(self, run_id: int) -> RunState:
        st = self._get(run_id)
        if st.status in ACTIVE:
            st._stop_requested = True
            st._pause_requested = False
            st._wake.set()
        return st

    def _get(self, run_id: int) -> RunState:
        st = self.runs.get(run_id)
        if not st:
            raise KeyError(run_id)
        return st

    async def _db(self, st: RunState, fn, *args) -> Any:
        try:
            res = await asyncio.to_thread(fn, *args)
            if st.db_error:
                st.db_error = None
                self._run_event(st)
            return res
        except Exception as e:  # noqa: BLE001 - DB down must not kill the run
            log.warning("persist failed: %s", type(e).__name__)
            if getattr(fn, "__name__", "") == "persist_item":
                st.unsaved_rows += 1
            st.db_error = "Database unavailable: run log is not being saved"
            self._run_event(st)
            return None

    async def _wait_paused(self, st: RunState, reason: str, banner: str | None) -> bool:
        """Block until resumed (True) or stopped (False)."""
        st.status, st.pause_reason, st.banner = PAUSED, reason, banner
        st._wake.clear()
        await self._db(st, st.backend.persist_run, st)
        self._run_event(st)
        while not st._stop_requested:
            await st._wake.wait()
            st._wake.clear()
            if not st._pause_requested:
                break
        if st._stop_requested:
            return False
        st.status, st.pause_reason, st.banner = RUNNING, None, None
        self._run_event(st)
        return True

    async def _sleep(self, st: RunState, seconds: float) -> bool:
        """Interruptible sleep. False if a stop was requested."""
        if seconds <= 0:
            return not st._stop_requested
        st._wake.clear()
        try:
            await asyncio.wait_for(st._wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        return not st._stop_requested

    async def _execute(self, st: RunState, backend: RunBackend) -> None:
        cfg = st.settings
        try:
            async with self._client_factory() as client:
                for idx, item in enumerate(st.items):
                    if st._pause_requested and not st._stop_requested:
                        if not await self._wait_paused(st, "user", "Paused by user"):
                            break
                    if st._stop_requested:
                        break
                    st.current_seq = item["seq"]
                    row = await self._send_item(st, backend, client, item)
                    if row is None:  # stopped while paused
                        break
                    if row["success"]:
                        st.success_count += 1
                    else:
                        st.failed_count += 1
                    self._run_event(st)
                    if not row["success"] and cfg.get("stop_on_error"):
                        st.status = FAILED
                        st.stop_reason = f"Stopped on first error (item {item['seq']})"
                        break
                    if idx < len(st.items) - 1 and cfg.get("delay_ms"):
                        if not await self._sleep(st, cfg["delay_ms"] / 1000):
                            break
        except Exception as e:  # noqa: BLE001
            log.exception("run crashed")
            st.status = FAILED
            st.stop_reason = f"Runner error: {type(e).__name__}: {e}"
        if st.status not in (FAILED,):
            st.status = STOPPED if st._stop_requested else DONE
            if st._stop_requested:
                st.stop_reason = st.stop_reason or "Stopped by user"
        st.current_seq = None
        st.banner = None
        st.pause_reason = None
        st.finished_at = utcnow()
        await self._db(st, backend.persist_run, st)
        await self._db(st, backend.after_finish, st)
        self._run_event(st)

    async def _send_item(self, st: RunState, backend: RunBackend, client: httpx.AsyncClient, item: dict) -> dict | None:
        cfg = st.settings
        retry = cfg["retry"]
        attempts = 0
        row: dict = {"seq": item["seq"], "label": item.get("label") or f"item {item['seq']}", "method": st.method,
                     "url": st.url_display, "http_status": None, "duration_ms": None, "success": False,
                     "attempts": 0, "message": "", "request_body": "", "response_body": "",
                     "created_at": utcnow(), "state": "sending"}
        st.rows[item["seq"]] = row
        refreshed = False   # one automatic token refresh per item (auth profiles)
        while True:
            if st._stop_requested:
                return None if attempts == 0 else row
            prepared, secrets = await asyncio.to_thread(backend.prepare, item)
            row["method"] = prepared.method
            row["url"] = mask_text(prepared.url, secrets)
            row["request_body"] = self._request_text(prepared, secrets)
            if not prepared.ok:
                if prepared.token_problem and cfg.get("pause_on_401"):
                    row.update(state="paused", message="; ".join(prepared.errors))
                    self._item_event(st, row)
                    banner = (BANNER_PROFILE.format(name=prepared.auth_profile_name)
                              if prepared.auth_profile_name else BANNER_TOKEN)
                    if not await self._wait_paused(st, "token", banner):
                        row.update(state="done", message="Not sent: " + "; ".join(prepared.errors))
                        await self._db(st, backend.persist_item, st, row)
                        return row
                    continue
                row.update(state="done", success=False, message="Not sent: " + "; ".join(prepared.errors))
                await self._db(st, backend.persist_item, st, row)
                self._item_event(st, row)
                return row

            attempts += 1
            row["attempts"] = attempts
            row["state"] = "sending"
            self._item_event(st, row)
            res = await he.send_async(client, prepared, cfg["timeout_s"])
            ok, reason = he.evaluate(res, cfg["success"])
            msg = he.response_message(res)
            # e.g. HTTP 200 with {"code": 400}: say why it counts as a failure
            if not ok and res.status is not None and reason != f"HTTP {res.status}":
                msg = f"{reason}: {msg}"
            row.update(http_status=res.status, duration_ms=res.duration_ms, success=ok,
                       message=mask_text(msg, secrets),
                       response_body=mask_and_truncate(res.text if res.status is not None else (res.error or ""), secrets),
                       created_at=utcnow())

            if res.status == 401 and not refreshed and prepared.auth_profile_name:
                hook = getattr(backend, "on_unauthorized", None)
                if hook and await asyncio.to_thread(hook):
                    refreshed = True
                    row.update(state="retrying",
                               message=mask_text(f"401 Unauthorized: fetching a new token from auth profile "
                                                 f"'{prepared.auth_profile_name}' and sending again", secrets))
                    self._item_event(st, row)
                    continue
            if res.status == 401 and cfg.get("pause_on_401"):
                row.update(state="paused", message=mask_text(f"401 Unauthorized, paused: {msg}", secrets))
                await self._db(st, backend.persist_item, st, row)
                self._item_event(st, row)
                banner = (BANNER_401_PROFILE.format(name=prepared.auth_profile_name)
                          if prepared.auth_profile_name else BANNER_401)
                if not await self._wait_paused(st, "401", banner):
                    row["state"] = "done"
                    return row
                refreshed = False
                continue  # re-send this same item with the (possibly updated) token
            if not ok and he.should_retry(res, retry, attempts):
                wait = he.backoff_seconds(retry, attempts)
                row.update(state="retrying",
                           message=mask_text(f"{msg} (retry {attempts}/{retry['max_retries']} in {wait:g}s)", secrets))
                self._item_event(st, row)
                if not await self._sleep(st, wait):
                    row["state"] = "done"
                    await self._db(st, backend.persist_item, st, row)
                    return row
                continue
            row["state"] = "done"
            await self._db(st, backend.persist_item, st, row)
            self._item_event(st, row)
            return row

    @staticmethod
    def _request_text(p: he.Prepared, secrets: list[str]) -> str:
        head = "\n".join(f"{k}: {v}" for k, v in he.display_headers(p, secrets).items())
        body = json.dumps(p.body, ensure_ascii=False, indent=2) if p.has_body else "(no body)"
        return mask_and_truncate(f"{head}\n\n{body}" if head else body, secrets)
