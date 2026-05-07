import io
import math
from pathlib import PurePath
from typing import Any

import pandas as pd
import xlrd
from openpyxl import load_workbook

_XLSX_MAGIC = b"PK"
_XLS_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _is_probably_xlsx(content: bytes) -> bool:
    return len(content) >= 2 and content[:2] == _XLSX_MAGIC


def _is_probably_xls(content: bytes) -> bool:
    return len(content) >= 8 and content[:8] == _XLS_MAGIC


def _all_sheet_row_counts_xlsx(content: bytes) -> dict[str, int]:
    bio = io.BytesIO(content)
    wb = load_workbook(bio, read_only=True, data_only=True)
    try:
        counts: dict[str, int] = {}
        for name in wb.sheetnames:
            ws = wb[name]
            mr = ws.max_row
            counts[name] = int(mr) if mr is not None else 0
        return counts
    finally:
        wb.close()


def _stringify_column(col: Any) -> str:
    if col is None or (isinstance(col, float) and math.isnan(col)):
        return ""
    return str(col).strip()


def _json_safe_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    return value


def _xls_cell_to_python(cell: Any, book: xlrd.Book) -> Any:
    t = cell.ctype
    v = cell.value
    if t == xlrd.XL_CELL_EMPTY or t == xlrd.XL_CELL_BLANK:
        return None
    if t == xlrd.XL_CELL_TEXT:
        return v
    if t == xlrd.XL_CELL_BOOLEAN:
        return bool(v)
    if t == xlrd.XL_CELL_ERROR:
        return None
    if t == xlrd.XL_CELL_NUMBER:
        return v
    if t == xlrd.XL_CELL_DATE:
        try:
            return xlrd.xldate_as_datetime(v, book.datemode)
        except Exception:
            return v
    return v


def _xls_unique_columns(raw_headers: list[Any]) -> list[str]:
    columns: list[str] = []
    counts: dict[str, int] = {}
    for i, h in enumerate(raw_headers):
        base = _stringify_column(h) or f"column_{i}"
        c = counts.get(base, 0)
        col = base if c == 0 else f"{base}_{c}"
        counts[base] = c + 1
        columns.append(col)
    return columns


def _dataframe_preview(df: pd.DataFrame, limit: int) -> tuple[list[str], list[dict[str, Any]]]:
    df = df.rename(columns={c: _stringify_column(c) for c in df.columns})
    columns = [str(c) for c in df.columns.tolist()]
    head = df.head(limit)
    rows: list[dict[str, Any]] = []
    for _, row in head.iterrows():
        record: dict[str, Any] = {}
        for col in columns:
            record[col] = _json_safe_value(row.get(col))
        rows.append(record)
    return columns, rows


