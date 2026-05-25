from __future__ import annotations

import io
from typing import Any

import openpyxl

from app.schemas.workbook import NormalizedRow

_OUTCOME_TO_AUTOMATION_STATUS: dict[str, str] = {
    "updated": "Done",
    "skipped": "Skipped",
    "failed": "Blocked",
}

_AUTOMATION_HEADER = "Automation Status"
_QA_HEADER = "QA Status"


def _filter_export_headers(headers: list[str]) -> list[str]:
    """Drop template ``Status`` column; export uses Automation/QA status columns instead."""
    return [h for h in headers if h.strip().lower() != "status"]


def _automation_lookup(row_results: list[dict[str, Any]]) -> dict[tuple[str, int], str]:
    result_lookup: dict[tuple[str, int], str] = {}
    for r in row_results:
        sheet = r.get("sheet_name", "")
        row_idx = r.get("row_index")
        outcome = r.get("outcome", "")
        if sheet and row_idx is not None:
            result_lookup[(sheet, int(row_idx))] = _OUTCOME_TO_AUTOMATION_STATUS.get(
                outcome, "Skipped"
            )
    return result_lookup


def _qa_lookup(qa_report: dict[str, Any] | None) -> dict[tuple[str, int], str]:
    if not qa_report:
        return {}
    lookup: dict[tuple[str, int], str] = {}
    for sub in qa_report.get("subagent_reports", []):
        for row in sub.get("rows", []):
            sheet = row.get("sheet_name", "")
            row_idx = row.get("row_index")
            if not sheet or row_idx is None:
                continue
            verified = row.get("verified")
            if verified is True:
                lookup[(sheet, int(row_idx))] = "Pass"
            elif verified is False:
                lookup[(sheet, int(row_idx))] = "Fail"
    return lookup


def build_export_excel(
    grouped: dict[str, list[NormalizedRow]],
    row_results: list[dict[str, Any]],
    qa_report: dict[str, Any] | None = None,
) -> bytes:
    """Build an Excel workbook from pipeline run results.

    Each action-type group becomes a sheet. Original column values are preserved
    (except a template ``Status`` column, which is replaced) and trailing
    ``Automation Status`` and ``QA Status`` columns are appended.

    Args:
        grouped: Action-type -> list[NormalizedRow] mapping from the stored payload.
        row_results: Accumulated per-row result dicts from ExecutionLogger.get_row_results().
        qa_report: Optional QA report dict from ExecutionLogger.get_qa_report(); when
            omitted, QA Status cells are left blank.

    Returns:
        Raw bytes of an .xlsx file.
    """
    automation_lookup = _automation_lookup(row_results)
    qa_lookup = _qa_lookup(qa_report)

    wb = openpyxl.Workbook()
    default_sheet = wb.active
    if default_sheet is not None:
        wb.remove(default_sheet)

    for action_type, rows in grouped.items():
        if not rows:
            continue

        ws = wb.create_sheet(title=action_type)

        first_row = rows[0]
        if first_row.original_values:
            raw_headers = list(first_row.original_values.keys())
        else:
            raw_headers = list(first_row.values.keys())

        headers = _filter_export_headers(raw_headers)
        ws.append([action_type])  # row 1: sheet title
        ws.append(headers + [_AUTOMATION_HEADER, _QA_HEADER])  # row 2: column headers

        for nr in rows:
            if nr.original_values:
                cell_values = [nr.original_values.get(h, "") for h in headers]
            else:
                cell_values = [nr.values.get(h, "") for h in headers]

            key = (nr.sheet_name, nr.row_index)
            automation = automation_lookup.get(key, "Skipped")
            qa_status = qa_lookup.get(key, "") if qa_report else ""
            ws.append(cell_values + [automation, qa_status])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
