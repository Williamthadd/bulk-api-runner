"""Application settings, read from environment / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=False)


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: os.environ.get("DATABASE_URL", "").strip())
    host: str = "127.0.0.1"  # never configurable: local use only
    port: int = field(default_factory=lambda: _int("APP_PORT", 8765))
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("BULK_RUNNER_DATA_DIR") or ROOT / "data"))
    script_timeout_s: int = field(default_factory=lambda: _int("SCRIPT_TIMEOUT_SECONDS", 60))
    script_memory_mb: int = field(default_factory=lambda: _int("SCRIPT_MEMORY_LIMIT_MB", 1024))
    upload_max_mb: int = field(default_factory=lambda: _int("UPLOAD_MAX_MB", 10))
    upload_ttl_hours: int = field(default_factory=lambda: _int("UPLOAD_TTL_HOURS", 24))
    retention_max_runs: int = field(default_factory=lambda: _int("RETENTION_MAX_RUNS", 20))
    retention_days: int = field(default_factory=lambda: _int("RETENTION_DAYS", 30))

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def key_file(self) -> Path:
        return self.data_dir / "secret.key"

    def db_location(self) -> str:
        """host:port for error messages (no credentials)."""
        from sqlalchemy.engine import make_url
        try:
            u = make_url(self.database_url)
            return f"{u.host}:{u.port or 3306}"
        except Exception:
            return "(DATABASE_URL not set)"


settings = Settings()
