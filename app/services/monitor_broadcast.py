"""Push high-level steps to the live browser monitor (shared ExecutionLogger registry)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.services.execution_log_registry import append_log_entry, get as get_execution_logger
from app.services.monitor_context import current_monitor_execution_id


def _emit(action: str, status: str, details: str | None) -> None:
    """Single monitor log line — uses registered ExecutionLogger when present."""
    execution_id = current_monitor_execution_id.get()
    registered = get_execution_logger(execution_id)
    if registered is not None:
        registered.log_sync(action, status, details)
        return
    append_log_entry(
        execution_id,
        {
            "id": f"log_{uuid.uuid4().hex[:12]}",
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "action": action,
            "status": status,
            **({"details": details} if details else {}),
        },
    )


MONITOR_DEFAULT_ID = "default"


def clear_monitor_logs() -> None:
    from app.services.execution_log_registry import clear_logs

    clear_logs(current_monitor_execution_id.get())


def emit_monitor_phase(label: str, details: str | None = None) -> None:
    _emit(label, "success", details)


def emit_monitor_row_dry(
    action: str,
    sheet_name: str,
    row_index: int,
    outcome: str,
    message: str | None = None,
) -> None:
    st = _outcome_to_status(outcome)
    msg = message or ""
    _emit(
        f"Dry-run [{action}] {sheet_name} row {row_index}: {outcome}",
        st,
        msg[:2000] if msg else None,
    )


def emit_monitor_row_exec(
    action: str,
    sheet_name: str,
    row_index: int,
    outcome: str,
    message: str | None = None,
) -> None:
    st = _exec_outcome_to_status(outcome)
    msg = message or ""
    _emit(
        f"Execute [{action}] {sheet_name} row {row_index}: {outcome}",
        st,
        msg[:2000] if msg else None,
    )


def emit_monitor_playwright(action: str, level: str, details: str) -> None:
    st = _level_to_status(level)
    _emit(action, st, details or None)


def _outcome_to_status(outcome: str) -> str:
    if outcome in ("error",):
        return "error"
    if outcome in ("blocked",):
        return "warning"
    return "success"


def _exec_outcome_to_status(outcome: str) -> str:
    if outcome in ("failed",):
        return "error"
    if outcome in ("skipped",):
        return "warning"
    return "success"


def _level_to_status(level: str) -> str:
    if level == "error":
        return "error"
    if level == "warning":
        return "warning"
    return "success"
