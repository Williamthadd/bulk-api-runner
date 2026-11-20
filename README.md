# Bulk API Runner

A small, local, Postman-like web app for sending **many REST requests from an Excel file**.

You upload an `.xlsx`. A **Python script that you can see and edit in the browser** turns the rows into JSON
bodies. You inspect the result with a **dry run**, which sends nothing, and then the bodies are sent
**one by one** while a monitor panel shows live progress.

Business logic is not hard-coded per endpoint. Each request stores its own script.

- REST/JSON only: `GET`, `POST`, `PUT`, `PATCH`, `DELETE`. Raw JSON bodies only.
- Auth: **Bearer Token**. Either you paste a token, or an **auth profile** (OAuth2 client credentials) fetches and
  refreshes it for you.
- Runs on your machine at `http://127.0.0.1:8765`. The data lives in MySQL 8.4 in Docker.

> **Not a security sandbox.** Scripts run as a normal Python process with *your* user's privileges
> (in a separate subprocess with a timeout). Only run scripts you trust. The app is meant to be used
> locally by trusted people. It listens on `127.0.0.1` only, and so does the MySQL port.

---

## Quick start

Requirements: **Python 3.11+** and **Docker** (Docker Desktop on Mac/Windows).

```bash
cp .env.example .env            # Windows (PowerShell): Copy-Item .env.example .env
#   -> fill in MYSQL_ROOT_PASSWORD, MYSQL_USER, MYSQL_PASSWORD and DATABASE_URL (same user/password)
docker compose up -d db         # MySQL 8.4 on 127.0.0.1:3307 (waits ~15 s to become healthy)
python run.py                   # migrates, seeds on first start, serves http://127.0.0.1:8765
```

Install the Python dependencies once, ideally in a virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

`run.py` waits for MySQL, runs `alembic upgrade head`, loads the seed collection if the database has no
folders yet, and starts the server. If MySQL is not reachable it exits with a message such as
`Cannot reach MySQL at 127.0.0.1:3307, run docker compose up -d db`.

