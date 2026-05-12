"""
Report builder skill.
Builds a human-readable (rich-colorised) + JSON QA report.
"""

from __future__ import annotations

from datetime import datetime, timezone

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

from models import QAReport, QARowResult, QASubagentReport


console = Console()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_report(
    subagent_reports: list[QASubagentReport],
    site_url: str,
    execution_id: str | None = None,
) -> QAReport:
    """
    Aggregate subagent reports into a top-level QAReport.
    Also populates the human-readable summary_lines.
    """
    total_rows = sum(r.total for r in subagent_reports)
    total_passed = sum(r.passed for r in subagent_reports)
    total_failed = sum(r.failed for r in subagent_reports)
    total_errors = sum(r.errors for r in subagent_reports)

    summary_lines = _build_summary_lines(
        subagent_reports, total_rows, total_passed, total_failed, total_errors
    )

    return QAReport(
        execution_id=execution_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        site_url=site_url,
        total_rows_checked=total_rows,
        total_passed=total_passed,
        total_failed=total_failed,
        total_errors=total_errors,
        subagent_reports=subagent_reports,
        summary_lines=summary_lines,
    )


def print_report(report: QAReport) -> None:
    """
    Print a rich-colourised report to the terminal.
    """
    console.print()
    console.print(
        Panel.fit(
            f"[bold cyan]QA Report[/bold cyan]  |  [dim]{report.site_url}[/dim]\n"
            f"[dim]Timestamp: {report.timestamp}[/dim]"
            + (f"\n[dim]Execution ID: {report.execution_id}[/dim]" if report.execution_id else ""),
            border_style="cyan",
        )
    )

    # Overall totals
    overall_color = "green" if report.total_failed == 0 and report.total_errors == 0 else "red"
    console.print(
        f"\n[bold {overall_color}]Overall:[/bold {overall_color}] "
        f"[green]{report.total_passed} passed[/green]  "
        f"[red]{report.total_failed} failed[/red]  "
        f"[yellow]{report.total_errors} errors[/yellow]  "
        f"[dim]{report.total_rows_checked} total checked[/dim]\n"
    )

    # Per-subagent breakdown
    for sub in report.subagent_reports:
        _print_subagent_report(sub)

    # Summary lines
    if report.summary_lines:
        console.print(Panel("\n".join(report.summary_lines), title="Summary", border_style="blue"))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _print_subagent_report(sub: QASubagentReport) -> None:
    """Render one subagent report as a rich table."""
    status_color = "green" if sub.failed == 0 and sub.errors == 0 else "red"
    title = (
        f"[bold {status_color}]{sub.action_type.upper()}[/bold {status_color}]  "
        f"[green]{sub.passed}/{sub.total} passed[/green]"
        + (f"  [red]{sub.failed} failed[/red]" if sub.failed else "")
        + (f"  [yellow]{sub.errors} errors[/yellow]" if sub.errors else "")
    )

    table = Table(
        box=box.SIMPLE,
        show_header=True,
        header_style="bold dim",
        title=title,
        title_justify="left",
        expand=False,
    )
    table.add_column("Row", style="dim", width=5)
    table.add_column("Status", width=8)
    table.add_column("Method", width=14)
    table.add_column("Expected", overflow="fold", max_width=45)
    table.add_column("Actual", overflow="fold", max_width=45)
    table.add_column("Error / Info", overflow="fold", max_width=40)

    for row in sub.rows:
        table.add_row(
            str(row.row_index),
            _status_cell(row),
            row.method or "",
            _truncate(row.expected, 45),
            _truncate(row.actual, 45),
            _error_or_extra(row),
        )

    console.print(table)
    console.print()


def _status_cell(row: QARowResult) -> str:
    if row.error and not row.verified:
        return "[yellow]ERROR[/yellow]"
    return "[green]PASS[/green]" if row.verified else "[red]FAIL[/red]"


def _error_or_extra(row: QARowResult) -> str:
    if row.error:
        return f"[yellow]{_truncate(row.error, 40)}[/yellow]"
    if row.extra:
        # Show first meaningful key
        for key in ("layer1_found", "old_url_absent", "new_url_present", "note"):
            if key in row.extra:
                return f"[dim]{key}={row.extra[key]}[/dim]"
        return f"[dim]{list(row.extra.keys())[0]}=...[/dim]"
    return ""


def _truncate(value: str | None, max_len: int) -> str:
    if value is None:
        return ""
    if len(value) <= max_len:
        return value
    return value[: max_len - 3] + "..."


def _build_summary_lines(
    subagent_reports: list[QASubagentReport],
    total: int,
    passed: int,
    failed: int,
    errors: int,
) -> list[str]:
    lines: list[str] = []
    lines.append(f"Total rows checked : {total}")
    lines.append(f"Passed             : {passed}")
    lines.append(f"Failed             : {failed}")
    lines.append(f"Errors             : {errors}")
    lines.append("")

    for sub in subagent_reports:
        indicator = "OK" if sub.failed == 0 and sub.errors == 0 else "ISSUES"
        lines.append(
            f"  [{indicator:6s}] {sub.action_type:<20s}  "
            f"{sub.passed}/{sub.total} passed"
            + (f", {sub.failed} failed" if sub.failed else "")
            + (f", {sub.errors} errors" if sub.errors else "")
        )

        # List individual failures
        for row in sub.rows:
            if not row.verified or row.error:
                prefix = "    ERROR" if row.error else "    FAIL "
                detail = row.error or f"expected={_truncate(row.expected, 40)!r}  actual={_truncate(row.actual, 40)!r}"
                lines.append(f"{prefix}  row {row.row_index}: {detail}")

    if failed == 0 and errors == 0:
        lines.append("")
        lines.append("All checks passed.")

    return lines
