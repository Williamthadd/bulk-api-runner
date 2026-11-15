"""Parent side of the script runner: spawns worker.py, answers ctx.call RPCs, enforces a timeout."""
from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

WORKER = str(Path(__file__).with_name("worker.py"))
MAX_LOG_LINES = 2000


class DryRunBlocked(Exception):
    pass


class CallFailed(Exception):
    pass


# call_handler(request_name, query, body, path) -> parsed JSON; may raise DryRunBlocked / CallFailed / anything
CallHandler = Callable[[str, Any, Any, Any], Any]


@dataclass
class ScriptResult:
    ok: bool
    items: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    rows_read: int = 0
    columns: list[str] = field(default_factory=list)
    error: str | None = None
    traceback: str | None = None
    line: int | None = None
    duration_ms: int = 0
    memory_limit_applied: bool = False

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def _short(v: Any) -> str:
    if not v:
        return ""
    s = json.dumps(v, default=str, ensure_ascii=False)
    return s if len(s) <= 120 else s[:117] + "..."


def run_script(script: str, xlsx_path: str | None, *, sheet: str | None = None, header_row: int = 1,
               variables: dict | None = None, call_handler: CallHandler | None = None,
               timeout_s: float = 60, memory_mb: int = 0, dry_run: bool = True) -> ScriptResult:
    """Blocking. Run from a worker thread (asyncio.to_thread) inside the web app."""
    t0 = time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, "-I", "-u", WORKER],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", bufsize=1,
    )
    lines: queue.Queue = queue.Queue()
    stderr_buf: list[str] = []

    def _read_stdout() -> None:
        assert proc.stdout
        for ln in proc.stdout:
            lines.put(ln)
        lines.put(None)

    def _read_stderr() -> None:
        assert proc.stderr
        for ln in proc.stderr:
            if sum(map(len, stderr_buf)) < 20000:
                stderr_buf.append(ln)

    threading.Thread(target=_read_stdout, daemon=True).start()
    threading.Thread(target=_read_stderr, daemon=True).start()

    def _send(msg: dict) -> None:
        assert proc.stdin
        proc.stdin.write(json.dumps(msg, default=str) + "\n")
        proc.stdin.flush()

    logs: list[str] = []
    result = ScriptResult(ok=False)

    def _log(line: str) -> None:
        if len(logs) < MAX_LOG_LINES:
            logs.append(line)
        elif len(logs) == MAX_LOG_LINES:
            logs.append(f"... log truncated after {MAX_LOG_LINES} lines")
    try:
        _send({"script": script, "xlsx_path": xlsx_path, "sheet": sheet, "header_row": header_row,
               "vars": variables or {}, "dry_run": dry_run, "memory_mb": memory_mb})
        deadline = t0 + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            try:
                ln = lines.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError from None
            if ln is None:  # worker exited without a final message
                proc.wait(timeout=5)
                tail = "".join(stderr_buf).strip()[-2000:]
                code = proc.returncode
                hint = " (possibly out of memory)" if code in (-9, 137, -6, 134) else ""
                result = ScriptResult(ok=False, error=f"Script process exited unexpectedly (code {code}){hint}",
                                      traceback=tail or None)
                break
            try:
                msg = json.loads(ln)
            except json.JSONDecodeError:
                logs.append(ln.rstrip("\n"))
                continue
            t = msg.get("type")
            if t == "log":
                _log(msg.get("msg", ""))
            elif t == "call":
                reply: dict = {"type": "call_result", "id": msg.get("id")}
                try:
                    if call_handler is None:
                        raise CallFailed("ctx.call is not available here")
                    reply.update(ok=True, data=call_handler(msg.get("request"), msg.get("query"),
                                                            msg.get("body"), msg.get("path")))
                    _log(f"[ctx.call] {msg.get('request')} {_short(msg.get('query'))} -> ok")
                except DryRunBlocked as e:
                    reply.update(ok=False, error_type="DryRunBlocked", error=str(e))
                    _log(f"[ctx.call] {msg.get('request')} -> blocked: {e}")
                except Exception as e:  # noqa: BLE001
                    reply.update(ok=False, error_type="CallError", error=str(e) or type(e).__name__)
                    _log(f"[ctx.call] {msg.get('request')} {_short(msg.get('query'))} -> failed: {reply['error']}")
                _send(reply)
            elif t == "result":
                result = ScriptResult(ok=True, items=msg.get("items") or [], skipped=msg.get("skipped") or [],
                                      warnings=msg.get("warnings") or [], rows_read=msg.get("rows_read") or 0,
                                      columns=msg.get("columns") or [],
                                      memory_limit_applied=bool(msg.get("memory_limit_applied")))
                break
            elif t == "error":
                result = ScriptResult(ok=False, error=msg.get("error"), traceback=msg.get("traceback"),
                                      line=msg.get("line"), rows_read=msg.get("rows_read") or 0,
                                      skipped=msg.get("skipped") or [], warnings=msg.get("warnings") or [])
                break
    except TimeoutError:
        result = ScriptResult(ok=False, error=f"Script timed out after {timeout_s:g} seconds and was killed")
    except (BrokenPipeError, OSError) as e:
        tail = "".join(stderr_buf).strip()[-2000:]
        result = ScriptResult(ok=False, error=f"Script process failed: {e}", traceback=tail or None)
    finally:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        for s in (proc.stdin, proc.stdout, proc.stderr):
            try:
                s and s.close()
            except Exception:  # noqa: BLE001
                pass
    result.logs = logs
    result.duration_ms = int((time.monotonic() - t0) * 1000)
    return result
