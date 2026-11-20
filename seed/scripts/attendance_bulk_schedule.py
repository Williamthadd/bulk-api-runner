from collections import OrderedDict
from datetime import datetime, date, time

# Set to True once the backend accepts several shifts on the same date for one employee.
ALLOW_MULTIPLE_SHIFTS_PER_DATE = False


def to_date(v):
    if isinstance(v, datetime): return v.date()
    if isinstance(v, date): return v
    return datetime.strptime(str(v).strip(), "%Y-%m-%d").date()


def to_time(v):
    if isinstance(v, datetime): v = v.time()
    if not isinstance(v, time):
        h, m = str(v).strip().split(":")[:2]
        v = time(int(h), int(m))
    secs = round((v.hour*3600 + v.minute*60 + v.second + v.microsecond/1e6) / 60) * 60
    if secs >= 86400: raise ValueError("time out of range")
    return time(secs // 3600, secs % 3600 // 60)


def transform(rows, ctx):
    """Excel rows -> ONE request body: an array with one element per (employee, warehouse).

    Expected columns: email, warehouse, date, start_time, end_time.
    The backend sets role_id itself, and resolves employee and warehouse from
    email / warehouse_name, so no UUIDs are needed here.
    """
    # (email, warehouse) lower-cased -> {"email", "warehouse_name", "errors", "shifts"}
    groups = OrderedDict()
    for r in rows:
        n = r["_row"]
        email = str(r.get("email") or "").strip().lower()
        wh = " ".join(str(r.get("warehouse") or "").split())  # trim + collapse inner spaces
        if not email or not wh:
            ctx.skip(f"row {n}", "email and warehouse are required"); continue
        g = groups.setdefault((email, wh.lower()),
                              {"email": email, "warehouse_name": wh, "errors": [], "shifts": {}})
        try:
            if "@" not in email: raise ValueError(f"email is not valid: {email!r}")
            d, st, et = to_date(r.get("date")), to_time(r.get("start_time")), to_time(r.get("end_time"))
            if et <= st: raise ValueError("end_time must be after start_time")
            if (d, st, et) in g["shifts"]:
                raise ValueError(f"duplicate shift {d} {st:%H:%M}-{et:%H:%M}")
            if not ALLOW_MULTIPLE_SHIFTS_PER_DATE and any(k[0] == d for k in g["shifts"]):
                raise ValueError(f"duplicate date {d}")
            g["shifts"][(d, st, et)] = {
                "date": d.isoformat(),
                "scheduled_time_end": f"{d} {et:%H:%M}:00",
                "scheduled_time_start": f"{d} {st:%H:%M}:00",
                "status": "off",
            }
        except (ValueError, TypeError) as e:
            g["errors"].append(f"row {n}: {e}")

    body = []
    for (email, _), g in groups.items():
        if g["errors"]:
            ctx.skip(f"{g['email']} / {g['warehouse_name']}", "; ".join(g["errors"])); continue
        body.append({
            "email": g["email"],
            "schedules": [g["shifts"][k] for k in sorted(g["shifts"])],
            "warehouse_name": g["warehouse_name"],
        })

    if not body:
        return []
    # A single request carries every employee, so there is exactly one item.
    return [{"label": f"{len(body)} employee schedule group(s)", "body": body}]
