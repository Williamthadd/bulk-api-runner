ROLE_ID = "dc74712f-42a2-44a8-a2c7-361851610bc6"

def text(v):
    if v is None: return ""
    if isinstance(v, float) and v.is_integer(): v = int(v)
    return str(v).strip()

def phone(v):
    s = text(v).replace(" ", "").replace("-", "")
    if s.startswith("+62"): s = "0" + s[3:]
    elif s.startswith("62"): s = "0" + s[2:]
    elif s and not s.startswith("0"): s = "0" + s   # Excel drops the leading zero of numeric cells
    return s

def transform(rows, ctx):
    items, seen = [], set()
    for r in rows:
        n = r["_row"]
        try:
            name, email = text(r.get("employee name")), text(r.get("email"))
            ph, nik = phone(r.get("phone number")), text(r.get("nik"))
            wh, mdi = text(r.get("warehouse_uuid")), text(r.get("moladin employee number"))
            if not name or len(name) > 100: raise ValueError("employee name is required (max 100)")
            if "@" not in email or len(email) > 100: raise ValueError("valid email is required (max 100)")
            if not ph or len(ph) > 14: raise ValueError("phone number is required (max 14 chars)")
            if not wh: raise ValueError("warehouse_uuid is required")
            if not mdi or len(mdi) > 50: raise ValueError("moladin employee number is required (max 50)")
            if email.lower() in seen: raise ValueError(f"duplicate email {email} in file")
            seen.add(email.lower())
        except ValueError as e:
            ctx.skip(f"row {n}", str(e)); continue
        if nik and len(nik) != 16:
            ctx.warn(email, f"nik has {len(nik)} digits (expected 16); check that the cell is formatted as Text")
        items.append({"label": email, "body": {
            "employee_email": email, "employee_name": name,
            "employee_primary_role_id": ROLE_ID,
            "employee_warehouse_role_id": {"role_id": ROLE_ID, "warehouse_id": wh},
            "moladin_employee_number": mdi, "nik": nik, "phone_number": ph, "status": "active"}})
    return items
