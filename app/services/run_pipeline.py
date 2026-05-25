from __future__ import annotations

import asyncio
import contextvars
import html
import logging
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable

from app.schemas.run import (
    DryRunResponse,
    DryRunRowResult,
    ExecuteResponse,
    ExecuteRowResult,
    FieldDiff,
)
from app.schemas.workbook import NormalizedRow
from app.schemas.wp import SiteAccess
from app.services.wp_rest import WpRestClient, _redirect_rest_backends
from app.services.wp_site import resolve_post_url, rest_client
from app.services.wp_playwright import (
    extract_slug_with_trailing_slash,
    launch_chromium,
    WordPressPlaywright,
    should_run_headless,
)
from app.services.monitor_broadcast import (
    MONITOR_DEFAULT_ID,
    emit_monitor_phase,
    emit_monitor_row_dry,
    emit_monitor_row_exec,
)
from app.services.monitor_context import current_monitor_execution_id
from app.services.execution_logger import ExecutionLogger

logger = logging.getLogger(__name__)

# Context variable holding the event loop that owns the shared Playwright browser session.
# When set, _run_seo_playwright_pool_coroutine and _exec_redirects_301 use this loop
# (via loop.run_until_complete) instead of asyncio.run(), so that the shared Page object
# — which is bound to this loop — remains accessible across all task runners.
_shared_pw_loop: contextvars.ContextVar[asyncio.AbstractEventLoop | None] = contextvars.ContextVar(
    "_shared_pw_loop", default=None
)

_ORDER = ("on_page", "meta", "meta_title", "images", "url_cleanup", "redirects_301")


def _max_run_rows() -> int:
    raw = os.getenv("MAX_RUN_ROWS", "500")
    try:
        return max(1, int(raw))
    except Exception:
        return 500


def _seo_playwright_worker_count() -> int:
    """Always 1 — single Playwright browser, single login, sequential task execution."""
    return 1


def _seo_playwright_min_rows_per_worker() -> int:
    """Avoid too many browsers for few rows (each browser still needs its own login)."""
    raw = os.getenv("PLAYWRIGHT_SEO_MIN_ROWS_PER_WORKER", "3")
    try:
        return max(1, int(raw))
    except Exception:
        return 3


def _seo_playwright_login_gap_sec() -> float:
    """Optional spacing between login lock handoffs (seconds). Default 0."""
    raw = os.getenv("PLAYWRIGHT_SEO_LOGIN_GAP_SEC", "0")
    try:
        return max(0.0, float(raw))
    except Exception:
        return 0.0


