"""
Subagent 6: redirect_verifier
Verifies 301 redirects via two layers.

Action type: "redirects_301"

Layer 1 (Plugin REST — optional confirmation):
  - If detail["plugin"] contains "Redirection": GET /wp-json/redirection/v1/redirect?per_page=100
  - If detail["plugin"] contains "Rank Math": GET /wp-json/rank-math/v1/redirects
  - Search results for matching source_url

Layer 2 (HTTP check — definitive):
  - httpx.head(detail["source_url"], follow_redirects=False, timeout=10)
  - Assert status_code == 301
  - Assert Location header matches detail["destination_url"]

verified = Layer2_passed (Layer1 is stored as extra info)
"""

from __future__ import annotations

from models import ExecuteRowResult, QARowResult, QASubagentReport
from skills.wp_rest_client import WPRestClient
from skills.http_checker import check_redirect


ACTION_TYPE = "redirects_301"


async def verify(
    rows: list[ExecuteRowResult],
    base_url: str,
    username: str,
    app_password: str,
) -> QASubagentReport:
    """
    Run redirect verification for all redirects_301 rows (outcome == "updated" only).
    Returns a QASubagentReport.
    """
    results: list[QARowResult] = []

    async with WPRestClient(base_url, username, app_password) as client:
        for row in rows:
            result = await _verify_row(row, client)
            results.append(result)

    passed = sum(1 for r in results if r.verified and not r.error)
    errors = sum(1 for r in results if r.error)
    failed = len(results) - passed - errors

    return QASubagentReport(
        action_type=ACTION_TYPE,
        total=len(results),
        passed=passed,
        failed=failed,
        errors=errors,
        rows=results,
    )


async def _verify_row(row: ExecuteRowResult, client: WPRestClient) -> QARowResult:
    """Verify a single redirects_301 row. Catches all exceptions."""
    base_result = QARowResult(
        action_type=ACTION_TYPE,
        row_index=row.row_index,
        sheet_name=row.sheet_name,
        verified=False,
    )

    try:
        detail = row.detail or {}
        source_url = detail.get("source_url", "")
        destination_url = detail.get("destination_url", "")

        if not source_url:
            base_result.error = "detail.source_url is missing — cannot verify"
            return base_result

        if not destination_url:
            base_result.error = "detail.destination_url is missing — cannot verify"
            return base_result

        base_result.expected = f"301 -> {destination_url}"

        # --- Layer 1: Plugin REST API (optional, bonus info) ---
        layer1_result: dict = {}
        plugin = detail.get("plugin", "")
        try:
            layer1_result = await _check_plugin_rest(client, plugin, source_url, destination_url)
        except Exception as exc:
            layer1_result = {"error": str(exc)}

        # --- Layer 2: HTTP HEAD check (definitive) ---
        http_result = await check_redirect(source_url, destination_url)

        base_result.method = "http_head"
        base_result.actual = (
            f"HTTP {http_result.status_code} -> {http_result.location_header or '(no Location)'}"
        )

        # verified is determined by Layer 2
        layer2_passed = http_result.is_301 and http_result.location_matches
        base_result.verified = layer2_passed

        if http_result.error:
            base_result.error = f"HTTP HEAD failed: {http_result.error}"

        base_result.extra = {
            "layer1_found": layer1_result.get("found"),
            "layer1_error": layer1_result.get("error"),
            "http_status_code": http_result.status_code,
            "http_location": http_result.location_header,
            "is_301": http_result.is_301,
            "location_matches": http_result.location_matches,
        }

        return base_result

    except Exception as exc:
        base_result.error = str(exc)
        return base_result


async def _check_plugin_rest(
    client: WPRestClient,
    plugin: str,
    source_url: str,
    destination_url: str,
) -> dict:
    """
    Query the redirect plugin REST endpoint and look for a matching redirect.
    Returns dict with "found" (bool | None), "matched_item" (dict | None), "error" (str | None).
    """
    plugin_lower = plugin.lower()
    redirects: list[dict] = []

    if "redirection" in plugin_lower:
        redirects = await client.get_redirection_redirects()
    elif "rank math" in plugin_lower or "rankmath" in plugin_lower:
        redirects = await client.get_rankmath_redirects()
    else:
        return {"found": None, "note": "Unknown plugin — skipped Layer 1"}

    if not redirects:
        return {"found": False, "note": "Plugin endpoint returned empty list"}

    # Search for a redirect whose source matches source_url
    normalised_source = source_url.strip().rstrip("/").lower()
    for item in redirects:
        # Different plugins use different field names
        item_source = (
            item.get("url")
            or item.get("source")
            or item.get("from")
            or item.get("source_url")
            or ""
        )
        item_dest = (
            (item.get("action_data") or {}).get("url", "")
            or item.get("destination")
            or item.get("to")
            or item.get("destination_url")
            or ""
        )
        if isinstance(item_dest, dict):
            item_dest = item_dest.get("url", "")

        if item_source.strip().rstrip("/").lower() == normalised_source:
            dest_matches = item_dest.strip().rstrip("/").lower() == destination_url.strip().rstrip("/").lower()
            return {
                "found": True,
                "dest_matches": dest_matches,
                "matched_item": {"source": item_source, "destination": item_dest},
            }

    return {"found": False, "note": "No matching redirect found in plugin data"}
