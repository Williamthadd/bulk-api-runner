"""Script worker. Runs in a separate process: loads the xlsx, executes the user script and
streams JSON-line messages to the parent over the original stdout. NOT a security sandbox.

Protocol (one JSON object per line):
  parent -> worker  first line: {"script", "xlsx_path", "sheet", "header_row", "vars", "dry_run", "memory_mb"}
  worker -> parent  {"type": "log", "msg"}
                    {"type": "call", "id", "request", "query", "body", "path"}
  parent -> worker  {"type": "call_result", "id", "ok", "data" | "error", "error_type"}
  worker -> parent  {"type": "result", ...} | {"type": "error", ...}   (last message)
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from types import MappingProxyType

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SCRIPT_FILE = "<script>"
ALLOWED_ITEM_KEYS = {"label", "body", "query", "headers"}


class DryRunBlocked(Exception):
    """ctx.call() attempted a non-GET/HEAD request during a dry run."""


class CallError(Exception):
    """ctx.call() got a response that is considered a failure (or could not be sent)."""


class _Proto:
    def __init__(self) -> None:
        # Keep a private handle on the real stdout; user prints go to the log instead.
        self.out = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1)
        self.inp = sys.stdin

    def send(self, msg: dict) -> None:
        self.out.write(json.dumps(msg, default=str) + "\n")
        self.out.flush()

    def recv(self) -> dict:
        line = self.inp.readline()
        if not line:
            raise SystemExit(3)
        return json.loads(line)


class _LogStream:
    def __init__(self, proto: _Proto, prefix: str = "") -> None:
        self.proto, self.prefix, self.buf = proto, prefix, ""

    def write(self, s: str) -> int:
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self.proto.send({"type": "log", "msg": self.prefix + line})
        return len(s)

    def flush(self) -> None:
        if self.buf:
            self.proto.send({"type": "log", "msg": self.prefix + self.buf})
            self.buf = ""


class Ctx:
    def __init__(self, proto: _Proto, variables: dict, dry_run: bool) -> None:
        self._proto = proto
        self._vars = MappingProxyType(dict(variables))
        self.dry_run = dry_run
        self.skipped: list[dict] = []
        self.warnings: list[dict] = []
        self._call_id = 0

    @property
    def vars(self):
        return self._vars

    def log(self, msg) -> None:
        self._proto.send({"type": "log", "msg": str(msg)})

    def skip(self, label, reason) -> None:
        self.skipped.append({"label": str(label), "reason": str(reason)})

    def warn(self, label, msg) -> None:
        self.warnings.append({"label": str(label), "msg": str(msg)})

    def call(self, request_name, query=None, body=None, path=None):
        self._call_id += 1
        cid = self._call_id
        self._proto.send({"type": "call", "id": cid, "request": str(request_name),
                          "query": query, "body": body, "path": path})
        while True:
            msg = self._proto.recv()
            if msg.get("type") == "call_result" and msg.get("id") == cid:
                break
        if msg.get("ok"):
            return msg.get("data")
        if msg.get("error_type") == "DryRunBlocked":
            raise DryRunBlocked(msg.get("error"))
        raise CallError(msg.get("error"))


def _apply_memory_limit(mb: int) -> bool:
    if not mb or mb <= 0:
        return False
    try:
        import resource  # POSIX only
    except ImportError:
        return False
    limit = mb * 1024 * 1024
    for name in ("RLIMIT_AS", "RLIMIT_DATA"):
        res = getattr(resource, name, None)
        if res is None:
            continue
        try:
            soft, hard = resource.getrlimit(res)
            new_hard = hard if hard != resource.RLIM_INFINITY and hard < limit else limit
            resource.setrlimit(res, (min(limit, new_hard), new_hard))
            return True
        except (ValueError, OSError):
            continue
    return False


def _script_line(tb) -> int | None:
    line = None
    for fr in traceback.extract_tb(tb):
        if fr.filename == SCRIPT_FILE:
            line = fr.lineno
    return line


def _format_tb(e: BaseException, script_lines: list[str]) -> str:
    frames = [fr for fr in traceback.extract_tb(e.__traceback__) if fr.filename == SCRIPT_FILE]
    out = ["Traceback (most recent call last):"]
    for fr in frames:
        out.append(f'  File "script", line {fr.lineno}, in {fr.name}')
        if fr.lineno and 0 < fr.lineno <= len(script_lines):
            out.append("    " + script_lines[fr.lineno - 1].strip())
    out.extend(l.rstrip("\n") for l in traceback.format_exception_only(type(e), e))
    return "\n".join(out)


def _validate_items(items) -> list[dict]:
    if items is None:
        raise TypeError("transform() returned None; it must return a list of items")
    if not isinstance(items, (list, tuple)):
        raise TypeError(f"transform() must return a list, got {type(items).__name__}")
    out = []
    for i, it in enumerate(items, start=1):
        if not isinstance(it, dict):
            raise TypeError(f"item {i} must be a dict like {{'label': ..., 'body': {{...}}}}, got {type(it).__name__}")
        extra = set(it) - ALLOWED_ITEM_KEYS
        if extra:
            raise TypeError(f"item {i} has unknown keys: {', '.join(sorted(extra))} "
                            f"(allowed: label, body, query, headers)")
        label = str(it.get("label") if it.get("label") is not None else f"item {i}")
        for key in ("query", "headers"):
            if it.get(key) is not None and not isinstance(it[key], dict):
                raise TypeError(f"item {i} ({label}): '{key}' must be a dict")
        clean = {"label": label, "body": it.get("body")}
        if it.get("query"):
            clean["query"] = it["query"]
        if it.get("headers"):
            clean["headers"] = {str(k): str(v) for k, v in it["headers"].items()}
        try:
            json.dumps(clean, allow_nan=False)
        except (TypeError, ValueError) as e:
            raise TypeError(f"item {i} ({label}) is not JSON serializable: {e}. "
                            "Convert dates/times to strings (e.g. d.isoformat()).") from None
        out.append(clean)
    return out


def main() -> None:
    proto = _Proto()
    sys.stdout = _LogStream(proto)
    sys.stderr = _LogStream(proto, prefix="[stderr] ")
    init = proto.recv()
    mem_applied = _apply_memory_limit(int(init.get("memory_mb") or 0))
    script: str = init.get("script") or ""
    lines = script.splitlines()
    ctx = Ctx(proto, init.get("vars") or {}, bool(init.get("dry_run", True)))
    rows_read = 0
    columns: list = []
    try:
        if init.get("xlsx_path"):
            from app.xlsx_loader import load_rows
            columns, rows = load_rows(init["xlsx_path"], init.get("sheet") or None, int(init.get("header_row") or 1))
        else:
            rows = []
        rows_read = len(rows)
        code = compile(script, SCRIPT_FILE, "exec")
        ns: dict = {"__name__": "__script__", "DryRunBlocked": DryRunBlocked, "CallError": CallError}
        exec(code, ns)
        fn = ns.get("transform")
        if not callable(fn):
            raise NameError("the script must define a function transform(rows, ctx)")
        items = _validate_items(fn(rows, ctx))
        sys.stdout.flush()
        proto.send({"type": "result", "ok": True, "items": items, "skipped": ctx.skipped,
                    "warnings": ctx.warnings, "rows_read": rows_read, "columns": columns,
                    "memory_limit_applied": mem_applied})
    except SyntaxError as e:
        sys.stdout.flush()
        msg = f"SyntaxError: {e.msg} (line {e.lineno})"
        proto.send({"type": "error", "error": msg, "traceback": msg, "line": e.lineno,
                    "rows_read": rows_read, "skipped": ctx.skipped, "warnings": ctx.warnings})
    except BaseException as e:  # noqa: BLE001 - report everything, including SystemExit/MemoryError
        try:
            sys.stdout.flush()
        except Exception:  # noqa: BLE001
            pass
        proto.send({"type": "error", "error": f"{type(e).__name__}: {e}",
                    "traceback": _format_tb(e, lines), "line": _script_line(e.__traceback__),
                    "rows_read": rows_read, "skipped": ctx.skipped, "warnings": ctx.warnings})


if __name__ == "__main__":
    main()
