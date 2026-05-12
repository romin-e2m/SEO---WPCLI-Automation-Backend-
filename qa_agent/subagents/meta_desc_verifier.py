"""
Subagent 2: meta_desc_verifier
Verifies meta description changes.

Action type: "meta"
Strategy (cascading — stop at first match):
  1. REST: GET .../wp/v2/posts/{post_id}?context=edit -> meta.rank_math_description
  2. REST: same response -> yoast_head_json.description
  3. HTML fallback: GET source_url -> <meta name="description" content="...">
Compare against detail["new_meta_description"]
"""

from __future__ import annotations
import unicodedata
import re

from models import ExecuteRowResult, QARowResult, QASubagentReport
from skills.wp_rest_client import WPRestClient
from skills.html_scraper import fetch_page_data


def _normalise(s: str) -> str:
    """Normalise for comparison: NFC, Unicode dashes → hyphen, collapse whitespace, lowercase."""
    s = unicodedata.normalize("NFC", s)
    s = re.sub(r"[‐-―−﹘﹣－]", "-", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip().lower()


ACTION_TYPE = "meta"


async def verify(
    rows: list[ExecuteRowResult],
    base_url: str,
    username: str,
    app_password: str,
) -> QASubagentReport:
    """
    Run meta description verification for all meta rows (outcome == "updated" only).
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
    """Verify a single meta row. Catches all exceptions."""
    base_result = QARowResult(
        action_type=ACTION_TYPE,
        row_index=row.row_index,
        sheet_name=row.sheet_name,
        post_id=row.post_id,
        verified=False,
    )

    try:
        detail = row.detail or {}
        if "new_meta_description" not in detail:
            base_result.error = "detail.new_meta_description is missing — nothing to verify"
            return base_result
        expected = (detail["new_meta_description"] or "").strip()
        source_url = detail.get("source_url", "")

        base_result.expected = expected

        # --- Layer 1: REST API via Rank Math meta field ---
        if row.post_id is not None:
            try:
                wp_data = await client.get_post_or_page(row.post_id)

                # --- Layer 1: REST API via Rank Math meta field ---
                meta_fields = wp_data.get("meta") or {}
                rank_math_desc = meta_fields.get("rank_math_description", "")
                if rank_math_desc:
                    base_result.actual = rank_math_desc
                    base_result.method = "rest_api:rank_math_description"
                    base_result.verified = (
                        _normalise(rank_math_desc) == _normalise(expected)
                    )
                    return base_result

                # --- Layer 2: REST API via Yoast head JSON ---
                yoast = wp_data.get("yoast_head_json") or {}
                yoast_desc = yoast.get("description", "")
                if yoast_desc:
                    base_result.actual = yoast_desc
                    base_result.method = "rest_api:yoast_head_json.description"
                    base_result.verified = (
                        _normalise(yoast_desc) == _normalise(expected)
                    )
                    return base_result

            except Exception:
                # REST failed — fall through to HTML scrape
                pass

        # --- Layer 3: HTML scrape fallback ---
        if not source_url:
            base_result.error = "REST meta fields not found and no source_url for HTML fallback"
            return base_result

        page_data = await fetch_page_data(source_url)
        if page_data.error:
            base_result.error = f"HTML fallback failed: {page_data.error}"
            return base_result

        actual_meta = page_data.meta_description
        base_result.actual = actual_meta
        base_result.method = "html_scrape"

        if actual_meta is None:
            base_result.error = "No <meta name='description'> found in HTML"
            return base_result

        base_result.verified = _normalise(actual_meta) == _normalise(expected)
        return base_result

    except Exception as exc:
        base_result.error = str(exc)
        return base_result
