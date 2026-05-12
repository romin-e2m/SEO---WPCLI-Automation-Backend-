"""
Subagent 5: url_cleanup_verifier
Verifies URL replacement in post/page content.

Action type: "url_cleanup"
Strategy:
  - Fetch post or page via WP REST API with context=edit
  - Read content["rendered"]
  - Check: detail["old_url"] is NOT present in content (absence check)
  - Check: detail["new_url"] IS present in content (presence check)
  - Both conditions must pass for verified=True
  - Also checks http/https variants of each URL
"""

from __future__ import annotations

from models import ExecuteRowResult, QARowResult, QASubagentReport
from skills.wp_rest_client import WPRestClient


ACTION_TYPE = "url_cleanup"


async def verify(
    rows: list[ExecuteRowResult],
    base_url: str,
    username: str,
    app_password: str,
) -> QASubagentReport:
    """
    Run URL cleanup verification for all url_cleanup rows (outcome == "updated" only).
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
    """Verify a single url_cleanup row. Catches all exceptions."""
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
        old_url = detail.get("old_url", "")
        new_url = detail.get("new_url", "")

        if not old_url and not new_url:
            base_result.error = "detail.old_url and detail.new_url are both empty — nothing to verify"
            return base_result

        base_result.expected = "old_url removed, new_url present"

        # Fetch post or page content
        wp_data = await client.get_post_or_page(row.post_id)
        rendered_html = (wp_data.get("content") or {}).get("rendered", "")
        base_result.method = "rest_api"

        # Check old_url absence (including both http/https variants)
        old_url_absent = True
        old_url_found_variant: str | None = None
        if old_url:
            for variant in _url_variants(old_url):
                if variant in rendered_html:
                    old_url_absent = False
                    old_url_found_variant = variant
                    break

        # Check new_url presence (including both http/https variants)
        new_url_present = False
        new_url_found_variant: str | None = None
        if new_url:
            for variant in _url_variants(new_url):
                if variant in rendered_html:
                    new_url_present = True
                    new_url_found_variant = variant
                    break
        else:
            # If there's no new_url to check, only check old_url absence
            new_url_present = True

        verified = old_url_absent and new_url_present

        # Build a human-readable actual value for reporting
        parts: list[str] = []
        if old_url:
            parts.append(
                f"old_url {'absent' if old_url_absent else f'STILL PRESENT as {old_url_found_variant!r}'}"
            )
        if new_url:
            parts.append(
                f"new_url {'present as ' + repr(new_url_found_variant) if new_url_present else 'MISSING'}"
            )
        base_result.actual = "; ".join(parts)

        base_result.verified = verified
        base_result.extra = {
            "old_url_absent": old_url_absent,
            "new_url_present": new_url_present,
            "old_url_found_as": old_url_found_variant,
            "new_url_found_as": new_url_found_variant,
        }
        return base_result

    except Exception as exc:
        base_result.error = str(exc)
        return base_result


def _url_variants(url: str) -> list[str]:
    """
    Return both http and https variants of a URL so we catch
    mixed-scheme content in rendered HTML.
    """
    url = url.strip()
    variants = [url]
    if url.startswith("https://"):
        variants.append("http://" + url[len("https://"):])
    elif url.startswith("http://"):
        variants.append("https://" + url[len("http://"):])
    return variants
