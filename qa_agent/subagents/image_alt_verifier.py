"""
Subagent 4: image_alt_verifier
Verifies image alt text changes.

Action type: "images"
Strategy:
  - Use attachment_id from row (or detail["media_id"] as fallback)
  - Calls GET {base_url}/wp-json/wp/v2/media/{attachment_id}?context=edit
  - Read response["alt_text"]
  - Compare against detail["new_alt_text"]
No fallback needed — media endpoint is definitive.
"""

from __future__ import annotations

from models import ExecuteRowResult, QARowResult, QASubagentReport
from skills.wp_rest_client import WPRestClient


ACTION_TYPE = "images"


async def verify(
    rows: list[ExecuteRowResult],
    base_url: str,
    username: str,
    app_password: str,
) -> QASubagentReport:
    """
    Run image alt text verification for all images rows (outcome == "updated" only).
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
    """Verify a single images row. Catches all exceptions."""
    base_result = QARowResult(
        action_type=ACTION_TYPE,
        row_index=row.row_index,
        sheet_name=row.sheet_name,
        attachment_id=row.attachment_id,
        verified=False,
    )

    try:
        detail = row.detail or {}
        if "new_alt_text" not in detail:
            base_result.error = "detail.new_alt_text is missing — nothing to verify"
            return base_result
        expected_alt = (detail["new_alt_text"] or "")

        base_result.expected = expected_alt

        # Resolve attachment ID — row-level takes priority, detail["media_id"] as fallback
        attachment_id = row.attachment_id
        if attachment_id is None:
            raw_media_id = detail.get("media_id")
            if raw_media_id is not None:
                try:
                    attachment_id = int(raw_media_id)
                except (TypeError, ValueError):
                    pass

        if attachment_id is None:
            base_result.error = "No attachment_id or detail.media_id available — cannot verify"
            return base_result

        base_result.attachment_id = attachment_id

        media_data = await client.get_media(attachment_id)
        actual_alt = media_data.get("alt_text", "")

        # alt_text is "" (empty string) when not set — treat None and "" the same
        base_result.actual = actual_alt if actual_alt else None
        base_result.method = "rest_api"

        base_result.verified = (actual_alt or "").strip().lower() == expected_alt.strip().lower()
        return base_result

    except Exception as exc:
        base_result.error = str(exc)
        return base_result
