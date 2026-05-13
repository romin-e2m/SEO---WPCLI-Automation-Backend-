from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr


class WpAuth(BaseModel):
    base_url: str = Field(description="Site base URL, e.g. https://example.com")
    username: str = Field(description="WP username for REST authentication")
    application_password: SecretStr = Field(
        description="WP Application Password (recommended over raw password)"
    )


class PlaywrightAuth(BaseModel):
    admin_url: str = Field(description="WordPress admin URL, e.g. https://example.com/wp-admin/")
    username: str = Field(description="WordPress username")
    password: SecretStr = Field(description="WordPress password for UI automation")


class SiteAccess(BaseModel):
    rest: WpAuth
    playwright: PlaywrightAuth | None = None


class SitePrecheckRequest(BaseModel):
    site: SiteAccess


class SitePrecheckResponse(BaseModel):
    rest_ok: bool
    rest_error: str | None = None


class ResolvePostRequest(BaseModel):
    site: SiteAccess
    url: str
    post_type: Literal["any", "post", "page"] = "any"


class ResolvedPost(BaseModel):
    id: int
    type: str
    link: str | None = None
    title: str | None = None
    content: str | None = None


class ResolvePostResponse(BaseModel):
    found: bool
    post: ResolvedPost | None = None
    method: str = Field(description="How we resolved URL to a post id.")


class UpdatePostRequest(BaseModel):
    site: SiteAccess
    post_id: int
    title: str | None = None
    content: str | None = None


class UpdatePostResponse(BaseModel):
    updated: bool
    post_id: int
    raw: dict[str, Any] | None = None


class PluginInfo(BaseModel):
    """Single plugin information."""

    name: str
    title: str
    status: str  # "active" or "inactive"
    type: str  # "seo", "redirect", or "other"


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
