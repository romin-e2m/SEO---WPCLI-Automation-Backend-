from __future__ import annotations

from typing import Literal

from app.schemas.wp import ResolvedPost, ResolvePostResponse, SiteAccess
from app.services.wp_rest import WpRestAuth, WpRestClient


def rest_client(site: SiteAccess) -> WpRestClient:
    r = site.rest
    return WpRestClient(
        WpRestAuth(
            base_url=r.base_url,
            username=r.username,
            application_password=r.application_password.get_secret_value(),
        )
    )


def resolve_post_url(
    site: SiteAccess,
    url: str,
    post_type: Literal["any", "post", "page"] = "any",
) -> ResolvePostResponse:
    client = rest_client(site)
    obj, method = client.resolve_post_by_url(url, post_type=post_type)
    if not obj:
        return ResolvePostResponse(found=False, post=None, method=method)
    pid = obj.get("id")
    if not isinstance(pid, int):
        return ResolvePostResponse(found=False, post=None, method="rest_invalid_id")
    full = client.get_post(pid)
    return ResolvePostResponse(
        found=True,
        post=ResolvedPost(**WpRestClient.extract_summary_fields(full)),
        method=method,
    )
