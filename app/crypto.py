"""Application-level encryption of secrets (Fernet). The key lives in data/secret.key, never in the DB."""
from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .config import settings

PREFIX = "enc:v1:"
_fernet: Fernet | None = None


class SecretUnreadable(Exception):
    """Stored secret cannot be decrypted (key lost or changed)."""


def load_or_create_key(path: Path | None = None) -> bytes:
    path = path or settings.key_file
    if path.exists():
        return path.read_bytes().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    try:
        os.chmod(path, 0o600)
    except OSError:  # e.g. Windows
        pass
    return key


def init(key: bytes | None = None) -> None:
    global _fernet
    _fernet = Fernet(key or load_or_create_key())


def _f() -> Fernet:
    if _fernet is None:
        init()
    return _fernet  # type: ignore[return-value]


def encrypt(plain: str | None) -> str:
    if not plain:
        return ""
    return PREFIX + _f().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt(stored: str | None) -> str:
    if not stored:
        return ""
    if not stored.startswith(PREFIX):
        return stored  # legacy / plaintext
    try:
        return _f().decrypt(stored[len(PREFIX):].encode("ascii")).decode("utf-8")
    except InvalidToken as e:
        raise SecretUnreadable("secret cannot be decrypted with the current key") from e


def try_decrypt(stored: str | None) -> tuple[str, bool]:
    """Return (plaintext, ok). ok=False means the secret must be re-entered."""
    try:
        return decrypt(stored), True
    except SecretUnreadable:
        return "", False
