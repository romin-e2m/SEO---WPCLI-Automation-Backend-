from __future__ import annotations

import os
from typing import Any

from app.schemas.run import (
    DryRunResponse,
    DryRunRowResult,
    ExecuteResponse,
    ExecuteRowResult,
    FieldDiff,
)
from app.schemas.workbook import NormalizedRow
from app.schemas.wp import SiteAccess
from app.services.wp_adapters import detect_seo_meta_adapter
from app.services.wp_rest import WpRestClient
from app.services.wp_site import resolve_post_url, rest_client, wp_cli_runner

_ORDER = ("on_page", "meta", "images", "url_cleanup", "redirects_301")


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


def _count_grouped(grouped: dict[str, list[NormalizedRow]]) -> int:
    return sum(len(rows) for rows in grouped.values())


def _iter_rows(grouped: dict[str, list[NormalizedRow]]):
    for key in _ORDER:
        for row in grouped.get(key, []) or []:
            yield key, row


def _redirect_system(runner) -> str | None:
    if not runner:
        return None
    names = {p.lower() for p in runner.active_plugins()}
    if "redirection" in names:
        return "redirection"
    return None


def _dry_on_page(site: SiteAccess, row: NormalizedRow) -> DryRunRowResult:
    v = row.values
    page_url = _s(v.get("page_url"))
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
    cur_title = _s(summ.get("title"))
    raw_content = summ.get("content")
    cur_content = raw_content if isinstance(raw_content, str) else ""
    cur_h1 = WpRestClient.first_h1_inner_text(cur_content) or ""

    rec_title = _s(v.get("recommended_title"))
    rec_h1 = _s(v.get("recommended_h1"))
    rec_content = _s(v.get("recommended_content"))

    if rec_h1 and not WpRestClient.first_h1_inner_text(cur_content):
        return DryRunRowResult(
            action_type="on_page",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="blocked",
            message="Recommended H1 is set but the post content has no <h1> tag to replace.",
            post_id=pid,
            resolution_method=res.method,
        )

    diffs: list[FieldDiff] = []
    if rec_title and rec_title != cur_title:
        diffs.append(FieldDiff(field="title", current=cur_title, proposed=rec_title))
    if rec_h1 and rec_h1 != cur_h1:
        diffs.append(FieldDiff(field="h1", current=cur_h1, proposed=rec_h1))
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


def _dry_meta(site: SiteAccess, row: NormalizedRow) -> DryRunRowResult:
    runner = wp_cli_runner(site)
    if not runner:
        return DryRunRowResult(
            action_type="meta",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="blocked",
            message="WP-CLI is required to read and write meta descriptions.",
        )
    v = row.values
    page_url = _s(v.get("page_url"))
    proposed = _s(v.get("recommended_meta_description"))
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
    adapter = detect_seo_meta_adapter(runner.active_plugins())
    current = runner.get_post_meta(pid, adapter.metadesc_key) or ""
    if proposed == current:
        return DryRunRowResult(
            action_type="meta",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="no_change",
            post_id=pid,
            resolution_method=res.method,
            diffs=[],
        )
    return DryRunRowResult(
        action_type="meta",
        sheet_name=row.sheet_name,
        row_index=row.row_index,
        outcome="change",
        post_id=pid,
        resolution_method=res.method,
        diffs=[FieldDiff(field="meta_description", current=current, proposed=proposed)],
    )


