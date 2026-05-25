"""
Shared HTML scraper skill.
Fetches a page and extracts meta/title/h1 without authentication.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx
from bs4 import BeautifulSoup

from config import HTTP_TIMEOUT


@dataclass
class PageData:
    """Parsed data extracted from an HTML page."""

    url: str
    status_code: int
    title: str | None = None
    meta_description: str | None = None
    h1: str | None = None
    # All h1 tags found (in order)
    all_h1: list[str] = field(default_factory=list)
    error: str | None = None


async def fetch_page_data(url: str) -> PageData:
    """
    Perform an HTTP GET on `url` (no auth) and parse:
      - <title> tag text
      - <meta name="description" content="...">
      - First <h1> tag text
      - All <h1> tags

    Returns a PageData dataclass. Never raises — on any error sets PageData.error.
    """
    page = PageData(url=url, status_code=0)
    try:
        async with httpx.AsyncClient(
            timeout=HTTP_TIMEOUT,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; QAAgent/1.0; +https://qa-agent)"
                ),
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
        ) as client:
            response = await client.get(url)
            page.status_code = response.status_code
            response.raise_for_status()

            # Detect redirect to WP login page (HTTP 200 after redirect)
            final_url = str(response.url)
            if "wp-login" in final_url or "action=login" in final_url:
                page.error = f"Page requires authentication (redirected to: {final_url})"
                return page

            soup = BeautifulSoup(response.text, "html.parser")

            # <title>
            title_tag = soup.find("title")
            if title_tag:
                page.title = title_tag.get_text(strip=True)

            # <meta name="description">
            meta_desc = soup.find("meta", attrs={"name": "description"})
            if meta_desc and meta_desc.get("content"):
                page.meta_description = meta_desc["content"].strip()

            # All <h1> tags
            h1_tags = soup.find_all("h1")
            page.all_h1 = [tag.get_text(strip=True) for tag in h1_tags]
            if page.all_h1:
                page.h1 = page.all_h1[0]

    except httpx.HTTPStatusError as exc:
        page.status_code = exc.response.status_code
        page.error = f"HTTP {exc.response.status_code}: {exc.response.reason_phrase}"
    except Exception as exc:
        page.error = str(exc)

    return page


def is_empty_html_body(html_content: str | None) -> bool:
    """True when HTML has no visible text (empty post content)."""
    if not html_content or not html_content.strip():
        return True
    soup = BeautifulSoup(html_content, "html.parser")
    return not bool(soup.get_text(strip=True))


def extract_h1_from_html(html_content: str) -> str | None:
    """
    Extract the first <h1> text from an HTML string (e.g. from WP REST rendered content).
    Returns None if no <h1> found.
    """
    if not html_content:
        return None
    soup = BeautifulSoup(html_content, "html.parser")
    h1 = soup.find("h1")
    if h1:
        return h1.get_text(strip=True)
    return None
