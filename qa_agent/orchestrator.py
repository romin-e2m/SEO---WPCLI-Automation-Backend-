"""
Top-level QA orchestrator.

Receives the ExecuteResponse dict from the automation backend,
groups rows by action_type, runs all 6 subagents concurrently,
and produces a QAReport.
"""

from __future__ import annotations

import asyncio
from typing import Any

from models import ExecuteRowResult, QAReport, QASubagentReport
from skills.report_builder import build_report
import subagents.content_verifier as content_verifier
import subagents.meta_desc_verifier as meta_desc_verifier
import subagents.meta_title_verifier as meta_title_verifier
import subagents.image_alt_verifier as image_alt_verifier
import subagents.url_cleanup_verifier as url_cleanup_verifier
import subagents.redirect_verifier as redirect_verifier


# Maps action_type -> subagent verify function
_SUBAGENT_MAP = {
    "on_page": content_verifier.verify,
    "meta": meta_desc_verifier.verify,
    "meta_title": meta_title_verifier.verify,
    "images": image_alt_verifier.verify,
    "url_cleanup": url_cleanup_verifier.verify,
    "redirects_301": redirect_verifier.verify,
}


async def run_qa(
    execute_response: dict[str, Any],
    base_url: str,
    username: str,
    app_password: str,
) -> QAReport:
    """
    Main entry point for the QA orchestrator.

    Args:
        execute_response: The full JSON dict from POST /api/run/execute.
        base_url:         WordPress site root URL (e.g. https://mysite.com).
        username:         WordPress username.
        app_password:     WordPress Application Password.

    Returns:
        A fully populated QAReport.
    """
    # Parse and validate input rows
    raw_rows: list[dict] = execute_response.get("rows", [])
    execution_id: str | None = execute_response.get("execution_id")

    all_rows: list[ExecuteRowResult] = []
    for raw in raw_rows:
        try:
            all_rows.append(ExecuteRowResult.model_validate(raw))
        except Exception:
            # Silently skip malformed rows — they were never "updated" anyway
            continue

    # Include updated and skipped rows — both represent a final WordPress state worth verifying
    rows_to_verify = [r for r in all_rows if r.outcome in ("updated", "skipped")]

    # Exclude skipped image rows — they have no attachment_id/media_id so cannot be verified
    def should_verify(row: ExecuteRowResult) -> bool:
        if row.action_type == "images" and row.outcome == "skipped":
            return False
        return True

    rows_to_verify = [r for r in rows_to_verify if should_verify(r)]

    # Group by action_type
    grouped: dict[str, list[ExecuteRowResult]] = {}
    for row in rows_to_verify:
        grouped.setdefault(row.action_type, []).append(row)

    # Build coroutines for each known action_type that has rows
    tasks: list = []

    for action_type, verify_fn in _SUBAGENT_MAP.items():
        rows_for_type = grouped.get(action_type, [])
        if rows_for_type:
            tasks.append(
                _run_subagent_safe(verify_fn, rows_for_type, base_url, username, app_password, action_type)
            )

    # Run all subagents in parallel
    if tasks:
        subagent_reports: list[QASubagentReport] = list(await asyncio.gather(*tasks))
    else:
        subagent_reports = []

    # Build and return the final report
    return build_report(
        subagent_reports=subagent_reports,
        site_url=base_url,
        execution_id=execution_id,
    )


async def _run_subagent_safe(
    verify_fn,
    rows: list[ExecuteRowResult],
    base_url: str,
    username: str,
    app_password: str,
    action_type: str,
) -> QASubagentReport:
    """
    Wrapper that catches top-level exceptions from a subagent so that
    one failing subagent never crashes the entire orchestrator.
    """
    try:
        return await verify_fn(rows, base_url, username, app_password)
    except Exception as exc:
        # Return an error report for the entire subagent
        from models import QARowResult

        error_rows = [
            QARowResult(
                action_type=action_type,
                row_index=row.row_index,
                sheet_name=row.sheet_name,
                verified=False,
                error=f"Subagent crashed: {exc}",
            )
            for row in rows
        ]
        return QASubagentReport(
            action_type=action_type,
            total=len(rows),
            passed=0,
            failed=0,
            errors=len(rows),
            rows=error_rows,
        )
