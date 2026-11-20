"""The seeded scripts against the sample files in samples/ (no network: ctx.call is faked)."""
from app.sandbox import run_script
from tests.helpers import EMPLOYEE_XLSX, SCHEDULE_XLSX, WAREHOUSE_XLSX, script

ROLE = "dc74712f-42a2-44a8-a2c7-361851610bc6"


def test_bulk_schedule_is_one_item_holding_an_array_of_groups():
    r = run_script(script("attendance_bulk_schedule.py"), str(SCHEDULE_XLSX), timeout_s=30)   # no ctx.call needed
    assert r.ok, r.traceback
    assert r.rows_read == 87 and r.skipped == []
    assert len(r.items) == 1 and r.items[0]["label"] == "3 employee schedule group(s)"
    body = r.items[0]["body"]
    assert isinstance(body, list)
    # one element per (employee, warehouse), in the order they appear in the file: 31 / 25 / 31 shifts
    assert [(g["email"], g["warehouse_name"], len(g["schedules"])) for g in body] == [
        ("william.thaddeus@moladin.com", "Sample Warehouse 1", 31),
        ("jason.adiwijaya@moladin.ai", "Sample Warehouse 2", 25),
        ("bryan.lakaoni@moladin.ai", "Sample Warehouse 3", 31)]
    assert list(body[0]) == ["email", "schedules", "warehouse_name"]       # the backend sets role_id itself
    assert body[0]["schedules"][0] == {"date": "2026-01-01", "scheduled_time_end": "2026-01-01 18:00:00",
                                       "scheduled_time_start": "2026-01-01 09:00:00", "status": "off"}
    assert list(body[0]["schedules"][0]) == ["date", "scheduled_time_end", "scheduled_time_start", "status"]
    dates = [s["date"] for s in body[0]["schedules"]]
    assert dates == sorted(dates)


def _bulk(rows):
    """Run the bulk-schedule script on in-memory rows (every row gets a `_row` number like the real loader)."""
    import tempfile, openpyxl, os
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["email", "warehouse", "date", "start_time", "end_time"])
    for r in rows: ws.append(r)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "s.xlsx"); wb.save(p)
        return run_script(script("attendance_bulk_schedule.py"), p, timeout_s=30)


def test_bulk_schedule_one_bad_row_skips_that_employee_only():
    r = _bulk([["a@x.com", " Main  WH ", "2026-02-01", "09:00", "18:00"],
               ["b@x.com", "Main WH", "2026-02-01", "18:00", "09:00"],          # end before start
               ["b@x.com", "Main WH", "2026-02-02", "09:00", "18:00"],
               ["", "Main WH", "2026-02-03", "09:00", "18:00"],                # no email
               ["a@x.com", "main wh", "2026-02-01", "10:00", "19:00"]])        # second shift on the same date
    assert r.ok, r.traceback
    assert r.items == []                                                       # a@x has a duplicate date, b@x a bad row
    reasons = {s["label"]: s["reason"] for s in r.skipped}
    assert "duplicate date 2026-02-01" in reasons["a@x.com / Main WH"]
    assert "end_time must be after start_time" in reasons["b@x.com / Main WH"]
    assert reasons["row 5"] == "email and warehouse are required"
    r = _bulk([["a@x.com", " Main  WH ", "2026-02-01", "09:00", "18:00"],
               ["b@x.com", "Main WH", "2026-02-01", "18:00", "09:00"]])
    assert [g["warehouse_name"] for g in r.items[0]["body"]] == ["Main WH"]    # whitespace cleaned; b@x skipped
    assert len(r.skipped) == 1


def test_warehouse_sample_row_skipped_for_latitude():
    r = run_script(script("warehouse_create.py"), str(WAREHOUSE_XLSX), timeout_s=30)
    assert r.ok and r.items == []
    assert r.skipped == [{"label": "row 2", "reason": "latitude -12839392.0 must be between -90 and 90"}]


def test_employee_sample_phone_and_nik():
    r = run_script(script("employee_create.py"), str(EMPLOYEE_XLSX), timeout_s=30)
    assert r.ok and r.skipped == []
    assert len(r.items) == 1
    body = r.items[0]["body"]
    assert body["phone_number"] == "08174949338"
    assert body["nik"] == "3673829287458" and isinstance(body["nik"], str)
    assert body["employee_primary_role_id"] == ROLE
    assert body["employee_warehouse_role_id"] == {"role_id": ROLE, "warehouse_id": "3123-13cfwexr-423423-4234c235245c234"}
    assert body["status"] == "active"
    assert r.warnings and "13 digits" in r.warnings[0]["msg"]


def test_warehouse_timezone_and_old_headers():
    import os, tempfile, openpyxl
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["warehouse name", "address", "latitude", "longitude", "attendance radius (meter)", "timezone"])
    ws.append(["Belitung Warehouse", "Jl. Example No. 1, Jakarta", -6.2088, 106.8456, 150, " wib "])
    ws.append(["No Zone", "Jl. B", -6.2, 106.8, 150, None])
    ws.append(["Bad Zone", "Jl. C", -6.2, 106.8, 150, "GMT"])
    ws.append(["Half Radius", "Jl. D", -6.2, 106.8, 10.5, "WIT"])
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "w.xlsx"); wb.save(p)
        r = run_script(script("warehouse_create.py"), p, timeout_s=30)
    assert r.ok, r.traceback
    assert r.items == [{"label": "Belitung Warehouse", "body": {
        "latitude": -6.2088, "longitude": 106.8456, "radius_m": 150, "timezone": "WIB",
        "warehouse_address": "Jl. Example No. 1, Jakarta", "warehouse_name": "Belitung Warehouse"}}]
    assert [s["reason"] for s in r.skipped] == [
        "timezone is required (WIB, WITA or WIT)", "timezone 'GMT' must be one of WIB, WITA, WIT",
        "attendance_radius_meter 10.5 must be a whole number"]
