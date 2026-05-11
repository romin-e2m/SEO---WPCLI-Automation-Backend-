from __future__ import annotations

import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.schemas.wp import (
    ResolvePostRequest,
    ResolvePostResponse,
    SitePrecheckRequest,
    SitePrecheckResponse,
    UpdatePostRequest,
    UpdatePostResponse,
)
from app.services.wp_site import resolve_post_url, rest_client

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/wp", tags=["wordpress"])


class PluginInfo(BaseModel):
    """Single plugin information."""
    name: str
    title: str
    status: str  # "active" or "inactive"
    type: str    # "seo", "redirect", or "other"


class PluginListResponse(BaseModel):
    """Response for listing all plugins."""
    installed: list[PluginInfo]
    seo_plugins: list[PluginInfo]
    redirect_plugins: list[PluginInfo]
    error: str | None = None
    list_source: str | None = None


class PluginDetectionResponse(BaseModel):
    """Response for plugin detection."""
    detected_plugins: dict[str, bool]
    seo_plugins: list[str]
    redirect_plugins: list[str]


@router.post("/site/precheck", response_model=SitePrecheckResponse)
def site_precheck(body: SitePrecheckRequest) -> SitePrecheckResponse:
    rest_ok = False
    rest_error: str | None = None
    try:
        rest_client(body.site).health_check()
        rest_ok = True
    except Exception as e:
        rest_error = str(e)
        logger.warning(f"REST precheck failed: {type(e).__name__}: {rest_error}")
    return SitePrecheckResponse(rest_ok=rest_ok, rest_error=rest_error)


@router.post("/site/list-plugins", response_model=PluginListResponse)
def list_all_plugins(body: SitePrecheckRequest) -> PluginListResponse:
    """List all installed plugins and categorize them by type."""
    client = rest_client(body.site)
    try:
        plugins_data = client.list_all_plugins()
        
        # Convert to response model
        installed = [PluginInfo(**p) for p in plugins_data.get("installed", [])]
        seo_plugins = [PluginInfo(**p) for p in plugins_data.get("seo_plugins", [])]
        redirect_plugins = [PluginInfo(**p) for p in plugins_data.get("redirect_plugins", [])]
        error = plugins_data.get("error")
        
        return PluginListResponse(
            installed=installed,
            seo_plugins=seo_plugins,
            redirect_plugins=redirect_plugins,
            error=error,
            list_source=plugins_data.get("list_source"),
        )
    except Exception as e:
        logger.error(f"Plugin listing failed: {str(e)}")
        raise HTTPException(status_code=400, detail=f"Plugin listing failed: {str(e)}") from e


@router.post("/site/detect-plugins", response_model=PluginDetectionResponse)
def detect_plugins(body: SitePrecheckRequest) -> PluginDetectionResponse:
    """Detect active SEO and redirect plugins on the WordPress site."""
    client = rest_client(body.site)
    try:
        client.health_check()
        plugins = client.detect_active_plugins()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Plugin detection failed: {str(e)}") from e
    
    # Categorize plugins
    seo_plugins = [
        name.replace("_", " ").title()
        for key, name in [
            ("yoast", "Yoast SEO"),
            ("rank_math", "Rank Math"),
            ("seopress", "SEOPress"),
        ]
        if plugins.get(key, False)
    ]
    
    redirect_plugins = [
        name.replace("_", " ").title()
        for key, name in [
            ("redirection", "Redirection"),
            ("rank_math_redirects", "Rank Math"),
            ("safe_redirect_manager", "Safe Redirect Manager"),
        ]
        if plugins.get(key, False)
    ]
    
    return PluginDetectionResponse(
        detected_plugins=plugins,
        seo_plugins=seo_plugins,
        redirect_plugins=redirect_plugins,
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
