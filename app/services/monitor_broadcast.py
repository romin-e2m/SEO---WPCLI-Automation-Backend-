"""Push high-level steps to the live browser monitor (SSE store in app.api.system)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone


def _emit(execution_id: str, action: str, status: str, details: str | None) -> None:
    from app.api.system import add_execution_log

    add_execution_log(
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


def clear_monitor_logs(execution_id: str = MONITOR_DEFAULT_ID) -> None:
    from app.api.system import clear_execution_logs

    clear_execution_logs(execution_id)


def emit_monitor_phase(execution_id: str, label: str, details: str | None = None) -> None:
    _emit(execution_id, label, "success", details)


def emit_monitor_row_dry(
    execution_id: str,
    action: str,
    sheet_name: str,
    row_index: int,
    outcome: str,
    message: str | None = None,
) -> None:
    st = _outcome_to_status(outcome)
    msg = message or ""
    _emit(
        execution_id,
        f"Dry-run [{action}] {sheet_name} row {row_index}: {outcome}",
        st,
        msg[:2000] if msg else None,
    )


def emit_monitor_row_exec(
    execution_id: str,
    action: str,
    sheet_name: str,
    row_index: int,
    outcome: str,
    message: str | None = None,
) -> None:
    st = _exec_outcome_to_status(outcome)
    msg = message or ""
    _emit(
        execution_id,
        f"Execute [{action}] {sheet_name} row {row_index}: {outcome}",
        st,
        msg[:2000] if msg else None,
    )


def emit_monitor_playwright(execution_id: str, action: str, level: str, details: str) -> None:
    st = _level_to_status(level)
    _emit(execution_id, action, st, details or None)


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
