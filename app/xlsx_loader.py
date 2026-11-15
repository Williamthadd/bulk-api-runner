"""Read an xlsx sheet into a list of dicts, keeping native openpyxl value types."""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time, timedelta
from typing import Any

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter


class XlsxError(ValueError):
    pass


class HeaderRowBeyondEnd(XlsxError):
    def __init__(self, header_row: int, last_row: int) -> None:
        self.header_row, self.last_row = header_row, last_row
        super().__init__(f"Header row {header_row} is beyond the end of the sheet "
                         f"(the last row with data is row {last_row})")


def _is_empty(v: Any) -> bool:
    return v is None or (isinstance(v, str) and v.strip() == "")


def _open(path: str):
    try:
        return load_workbook(path, data_only=True, read_only=True)
    except Exception as e:  # noqa: BLE001
        raise XlsxError(f"Cannot open workbook: {e}") from e


def list_sheets(path: str) -> list[str]:
    wb = _open(path)
    try:
        return list(wb.sheetnames)
    finally:
        wb.close()


def _headers(raw: tuple) -> list[str | None]:
    names: list[str | None] = []
    seen: Counter[str] = Counter()
    for i, v in enumerate(raw):
        name = "" if v is None else str(v).strip()
        if not name:
            names.append(None)  # resolved later if the column holds data
            continue
        seen[name] += 1
        names.append(name if seen[name] == 1 else f"{name}_{seen[name]}")
    return names


def load_rows(path: str, sheet: str | None = None, header_row: int = 1) -> tuple[list[str], list[dict]]:
    """Return (columns, rows). Each row dict has the header texts as keys plus `_row` (Excel row number).

    - Header texts are stripped; duplicate headers get a `_2`, `_3` suffix.
    - Columns without a header are only kept if they contain data (named `column_<LETTER>`).
    - Completely empty rows are skipped.
    """
    if header_row < 1:
        raise XlsxError("header row must be >= 1")
    wb = _open(path)
    try:
        if sheet:
            if sheet not in wb.sheetnames:
                raise XlsxError(f"Sheet '{sheet}' not found. Available: {', '.join(wb.sheetnames)}")
            ws = wb[sheet]
        else:
            ws = wb.worksheets[0]
        header: tuple | None = None
        data: list[tuple[int, tuple]] = []
        last_row = 0   # last row that has any value (sheets often have formatted but empty rows below)
        for rnum, values in enumerate(ws.iter_rows(values_only=True), start=1):
            if not all(_is_empty(v) for v in values):
                last_row = rnum
            if rnum < header_row:
                continue
            if rnum == header_row:
                header = values
                continue
            if all(_is_empty(v) for v in values):
                continue
            data.append((rnum, values))
    finally:
        wb.close()
    if header is None or header_row > last_row:
        raise HeaderRowBeyondEnd(header_row, last_row)

    width = max([len(header)] + [len(v) for _, v in data]) if data else len(header)
    names = _headers(tuple(header) + (None,) * (width - len(header)))
    for i, n in enumerate(names):
        if n is None and any(i < len(v) and not _is_empty(v[i]) for _, v in data):
            names[i] = f"column_{get_column_letter(i + 1)}"
    columns = [n for n in names if n is not None]

    rows: list[dict] = []
    for rnum, values in data:
        row: dict[str, Any] = {}
        for i, n in enumerate(names):
            if n is not None:
                row[n] = values[i] if i < len(values) else None
        row["_row"] = rnum
        rows.append(row)
    return columns, rows


def type_name(v: Any) -> str | None:
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return None
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, datetime):
        return "date" if v.time() == time(0) else "datetime"
    if isinstance(v, date):
        return "date"
    if isinstance(v, time):
        return "time"
    if isinstance(v, timedelta):
        return "duration"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "integer" if v.is_integer() else "number"
    if isinstance(v, str):
        return "text"
    return type(v).__name__


def detect_types(columns: list[str], rows: list[dict]) -> dict[str, str]:
    out: dict[str, str] = {}
    for c in columns:
        cnt = Counter(t for t in (type_name(r.get(c)) for r in rows) if t)
        if not cnt:
            out[c] = "empty"
        elif len(cnt) == 1:
            out[c] = next(iter(cnt))
        elif set(cnt) <= {"integer", "number"}:
            out[c] = "number"
        elif set(cnt) <= {"date", "datetime"}:
            out[c] = "datetime"
        else:
            out[c] = "mixed (" + ", ".join(t for t, _ in cnt.most_common()) + ")"
    return out


def display_value(v: Any) -> Any:
    """JSON-safe rendering for the preview table."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d") if v.time() == time(0) else v.isoformat(sep=" ")
    if isinstance(v, (date, time)):
        return v.isoformat()
    if isinstance(v, timedelta):
        return str(v)
    if isinstance(v, float):
        return int(v) if v.is_integer() and abs(v) < 1e15 else v
    if isinstance(v, (int, str, bool)):
        return v
    return str(v)


def preview(path: str, sheet: str | None = None, header_row: int = 1, limit: int = 50) -> dict:
    """Preview of the sheet. A header row beyond the end of the sheet falls back to row 1;
    `header_row_reset_from` then holds the row that was asked for."""
    sheets = list_sheets(path)
    reset_from = None
    try:
        columns, rows = load_rows(path, sheet, header_row)
    except HeaderRowBeyondEnd:
        if header_row == 1:
            raise
        reset_from, header_row = header_row, 1
        columns, rows = load_rows(path, sheet, header_row)
    return {
        "sheets": sheets,
        "sheet": sheet or (sheets[0] if sheets else None),
        "header_row": header_row,
        "header_row_reset_from": reset_from,
        "columns": columns,
        "types": detect_types(columns, rows),
        "total_rows": len(rows),
        "rows": [{**{c: display_value(r.get(c)) for c in columns}, "_row": r["_row"]} for r in rows[:limit]],
    }
