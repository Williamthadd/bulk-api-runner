import os

import pytest
from sqlalchemy import text

from app.config import settings

TEST_URL = os.environ.get("TEST_DATABASE_URL", "").strip()
SKIP_MSG = ("MySQL test database not reachable{where}. Start it with "
            "`docker compose --profile test up -d db-test` (requires Docker) and set TEST_DATABASE_URL in .env.")
TABLES = ["run_items", "runs", "uploads", "environment_variables", "environments", "requests", "folders", "auth_profiles"]


def _reset_schema(url: str) -> None:
    from app.db import make_engine, migrate
    eng = make_engine(url)
    with eng.begin() as c:
        c.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        for t in TABLES + ["alembic_version"]:
            c.execute(text(f"DROP TABLE IF EXISTS `{t}`"))
        c.execute(text("SET FOREIGN_KEY_CHECKS=1"))
    eng.dispose()
    migrate(url)


@pytest.fixture(scope="session")
def mysql_url():
    if not TEST_URL:
        pytest.skip(SKIP_MSG.format(where=" (TEST_DATABASE_URL is not set)"))
    from app.db import make_engine, wait_for_db
    eng = make_engine(TEST_URL)
    try:
        wait_for_db(eng, timeout_s=3, interval_s=1)
    except ConnectionError:
        pytest.skip(SKIP_MSG.format(where=f" at {eng.url.host}:{eng.url.port}"))
    finally:
        eng.dispose()
    _reset_schema(TEST_URL)
    return TEST_URL


@pytest.fixture
def db(mysql_url, tmp_path, monkeypatch):
    """Clean tables, temp data dir + fresh encryption key, engine pointed at the test DB."""
    from app import crypto
    from app import db as appdb
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "database_url", mysql_url)
    (tmp_path / "data").mkdir()
    crypto.init()
    engine = appdb.init_engine(mysql_url)
    with engine.begin() as c:
        c.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        for t in TABLES:
            c.execute(text(f"TRUNCATE TABLE `{t}`"))
        c.execute(text("SET FOREIGN_KEY_CHECKS=1"))
    yield engine
    engine.dispose()
