#!/usr/bin/env python3
"""Start Bulk API Runner: wait for MySQL, migrate, seed, serve on http://127.0.0.1:<APP_PORT>."""
import sys

if sys.version_info < (3, 11):
    sys.exit(f"Python 3.11+ is required (this is {sys.version.split()[0]}).")

import logging  # noqa: E402
import os  # noqa: E402


def main() -> None:
    from app.config import settings
    if not settings.database_url:
        sys.exit("DATABASE_URL is not set. Run `cp .env.example .env`, fill it in, then `docker compose up -d db`.")

    from app import crypto, db, services

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    new_key = not settings.key_file.exists()
    crypto.init()
    if new_key:
        print(f"Generated a new encryption key at {settings.key_file} (keep it; secrets cannot be read without it).")

    where = settings.db_location()
    engine = db.init_engine()
    wait = float(os.environ.get("DB_WAIT_SECONDS", "45"))
    print(f"Connecting to MySQL at {where} ...")
    try:
        db.wait_for_db(engine, timeout_s=wait)
    except ConnectionError as e:
        msg = str(e)
        if "Access denied" in msg:
            sys.exit(f"MySQL at {where} rejected the login. Check that DATABASE_URL matches MYSQL_USER / "
                     "MYSQL_PASSWORD in .env (the container keeps the credentials it was first created with).")
        if "Unknown database" in msg:
            sys.exit(f"The database in DATABASE_URL does not exist on {where}. Check MYSQL_DATABASE in .env.")
        sys.exit(f"Cannot reach MySQL at {where}, run `docker compose up -d db` (and wait until it is healthy).")

    print("Applying database migrations (alembic upgrade head) ...")
    db.migrate()
    with db.session_scope() as s:
        if services.seed_if_empty(s):
            print("Loaded the seed collection 'employee-attendance-management-system'.")

    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    print(f"\n  Bulk API Runner is running at http://{settings.host}:{settings.port}\n")
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, log_level="warning",
                timeout_graceful_shutdown=3)


if __name__ == "__main__":
    main()