def _analyze_xlsx_bytes(
    content: bytes,
    *,
    preview_rows: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    if not _is_probably_xlsx(content):
        raise ValueError("File is not a valid .xlsx (Office Open XML) workbook.")

    try:
        row_counts = _all_sheet_row_counts_xlsx(content)
    except Exception:
        row_counts = {}

    bio = io.BytesIO(content)
    xl = pd.ExcelFile(bio, engine="openpyxl")
    try:
        sheet_names = list(xl.sheet_names)
        out: list[dict[str, Any]] = []

        for name in sheet_names:
            df = pd.read_excel(
                xl,
                sheet_name=name,
                header=0,
                dtype=object,
                nrows=preview_rows,
            )
            columns, rows = _dataframe_preview(df, preview_rows)
            out.append(
                {
                    "name": name,
                    "columns": columns,
                    "row_count": row_counts.get(name),
                    "preview_row_count": len(rows),
                    "rows": rows,
                }
            )

        return out, sheet_names
    finally:
        xl.close()


def _analyze_xls_bytes(
    content: bytes,
    *,
    preview_rows: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    if not _is_probably_xls(content):
        raise ValueError("File is not a valid .xls (Excel 97-2003) workbook.")

    book = xlrd.open_workbook(file_contents=content)
    sheet_names = list(book.sheet_names())
    out: list[dict[str, Any]] = []

    for name in sheet_names:
        sh = book.sheet_by_name(name)
        nrows = int(sh.nrows)
        ncol = int(sh.ncols)

        if nrows == 0 or ncol == 0:
            out.append(
                {
                    "name": name,
                    "columns": [],
                    "row_count": nrows,
                    "preview_row_count": 0,
                    "rows": [],
                }
            )
            continue

        raw_headers = [sh.cell_value(0, c) for c in range(ncol)]
        columns = _xls_unique_columns(raw_headers)

        data_rows_upper = min(nrows - 1, preview_rows)
        rows_out: list[dict[str, Any]] = []
        for r in range(1, 1 + data_rows_upper):
            record: dict[str, Any] = {}
            for c, col in enumerate(columns):
                if c >= ncol:
                    record[col] = None
                    continue
                cell = sh.cell(r, c)
                record[col] = _json_safe_value(_xls_cell_to_python(cell, book))
            rows_out.append(record)

        out.append(
            {
                "name": name,
                "columns": columns,
                "row_count": nrows,
                "preview_row_count": len(rows_out),
                "rows": rows_out,
            }
        )

    return out, sheet_names


_CSV_ENCODINGS_TRY = ("utf-8-sig", "utf-8", "cp1252", "latin-1")


def _analyze_csv_bytes(
    content: bytes,
    *,
    preview_rows: int,
    sheet_name: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    if not content.strip():
        raise ValueError("CSV file is empty.")

    last_err: Exception | None = None
    chosen_enc: str | None = None
    preview_df: pd.DataFrame | None = None

    for enc in _CSV_ENCODINGS_TRY:
        try:
            bio = io.BytesIO(content)
            preview_df = pd.read_csv(
                bio,
                encoding=enc,
                dtype=object,
                nrows=preview_rows,
                on_bad_lines="skip",
            )
            chosen_enc = enc
            break
        except Exception as e:
            last_err = e

    if preview_df is None or chosen_enc is None:
        msg = "Could not decode CSV (tried UTF-8 with BOM, UTF-8, Windows-1252, Latin-1)."
        if last_err:
            raise ValueError(msg) from last_err
        raise ValueError(msg)

    row_total = 0
    try:
        bio_count = io.BytesIO(content)
        for chunk in pd.read_csv(
            bio_count,
            encoding=chosen_enc,
            dtype=object,
            chunksize=50_000,
            on_bad_lines="skip",
        ):
            row_total += len(chunk)
    except Exception:
        row_total = len(preview_df)

    columns, rows = _dataframe_preview(preview_df, preview_rows)
    out = [
        {
            "name": sheet_name,
            "columns": columns,
            "row_count": row_total,
            "preview_row_count": len(rows),
            "rows": rows,
        }
    ]
    return out, [sheet_name]


def analyze_spreadsheet_bytes(
    content: bytes,
    filename: str,
    *,
    preview_rows: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    if not content:
        raise ValueError("Empty file.")

    suffix = PurePath(filename).suffix.lower()
    if suffix == ".csv":
        label = PurePath(filename).stem.strip() or "CSV"
        return _analyze_csv_bytes(content, preview_rows=preview_rows, sheet_name=label)
    if suffix == ".xls":
        return _analyze_xls_bytes(content, preview_rows=preview_rows)
    if suffix == ".xlsx":
        return _analyze_xlsx_bytes(content, preview_rows=preview_rows)

    raise ValueError(f"Unsupported file type {suffix or '(none)'}. Use .xlsx, .xls, or .csv.")


def analyze_xlsx_bytes(
    content: bytes,
    *,
    preview_rows: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Backward-compatible entry for tests; prefer analyze_spreadsheet_bytes."""
    return _analyze_xlsx_bytes(content, preview_rows=preview_rows)
