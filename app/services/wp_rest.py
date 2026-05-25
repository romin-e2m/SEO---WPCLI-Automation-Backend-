from __future__ import annotations

import html
import ipaddress
import logging
import os
import re
import time
from dataclasses import dataclass, asdict
from enum import Enum
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit, urlparse

import httpx

logger = logging.getLogger(__name__)


def _meta_plugin_keys_from_seo_selection(seo_plugin: str | None) -> set[str] | None:
    """Map REST plugin file slug (from /wp/v2/plugins) to internal meta handler keys."""
    if not (seo_plugin or "").strip():
        return None
    s = seo_plugin.lower()
    keys: set[str] = set()
    if any(k in s for k in ("wordpress-seo", "yoast", "wp-seo", "/wp-seo.php")):
        keys.add("yoast")
    if any(k in s for k in ("rank-math", "seo-by-rank", "rank_math")):
        keys.add("rank_math")
    if "seopress" in s:
        keys.add("seopress")
    return keys if keys else None


@dataclass(frozen=True)
class _SeoMetaRestProfile:
    """Shared REST update behaviour for meta description vs SEO title."""

    field_configs: tuple[dict[str, Any], ...]
    success_log_fmt: str
    debug_fail_fmt: str
    debug_fail_private_fmt: str
    warn_no_plugin: str
    warn_all_failed: str


_SEO_META_DESC_PROFILE = _SeoMetaRestProfile(
    field_configs=(
        {
            "key": "rank_math_description",
            "plugin": "rank_math",
            "name": "Rank Math",
            "priority": 1,
            "rest_safe": True,
        },
        {
            "key": "_yoast_wpseo_metadesc",
            "plugin": "yoast",
            "name": "Yoast SEO",
            "priority": 2,
            "rest_safe": False,
        },
        {
            "key": "_rank_math_description",
            "plugin": "rank_math",
            "name": "Rank Math",
            "priority": 2,
            "rest_safe": False,
        },
        {
            "key": "_seopress_titles_desc",
            "plugin": "seopress",
            "name": "SEOPress",
            "priority": 3,
            "rest_safe": False,
        },
    ),
    success_log_fmt="Updated post {post_id} meta via REST API: {plugin_name} ({meta_key})",
    debug_fail_fmt="REST API attempt for {plugin_name} failed: {exc}",
    debug_fail_private_fmt="REST API attempt for {plugin_name} failed: {exc}",
    warn_no_plugin="No SEO plugin detected on post {post_id}. Meta description cannot be updated.",
    warn_all_failed=(
        "REST API meta update for post {post_id} failed for all {detected} fields. "
        "This is likely due to REST API permission restrictions on private meta fields. "
        "Using WP-CLI would succeed, but REST API access is currently being used."
    ),
)

_SEO_META_TITLE_PROFILE = _SeoMetaRestProfile(
    field_configs=(
        {
            "key": "rank_math_title",
            "plugin": "rank_math",
            "name": "Rank Math",
            "priority": 1,
            "rest_safe": True,
        },
        {
            "key": "_yoast_wpseo_title",
            "plugin": "yoast",
            "name": "Yoast SEO",
            "priority": 2,
            "rest_safe": False,
        },
        {
            "key": "_rank_math_title",
            "plugin": "rank_math",
            "name": "Rank Math",
            "priority": 2,
            "rest_safe": False,
        },
        {
            "key": "_seopress_titles_title",
            "plugin": "seopress",
            "name": "SEOPress",
            "priority": 3,
            "rest_safe": False,
        },
    ),
    success_log_fmt="Updated post {post_id} SEO title via REST API: {plugin_name} ({meta_key})",
    debug_fail_fmt="REST API SEO title attempt for {plugin_name} failed: {exc}",
    debug_fail_private_fmt="REST API SEO title attempt for {plugin_name} failed: {exc}",
    warn_no_plugin="No SEO plugin detected on post {post_id}. SEO title cannot be updated.",
    warn_all_failed=(
        "REST API SEO title update for post {post_id} failed for all {detected} fields. "
        "This is likely due to REST API permission restrictions on private meta fields."
    ),
)


def _redirect_rest_backends(preferred_plugin: str | None) -> list[str]:
    """
    Which REST redirect backends to try, in order.
    Empty list: no REST API for the selected plugin (UI / Playwright only).
    """
    if not (preferred_plugin or "").strip():
        return ["redirection", "rank_math", "safe_redirect_manager"]
    s = preferred_plugin.lower()
    if "eps-301" in s or "eps_301" in s:
        return []
    # WebFactory / similar "301 Redirects" — not the same as John Godley's "Redirection" plugin
    if "301-redirects/" in s or s.startswith("301-redirects/"):
        return []
    if "redirection/" in s:
        return ["redirection"]
    if "rank-math" in s or "seo-by-rank-math" in s:
        return ["rank_math"]
    if "safe-redirect-manager" in s:
        return ["safe_redirect_manager"]
    return ["redirection", "rank_math", "safe_redirect_manager"]


DEFAULT_WP_REST_TIMEOUT_S = 30
DEFAULT_WP_REST_RATE_LIMIT_S = 0.5


def _wp_json_root_base(base_url: str) -> str:
    """REST root ``/wp-json`` for plugin namespaces (Redirection, Rank Math). Core routes use ``/wp-json/wp/v2``."""
    return f"{_normalize_base_url(base_url)}/wp-json"


def _redirection_plugin_create_payload(from_url: str, to_url: str) -> dict[str, Any]:
    """Body for POST ``/redirection/v1/redirect`` (John Godley Redirection)."""
    return {
        "source": from_url,
        "target": to_url,
        "regex": False,
        "match_type": "url",
        "status_code": 301,
        "group_id": 1,
        "enabled": True,
    }


def _rank_math_redirect_create_payload(from_url: str, to_url: str) -> dict[str, Any]:
    """Body for POST ``/rank-math/v1/redirects`` (Rank Math REST API)."""
    return {
        "sources": [{"source": from_url}],
        "destination": to_url,
        "type": "301",
    }


def _safe_redirect_manager_create_payload(from_url: str, to_url: str) -> dict[str, Any]:
    """Body for POST under wp/v2 — use path ``/redirects`` with base ``.../wp-json/wp/v2``."""
    return {
        "redirect_from": from_url,
        "redirect_to": to_url,
        "redirect_code": "301",
    }


def _normalize_base_url(base_url: str) -> str:
    base_url = (base_url or "").strip()
    return base_url.rstrip("/")


def _wp_api_base(base_url: str) -> str:
    return f"{_normalize_base_url(base_url)}/wp-json/wp/v2"


def _running_in_docker() -> bool:
    if os.path.exists("/.dockerenv"):
        return True
    try:
        with open("/proc/1/cgroup") as f:
            content = f.read()
            return "docker" in content or "containerd" in content
    except OSError:
        return False