def _dry_images(site: SiteAccess, row: NormalizedRow) -> DryRunRowResult:
    runner = wp_cli_runner(site)
    if not runner:
        return DryRunRowResult(
            action_type="images",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="blocked",
            message="WP-CLI is required to resolve media URLs and alt text.",
        )
    v = row.values
    media_url = _s(v.get("image_url"))
    proposed = _s(v.get("recommended_alt_text"))
    aid = runner.attachment_id_from_url(media_url)
    if not aid:
        return DryRunRowResult(
            action_type="images",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="blocked",
            message="Could not resolve image URL to a media attachment.",
            resolution_method="wp_cli:attachment_url_to_postid",
        )
    current = runner.get_post_meta(aid, "_wp_attachment_image_alt") or ""
    if proposed == current:
        return DryRunRowResult(
            action_type="images",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="no_change",
            attachment_id=aid,
            resolution_method="wp_cli:attachment_url_to_postid",
            diffs=[],
        )
    return DryRunRowResult(
        action_type="images",
        sheet_name=row.sheet_name,
        row_index=row.row_index,
        outcome="change",
        attachment_id=aid,
        resolution_method="wp_cli:attachment_url_to_postid",
        diffs=[FieldDiff(field="alt_text", current=current, proposed=proposed)],
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
    if old_u not in content:
        return DryRunRowResult(
            action_type="url_cleanup",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="blocked",
            message="The URL to replace was not found in the post body HTML.",
            post_id=pid,
            resolution_method=res.method,
            diffs=[FieldDiff(field="content_url_replace", current=None, proposed=f"{old_u} → {new_u}")],
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


def _dry_redirects(site: SiteAccess, row: NormalizedRow) -> DryRunRowResult:
    runner = wp_cli_runner(site)
    if not runner:
        return DryRunRowResult(
            action_type="redirects_301",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="blocked",
            message="WP-CLI is required to create redirects.",
        )
    if _redirect_system(runner) is None:
        return DryRunRowResult(
            action_type="redirects_301",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="blocked",
            message="No supported redirect system (Redirection plugin) detected.",
        )
    v = row.values
    src = _s(v.get("source_url"))
    dst = _s(v.get("target_url"))
    if not src.startswith("http") or not dst.startswith("http"):
        return DryRunRowResult(
            action_type="redirects_301",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="blocked",
            message="Source and destination must be absolute http(s) URLs.",
        )
    if src.rstrip("/") == dst.rstrip("/"):
        return DryRunRowResult(
            action_type="redirects_301",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="blocked",
            message="Source and destination URLs must differ.",
        )
    return DryRunRowResult(
        action_type="redirects_301",
        sheet_name=row.sheet_name,
        row_index=row.row_index,
        outcome="change",
        diffs=[FieldDiff(field="redirect", current=src, proposed=dst)],
    )


def run_dry_run(site: SiteAccess, grouped: dict[str, list[NormalizedRow]]) -> DryRunResponse:
    total = _count_grouped(grouped)
    limit = _max_run_rows()
    if total > limit:
        raise ValueError(f"Too many rows ({total}). Maximum is {limit} (set MAX_RUN_ROWS).")

    runner = wp_cli_runner(site)
    adapter = detect_seo_meta_adapter(runner.active_plugins()) if runner else None
    rows_out: list[DryRunRowResult] = []

    for action, row in _iter_rows(grouped):
        if action == "on_page":
            rows_out.append(_dry_on_page(site, row))
        elif action == "meta":
            rows_out.append(_dry_meta(site, row))
        elif action == "images":
            rows_out.append(_dry_images(site, row))
        elif action == "url_cleanup":
            rows_out.append(_dry_url_cleanup(site, row))
        elif action == "redirects_301":
            rows_out.append(_dry_redirects(site, row))

    ready = sum(1 for r in rows_out if r.outcome == "change")
    blocked = sum(1 for r in rows_out if r.outcome == "blocked")
    errors = sum(1 for r in rows_out if r.outcome == "error")
    no_change = sum(1 for r in rows_out if r.outcome == "no_change")

    return DryRunResponse(
        rows_processed=len(rows_out),
        ready_to_execute=ready,
        blocked=blocked,
        errors=errors,
        no_change=no_change,
        detected_meta_plugin=adapter.plugin if adapter else None,
        meta_description_key=adapter.metadesc_key if adapter else None,
        redirect_system=_redirect_system(runner),
        rows=rows_out,
    )


def _exec_on_page(site: SiteAccess, row: NormalizedRow) -> ExecuteRowResult:
    dr = _dry_on_page(site, row)
    if dr.outcome == "blocked":
        return ExecuteRowResult(
            action_type="on_page",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="failed",
            message=dr.message,
        )
    if dr.outcome == "no_change":
        return ExecuteRowResult(
            action_type="on_page",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="skipped",
            message="Already matches requested values.",
            post_id=dr.post_id,
        )
    v = row.values
    pid = dr.post_id
    if not pid:
        return ExecuteRowResult(
            action_type="on_page",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="failed",
            message="Missing post id.",
        )
    client = rest_client(site)
    obj = client.get_post(pid)
    summ = WpRestClient.extract_summary_fields(obj)
    cur_title = _s(summ.get("title"))
    raw_content = summ.get("content")
    content = raw_content if isinstance(raw_content, str) else ""

    new_title = cur_title
    rec_title = _s(v.get("recommended_title"))
    if rec_title:
        new_title = rec_title

    new_content = content
    rec_content = _s(v.get("recommended_content"))
    if rec_content:
        new_content = rec_content
    rec_h1 = _s(v.get("recommended_h1"))
    if rec_h1:
        patched, ok = WpRestClient.replace_first_h1_inner(new_content, rec_h1)
        if not ok:
            return ExecuteRowResult(
                action_type="on_page",
                sheet_name=row.sheet_name,
                row_index=row.row_index,
                outcome="failed",
                message="No <h1> found to replace.",
                post_id=pid,
            )
        new_content = patched

    title_arg = new_title if new_title != cur_title else None
    content_arg = new_content if new_content != content else None
    if title_arg is None and content_arg is None:
        return ExecuteRowResult(
            action_type="on_page",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="skipped",
            message="No effective changes after re-fetch.",
            post_id=pid,
        )

    try:
        out = client.update_post(pid, title=title_arg, content=content_arg)
    except Exception as e:
        return ExecuteRowResult(
            action_type="on_page",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="failed",
            message=str(e),
            post_id=pid,
        )
    return ExecuteRowResult(
        action_type="on_page",
        sheet_name=row.sheet_name,
        row_index=row.row_index,
        outcome="updated",
        post_id=pid,
        detail={"raw_id": out.get("id")},
    )


def _exec_meta(site: SiteAccess, row: NormalizedRow) -> ExecuteRowResult:
    dr = _dry_meta(site, row)
    if dr.outcome != "change":
        return ExecuteRowResult(
            action_type="meta",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="skipped" if dr.outcome == "no_change" else "failed",
            message=dr.message or ("No meta change needed." if dr.outcome == "no_change" else None),
            post_id=dr.post_id,
        )
    runner = wp_cli_runner(site)
    if not runner or not dr.post_id:
        return ExecuteRowResult(
            action_type="meta",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="failed",
            message="WP-CLI unavailable.",
        )
    adapter = detect_seo_meta_adapter(runner.active_plugins())
    proposed = _s(row.values.get("recommended_meta_description"))
    try:
        runner.update_post_meta(dr.post_id, adapter.metadesc_key, proposed)
    except Exception as e:
        return ExecuteRowResult(
            action_type="meta",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="failed",
            message=str(e),
            post_id=dr.post_id,
        )
    return ExecuteRowResult(
        action_type="meta",
        sheet_name=row.sheet_name,
        row_index=row.row_index,
        outcome="updated",
        post_id=dr.post_id,
        detail={"plugin": adapter.plugin, "meta_key": adapter.metadesc_key},
    )


def _exec_images(site: SiteAccess, row: NormalizedRow) -> ExecuteRowResult:
    dr = _dry_images(site, row)
    if dr.outcome != "change":
        return ExecuteRowResult(
            action_type="images",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="skipped" if dr.outcome == "no_change" else "failed",
            message=dr.message,
            attachment_id=dr.attachment_id,
        )
    runner = wp_cli_runner(site)
    if not runner or not dr.attachment_id:
        return ExecuteRowResult(
            action_type="images",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="failed",
            message="WP-CLI unavailable.",
        )
    proposed = _s(row.values.get("recommended_alt_text"))
    try:
        runner.update_post_meta(dr.attachment_id, "_wp_attachment_image_alt", proposed)
    except Exception as e:
        return ExecuteRowResult(
            action_type="images",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="failed",
            message=str(e),
            attachment_id=dr.attachment_id,
        )
    return ExecuteRowResult(
        action_type="images",
        sheet_name=row.sheet_name,
        row_index=row.row_index,
        outcome="updated",
        attachment_id=dr.attachment_id,
    )


def _exec_url_cleanup(site: SiteAccess, row: NormalizedRow) -> ExecuteRowResult:
    dr = _dry_url_cleanup(site, row)
    if dr.outcome != "change":
        return ExecuteRowResult(
            action_type="url_cleanup",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="skipped" if dr.outcome == "no_change" else "failed",
            message=dr.message,
            post_id=dr.post_id,
        )
    pid = dr.post_id
    if not pid:
        return ExecuteRowResult(
            action_type="url_cleanup",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="failed",
            message="Missing post id.",
        )
    v = row.values
    old_u = _s(v.get("old_url"))
    new_u = _s(v.get("new_url"))
    client = rest_client(site)
    obj = client.get_post(pid)
    raw = WpRestClient.extract_summary_fields(obj).get("content")
    content = raw if isinstance(raw, str) else ""
    new_content = content.replace(old_u, new_u)
    if new_content == content:
        return ExecuteRowResult(
            action_type="url_cleanup",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="failed",
            message="Replacement produced no change.",
            post_id=pid,
        )
    try:
        client.update_post(pid, title=None, content=new_content)
    except Exception as e:
        return ExecuteRowResult(
            action_type="url_cleanup",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="failed",
            message=str(e),
            post_id=pid,
        )
    return ExecuteRowResult(
        action_type="url_cleanup",
        sheet_name=row.sheet_name,
        row_index=row.row_index,
        outcome="updated",
        post_id=pid,
    )


def _exec_redirects(site: SiteAccess, row: NormalizedRow) -> ExecuteRowResult:
    dr = _dry_redirects(site, row)
    if dr.outcome != "change":
        return ExecuteRowResult(
            action_type="redirects_301",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="failed",
            message=dr.message,
        )
    runner = wp_cli_runner(site)
    if not runner:
        return ExecuteRowResult(
            action_type="redirects_301",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="failed",
            message="WP-CLI unavailable.",
        )
    v = row.values
    src = _s(v.get("source_url"))
    dst = _s(v.get("target_url"))
    try:
        out = runner.run(
            ["redirection", "add", "--url", src, "--target", dst, "--code", "301"],
            timeout_seconds=30,
        )
    except Exception as e:
        return ExecuteRowResult(
            action_type="redirects_301",
            sheet_name=row.sheet_name,
            row_index=row.row_index,
            outcome="failed",
            message=str(e),
        )
    return ExecuteRowResult(
        action_type="redirects_301",
        sheet_name=row.sheet_name,
        row_index=row.row_index,
        outcome="updated",
        detail={"system": "redirection", "output": out},
    )


def run_execute(site: SiteAccess, grouped: dict[str, list[NormalizedRow]]) -> ExecuteResponse:
    total = _count_grouped(grouped)
    limit = _max_run_rows()
    if total > limit:
        raise ValueError(f"Too many rows ({total}). Maximum is {limit} (set MAX_RUN_ROWS).")

    rows_out: list[ExecuteRowResult] = []
    for action, row in _iter_rows(grouped):
        if action == "on_page":
            rows_out.append(_exec_on_page(site, row))
        elif action == "meta":
            rows_out.append(_exec_meta(site, row))
        elif action == "images":
            rows_out.append(_exec_images(site, row))
        elif action == "url_cleanup":
            rows_out.append(_exec_url_cleanup(site, row))
        elif action == "redirects_301":
            rows_out.append(_exec_redirects(site, row))

    updated = sum(1 for r in rows_out if r.outcome == "updated")
    skipped = sum(1 for r in rows_out if r.outcome == "skipped")
    failed = sum(1 for r in rows_out if r.outcome == "failed")
    return ExecuteResponse(
        rows_processed=len(rows_out),
        updated=updated,
        skipped=skipped,
        failed=failed,
        rows=rows_out,
    )
