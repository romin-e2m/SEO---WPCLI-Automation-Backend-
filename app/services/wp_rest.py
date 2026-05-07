from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

import httpx


def _normalize_base_url(base_url: str) -> str:
    base_url = (base_url or "").strip()
    return base_url.rstrip("/")


def _wp_api_base(base_url: str) -> str:
    return f"{_normalize_base_url(base_url)}/wp-json/wp/v2"


def _strip_html(s: str | None) -> str | None:
    if s is None:
        return None
    return re.sub(r"<[^>]*>", "", s).strip()


@dataclass(frozen=True)
class WpRestAuth:
    base_url: str
    username: str
    application_password: str


class WpRestClient:
    def __init__(self, auth: WpRestAuth, *, timeout_seconds: int = 20) -> None:
        self._auth = auth
        self._timeout = timeout_seconds

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=_wp_api_base(self._auth.base_url),
            auth=(self._auth.username, self._auth.application_password),
            timeout=httpx.Timeout(self._timeout),
            headers={"Accept": "application/json"},
        )

    def health_check(self) -> None:
        # A cheap authenticated call: current user.
        with self._client() as c:
            r = c.get("/users/me")
            r.raise_for_status()

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
        title: str | None,
        content: str | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if title is not None:
            payload["title"] = title
        if content is not None:
            payload["content"] = content
        if not payload:
            return self.get_post(post_id)

        # Try posts then pages; if type doesn't match id.
        with self._client() as c:
            r = c.post(f"/posts/{post_id}", json=payload)
            if r.status_code == 404:
                r = c.post(f"/pages/{post_id}", json=payload)
            r.raise_for_status()
            return r.json()

    def resolve_post_by_url(
        self, url: str, *, post_type: Literal["any", "post", "page"] = "any"
    ) -> tuple[dict[str, Any] | None, str]:
        """
        Best-effort REST-only resolver.
        Prefers an exact match on returned `link`.
        """
        normalized = (url or "").strip()
        if not normalized:
            return None, "invalid_url"

        # Use search across endpoints and match by link.
        endpoints: list[str]
        if post_type == "post":
            endpoints = ["/posts"]
        elif post_type == "page":
            endpoints = ["/pages"]
        else:
            endpoints = ["/posts", "/pages"]

        with self._client() as c:
            for ep in endpoints:
                r = c.get(ep, params={"search": normalized, "per_page": 20, "context": "view"})
                if r.status_code >= 400:
                    continue
                items = r.json()
                if not isinstance(items, list):
                    continue
                # Exact link match first.
                for it in items:
                    link = it.get("link")
                    if isinstance(link, str) and link.rstrip("/") == normalized.rstrip("/"):
                        return it, f"rest_search_exact:{ep}"
                # Fallback: contains match.
                for it in items:
                    link = it.get("link")
                    if isinstance(link, str) and normalized.rstrip("/") in link.rstrip("/"):
                        return it, f"rest_search_contains:{ep}"

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

