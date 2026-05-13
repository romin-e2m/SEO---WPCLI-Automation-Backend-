"""Execution id for live monitor logs (dry-run vs execute) via contextvars."""

from __future__ import annotations

from contextvars import ContextVar

current_monitor_execution_id: ContextVar[str] = ContextVar(
    "current_monitor_execution_id",
    default="default",
)
