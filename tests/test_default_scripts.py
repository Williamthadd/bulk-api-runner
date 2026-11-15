"""The seeded scripts against the sample files in samples/ (no network: ctx.call is faked)."""
from app.sandbox import run_script
from tests.helpers import EMPLOYEE_XLSX, SCHEDULE_XLSX, WAREHOUSE_XLSX, script

ROLE = "dc74712f-42a2-44a8-a2c7-361851610bc6"
IDS = {
    "william.thaddeus@moladin.com": "88032f68-0302-4c2a-8fcb-4c47898cbb6e",
    "bryan.lakaoni@moladin.ai": "11111111-1111-1111-1111-111111111111",
    "jason.adiwijaya@moladin.ai": "22222222-2222-2222-2222-222222222222",
}


def fake_employee_lookup(name, query, body, path):
    assert name == "Employee - find by email"
    assert query["filter_keys"] == "employee_email"
    email = query["value"]
    items = [{"id": IDS[email], "employee_email": email.upper()}] if email in IDS else []
    return {"code": 200, "msg": "ok", "data": {"items": items}}


def test_bulk_schedule_one_item_per_employee():
    r = run_script(script("attendance_bulk_schedule.py"), str(SCHEDULE_XLSX),
                   call_handler=fake_employee_lookup, timeout_s=30)
    assert r.ok, r.traceback
    assert r.rows_read == 87 and r.skipped == []
    assert [(i["label"], len(i["body"]["schedules"])) for i in r.items] == [
        ("william.thaddeus@moladin.com", 31), ("jason.adiwijaya@moladin.ai", 25), ("bryan.lakaoni@moladin.ai", 31)]
    body = r.items[0]["body"]
    assert list(body) == ["employee_id", "role_id", "schedules", "warehouse_id"]
    assert body["employee_id"] == "88032f68-0302-4c2a-8fcb-4c47898cbb6e"
    assert body["role_id"] == ROLE
    assert body["warehouse_id"] == "123n23jn123j123-12312cfwfc-c2434c"
    assert body["schedules"][0] == {"date": "2026-01-01", "scheduled_time_start": "2026-01-01 09:00:00",
                                    "scheduled_time_end": "2026-01-01 18:00:00", "status": "off"}
    dates = [s["date"] for s in body["schedules"]]
    assert dates == sorted(dates)


def test_bulk_schedule_unknown_employee_is_skipped():
    def handler(name, query, body, path):
        return {"code": 200, "data": {"items": []}}
    r = run_script(script("attendance_bulk_schedule.py"), str(SCHEDULE_XLSX), call_handler=handler, timeout_s=30)
    assert r.ok and r.items == []
    assert len(r.skipped) == 3 and "not found" in r.skipped[0]["reason"]


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