def _connection_help(base_url: str) -> str | None:
    """
    Actionable, site-type-aware hints for connection failures.
    Covers all site types: local dev, staging, production, SSH-hosted.
    Must not include secrets.
    """
    try:
        p = urlsplit(_normalize_base_url(base_url))
    except Exception:
        return None

    if not p.scheme:
        return "URL is missing scheme. Use http:// or https://"

    host = (p.hostname or "").lower()
    port = p.port
    display_port = f":{port}" if port else ""

    if _running_in_docker():
        if host in {"localhost", "127.0.0.1", "::1"}:
            return (
                f"Backend is running in Docker. '{host}' points to the container itself, not your machine. "
                f"Use http://host.docker.internal{display_port}/ to reach your host machine. "
                f"On Linux, ensure 'host.docker.internal:host-gateway' is in extra_hosts in docker-compose.yml."
            )

        if host.endswith(".local"):
            return (
                f"Backend is running in Docker. mDNS hostnames like '{host}' are not resolvable from inside a container "
                f"(mDNS is a host-OS service). Fix options:\n"
                f"  1. Add '{host}:host-gateway' under extra_hosts in docker-compose.yml, then rebuild.\n"
                f"  2. Find the site's IP address and use that directly (e.g. http://192.168.x.x{display_port}/).\n"
                f"  3. If the site is on localhost, use http://host.docker.internal{display_port}/ instead."
            )

        # Private/RFC-1918 IP ranges — reachable directly, so this is likely a firewall or routing issue
        try:
            ip = ipaddress.ip_address(host)
            if ip.is_private:
                return (
                    f"Could not reach private IP '{host}' from the container. "
                    f"Ensure the container and the site are on the same Docker network, "
                    f"or that the host firewall allows inbound connections from the container subnet."
                )
        except ValueError:
            pass  # host is a hostname, not an IP

        # Public hostname or staging server — DNS should resolve, so it's likely a connectivity issue
        return (
            f"Could not connect to '{host}'. "
            f"If this is a staging or production server, check that the site is reachable from this machine. "
            f"If it is on your local network, verify the hostname resolves and the server is running."
        )

    # Not in Docker — give general guidance
    return (
        f"Could not connect to '{host}'. "
        f"Check that the site URL is correct and the WordPress site is running and accessible."
    )


def _html_plain_text(s: str) -> str:
    """Strip HTML tags and decode HTML entities; non-empty string in, plain text out."""
    stripped = re.sub(r"<[^>]*>", "", s).strip()
    return html.unescape(stripped)


def _strip_html(s: str | None) -> str | None:
    if s is None:
        return None
    return _html_plain_text(s)


def strip_html_tags(s: str | None) -> str:
    """Plain text from HTML-ish strings (titles, rendered excerpts)."""
    return _strip_html(s) or ""


@dataclass(frozen=True)
class WpRestAuth:
    base_url: str
    username: str
    application_password: str


