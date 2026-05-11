"""
WordPress REST API client with support for all SEO automation tasks.
Handles: posts, pages, media, redirects, and URL cleanup.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, asdict
from enum import Enum
from typing import Any, Literal
from urllib.parse import urlparse, urljoin

import httpx

logger = logging.getLogger(__name__)


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
        timeout_seconds: int = 30,
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
        self._rate_limit_delay = 0.5  # seconds between requests

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
            logger.error(f"Health check failed: {e}")
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
                    logger.warning(f"Search failed for {endpoint}: {e}")
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
                    json={
                        "source": source_url,
                        "target": target_url,
                        "status_code": 301,
                    },
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
                    "/rankmath/v1/redirects",
                    json={
                        "sources": [{"source": source_url}],
                        "destination": target_url,
                        "type": "301",
                    },
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
