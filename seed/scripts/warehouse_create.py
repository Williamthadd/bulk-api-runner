def num(v, name):
    if v is None or str(v).strip() == "": raise ValueError(f"{name} is required")
    return float(str(v).replace(",", ".")) if isinstance(v, str) else float(v)

def transform(rows, ctx):
    items = []
    for r in rows:
        n = r["_row"]
        try:
            name = str(r.get("warehouse name") or "").strip()
            addr = str(r.get("address") or "").strip()
            if not name or len(name) > 100: raise ValueError("warehouse name is required (max 100 chars)")
            if not addr or len(addr) > 225: raise ValueError("address is required (max 225 chars)")
            lat, lng = num(r.get("latitude"), "latitude"), num(r.get("longitude"), "longitude")
            if not -90 <= lat <= 90: raise ValueError(f"latitude {lat} must be between -90 and 90")
            if not -180 <= lng <= 180: raise ValueError(f"longitude {lng} must be between -180 and 180")
            radius = int(num(r.get("attendance radius (meter)"), "radius"))
            if not 1 <= radius <= 100000: raise ValueError("radius must be 1..100000")
        except ValueError as e:
            ctx.skip(f"row {n}", str(e)); continue
        items.append({"label": name, "body": {"latitude": lat, "longitude": lng, "radius_m": radius,
                                              "warehouse_address": addr, "warehouse_name": name}})
    return items
