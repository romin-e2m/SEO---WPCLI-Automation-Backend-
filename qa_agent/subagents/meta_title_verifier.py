"""
Subagent 3: meta_title_verifier
Verifies meta title changes.

Action type: "meta_title"
Strategy (cascading — stop at first match):
  1. REST: GET .../wp/v2/posts/{post_id}?context=edit -> meta.rank_math_title
  2. REST: same response -> yoast_head_json.title
  3. HTML fallback: GET source_url -> <title> tag text
Compare against detail["new_meta_title"]
"""

from __future__ import annotations
import unicodedata
import re

from models import ExecuteRowResult, QARowResult, QASubagentReport
from skills.wp_rest_client import WPRestClient
from skills.html_scraper import fetch_page_data


def _normalise(s: str) -> str:
    """
    Normalise a title string for comparison.
    - NFC unicode normalisation (resolves composed vs decomposed chars)
    - Collapse all Unicode dash variants (en-dash, em-dash, figure dash, etc.) to plain hyphen
    - Collapse multiple whitespace to single space
    - Strip leading/trailing whitespace
    - Lowercase
    """
    s = unicodedata.normalize("NFC", s)
    # Replace all Unicode dashes with plain hyphen
    s = re.sub(r"[‐-―−﹘﹣－]", "-", s)
    # Collapse multiple spaces/tabs
    s = re.sub(r"\s+", " ", s)
    return s.strip().lower()


ACTION_TYPE = "meta_title"


async def verify(
    rows: list[ExecuteRowResult],
    base_url: str,
    username: str,
    app_password: str,
) -> QASubagentReport:
    """
    Run meta title verification for all meta_title rows (outcome == "updated" only).
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
    """Verify a single meta_title row. Catches all exceptions."""
    base_result = QARowResult(
        action_type=ACTION_TYPE,
        row_index=row.row_index,
        sheet_name=row.sheet_name,
        post_id=row.post_id,
        verified=False,
    )

    try:
        detail = row.detail or {}
        if "new_meta_title" not in detail:
            base_result.error = "detail.new_meta_title is missing — nothing to verify"
            return base_result
        expected = (detail["new_meta_title"] or "").strip()
        source_url = detail.get("source_url", "")

        base_result.expected = expected

        # --- Layer 1: REST API via Rank Math meta field ---
        if row.post_id is not None:
            try:
                wp_data = await client.get_post_or_page(row.post_id)

                # --- Layer 1: REST API via Rank Math meta field ---
                meta_fields = wp_data.get("meta") or {}
                rank_math_title = meta_fields.get("rank_math_title", "")
                if rank_math_title:
                    base_result.actual = rank_math_title
                    base_result.method = "rest_api:rank_math_title"
                    base_result.verified = (
                        _normalise(rank_math_title) == _normalise(expected)
                    )
                    return base_result

                # --- Layer 2: REST API via Yoast head JSON ---
                yoast = wp_data.get("yoast_head_json") or {}
                yoast_title = yoast.get("title", "")
                if yoast_title:
                    base_result.actual = yoast_title
                    base_result.method = "rest_api:yoast_head_json.title"
                    base_result.verified = (
                        _normalise(yoast_title) == _normalise(expected)
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

        actual_title = page_data.title
        base_result.actual = actual_title
        base_result.method = "html_scrape"

        if actual_title is None:
            base_result.error = "No <title> tag found in HTML"
            return base_result

        base_result.verified = _normalise(actual_title) == _normalise(expected)
        return base_result

    except Exception as exc:
        base_result.error = str(exc)
        return base_result
