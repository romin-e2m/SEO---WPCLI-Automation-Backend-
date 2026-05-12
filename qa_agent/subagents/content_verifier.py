"""
Subagent 1: content_verifier
Verifies on_page changes — specifically the H1 heading in post/page content.

Action type: "on_page"
Strategy:
  - Fetch post or page via WP REST API with context=edit
  - Extract first <h1> from content["rendered"]
  - Compare (strip, lower) against detail["new_title"]
"""

from __future__ import annotations

from models import ExecuteRowResult, QARowResult, QASubagentReport
from skills.wp_rest_client import WPRestClient
from skills.html_scraper import extract_h1_from_html


ACTION_TYPE = "on_page"


async def verify(
    rows: list[ExecuteRowResult],
    base_url: str,
    username: str,
    app_password: str,
) -> QASubagentReport:
    """
    Run content verification for all on_page rows (outcome == "updated" only).
    Returns a QASubagentReport.
    """
    results: list[QARowResult] = []

    async with WPRestClient(base_url, username, app_password) as client:
        for row in rows:
            result = await _verify_row(row, client)
            results.append(result)

    passed = sum(1 for r in results if r.verified and not r.error)
    errors = sum(1 for r in results if r.error)
    failed = len(results) - passed - errors

    return QASubagentReport(
        action_type=ACTION_TYPE,
        total=len(results),
        passed=passed,
        failed=failed,
        errors=errors,
        rows=results,
    )


async def _verify_row(row: ExecuteRowResult, client: WPRestClient) -> QARowResult:
    """Verify a single on_page row. Catches all exceptions."""
    base_result = QARowResult(
        action_type=ACTION_TYPE,
        row_index=row.row_index,
        sheet_name=row.sheet_name,
        post_id=row.post_id,
        verified=False,
    )

    try:
        if row.post_id is None:
            base_result.error = "Missing post_id — cannot verify"
            return base_result

        detail = row.detail or {}
        expected_h1 = detail.get("new_title", "")
        if not expected_h1:
            base_result.error = "detail.new_title is empty — nothing to verify"
            return base_result

        base_result.expected = expected_h1

        # Fetch post or page
        wp_data = await client.get_post_or_page(row.post_id)

        # Extract rendered content HTML
        rendered_html = (wp_data.get("content") or {}).get("rendered", "")
        actual_h1 = extract_h1_from_html(rendered_html)
        base_result.actual = actual_h1
        base_result.method = "rest_api"

        if actual_h1 is None:
            base_result.error = "No <h1> found in rendered content"
            return base_result

        base_result.verified = actual_h1.strip().lower() == expected_h1.strip().lower()
        return base_result

    except Exception as exc:
        base_result.error = str(exc)
        return base_result
