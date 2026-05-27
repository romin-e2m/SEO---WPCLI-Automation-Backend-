import threading
from datetime import datetime, timezone
from typing import Any, Optional, Callable
from dataclasses import dataclass, asdict


@dataclass
class ExecutionLogEntry:
    id: str
    timestamp: str
    action: str
    status: str  # 'pending', 'success', 'error', 'warning'
    details: Optional[str] = None
    execution_id: str = "default"

    def to_dict(self):
        return {
            k: v for k, v in asdict(self).items() if v is not None
        }


class ExecutionLogger:
    """Logger for execution steps (thread-safe for sync run_pipeline + async SSE)."""

    def __init__(self, execution_id: str = "default"):
        self.execution_id = execution_id
        self._lock = threading.Lock()
        self.logs: list[ExecutionLogEntry] = []
        self._log_counter = 0
        self._complete = False
        self._paused = False
        # Stored on execution start so resume can replay without the client re-sending the payload.
        self._payload: Optional[dict[str, Any]] = None
        # Index of the last fully-completed row (0-based into the flat rows_iter list).
        self._rows_completed: int = 0
        # Structured per-row results for live table rendering on the frontend.
        self._row_results: list[dict[str, Any]] = []
        # Last QA report dict (set after POST /api/qa/run) for Excel export.
        self._qa_report: Optional[dict[str, Any]] = None
        # Optional PauseController callback for action-level pause (set by caller)
        self._on_pause_callback: Optional[Callable[[], None]] = None
        self._on_resume_callback: Optional[Callable[[], None]] = None
        # PauseController instance for async Playwright (set by API layer)
        self._pause_controller: Optional[Any] = None

    def is_complete(self) -> bool:
        with self._lock:
            return self._complete

    def mark_complete(self) -> None:
        with self._lock:
            self._complete = True

    def is_paused(self) -> bool:
        with self._lock:
            return self._paused

    def set_pause_callbacks(
        self, on_pause: Optional[Callable[[], None]] = None, on_resume: Optional[Callable[[], None]] = None
    ) -> None:
        """
        Register callbacks to notify PauseController.
        
        Called by the API layer to bridge ExecutionLogger pause state
        to the async PauseController in the Playwright event loop.
        """
        with self._lock:
            self._on_pause_callback = on_pause
            self._on_resume_callback = on_resume

    def set_pause_controller(self, controller: Any) -> None:
        """
        Set the PauseController instance for async pause checkpoints.
        
        Called by the API layer before starting Playwright batch execution.
        """
        with self._lock:
            self._pause_controller = controller

    def pause(self) -> None:
        cb = None
        with self._lock:
            if not self._complete:
                self._paused = True
                cb = self._on_pause_callback

        # Call outside lock to avoid deadlock
        if cb is not None:
            try:
                cb()
            except Exception as e:
                import logging as _logging
                _logging.getLogger(__name__).error(f"Error calling pause callback: {e}")

    def resume(self) -> None:
        cb = None
        with self._lock:
            self._paused = False
            cb = self._on_resume_callback

        # Call outside lock to avoid deadlock
        if cb is not None:
            try:
                cb()
            except Exception as e:
                import logging as _logging
                _logging.getLogger(__name__).error(f"Error calling resume callback: {e}")

    def resume_and_reset_complete(self) -> None:
        """Clear paused/complete flags for resume and unblock PauseController.

        _row_results are intentionally kept so the SSE stream can replay the
        full set of row results (both halves) after a page refresh/reconnect.
        The frontend's onRowResult handler deduplicates by action_type|sheet_name|row_index.
        """
        cb = None
        with self._lock:
            self._paused = False
            self._complete = False
            cb = self._on_resume_callback
            # Do NOT clear _row_results — SSE will replay them; frontend deduplication handles it.

        # Call outside lock to avoid deadlock — mirrors resume()
        if cb is not None:
            try:
                cb()
            except Exception as e:
                import logging as _logging
                _logging.getLogger(__name__).error(f"Error calling resume callback in resume_and_reset_complete: {e}")

    def set_rows_completed(self, n: int) -> None:
        with self._lock:
            self._rows_completed = n

    def get_rows_completed(self) -> int:
        with self._lock:
            return self._rows_completed

    def store_payload(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._payload = payload

    def get_payload(self) -> Optional[dict[str, Any]]:
        with self._lock:
            return self._payload

    @staticmethod
    def _utc_timestamp() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def log_sync(
        self,
        action: str,
        status: str = "success",
        details: Optional[str] = None,
    ) -> None:
        """Append a log entry (safe from worker threads used by sync FastAPI routes)."""
        with self._lock:
            self._log_counter += 1
            entry = ExecutionLogEntry(
                id=f"log_{self.execution_id}_{self._log_counter}",
                timestamp=self._utc_timestamp(),
                action=action,
                status=status,
                details=details,
                execution_id=self.execution_id,
            )
            self.logs.append(entry)

    def append_monitor_entry(self, log_entry: dict) -> None:
        """Append a preformatted dict from the monitor broadcaster (same shape as log_sync output)."""
        with self._lock:
            self._log_counter += 1
            raw_id = log_entry.get("id")
            entry_id = raw_id if raw_id else f"log_{self.execution_id}_{self._log_counter}"
            ts = log_entry.get("timestamp")
            if not isinstance(ts, str):
                ts = self._utc_timestamp()
            entry = ExecutionLogEntry(
                id=entry_id,
                timestamp=ts,
                action=log_entry["action"],
                status=log_entry["status"],
                details=log_entry.get("details"),
                execution_id=self.execution_id,
            )
            self.logs.append(entry)

    def append_row_result(self, result_dict: dict) -> None:
        """Append a structured per-row result dict for SSE streaming to the frontend."""
        with self._lock:
            self._row_results.append(result_dict)

    def get_row_results(self) -> list[dict]:
        """Return all row results accumulated so far."""
        with self._lock:
            return list(self._row_results)

    def store_qa_report(self, report: dict[str, Any]) -> None:
        with self._lock:
            self._qa_report = report

    def get_qa_report(self) -> Optional[dict[str, Any]]:
        with self._lock:
            return self._qa_report

    def clear(self) -> None:
        """Reset logs (e.g. before a new dry-run or execute)."""
        with self._lock:
            self.logs = []
            self._log_counter = 0
            self._complete = False
            self._paused = False
            self._rows_completed = 0
            self._row_results = []
            self._qa_report = None

    def get_logs(self) -> list[dict]:
        """Get all logs so far."""
        with self._lock:
            return [log.to_dict() for log in self.logs]

    def get_summary(self) -> dict:
        """Get a summary of logged actions."""
        with self._lock:
            status_counts = {
                "success": 0,
                "error": 0,
                "warning": 0,
                "pending": 0,
            }
            for log in self.logs:
                status_counts[log.status] = status_counts.get(log.status, 0) + 1
            return {
                "total": len(self.logs),
                "status_counts": status_counts,
                "execution_id": self.execution_id,
                "complete": self._complete,
                "paused": self._paused,
                "rows_completed": self._rows_completed,
                "first_log": self.logs[0].timestamp if self.logs else None,
                "last_log": self.logs[-1].timestamp if self.logs else None,
            }
