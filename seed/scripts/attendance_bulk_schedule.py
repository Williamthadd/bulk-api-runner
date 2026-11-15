from collections import OrderedDict
from datetime import datetime, date, time

ROLE_ID = "dc74712f-42a2-44a8-a2c7-361851610bc6"

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

def find_employee_id(ctx, email):
    res = ctx.call("Employee - find by email", query={"filter_keys": "employee_email", "value": email})
    for it in ((res.get("data") or {}).get("items") or []):
        if (it.get("employee_email") or "").strip().lower() == email:
            return it["id"]
    raise LookupError(f"employee {email} not found")

def transform(rows, ctx):
    groups = OrderedDict()  # (email, warehouse_id) -> {"errors": [], "days": {}}
    for r in rows:
        n = r["_row"]
        email = str(r.get("email") or "").strip().lower()
        wh = str(r.get("warehouse_uuid") or "").strip()
        if not email or not wh:
            ctx.skip(f"row {n}", "email and warehouse_uuid are required"); continue
        g = groups.setdefault((email, wh), {"errors": [], "days": {}})
        try:
            d, st, et = to_date(r.get("date")), to_time(r.get("start_time")), to_time(r.get("end_time"))
            if et <= st: raise ValueError("end_time must be after start_time")
            if d in g["days"]: raise ValueError(f"duplicate date {d}")
            g["days"][d] = {"date": d.isoformat(),
                            "scheduled_time_start": f"{d} {st:%H:%M}:00",
                            "scheduled_time_end": f"{d} {et:%H:%M}:00",
                            "status": "off"}
        except (ValueError, TypeError) as e:
            g["errors"].append(f"row {n}: {e}")
    items = []
    for (email, wh), g in groups.items():
        try:
            if g["errors"]: raise ValueError("; ".join(g["errors"]))
            employee_id = find_employee_id(ctx, email)
        except (ValueError, LookupError) as e:
            ctx.skip(email, str(e)); continue
        items.append({"label": email, "body": {
            "employee_id": employee_id, "role_id": ROLE_ID,
            "schedules": [g["days"][k] for k in sorted(g["days"])],
            "warehouse_id": wh}})
    return items
