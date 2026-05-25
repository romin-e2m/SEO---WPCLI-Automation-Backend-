"""
Subagent 1: content_verifier
Verifies on_page changes — specifically the H1 heading in post/page content.

Action type: "on_page"
Strategy:
  - Fetch post or page via WP REST API with context=edit
  - Extract first <h1> from content["rendered"]
  - Fall back to public HTML scrape when REST body is empty (e.g. Elementor)
  - Compare (strip, lower) against detail["new_title"]
"""

from __future__ import annotations

from models import ExecuteRowResult, QARowResult, QASubagentReport
from skills.wp_rest_client import WPRestClient
from skills.html_scraper import extract_h1_from_html, fetch_page_data, is_empty_html_body


ACTION_TYPE = "on_page"


def _on_page_h1_was_applied(row: ExecuteRowResult) -> bool:
    """True when execute actually changed post content H1 (not a no-op skip)."""
    if row.outcome == "updated":
        fields = (row.detail or {}).get("fields_updated") or []
        return "h1" in fields
    return False


async def verify(
    rows: list[ExecuteRowResult],
    base_url: str,
    username: str,
    app_password: str,
) -> QASubagentReport:
    """
    Run content verification for on_page rows that had an H1 change applied.
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
        if row.outcome == "skipped" and not _on_page_h1_was_applied(row):
            base_result.verified = True
            base_result.method = "not_applicable"
            base_result.extra = {
                "note": "skipped_no_h1_change",
                "message": "Automation did not apply an H1 change — QA skipped.",
            }
            return base_result

        if row.post_id is None:
            base_result.error = "Missing post_id — cannot verify"
            return base_result

        detail = row.detail or {}
        expected_h1 = detail.get("new_title", "")
        if not expected_h1:
            base_result.error = "detail.new_title is empty — nothing to verify"
            return base_result

        base_result.expected = expected_h1

        wp_data = await client.get_post_or_page(row.post_id)

        rendered_html = (wp_data.get("content") or {}).get("rendered", "")
        actual_h1 = extract_h1_from_html(rendered_html)
        base_result.method = "rest_api"

        source_url = (detail.get("source_url") or detail.get("url") or "").strip()
        if actual_h1 is None and source_url:
            page_data = await fetch_page_data(source_url)
            if not page_data.error and page_data.h1:
                actual_h1 = page_data.h1
                base_result.method = "html_scrape"

        base_result.actual = actual_h1

        if actual_h1 is None and is_empty_html_body(rendered_html):
            if _on_page_h1_was_applied(row):
                base_result.error = (
                    "H1 was saved to post content but is not visible in rendered output "
                    "(likely a page-builder page). Add recommended_content or set H1 in the builder."
                )
                return base_result
            base_result.verified = True
            base_result.method = "rest_api:empty_content"
            base_result.extra = {
                "note": "empty_or_builder_page",
                "message": (
                    "No post content in WordPress (e.g. Elementor-only page). "
                    "Use recommended_content in the sheet or set H1 in the builder."
                ),
            }
            return base_result

        if actual_h1 is None:
            base_result.error = "No <h1> found in rendered content or public page"
            return base_result

        base_result.verified = actual_h1.strip().lower() == expected_h1.strip().lower()
        return base_result

    except Exception as exc:
        base_result.error = str(exc)
        return base_result
