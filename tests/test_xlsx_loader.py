from datetime import date, datetime, time

import openpyxl
import pytest

from app.xlsx_loader import XlsxError, detect_types, list_sheets, load_rows, preview
from tests.helpers import EMPLOYEE_XLSX, SCHEDULE_XLSX, WAREHOUSE_XLSX


@pytest.fixture
def book(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "first"
    ws.append(["  name ", "when", "start", "qty", "price", None, "name"])
    ws.append(["a", datetime(2026, 1, 2), time(9, 30), 3, 1.5, None, "dup"])
    ws.append([None, None, None, None, None, None, None])          # fully empty -> skipped
    ws.append(["   ", None, None, None, None, None, None])         # whitespace only -> skipped
    ws.append(["b", date(2026, 1, 3), time(18, 0), 4.0, None, "x", None])
    other = wb.create_sheet("second")
    other.append(["title row"])
    other.append(["k", "v"])
    other.append([1, "one"])
    p = tmp_path / "t.xlsx"
    wb.save(p)
    return p


def test_types_kept_and_empty_rows_skipped(book):
    cols, rows = load_rows(str(book))
    assert cols == ["name", "when", "start", "qty", "price", "column_F", "name_2"]
    assert [r["_row"] for r in rows] == [2, 5]
    r = rows[0]
    assert r["name"] == "a"
    assert isinstance(r["when"], datetime) and r["when"] == datetime(2026, 1, 2)
    assert isinstance(r["start"], time) and r["start"] == time(9, 30)
    assert r["qty"] == 3 and isinstance(r["price"], float)
    assert r["column_F"] is None and r["name_2"] == "dup"
    assert rows[1]["start"] == time(18, 0)


def test_sheet_and_header_row(book):
    assert list_sheets(str(book)) == ["first", "second"]
    cols, rows = load_rows(str(book), "second", header_row=2)
    assert cols == ["k", "v"]
    assert rows == [{"k": 1, "v": "one", "_row": 3}]
    with pytest.raises(XlsxError):
        load_rows(str(book), "nope")
    with pytest.raises(XlsxError):
        load_rows(str(book), "second", header_row=50)


def test_preview_detects_types(book):
    p = preview(str(book))
    assert p["types"]["when"] == "date"
    assert p["types"]["start"] == "time"
    assert p["types"]["qty"] == "integer"
    assert p["types"]["name"] == "text"
    assert p["rows"][0]["when"] == "2026-01-02" and p["rows"][0]["start"] == "09:30:00"
    assert p["total_rows"] == 2


def test_samples_load():
    cols, rows = load_rows(str(SCHEDULE_XLSX))
    assert cols == ["email", "warehouse_uuid", "date", "start_time", "end_time"]
    assert len(rows) == 87
    assert isinstance(rows[0]["date"], datetime) and isinstance(rows[0]["start_time"], time)
    _, rows = load_rows(str(WAREHOUSE_XLSX))
    assert len(rows) == 1 and rows[0]["latitude"] == -12839392.0
    _, rows = load_rows(str(EMPLOYEE_XLSX))
    assert len(rows) == 1 and rows[0]["phone number"] == 8174949338.0
    assert detect_types(["a"], [{"a": 1}, {"a": 1.5}]) == {"a": "number"}


def test_preview_header_row_beyond_end_falls_back_to_1(book):
    with pytest.raises(XlsxError, match="Header row 50 is beyond the end of the sheet .the last row with data is row 3"):
        load_rows(str(book), "second", header_row=50)
    p = preview(str(book), "second", header_row=50)
    assert p["header_row"] == 1 and p["header_row_reset_from"] == 50 and p["columns"] == ["title row", "column_B"]
    assert preview(str(book), "second", header_row=2)["header_row_reset_from"] is None
    # formatted-but-empty rows below the data count as "beyond the end" too
    assert preview(str(EMPLOYEE_XLSX), None, 100)["header_row_reset_from"] == 100
