from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class WpAuth(BaseModel):
    base_url: str = Field(description="Site base URL, e.g. https://example.com")
    username: str = Field(description="WP username for REST authentication")
    application_password: str = Field(
        description="WP Application Password (recommended over raw password)"
    )


class WpCliSsh(BaseModel):
    host: str = Field(description="SSH hostname or IP address")
    user: str = Field(description="SSH username")
    port: int = Field(default=22, ge=1, le=65535, description="SSH port")
    identity_file: str | None = Field(
        default=None,
        description="Absolute path to SSH private key file (optional).",
    )
    connect_timeout_seconds: int = Field(default=10, ge=1)

    @field_validator("host", "user", mode="before")
    @classmethod
    def validate_required_strings(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip()
        if not v:
            raise ValueError("host and user must not be empty")
        return v


class WpCliConfig(BaseModel):
    mode: Literal["local", "ssh"] = "ssh"
    wp_path: str | None = Field(
        default=None,
        description="Absolute path to WP install on the server (passed as --path=...).",
    )
    ssh: WpCliSsh | None = None


class SiteAccess(BaseModel):
    rest: WpAuth
    wp_cli: WpCliConfig | None = None


class SitePrecheckRequest(BaseModel):
    site: SiteAccess


class SitePrecheckResponse(BaseModel):
    rest_ok: bool
    wp_cli_ok: bool = False
    wp_cli_mode: str | None = None
    wp_cli_version: str | None = None
    active_plugins: list[str] = []


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


class ResolveMediaRequest(BaseModel):
    site: SiteAccess
    media_url: str


class ResolveMediaResponse(BaseModel):
    found: bool
    attachment_id: int | None = None
    method: str


class UpdateAltTextRequest(BaseModel):
    site: SiteAccess
    attachment_id: int
    alt_text: str


class UpdateAltTextResponse(BaseModel):
    updated: bool
    attachment_id: int


class UpdateMetaDescriptionRequest(BaseModel):
    site: SiteAccess
    post_id: int
    meta_description: str


class UpdateMetaDescriptionResponse(BaseModel):
    updated: bool
    post_id: int
    plugin: str
    meta_key: str


class CreateRedirectRequest(BaseModel):
    site: SiteAccess
    source_url: str
    target_url: str
    http_code: int = 301


class CreateRedirectResponse(BaseModel):
    created: bool
    system: str
    details: dict[str, Any] | None = None
