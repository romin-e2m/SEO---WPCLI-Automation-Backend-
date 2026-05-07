from __future__ import annotations

from typing import Literal

from app.schemas.wp import ResolvedPost, ResolvePostResponse, SiteAccess
from app.services.wp_cli import WpCliConfig, WpCliRunner, WpCliSshConfig
from app.services.wp_rest import WpRestAuth, WpRestClient


def rest_client(site: SiteAccess) -> WpRestClient:
    r = site.rest
    return WpRestClient(
        WpRestAuth(
            base_url=r.base_url,
            username=r.username,
            application_password=r.application_password,
        )
    )


def wp_cli_runner(site: SiteAccess) -> WpCliRunner | None:
    if not site.wp_cli:
        return None
    cfg = site.wp_cli
    if cfg.mode == "local":
        return WpCliRunner(WpCliConfig(mode="local", wp_path=cfg.wp_path, ssh=None))
    if cfg.mode == "ssh":
        if not cfg.ssh:
            return None
        ssh = cfg.ssh
        return WpCliRunner(
            WpCliConfig(
                mode="ssh",
                wp_path=cfg.wp_path,
                ssh=WpCliSshConfig(
                    host=ssh.host,
                    user=ssh.user,
                    port=ssh.port,
                    identity_file=ssh.identity_file,
                    connect_timeout_seconds=ssh.connect_timeout_seconds,
                ),
            )
        )
    return None


def resolve_post_url(
    site: SiteAccess,
    url: str,
    post_type: Literal["any", "post", "page"] = "any",
) -> ResolvePostResponse:
    """Match URL to a post/page id using WP-CLI when available, otherwise REST search."""
    runner = wp_cli_runner(site)
    client = rest_client(site)

    if runner:
        post_id = runner.url_to_postid(url)
        if post_id:
            obj = client.get_post(post_id)
            return ResolvePostResponse(
                found=True,
                post=ResolvedPost(**WpRestClient.extract_summary_fields(obj)),
                method="wp_cli:url_to_postid",
            )

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
