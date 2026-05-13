import threading
from datetime import datetime, timezone
from typing import Optional
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

    def is_complete(self) -> bool:
        with self._lock:
            return self._complete

    def mark_complete(self) -> None:
        with self._lock:
            self._complete = True

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
                timestamp=datetime.now(timezone.utc).isoformat() + "Z",
                action=action,
                status=status,
                details=details,
                execution_id=self.execution_id,
            )
            self.logs.append(entry)

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
                "first_log": self.logs[0].timestamp if self.logs else None,
                "last_log": self.logs[-1].timestamp if self.logs else None,
            }
