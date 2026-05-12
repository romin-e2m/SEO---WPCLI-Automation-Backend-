"""
QA Agent CLI entry point.

Usage:
  python main.py --execute-response execute_result.json --site https://mysite.com \\
                 --user admin --password "xxxx yyyy zzzz"

  python main.py --execute-response execute_result.json   # uses .env for site creds

  python main.py --help
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure qa_agent/ is on the Python path when run as a script
sys.path.insert(0, str(Path(__file__).parent))

import config as cfg
from orchestrator import run_qa
from skills.report_builder import print_report


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="qa_agent",
        description="QA agent that verifies WordPress automation changes were applied correctly.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Use credentials from .env
  python main.py --execute-response execute_result.json

  # Override credentials at CLI
  python main.py --execute-response execute_result.json \\
    --site https://example.com --user admin --password "xxxx yyyy zzzz"

  # Save JSON report to a specific path
  python main.py --execute-response execute_result.json --output my_report.json
        """,
    )
    parser.add_argument(
        "--execute-response",
        required=True,
        metavar="PATH",
        help="Path to the ExecuteResponse JSON file (output of POST /api/run/execute).",
    )
    parser.add_argument(
        "--site",
        metavar="URL",
        default=None,
        help="WordPress site root URL (overrides WP_REST_BASE_URL in .env).",
    )
    parser.add_argument(
        "--user",
        metavar="USERNAME",
        default=None,
        help="WordPress username (overrides WP_USERNAME in .env).",
    )
    parser.add_argument(
        "--password",
        metavar="APP_PASSWORD",
        default=None,
        help="WordPress Application Password (overrides WP_APP_PASSWORD in .env).",
    )
    parser.add_argument(
        "--output",
        metavar="PATH",
        default=None,
        help=(
            "Path to save the JSON report. "
            "Defaults to qa_report_<timestamp>.json in the current directory."
        ),
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        default=False,
        help="Do not save a JSON report file — only print to stdout.",
    )
    return parser.parse_args()


def _resolve_credentials(args: argparse.Namespace) -> tuple[str, str, str]:
    """
    Resolve site URL, username, and app password from CLI args or env vars.
    Exits with an error message if any required value is missing.
    """
    site = args.site or cfg.WP_REST_BASE_URL
    user = args.user or cfg.WP_USERNAME
    password = args.password or cfg.WP_APP_PASSWORD

    missing = []
    if not site:
        missing.append("--site / WP_REST_BASE_URL")
    if not user:
        missing.append("--user / WP_USERNAME")
    if not password:
        missing.append("--password / WP_APP_PASSWORD")

    if missing:
        print(
            f"[ERROR] Missing required credentials: {', '.join(missing)}\n"
            "Provide them via CLI flags or a .env file.",
            file=sys.stderr,
        )
        sys.exit(1)

    return site, user, password  # type: ignore[return-value]


def _load_execute_response(path_str: str) -> dict:
    """Load and parse the ExecuteResponse JSON file."""
    path = Path(path_str)
    if not path.exists():
        print(f"[ERROR] File not found: {path}", file=sys.stderr)
        sys.exit(1)

    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        print(f"[ERROR] Invalid JSON in {path}: {exc}", file=sys.stderr)
        sys.exit(1)


def _default_output_path() -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path(f"qa_report_{ts}.json")


async def _async_main(args: argparse.Namespace) -> int:
    """Async main — returns exit code."""
    site, user, password = _resolve_credentials(args)
    execute_response = _load_execute_response(args.execute_response)

    print(f"\nRunning QA checks against: {site}")
    print(f"Input file             : {args.execute_response}")
    total_rows = len(execute_response.get("rows", []))
    updated_rows = sum(
        1 for r in execute_response.get("rows", []) if r.get("outcome") == "updated"
    )
    print(f"Total rows in input    : {total_rows}")
    print(f"Rows with outcome=updated (will be checked): {updated_rows}\n")

    report = await run_qa(
        execute_response=execute_response,
        base_url=site,
        username=user,
        app_password=password,
    )

    # Print rich report to stdout
    print_report(report)

    # Save JSON report
    if not args.no_save:
        output_path = Path(args.output) if args.output else _default_output_path()
        with output_path.open("w", encoding="utf-8") as f:
            json.dump(report.model_dump(), f, indent=2, ensure_ascii=False)
        print(f"\nJSON report saved to: {output_path.resolve()}")

    # Exit with non-zero code if any checks failed or errored
    if report.total_failed > 0 or report.total_errors > 0:
        return 1
    return 0


def main() -> None:
    args = _parse_args()
    exit_code = asyncio.run(_async_main(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