On the first start, `data/secret.key` is generated. **Keep it** (see [Secrets](#secrets-and-the-encryption-key)).

---

## Workflow

1. **Pick a request** in the sidebar (collection → folder → request; right-click or `⋯` for
   create / rename / duplicate / delete / export; drag to move).
2. **Data tab:** upload an `.xlsx` (max 10 MB) or click *Use sample*. Choose the sheet (default: the first)
   and the header row (default: 1). A preview shows the first 50 rows and the detected type of each column.
3. **Script tab:** edit `transform(rows, ctx)` (line numbers, Python highlighting).
   `Ctrl/Cmd+S` saves, `Ctrl/Cmd+Enter` runs the dry run.
4. **Run (dry run):** the script runs and nothing is sent.
   **Output tab** shows rows read, items ready, skipped, warnings, the item list
   (skipped rows in red) and the JSON of the selected item (collapsible, searchable, *Copy as cURL* with the
   token masked). If the script fails you get the traceback, and the offending line is highlighted in the editor.
5. **Send** / **Send first item only:** a confirmation shows the item count, URL and environment.
   Send is only enabled if the **latest dry run succeeded for the same file + script + sheet/header row +
   environment**. Change any of them and you must dry-run again. If the script skipped items, you must tick
   **"send valid items only"**.
6. **Monitor** (bottom panel, resizable and collapsible) shows progress live.

Requests without a data source can be dry-run without a file; the script then
gets `rows = []`.

## Script contract

```python
def transform(rows, ctx):
    return [{"label": "shown in the monitor", "body": {...}}]   # optional keys: "query": {...}, "headers": {...}
```

| | |
|---|---|
| `rows` | list of dicts. Keys are the header texts (stripped); values keep their native openpyxl types (`datetime`, `time`, `float`, `int`, `str`, `None`). `_row` is the Excel row number. Completely empty rows are skipped. Read with `data_only=True`. |
| `ctx.log(msg)` | line in the script log (also `print(...)`). |
| `ctx.skip(label, reason)` | marks a row/item invalid: shown in red, never sent; the script continues. |
| `ctx.warn(label, msg)` | warning attached to an item; the item is still sent. |
| `ctx.vars` | read-only dict of the active environment's **non-secret** variables. |
| `ctx.call(request_name, query=None, body=None, path=None)` | calls another request of the collection through the app's HTTP engine and auth, and returns the parsed JSON. Raises `CallError` if the response counts as a failure. **During a dry run only GET/HEAD requests are allowed**; anything else raises `DryRunBlocked`. |

Any uncaught exception means the script failed and nothing is sent.

Execution details: a separate subprocess per run (`python -I`). The xlsx is loaded **inside** that
subprocess so date/time types survive. The default timeout is 60 s (`SCRIPT_TIMEOUT_SECONDS`, or per request in
Settings). `ctx.call` is a JSON-lines RPC over stdin/stdout to the main process. A memory limit
(`SCRIPT_MEMORY_LIMIT_MB`) is applied with `setrlimit` where the OS enforces it: **Linux yes, macOS and
Windows no** (there the timeout is the only limit).

## Auth, environments and the 401 flow

- The Auth tab of a request has three options: **Inherit from parent** (default), **Auth profile (automatic token)**
  and **Bearer Token**. Folders have the same choice. A request uses the nearest folder's auth unless it sets its own.
- Normally the token lives in the environment variable `bearer_token` (secret) and the collection folder uses
  `{{bearer_token}}`. Replacing that one variable updates every request. The Auth tab and the monitor
  banner both have an inline field to paste a new value.
- The app sends `Authorization: Bearer <token>`, plus `Content-Type: application/json` when the request has a JSON
  body. It adds no `Accept` header. A GET therefore looks like
  `curl --request GET --url ... --header 'Authorization: Bearer ...'`. A manual `Authorization` header in the Headers tab
  is rejected, and one coming from a script item is ignored.
- Paste **only the token**. A leading `Bearer `, `Authorization:`, quotes and line breaks are removed automatically.
  If a JWT is not complete (for example cut off while copying: not 3 dot-separated parts), sending is blocked with
  a "token looks incomplete" message. The Auth tab shows the current token's length and expiry (read from the JWT,
  not verified), and Send warns if it has already expired.
- If the token is empty, Send is blocked with a warning.
- **401 while sending:** the run becomes **Paused** at that item and shows
  *"Token expired or invalid, update the token and click Resume"*. Paste the new token and click
  **Update token & Resume** (or Resume). The same item is sent again; items that already succeeded are not repeated.
  You can turn this off per request in Settings (*Pause on 401*), in which case 401 is an ordinary failure.
- `{{var}}` works in the URL, headers, params and the token field. Use the top-bar selector to switch environments
  and **Manage** to edit them. Secret values are masked and never sent back to the browser.

### Auth profiles (automatic tokens)

An auth profile stores how to get a token from an OAuth2 token endpoint with the **client credentials** grant, so
you never paste a token:

1. Top bar → **Auth profiles** → fill in *Name* (e.g. `EAMS-Auth`), *Token URL* (the `.../protocol/openid-connect/token`
   endpoint), *client_id*, *grant_type* `client_credentials` and *client_secret* → **Save & test**. The test fetches a
   token and shows its length and expiry (never the token itself).
2. Open the collection folder (or a single request) → Auth → **Auth profile (automatic token)** → pick the profile →
   Save. Every request that inherits now shows *Auto token: EAMS-Auth* in its status line.

How it works:

- The app POSTs `grant_type`, `client_id` and `client_secret` (form-encoded) to the token URL and sends the returned
  `access_token` as `Authorization: Bearer <token>`.
- The token is kept **in memory only** (never in MySQL, logs or `run_items`) and reused until shortly before it
  expires (`expires_in` from the response, or the JWT `exp`). A restart simply fetches a new one.
- If the API answers **401**, the app throws the cached token away, fetches a new one and sends the same item again,
  once. Only if the fresh token is also rejected does the run pause (*"…even with a freshly fetched token…"*); fix the
  profile and click Resume. If the token endpoint itself fails (wrong secret, unreachable), the run pauses with the
  endpoint's error message.
- The send confirmation (and Copy as cURL) fetch a token first, so wrong credentials show up before anything is sent.
- The client secret is encrypted like other secrets, never shown again (leave the field empty to keep it) and never
  exported. Exports only contain the profile **name**; on import it is linked to the profile with the same name, or
  falls back to *Inherit* if there is none.
- `{{variables}}` from the active environment work in the token URL, client_id, client_secret and scope.
- A profile that is still used by a folder or request cannot be deleted.

## Runner behaviour (per request, Settings tab)

- Requests are sent sequentially. *Delay between requests* defaults to 0 ms, *timeout per request* to 30 s,
  *stop on first error* to off and *pause on 401* to on.
- **Success** means HTTP 2xx and, if the JSON body has a numeric `code` field, a value from the accepted list
  (default `200`). The backend uses `{code, msg, data}` and some errors arrive as HTTP 200 with another `code`;
  those count as failures and the message says why (`code 409 != 200: ...`).
- **Retry:** up to *N* retries with exponential backoff on the listed statuses. The seeded *bulk schedule*
  request uses 3 retries on 503 (it is an upsert). The *create* requests have no automatic retry because
  they are not idempotent. 400/401/403/404/409 are never retried automatically.
- Controls: **Pause / Resume / Stop**. When a run finishes, **Retry failed only** starts a new run with just the
  failed items, after a warning if the request is not marked idempotent. This needs the run's items in
  memory, so it is only available for runs started since the app was last restarted.

## Monitor and history

The monitor header shows the request, status (Running/Paused/Done/Stopped/Failed), `n/total`, success/failure
counts and elapsed time. Each log row shows time, item number, label, method + URL, HTTP status, duration and
the response `msg` (or a body snippet). Click a row to see the request body and the response. The
Authorization header is never stored and secrets are masked.

**202 Accepted** (bulk schedule) is labelled *accepted* and shows `upload_id`, `schedule_count` and
`batch_count`. 202 only means the request was queued; the actual write is done asynchronously by a worker.

Filters: All / Failed. You can also Clear the panel or Export it as CSV. Runs and log rows are stored in MySQL
(`runs`, `run_items`) and survive restarts; reopen them from the **Run history** dropdown.

**Retention:** a run is kept only while it is among the last 20 runs of its request **and** younger than
30 days (`RETENTION_MAX_RUNS`, `RETENTION_DAYS`). Cleanup runs at the end of each run. Request/response bodies
are masked and truncated to 64 KB before they are stored. Uploaded files are kept in `data/uploads/` (only their
metadata is in MySQL) and deleted after 24 hours.

## Collections: export and import

Folder menu → **Export as JSON**, and the sidebar ⤓ button → import. Exports contain scripts, settings and
variable references such as `{{bearer_token}}` and auth profile names, but **never secrets**: literal tokens are
dropped, and environments and auth profiles are not exported.

## Seed collection

On first start (empty `folders` table) the app loads `seed/collection.json` with the scripts in `seed/scripts/`:

| Request | Method / URL | Source |
|---|---|---|
| Attendance - bulk schedule | `POST {{base_url}}/api/v1/attendance/schedule/bulk` | `samples/EAMS_Schedule_template_2.xlsx` (columns `email`, `warehouse`, `date`, `start_time`, `end_time`). **One request** whose body is an array with one element per (employee, warehouse): `{email, warehouse_name, schedules: [...]}`. The backend resolves the employee and warehouse from the email and warehouse name, so no lookups or UUIDs are needed. A bad row skips that whole employee/warehouse group. |
| Warehouse - create | `POST {{base_url}}/api/v1/warehouse/create` | `samples/Eams_New_Warehouse_Registration_Template.xlsx`. Needs a `timezone` column (`WIB`, `WITA` or `WIT`); the sample file has none, so its row is skipped. |
| Employee - create | `POST {{base_url}}/api/v1/employee/create` | `samples/EAMS_NEW_EMPLOYEE_TEMPLATE.xlsx` |

The environments **Development** (`base_url = https://eams.development.mofi.id`) and **Production** (`base_url` empty)
both have an empty secret `bearer_token`. Paste a token before sending, or set up an auth profile (no auth profile is
seeded: the client secret is yours to enter).

## Secrets and the encryption key

Bearer tokens on folders/requests, auth profile client secrets and every environment variable flagged *secret* are **encrypted in the
application (Fernet) before they are written to MySQL**. The key is in `data/secret.key`. It is created on the
first run (permission `0600` where supported) and is **not** in the database and not in exports.

- Back up `data/secret.key` together with your database dumps.
- **If the key is lost or replaced, the stored secrets cannot be decrypted.** The UI then marks those fields
  "cannot decrypt: re-enter" and sending is blocked until you enter them again. Nothing else is lost.

Secrets are masked in the UI and are never written to logs or `run_items`.

## Backup and restore

```bash
# backup (run from this folder; the DB container must be running)
docker compose exec -T db sh -c 'exec mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --single-transaction --routines --triggers --databases "$MYSQL_DATABASE"' > backup.sql

# restore (recreates the tables from the dump)
docker compose exec -T db sh -c 'exec mysql -uroot -p"$MYSQL_ROOT_PASSWORD"' < backup.sql
```

Keep `data/secret.key` with the dump.

> ⚠️ `docker compose down -v` **deletes the `bulk_runner_mysql_data` volume and all data** (collections, history).
> Use `docker compose down` (without `-v`) or `docker compose stop` to just stop MySQL.

The container keeps the credentials it was first created with. If you change `MYSQL_*` in `.env` later, either
change them inside MySQL too or start from a fresh volume.

## Configuration (`.env`)

| Variable | Default | |
|---|---|---|
| `MYSQL_ROOT_PASSWORD`, `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_DATABASE` | (none) / `bulk_runner` | used by `docker compose` to create the DB and a dedicated app user |
| `MYSQL_PORT` | `3307` | published on `127.0.0.1` only |
| `DATABASE_URL` | (none) | `mysql+pymysql://user:pass@127.0.0.1:3307/bulk_runner?charset=utf8mb4` |
| `APP_PORT` | `8765` | the app always binds `127.0.0.1` |
| `SCRIPT_TIMEOUT_SECONDS` | `60` | per request override in Settings |
| `SCRIPT_MEMORY_LIMIT_MB` | `1024` | enforced on Linux only |
| `UPLOAD_MAX_MB` / `UPLOAD_TTL_HOURS` | `10` / `24` | |
| `RETENTION_MAX_RUNS` / `RETENTION_DAYS` | `20` / `30` | |
| `MYSQL_TEST_PORT`, `MYSQL_TEST_DATABASE`, `TEST_DATABASE_URL` | `3308`, `bulk_runner_test` | for pytest |
| `DB_WAIT_SECONDS` | `45` | how long `run.py` waits for MySQL |

## Rehearsing without a real server

`devtools/fake_eams.py` is a local fake of the EAMS endpoints (stdlib only):

```bash
python devtools/fake_eams.py --port 8899 --expire-after 3 --fail-503 1
```

Create an environment with `base_url = http://127.0.0.1:8899` and any `bearer_token`. `--expire-after N` makes the first
token return 401 after N calls (to try the pause/resume flow). `--fail-503 K` makes the first K bulk calls return
503 (to see the retry). Emails containing `dup` are rejected with `code 409`.

It also has a token endpoint for auth profiles: Token URL `http://127.0.0.1:8899/token`, any client_id, client_secret
`local-secret` (change with `--client-secret`, token lifetime with `--token-ttl`). With `--expire-after 3` the first
fetched token stops working after 3 calls and you can watch the app fetch a new one by itself.

## Tests

```bash
docker compose --profile test up -d db-test     # throw-away MySQL on 127.0.0.1:3308 (tmpfs, no volume)
pytest
```

The DB tests need a **real MySQL** (JSON, collation and foreign-key behaviour differ from SQLite). If
`db-test` is not reachable they are **skipped** with a message telling you how to start it. No test calls a real
API: HTTP tests use a fake local server.

| File | Covers |
|---|---|
| `test_xlsx_loader.py` | date/time/number types, empty rows skipped, sheet + header row, duplicate/blank headers, type detection |
| `test_sandbox.py` | items/logs/skip/warn, line numbers in tracebacks, syntax errors, timeout kill, hard exit, read-only `ctx.vars`, `ctx.call` RPC, `DryRunBlocked` |
| `test_default_scripts.py` | the three seed scripts against `samples/`: one bulk-schedule item with an array body of 31/25/31 shifts per employee in file order, bad-row skipping, warehouse latitude skip and timezone rules, employee phone `08174949338` and `nik` as a string |
| `test_auth.py` | folder vs request token, manual Authorization rejected, empty token blocks, cURL masking, success criteria |
| `test_runner.py` | success, `code != 200` = failure, 503 retry + backoff, 4xx never retried, stop on error, 401 pause → Resume re-sends only that item with the new token, 401 as failure when disabled, pause/stop, masking |
| `test_db.py` | Alembic upgrade/downgrade from empty, seed only once, encryption at rest + lost key, export without secrets, cascade folder → request, retention, masking + 64 KB truncation in `run_items`, DB unavailable (503 + health), runner survives a DB outage, upload expiry |
| `test_auth_profiles.py` | token endpoint form + caching + refresh before expiry, readable/masked errors, run refreshes once after 401 then pauses, pause when the token cannot be fetched, profile CRUD never returns the secret, folder profile used for dry run/cURL/send, delete blocked while in use, export/import by name |
| `test_api.py` | end-to-end through the HTTP API: upload → dry run → send → 202 accepted data → history after restart → CSV; send guards (empty token, changed script/file, skipped items); `DryRunBlocked`; `ctx.call` through the engine; request token override; retry failed only; tree CRUD and move; upload limits |

## Project layout

```
run.py                  start-up: wait for DB, migrate, seed, serve on 127.0.0.1
app/config.py           settings from .env
app/db.py, models.py    SQLAlchemy 2.x (pool_pre_ping), Alembic helpers
app/crypto.py           Fernet encryption of secrets, key in data/secret.key
app/xlsx_loader.py      openpyxl loader + preview/type detection
app/sandbox/            host.py (subprocess, timeout, RPC) and worker.py (runs the script)
app/http_engine.py      {{vars}}, auth resolution, request preparation, success rules, cURL
app/oauth.py            auth profiles: client-credentials token fetch + in-memory cache
app/runner.py           sequential runner: pause/resume/stop, retry, pause on 401, SSE events
app/services.py         collection/env CRUD, export/import, seed, uploads, retention, DB run backend
app/main.py             FastAPI routes + SSE + static UI
migrations/             Alembic environment and versions
static/                 UI (plain ES modules; CodeMirror 5 vendored in static/vendor/)
seed/                   collection.json + default scripts
samples/                example xlsx files
devtools/fake_eams.py   local fake API for rehearsals
tests/                  pytest suite
```
