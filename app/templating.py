"""{{variable}} substitution, secret masking and truncation helpers."""
from __future__ import annotations

import re
from typing import Iterable

VAR_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*\}\}")
PURE_REF_RE = re.compile(r"^\s*\{\{\s*[A-Za-z0-9_.\-]+\s*\}\}\s*$")
MASK = "****"
MAX_STORED_BYTES = 64 * 1024


def render(template: str | None, variables: dict[str, str]) -> tuple[str, set[str]]:
    """Replace {{name}} with variables[name]. Returns (text, missing_names)."""
    missing: set[str] = set()
    if not template:
        return "", missing

    def sub(m: re.Match) -> str:
        name = m.group(1)
        if name in variables:
            return str(variables[name])
        missing.add(name)
        return m.group(0)

    return VAR_RE.sub(sub, template), missing


def is_pure_reference(value: str | None) -> bool:
    """True if the value is only a variable reference like '{{bearer_token}}' (not a secret itself)."""
    return bool(value and PURE_REF_RE.match(value))


def mask_text(text: str | None, secrets: Iterable[str]) -> str:
    if not text:
        return text or ""
    for s in sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True):
        text = text.replace(s, MASK)
    return text


def truncate_bytes(text: str | None, limit: int = MAX_STORED_BYTES) -> str:
    if not text:
        return text or ""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    note = f"\n... [truncated, {len(raw)} bytes total]"
    cut = raw[: limit - len(note.encode("utf-8"))].decode("utf-8", errors="ignore")
    return cut + note


def mask_and_truncate(text: str | None, secrets: Iterable[str], limit: int = MAX_STORED_BYTES) -> str:
    return truncate_bytes(mask_text(text, secrets), limit)
