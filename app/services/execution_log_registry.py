"""Single in-memory registry for ExecutionLogger instances (monitor SSE + /api/run)."""

from __future__ import annotations

import os
import threading

from app.services.execution_logger import ExecutionLogger

_MAX_TRACKED_EXECUTIONS = int(os.getenv("MAX_TRACKED_EXECUTIONS", "50"))

_executions: dict[str, ExecutionLogger] = {}
_registry_lock = threading.Lock()


def register(execution_id: str, logger: ExecutionLogger) -> None:
    """Store a logger (e.g. from /api/run/execute). Prunes completed entries when over capacity."""
    with _registry_lock:
        if len(_executions) >= _MAX_TRACKED_EXECUTIONS:
            for eid, ex in list(_executions.items()):
                if ex.is_complete():
                    _executions.pop(eid, None)
                if len(_executions) < _MAX_TRACKED_EXECUTIONS:
                    break
        _executions[execution_id] = logger


def get(execution_id: str) -> ExecutionLogger | None:
    return _executions.get(execution_id)


def ensure(execution_id: str) -> ExecutionLogger:
    """Return existing logger or create one (e.g. lazy monitor /system stream)."""
    if execution_id not in _executions:
        _executions[execution_id] = ExecutionLogger(execution_id)
    return _executions[execution_id]


def clear_logs(execution_id: str) -> None:
    """Clear buffered entries before a new dry-run or execute."""
    ensure(execution_id).clear()


def append_log_entry(execution_id: str, log_entry: dict) -> None:
    ensure(execution_id).append_monitor_entry(log_entry)