class WpRestClient:
    def __init__(self, auth: WpRestAuth, *, timeout_seconds: int = DEFAULT_WP_REST_TIMEOUT_S) -> None:
        self._auth = auth
        self._timeout = timeout_seconds
        self._rate_limit_delay = DEFAULT_WP_REST_RATE_LIMIT_S

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=_wp_api_base(self._auth.base_url),
            auth=(self._auth.username, self._auth.application_password),
            timeout=httpx.Timeout(self._timeout),
            headers={"Accept": "application/json"},
        )

    def _wp_json_root_client(self) -> httpx.Client:
        """Client rooted at ``/wp-json`` for plugin REST namespaces (Redirection, Rank Math)."""
        return httpx.Client(
            base_url=_wp_json_root_base(self._auth.base_url),
            auth=(self._auth.username, self._auth.application_password),
            timeout=httpx.Timeout(self._timeout),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )

    def detect_seo_plugins(self) -> dict[str, bool]:
        """
        Probe WordPress REST namespaces for common SEO plugins (no manage_plugins required).
        """
        auth = (self._auth.username, self._auth.application_password)
        base = _normalize_base_url(self._auth.base_url)
        out: dict[str, bool] = {"yoast": False, "rank_math": False, "seopress": False}
        probes = [
            ("yoast", "yoast/v1"),
            ("rank_math", "rank-math/v1"),
            ("seopress", "seopress/v1"),
        ]
        for key, path in probes:
            try:
                url = f"{base}/wp-json/{path}"
                r = httpx.get(
                    url,
                    auth=auth,
                    timeout=httpx.Timeout(self._timeout),
                    headers={"Accept": "application/json"},
                )
                if r.status_code < 400:
                    out[key] = True
            except Exception:
                continue
        return out

    def list_all_plugins(self) -> dict[str, Any]:
        """
        List all installed plugins with their status.
        Tries the full /plugins endpoint first, then falls back to endpoint detection.
        
        Returns: {
            "installed": [...],
            "seo_plugins": [...],
            "redirect_plugins": [...],
            "error": null,
            "list_source": "wp_v2_plugins" | "endpoint_probe" | null
        }
        """
        plugins_list: dict[str, Any] = {
            "installed": [],
            "seo_plugins": [],
            "redirect_plugins": [],
            "error": None,
            "list_source": None,
        }
        
        try:
            with self._client() as c:
                # Try to list all plugins (requires manage_plugins capability)
                r = c.get("/plugins", params={"context": "edit", "per_page": 100})
                
                # If we have full plugin list, use it
                if r.status_code == 200:
                    plugins_data = r.json()
                    if isinstance(plugins_data, list):
                        plugins_list["list_source"] = "wp_v2_plugins"
                        for plugin in plugins_data:
                            if not isinstance(plugin, dict):
                                continue
                            
                            slug = plugin.get("plugin", "").lower()
                            title = plugin.get("name", "Unknown")
                            status = "active" if plugin.get("status") == "active" else "inactive"
                            
                            plugin_info = {
                                "name": slug,
                                "title": title,
                                "status": status,
                                "type": "other"
                            }
                            
                            # Categorize by plugin slug and title
                            slug_lower = slug.lower()
                            title_lower = title.lower()
                            
                            # Check for SEO plugins
                            seo_keywords = ["yoast", "rank-math", "rankmath", "seopress", "wp-seo", "wordpress-seo"]
                            is_seo = any(keyword in slug_lower or keyword in title_lower for keyword in seo_keywords)
                            if is_seo:
                                plugin_info["type"] = "seo"
                                if status == "active":
                                    plugins_list["seo_plugins"].append(plugin_info)
                            
                            # Check for redirect plugins - add ALL (active + inactive)
                            redirect_keywords = ["redirect", "safe-redirect", "301", "rewrite", "slug-redirect"]
                            is_redirect = any(keyword in slug_lower or keyword in title_lower for keyword in redirect_keywords)
                            if is_redirect:
                                plugin_info["type"] = "redirect"
                                plugins_list["redirect_plugins"].append(plugin_info)
                            
                            plugins_list["installed"].append(plugin_info)
                        return plugins_list
                
                # If permission denied or endpoint not available, use fallback detection
                if r.status_code in (401, 403, 404):
                    logger.info(
                        "Full plugin listing not available (HTTP %s), using endpoint detection fallback",
                        r.status_code,
                    )
                    plugins_list["list_source"] = "endpoint_probe"
                    detected = self.detect_active_plugins()
                    seo_probe = self.detect_seo_plugins()
                    
                    # Map detected plugins to the response format
                    if detected.get("redirection"):
                        plugins_list["redirect_plugins"].append({
                            "name": "redirection",
                            "title": "Redirection",
                            "status": "active",
                            "type": "redirect"
                        })
                    
                    if detected.get("rank_math_redirects"):
                        plugins_list["redirect_plugins"].append({
                            "name": "rank-math",
                            "title": "Rank Math",
                            "status": "active",
                            "type": "redirect"
                        })
                    
                    if detected.get("safe_redirect_manager"):
                        plugins_list["redirect_plugins"].append({
                            "name": "safe-redirect-manager",
                            "title": "Safe Redirect Manager",
                            "status": "active",
                            "type": "redirect"
                        })
                    
                    if seo_probe.get("yoast"):
                        plugins_list["seo_plugins"].append({
                            "name": "wordpress-seo/wordpress-seo.php",
                            "title": "Yoast SEO",
                            "status": "active",
                            "type": "seo",
                        })
                    if seo_probe.get("rank_math"):
                        plugins_list["seo_plugins"].append({
                            "name": "seo-by-rank-math/rank-math.php",
                            "title": "Rank Math SEO",
                            "status": "active",
                            "type": "seo",
                        })
                    if seo_probe.get("seopress"):
                        plugins_list["seo_plugins"].append({
                            "name": "wp-seopress/seopress.php",
                            "title": "SEOPress",
                            "status": "active",
                            "type": "seo",
                        })

                    if not plugins_list["redirect_plugins"]:
                        plugins_list["error"] = "No redirect plugins detected"

                    return plugins_list
                
                # Other HTTP errors
                plugins_list["list_source"] = None
                plugins_list["error"] = f"Failed to fetch plugins (HTTP {r.status_code})"
                logger.warning("Plugin listing returned %s", r.status_code)
                return plugins_list
                
        except Exception as e:
            plugins_list["error"] = f"Error fetching plugins: {str(e)}"
            logger.error("Failed to list plugins: %s", e)
        
        return plugins_list

    def detect_active_plugins(self) -> dict[str, bool]:
        """
        Detect which redirect plugins are active by trying their API endpoints directly.
        This is more reliable than checking /wp/v2/plugins (which requires manage_plugins capability).
        
        Returns dict like: {'redirection': True, 'rank_math_redirects': False, ...}
        """
        active_plugins = {}

        with self._wp_json_root_client() as c:
            # Try Redirection plugin endpoint - this is the most reliable method
            try:
                r = c.get("/redirection/v1/redirect", params={"per_page": 1})
                # 200 = plugin active and working
                # 403 = plugin active but permission denied
                # 404 = plugin not installed
                if r.status_code in (200, 403):
                    active_plugins["redirection"] = True
                    logger.debug("Redirection plugin detected (API endpoint responsive)")
                else:
                    logger.debug("Redirection plugin check returned %s", r.status_code)
            except Exception as e:
                logger.debug("Redirection plugin check failed: %s", e)

            # Try Rank Math redirects endpoint
            try:
                r = c.get("/rank-math/v1/redirects", params={"per_page": 1})
                if r.status_code in (200, 403):
                    active_plugins["rank_math_redirects"] = True
                    logger.debug("Rank Math redirects detected")
            except Exception as e:
                logger.debug("Rank Math redirects check failed: %s", e)

        # Safe Redirect Manager registers CPT at wp/v2/redirects (base …/wp-json/wp/v2 + path /redirects)
        with self._client() as c:
            try:
                r = c.get("/redirects", params={"per_page": 1})
                if r.status_code in (200, 403):
                    active_plugins["safe_redirect_manager"] = True
                    logger.debug("Safe Redirect Manager detected")
            except Exception as e:
                logger.debug("Safe Redirect Manager check failed: %s", e)
        
        # SEO plugins: reuse redirect namespace for Rank Math; probe others
        seo = self.detect_seo_plugins()
        active_plugins["rank_math"] = bool(
            active_plugins.get("rank_math_redirects") or seo.get("rank_math")
        )
        active_plugins["yoast"] = bool(seo.get("yoast"))
        active_plugins["seopress"] = bool(seo.get("seopress"))

        return active_plugins

    def health_check(self) -> None:
        try:
            with self._client() as c:
                r = c.get("/users/me")
                if r.status_code == 401:
                    raise RuntimeError(
                        f"Authentication failed (401). Check username ({self._auth.username!r}) and application password."
                    )
                elif r.status_code == 403:
                    raise RuntimeError(
                        "Access forbidden (403). User may lack REST API permissions."
                    )
                elif r.status_code == 404:
                    raise RuntimeError(
                        f"REST API endpoint not found (404). Site URL {self._auth.base_url!r} may not be a WordPress installation or have REST API disabled."
                    )
                r.raise_for_status()
        except httpx.ConnectError as e:
            hint = _connection_help(self._auth.base_url)
            msg = f"Unable to connect to {self._auth.base_url!r}."
            if hint:
                msg = f"{msg}\n{hint}"
            raise RuntimeError(msg) from e
        except httpx.UnsupportedProtocol as e:
            raise RuntimeError(
                f"Invalid site URL {self._auth.base_url!r}. Include http:// or https://"
            ) from e
        except httpx.TimeoutException as e:
            raise RuntimeError(
                f"Connection timed out to {self._auth.base_url!r}. Server may be slow or unreachable."
            ) from e
        except httpx.HTTPError as e:
            raise RuntimeError(f"REST API health check failed: {str(e)}") from e

    def get_post(self, post_id: int) -> dict[str, Any]:
        with self._client() as c:
            r = c.get(f"/posts/{post_id}", params={"context": "edit"})
            if r.status_code == 404:
                r = c.get(f"/pages/{post_id}", params={"context": "edit"})
            r.raise_for_status()
            return r.json()

    def update_post(
        self,
        post_id: int,
        *,
        title: str | None = None,
        content: str | None = None,
        meta_description: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if title is not None:
            payload["title"] = title
        if content is not None:
            payload["content"] = content
        
        # For meta description, we need to update the SEO plugin-specific field
        # This will be handled separately via _update_seo_meta_description
        # For now, we just create the main payload without meta_description
        # and return the updated post
        
        if not payload:
            return self.get_post(post_id)

        with self._client() as c:
            # Try PUT first (correct HTTP method), then POST as fallback
            r = c.put(f"/posts/{post_id}", json=payload)
            if r.status_code == 404:
                r = c.put(f"/pages/{post_id}", json=payload)
            
            # If PUT not supported, try POST
            if r.status_code in [405, 501]:  # Method Not Allowed or Not Implemented
                r = c.post(f"/posts/{post_id}", json=payload)
                if r.status_code == 404:
                    r = c.post(f"/pages/{post_id}", json=payload)
            
            r.raise_for_status()
            result = r.json()
            
            # Validate that the update was actually applied
            if not isinstance(result, dict) or result.get("id") != post_id:
                logger.warning("Update post %s returned unexpected response: %s", post_id, result)
            
            return result
    
    def _update_seo_meta_field(
        self,
        post_id: int,
        value: str,
        *,
        seo_plugin: str | None,
        profile: _SeoMetaRestProfile,
    ) -> dict[str, Any]:
        if not value:
            return self.get_post(post_id)
        try:
            post_obj = self.get_post(post_id)
        except Exception as e:
            logger.error("Failed to fetch post %s: %s", post_id, e)
            raise

        active_plugins = self.detect_active_plugins()
        selected_keys = _meta_plugin_keys_from_seo_selection(seo_plugin)
        meta_field_configs = list(profile.field_configs)

        if selected_keys:
            active_configs = [cfg for cfg in meta_field_configs if cfg["plugin"] in selected_keys]
        else:
            active_configs = [
                cfg for cfg in meta_field_configs
                if active_plugins.get(cfg["plugin"], False)
            ]
        active_configs.sort(key=lambda x: x["priority"])

        if not active_configs:
            active_configs = [
                cfg for cfg in meta_field_configs
                if active_plugins.get(cfg["plugin"], False)
            ]
            active_configs.sort(key=lambda x: x["priority"])

        rest_safe_configs = [cfg for cfg in active_configs if cfg["rest_safe"]]
        rest_unsafe_configs = [cfg for cfg in active_configs if not cfg["rest_safe"]]

        with self._client() as c:
            for config in rest_safe_configs:
                try:
                    meta_key = config["key"]
                    plugin_name = config["name"]
                    payload = {"meta": {meta_key: value}}
                    params = {"context": "edit"}
                    r = c.put(f"/posts/{post_id}", json=payload, params=params)
                    if r.status_code == 404:
                        r = c.put(f"/pages/{post_id}", json=payload, params=params)
                    if r.status_code < 400:
                        result = r.json()
                        logger.info(
                            profile.success_log_fmt.format(
                                post_id=post_id,
                                plugin_name=plugin_name,
                                meta_key=meta_key,
                            )
                        )
                        return result
                    if r.status_code == 403:
                        logger.debug("%s (%s): 403 Forbidden", plugin_name, meta_key)
                except Exception as e:
                    logger.debug(
                        profile.debug_fail_fmt.format(
                            plugin_name=config["name"],
                            exc=type(e).__name__,
                        )
                    )
                    continue

            for config in rest_unsafe_configs:
                try:
                    meta_key = config["key"]
                    plugin_name = config["name"]
                    payload = {"meta": {meta_key: value}}
                    params = {"context": "edit"}
                    r = c.put(f"/posts/{post_id}", json=payload, params=params)
                    if r.status_code == 404:
                        r = c.put(f"/pages/{post_id}", json=payload, params=params)
                    if r.status_code < 400:
                        result = r.json()
                        logger.info(
                            profile.success_log_fmt.format(
                                post_id=post_id,
                                plugin_name=plugin_name,
                                meta_key=meta_key,
                            )
                        )
                        return result
                    if r.status_code == 403:
                        logger.debug(
                            "%s (%s): 403 Forbidden (expected for private meta)",
                            plugin_name,
                            meta_key,
                        )
                except Exception as e:
                    logger.debug(
                        profile.debug_fail_private_fmt.format(
                            plugin_name=config["name"],
                            exc=type(e).__name__,
                        )
                    )
                    continue

            detected_seo_plugins = ", ".join(
                k.replace("_", " ").title()
                for k, v in active_plugins.items()
                if v and k in ["yoast", "rank_math", "seopress"]
            )
            if not detected_seo_plugins:
                logger.warning(profile.warn_no_plugin.format(post_id=post_id))
                return post_obj

            logger.warning(
                profile.warn_all_failed.format(
                    post_id=post_id, detected=detected_seo_plugins
                )
            )
            return post_obj

    def update_seo_meta_description(
        self,
        post_id: int,
        meta_description: str,
        *,
        seo_plugin: str | None = None,
    ) -> dict[str, Any]:
        """
        Update meta description using plugin-specific post meta fields.
        Handles Yoast, Rank Math, and SEOPress with proper REST API restrictions handling.

        When ``seo_plugin`` is set (REST `/wp/v2/plugins` file slug from the Run panel), only
        that plugin's meta keys are attempted; otherwise active plugins are auto-detected.

        IMPORTANT: Due to REST API restrictions on private meta fields (starting with _),
        direct meta updates may fail even with proper authentication and context=edit.
        This method tries REST API but returns clear status for fallback to WP-CLI.

        Supported plugins:
        - Yoast SEO (_yoast_wpseo_metadesc) - May require WP-CLI due to meta restrictions
        - Rank Math (rank_math_description) - Better REST API support
        - SEOPress (_seopress_titles_desc) - May require WP-CLI
        """
        return self._update_seo_meta_field(
            post_id,
            meta_description,
            seo_plugin=seo_plugin,
            profile=_SEO_META_DESC_PROFILE,
        )

    def update_seo_meta_title(
        self,
        post_id: int,
        meta_title: str,
        *,
        seo_plugin: str | None = None,
    ) -> dict[str, Any]:
        """
        Update SEO title (search snippet title) using plugin-specific post meta fields.
        Mirrors ``update_seo_meta_description`` for Yoast, Rank Math, and SEOPress.
        """
        return self._update_seo_meta_field(
            post_id,
            meta_title,
            seo_plugin=seo_plugin,
            profile=_SEO_META_TITLE_PROFILE,
        )

    def resolve_post_by_url(
        self, url: str, *, post_type: Literal["any", "post", "page"] = "any"
    ) -> tuple[dict[str, Any] | None, str]:
        """
        Best-effort REST-only resolver.
        Tries multiple strategies:
        1. Exact match on returned `link`
        2. Prefix match on `link`
        3. URL contains post/page ID in query params or slug
        """
        normalized = (url or "").strip()
        if not normalized:
            return None, "invalid_url"

        endpoints: list[str]
        if post_type == "post":
            endpoints = ["/posts"]
        elif post_type == "page":
            endpoints = ["/pages"]
        else:
            endpoints = ["/posts", "/pages"]

        # Try direct slug/URL matching
        def _url_has_non_root_path(u: str) -> bool:
            """True when the URL has a path segment beyond ``/`` (not bare origin)."""
            try:
                p = urlsplit((u or "").strip())
                return bool((p.path or "").strip("/"))
            except Exception:
                return False

        def _permalink_prefix_match(link: str, prefix_url: str) -> bool:
            """``link`` is under ``prefix_url`` as a URL path prefix (boundary at ``/``)."""
            a = link.rstrip("/")
            b = prefix_url.rstrip("/")
            if not a.startswith(b):
                return False
            if len(a) == len(b):
                return True
            return a[len(b)] == "/"

        with self._client() as c:
            for ep in endpoints:
                # Strategy 1: Search by URL
                r = c.get(ep, params={"search": normalized, "per_page": 100, "context": "view"})
                if r.status_code < 400:
                    items = r.json()
                    if isinstance(items, list):
                        # Exact match
                        for it in items:
                            link = it.get("link")
                            if isinstance(link, str) and link.rstrip("/") == normalized.rstrip("/"):
                                return it, f"rest_search_exact:{ep}"
                        # Prefix match only when the requested URL is not the site root.
                        # Otherwise ``https://site/`` is a prefix of every permalink and the first
                        # search hit (often the oldest post) is returned — wrong for the homepage.
                        if _url_has_non_root_path(normalized):
                            for it in items:
                                link = it.get("link")
                                if isinstance(link, str) and _permalink_prefix_match(
                                    link, normalized
                                ):
                                    return it, f"rest_search_prefix:{ep}"

                # Strategy 2: Try to extract ID from URL if it's p=XXX or post=XXX
                try:
                    parsed = urlparse(normalized)
                    query_params = parse_qs(parsed.query)
                    post_id = None
                    if "p" in query_params:
                        post_id = int(query_params["p"][0])
                    elif "post_id" in query_params:
                        post_id = int(query_params["post_id"][0])
                    
                    if post_id:
                        try:
                            obj = c.get(f"{ep}/{post_id}", params={"context": "view"})
                            if obj.status_code == 200:
                                return obj.json(), f"rest_url_param_id:{ep}"
                        except Exception:
                            pass
                except Exception:
                    pass

                # Strategy 3: Try to match by slug from URL path
                try:
                    slug = normalized.rstrip("/").split("/")[-1]
                    if slug and slug not in {"", "index.html", "index.php"}:
                        r = c.get(ep, params={"slug": slug, "per_page": 10, "context": "view"})
                        if r.status_code == 200:
                            items = r.json()
                            if isinstance(items, list) and len(items) > 0:
                                return items[0], f"rest_slug_match:{ep}"
                except Exception:
                    pass

        return None, "rest_search_none"

    @staticmethod
    def extract_summary_fields(post_obj: dict[str, Any]) -> dict[str, Any]:
        title = post_obj.get("title")
        rendered_title = title.get("rendered") if isinstance(title, dict) else None
        content = post_obj.get("content")
        rendered_content = content.get("rendered") if isinstance(content, dict) else None
        return {
            "id": post_obj.get("id"),
            "type": post_obj.get("type"),
            "link": post_obj.get("link"),
            "title": _strip_html(rendered_title),
            "content": rendered_content,
        }

    @staticmethod
    def is_effectively_empty_post_content(html: str | None) -> bool:
        """True when post body has no visible text (empty or builder-only pages)."""
        if not html or not str(html).strip():
            return True
        return not bool(strip_html_tags(html).strip())

    @staticmethod
    def first_h1_inner_text(html: str | None) -> str | None:
        if not html:
            return None
        m = re.search(r"<h1\b[^>]*>(.*?)</h1>", html, re.IGNORECASE | re.DOTALL)
        if not m:
            return None
        return strip_html_tags(m.group(1)) or None

    @staticmethod
    def replace_first_h1_inner(html_content: str, new_inner_plain: str) -> tuple[str, bool]:
        """Replace the inner HTML of the first <h1>. Plain text is HTML-escaped."""
        import html as html_module
        inner = html_module.escape(new_inner_plain, quote=False)

        def repl(m: re.Match[str]) -> str:
            return f"{m.group(1)}{inner}{m.group(3)}"

        new_html, n = re.subn(
            r"(<h1\b[^>]*>)(.*?)(</h1>)",
            repl,
            html_content,
            count=1,
            flags=re.IGNORECASE | re.DOTALL,
        )
        return new_html, n > 0

    def get_media(self, media_id: int) -> dict[str, Any]:
        """Get media item (image) details."""
        with self._client() as c:
            r = c.get(f"/media/{media_id}", params={"context": "edit"})
            r.raise_for_status()
            return r.json()

    def search_media_by_url(self, image_url: str) -> int | None:
        """
        Search for media ID by image URL.
        Tries multiple strategies:
        1. Exact URL match (with/without size variants)
        2. Filename-based search
        3. All media library search (fallback)
        Returns the media ID if found, None otherwise.
        """
        if not image_url:
            return None

        with self._client() as c:
            # Strategy 1: Get all media and search through them (more reliable)
            # Fetch all media in chunks
            per_page = 100
            for page in range(1, 10):  # Check up to 1000 items
                try:
                    r = c.get("/media", params={"per_page": per_page, "page": page, "context": "edit"})
                    if r.status_code >= 400:
                        break
                    items = r.json()
                    if not isinstance(items, list) or len(items) == 0:
                        break

                    # Try exact match first
                    for item in items:
                        source_url = item.get("source_url", "")
                        if source_url and source_url.rstrip("/") == image_url.rstrip("/"):
                            return item.get("id")

                    # Try variations (with/without size suffix)
                    image_lower = image_url.lower()
                    for item in items:
                        source_url = item.get("source_url", "").lower()
                        if source_url:
                            # Check if URLs contain each other (handling size variants)
                            if image_lower in source_url or source_url in image_lower:
                                return item.get("id")
                except Exception:
                    break

            # Strategy 2: Search by filename/title
            try:
                parsed = urlparse(image_url)
                filename = parsed.path.split("/")[-1]
                if filename and "." in filename:
                    # Remove size suffix like -1024x683
                    base_name = re.sub(r'-\d+x\d+', '', filename)
                    r = c.get(
                        "/media",
                        params={"search": base_name, "per_page": 100, "context": "edit"},
                    )
                    if r.status_code == 200:
                        items = r.json()
                        if isinstance(items, list):
                            for item in items:
                                source_url = item.get("source_url", "")
                                if source_url and (filename in source_url or base_name in source_url):
                                    return item.get("id")
                            # Return first match if we have any
                            if len(items) > 0:
                                return items[0].get("id")
            except Exception:
                pass

        return None

    def update_media_alt_text(
        self,
        media_id: int,
        *,
        alt_text: str,
    ) -> dict[str, Any]:
        """Update alt text for a media attachment."""
        payload = {"alt_text": alt_text}
        with self._client() as c:
            r = c.post(f"/media/{media_id}", json=payload)
            r.raise_for_status()
            return r.json()

    def create_redirect(self, from_url: str, to_url: str, preferred_plugin: str | None = None) -> dict[str, Any]:
        """
        Create a 301 redirect using REST API with correct payload formats.
        
        According to https://redirection.me/developer/rest-api/
        Correct Redirection plugin API format:
        POST /wp-json/redirection/v1/redirect with:
        {
            "source": "/old-path/",
            "target": "/new-path/",
            "regex": false,
            "match_type": "url",
            "status_code": 301,
            "group_id": 1,
            "enabled": true
        }
        
        Strategy:
        - If ``preferred_plugin`` matches a REST-capable plugin, only that backend is used.
        - If it matches EPS / "301 Redirects" (no REST), returns ``status: failed`` with guidance.
        - If unset, tries Redirection, Rank Math, then Safe Redirect Manager.
        
        Returns: {status: "created|failed", message: "", id: redirect_id or None, ...}
        """
        from_url = from_url.strip()
        to_url = to_url.strip()

        backends = _redirect_rest_backends(preferred_plugin)
        if not backends:
            error_msg = (
                "Selected redirect plugin has no REST API. "
                "Add WP admin password + URL in Run panel to use UI automation, or choose Redirection / Rank Math / Safe Redirect Manager."
            )
            logger.info(error_msg)
            return {
                "status": "failed",
                "error": error_msg,
                "from_url": from_url,
                "to_url": to_url,
                "message": error_msg,
            }

        # Normalize source URL - keep leading slash for paths
        if from_url and not from_url.startswith("/"):
            from_url = "/" + from_url

        for backend in backends:
            if backend == "redirection":
                try:
                    time.sleep(self._rate_limit_delay)
                    with self._wp_json_root_client() as c:
                        r = c.post(
                            "/redirection/v1/redirect",
                            json=_redirection_plugin_create_payload(from_url, to_url),
                            timeout=10.0,
                        )

                    if 200 <= r.status_code < 300:
                        result = r.json()
                        redirect_id = result.get("id") if isinstance(result, dict) else None
                        logger.info(
                            "Redirection: created redirect from=%s to=%s id=%s",
                            from_url,
                            to_url,
                            redirect_id,
                        )
                        return {
                            "status": "created",
                            "plugin": "Redirection",
                            "id": redirect_id,
                            "from_url": from_url,
                            "to_url": to_url,
                            "message": "Redirect created successfully via Redirection plugin",
                        }
                    elif r.status_code == 403:
                        logger.warning("Redirection plugin API returned 403 (permission denied)")
                    elif r.status_code == 404:
                        logger.debug("Redirection plugin API endpoint not found (404)")
                    else:
                        logger.warning("Redirection plugin API returned %s", r.status_code)
                except Exception as e:
                    logger.debug(
                        "Redirection plugin attempt failed: %s: %s",
                        type(e).__name__,
                        e,
                    )

            elif backend == "rank_math":
                try:
                    time.sleep(self._rate_limit_delay)
                    with self._wp_json_root_client() as c:
                        r = c.post(
                            "/rank-math/v1/redirects",
                            json=_rank_math_redirect_create_payload(from_url, to_url),
                            timeout=10.0,
                        )

                    if 200 <= r.status_code < 300:
                        result = r.json()
                        redirect_id = result.get("id") if isinstance(result, dict) else None
                        logger.info(
                            "Rank Math: created redirect from=%s to=%s id=%s",
                            from_url,
                            to_url,
                            redirect_id,
                        )
                        return {
                            "status": "created",
                            "plugin": "Rank Math",
                            "id": redirect_id,
                            "from_url": from_url,
                            "to_url": to_url,
                            "message": "Redirect created successfully via Rank Math",
                        }
                except Exception as e:
                    logger.debug("Rank Math redirects API attempt failed: %s", e)

            elif backend == "safe_redirect_manager":
                try:
                    time.sleep(self._rate_limit_delay)
                    with self._client() as c:
                        r = c.post(
                            "/redirects",
                            json=_safe_redirect_manager_create_payload(from_url, to_url),
                            timeout=10.0,
                        )

                    if 200 <= r.status_code < 300:
                        result = r.json()
                        redirect_id = result.get("id") if isinstance(result, dict) else None
                        logger.info(
                            "Safe Redirect Manager: created redirect from=%s to=%s id=%s",
                            from_url,
                            to_url,
                            redirect_id,
                        )
                        return {
                            "status": "created",
                            "plugin": "Safe Redirect Manager",
                            "id": redirect_id,
                            "from_url": from_url,
                            "to_url": to_url,
                            "message": "Redirect created successfully via Safe Redirect Manager",
                        }
                except Exception as e:
                    logger.debug("Safe Redirect Manager API attempt failed: %s", e)

        error_msg = "No redirect plugin REST API succeeded for this request."
        logger.error(
            "Failed to create redirect from=%s to=%s: %s",
            from_url,
            to_url,
            error_msg,
        )

        return {
            "status": "failed",
            "error": error_msg,
            "from_url": from_url,
            "to_url": to_url,
            "message": error_msg,
        }


# --- WordPress REST SEO task client (full /wp-json base; standalone task helpers) ---
class TaskType(str, Enum):
    """All supported SEO automation tasks."""
    UPDATE_TITLE = "update_title"
    UPDATE_CONTENT = "update_content"
    UPDATE_ALT_TEXT = "update_alt_text"
    CLEANUP_URLS = "cleanup_urls"
    CREATE_REDIRECT = "create_redirect"


class RedirectSource(str, Enum):
    """Supported redirect management plugins."""
    REDIRECTION_PLUGIN = "redirection"
    RANK_MATH = "rank_math"
    SEOPRESS = "seopress"
    CUSTOM = "custom"


@dataclass
class DryRunResult:
    """Result of a dry-run operation."""
    task_type: TaskType
    resource_id: int | str
    current_state: dict[str, Any]
    proposed_changes: dict[str, Any]
    validation_passed: bool
    warnings: list[str]
    errors: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OperationResult:
    """Result of an actual operation."""
    task_type: TaskType
    resource_id: int | str
    success: bool
    message: str
    data: dict[str, Any] | None = None
    duration_ms: float = 0.0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class WordPressRESTClient:
    """Enhanced WordPress REST API client for SEO automation tasks."""

    def __init__(
        self,
        base_url: str,
        username: str,
        app_password: str,
        timeout_seconds: int = DEFAULT_WP_REST_TIMEOUT_S,
    ):
        """Initialize the REST API client.

        Args:
            base_url: WordPress site URL (e.g., https://example.com)
            username: WordPress username
            app_password: Application password (from WordPress settings)
            timeout_seconds: Request timeout
        """
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.app_password = app_password
        self.timeout = httpx.Timeout(timeout_seconds)
        self._rate_limit_delay = float(
            os.getenv("WP_REST_RATE_LIMIT_DELAY_SEC", str(DEFAULT_WP_REST_RATE_LIMIT_S))
        )

    def _get_client(self) -> httpx.Client:
        """Create an authenticated httpx client."""
        return httpx.Client(
            base_url=f"{self.base_url}/wp-json",
            auth=(self.username, self.app_password),
            timeout=self.timeout,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )

    def health_check(self) -> bool:
        """Verify connection and authentication."""
        try:
            with self._get_client() as client:
                resp = client.get("/wp/v2/users/me")
                resp.raise_for_status()
                return True
        except Exception as e:
            logger.error("Health check failed: %s", e)
            raise

    # ========================
    # POST/PAGE OPERATIONS
    # ========================

    def get_post(self, post_id: int) -> dict[str, Any]:
        """Get post/page details."""
        with self._get_client() as client:
            # Try posts first, then pages
            resp = client.get(f"/wp/v2/posts/{post_id}", params={"context": "edit"})
            if resp.status_code == 404:
                resp = client.get(f"/wp/v2/pages/{post_id}", params={"context": "edit"})
            resp.raise_for_status()
            return resp.json()

    def search_posts(
        self, query: str, post_type: Literal["post", "page", "any"] = "any"
    ) -> list[dict[str, Any]]:
        """Search for posts/pages by title or slug."""
        with self._get_client() as client:
            endpoints = []
            if post_type in ("post", "any"):
                endpoints.append("/wp/v2/posts")
            if post_type in ("page", "any"):
                endpoints.append("/wp/v2/pages")

            results = []
            for endpoint in endpoints:
                try:
                    resp = client.get(
                        endpoint,
                        params={"search": query, "per_page": 20, "context": "view"},
                    )
                    if resp.status_code == 200:
                        results.extend(resp.json())
                except Exception as e:
                    logger.warning("Search failed for %s: %s", endpoint, e)
            return results

    def resolve_post_by_url(self, url: str) -> int | None:
        """Resolve a post URL to its ID."""
        normalized_url = url.rstrip("/")
        results = self.search_posts(url)
        for item in results:
            item_url = item.get("link", "").rstrip("/")
            if item_url == normalized_url:
                return item.get("id")
        return None

    def update_post_title(self, post_id: int, new_title: str) -> DryRunResult:
        """Dry-run for title update."""
        current = self.get_post(post_id)
        current_title = current.get("title", {}).get("rendered", "")

        return DryRunResult(
            task_type=TaskType.UPDATE_TITLE,
            resource_id=post_id,
            current_state={"title": current_title},
            proposed_changes={"title": new_title},
            validation_passed=new_title.strip() != "",
            warnings=[
                f"Title will change from '{current_title}' to '{new_title}'"
            ],
            errors=[],
        )

    def apply_update_post_title(self, post_id: int, new_title: str) -> OperationResult:
        """Apply title update."""
        start_time = time.time()
        try:
            with self._get_client() as client:
                time.sleep(self._rate_limit_delay)
                resp = client.post(f"/wp/v2/posts/{post_id}", json={"title": new_title})
                if resp.status_code == 404:
                    resp = client.post(
                        f"/wp/v2/pages/{post_id}", json={"title": new_title}
                    )
                resp.raise_for_status()
                duration = (time.time() - start_time) * 1000

                return OperationResult(
                    task_type=TaskType.UPDATE_TITLE,
                    resource_id=post_id,
                    success=True,
                    message=f"Title updated to '{new_title}'",
                    duration_ms=duration,
                )
        except Exception as e:
            return OperationResult(
                task_type=TaskType.UPDATE_TITLE,
                resource_id=post_id,
                success=False,
                message="Title update failed",
                error=str(e),
                duration_ms=(time.time() - start_time) * 1000,
            )

    def update_post_content(self, post_id: int, new_content: str) -> DryRunResult:
        """Dry-run for content update."""
        current = self.get_post(post_id)
        current_content = current.get("content", {}).get("rendered", "")
        content_preview = new_content[:200] + "..." if len(new_content) > 200 else new_content

        return DryRunResult(
            task_type=TaskType.UPDATE_CONTENT,
            resource_id=post_id,
            current_state={"content_length": len(current_content)},
            proposed_changes={"content_preview": content_preview},
            validation_passed=new_content.strip() != "",
            warnings=[f"Content will change ({len(new_content)} characters)"],
            errors=[],
        )

    def apply_update_post_content(
        self, post_id: int, new_content: str
    ) -> OperationResult:
        """Apply content update."""
        start_time = time.time()
        try:
            with self._get_client() as client:
                time.sleep(self._rate_limit_delay)
                resp = client.post(f"/wp/v2/posts/{post_id}", json={"content": new_content})
                if resp.status_code == 404:
                    resp = client.post(
                        f"/wp/v2/pages/{post_id}", json={"content": new_content}
                    )
                resp.raise_for_status()
                duration = (time.time() - start_time) * 1000

                return OperationResult(
                    task_type=TaskType.UPDATE_CONTENT,
                    resource_id=post_id,
                    success=True,
                    message=f"Content updated ({len(new_content)} characters)",
                    duration_ms=duration,
                )
        except Exception as e:
            return OperationResult(
                task_type=TaskType.UPDATE_CONTENT,
                resource_id=post_id,
                success=False,
                message="Content update failed",
                error=str(e),
                duration_ms=(time.time() - start_time) * 1000,
            )

    # ========================
    # MEDIA/IMAGE OPERATIONS
    # ========================

    def get_media(self, media_id: int) -> dict[str, Any]:
        """Get media item details."""
        with self._get_client() as client:
            resp = client.get(f"/wp/v2/media/{media_id}", params={"context": "edit"})
            resp.raise_for_status()
            return resp.json()

    def search_media_by_url(self, image_url: str) -> int | None:
        """Find media ID by image URL."""
        with self._get_client() as client:
            resp = client.get(
                "/wp/v2/media",
                params={"search": image_url, "per_page": 20},
            )
            if resp.status_code == 200:
                items = resp.json()
                for item in items:
                    if item.get("source_url") == image_url:
                        return item.get("id")
        return None

    def update_image_alt_text(
        self, media_id: int, alt_text: str
    ) -> DryRunResult:
        """Dry-run for alt text update."""
        current = self.get_media(media_id)
        current_alt = current.get("alt_text", "")

        return DryRunResult(
            task_type=TaskType.UPDATE_ALT_TEXT,
            resource_id=media_id,
            current_state={"alt_text": current_alt},
            proposed_changes={"alt_text": alt_text},
            validation_passed=alt_text.strip() != "",
            warnings=[f"Alt text will change from '{current_alt}' to '{alt_text}'"],
            errors=[],
        )

    def apply_update_image_alt_text(
        self, media_id: int, alt_text: str
    ) -> OperationResult:
        """Apply alt text update."""
        start_time = time.time()
        try:
            with self._get_client() as client:
                time.sleep(self._rate_limit_delay)
                resp = client.post(
                    f"/wp/v2/media/{media_id}", json={"alt_text": alt_text}
                )
                resp.raise_for_status()
                duration = (time.time() - start_time) * 1000

                return OperationResult(
                    task_type=TaskType.UPDATE_ALT_TEXT,
                    resource_id=media_id,
                    success=True,
                    message=f"Alt text updated to '{alt_text}'",
                    duration_ms=duration,
                )
        except Exception as e:
            return OperationResult(
                task_type=TaskType.UPDATE_ALT_TEXT,
                resource_id=media_id,
                success=False,
                message="Alt text update failed",
                error=str(e),
                duration_ms=(time.time() - start_time) * 1000,
            )

    # ========================
    # URL CLEANUP OPERATIONS
    # ========================

    @staticmethod
    def find_urls_in_content(content: str) -> list[str]:
        """Extract all URLs from HTML content."""
        url_pattern = r'https?://[^\s<>"{}|\\^`\[\]]*'
        return re.findall(url_pattern, content)

    @staticmethod
    def replace_url_in_content(
        content: str, old_url: str, new_url: str
    ) -> tuple[str, int]:
        """Replace old URLs with new URLs in content.

        Returns:
            tuple: (updated_content, replacements_count)
        """
        # Escape special regex characters
        escaped_old = re.escape(old_url)
        updated, count = re.subn(escaped_old, new_url, content)
        return updated, count

    def cleanup_urls_in_post(
        self, post_id: int, replacements: dict[str, str]
    ) -> DryRunResult:
        """Dry-run for URL cleanup."""
        current = self.get_post(post_id)
        current_content = current.get("content", {}).get("rendered", "")
        updated_content = current_content

        changes_summary = []
        for old_url, new_url in replacements.items():
            updated_content, count = self.replace_url_in_content(
                updated_content, old_url, new_url
            )
            if count > 0:
                changes_summary.append(f"{old_url} → {new_url} ({count}x)")

        return DryRunResult(
            task_type=TaskType.CLEANUP_URLS,
            resource_id=post_id,
            current_state={"urls": self.find_urls_in_content(current_content)},
            proposed_changes={"url_replacements": replacements, "replacements": changes_summary},
            validation_passed=len(changes_summary) > 0,
            warnings=changes_summary,
            errors=[],
        )

    def apply_cleanup_urls_in_post(
        self, post_id: int, replacements: dict[str, str]
    ) -> OperationResult:
        """Apply URL cleanup."""
        start_time = time.time()
        try:
            current = self.get_post(post_id)
            content = current.get("content", {}).get("rendered", "")

            updated_content = content
            total_replacements = 0
            for old_url, new_url in replacements.items():
                updated_content, count = self.replace_url_in_content(
                    updated_content, old_url, new_url
                )
                total_replacements += count

            if total_replacements == 0:
                return OperationResult(
                    task_type=TaskType.CLEANUP_URLS,
                    resource_id=post_id,
                    success=False,
                    message="No URLs found to replace",
                    duration_ms=(time.time() - start_time) * 1000,
                )

            with self._get_client() as client:
                time.sleep(self._rate_limit_delay)
                resp = client.post(
                    f"/wp/v2/posts/{post_id}", json={"content": updated_content}
                )
                if resp.status_code == 404:
                    resp = client.post(
                        f"/wp/v2/pages/{post_id}", json={"content": updated_content}
                    )
                resp.raise_for_status()
                duration = (time.time() - start_time) * 1000

                return OperationResult(
                    task_type=TaskType.CLEANUP_URLS,
                    resource_id=post_id,
                    success=True,
                    message=f"URLs updated ({total_replacements} replacements)",
                    data={"replacements_count": total_replacements},
                    duration_ms=duration,
                )
        except Exception as e:
            return OperationResult(
                task_type=TaskType.CLEANUP_URLS,
                resource_id=post_id,
                success=False,
                message="URL cleanup failed",
                error=str(e),
                duration_ms=(time.time() - start_time) * 1000,
            )

    # ========================
    # REDIRECT OPERATIONS
    # ========================

    def create_redirect_dry_run(
        self, source_url: str, target_url: str, plugin: RedirectSource = RedirectSource.REDIRECTION_PLUGIN
    ) -> DryRunResult:
        """Dry-run for redirect creation."""
        warnings = []
        errors = []

        # Validate URLs
        if not source_url or not target_url:
            errors.append("Both source and target URLs are required")
        if source_url.rstrip("/") == target_url.rstrip("/"):
            errors.append("Source and target URLs cannot be the same")

        # Check for circular redirects
        if self._check_circular_redirect(target_url):
            warnings.append("Warning: Target URL appears to already redirect elsewhere")

        return DryRunResult(
            task_type=TaskType.CREATE_REDIRECT,
            resource_id=f"{source_url}",
            current_state={},
            proposed_changes={
                "source": source_url,
                "target": target_url,
                "status_code": 301,
                "plugin": plugin.value,
            },
            validation_passed=len(errors) == 0,
            warnings=warnings,
            errors=errors,
        )

    def _check_circular_redirect(self, url: str) -> bool:
        """Check if URL already has an active redirect (preventing circular chains)."""
        try:
            with self._get_client() as client:
                resp = client.get("/redirection/v1/redirect", params={"search": url})
                if resp.status_code == 200:
                    items = resp.json()
                    return len(items) > 0 if isinstance(items, list) else False
        except Exception:
            pass
        return False

    def apply_create_redirect_redirection_plugin(
        self, source_url: str, target_url: str
    ) -> OperationResult:
        """Apply redirect using Redirection plugin."""
        start_time = time.time()
        try:
            with self._get_client() as client:
                time.sleep(self._rate_limit_delay)
                resp = client.post(
                    "/redirection/v1/redirect",
                    json=_redirection_plugin_create_payload(source_url, target_url),
                )
                resp.raise_for_status()
                duration = (time.time() - start_time) * 1000

                return OperationResult(
                    task_type=TaskType.CREATE_REDIRECT,
                    resource_id=f"{source_url}",
                    success=True,
                    message=f"Redirect created: {source_url} → {target_url}",
                    data=resp.json(),
                    duration_ms=duration,
                )
        except Exception as e:
            return OperationResult(
                task_type=TaskType.CREATE_REDIRECT,
                resource_id=f"{source_url}",
                success=False,
                message="Redirect creation failed",
                error=str(e),
                duration_ms=(time.time() - start_time) * 1000,
            )

    def apply_create_redirect_rank_math(
        self, source_url: str, target_url: str
    ) -> OperationResult:
        """Apply redirect using Rank Math."""
        start_time = time.time()
        try:
            with self._get_client() as client:
                time.sleep(self._rate_limit_delay)
                resp = client.post(
                    "/rank-math/v1/redirects",
                    json=_rank_math_redirect_create_payload(source_url, target_url),
                )
                resp.raise_for_status()
                duration = (time.time() - start_time) * 1000

                return OperationResult(
                    task_type=TaskType.CREATE_REDIRECT,
                    resource_id=f"{source_url}",
                    success=True,
                    message=f"Rank Math redirect created: {source_url} → {target_url}",
                    data=resp.json(),
                    duration_ms=duration,
                )
        except Exception as e:
            return OperationResult(
                task_type=TaskType.CREATE_REDIRECT,
                resource_id=f"{source_url}",
                success=False,
                message="Rank Math redirect creation failed",
                error=str(e),
                duration_ms=(time.time() - start_time) * 1000,
            )
