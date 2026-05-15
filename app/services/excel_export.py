from __future__ import annotations

import io
from typing import Any

import openpyxl

from app.schemas.workbook import NormalizedRow

_OUTCOME_TO_STATUS: dict[str, str] = {
    "updated": "Done",
    "skipped": "Skipped",
    "failed": "Blocked",
}


def build_export_excel(
    grouped: dict[str, list[NormalizedRow]],
    row_results: list[dict[str, Any]],
) -> bytes:
    """Build an Excel workbook from pipeline run results.

    Each action-type group becomes a sheet. Original column values are preserved
    and a trailing ``Status`` column is appended with the per-row outcome.

    Args:
        grouped: Action-type -> list[NormalizedRow] mapping from the stored payload.
        row_results: Accumulated per-row result dicts from ExecutionLogger.get_row_results().

    Returns:
        Raw bytes of an .xlsx file.
    """
    # Build a fast lookup: (sheet_name, row_index) -> status label
    result_lookup: dict[tuple[str, int], str] = {}
    for r in row_results:
        sheet = r.get("sheet_name", "")
        row_idx = r.get("row_index")
        outcome = r.get("outcome", "")
        if sheet and row_idx is not None:
            result_lookup[(sheet, int(row_idx))] = _OUTCOME_TO_STATUS.get(outcome, "Skipped")

    wb = openpyxl.Workbook()
    # Remove the default empty sheet openpyxl creates
    default_sheet = wb.active
    if default_sheet is not None:
        wb.remove(default_sheet)

    for action_type, rows in grouped.items():
        if not rows:
            continue

        ws = wb.create_sheet(title=action_type)

        # Derive column headers from the first row's original_values; fall back to
        # canonical values keys when original_values was not populated (e.g. old data).
        first_row = rows[0]
        if first_row.original_values:
            headers = list(first_row.original_values.keys())
        else:
            headers = list(first_row.values.keys())

        # Write header row
        ws.append(headers + ["Status"])

        for nr in rows:
            if nr.original_values:
                cell_values = [nr.original_values.get(h, "") for h in headers]
            else:
                cell_values = [nr.values.get(h, "") for h in headers]

            status = result_lookup.get((nr.sheet_name, nr.row_index), "Skipped")
            ws.append(cell_values + [status])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
