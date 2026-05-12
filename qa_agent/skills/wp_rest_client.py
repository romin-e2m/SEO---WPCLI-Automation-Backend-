"""
Shared WordPress REST API GET helper.
Only performs read operations — no write ops.
"""

from __future__ import annotations

from typing import Any

import httpx

from config import HTTP_TIMEOUT


class WPRestClient:
    """
    Lightweight async client for WordPress REST API GET requests.

    Usage:
        async with WPRestClient(base_url, username, app_password) as client:
            post = await client.get_post_or_page(post_id)
            media = await client.get_media(attachment_id)
    """

    def __init__(self, base_url: str, username: str, app_password: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._auth = httpx.BasicAuth(username, app_password)
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "WPRestClient":
        self._client = httpx.AsyncClient(
            auth=self._auth,
            timeout=HTTP_TIMEOUT,
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._client is not None:
            await self._client.aclose()

    def _api(self, path: str) -> str:
        """Build a full WP REST API URL."""
        return f"{self.base_url}/wp-json{path}"

    async def get_post(self, post_id: int) -> dict[str, Any]:
        """
        Fetch a post by ID with context=edit.
        Raises httpx.HTTPStatusError on non-2xx.
        """
        assert self._client is not None, "Use as async context manager"
        url = self._api(f"/wp/v2/posts/{post_id}?context=edit")
        response = await self._client.get(url)
        response.raise_for_status()
        return response.json()

    async def get_page(self, page_id: int) -> dict[str, Any]:
        """
        Fetch a page by ID with context=edit.
        Raises httpx.HTTPStatusError on non-2xx.
        """
        assert self._client is not None, "Use as async context manager"
        url = self._api(f"/wp/v2/pages/{page_id}?context=edit")
        response = await self._client.get(url)
        response.raise_for_status()
        return response.json()

    async def get_post_or_page(self, post_id: int) -> dict[str, Any]:
        """
        Try to fetch as a post first; if 404, try as a page.
        Raises httpx.HTTPStatusError if both fail.
        """
        assert self._client is not None, "Use as async context manager"
        try:
            return await self.get_post(post_id)
        except httpx.HTTPStatusError as post_err:
            if post_err.response.status_code == 404:
                return await self.get_page(post_id)
            raise

    async def get_media(self, attachment_id: int) -> dict[str, Any]:
        """
        Fetch a media item by ID with context=edit.
        Raises httpx.HTTPStatusError on non-2xx.
        """
        assert self._client is not None, "Use as async context manager"
        url = self._api(f"/wp/v2/media/{attachment_id}?context=edit")
        response = await self._client.get(url)
        response.raise_for_status()
        return response.json()

    async def get_redirection_redirects(self, per_page: int = 100) -> list[dict[str, Any]]:
        """
        Fetch redirects from the Redirection plugin REST endpoint.
        Returns empty list on error (endpoint may not exist on all sites).
        """
        assert self._client is not None, "Use as async context manager"
        url = self._api(f"/redirection/v1/redirect?per_page={per_page}")
        try:
            response = await self._client.get(url)
            response.raise_for_status()
            data = response.json()
            # Redirection plugin wraps results in {"items": [...]}
            if isinstance(data, dict) and "items" in data:
                return data["items"]
            if isinstance(data, list):
                return data
            return []
        except Exception:
            return []

    async def get_rankmath_redirects(self) -> list[dict[str, Any]]:
        """
        Fetch redirects from the Rank Math REST endpoint.
        Returns empty list on error (endpoint may not exist on all sites).
        """
        assert self._client is not None, "Use as async context manager"
        url = self._api("/rank-math/v1/redirects")
        try:
            response = await self._client.get(url)
            response.raise_for_status()
            data = response.json()
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                # Some versions return {"redirects": [...]}
                for key in ("redirects", "data", "items"):
                    if key in data and isinstance(data[key], list):
                        return data[key]
            return []
        except Exception:
            return []
