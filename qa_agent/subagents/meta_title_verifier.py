"""
Subagent 3: meta_title_verifier
Verifies meta title changes.

Action type: "meta_title"
Strategy (cascading — stop at first match):
  1. REST: GET .../wp/v2/posts/{post_id}?context=edit
       a. meta.rank_math_title          (Rank Math — registered postmeta)
       b. meta._rank_math_title         (Rank Math — alternate key)
       c. yoast_head_json.title         (Yoast SEO — rendered title)
  2. HTML scrape: GET source_url (no auth) -> <title> tag text
     Ground truth for both plugins. Note: Yoast and Rank Math append the site name
     to rendered titles (e.g. "My Page - Site Name"), so comparison is substring-based
     when the expected value doesn't include the suffix.

Note: Rank Math's free plan may not register rank_math_title in the REST API
?context=edit response. The HTML <title> scrape is the definitive check for
Rank Math sites.
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


def _title_matches(actual: str, expected: str) -> bool:
    """
    Compare actual rendered title against expected.

    Both Yoast and Rank Math append the site name to the rendered <title>
    (e.g. "My Page Title - Site Name"). The stored/expected value is the raw
    title without the suffix.

    Passes when:
      - Exact normalised match, OR
      - The normalised expected value is contained within the normalised actual value
        (handles " - Site Name" suffix appended by either plugin).
    """
    n_actual = _normalise(actual)
    n_expected = _normalise(expected)
    if not n_expected:
        return False
    return n_actual == n_expected or n_expected in n_actual


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

        # --- Layer 1: REST API (fast path — Yoast or Rank Math registered postmeta) ---
        if row.post_id is not None:
            try:
                wp_data = await client.get_post_or_page(row.post_id)
                meta_fields = wp_data.get("meta") or {}

                # Rank Math: stores raw title as rank_math_title (or _rank_math_title)
                rank_math_title = (
                    meta_fields.get("rank_math_title") or
                    meta_fields.get("_rank_math_title") or
                    ""
                )
                if rank_math_title:
                    base_result.actual = rank_math_title
                    base_result.method = "rest_api:rank_math_title"
                    base_result.verified = _normalise(rank_math_title) == _normalise(expected)
                    return base_result

                # Yoast: rendered title in yoast_head_json (includes site name suffix)
                yoast = wp_data.get("yoast_head_json") or {}
                yoast_title = yoast.get("title", "")
                if yoast_title:
                    base_result.actual = yoast_title
                    base_result.method = "rest_api:yoast_head_json.title"
                    base_result.verified = _title_matches(yoast_title, expected)
                    return base_result

            except Exception:
                # REST unavailable — fall through to HTML scrape
                pass

        # --- Layer 2: HTML scrape (ground truth for both plugins) ---
        # For Rank Math free plan, rank_math_title may not be in the REST response.
        # The rendered <title> tag is the definitive check — it's what Google indexes.
        if not source_url:
            base_result.error = "REST meta fields not found and no source_url for HTML fallback"
            return base_result

        page_data = await fetch_page_data(source_url)
        if page_data.error:
            base_result.error = f"HTML scrape failed: {page_data.error}"
            return base_result

        actual_title = page_data.title
        base_result.actual = actual_title
        base_result.method = "html_scrape"

        if actual_title is None:
            base_result.error = "No <title> tag found in HTML — title not saved to DB"
            return base_result

        # Both Yoast and Rank Math append " - Site Name" to the rendered <title>.
        # Verify that the expected value is present within the rendered title.
        base_result.verified = _title_matches(actual_title, expected)
        return base_result

    except Exception as exc:
        base_result.error = str(exc)
        return base_result
