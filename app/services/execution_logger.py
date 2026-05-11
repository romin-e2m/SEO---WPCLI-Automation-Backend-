import asyncio
import json
from datetime import datetime
from typing import Optional, Any
from dataclasses import dataclass, asdict


@dataclass
class ElementInfo:
    selector: str
    visible: bool
    in_viewport: bool
    x: int = 0
    y: int = 0


@dataclass
class ExecutionLogEntry:
    id: str
    timestamp: str
    action: str
    status: str  # 'pending', 'success', 'error', 'warning'
    details: Optional[str] = None
    element_info: Optional[dict] = None
    browser_screenshot: Optional[str] = None
    execution_id: str = "default"

    def to_dict(self):
        return {
            k: v for k, v in asdict(self).items() if v is not None
        }


class ExecutionLogger:
    """Logger for execution steps with browser state capture."""

    def __init__(self, execution_id: str = "default"):
        self.execution_id = execution_id
        self.logs: list[ExecutionLogEntry] = []
        self._subscribers: dict[str, asyncio.Queue] = {}
        self._log_counter = 0

    def add_subscriber(self, subscriber_id: str, queue: asyncio.Queue):
        """Add a subscriber to receive log updates."""
        self._subscribers[subscriber_id] = queue

    def remove_subscriber(self, subscriber_id: str):
        """Remove a subscriber."""
        self._subscribers.pop(subscriber_id, None)

    async def log(
        self,
        action: str,
        status: str = "success",
        details: Optional[str] = None,
        element_info: Optional[ElementInfo] = None,
        browser_screenshot: Optional[str] = None,
    ):
        """Log an execution step."""
        self._log_counter += 1
        entry = ExecutionLogEntry(
            id=f"log_{self.execution_id}_{self._log_counter}",
            timestamp=datetime.utcnow().isoformat() + "Z",
            action=action,
            status=status,
            details=details,
            element_info=asdict(element_info) if element_info else None,
            browser_screenshot=browser_screenshot,
            execution_id=self.execution_id,
        )

        self.logs.append(entry)

        # Broadcast to subscribers
        for queue in self._subscribers.values():
            try:
                queue.put_nowait(entry.to_dict())
            except asyncio.QueueFull:
                pass

    def get_logs(self) -> list[dict]:
        """Get all logs so far."""
        return [log.to_dict() for log in self.logs]

    def get_summary(self) -> dict:
        """Get a summary of logged actions."""
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
            "first_log": self.logs[0].timestamp if self.logs else None,
            "last_log": self.logs[-1].timestamp if self.logs else None,
        }
