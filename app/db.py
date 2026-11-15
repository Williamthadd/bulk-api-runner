"""SQLAlchemy engine/session handling."""
from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError, InterfaceError  # noqa: F401 (re-exported)
from sqlalchemy.orm import Session, sessionmaker

from .config import settings, ROOT

_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None

DB_ERRORS = (OperationalError, InterfaceError)


def make_engine(url: str) -> Engine:
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args={"connect_timeout": 5, "read_timeout": 30, "write_timeout": 30},
        future=True,
    )


def init_engine(url: str | None = None) -> Engine:
    global _engine, _SessionLocal
    url = url or settings.database_url
    if not url:
        raise RuntimeError("DATABASE_URL is not set. Copy .env.example to .env and fill it in.")
    if _engine is not None:
        _engine.dispose()
    _engine = make_engine(url)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        init_engine()
    return _engine  # type: ignore[return-value]


@contextmanager
def session_scope() -> Iterator[Session]:
    if _SessionLocal is None:
        init_engine()
    s: Session = _SessionLocal()  # type: ignore[misc]
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def ping(engine: Engine | None = None) -> bool:
    try:
        with (engine or get_engine()).connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def wait_for_db(engine: Engine, timeout_s: float = 60, interval_s: float = 2) -> None:
    """Retry until the DB answers or the deadline passes (then raise the last error)."""
    deadline = time.monotonic() + timeout_s
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with engine.connect() as c:
                c.execute(text("SELECT 1"))
            return
        except Exception as e:  # noqa: BLE001
            last = e
            text_e = str(e)
            # wrong credentials / database will not fix themselves by waiting
            if "Access denied" in text_e or "Unknown database" in text_e:
                raise ConnectionError(text_e) from e
            time.sleep(interval_s)
    raise ConnectionError(str(last))


def alembic_config(url: str | None = None):
    from alembic.config import Config
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["database_url"] = url or settings.database_url
    return cfg


def migrate(url: str | None = None, revision: str = "head") -> None:
    from alembic import command
    command.upgrade(alembic_config(url), revision)


def downgrade(url: str | None = None, revision: str = "base") -> None:
    from alembic import command
    command.downgrade(alembic_config(url), revision)
