VALID_TIMEZONES = {"WIB", "WITA", "WIT"}
MAX_RADIUS_M = 100000


def num(v, name):
    if v is None or str(v).strip() == "": raise ValueError(f"{name} is required")
    try:
        return float(str(v).replace(",", ".")) if isinstance(v, str) else float(v)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {v!r}")


def pick(r, *keys):
    """First non-empty value among the given column names (supports old and new headers)."""
    for k in keys:
        if r.get(k) is not None and str(r.get(k)).strip() != "":
            return r.get(k)
    return None


def transform(rows, ctx):
    items = []
    for r in rows:
        n = r["_row"]
        try:
            name = str(pick(r, "warehouse_name", "warehouse name") or "").strip()
            addr = str(pick(r, "address") or "").strip()
            if not name or len(name) > 100: raise ValueError("warehouse_name is required (max 100 chars)")
            if not addr or len(addr) > 225: raise ValueError("address is required (max 225 chars)")
            lat, lng = num(r.get("latitude"), "latitude"), num(r.get("longitude"), "longitude")
            if not -90 <= lat <= 90: raise ValueError(f"latitude {lat} must be between -90 and 90")
            if not -180 <= lng <= 180: raise ValueError(f"longitude {lng} must be between -180 and 180")
            radius_f = num(pick(r, "attendance_radius_meter", "attendance radius (meter)"), "attendance_radius_meter")
            if radius_f != int(radius_f): raise ValueError(f"attendance_radius_meter {radius_f} must be a whole number")
            radius = int(radius_f)
            if not 1 <= radius <= MAX_RADIUS_M: raise ValueError(f"attendance_radius_meter must be 1..{MAX_RADIUS_M}")
            tz = str(pick(r, "timezone") or "").strip().upper()
            if not tz: raise ValueError("timezone is required (WIB, WITA or WIT)")
            if tz not in VALID_TIMEZONES:
                raise ValueError(f"timezone {tz!r} must be one of WIB, WITA, WIT")
        except ValueError as e:
            ctx.skip(f"row {n}", str(e)); continue
        items.append({"label": name, "body": {"latitude": lat, "longitude": lng, "radius_m": radius,
                                              "timezone": tz,
                                              "warehouse_address": addr, "warehouse_name": name}})
    return items
