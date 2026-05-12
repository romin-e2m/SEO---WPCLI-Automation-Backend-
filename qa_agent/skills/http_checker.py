"""
HTTP HEAD checker skill.
Performs HEAD requests to verify HTTP redirects without following them.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from config import HTTP_TIMEOUT


@dataclass
class RedirectCheckResult:
    """Result of an HTTP redirect check."""

    url: str
    status_code: int | None = None
    location_header: str | None = None
    is_301: bool = False
    location_matches: bool = False
    error: str | None = None


async def check_redirect(source_url: str, expected_destination: str) -> RedirectCheckResult:
    """
    Issue an HTTP HEAD to `source_url` WITHOUT following redirects.

    Checks:
      - Status code == 301
      - Location header matches `expected_destination` (case-insensitive, trailing-slash tolerant)

    Returns a RedirectCheckResult. Never raises.
    """
    result = RedirectCheckResult(url=source_url)
    try:
        async with httpx.AsyncClient(
            timeout=HTTP_TIMEOUT,
            follow_redirects=False,
        ) as client:
            response = await client.head(source_url)
            result.status_code = response.status_code
            result.is_301 = response.status_code == 301

            location = response.headers.get("location")
            if location:
                result.location_header = location
                result.location_matches = _urls_match(location, expected_destination)

    except Exception as exc:
        result.error = str(exc)

    return result


def _urls_match(actual: str, expected: str) -> bool:
    """
    Compare two URLs with tolerance for:
    - Trailing slashes
    - http vs https (not normalised — exact scheme expected from automation)

    Returns True if they are equivalent.
    """
    def normalise(url: str) -> str:
        return url.strip().rstrip("/").lower()

    return normalise(actual) == normalise(expected)
