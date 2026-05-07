from __future__ import annotations

import logging
from fastapi import APIRouter, HTTPException

from app.schemas.wp import (
    CreateRedirectRequest,
    CreateRedirectResponse,
    ResolveMediaRequest,
    ResolveMediaResponse,
    ResolvePostRequest,
    ResolvePostResponse,
    SitePrecheckRequest,
    SitePrecheckResponse,
    UpdateAltTextRequest,
    UpdateAltTextResponse,
    UpdateMetaDescriptionRequest,
    UpdateMetaDescriptionResponse,
    UpdatePostRequest,
    UpdatePostResponse,
)
from app.services.wp_adapters import detect_seo_meta_adapter
from app.services.wp_site import resolve_post_url, rest_client, wp_cli_runner

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/wp", tags=["wordpress"])


@router.post("/site/precheck", response_model=SitePrecheckResponse)
def site_precheck(body: SitePrecheckRequest) -> SitePrecheckResponse:
    rest_ok = False
    wp_cli_ok = False
    wp_cli_version = None
    active_plugins: list[str] = []
    wp_cli_mode = None

    try:
        rest_client(body.site).health_check()
        rest_ok = True
    except Exception as e:
        logger.warning(f"REST precheck failed (non-blocking): {type(e).__name__}: {str(e)}")

    if body.site.wp_cli:
        wp_cli_mode = body.site.wp_cli.mode
        runner = wp_cli_runner(body.site)
        if runner:
            try:
                wp_cli_version = runner.version()
                active_plugins = runner.active_plugins()
                wp_cli_ok = True
            except Exception as e:
                logger.error(f"WP-CLI precheck failed: {type(e).__name__}: {str(e)}")
                wp_cli_ok = False
        else:
            logger.warning("WP-CLI config provided but runner could not be initialized (incomplete SSH config?)")
            wp_cli_ok = False

    return SitePrecheckResponse(
        rest_ok=rest_ok,
        wp_cli_ok=wp_cli_ok,
        wp_cli_mode=wp_cli_mode,
        wp_cli_version=wp_cli_version,
        active_plugins=active_plugins,
    )


@router.post("/posts/resolve", response_model=ResolvePostResponse)
def resolve_post(body: ResolvePostRequest) -> ResolvePostResponse:
    return resolve_post_url(body.site, body.url, body.post_type)


@router.post("/posts/update", response_model=UpdatePostResponse)
def update_post(body: UpdatePostRequest) -> UpdatePostResponse:
    if body.title is None and body.content is None:
        raise HTTPException(status_code=400, detail="Provide title and/or content.")
    client = rest_client(body.site)
    try:
        updated = client.update_post(body.post_id, title=body.title, content=body.content)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Update failed: {str(e)}") from e
    return UpdatePostResponse(updated=True, post_id=body.post_id, raw=updated)


@router.post("/media/resolve", response_model=ResolveMediaResponse)
def resolve_media(body: ResolveMediaRequest) -> ResolveMediaResponse:
    runner = wp_cli_runner(body.site)
    if not runner:
        raise HTTPException(
            status_code=400,
            detail="Media resolution requires wp_cli config (uses attachment_url_to_postid).",
        )
    aid = runner.attachment_id_from_url(body.media_url)
    return ResolveMediaResponse(
        found=aid is not None,
        attachment_id=aid,
        method="wp_cli:attachment_url_to_postid",
    )


@router.post("/media/update-alt", response_model=UpdateAltTextResponse)
def update_alt_text(body: UpdateAltTextRequest) -> UpdateAltTextResponse:
    runner = wp_cli_runner(body.site)
    if not runner:
        raise HTTPException(status_code=400, detail="Alt text update requires wp_cli config.")
    if not body.alt_text.strip():
        raise HTTPException(status_code=400, detail="alt_text must not be empty.")
    try:
        runner.update_post_meta(body.attachment_id, "_wp_attachment_image_alt", body.alt_text)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"WP-CLI meta update failed: {str(e)}") from e
    return UpdateAltTextResponse(updated=True, attachment_id=body.attachment_id)


@router.post("/meta/update-description", response_model=UpdateMetaDescriptionResponse)
def update_meta_description(body: UpdateMetaDescriptionRequest) -> UpdateMetaDescriptionResponse:
    runner = wp_cli_runner(body.site)
    if not runner:
        raise HTTPException(status_code=400, detail="Meta updates require wp_cli config.")
    plugins = runner.active_plugins()
    adapter = detect_seo_meta_adapter(plugins)
    if not body.meta_description.strip():
        raise HTTPException(status_code=400, detail="meta_description must not be empty.")
    try:
        runner.update_post_meta(body.post_id, adapter.metadesc_key, body.meta_description)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Meta update failed: {str(e)}") from e
    return UpdateMetaDescriptionResponse(
        updated=True,
        post_id=body.post_id,
        plugin=adapter.plugin,
        meta_key=adapter.metadesc_key,
    )


@router.post("/redirects/create", response_model=CreateRedirectResponse)
def create_redirect(body: CreateRedirectRequest) -> CreateRedirectResponse:
    """
    Adapter placeholder.
    For now, we implement a conservative baseline using the Redirection plugin command if present.
    """
    runner = wp_cli_runner(body.site)
    if not runner:
        raise HTTPException(status_code=400, detail="Redirect creation requires wp_cli config.")
    if body.http_code != 301:
        raise HTTPException(status_code=400, detail="Only 301 redirects are supported right now.")
    src = body.source_url.strip()
    dst = body.target_url.strip()
    if not src or not dst:
        raise HTTPException(status_code=400, detail="source_url and target_url are required.")
    if src.rstrip("/") == dst.rstrip("/"):
        raise HTTPException(status_code=400, detail="source_url and target_url must differ.")

    plugins = runner.active_plugins()
    names = {p.lower() for p in plugins}

    # Redirection plugin provides `wp redirection` commands on many installs.
    if "redirection" in names:
        try:
            out = runner.run(
                [
                    "redirection",
                    "add",
                    "--url",
                    src,
                    "--target",
                    dst,
                    "--code",
                    "301",
                ],
                timeout_seconds=30,
            )
            return CreateRedirectResponse(
                created=True,
                system="redirection",
                details={"output": out},
            )
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Redirect failed: {str(e)}") from e

    raise HTTPException(
        status_code=400,
        detail="No supported redirect system detected. Install/enable Redirection plugin or add an adapter.",
    )
