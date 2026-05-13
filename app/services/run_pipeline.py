from __future__ import annotations

import asyncio
import html
import logging
import os
import re
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
    clear_monitor_logs,
    emit_monitor_phase,
    emit_monitor_row_dry,
    emit_monitor_row_exec,
)
from app.services.monitor_context import current_monitor_execution_id
from app.services.execution_logger import ExecutionLogger

logger = logging.getLogger(__name__)

_ORDER = ("on_page", "meta", "meta_title", "images", "url_cleanup", "redirects_301")


def _max_run_rows() -> int:
    raw = os.getenv("MAX_RUN_ROWS", "500")
    try:
        return max(1, int(raw))
    except Exception:
        return 500


def _s(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip()


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

    # Second check: verify Excel current matches WordPress current
    if current_meta_from_sheet and wp_cur_meta and _normalize_for_comparison(current_meta_from_sheet) != _normalize_for_comparison(wp_cur_meta):
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

    # Second check: verify Excel current matches WordPress current
    if current_title_from_sheet and wp_cur_title and _normalize_for_comparison(current_title_from_sheet) != _normalize_for_comparison(wp_cur_title):
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



def _seo_meta_pw_rest_fallback(
    site: SiteAccess,
    row: NormalizedRow,
    pid: int,
    value: str,
    seo_plugin: str | None,
    page_url: str,
    old_value: str | None,
) -> ExecuteRowResult:
    return _exec_meta_via_rest(
        site, row, pid, value, seo_plugin, page_url=page_url, old_meta=old_value
    )


def _seo_meta_title_pw_rest_fallback(
    site: SiteAccess,
    row: NormalizedRow,
    pid: int,
    value: str,
    seo_plugin: str | None,
    page_url: str,
    old_value: str | None,
) -> ExecuteRowResult:
    return _exec_meta_title_via_rest(
        site, row, pid, value, seo_plugin, page_url=page_url, old_title=old_value
    )


@dataclass(frozen=True)
class _SeoPlaywrightSpec:
    action_type: str
    recommended_values_key: str
    diff_field: str
    detail_old_key: str
    detail_new_key: str
    wp_update_method: str
    asyncio_batch_fallback_log: str
    success_log_pattern: str
    failure_log_prefix: str
    dry_fn: Callable[[SiteAccess, NormalizedRow], DryRunRowResult]
    rest_fallback: Callable[
        [SiteAccess, NormalizedRow, int, str, str | None, str, str | None],
        ExecuteRowResult,
    ]


_SEO_PW_META = _SeoPlaywrightSpec(
    action_type="meta",
    recommended_values_key="recommended_meta_description",
    diff_field="meta_description",
    detail_old_key="old_meta_description",
    detail_new_key="new_meta_description",
    wp_update_method="update_meta_description",
    asyncio_batch_fallback_log="asyncio.run unavailable for meta batch; using REST per row",
    success_log_pattern="Meta description updated via Playwright for post_id=%s url=%s",
    failure_log_prefix="Playwright meta update failed: ",
    dry_fn=_dry_meta,
    rest_fallback=_seo_meta_pw_rest_fallback,
)

_SEO_PW_META_TITLE = _SeoPlaywrightSpec(
    action_type="meta_title",
    recommended_values_key="recommended_meta_title",
    diff_field="meta_title",
    detail_old_key="old_meta_title",
    detail_new_key="new_meta_title",
    wp_update_method="update_meta_title",
    asyncio_batch_fallback_log="asyncio.run unavailable for meta_title batch; using REST per row",
    success_log_pattern="SEO title updated via Playwright for post_id=%s url=%s",
    failure_log_prefix="Playwright SEO title update failed: ",
    dry_fn=_dry_meta_title,
    rest_fallback=_seo_meta_title_pw_rest_fallback,
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


async def _exec_seo_playwright_batch_run(
    site: SiteAccess,
    jobs: list[tuple[NormalizedRow, int, str, str, str | None]],
    spec: _SeoPlaywrightSpec,
) -> list[ExecuteRowResult]:
    """One browser session: login once, then run each job ``(row, post_id, page_url, new_value, old_value)``."""
    if not site.playwright:
        raise ValueError("Playwright credentials not provided")

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

        if not await wp.login(page):
            await context.close()
            await browser.close()
            fail_logs = wp.logger.get_logs()
            return [
                ExecuteRowResult(
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
                for row, pid, page_url, new_val, old_meta in jobs
            ]

        updater = getattr(wp, spec.wp_update_method)
        out: list[ExecuteRowResult] = []
        for row, pid, page_url, new_val, old_meta in jobs:
            wp.logger.clear_logs()
            result = await updater(
                page_url,
                new_val,
                page,
                post_id=pid,
                light_mode=True,
            )
            logs = wp.logger.get_logs()
            if result.get("status") == "updated":
                out.append(
                    ExecuteRowResult(
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
                )
            else:
                out.append(
                    ExecuteRowResult(
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
                )

        await context.close()
        await browser.close()
        return out


def _exec_seo_consecutive_playwright_batch(
    site: SiteAccess,
    batch: list[NormalizedRow],
    seo_plugin: str | None,
    spec: _SeoPlaywrightSpec,
) -> list[ExecuteRowResult]:
    """Dry-run each row; run one Playwright session for all rows that need a change."""
    n = len(batch)
    results: list[ExecuteRowResult | None] = [None] * n
    jobs: list[tuple[int, NormalizedRow, int, str, str, str | None]] = []

    for i, row in enumerate(batch):
        dr = spec.dry_fn(site, row)
        v = row.values
        page_url = _s(v.get("page_url"))
        rec = _s(v.get(spec.recommended_values_key))
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
            continue

        jobs.append((i, row, pid, page_url, rec, old_val))

    if jobs:
        ordered = [(row, pid, url, new_v, old_v) for (_i, row, pid, url, new_v, old_v) in jobs]
        try:
            pw_list = asyncio.run(_exec_seo_playwright_batch_run(site, ordered, spec))
        except RuntimeError as e:
            if "asyncio.run() cannot be called from a running event loop" in str(e):
                logger.warning(spec.asyncio_batch_fallback_log)
                for idx, row, pid, url, new_v, old_v in jobs:
                    results[idx] = spec.rest_fallback(
                        site, row, pid, new_v, seo_plugin, url, old_v
                    )
            else:
                raise
        else:
            for k, (idx, row, pid, url, new_v, old_v) in enumerate(jobs):
                results[idx] = pw_list[k]

    return [r for r in results if r is not None]


async def _exec_seo_playwright(
    site: SiteAccess,
    page_url: str,
    new_value: str,
    row: NormalizedRow,
    *,
    post_id: int,
    old_value: str | None,
    spec: _SeoPlaywrightSpec,
) -> ExecuteRowResult:
    """Execute meta description or SEO title update using Playwright."""
    if not site.playwright:
        raise ValueError("Playwright credentials not provided")

    base_detail = {
        "source_url": page_url,
        "url": page_url,
        spec.detail_old_key: old_value,
        spec.detail_new_key: new_value,
    }

    try:
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

            if not await wp.login(page):
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
    site: SiteAccess, from_url: str, to_url: str, row: NormalizedRow
) -> ExecuteRowResult:
    """Execute 301 redirect creation using Playwright."""
    if not site.playwright:
        raise ValueError("Playwright credentials not provided")

    base_detail = dict(
        source_url=from_url,
        destination_url=to_url,
    )

    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await launch_chromium(p, headless=should_run_headless())
            context = await browser.new_context()
            page = await context.new_page()

            wp = WordPressPlaywright(
                site.playwright.admin_url,
                site.playwright.username,
                site.playwright.password.get_secret_value(),
            )

            # Login
            if not await wp.login(page):
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



def run_dry_run(site: SiteAccess, grouped: dict[str, list[NormalizedRow]], redirect_plugin: str | None = None, seo_plugin: str | None = None) -> DryRunResponse:
    total = _count_grouped(grouped)
    limit = _max_run_rows()
    if total > limit:
        raise ValueError(f"Too many rows ({total}). Maximum is {limit} (set MAX_RUN_ROWS).")

    token = current_monitor_execution_id.set(MONITOR_DEFAULT_ID)
    try:
        clear_monitor_logs()
        emit_monitor_phase("Dry-run started", f"{total} row(s)")

        rows_out: list[DryRunRowResult] = []
        for action, row in _iter_rows(grouped):
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
            emit_monitor_row_dry(
                action,
                dr.sheet_name,
                dr.row_index,
                str(dr.outcome),
                dr.message,
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


def _exec_meta(site: SiteAccess, row: NormalizedRow, seo_plugin: str | None = None) -> ExecuteRowResult:
    """Execute meta description update via Playwright or REST API."""
    v = row.values
    page_url = _s(v.get("page_url"))
    rec_meta = _s(v.get("recommended_meta_description"))

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

    # If Playwright auth is provided, use UI automation instead of REST API
    if site.playwright:
        try:
            logger.info(f"Using Playwright to update meta description for {page_url}")
            try:
                result = asyncio.run(
                    _exec_seo_playwright(
                        site,
                        page_url,
                        rec_meta,
                        row,
                        post_id=pid,
                        old_value=old_meta,
                        spec=_SEO_PW_META,
                    )
                )
                return result
            except RuntimeError as e:
                if "asyncio.run() cannot be called from a running event loop" in str(e):
                    logger.warning("Sync context only - Playwright UI automation not available in async context, using REST API")
                else:
                    raise
        except Exception as e:
            logger.error(f"Playwright meta update failed, falling back to REST API: {e}")

    return _exec_meta_via_rest(
        site,
        row,
        pid,
        rec_meta,
        seo_plugin,
        page_url=page_url,
        old_meta=old_meta,
    )


def _exec_meta_title(site: SiteAccess, row: NormalizedRow, seo_plugin: str | None = None) -> ExecuteRowResult:
    """Execute SEO title update via Playwright or REST API."""
    v = row.values
    page_url = _s(v.get("page_url"))
    rec_title = _s(v.get("recommended_meta_title"))

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

    if site.playwright:
        try:
            logger.info(f"Using Playwright to update SEO title for {page_url}")
            try:
                return asyncio.run(
                    _exec_seo_playwright(
                        site,
                        page_url,
                        rec_title,
                        row,
                        post_id=pid,
                        old_value=old_title,
                        spec=_SEO_PW_META_TITLE,
                    )
                )
            except RuntimeError as e:
                if "asyncio.run() cannot be called from a running event loop" in str(e):
                    logger.warning(
                        "Sync context only - Playwright UI automation not available in async context, using REST API"
                    )
                else:
                    raise
        except Exception as e:
            logger.error(f"Playwright SEO title update failed, falling back to REST API: {e}")

    return _exec_meta_title_via_rest(
        site,
        row,
        pid,
        rec_title,
        seo_plugin,
        page_url=page_url,
        old_title=old_title,
    )


def _exec_redirects_301(site: SiteAccess, row: NormalizedRow, redirect_plugin: str | None = None) -> ExecuteRowResult:
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
                pw_result = asyncio.run(_exec_redirects_301_playwright(site, from_url, to_url, row))
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
            if execution_logger is None:
                return
            st = (
                "success"
                if er.outcome == "updated"
                else ("warning" if er.outcome == "skipped" else "error")
            )
            execution_logger.log_sync(
                f"{er.action_type} row {er.row_index}",
                st,
                er.message or er.outcome,
            )

        clear_monitor_logs()
        emit_monitor_phase("Execute started", f"{total} row(s)")
        if execution_logger is not None:
            execution_logger.log_sync(
                "execute_started", "pending", f"{total} row(s) to process"
            )

        rows_out: list[ExecuteRowResult] = []
        try:
            rows_iter = list(_iter_rows(grouped))
            i = 0
            while i < len(rows_iter):
                action, row = rows_iter[i]
                try:
                    if action in ("meta", "meta_title") and site.playwright:
                        spec = _SEO_PW_META if action == "meta" else _SEO_PW_META_TITLE
                        batch_rows: list[NormalizedRow] = []
                        while i < len(rows_iter) and rows_iter[i][0] == action:
                            batch_rows.append(rows_iter[i][1])
                            i += 1
                        for er in _exec_seo_consecutive_playwright_batch(
                            site, batch_rows, seo_plugin, spec
                        ):
                            rows_out.append(er)
                            emit_monitor_row_exec(
                                er.action_type,
                                er.sheet_name,
                                er.row_index,
                                str(er.outcome),
                                er.message,
                            )
                            _log_row(er)
                        continue

                    if action == "on_page":
                        rows_out.append(_exec_on_page(site, row))
                    elif action == "meta":
                        rows_out.append(_exec_meta(site, row, seo_plugin))
                    elif action == "meta_title":
                        rows_out.append(_exec_meta_title(site, row, seo_plugin))
                    elif action == "images":
                        rows_out.append(_exec_on_image(site, row))
                    elif action == "url_cleanup":
                        rows_out.append(_exec_url_cleanup(site, row))
                    elif action == "redirects_301":
                        rows_out.append(_exec_redirects_301(site, row, redirect_plugin))
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

            updated = sum(1 for r in rows_out if r.outcome == "updated")
            skipped = sum(1 for r in rows_out if r.outcome == "skipped")
            failed = sum(1 for r in rows_out if r.outcome == "failed")

            emit_monitor_phase(
                "Execute finished",
                f"updated={updated}, skipped={skipped}, failed={failed}",
            )
            if execution_logger is not None:
                execution_logger.log_sync(
                    "execute_finished",
                    "success",
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
    finally:
        current_monitor_execution_id.reset(token)