def _effective_seo_worker_count(playwright_job_count: int) -> int:
    """
    Honor PLAYWRIGHT_SEO_WORKERS but cap workers when rows are few.
    Example: 6 jobs, min 3 rows/worker -> at most 2 workers even if env says 5.
    """
    configured = _seo_playwright_worker_count()
    jobs = max(0, playwright_job_count)
    if jobs == 0:
        return 1
    min_rows = _seo_playwright_min_rows_per_worker()
    max_useful = max(1, jobs // min_rows)
    return min(configured, jobs, max_useful)


def _s(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip()


def _sanitize_angle_bracket_tags(text: str) -> str:
    """Replace <tag> placeholders with %%tag%% so SEO plugins treat them as dynamic variables."""
    return re.sub(r"<([^<>\s][^<>]*)>", r"%%\1%%", text)


def _normalize_for_comparison(s: str) -> str:
    """
    Normalize a string for comparison.
    - NFC unicode normalization (resolves composed vs decomposed chars)
    - Collapse all Unicode dash variants (en-dash, em-dash, figure dash, etc.) to plain hyphen
    - Collapse multiple whitespace to single space
    - Strip leading/trailing whitespace
    """
    s = unicodedata.normalize("NFC", s)
    # Replace all Unicode dashes with plain hyphen
    s = re.sub(r"[‐-―−﹘﹣－]", "-", s)
    # Collapse multiple spaces/tabs
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _sheet_value_missing_for_compare(value: str) -> bool:
    """True when workbook 'current' is empty or a placeholder, not a real WP snapshot."""
    t = _s(value).upper()
    if not t:
        return True
    return t in ("MISSING", "N/A", "NA", "-", "—", "NONE", "NULL")


# Empty old_* strings are meaningful for execute UI (e.g. missing H1 vs blank meta).
_DETAIL_PRESERVE_EMPTY_KEYS = frozenset(
    {"old_title", "old_meta_title", "old_meta_description", "old_alt_text"}
)


def _detail(**kwargs: Any) -> dict[str, Any]:
    """
    Build a `detail` dict for ExecuteRowResult while dropping empty values.

    Centralised so every executor surfaces the same shape to the frontend:
        - source_url / destination_url / page_url / image_url
        - old_value / new_value (plus action-specific aliases for clarity)
        - post_id / media_id / redirect_id
        - playwright_logs (when applicable)
    """
    out: dict[str, Any] = {}
    for key, value in kwargs.items():
        if value is None:
            continue
        if isinstance(value, str) and value.strip() == "":
            if key in _DETAIL_PRESERVE_EMPTY_KEYS:
                out[key] = value
            continue
        out[key] = value
    return out


def _on_page_h1_detail_from_dry(dr: DryRunRowResult) -> tuple[str | None, str | None]:
    """Extract (old_h1, new_h1) strings from dry-run diffs for on_page."""
    for d in dr.diffs:
        if d.field == "h1":
            cur = d.current if d.current is not None else ""
            prop = d.proposed if d.proposed is not None else ""
            return cur, prop
    return None, None


def _count_grouped(grouped: dict[str, list[NormalizedRow]]) -> int:
    return sum(len(rows) for rows in grouped.values())


def _iter_rows(grouped: dict[str, list[NormalizedRow]]):
    for key in _ORDER:
        for row in grouped.get(key, []) or []:
            yield key, row


def _dry_on_page(site: SiteAccess, row: NormalizedRow) -> DryRunRowResult:
    v = row.values
    page_url = _s(v.get("page_url"))
    current_h1_from_sheet = _s(v.get("current_h1"))
    rec_h1 = _s(v.get("recommended_h1"))
    rec_content = _s(v.get("recommended_content"))

    # First check: compare Excel current vs Excel recommended
    # If they are the same, skip this row
    if current_h1_from_sheet and rec_h1 and current_h1_from_sheet == rec_h1:
        return DryRunRowResult(
            action_type="on_page",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="skip",
            message="Current H1 in sheet matches recommended H1 - already updated.",
        )

    res = resolve_post_url(site, page_url)
    if not res.found or not res.post:
        return DryRunRowResult(
            action_type="on_page",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="blocked",
            message="Could not resolve page URL to a post or page.",
            resolution_method=res.method,
        )
    pid = res.post.id
    client = rest_client(site)
    try:
        obj = client.get_post(pid)
    except Exception as e:
        return DryRunRowResult(
            action_type="on_page",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="error",
            message=f"REST fetch failed: {e}",
            post_id=pid,
            resolution_method=res.method,
        )

    summ = WpRestClient.extract_summary_fields(obj)
    raw_content = summ.get("content")
    cur_content = raw_content if isinstance(raw_content, str) else ""
    wp_cur_h1 = WpRestClient.first_h1_inner_text(cur_content) or ""

    # Second check: verify Excel current matches WordPress current
    if current_h1_from_sheet and wp_cur_h1 and current_h1_from_sheet != wp_cur_h1:
        return DryRunRowResult(
            action_type="on_page",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="blocked",
            message=f"Current H1 in sheet doesn't match WordPress: sheet='{current_h1_from_sheet}' vs wp='{wp_cur_h1}'",
            post_id=pid,
            resolution_method=res.method,
        )

    if (
        rec_h1
        and not rec_content
        and WpRestClient.is_effectively_empty_post_content(cur_content)
    ):
        return DryRunRowResult(
            action_type="on_page",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="blocked",
            message=(
                "Page has no WordPress post content (empty or page-builder only). "
                "Add recommended_content in the sheet, or set the H1 in Elementor/the builder."
            ),
            post_id=pid,
            resolution_method=res.method,
        )

    diffs: list[FieldDiff] = []
    if rec_h1 and rec_h1 != wp_cur_h1:
        diffs.append(FieldDiff(field="h1", current=current_h1_from_sheet or wp_cur_h1, proposed=rec_h1))
    if rec_content and rec_content != cur_content:
        diffs.append(FieldDiff(field="content", current="(HTML body)", proposed="(replace entire body)"))

    outcome: Any = "no_change" if not diffs else "change"
    return DryRunRowResult(
        action_type="on_page",
        sheet_name=row.sheet_name,
        row_index=row.row_index,
        outcome=outcome,
        post_id=pid,
        resolution_method=res.method,
        diffs=diffs,
    )


def _dry_url_cleanup(site: SiteAccess, row: NormalizedRow) -> DryRunRowResult:
    v = row.values
    page_url = _s(v.get("page_url"))
    old_u = _s(v.get("old_url"))
    new_u = _s(v.get("new_url"))
    res = resolve_post_url(site, page_url)
    if not res.found or not res.post:
        return DryRunRowResult(
            action_type="url_cleanup",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="blocked",
            message="Could not resolve page URL to a post or page.",
            resolution_method=res.method,
        )
    pid = res.post.id
    client = rest_client(site)
    try:
        obj = client.get_post(pid)
    except Exception as e:
        return DryRunRowResult(
            action_type="url_cleanup",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="error",
            message=f"REST fetch failed: {e}",
            post_id=pid,
            resolution_method=res.method,
        )
    raw = WpRestClient.extract_summary_fields(obj).get("content")
    content = raw if isinstance(raw, str) else ""
    
    # Try to find the URL in content with different variations
    found = False
    if old_u in content:
        found = True
    else:
        # Try URL variations (with/without trailing slash, different protocols, etc.)
        # Make a regex pattern that matches the URL with various modifications
        escaped_url = re.escape(old_u)
        # Try with optional trailing slash, optional https/http variations
        patterns = [
            escaped_url,
            escaped_url.rstrip("/") + "/?",  # with optional trailing slash
            escaped_url.replace("http://", "https?://"),  # http or https
            escaped_url.replace("https://", "https?://"),
        ]
        for pattern in patterns:
            if re.search(pattern, content):
                found = True
                break
    
    if not found:
        # Don't block - just warn. The URL might be in meta fields, custom fields, or will be skipped.
        # Return as "change" anyway to allow user to decide
        return DryRunRowResult(
            action_type="url_cleanup",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="change",  # Changed from "blocked" to "change"
            post_id=pid,
            resolution_method=res.method,
            diffs=[FieldDiff(field="content_url_replace", current="[URL not found - will be skipped]", proposed=f"{old_u} → {new_u}")],
        )
    return DryRunRowResult(
        action_type="url_cleanup",
        sheet_name=row.sheet_name,
        row_index=row.row_index,
        outcome="change",
        post_id=pid,
        resolution_method=res.method,
        diffs=[FieldDiff(field="content_url_replace", current=old_u, proposed=new_u)],
    )


def _dry_on_image(site: SiteAccess, row: NormalizedRow) -> DryRunRowResult:
    v = row.values
    image_url = _s(v.get("image_url"))
    rec_alt = _s(v.get("recommended_alt_text"))

    if not image_url:
        return DryRunRowResult(
            action_type="images",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="blocked",
            message="Image URL is missing.",
        )

    if not rec_alt:
        return DryRunRowResult(
            action_type="images",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="blocked",
            message="Recommended alt text is missing.",
        )

    client = rest_client(site)
    try:
        media_id = client.search_media_by_url(image_url)
    except Exception as e:
        return DryRunRowResult(
            action_type="images",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="error",
            message=f"Media search failed: {e}",
        )

    if not media_id:
        return DryRunRowResult(
            action_type="images",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="blocked",
            message=f"Could not find media ID for image URL: {image_url}",
        )

    try:
        media_obj = client.get_media(media_id)
    except Exception as e:
        return DryRunRowResult(
            action_type="images",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="error",
            message=f"REST fetch failed: {e}",
        )

    cur_alt = _s(media_obj.get("alt_text", ""))

    if rec_alt == cur_alt:
        return DryRunRowResult(
            action_type="images",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="no_change",
        )

    return DryRunRowResult(
        action_type="images",
        sheet_name=row.sheet_name,
        row_index=row.row_index,
        outcome="change",
        diffs=[FieldDiff(field="alt_text", current=cur_alt, proposed=rec_alt)],
    )


def _dry_meta(site: SiteAccess, row: NormalizedRow) -> DryRunRowResult:
    """Dry-run for meta description updates."""
    v = row.values
    page_url = _s(v.get("page_url"))
    current_meta_from_sheet = _s(v.get("current_meta_description"))
    rec_meta = _s(v.get("recommended_meta_description"))

    if not page_url:
        return DryRunRowResult(
            action_type="meta",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="blocked",
            message="Page URL is missing.",
        )

    if not rec_meta:
        return DryRunRowResult(
            action_type="meta",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="blocked",
            message="Recommended meta description is missing.",
        )

    # First check: compare Excel current vs Excel recommended
    # If they are the same, skip this row
    if current_meta_from_sheet and rec_meta and current_meta_from_sheet == rec_meta:
        return DryRunRowResult(
            action_type="meta",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="skip",
            message="Current meta description in sheet matches recommended meta description - already updated.",
        )

    res = resolve_post_url(site, page_url)
    if not res.found or not res.post:
        return DryRunRowResult(
            action_type="meta",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="blocked",
            message="Could not resolve page URL to a post or page.",
            resolution_method=res.method,
        )

    pid = res.post.id
    client = rest_client(site)
    try:
        obj = client.get_post(pid)
    except Exception as e:
        return DryRunRowResult(
            action_type="meta",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="error",
            message=f"REST fetch failed: {e}",
            post_id=pid,
            resolution_method=res.method,
        )

    # Try to extract existing meta description from multiple sources
    # Plugin-specific meta fields
    wp_cur_meta = ""
    
    # Check Yoast SEO
    yoast_meta = obj.get("meta", {})
    if isinstance(yoast_meta, dict):
        # Yoast stores in _yoast_wpseo_metadesc
        wp_cur_meta = _s(yoast_meta.get("_yoast_wpseo_metadesc", ""))
    
    # Check Rank Math
    if not wp_cur_meta and isinstance(yoast_meta, dict):
        wp_cur_meta = _s(yoast_meta.get("rank_math_description", "")) or _s(yoast_meta.get("_rank_math_description", ""))
    
    # Check SEOPress
    if not wp_cur_meta and isinstance(yoast_meta, dict):
        wp_cur_meta = _s(yoast_meta.get("_seopress_titles_desc", ""))
    
    # Fallback: Check yoast_head_json (rendered meta from Yoast)
    if not wp_cur_meta:
        yoast_head_json = obj.get("yoast_head_json", {})
        if isinstance(yoast_head_json, dict):
            wp_cur_meta = _s(yoast_head_json.get("description", ""))

    # Second check: verify sheet current matches WordPress (skip when sheet has no real current)
    if not _sheet_value_missing_for_compare(current_meta_from_sheet):
        if wp_cur_meta and _normalize_for_comparison(current_meta_from_sheet) != _normalize_for_comparison(wp_cur_meta):
            return DryRunRowResult(
                action_type="meta",
                sheet_name=row.sheet_name,
                row_index=row.row_index,
                outcome="blocked",
                message=f"Current meta description in sheet doesn't match WordPress: sheet='{current_meta_from_sheet}' vs wp='{wp_cur_meta}'",
                post_id=pid,
                resolution_method=res.method,
            )

    if _normalize_for_comparison(rec_meta) == _normalize_for_comparison(wp_cur_meta):
        return DryRunRowResult(
            action_type="meta",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="no_change",
            post_id=pid,
            resolution_method=res.method,
        )

    return DryRunRowResult(
        action_type="meta",
        sheet_name=row.sheet_name,
        row_index=row.row_index,
        outcome="change",
        post_id=pid,
        resolution_method=res.method,
        diffs=[FieldDiff(field="meta_description", current=current_meta_from_sheet or wp_cur_meta, proposed=rec_meta)],
    )


def _dry_meta_title(site: SiteAccess, row: NormalizedRow) -> DryRunRowResult:
    """Dry-run for SEO title (meta title) updates."""
    v = row.values
    page_url = _s(v.get("page_url"))
    current_title_from_sheet = _s(v.get("current_meta_title"))
    rec_title = _s(v.get("recommended_meta_title"))

    if not page_url:
        return DryRunRowResult(
            action_type="meta_title",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="blocked",
            message="Page URL is missing.",
        )

    if not rec_title:
        return DryRunRowResult(
            action_type="meta_title",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="blocked",
            message="Recommended meta title is missing.",
        )

    # First check: compare Excel current vs Excel recommended
    # If they are the same, skip this row
    if current_title_from_sheet and rec_title and current_title_from_sheet == rec_title:
        return DryRunRowResult(
            action_type="meta_title",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="skip",
            message="Current meta title in sheet matches recommended meta title - already updated.",
        )

    res = resolve_post_url(site, page_url)
    if not res.found or not res.post:
        return DryRunRowResult(
            action_type="meta_title",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="blocked",
            message="Could not resolve page URL to a post or page.",
            resolution_method=res.method,
        )

    pid = res.post.id
    client = rest_client(site)
    try:
        obj = client.get_post(pid)
    except Exception as e:
        return DryRunRowResult(
            action_type="meta_title",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="error",
            message=f"REST fetch failed: {e}",
            post_id=pid,
            resolution_method=res.method,
        )

    wp_cur_title = ""
    yoast_meta = obj.get("meta", {})
    if isinstance(yoast_meta, dict):
        wp_cur_title = _s(yoast_meta.get("_yoast_wpseo_title", ""))
    if not wp_cur_title and isinstance(yoast_meta, dict):
        wp_cur_title = _s(yoast_meta.get("rank_math_title", "")) or _s(
            yoast_meta.get("_rank_math_title", "")
        )
    if not wp_cur_title and isinstance(yoast_meta, dict):
        wp_cur_title = _s(yoast_meta.get("_seopress_titles_title", ""))
    if not wp_cur_title:
        yoast_head_json = obj.get("yoast_head_json", {})
        if isinstance(yoast_head_json, dict):
            wp_cur_title = _s(yoast_head_json.get("title", ""))

    if not _sheet_value_missing_for_compare(current_title_from_sheet):
        if wp_cur_title and _normalize_for_comparison(current_title_from_sheet) != _normalize_for_comparison(wp_cur_title):
            return DryRunRowResult(
                action_type="meta_title",
                sheet_name=row.sheet_name,
                row_index=row.row_index,
                outcome="blocked",
                message=f"Current meta title in sheet doesn't match WordPress: sheet='{current_title_from_sheet}' vs wp='{wp_cur_title}'",
                post_id=pid,
                resolution_method=res.method,
            )

    if _normalize_for_comparison(rec_title) == _normalize_for_comparison(wp_cur_title):
        return DryRunRowResult(
            action_type="meta_title",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="no_change",
            post_id=pid,
            resolution_method=res.method,
        )

    return DryRunRowResult(
        action_type="meta_title",
        sheet_name=row.sheet_name,
        row_index=row.row_index,
        outcome="change",
        post_id=pid,
        resolution_method=res.method,
        diffs=[FieldDiff(field="meta_title", current=current_title_from_sheet or wp_cur_title, proposed=rec_title)],
    )


def _dry_redirects_301(site: SiteAccess, row: NormalizedRow, redirect_plugin: str | None = None) -> DryRunRowResult:
    """Dry-run for 301 redirect creation - validates URLs."""
    v = row.values
    from_url = _s(v.get("source_url"))
    to_url = _s(v.get("target_url"))

    if not from_url:
        return DryRunRowResult(
            action_type="redirects_301",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="blocked",
            message="Source URL (source_url) is missing.",
        )

    if not to_url:
        return DryRunRowResult(
            action_type="redirects_301",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="blocked",
            message="Destination URL (target_url) is missing.",
        )

    # Basic validation
    if from_url == to_url:
        return DryRunRowResult(
            action_type="redirects_301",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="blocked",
            message="Source and destination URLs cannot be the same (self-redirect).",
        )

    # Redirect will be attempted via REST API
    # The system will try Redirection, Rank Math, and Safe Redirect Manager plugins in order
    return DryRunRowResult(
        action_type="redirects_301",
        sheet_name=row.sheet_name,
        row_index=row.row_index,
        outcome="change",
        message="Will create 301 redirect using the redirect plugin you selected (REST API when supported, otherwise admin UI).",
        diffs=[FieldDiff(field="redirect_301", current=None, proposed=f"{from_url} → {to_url}")],
    )


def _exec_meta_via_rest(
    site: SiteAccess,
    row: NormalizedRow,
    pid: int,
    rec_meta: str,
    seo_plugin: str | None,
    *,
    page_url: str = "",
    old_meta: str | None = None,
) -> ExecuteRowResult:
    """Apply meta description via REST (shared by single-row and batch fallbacks)."""
    client = rest_client(site)
    base_detail = dict(
        source_url=page_url,
        url=page_url,
        old_meta_description=old_meta,
        new_meta_description=rec_meta,
    )
    try:
        obj = client.update_seo_meta_description(pid, rec_meta, seo_plugin=seo_plugin)

        if not obj or not isinstance(obj, dict):
            logger.warning(
                "Unexpected response from update_seo_meta_description for post %s",
                pid,
            )
            return ExecuteRowResult(
                action_type="meta",
                sheet_name=row.sheet_name,
                row_index=row.row_index,
                outcome="failed",
                message="Invalid response from meta update (REST API may have permission restrictions)",
                post_id=pid,
                detail=_detail(**base_detail),
            )

        post_id_returned = obj.get("id")
        if not post_id_returned or post_id_returned != pid:
            logger.warning(
                "Meta update for post %s returned unexpected post id: %s",
                pid,
                post_id_returned,
            )
            return ExecuteRowResult(
                action_type="meta",
                sheet_name=row.sheet_name,
                row_index=row.row_index,
                outcome="failed",
                message="Post ID mismatch in response (REST API may have permission restrictions)",
                post_id=pid,
                detail=_detail(**base_detail, raw_id=post_id_returned),
            )

        # Verify the meta was actually updated by checking the returned object
        meta_fields = obj.get("meta") or {}
        updated_meta = (
            meta_fields.get("rank_math_description", "") or
            meta_fields.get("_rank_math_description", "") or
            meta_fields.get("_yoast_wpseo_metadesc", "") or
            meta_fields.get("_seopress_titles_desc", "")
        )
        
        # If no meta field was updated, check yoast_head_json as fallback
        if not updated_meta:
            yoast_head = obj.get("yoast_head_json") or {}
            updated_meta = yoast_head.get("description", "")
        
        # If still no meta or doesn't match what we tried to set, REST API likely failed
        if not updated_meta or _normalize_for_comparison(updated_meta) != _normalize_for_comparison(rec_meta):
            logger.warning(
                f"REST API meta update for post {pid} did not persist. "
                f"Expected: '{rec_meta}', Got: '{updated_meta}'. "
                f"This is likely due to REST API permission restrictions on private meta fields."
            )
            return ExecuteRowResult(
                action_type="meta",
                sheet_name=row.sheet_name,
                row_index=row.row_index,
                outcome="failed",
                message="REST API meta update did not persist (permission restrictions - try Playwright/WP-CLI)",
                post_id=pid,
                detail=_detail(**base_detail, raw_id=post_id_returned),
            )

        logger.info("Successfully updated post %s meta description (REST)", pid)

    except Exception as e:
        logger.error(
            "Failed to update meta for post %s: %s: %s",
            pid,
            type(e).__name__,
            str(e),
        )
        return ExecuteRowResult(
            action_type="meta",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="failed",
            message=(
                f"Meta update failed: {str(e)}. REST API may have permission "
                "restrictions - consider using WP-CLI."
            ),
            post_id=pid,
            detail=_detail(**base_detail),
        )

    return ExecuteRowResult(
        action_type="meta",
        sheet_name=row.sheet_name,
        row_index=row.row_index,
        outcome="updated",
        post_id=pid,
        detail=_detail(**base_detail, raw_id=post_id_returned),
    )


def _exec_meta_title_via_rest(
    site: SiteAccess,
    row: NormalizedRow,
    pid: int,
    rec_title: str,
    seo_plugin: str | None,
    *,
    page_url: str = "",
    old_title: str | None = None,
) -> ExecuteRowResult:
    """Apply SEO title via REST."""
    client = rest_client(site)
    base_detail = dict(
        source_url=page_url,
        url=page_url,
        old_meta_title=old_title,
        new_meta_title=rec_title,
    )
    try:
        obj = client.update_seo_meta_title(pid, rec_title, seo_plugin=seo_plugin)
        if not obj or not isinstance(obj, dict):
            logger.warning(
                "Unexpected response from update_seo_meta_title for post %s",
                pid,
            )
            return ExecuteRowResult(
                action_type="meta_title",
                sheet_name=row.sheet_name,
                row_index=row.row_index,
                outcome="failed",
                message="Invalid response from SEO title update (REST API may have permission restrictions)",
                post_id=pid,
                detail=_detail(**base_detail),
            )
        post_id_returned = obj.get("id")
        if not post_id_returned or post_id_returned != pid:
            logger.warning(
                "SEO title update for post %s returned unexpected post id: %s",
                pid,
                post_id_returned,
            )
            return ExecuteRowResult(
                action_type="meta_title",
                sheet_name=row.sheet_name,
                row_index=row.row_index,
                outcome="failed",
                message="Post ID mismatch in response (REST API may have permission restrictions)",
                post_id=pid,
                detail=_detail(**base_detail, raw_id=post_id_returned),
            )
        
        # Verify the title was actually updated by checking the returned object
        meta_fields = obj.get("meta") or {}
        updated_title = (
            meta_fields.get("rank_math_title", "") or
            meta_fields.get("_rank_math_title", "") or
            meta_fields.get("_yoast_wpseo_title", "") or
            meta_fields.get("_seopress_titles_title", "")
        )
        
        # If no meta field was updated, check yoast_head_json as fallback
        if not updated_title:
            yoast_head = obj.get("yoast_head_json") or {}
            updated_title = yoast_head.get("title", "")
        
        # If still no title or doesn't match what we tried to set, REST API likely failed
        if not updated_title or _normalize_for_comparison(updated_title) != _normalize_for_comparison(rec_title):
            logger.warning(
                f"REST API SEO title update for post {pid} did not persist. "
                f"Expected: '{rec_title}', Got: '{updated_title}'. "
                f"This is likely due to REST API permission restrictions on private meta fields."
            )
            return ExecuteRowResult(
                action_type="meta_title",
                sheet_name=row.sheet_name,
                row_index=row.row_index,
                outcome="failed",
                message="REST API SEO title update did not persist (permission restrictions - try Playwright/WP-CLI)",
                post_id=pid,
                detail=_detail(**base_detail, raw_id=post_id_returned),
            )
        
        logger.info("Successfully updated post %s SEO title (REST)", pid)
    except Exception as e:
        logger.error(
            "Failed to update SEO title for post %s: %s: %s",
            pid,
            type(e).__name__,
            str(e),
        )
        return ExecuteRowResult(
            action_type="meta_title",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="failed",
            message=(
                f"SEO title update failed: {str(e)}. REST API may have permission "
                "restrictions - consider using WP-CLI or Playwright."
            ),
            post_id=pid,
            detail=_detail(**base_detail),
        )

    return ExecuteRowResult(
        action_type="meta_title",
        sheet_name=row.sheet_name,
        row_index=row.row_index,
        outcome="updated",
        post_id=pid,
        detail=_detail(**base_detail, raw_id=post_id_returned),
    )



@dataclass(frozen=True)
class _SeoPlaywrightSpec:
    action_type: str
    recommended_values_key: str
    diff_field: str
    detail_old_key: str
    detail_new_key: str
    wp_update_method: str
    success_log_pattern: str
    failure_log_prefix: str
    dry_fn: Callable[[SiteAccess, NormalizedRow], DryRunRowResult]


_SEO_PW_META = _SeoPlaywrightSpec(
    action_type="meta",
    recommended_values_key="recommended_meta_description",
    diff_field="meta_description",
    detail_old_key="old_meta_description",
    detail_new_key="new_meta_description",
    wp_update_method="update_meta_description",
    success_log_pattern="Meta description updated via Playwright for post_id=%s url=%s",
    failure_log_prefix="Playwright meta update failed: ",
    dry_fn=_dry_meta,
)

_SEO_PW_META_TITLE = _SeoPlaywrightSpec(
    action_type="meta_title",
    recommended_values_key="recommended_meta_title",
    diff_field="meta_title",
    detail_old_key="old_meta_title",
    detail_new_key="new_meta_title",
    wp_update_method="update_meta_title",
    success_log_pattern="SEO title updated via Playwright for post_id=%s url=%s",
    failure_log_prefix="Playwright SEO title update failed: ",
    dry_fn=_dry_meta_title,
)


def _seo_pw_job_detail(
    spec: _SeoPlaywrightSpec, page_url: str, old: str | None, new: str, **extra: Any
) -> dict[str, Any]:
    return _detail(
        source_url=page_url,
        url=page_url,
        **{spec.detail_old_key: old, spec.detail_new_key: new},
        **extra,
    )


def _pw_job_log_context(sheet_name: str, row_index: int, page_url: str) -> str:
    url = (page_url or "").strip()
    if len(url) > 120:
        url = url[:117] + "..."
    return f"{sheet_name} row {row_index} · {url}"


def _publish_execute_row_result(
    execution_logger: ExecutionLogger | None,
    er: ExecuteRowResult,
    *,
    run_type: str = "execute",
) -> None:
    """Push structured row_result over SSE and a one-line monitor summary."""
    if execution_logger is None:
        return
    d = er.detail or {}
    url = (
        d.get("source_url")
        or d.get("url")
        or d.get("page_url")
        or d.get("image_url")
        or d.get("destination_url")
    )
    current = (
        d.get("old_title")
        or d.get("old_meta_description")
        or d.get("old_meta_title")
        or d.get("old_alt_text")
        or d.get("old_url")
    )
    updated = (
        d.get("new_title")
        or d.get("new_meta_description")
        or d.get("new_meta_title")
        or d.get("new_alt_text")
        or d.get("new_url")
        or d.get("destination_url")
    )
    execution_logger.append_row_result(
        {
            "event_type": "row_result",
            "action_type": er.action_type,
            "sheet_name": er.sheet_name,
            "row_index": er.row_index,
            "outcome": er.outcome,
            "run_type": run_type,
            "url": url or None,
            "current": current or None,
            "updated": updated or None,
            "message": er.message or None,
            "post_id": er.post_id,
            "attachment_id": er.attachment_id,
            "detail": dict(er.detail) if er.detail else None,
        }
    )
    emit_monitor_row_exec(
        er.action_type,
        er.sheet_name,
        er.row_index,
        str(er.outcome),
        er.message,
    )


async def _wp_login_and_save_state(site: SiteAccess, session_path: str) -> bool:
    """
    Launch a single browser, perform one WordPress login, save the session via
    Playwright storageState, and close the browser.  Returns True on success.

    Call this once at the start of a run; all subsequent browser contexts can
    load from *session_path* and skip the login entirely.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await launch_chromium(p, headless=should_run_headless())
        context = await browser.new_context(
            viewport={"width": 1280, "height": 1024},
            ignore_https_errors=True,
        )
        page = await context.new_page()
        page.set_default_timeout(45000)
        page.set_default_navigation_timeout(45000)

        wp = WordPressPlaywright(
            site.playwright.admin_url,
            site.playwright.username,
            site.playwright.password.get_secret_value(),
        )

        logged_in = await wp.login(page)
        if logged_in:
            await context.storage_state(path=session_path)
            logger.info("WP session state saved to %s", session_path)
        else:
            logger.error("WP pre-run login failed; session state NOT saved")

        await context.close()
        await browser.close()
    return logged_in


def create_wp_session_state(site: SiteAccess) -> str | None:
    """
    Perform a single WordPress login and persist the session via Playwright
    storageState.  Returns the path to the JSON session file, or None if login
    failed.

    The caller is responsible for deleting the file when the run finishes.
    Uses asyncio.run so it must be called from a non-async context (same as the
    rest of run_execute / _exec_redirects_301).
    """
    if not site.playwright:
        return None

    fd, session_path = tempfile.mkstemp(suffix=".json", prefix="wp_session_")
    os.close(fd)  # close the raw fd; Playwright will overwrite it

    try:
        success = asyncio.run(_wp_login_and_save_state(site, session_path))
    except RuntimeError as e:
        if "asyncio.run() cannot be called from a running event loop" in str(e):
            # Already inside an event loop — skip pre-login, each worker logs in normally
            logger.warning("create_wp_session_state: running inside event loop, skipping pre-login")
            try:
                os.unlink(session_path)
            except OSError:
                pass
            return None
        logger.error("Failed to create WP session state: %s", e)
        try:
            os.unlink(session_path)
        except OSError:
            pass
        return None
    except Exception as e:
        logger.error("Failed to create WP session state: %s", e)
        try:
            os.unlink(session_path)
        except OSError:
            pass
        return None

    if not success:
        try:
            os.unlink(session_path)
        except OSError:
            pass
        return None

    return session_path


async def _exec_seo_playwright_batch_run(
    site: SiteAccess,
    jobs: list[tuple[NormalizedRow, int, str, str, str | None]],
    spec: _SeoPlaywrightSpec,
    pause_ctrl: "Any | None" = None,
    execution_logger: "ExecutionLogger | None" = None,
    login_lock: asyncio.Lock | None = None,
    session_path: str | None = None,
    shared_page: "Any | None" = None,
) -> list[ExecuteRowResult]:
    """One browser session: login once (or reuse session_path), then run each job
    ``(row, post_id, page_url, new_value, old_value)``.

    Args:
        site: WordPress site credentials
        jobs: List of (row, post_id, page_url, new_value, old_value) tuples
        spec: Playwright batch spec (meta/meta_title update configuration)
        pause_ctrl: Optional PauseController for action-level pause checkpoints
        execution_logger: Optional ExecutionLogger to check pause state during batch
        session_path: Path to a Playwright storageState JSON file produced by a
            prior single login.  When provided the context is initialised from it
            and ``wp.login()`` is skipped entirely.
        shared_page: A pre-authenticated Playwright Page object shared across all
            task runners.  When provided the browser launch, context creation, and
            login steps are all skipped.  The context/browser are NOT closed at the
            end — the caller owns their lifetime.
    """
    if not site.playwright:
        raise ValueError("Playwright credentials not provided")

    if shared_page is not None:
        # Use the caller-managed shared page — no browser setup, no login, no teardown.
        page = shared_page
        wp = WordPressPlaywright(
            site.playwright.admin_url,
            site.playwright.username,
            site.playwright.password.get_secret_value(),
            pause_ctrl=pause_ctrl,
            execution_logger=execution_logger,
        )
        updater = getattr(wp, spec.wp_update_method)
        out: list[ExecuteRowResult] = []
        for row, pid, page_url, new_val, old_meta in jobs:
            if execution_logger is not None and execution_logger.is_paused():
                logger.info("Execution paused while processing %s", spec.action_type)
                break
            if pause_ctrl is not None:
                await pause_ctrl.wait_if_paused()
            wp.logger.clear_logs()
            job_ctx = _pw_job_log_context(row.sheet_name, row.row_index, page_url)
            result = await updater(
                page_url,
                new_val,
                page,
                post_id=pid,
                light_mode=True,
                job_context=job_ctx,
            )
            if result.get("status") == "paused":
                break
            logs = wp.logger.get_logs()
            if result.get("status") == "updated":
                er = ExecuteRowResult(
                    action_type=spec.action_type,
                    sheet_name=row.sheet_name,
                    row_index=row.row_index,
                    outcome="updated",
                    post_id=pid,
                    detail=_seo_pw_job_detail(
                        spec, page_url, old_meta, new_val, playwright_logs=logs, raw_id=pid
                    ),
                )
                _publish_execute_row_result(execution_logger, er)
                out.append(er)
            else:
                er = ExecuteRowResult(
                    action_type=spec.action_type,
                    sheet_name=row.sheet_name,
                    row_index=row.row_index,
                    outcome="failed",
                    message=result.get("error", "Unknown error"),
                    detail=_seo_pw_job_detail(
                        spec, page_url, old_meta, new_val, playwright_logs=logs
                    ),
                    post_id=pid,
                )
                _publish_execute_row_result(execution_logger, er)
                out.append(er)
        return out

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await launch_chromium(p, headless=should_run_headless())
        ctx_kwargs: dict[str, Any] = {
            "viewport": {"width": 1280, "height": 1024},
            "ignore_https_errors": True,
        }
        if session_path:
            ctx_kwargs["storage_state"] = session_path
        context = await browser.new_context(**ctx_kwargs)
        page = await context.new_page()
        page.set_default_timeout(45000)
        page.set_default_navigation_timeout(45000)

        wp = WordPressPlaywright(
            site.playwright.admin_url,
            site.playwright.username,
            site.playwright.password.get_secret_value(),
            pause_ctrl=pause_ctrl,
            execution_logger=execution_logger,
        )

        # Pause checkpoint: before login / session load
        if pause_ctrl is not None:
            await pause_ctrl.wait_if_paused()

        if session_path:
            # Verify the storageState session is still valid; fall back to login if not.
            logged_in = await wp.verify_session(page)
            if not logged_in:
                logger.warning("storageState session invalid, falling back to fresh login")
                logged_in = await wp.login(page)
        elif login_lock is not None:
            async with login_lock:
                logged_in = await wp.login(page)
                gap = _seo_playwright_login_gap_sec()
                if gap > 0:
                    await asyncio.sleep(gap)
        else:
            logged_in = await wp.login(page)

        if not logged_in:
            await context.close()
            await browser.close()
            fail_logs = wp.logger.get_logs()
            login_fail: list[ExecuteRowResult] = []
            for row, pid, page_url, new_val, old_meta in jobs:
                er = ExecuteRowResult(
                    action_type=spec.action_type,
                    sheet_name=row.sheet_name,
                    row_index=row.row_index,
                    outcome="failed",
                    message="Failed to log in to WordPress admin panel",
                    detail=_seo_pw_job_detail(
                        spec, page_url, old_meta, new_val, playwright_logs=fail_logs
                    ),
                    post_id=pid,
                )
                _publish_execute_row_result(execution_logger, er)
                login_fail.append(er)
            return login_fail

        updater = getattr(wp, spec.wp_update_method)
        out2: list[ExecuteRowResult] = []
        for row, pid, page_url, new_val, old_meta in jobs:
            # CRITICAL: Check pause before processing each job
            if execution_logger is not None and execution_logger.is_paused():
                logger.info(f"Execution paused while processing {spec.action_type}")
                break

            # Pause checkpoint: before each job/row
            if pause_ctrl is not None:
                await pause_ctrl.wait_if_paused()

            wp.logger.clear_logs()
            job_ctx = _pw_job_log_context(row.sheet_name, row.row_index, page_url)
            result = await updater(
                page_url,
                new_val,
                page,
                post_id=pid,
                light_mode=True,
                job_context=job_ctx,
            )
            if result.get("status") == "paused":
                break
            logs = wp.logger.get_logs()
            if result.get("status") == "updated":
                er = ExecuteRowResult(
                    action_type=spec.action_type,
                    sheet_name=row.sheet_name,
                    row_index=row.row_index,
                    outcome="updated",
                    post_id=pid,
                    detail=_seo_pw_job_detail(
                        spec,
                        page_url,
                        old_meta,
                        new_val,
                        playwright_logs=logs,
                        raw_id=pid,
                    ),
                )
                _publish_execute_row_result(execution_logger, er)
                out2.append(er)
            else:
                er = ExecuteRowResult(
                    action_type=spec.action_type,
                    sheet_name=row.sheet_name,
                    row_index=row.row_index,
                    outcome="failed",
                    message=result.get("error", "Unknown error"),
                    detail=_seo_pw_job_detail(
                        spec, page_url, old_meta, new_val, playwright_logs=logs
                    ),
                    post_id=pid,
                )
                _publish_execute_row_result(execution_logger, er)
                out2.append(er)

        await context.close()
        await browser.close()
        return out2


_SeoPwJob = tuple[int, NormalizedRow, int, str, str, str | None]
_CombinedSeoWork = tuple[int, str, _SeoPwJob | None, _SeoPwJob | None]


def _build_combined_seo_works(
    meta_jobs: list[_SeoPwJob], title_jobs: list[_SeoPwJob]
) -> list[_CombinedSeoWork]:
    """One work unit per post_id (meta and/or title in a single browser session)."""
    by_pid: dict[int, dict[str, Any]] = {}
    for job in meta_jobs:
        pid = job[2]
        by_pid[pid] = {"page_url": job[3], "meta": job}
    for job in title_jobs:
        pid = job[2]
        entry = by_pid.setdefault(pid, {"page_url": job[3]})
        entry["page_url"] = entry.get("page_url") or job[3]
        entry["title"] = job
    works: list[_CombinedSeoWork] = []
    for pid, entry in by_pid.items():
        works.append(
            (
                pid,
                entry.get("page_url") or "",
                entry.get("meta"),
                entry.get("title"),
            )
        )
    return works


async def _exec_seo_playwright_combined_batch_run(
    site: SiteAccess,
    works: list[_CombinedSeoWork],
    pause_ctrl: "Any | None",
    execution_logger: "ExecutionLogger | None",
    login_lock: asyncio.Lock | None,
    session_path: str | None = None,
    shared_page: "Any | None" = None,
) -> list[tuple[str, int, ExecuteRowResult]]:
    """Login once (or reuse session_path); for each post update meta description and/or SEO title.

    When *shared_page* is provided the browser launch, context creation, and login steps are
    skipped entirely.  The caller owns the page lifetime — nothing is closed here.
    """
    if not site.playwright or not works:
        return []

    updates: list[tuple[str, int, ExecuteRowResult]] = []

    if shared_page is not None:
        # Use the caller-managed shared page — no browser setup, no login, no teardown.
        page = shared_page
        wp = WordPressPlaywright(
            site.playwright.admin_url,
            site.playwright.username,
            site.playwright.password.get_secret_value(),
            pause_ctrl=pause_ctrl,
            execution_logger=execution_logger,
        )
        for _pid, page_url, meta_job, title_job in works:
            if execution_logger is not None and execution_logger.is_paused():
                break
            if pause_ctrl is not None:
                await pause_ctrl.wait_if_paused()

            if meta_job:
                idx, row, pid, url, new_v, old_v = meta_job
                wp.logger.clear_logs()
                job_ctx = _pw_job_log_context(row.sheet_name, row.row_index, url)
                result = await wp.update_meta_description(
                    url, new_v, page, post_id=pid, light_mode=True, job_context=job_ctx
                )
                logs = wp.logger.get_logs()
                if result.get("status") == "updated":
                    er = ExecuteRowResult(
                        action_type="meta",
                        sheet_name=row.sheet_name,
                        row_index=row.row_index,
                        outcome="updated",
                        post_id=pid,
                        detail=_seo_pw_job_detail(
                            _SEO_PW_META, url, old_v, new_v, playwright_logs=logs, raw_id=pid
                        ),
                    )
                    _publish_execute_row_result(execution_logger, er)
                    updates.append(("meta", idx, er))
                else:
                    er = ExecuteRowResult(
                        action_type="meta",
                        sheet_name=row.sheet_name,
                        row_index=row.row_index,
                        outcome="failed",
                        message=result.get("error", "Unknown error"),
                        post_id=pid,
                        detail=_seo_pw_job_detail(_SEO_PW_META, url, old_v, new_v, playwright_logs=logs),
                    )
                    _publish_execute_row_result(execution_logger, er)
                    updates.append(("meta", idx, er))
                if result.get("status") == "paused":
                    break

            if title_job:
                idx, row, pid, url, new_v, old_v = title_job
                wp.logger.clear_logs()
                job_ctx = _pw_job_log_context(row.sheet_name, row.row_index, url)
                result = await wp.update_meta_title(
                    url, new_v, page, post_id=pid, light_mode=True, job_context=job_ctx
                )
                logs = wp.logger.get_logs()
                if result.get("status") == "updated":
                    er = ExecuteRowResult(
                        action_type="meta_title",
                        sheet_name=row.sheet_name,
                        row_index=row.row_index,
                        outcome="updated",
                        post_id=pid,
                        detail=_seo_pw_job_detail(
                            _SEO_PW_META_TITLE, url, old_v, new_v, playwright_logs=logs, raw_id=pid
                        ),
                    )
                    _publish_execute_row_result(execution_logger, er)
                    updates.append(("meta_title", idx, er))
                else:
                    er = ExecuteRowResult(
                        action_type="meta_title",
                        sheet_name=row.sheet_name,
                        row_index=row.row_index,
                        outcome="failed",
                        message=result.get("error", "Unknown error"),
                        post_id=pid,
                        detail=_seo_pw_job_detail(_SEO_PW_META_TITLE, url, old_v, new_v, playwright_logs=logs),
                    )
                    _publish_execute_row_result(execution_logger, er)
                    updates.append(("meta_title", idx, er))
                if result.get("status") == "paused":
                    break

        return updates

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await launch_chromium(p, headless=should_run_headless())
        ctx_kwargs: dict[str, Any] = {
            "viewport": {"width": 1280, "height": 1024},
            "ignore_https_errors": True,
        }
        if session_path:
            ctx_kwargs["storage_state"] = session_path
        context = await browser.new_context(**ctx_kwargs)
        page = await context.new_page()
        page.set_default_timeout(45000)
        page.set_default_navigation_timeout(45000)

        wp = WordPressPlaywright(
            site.playwright.admin_url,
            site.playwright.username,
            site.playwright.password.get_secret_value(),
            pause_ctrl=pause_ctrl,
            execution_logger=execution_logger,
        )

        if pause_ctrl is not None:
            await pause_ctrl.wait_if_paused()

        if session_path:
            # Verify the storageState session is still valid; fall back to login if not.
            logged_in = await wp.verify_session(page)
            if not logged_in:
                logger.warning("storageState session invalid, falling back to fresh login")
                logged_in = await wp.login(page)
        elif login_lock is not None:
            async with login_lock:
                logged_in = await wp.login(page)
                gap = _seo_playwright_login_gap_sec()
                if gap > 0:
                    await asyncio.sleep(gap)
        else:
            logged_in = await wp.login(page)

        if not logged_in:
            fail_logs = wp.logger.get_logs()
            for _pid, page_url, meta_job, title_job in works:
                if meta_job:
                    idx, row, pid, url, new_v, old_v = meta_job
                    er = ExecuteRowResult(
                        action_type="meta",
                        sheet_name=row.sheet_name,
                        row_index=row.row_index,
                        outcome="failed",
                        message="Failed to log in to WordPress admin panel",
                        post_id=pid,
                        detail=_seo_pw_job_detail(
                            _SEO_PW_META, url, old_v, new_v, playwright_logs=fail_logs
                        ),
                    )
                    _publish_execute_row_result(execution_logger, er)
                    updates.append(("meta", idx, er))
                if title_job:
                    idx, row, pid, url, new_v, old_v = title_job
                    er = ExecuteRowResult(
                        action_type="meta_title",
                        sheet_name=row.sheet_name,
                        row_index=row.row_index,
                        outcome="failed",
                        message="Failed to log in to WordPress admin panel",
                        post_id=pid,
                        detail=_seo_pw_job_detail(
                            _SEO_PW_META_TITLE,
                            url,
                            old_v,
                            new_v,
                            playwright_logs=fail_logs,
                        ),
                    )
                    _publish_execute_row_result(execution_logger, er)
                    updates.append(("meta_title", idx, er))
            await context.close()
            await browser.close()
            return updates

        for _pid, page_url, meta_job, title_job in works:
            if execution_logger is not None and execution_logger.is_paused():
                break
            if pause_ctrl is not None:
                await pause_ctrl.wait_if_paused()

            if meta_job:
                idx, row, pid, url, new_v, old_v = meta_job
                wp.logger.clear_logs()
                job_ctx = _pw_job_log_context(row.sheet_name, row.row_index, url)
                result = await wp.update_meta_description(
                    url, new_v, page, post_id=pid, light_mode=True, job_context=job_ctx
                )
                logs = wp.logger.get_logs()
                if result.get("status") == "updated":
                    er = ExecuteRowResult(
                        action_type="meta",
                        sheet_name=row.sheet_name,
                        row_index=row.row_index,
                        outcome="updated",
                        post_id=pid,
                        detail=_seo_pw_job_detail(
                            _SEO_PW_META,
                            url,
                            old_v,
                            new_v,
                            playwright_logs=logs,
                            raw_id=pid,
                        ),
                    )
                    _publish_execute_row_result(execution_logger, er)
                    updates.append(("meta", idx, er))
                else:
                    er = ExecuteRowResult(
                        action_type="meta",
                        sheet_name=row.sheet_name,
                        row_index=row.row_index,
                        outcome="failed",
                        message=result.get("error", "Unknown error"),
                        post_id=pid,
                        detail=_seo_pw_job_detail(
                            _SEO_PW_META, url, old_v, new_v, playwright_logs=logs
                        ),
                    )
                    _publish_execute_row_result(execution_logger, er)
                    updates.append(("meta", idx, er))
                if result.get("status") == "paused":
                    break

            if title_job:
                idx, row, pid, url, new_v, old_v = title_job
                wp.logger.clear_logs()
                job_ctx = _pw_job_log_context(row.sheet_name, row.row_index, url)
                result = await wp.update_meta_title(
                    url, new_v, page, post_id=pid, light_mode=True, job_context=job_ctx
                )
                logs = wp.logger.get_logs()
                if result.get("status") == "updated":
                    er = ExecuteRowResult(
                        action_type="meta_title",
                        sheet_name=row.sheet_name,
                        row_index=row.row_index,
                        outcome="updated",
                        post_id=pid,
                        detail=_seo_pw_job_detail(
                            _SEO_PW_META_TITLE,
                            url,
                            old_v,
                            new_v,
                            playwright_logs=logs,
                            raw_id=pid,
                        ),
                    )
                    _publish_execute_row_result(execution_logger, er)
                    updates.append(("meta_title", idx, er))
                else:
                    er = ExecuteRowResult(
                        action_type="meta_title",
                        sheet_name=row.sheet_name,
                        row_index=row.row_index,
                        outcome="failed",
                        message=result.get("error", "Unknown error"),
                        post_id=pid,
                        detail=_seo_pw_job_detail(
                            _SEO_PW_META_TITLE,
                            url,
                            old_v,
                            new_v,
                            playwright_logs=logs,
                        ),
                    )
                    _publish_execute_row_result(execution_logger, er)
                    updates.append(("meta_title", idx, er))
                if result.get("status") == "paused":
                    break

        await context.close()
        await browser.close()

    return updates


async def _seo_playwright_combined_chunk_async(
    site: SiteAccess,
    chunk: list[_CombinedSeoWork],
    pause_ctrl: "Any | None",
    execution_logger: "ExecutionLogger | None",
    login_lock: asyncio.Lock | None,
    session_path: str | None = None,
    shared_page: "Any | None" = None,
) -> list[tuple[str, int, ExecuteRowResult]]:
    try:
        return await _exec_seo_playwright_combined_batch_run(
            site, chunk, pause_ctrl, execution_logger, login_lock, session_path, shared_page
        )
    except Exception as e:
        logger.error("Combined Playwright worker failed: %s: %s", type(e).__name__, e)
        out: list[tuple[str, int, ExecuteRowResult]] = []
        for _pid, _url, meta_job, title_job in chunk:
            if meta_job:
                idx, row, pid, url, new_v, old_v = meta_job
                er = ExecuteRowResult(
                    action_type="meta",
                    sheet_name=row.sheet_name,
                    row_index=row.row_index,
                    outcome="failed",
                    message=f"Playwright worker error: {e}",
                    post_id=pid,
                    detail=_seo_pw_job_detail(_SEO_PW_META, url, old_v, new_v),
                )
                _publish_execute_row_result(execution_logger, er)
                out.append(("meta", idx, er))
            if title_job:
                idx, row, pid, url, new_v, old_v = title_job
                er = ExecuteRowResult(
                    action_type="meta_title",
                    sheet_name=row.sheet_name,
                    row_index=row.row_index,
                    outcome="failed",
                    message=f"Playwright worker error: {e}",
                    post_id=pid,
                    detail=_seo_pw_job_detail(_SEO_PW_META_TITLE, url, old_v, new_v),
                )
                _publish_execute_row_result(execution_logger, er)
                out.append(("meta_title", idx, er))
        return out


async def _seo_playwright_combined_pool_gather_async(
    site: SiteAccess,
    buckets: list[list[_CombinedSeoWork]],
    pause_ctrl: "Any | None",
    execution_logger: "ExecutionLogger | None",
    session_path: str | None = None,
) -> list[tuple[str, int, ExecuteRowResult]]:
    # When a shared session is available workers do not need to serialise logins.
    login_lock = asyncio.Lock() if (len(buckets) > 1 and not session_path) else None
    outcomes = await asyncio.gather(
        *[
            _seo_playwright_combined_chunk_async(
                site, bucket, pause_ctrl, execution_logger, login_lock, session_path
            )
            for bucket in buckets
        ],
        return_exceptions=True,
    )
    merged: list[tuple[str, int, ExecuteRowResult]] = []
    for bucket, outcome in zip(buckets, outcomes):
        if isinstance(outcome, BaseException):
            logger.error("Combined pool worker failed: %s", outcome)
            for _pid, _url, meta_job, title_job in bucket:
                if meta_job:
                    idx, row, pid, url, new_v, old_v = meta_job
                    er = ExecuteRowResult(
                        action_type="meta",
                        sheet_name=row.sheet_name,
                        row_index=row.row_index,
                        outcome="failed",
                        message=f"Playwright pool error: {outcome}",
                        post_id=pid,
                        detail=_seo_pw_job_detail(_SEO_PW_META, url, old_v, new_v),
                    )
                    _publish_execute_row_result(execution_logger, er)
                    merged.append(("meta", idx, er))
                if title_job:
                    idx, row, pid, url, new_v, old_v = title_job
                    er = ExecuteRowResult(
                        action_type="meta_title",
                        sheet_name=row.sheet_name,
                        row_index=row.row_index,
                        outcome="failed",
                        message=f"Playwright pool error: {outcome}",
                        post_id=pid,
                        detail=_seo_pw_job_detail(_SEO_PW_META_TITLE, url, old_v, new_v),
                    )
                    _publish_execute_row_result(execution_logger, er)
                    merged.append(("meta_title", idx, er))
        else:
            merged.extend(outcome)
    return merged


def _partition_combined_works(
    works: list[_CombinedSeoWork], worker_count: int
) -> list[list[_CombinedSeoWork]]:
    buckets: list[list[_CombinedSeoWork]] = [[] for _ in range(worker_count)]
    for n, work in enumerate(works):
        buckets[n % worker_count].append(work)
    return [b for b in buckets if b]


def _exec_seo_combined_playwright_batch(
    site: SiteAccess,
    meta_rows: list[NormalizedRow],
    title_rows: list[NormalizedRow],
    seo_plugin: str | None,
    execution_logger: "ExecutionLogger | None" = None,
    session_path: str | None = None,
    shared_page: "Any | None" = None,
) -> list[ExecuteRowResult]:
    """Meta + meta_title in one Playwright pass per worker (one login per worker, or reuse session)."""
    if execution_logger is not None and execution_logger.is_paused():
        return []

    res_m, jobs_m = _seo_playwright_prepare_batch(
        site, meta_rows, _SEO_PW_META, execution_logger
    )
    res_t, jobs_t = _seo_playwright_prepare_batch(
        site, title_rows, _SEO_PW_META_TITLE, execution_logger
    )
    works = _build_combined_seo_works(jobs_m, jobs_t)

    if not works:
        return [r for r in res_m if r is not None] + [r for r in res_t if r is not None]

    configured = _seo_playwright_worker_count()
    row_units = max(len(works), len(jobs_m) + len(jobs_t), len(meta_rows) + len(title_rows))
    worker_count = _effective_seo_worker_count(row_units)
    if execution_logger is not None:
        execution_logger.log_sync(
            "meta_playwright_combined_pool",
            "pending",
            f"{len(works)} post(s), {len(jobs_m) + len(jobs_t)} Playwright job(s), "
            f"{worker_count} worker(s) (PLAYWRIGHT_SEO_WORKERS={configured}, "
            f"min {_seo_playwright_min_rows_per_worker()} rows/worker)",
        )

    pause_ctrl = _seo_playwright_pool_pause_ctrl(execution_logger)

    if worker_count <= 1:
        updates = _run_seo_playwright_pool_coroutine(
            _seo_playwright_combined_chunk_async(
                site, works, pause_ctrl, execution_logger, None, session_path, shared_page
            )
        )
    else:
        # Multi-worker path: shared_page cannot be safely used across concurrent workers.
        buckets = _partition_combined_works(works, worker_count)
        updates = _run_seo_playwright_pool_coroutine(
            _seo_playwright_combined_pool_gather_async(
                site, buckets, pause_ctrl, execution_logger, session_path
            )
        )

    for kind, idx, er in updates:
        if kind == "meta":
            res_m[idx] = er
        else:
            res_t[idx] = er

    return [r for r in res_m if r is not None] + [r for r in res_t if r is not None]


def _seo_playwright_prepare_batch(
    site: SiteAccess,
    batch: list[NormalizedRow],
    spec: _SeoPlaywrightSpec,
    execution_logger: "ExecutionLogger | None",
) -> tuple[list[ExecuteRowResult | None], list[_SeoPwJob]]:
    """Dry-run rows; return per-row results (or None) and Playwright jobs (unique post_id)."""
    n = len(batch)
    results: list[ExecuteRowResult | None] = [None] * n
    jobs: list[_SeoPwJob] = []
    seen_post_ids: set[int] = set()

    for i, row in enumerate(batch):
        if execution_logger is not None and execution_logger.is_paused():
            break

        dr = spec.dry_fn(site, row)
        v = row.values
        page_url = _s(v.get("page_url"))
        rec = _sanitize_angle_bracket_tags(_s(v.get(spec.recommended_values_key)))
        old_val: str | None = None
        for d in dr.diffs:
            if d.field == spec.diff_field:
                old_val = d.current
                break

        if dr.outcome != "change":
            results[i] = ExecuteRowResult(
                action_type=spec.action_type,
                sheet_name=row.sheet_name,
                row_index=row.row_index,
                outcome="skipped" if dr.outcome == "no_change" else "failed",
                message=dr.message,
                post_id=dr.post_id,
                detail=_seo_pw_job_detail(spec, page_url, old_val, rec),
            )
            _publish_execute_row_result(execution_logger, results[i])
            continue

        pid = dr.post_id
        if not pid:
            results[i] = ExecuteRowResult(
                action_type=spec.action_type,
                sheet_name=row.sheet_name,
                row_index=row.row_index,
                outcome="failed",
                message="Missing post id.",
                detail=_seo_pw_job_detail(spec, page_url, old_val, rec),
            )
            _publish_execute_row_result(execution_logger, results[i])
            continue

        if pid in seen_post_ids:
            results[i] = ExecuteRowResult(
                action_type=spec.action_type,
                sheet_name=row.sheet_name,
                row_index=row.row_index,
                outcome="failed",
                message="Duplicate post id in this run; only one parallel worker may update a post.",
                post_id=pid,
                detail=_seo_pw_job_detail(spec, page_url, old_val, rec),
            )
            _publish_execute_row_result(execution_logger, results[i])
            continue
        seen_post_ids.add(pid)
        jobs.append((i, row, pid, page_url, rec, old_val))

    return results, jobs


def _chunk_results_from_pw_list(
    chunk: list[_SeoPwJob],
    spec: _SeoPlaywrightSpec,
    pw_list: list[ExecuteRowResult] | None,
    *,
    error_message: str | None = None,
    execution_logger: ExecutionLogger | None = None,
) -> list[tuple[int, ExecuteRowResult]]:
    if error_message is not None:
        out_err: list[tuple[int, ExecuteRowResult]] = []
        for idx, row, pid, url, new_v, old_v in chunk:
            er = ExecuteRowResult(
                action_type=spec.action_type,
                sheet_name=row.sheet_name,
                row_index=row.row_index,
                outcome="failed",
                message=error_message,
                post_id=pid,
                detail=_seo_pw_job_detail(spec, url, old_v, new_v),
            )
            _publish_execute_row_result(execution_logger, er)
            out_err.append((idx, er))
        return out_err
    out: list[tuple[int, ExecuteRowResult]] = []
    pw_list = pw_list or []
    for k, (idx, row, pid, url, new_v, old_v) in enumerate(chunk):
        if k < len(pw_list):
            out.append((idx, pw_list[k]))
        else:
            er = ExecuteRowResult(
                action_type=spec.action_type,
                sheet_name=row.sheet_name,
                row_index=row.row_index,
                outcome="failed",
                message="Playwright worker stopped early (paused or error).",
                post_id=pid,
                detail=_seo_pw_job_detail(spec, url, old_v, new_v),
            )
            _publish_execute_row_result(execution_logger, er)
            out.append((idx, er))
    return out


async def _seo_playwright_worker_run_chunk_async(
    site: SiteAccess,
    chunk: list[_SeoPwJob],
    spec: _SeoPlaywrightSpec,
    pause_ctrl: "Any | None",
    execution_logger: "ExecutionLogger | None",
    login_lock: asyncio.Lock | None = None,
    session_path: str | None = None,
    shared_page: "Any | None" = None,
) -> list[tuple[int, ExecuteRowResult]]:
    """One browser session per worker: login once (or reuse session), then each job in chunk."""
    if not chunk:
        return []
    ordered = [(row, pid, url, new_v, old_v) for (_i, row, pid, url, new_v, old_v) in chunk]
    try:
        pw_list = await _exec_seo_playwright_batch_run(
            site,
            ordered,
            spec,
            pause_ctrl=pause_ctrl,
            execution_logger=execution_logger,
            login_lock=login_lock,
            session_path=session_path,
            shared_page=shared_page,
        )
    except Exception as e:
        logger.error(
            "Playwright worker failed for %s: %s: %s",
            spec.action_type,
            type(e).__name__,
            e,
        )
        return _chunk_results_from_pw_list(
            chunk,
            spec,
            None,
            error_message=f"Playwright worker error: {e}",
            execution_logger=execution_logger,
        )
    return _chunk_results_from_pw_list(chunk, spec, pw_list, execution_logger=execution_logger)


async def _seo_playwright_pool_gather_async(
    site: SiteAccess,
    buckets: list[list[_SeoPwJob]],
    spec: _SeoPlaywrightSpec,
    pause_ctrl: "Any | None",
    execution_logger: "ExecutionLogger | None",
    session_path: str | None = None,
) -> list[tuple[int, ExecuteRowResult]]:
    """Run all worker buckets on one asyncio loop (avoids uvloop subprocess races)."""
    # When a shared session is available workers do not need to serialise logins.
    login_lock = asyncio.Lock() if (len(buckets) > 1 and not session_path) else None
    outcomes = await asyncio.gather(
        *[
            _seo_playwright_worker_run_chunk_async(
                site, bucket, spec, pause_ctrl, execution_logger, login_lock, session_path
            )
            for bucket in buckets
        ],
        return_exceptions=True,
    )
    merged: list[tuple[int, ExecuteRowResult]] = []
    for bucket, outcome in zip(buckets, outcomes):
        if isinstance(outcome, BaseException):
            logger.error(
                "Playwright pool worker failed for %s: %s: %s",
                spec.action_type,
                type(outcome).__name__,
                outcome,
            )
            merged.extend(
                _chunk_results_from_pw_list(
                    bucket,
                    spec,
                    None,
                    error_message=f"Playwright pool error: {outcome}",
                    execution_logger=execution_logger,
                )
            )
        else:
            merged.extend(outcome)
    return merged


def _seo_playwright_pool_pause_ctrl(
    execution_logger: "ExecutionLogger | None",
) -> "Any | None":
    if execution_logger is not None and hasattr(execution_logger, "_pause_controller"):
        return execution_logger._pause_controller
    return None


def _run_seo_playwright_pool_coroutine(coro: Any) -> Any:
    """Run pool on one loop per thread (never ThreadPoolExecutor + asyncio.run).

    When a shared Playwright session is active (_shared_pw_loop is set), the coroutine is
    run on that loop via loop.run_until_complete() so that the shared Page object — which
    is bound to that loop — remains usable.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError("asyncio.run() cannot be called from a running event loop")

    # Use the shared loop when one is active (shared Playwright session in progress).
    shared_loop = _shared_pw_loop.get()
    if shared_loop is not None and not shared_loop.is_closed():
        return shared_loop.run_until_complete(coro)

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        return asyncio.run(coro)

    if loop.is_running() or loop.is_closed():
        return asyncio.run(coro)

    return loop.run_until_complete(coro)


def _partition_jobs_for_workers(
    jobs: list[_SeoPwJob], worker_count: int
) -> list[list[_SeoPwJob]]:
    """Round-robin split so each worker gets a similar number of rows."""
    buckets: list[list[_SeoPwJob]] = [[] for _ in range(worker_count)]
    for n, job in enumerate(jobs):
        buckets[n % worker_count].append(job)
    return [b for b in buckets if b]


def _exec_seo_playwright_worker_pool(
    site: SiteAccess,
    batch: list[NormalizedRow],
    seo_plugin: str | None,
    spec: _SeoPlaywrightSpec,
    execution_logger: "ExecutionLogger | None" = None,
    session_path: str | None = None,
    shared_page: "Any | None" = None,
) -> list[ExecuteRowResult]:
    """Meta / meta_title only: dry-run, then up to N parallel Playwright browsers (login once per worker)."""
    results, jobs = _seo_playwright_prepare_batch(site, batch, spec, execution_logger)

    if not jobs:
        return [r for r in results if r is not None]

    configured = _seo_playwright_worker_count()
    worker_count = _effective_seo_worker_count(len(jobs))
    if execution_logger is not None:
        execution_logger.log_sync(
            f"{spec.action_type}_playwright_pool",
            "pending",
            f"{len(jobs)} Playwright job(s), {worker_count} worker(s) "
            f"(PLAYWRIGHT_SEO_WORKERS={configured}, "
            f"min {_seo_playwright_min_rows_per_worker()} rows/worker)",
        )

    pause_ctrl = _seo_playwright_pool_pause_ctrl(execution_logger)

    if worker_count <= 1:
        chunk_results = _run_seo_playwright_pool_coroutine(
            _seo_playwright_worker_run_chunk_async(
                site, jobs, spec, pause_ctrl, execution_logger,
                session_path=session_path,
                shared_page=shared_page,
            )
        )
        for idx, er in chunk_results:
            results[idx] = er
        return [r for r in results if r is not None]

    # Multi-worker path: shared_page cannot be safely used across concurrent workers.
    buckets = _partition_jobs_for_workers(jobs, worker_count)
    paused_during_pool = (
        execution_logger is not None and execution_logger.is_paused()
    )

    merged = _run_seo_playwright_pool_coroutine(
        _seo_playwright_pool_gather_async(
            site, buckets, spec, pause_ctrl, execution_logger, session_path
        )
    )
    if execution_logger is not None and execution_logger.is_paused():
        paused_during_pool = True

    for idx, er in merged:
        results[idx] = er

    if paused_during_pool:
        for j in jobs:
            idx = j[0]
            if results[idx] is None:
                _i, row, pid, url, new_v, old_v = j
                results[idx] = ExecuteRowResult(
                    action_type=spec.action_type,
                    sheet_name=row.sheet_name,
                    row_index=row.row_index,
                    outcome="failed",
                    message="Not run: execution paused during parallel Playwright.",
                    post_id=pid,
                    detail=_seo_pw_job_detail(spec, url, old_v, new_v),
                )
                _publish_execute_row_result(execution_logger, results[idx])

    # REST fallback if asyncio unavailable in main thread only (workers use asyncio.run)
    return [r for r in results if r is not None]


def _exec_seo_consecutive_playwright_batch(
    site: SiteAccess,
    batch: list[NormalizedRow],
    seo_plugin: str | None,
    spec: _SeoPlaywrightSpec,
    execution_logger: "ExecutionLogger | None" = None,
    session_path: str | None = None,
    shared_page: "Any | None" = None,
) -> list[ExecuteRowResult]:
    """Dry-run each row; parallel Playwright workers (meta / meta_title)."""
    if execution_logger is not None and execution_logger.is_paused():
        return []

    return _exec_seo_playwright_worker_pool(
        site, batch, seo_plugin, spec, execution_logger, session_path, shared_page
    )


async def _exec_seo_playwright(
    site: SiteAccess,
    page_url: str,
    new_value: str,
    row: NormalizedRow,
    *,
    post_id: int,
    old_value: str | None,
    spec: _SeoPlaywrightSpec,
    pause_ctrl: "Any | None" = None,
    session_path: str | None = None,
    shared_page: "Any | None" = None,
) -> ExecuteRowResult:
    """Execute meta description or SEO title update using Playwright.

    When *shared_page* is provided the browser launch, context creation, and login are skipped.
    The caller owns the page lifetime — nothing is closed here.
    """
    if not site.playwright:
        raise ValueError("Playwright credentials not provided")

    base_detail = {
        "source_url": page_url,
        "url": page_url,
        spec.detail_old_key: old_value,
        spec.detail_new_key: new_value,
    }

    if shared_page is not None:
        try:
            wp = WordPressPlaywright(
                site.playwright.admin_url,
                site.playwright.username,
                site.playwright.password.get_secret_value(),
                pause_ctrl=pause_ctrl,
            )
            updater = getattr(wp, spec.wp_update_method)
            result = await updater(page_url, new_value, shared_page, post_id=post_id, light_mode=True)
            logs = wp.logger.get_logs()
            if result.get("status") == "updated":
                logger.info(spec.success_log_pattern, post_id, page_url)
                return ExecuteRowResult(
                    action_type=spec.action_type,
                    sheet_name=row.sheet_name,
                    row_index=row.row_index,
                    outcome="updated",
                    post_id=post_id,
                    detail=_detail(**base_detail, playwright_logs=logs, raw_id=post_id),
                )
            logger.error(f"{spec.failure_log_prefix}{result.get('error')}")
            return ExecuteRowResult(
                action_type=spec.action_type,
                sheet_name=row.sheet_name,
                row_index=row.row_index,
                outcome="failed",
                message=result.get("error", "Unknown error"),
                detail=_detail(**base_detail, playwright_logs=logs),
                post_id=post_id,
            )
        except Exception as e:
            logger.error(f"Playwright execution failed (shared page): {e}")
            return ExecuteRowResult(
                action_type=spec.action_type,
                sheet_name=row.sheet_name,
                row_index=row.row_index,
                outcome="failed",
                message=f"Playwright error: {str(e)}",
                post_id=post_id,
                detail=_detail(**base_detail),
            )

    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await launch_chromium(p, headless=should_run_headless())
            ctx_kwargs: dict[str, Any] = {
                "viewport": {"width": 1280, "height": 1024},
                "ignore_https_errors": True,
            }
            if session_path:
                ctx_kwargs["storage_state"] = session_path
            context = await browser.new_context(**ctx_kwargs)
            page = await context.new_page()
            page.set_default_timeout(45000)
            page.set_default_navigation_timeout(45000)

            wp = WordPressPlaywright(
                site.playwright.admin_url,
                site.playwright.username,
                site.playwright.password.get_secret_value(),
                pause_ctrl=pause_ctrl,
            )

            if session_path:
                # Verify the storageState session is still valid; fall back to login if not.
                logged_in = await wp.verify_session(page)
                if not logged_in:
                    logger.warning("storageState session invalid for redirect, falling back to fresh login")
                    logged_in = await wp.login(page)
            else:
                logged_in = await wp.login(page)

            if not logged_in:
                await context.close()
                await browser.close()
                return ExecuteRowResult(
                    action_type=spec.action_type,
                    sheet_name=row.sheet_name,
                    row_index=row.row_index,
                    outcome="failed",
                    message="Failed to log in to WordPress admin panel",
                    detail=_detail(**base_detail, playwright_logs=wp.logger.get_logs()),
                    post_id=post_id,
                )

            updater = getattr(wp, spec.wp_update_method)
            result = await updater(
                page_url,
                new_value,
                page,
                post_id=post_id,
                light_mode=True,
            )

            await context.close()
            await browser.close()

            logs = wp.logger.get_logs()

            if result.get("status") == "updated":
                logger.info(spec.success_log_pattern, post_id, page_url)
                return ExecuteRowResult(
                    action_type=spec.action_type,
                    sheet_name=row.sheet_name,
                    row_index=row.row_index,
                    outcome="updated",
                    post_id=post_id,
                    detail=_detail(**base_detail, playwright_logs=logs, raw_id=post_id),
                )
            logger.error(f"{spec.failure_log_prefix}{result.get('error')}")
            return ExecuteRowResult(
                action_type=spec.action_type,
                sheet_name=row.sheet_name,
                row_index=row.row_index,
                outcome="failed",
                message=result.get("error", "Unknown error"),
                detail=_detail(**base_detail, playwright_logs=logs),
                post_id=post_id,
            )

    except Exception as e:
        logger.error(f"Playwright execution failed: {e}")
        return ExecuteRowResult(
            action_type=spec.action_type,
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="failed",
            message=f"Playwright error: {str(e)}",
            post_id=post_id,
            detail=_detail(**base_detail),
        )


async def _exec_redirects_301_playwright(
    site: SiteAccess,
    from_url: str,
    to_url: str,
    row: NormalizedRow,
    pause_ctrl: "Any | None" = None,
    session_path: str | None = None,
    shared_page: "Any | None" = None,
) -> ExecuteRowResult:
    """Execute 301 redirect creation using Playwright.

    When *session_path* points to a Playwright storageState JSON file the
    browser context is initialised from it and the login step is skipped.

    When *shared_page* is provided the browser launch, context creation, and login are skipped
    entirely.  The caller owns the page lifetime — nothing is closed here.
    """
    if not site.playwright:
        raise ValueError("Playwright credentials not provided")

    base_detail = dict(
        source_url=from_url,
        destination_url=to_url,
    )

    if shared_page is not None:
        try:
            wp = WordPressPlaywright(
                site.playwright.admin_url,
                site.playwright.username,
                site.playwright.password.get_secret_value(),
                pause_ctrl=pause_ctrl,
            )
            from_slug = extract_slug_with_trailing_slash(from_url)
            if not from_slug:
                return ExecuteRowResult(
                    action_type="redirects_301",
                    sheet_name=row.sheet_name,
                    row_index=row.row_index,
                    outcome="failed",
                    message=f"Could not extract slug from URL: {from_url}",
                    detail=_detail(**base_detail),
                )
            result = await wp.create_301_redirect(from_slug, to_url, shared_page)
            logs = wp.logger.get_logs()
            if result.get("status") == "created":
                logger.info(f"301 redirect created via Playwright (shared page): {from_slug} → {to_url}")
                return ExecuteRowResult(
                    action_type="redirects_301",
                    sheet_name=row.sheet_name,
                    row_index=row.row_index,
                    outcome="updated",
                    detail=_detail(**base_detail, plugin="Playwright (admin UI)", playwright_logs=logs),
                )
            if result.get("status") == "skipped":
                msg = result.get("message") or "Identical redirect already exists (Playwright table check)."
                logger.info(f"301 redirect skipped (duplicate, shared page): {from_slug} → {to_url}")
                return ExecuteRowResult(
                    action_type="redirects_301",
                    sheet_name=row.sheet_name,
                    row_index=row.row_index,
                    outcome="skipped",
                    message=msg,
                    detail=_detail(
                        **base_detail,
                        plugin="Playwright (admin UI)",
                        playwright_logs=logs,
                        skip_reason=result.get("reason"),
                    ),
                )
            logger.error(f"Playwright redirect creation failed (shared page): {result.get('error')}")
            return ExecuteRowResult(
                action_type="redirects_301",
                sheet_name=row.sheet_name,
                row_index=row.row_index,
                outcome="failed",
                message=result.get("error", "Unknown error"),
                detail=_detail(**base_detail, playwright_logs=logs),
            )
        except Exception as e:
            logger.error(f"Playwright execution failed (shared page): {e}")
            return ExecuteRowResult(
                action_type="redirects_301",
                sheet_name=row.sheet_name,
                row_index=row.row_index,
                outcome="failed",
                message=f"Playwright error: {str(e)}",
                detail=_detail(**base_detail),
            )

    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await launch_chromium(p, headless=should_run_headless())
            ctx_kwargs: dict[str, Any] = {"ignore_https_errors": True}
            if session_path:
                ctx_kwargs["storage_state"] = session_path
            context = await browser.new_context(**ctx_kwargs)
            page = await context.new_page()

            wp = WordPressPlaywright(
                site.playwright.admin_url,
                site.playwright.username,
                site.playwright.password.get_secret_value(),
                pause_ctrl=pause_ctrl,
            )

            # Login or reuse session
            if session_path:
                logged_in = await wp.verify_session(page)
                if not logged_in:
                    logger.warning("storageState session invalid for 301 redirect, falling back to fresh login")
                    logged_in = await wp.login(page)
            else:
                logged_in = await wp.login(page)

            if not logged_in:
                await context.close()
                await browser.close()
                return ExecuteRowResult(
                    action_type="redirects_301",
                    sheet_name=row.sheet_name,
                    row_index=row.row_index,
                    outcome="failed",
                    message="Failed to log in to WordPress admin panel",
                    detail=_detail(**base_detail, playwright_logs=wp.logger.get_logs()),
                )

            # Extract slug with trailing slash from from_url
            from_slug = extract_slug_with_trailing_slash(from_url)

            if not from_slug:
                await context.close()
                await browser.close()
                return ExecuteRowResult(
                    action_type="redirects_301",
                    sheet_name=row.sheet_name,
                    row_index=row.row_index,
                    outcome="failed",
                    message=f"Could not extract slug from URL: {from_url}",
                    detail=_detail(**base_detail),
                )

            # Create redirect
            result = await wp.create_301_redirect(from_slug, to_url, page)

            await context.close()
            await browser.close()

            logs = wp.logger.get_logs()

            if result.get("status") == "created":
                logger.info(f"301 redirect created via Playwright: {from_slug} → {to_url}")
                return ExecuteRowResult(
                    action_type="redirects_301",
                    sheet_name=row.sheet_name,
                    row_index=row.row_index,
                    outcome="updated",
                    detail=_detail(
                        **base_detail,
                        plugin="Playwright (admin UI)",
                        playwright_logs=logs,
                    ),
                )
            if result.get("status") == "skipped":
                msg = result.get("message") or "Identical redirect already exists (Playwright table check)."
                logger.info(f"301 redirect skipped (duplicate): {from_slug} → {to_url}")
                return ExecuteRowResult(
                    action_type="redirects_301",
                    sheet_name=row.sheet_name,
                    row_index=row.row_index,
                    outcome="skipped",
                    message=msg,
                    detail=_detail(
                        **base_detail,
                        plugin="Playwright (admin UI)",
                        playwright_logs=logs,
                        skip_reason=result.get("reason"),
                    ),
                )
            logger.error(f"Playwright redirect creation failed: {result.get('error')}")
            return ExecuteRowResult(
                action_type="redirects_301",
                sheet_name=row.sheet_name,
                row_index=row.row_index,
                outcome="failed",
                message=result.get("error", "Unknown error"),
                detail=_detail(**base_detail, playwright_logs=logs),
            )

    except Exception as e:
        logger.error(f"Playwright execution failed: {e}")
        return ExecuteRowResult(
            action_type="redirects_301",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="failed",
            message=f"Playwright error: {str(e)}",
            detail=_detail(**base_detail),
        )



def run_dry_run(
    site: SiteAccess,
    grouped: dict[str, list[NormalizedRow]],
    redirect_plugin: str | None = None,
    seo_plugin: str | None = None,
    execution_logger: ExecutionLogger | None = None,
    start_from_row: int = 0,
) -> DryRunResponse:
    total = _count_grouped(grouped)
    limit = _max_run_rows()
    if total > limit:
        if execution_logger is not None:
            execution_logger.log_sync("dry_run_rejected", "error",
                f"Too many rows ({total}). Maximum is {limit} (set MAX_RUN_ROWS).")
            execution_logger.mark_complete()
        raise ValueError(f"Too many rows ({total}). Maximum is {limit} (set MAX_RUN_ROWS).")

    eid = execution_logger.execution_id if execution_logger else MONITOR_DEFAULT_ID
    token = current_monitor_execution_id.set(eid)
    try:
        emit_monitor_phase("Dry-run started", f"{total} row(s)")

        rows_out: list[DryRunRowResult] = []
        rows_iter = list(_iter_rows(grouped))
        i = max(0, start_from_row)
        if i > 0 and execution_logger is not None:
            execution_logger.log_sync("dry_run_resumed", "pending",
                f"Resuming from row {i} of {len(rows_iter)}")

        while i < len(rows_iter):
            action, row = rows_iter[i]
            try:
                if action == "on_page":
                    rows_out.append(_dry_on_page(site, row))
                elif action == "meta":
                    rows_out.append(_dry_meta(site, row))
                elif action == "meta_title":
                    rows_out.append(_dry_meta_title(site, row))
                elif action == "images":
                    rows_out.append(_dry_on_image(site, row))
                elif action == "url_cleanup":
                    rows_out.append(_dry_url_cleanup(site, row))
                elif action == "redirects_301":
                    rows_out.append(_dry_redirects_301(site, row, redirect_plugin))
            except Exception as e:
                logger.error(f"Dry-run error for {action} row {row.row_index}: {type(e).__name__}: {str(e)}")
                rows_out.append(DryRunRowResult(
                    action_type=action,
                    sheet_name=row.sheet_name,
                    row_index=row.row_index,
                    outcome="error",
                    message=f"Internal error: {str(e)}",
                ))
            dr = rows_out[-1]
            emit_monitor_row_dry(action, dr.sheet_name, dr.row_index, str(dr.outcome), dr.message)
            if execution_logger is not None:
                v = row.values
                dr_url: str | None = (
                    v.get("page_url")
                    or v.get("source_url")
                    or v.get("image_url")
                ) or None
                dr_current: str | None = None
                dr_updated: str | None = None
                if dr.diffs:
                    d0 = dr.diffs[0]
                    dr_current = d0.current
                    dr_updated = d0.proposed
                execution_logger.append_row_result({
                    "event_type": "row_result",
                    "action_type": dr.action_type,
                    "sheet_name": dr.sheet_name,
                    "row_index": dr.row_index,
                    "outcome": dr.outcome,
                    "run_type": "dry_run",
                    "url": dr_url,
                    "current": dr_current,
                    "updated": dr_updated,
                    "message": dr.message or None,
                })
            i += 1
            if execution_logger is not None:
                execution_logger.set_rows_completed(i)
                if execution_logger.is_paused():
                    execution_logger.log_sync("dry_run_paused", "warning",
                        f"Paused after row {i} of {len(rows_iter)}")
                    execution_logger.mark_complete()
                    return DryRunResponse(
                        rows_processed=len(rows_out),
                        ready_to_execute=sum(1 for r in rows_out if r.outcome == "change"),
                        blocked=sum(1 for r in rows_out if r.outcome == "blocked"),
                        errors=sum(1 for r in rows_out if r.outcome == "error"),
                        no_change=sum(1 for r in rows_out if r.outcome == "no_change"),
                        rows=rows_out,
                        paused=True,
                        rows_completed=i,
                    )

        ready = sum(1 for r in rows_out if r.outcome == "change")
        blocked = sum(1 for r in rows_out if r.outcome == "blocked")
        errors = sum(1 for r in rows_out if r.outcome == "error")
        no_change = sum(1 for r in rows_out if r.outcome == "no_change")

        emit_monitor_phase(
            "Dry-run finished",
            f"change={ready}, blocked={blocked}, error={errors}, no_change={no_change}",
        )

        return DryRunResponse(
            rows_processed=len(rows_out),
            ready_to_execute=ready,
            blocked=blocked,
            errors=errors,
            no_change=no_change,
            rows=rows_out,
        )
    finally:
        if execution_logger is not None:
            execution_logger.mark_complete()
        current_monitor_execution_id.reset(token)


def _exec_on_page(site: SiteAccess, row: NormalizedRow) -> ExecuteRowResult:
    v = row.values
    page_url = _s(v.get("page_url"))
    rec_h1 = _s(v.get("recommended_h1"))
    rec_content = _s(v.get("recommended_content"))

    dr = _dry_on_page(site, row)
    if dr.outcome == "blocked":
        return ExecuteRowResult(
            action_type="on_page",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="failed",
            message=dr.message,
            detail=_detail(url=page_url, source_url=page_url),
        )
    if dr.outcome == "no_change":
        return ExecuteRowResult(
            action_type="on_page",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="skipped",
            message="Already matches requested values.",
            post_id=dr.post_id,
            detail=_detail(url=page_url, source_url=page_url, raw_id=dr.post_id),
        )
    pid = dr.post_id
    if not pid:
        return ExecuteRowResult(
            action_type="on_page",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="failed",
            message="Missing post id.",
            detail=_detail(url=page_url, source_url=page_url),
        )
    client = rest_client(site)
    obj = client.get_post(pid)
    summ = WpRestClient.extract_summary_fields(obj)
    raw_content = summ.get("content")
    content = raw_content if isinstance(raw_content, str) else ""
    old_h1_snapshot = WpRestClient.first_h1_inner_text(content) or ""

    if (
        rec_h1
        and not rec_content
        and WpRestClient.is_effectively_empty_post_content(content)
    ):
        return ExecuteRowResult(
            action_type="on_page",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="failed",
            message=(
                "Page has no WordPress post content (empty or page-builder only). "
                "Add recommended_content in the sheet, or set the H1 in Elementor/the builder."
            ),
            post_id=pid,
            detail=_detail(
                url=page_url,
                source_url=page_url,
                raw_id=pid,
                old_title=_s(v.get("current_h1")) or old_h1_snapshot,
            ),
        )

    new_content = content
    if rec_content:
        new_content = rec_content
    if rec_h1:
        cur_h1 = WpRestClient.first_h1_inner_text(new_content)
        if cur_h1:
            # H1 tag exists, replace it
            patched, ok = WpRestClient.replace_first_h1_inner(new_content, rec_h1)
            if not ok:
                return ExecuteRowResult(
                    action_type="on_page",
                    sheet_name=row.sheet_name,
                    row_index=row.row_index,
                    outcome="failed",
                    message="No <h1> found to replace.",
                    post_id=pid,
                    detail=_detail(
                        url=page_url,
                        source_url=page_url,
                        raw_id=pid,
                        old_title=cur_h1,
                        new_title=rec_h1,
                    ),
                )
            new_content = patched
        else:
            # No H1 tag exists, create one at the beginning of content
            escaped_h1 = html.escape(rec_h1, quote=False)
            h1_html = f"<h1>{escaped_h1}</h1>"
            new_content = h1_html + "\n" + new_content

    content_arg = new_content if new_content != content else None
    if content_arg is None:
        h1_old, h1_new = _on_page_h1_detail_from_dry(dr)
        return ExecuteRowResult(
            action_type="on_page",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="skipped",
            message="No effective changes after re-fetch.",
            post_id=pid,
            detail=_detail(
                url=page_url,
                source_url=page_url,
                raw_id=pid,
                old_title=h1_old,
                new_title=h1_new,
            ),
        )

    fields_updated: list[str] = []
    if rec_h1:
        fields_updated.append("h1")
    if content_arg is not None and "content" not in fields_updated:
        # Only flag content separately if the body itself was replaced.
        if rec_content:
            fields_updated.append("content")

    try:
        out = client.update_post(pid, title=None, content=content_arg)
        logger.info(f"Updated post {pid}: content={bool(content_arg)}, response_id={out.get('id')}")
    except Exception as e:
        logger.error(f"Failed to update post {pid}: {type(e).__name__}: {str(e)}")
        h1_detail: dict[str, Any] = {}
        if rec_h1:
            h1_old, h1_new = _on_page_h1_detail_from_dry(dr)
            h1_detail["old_title"] = (
                old_h1_snapshot if h1_old is None else h1_old
            )
            h1_detail["new_title"] = rec_h1 if h1_new is None else h1_new
        return ExecuteRowResult(
            action_type="on_page",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="failed",
            message=str(e),
            post_id=pid,
            detail=_detail(
                url=page_url,
                source_url=page_url,
                raw_id=pid,
                **h1_detail,
            ),
        )
    h1_exec_detail: dict[str, Any] = {}
    if rec_h1:
        h1_exec_detail["old_title"] = old_h1_snapshot
        h1_exec_detail["new_title"] = rec_h1
    return ExecuteRowResult(
        action_type="on_page",
        sheet_name=row.sheet_name,
        row_index=row.row_index,
        outcome="updated",
        post_id=pid,
        detail=_detail(
            url=page_url,
            source_url=page_url,
            fields_updated=fields_updated or None,
            raw_id=out.get("id"),
            **h1_exec_detail,
        ),
    )


def _exec_url_cleanup(site: SiteAccess, row: NormalizedRow) -> ExecuteRowResult:
    v = row.values
    page_url = _s(v.get("page_url"))
    old_u = _s(v.get("old_url"))
    new_u = _s(v.get("new_url"))

    dr = _dry_url_cleanup(site, row)
    if dr.outcome != "change":
        return ExecuteRowResult(
            action_type="url_cleanup",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="skipped" if dr.outcome == "no_change" else "failed",
            message=dr.message,
            post_id=dr.post_id,
            detail=_detail(
                page_url=page_url,
                url=page_url,
                old_url=old_u,
                new_url=new_u,
                raw_id=dr.post_id,
            ),
        )
    pid = dr.post_id
    if not pid:
        return ExecuteRowResult(
            action_type="url_cleanup",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="failed",
            message="Missing post id.",
            detail=_detail(
                page_url=page_url,
                url=page_url,
                old_url=old_u,
                new_url=new_u,
            ),
        )
    client = rest_client(site)
    obj = client.get_post(pid)
    raw = WpRestClient.extract_summary_fields(obj).get("content")
    content = raw if isinstance(raw, str) else ""

    # Replace URLs with regex to handle variations
    escaped_url = re.escape(old_u)
    patterns = [
        (escaped_url, new_u),
        (escaped_url.rstrip("/") + "/?", new_u),
        (escaped_url.replace("http://", "https?://"), new_u),
    ]

    new_content = content
    replacements_made = 0
    for pattern, replacement in patterns:
        try:
            new_new_content, count = re.subn(pattern, replacement, new_content)
            if count:
                replacements_made += count
                new_content = new_new_content
        except Exception:
            pass

    if new_content == content:
        # No replacements made - URL not found, but that's OK, just skip
        return ExecuteRowResult(
            action_type="url_cleanup",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="skipped",
            message="URL not found in content (already clean or in meta fields).",
            post_id=pid,
            detail=_detail(
                page_url=page_url,
                url=page_url,
                old_url=old_u,
                new_url=new_u,
                raw_id=pid,
            ),
        )
    try:
        result = client.update_post(pid, title=None, content=new_content)
        logger.info(f"Updated post {pid} URL cleanup (content replaced), response_id={result.get('id')}")
    except Exception as e:
        logger.error(f"Failed to update post {pid} for URL cleanup: {type(e).__name__}: {str(e)}")
        return ExecuteRowResult(
            action_type="url_cleanup",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="failed",
            message=str(e),
            post_id=pid,
            detail=_detail(
                page_url=page_url,
                url=page_url,
                old_url=old_u,
                new_url=new_u,
                raw_id=pid,
            ),
        )
    return ExecuteRowResult(
        action_type="url_cleanup",
        sheet_name=row.sheet_name,
        row_index=row.row_index,
        outcome="updated",
        post_id=pid,
        detail=_detail(
            page_url=page_url,
            url=page_url,
            old_url=old_u,
            new_url=new_u,
            replacements=replacements_made or None,
            raw_id=result.get("id"),
        ),
    )


def _exec_on_image(site: SiteAccess, row: NormalizedRow) -> ExecuteRowResult:
    v = row.values
    image_url = _s(v.get("image_url"))
    rec_alt = _s(v.get("recommended_alt_text"))

    dr = _dry_on_image(site, row)
    # Pull the current alt from the dry-run diff (when available) so we can
    # show "old → new" without re-fetching the media object.
    cur_alt: str | None = None
    for d in dr.diffs:
        if d.field == "alt_text":
            cur_alt = d.current
            break

    if dr.outcome != "change":
        return ExecuteRowResult(
            action_type="images",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="skipped" if dr.outcome == "no_change" else "failed",
            message=dr.message,
            detail=_detail(
                image_url=image_url,
                old_alt_text=cur_alt,
                new_alt_text=rec_alt,
            ),
        )

    client = rest_client(site)
    try:
        media_id = client.search_media_by_url(image_url)
    except Exception as e:
        return ExecuteRowResult(
            action_type="images",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="failed",
            message=f"Media search failed: {e}",
            detail=_detail(
                image_url=image_url,
                old_alt_text=cur_alt,
                new_alt_text=rec_alt,
            ),
        )

    if not media_id:
        return ExecuteRowResult(
            action_type="images",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="failed",
            message=f"Could not find media ID for image URL: {image_url}",
            detail=_detail(
                image_url=image_url,
                old_alt_text=cur_alt,
                new_alt_text=rec_alt,
            ),
        )

    try:
        result = client.update_media_alt_text(media_id, alt_text=rec_alt)
        logger.info(f"Updated media {media_id} alt text: {rec_alt}, response_id={result.get('id')}")
    except Exception as e:
        logger.error(f"Failed to update media {media_id}: {type(e).__name__}: {str(e)}")
        return ExecuteRowResult(
            action_type="images",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="failed",
            message=str(e),
            attachment_id=media_id,
            detail=_detail(
                image_url=image_url,
                old_alt_text=cur_alt,
                new_alt_text=rec_alt,
                media_id=media_id,
            ),
        )

    return ExecuteRowResult(
        action_type="images",
        sheet_name=row.sheet_name,
        row_index=row.row_index,
        outcome="updated",
        attachment_id=media_id,
        detail=_detail(
            image_url=image_url,
            old_alt_text=cur_alt,
            new_alt_text=rec_alt,
            media_id=media_id,
            raw_id=result.get("id"),
        ),
    )


def _exec_meta(site: SiteAccess, row: NormalizedRow, seo_plugin: str | None = None, session_path: str | None = None, shared_page: "Any | None" = None) -> ExecuteRowResult:
    """Execute meta description update via Playwright or REST API."""
    v = row.values
    page_url = _s(v.get("page_url"))
    rec_meta = _sanitize_angle_bracket_tags(_s(v.get("recommended_meta_description")))

    dr = _dry_meta(site, row)
    old_meta: str | None = None
    for d in dr.diffs:
        if d.field == "meta_description":
            old_meta = d.current
            break

    if dr.outcome != "change":
        return ExecuteRowResult(
            action_type="meta",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="skipped" if dr.outcome == "no_change" else "failed",
            message=dr.message,
            post_id=dr.post_id,
            detail=_detail(
                source_url=page_url,
                url=page_url,
                old_meta_description=old_meta,
                new_meta_description=rec_meta,
            ),
        )

    pid = dr.post_id
    if not pid:
        return ExecuteRowResult(
            action_type="meta",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="failed",
            message="Missing post id.",
            detail=_detail(
                source_url=page_url,
                url=page_url,
                old_meta_description=old_meta,
                new_meta_description=rec_meta,
            ),
        )

    if not site.playwright:
        return ExecuteRowResult(
            action_type="meta",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="failed",
            message="Playwright credentials not configured.",
            post_id=pid,
            detail=_detail(source_url=page_url, url=page_url, old_meta_description=old_meta, new_meta_description=rec_meta),
        )

    try:
        logger.info(f"Using Playwright to update meta description for {page_url}")
        coro = _exec_seo_playwright(
            site,
            page_url,
            rec_meta,
            row,
            post_id=pid,
            old_value=old_meta,
            spec=_SEO_PW_META,
            session_path=session_path,
            shared_page=shared_page,
        )
        if shared_page is not None:
            shared_loop = _shared_pw_loop.get()
            if shared_loop is not None and not shared_loop.is_closed():
                return shared_loop.run_until_complete(coro)
            raise RuntimeError("shared_page is set but _shared_pw_loop is None or closed")
        return asyncio.run(coro)
    except Exception as e:
        logger.error(f"Playwright meta update failed: {e}")
        return ExecuteRowResult(
            action_type="meta",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="failed",
            message=f"Playwright meta update failed: {e}",
            post_id=pid,
            detail=_detail(source_url=page_url, url=page_url, old_meta_description=old_meta, new_meta_description=rec_meta),
        )


def _exec_meta_title(site: SiteAccess, row: NormalizedRow, seo_plugin: str | None = None, session_path: str | None = None, shared_page: "Any | None" = None) -> ExecuteRowResult:
    """Execute SEO title update via Playwright or REST API."""
    v = row.values
    page_url = _s(v.get("page_url"))
    rec_title = _sanitize_angle_bracket_tags(_s(v.get("recommended_meta_title")))

    dr = _dry_meta_title(site, row)
    old_title: str | None = None
    for d in dr.diffs:
        if d.field == "meta_title":
            old_title = d.current
            break

    if dr.outcome != "change":
        return ExecuteRowResult(
            action_type="meta_title",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="skipped" if dr.outcome == "no_change" else "failed",
            message=dr.message,
            post_id=dr.post_id,
            detail=_detail(
                source_url=page_url,
                url=page_url,
                old_meta_title=old_title,
                new_meta_title=rec_title,
            ),
        )

    pid = dr.post_id
    if not pid:
        return ExecuteRowResult(
            action_type="meta_title",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="failed",
            message="Missing post id.",
            detail=_detail(
                source_url=page_url,
                url=page_url,
                old_meta_title=old_title,
                new_meta_title=rec_title,
            ),
        )

    if not site.playwright:
        return ExecuteRowResult(
            action_type="meta_title",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="failed",
            message="Playwright credentials not configured.",
            post_id=pid,
            detail=_detail(source_url=page_url, url=page_url, old_meta_title=old_title, new_meta_title=rec_title),
        )

    try:
        logger.info(f"Using Playwright to update SEO title for {page_url}")
        coro = _exec_seo_playwright(
            site,
            page_url,
            rec_title,
            row,
            post_id=pid,
            old_value=old_title,
            spec=_SEO_PW_META_TITLE,
            session_path=session_path,
            shared_page=shared_page,
        )
        if shared_page is not None:
            shared_loop = _shared_pw_loop.get()
            if shared_loop is not None and not shared_loop.is_closed():
                return shared_loop.run_until_complete(coro)
            raise RuntimeError("shared_page is set but _shared_pw_loop is None or closed")
        return asyncio.run(coro)
    except Exception as e:
        logger.error(f"Playwright SEO title update failed: {e}")
        return ExecuteRowResult(
            action_type="meta_title",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="failed",
            message=f"Playwright SEO title update failed: {e}",
            post_id=pid,
            detail=_detail(source_url=page_url, url=page_url, old_meta_title=old_title, new_meta_title=rec_title),
        )


def _exec_redirects_301(site: SiteAccess, row: NormalizedRow, redirect_plugin: str | None = None, session_path: str | None = None, shared_page: "Any | None" = None) -> ExecuteRowResult:
    """Execute 301 redirect: REST API for capable plugins; Playwright for UI-only (e.g. EPS 301 Redirects)."""
    v = row.values
    from_url = _s(v.get("source_url"))
    to_url = _s(v.get("target_url"))
    base_detail = dict(source_url=from_url, destination_url=to_url)

    dr = _dry_redirects_301(site, row, redirect_plugin)
    if dr.outcome != "change":
        return ExecuteRowResult(
            action_type="redirects_301",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="skipped" if dr.outcome == "no_change" else "failed",
            message=dr.message,
            detail=_detail(**base_detail),
        )

    rest_backends = _redirect_rest_backends(redirect_plugin)
    plugin_explicit = bool((redirect_plugin or "").strip())
    client = rest_client(site)

    result: dict[str, Any] | None = None
    if rest_backends:
        try:
            result = client.create_redirect(from_url, to_url, redirect_plugin)
        except Exception as e:
            logger.error(f"REST redirect creation raised: {type(e).__name__}: {str(e)}")
            result = {"status": "failed", "message": str(e)}

        if isinstance(result, dict) and result.get("status") == "created":
            redirect_id = result.get("id")
            plugin_used = result.get("plugin", "Unknown")
            logger.info(f"Created redirect {from_url} → {to_url} via {plugin_used}, id={redirect_id}")
            return ExecuteRowResult(
                action_type="redirects_301",
                sheet_name=row.sheet_name,
                row_index=row.row_index,
                outcome="updated",
                detail=_detail(
                    **base_detail,
                    plugin=plugin_used,
                    redirect_id=redirect_id,
                ),
            )

        if plugin_explicit:
            msg = (result or {}).get("message") or (result or {}).get("error") or "REST redirect creation failed"
            return ExecuteRowResult(
                action_type="redirects_301",
                sheet_name=row.sheet_name,
                row_index=row.row_index,
                outcome="failed",
                message=str(msg),
                detail=_detail(**base_detail, plugin=redirect_plugin),
            )

    if site.playwright:
        try:
            logger.info(f"Using Playwright to create 301 redirect: {from_url} → {to_url}")
            try:
                coro = _exec_redirects_301_playwright(
                    site, from_url, to_url, row,
                    session_path=session_path,
                    shared_page=shared_page,
                )
                # Use the shared event loop when one is active so the shared Page
                # object (bound to that loop) remains accessible.
                shared_loop = _shared_pw_loop.get()
                if shared_loop is not None and not shared_loop.is_closed():
                    pw_result = shared_loop.run_until_complete(coro)
                else:
                    pw_result = asyncio.run(coro)
                return pw_result
            except RuntimeError as e:
                if "asyncio.run() cannot be called from a running event loop" in str(e):
                    logger.warning("Sync context only - Playwright UI automation not available in async context")
                else:
                    raise
        except Exception as e:
            logger.error(f"Playwright redirect creation failed: {e}")
            return ExecuteRowResult(
                action_type="redirects_301",
                sheet_name=row.sheet_name,
                row_index=row.row_index,
                outcome="failed",
                message=str(e),
                detail=_detail(**base_detail),
            )

    if not rest_backends:
        return ExecuteRowResult(
            action_type="redirects_301",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="failed",
            message="Selected redirect plugin has no REST API. Add WP admin password and Admin panel URL for UI automation.",
            detail=_detail(**base_detail, plugin=redirect_plugin),
        )

    msg = (result or {}).get("message") or (result or {}).get("error") or "Redirect creation failed"
    return ExecuteRowResult(
        action_type="redirects_301",
        sheet_name=row.sheet_name,
        row_index=row.row_index,
        outcome="failed",
        message=str(msg),
        detail=_detail(**base_detail, plugin=redirect_plugin),
    )



def run_execute(
    site: SiteAccess,
    grouped: dict[str, list[NormalizedRow]],
    redirect_plugin: str | None = None,
    seo_plugin: str | None = None,
    execution_logger: ExecutionLogger | None = None,
    start_from_row: int = 0,
) -> ExecuteResponse:
    total = _count_grouped(grouped)
    limit = _max_run_rows()
    if total > limit:
        if execution_logger is not None:
            execution_logger.log_sync(
                "execute_rejected",
                "error",
                f"Too many rows ({total}). Maximum is {limit} (set MAX_RUN_ROWS).",
            )
            execution_logger.mark_complete()
        raise ValueError(f"Too many rows ({total}). Maximum is {limit} (set MAX_RUN_ROWS).")

    eid = execution_logger.execution_id if execution_logger else MONITOR_DEFAULT_ID
    token = current_monitor_execution_id.set(eid)
    try:

        def _log_row(er: ExecuteRowResult) -> None:
            _publish_execute_row_result(execution_logger, er)

        emit_monitor_phase("Execute started", f"{total} row(s)")

        # ------------------------------------------------------------------
        # One-time Playwright login: launch ONE browser, ONE context, ONE page
        # and reuse that single Page object for ALL tasks (meta, meta_title,
        # redirects_301) sequentially.  All async operations run on a single
        # persistent event loop so the Page object — which is loop-bound — is
        # safely accessible from every task runner.
        #
        # The storageState approach (_wp_session_path) is kept as a fallback
        # in case the shared-page path fails.
        # ------------------------------------------------------------------
        # Clear any stale shared-loop reference left by a prior run on this thread.
        _stale_loop = _shared_pw_loop.get()
        if _stale_loop is not None and _stale_loop.is_closed():
            _shared_pw_loop.set(None)

        rows_iter_peek = list(_iter_rows(grouped))
        _needs_playwright = site.playwright and any(
            action in ("meta", "meta_title", "redirects_301")
            for action, _ in rows_iter_peek
        )
        _wp_session_path: str | None = None
        _shared_page: Any = None
        _shared_browser: Any = None
        _shared_context: Any = None
        _shared_pw_instance: Any = None
        _shared_loop: asyncio.AbstractEventLoop | None = None
        _loop_token: Any = None

        if _needs_playwright:
            async def _init_shared_session() -> tuple[Any, Any, Any, Any, bool]:
                from playwright.async_api import async_playwright as _async_playwright
                pw = await _async_playwright().__aenter__()
                browser = await launch_chromium(pw, headless=should_run_headless())
                context = await browser.new_context(
                    viewport={"width": 1280, "height": 1024},
                    ignore_https_errors=True,
                )
                page = await context.new_page()
                page.set_default_timeout(45000)
                page.set_default_navigation_timeout(45000)
                wp_helper = WordPressPlaywright(
                    site.playwright.admin_url,  # type: ignore[union-attr]
                    site.playwright.username,   # type: ignore[union-attr]
                    site.playwright.password.get_secret_value(),  # type: ignore[union-attr]
                )
                logged_in = await wp_helper.login(page)
                return pw, browser, context, page, logged_in

            try:
                _shared_loop = asyncio.new_event_loop()
                (
                    _shared_pw_instance,
                    _shared_browser,
                    _shared_context,
                    _shared_page,
                    _logged_in,
                ) = _shared_loop.run_until_complete(_init_shared_session())
                if _logged_in:
                    # Register the shared loop so all async-from-sync bridges use it.
                    _loop_token = _shared_pw_loop.set(_shared_loop)
                    logger.info("Shared Playwright session ready (single browser, single login)")
                else:
                    logger.warning(
                        "Shared Playwright session login failed; falling back to storageState approach"
                    )
                    _shared_page = None
                    # Close the failed shared session resources.
                    async def _close_failed() -> None:
                        await _shared_context.close()
                        await _shared_browser.close()
                        await _shared_pw_instance.__aexit__(None, None, None)
                    try:
                        _shared_loop.run_until_complete(_close_failed())
                    except Exception:
                        pass
                    finally:
                        _shared_loop.close()
                        _shared_loop = None
                    _shared_browser = None
                    _shared_context = None
                    _shared_pw_instance = None
            except Exception as _init_err:
                logger.warning(
                    "Failed to create shared Playwright session (%s); falling back to storageState approach",
                    _init_err,
                )
                _shared_page = None
                if _shared_loop is not None and not _shared_loop.is_closed():
                    _shared_loop.close()
                _shared_loop = None
                _shared_browser = None
                _shared_context = None
                _shared_pw_instance = None

            # Fallback: storageState session (used when shared_page is None).
            if _shared_page is None:
                try:
                    _wp_session_path = create_wp_session_state(site)
                    if _wp_session_path:
                        logger.info("Shared WP session state ready at %s", _wp_session_path)
                    else:
                        logger.warning(
                            "Pre-run WP login failed; each Playwright worker will log in independently"
                        )
                except Exception as _pre_err:
                    logger.warning(
                        "Pre-run WP session creation raised %s; workers will log in independently",
                        _pre_err,
                    )
                    _wp_session_path = None

        rows_out: list[ExecuteRowResult] = []
        try:
            rows_iter = rows_iter_peek
            i = max(0, start_from_row)
            if i > 0 and execution_logger is not None:
                execution_logger.log_sync(
                    "execute_resumed",
                    "pending",
                    f"Resuming from row {i} of {len(rows_iter)}",
                )
            while i < len(rows_iter):
                action, row = rows_iter[i]
                try:
                    pw_batch_results: list[ExecuteRowResult] | None = None
                    pw_next_i: int | None = None
                    pw_pause_label = action

                    if action == "meta" and site.playwright:
                        block_meta: list[NormalizedRow] = []
                        j = i
                        while j < len(rows_iter) and rows_iter[j][0] == "meta":
                            block_meta.append(rows_iter[j][1])
                            j += 1
                        block_title: list[NormalizedRow] = []
                        if j < len(rows_iter) and rows_iter[j][0] == "meta_title":
                            while j < len(rows_iter) and rows_iter[j][0] == "meta_title":
                                block_title.append(rows_iter[j][1])
                                j += 1
                        if block_title:
                            pw_batch_results = _exec_seo_combined_playwright_batch(
                                site,
                                block_meta,
                                block_title,
                                seo_plugin,
                                execution_logger,
                                _wp_session_path,
                                _shared_page,
                            )
                            pw_pause_label = "meta+meta_title"
                        else:
                            pw_batch_results = _exec_seo_consecutive_playwright_batch(
                                site,
                                block_meta,
                                seo_plugin,
                                _SEO_PW_META,
                                execution_logger,
                                _wp_session_path,
                                _shared_page,
                            )
                        pw_next_i = j
                    elif action == "meta_title" and site.playwright:
                        block_rows: list[NormalizedRow] = []
                        j = i
                        while j < len(rows_iter) and rows_iter[j][0] == "meta_title":
                            block_rows.append(rows_iter[j][1])
                            j += 1
                        pw_batch_results = _exec_seo_consecutive_playwright_batch(
                            site,
                            block_rows,
                            seo_plugin,
                            _SEO_PW_META_TITLE,
                            execution_logger,
                            _wp_session_path,
                            _shared_page,
                        )
                        pw_next_i = j

                    if pw_batch_results is not None and pw_next_i is not None:
                        if (
                            not pw_batch_results
                            and execution_logger is not None
                            and execution_logger.is_paused()
                        ):
                            execution_logger.log_sync(
                                "execute_paused",
                                "warning",
                                f"Paused during {pw_pause_label} Playwright block at row {i + 1}",
                            )
                            execution_logger.mark_complete()
                            return ExecuteResponse(
                                rows_processed=len(rows_out),
                                updated=sum(1 for r in rows_out if r.outcome == "updated"),
                                skipped=sum(1 for r in rows_out if r.outcome == "skipped"),
                                failed=sum(1 for r in rows_out if r.outcome == "failed"),
                                rows=rows_out,
                                paused=True,
                                rows_completed=i,
                            )
                        for er in pw_batch_results:
                            rows_out.append(er)
                        i = pw_next_i
                        if execution_logger is not None:
                            execution_logger.set_rows_completed(i)
                            if execution_logger.is_paused():
                                execution_logger.log_sync(
                                    "execute_paused",
                                    "warning",
                                    f"Paused after row {i} of {len(rows_iter)}",
                                )
                                execution_logger.mark_complete()
                                return ExecuteResponse(
                                    rows_processed=len(rows_out),
                                    updated=sum(1 for r in rows_out if r.outcome == "updated"),
                                    skipped=sum(1 for r in rows_out if r.outcome == "skipped"),
                                    failed=sum(1 for r in rows_out if r.outcome == "failed"),
                                    rows=rows_out,
                                    paused=True,
                                    rows_completed=i,
                                )
                        continue

                    if action == "on_page":
                        rows_out.append(_exec_on_page(site, row))
                    elif action == "meta":
                        rows_out.append(_exec_meta(site, row, seo_plugin, _wp_session_path, _shared_page))
                    elif action == "meta_title":
                        rows_out.append(_exec_meta_title(site, row, seo_plugin, _wp_session_path, _shared_page))
                    elif action == "images":
                        rows_out.append(_exec_on_image(site, row))
                    elif action == "url_cleanup":
                        rows_out.append(_exec_url_cleanup(site, row))
                    elif action == "redirects_301":
                        rows_out.append(_exec_redirects_301(site, row, redirect_plugin, _wp_session_path, _shared_page))
                except Exception as e:
                    logger.error(
                        f"Execute error for {action} row {row.row_index}: {type(e).__name__}: {str(e)}"
                    )
                    rows_out.append(
                        ExecuteRowResult(
                            action_type=action,
                            sheet_name=row.sheet_name,
                            row_index=row.row_index,
                            outcome="failed",
                            message=f"Internal error: {str(e)}",
                        )
                    )
                er = rows_out[-1]
                emit_monitor_row_exec(
                    action,
                    er.sheet_name,
                    er.row_index,
                    str(er.outcome),
                    er.message,
                )
                _log_row(er)
                i += 1
                if execution_logger is not None:
                    execution_logger.set_rows_completed(i)
                    if execution_logger.is_paused():
                        execution_logger.log_sync(
                            "execute_paused",
                            "warning",
                            f"Paused after row {i} of {len(rows_iter)}",
                        )
                        execution_logger.mark_complete()
                        return ExecuteResponse(
                            rows_processed=len(rows_out),
                            updated=sum(1 for r in rows_out if r.outcome == "updated"),
                            skipped=sum(1 for r in rows_out if r.outcome == "skipped"),
                            failed=sum(1 for r in rows_out if r.outcome == "failed"),
                            rows=rows_out,
                            paused=True,
                            rows_completed=i,
                        )

            updated = sum(1 for r in rows_out if r.outcome == "updated")
            skipped = sum(1 for r in rows_out if r.outcome == "skipped")
            failed = sum(1 for r in rows_out if r.outcome == "failed")

            emit_monitor_phase(
                "Execute finished",
                f"updated={updated}, skipped={skipped}, failed={failed}",
            )

            return ExecuteResponse(
                rows_processed=len(rows_out),
                updated=updated,
                skipped=skipped,
                failed=failed,
                rows=rows_out,
            )
        finally:
            if execution_logger is not None:
                execution_logger.mark_complete()
            # Clean up the shared Playwright session (browser, context, playwright instance).
            if _shared_loop is not None and not _shared_loop.is_closed():
                async def _close_shared_session() -> None:
                    if _shared_context is not None:
                        try:
                            await _shared_context.close()
                        except Exception:
                            pass
                    if _shared_browser is not None:
                        try:
                            await _shared_browser.close()
                        except Exception:
                            pass
                    if _shared_pw_instance is not None:
                        try:
                            await _shared_pw_instance.__aexit__(None, None, None)
                        except Exception:
                            pass
                try:
                    _shared_loop.run_until_complete(_close_shared_session())
                except Exception as _close_err:
                    logger.debug("Error closing shared Playwright session: %s", _close_err)
                finally:
                    try:
                        _shared_loop.close()
                    except Exception:
                        pass
                    logger.debug("Shared Playwright browser session closed")
            # Reset the shared-loop context variable.
            if _loop_token is not None:
                _shared_pw_loop.reset(_loop_token)
            # Clean up the storageState fallback file if it was used.
            if _wp_session_path:
                try:
                    os.unlink(_wp_session_path)
                    logger.debug("Removed WP session state file %s", _wp_session_path)
                except OSError:
                    pass
    finally:
        current_monitor_execution_id.reset(token)
