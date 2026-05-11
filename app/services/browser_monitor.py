"""
Real-time browser monitoring and display system.
Provides live feedback for Playwright automation in both headed and headless modes.
Shows users what the automation is doing in real-time.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Callable
from enum import Enum


class ActionStatus(Enum):
    """Status of an action."""
    PENDING = "⏳"
    IN_PROGRESS = "🔄"
    SUCCESS = "✅"
    WARNING = "⚠️"
    ERROR = "❌"


class BrowserMonitor:
    """
    Real-time browser automation monitor.
    Displays live progress of Playwright automation with formatted output.
    """
    
    def __init__(self, verbose: bool = True):
        self.verbose = verbose
        self.actions: list[dict[str, Any]] = []
        self.current_action: dict[str, Any] | None = None
        self.start_time: datetime | None = None
        self.callbacks: list[Callable] = []
    
    def start(self):
        """Mark the start of automation."""
        self.start_time = datetime.now()
        self._print("🚀 BROWSER AUTOMATION STARTED")
        self._print("━" * 80)
    
    def end(self, status: str = "success"):
        """Mark the end of automation."""
        if self.start_time:
            elapsed = (datetime.now() - self.start_time).total_seconds()
            status_emoji = "✅" if status == "success" else "❌"
            self._print("━" * 80)
            self._print(f"{status_emoji} AUTOMATION COMPLETED in {elapsed:.1f}s")
        else:
            self._print("❌ AUTOMATION FAILED")
    
    def log_action(self, action: str, level: str = "info", details: str = ""):
        """Log a single action step."""
        timestamp = datetime.now().isoformat(timespec="milliseconds")
        
        status_map = {
            "info": "ℹ️",
            "success": "✅",
            "warning": "⚠️",
            "error": "❌",
        }
        
        emoji = status_map.get(level, "ℹ️")
        
        log_entry = {
            "timestamp": timestamp,
            "level": level,
            "action": action,
            "details": details,
            "emoji": emoji,
        }
        
        self.actions.append(log_entry)
        
        output = f"{emoji} [{timestamp}] {action}"
        if details:
            output += f"\n   └─ {details}"
        
        self._print(output)
        self._notify_subscribers(log_entry)
    
    def log_step(self, step_num: int, total_steps: int, description: str):
        """Log a numbered step."""
        progress = f"Step {step_num}/{total_steps}"
        self._print(f"\n📍 {progress}: {description}")
        self.log_action(progress, "info", description)
    
    def log_screenshot(self, path: str, description: str = ""):
        """Log a screenshot capture."""
        msg = f"Screenshot saved: {path}"
        if description:
            msg += f" ({description})"
        self._print(f"📸 {msg}")
        self.log_action("Screenshot", "info", msg)
    
    def log_form_fill(self, field_name: str, value: str = ""):
        """Log form field interaction."""
        msg = f"Filling '{field_name}'"
        if value:
            msg += f" with {len(value)} chars"
        self._print(f"📝 {msg}")
        self.log_action("Form Fill", "info", msg)
    
    def log_click(self, element: str):
        """Log element click."""
        self._print(f"🖱️  Clicking: {element}")
        self.log_action("Click", "info", f"Element: {element}")
    
    def log_navigation(self, url: str):
        """Log page navigation."""
        self._print(f"🌐 Navigating to: {url}")
        self.log_action("Navigation", "info", f"URL: {url}")
    
    def log_wait(self, duration_ms: int, reason: str = ""):
        """Log a wait period."""
        duration_s = duration_ms / 1000
        msg = f"Waiting {duration_s}s"
        if reason:
            msg += f" ({reason})"
        self._print(f"⏳ {msg}")
        self.log_action("Wait", "info", msg)
    
    def add_subscriber(self, callback: Callable):
        """Add a callback to receive log notifications."""
        self.callbacks.append(callback)
    
    def _notify_subscribers(self, log_entry: dict):
        """Notify all subscribers of new log entry."""
        for callback in self.callbacks:
            try:
                callback(log_entry)
            except Exception:
                pass
    
    def _print(self, message: str):
        """Print message if verbose mode is enabled."""
        if self.verbose:
            print(message, flush=True)
    
    def get_summary(self) -> dict[str, Any]:
        """Get a summary of all actions."""
        action_counts = {
            "info": 0,
            "success": 0,
            "warning": 0,
            "error": 0,
        }
        
        for action in self.actions:
            level = action.get("level", "info")
            action_counts[level] = action_counts.get(level, 0) + 1
        
        elapsed = 0
        if self.start_time:
            elapsed = (datetime.now() - self.start_time).total_seconds()
        
        return {
            "total_actions": len(self.actions),
            "action_counts": action_counts,
            "elapsed_seconds": elapsed,
            "started": self.start_time.isoformat() if self.start_time else None,
        }
    
    def get_all_logs(self) -> list[dict[str, Any]]:
        """Get all logged actions."""
        return self.actions.copy()


class HeadlessBrowserDisplay:
    """
    Enhanced display system for headless browser automation.
    Shows progress with formatted output suitable for CLI/logs.
    """
    
    def __init__(self):
        self.monitor = BrowserMonitor(verbose=True)
        self.frames: list[str] = []
    
    def render_frame(self) -> str:
        """Render current frame of automation."""
        frame = ""
        frame += "┌" + "─" * 78 + "┐\n"
        frame += "│ PLAYWRIGHT AUTOMATION MONITOR (Headless Mode)" + " " * 30 + "│\n"
        frame += "├" + "─" * 78 + "┤\n"
        
        summary = self.monitor.get_summary()
        frame += f"│ Actions: {summary['total_actions']:3d} | "
        frame += f"✅ {summary['action_counts'].get('success', 0):2d} | "
        frame += f"⚠️  {summary['action_counts'].get('warning', 0):2d} | "
        frame += f"❌ {summary['action_counts'].get('error', 0):2d} | "
        frame += f"Time: {summary['elapsed_seconds']:.1f}s" + " " * 5 + "│\n"
        
        frame += "├" + "─" * 78 + "┤\n"
        
        recent_logs = self.monitor.get_all_logs()[-5:]
        for log in recent_logs:
            emoji = log.get("emoji", "ℹ️")
            action = log.get("action", "")
            truncated_action = (action[:60] + "...") if len(action) > 60 else action
            frame += f"│ {emoji} {truncated_action:<72} │\n"
        
        frame += "└" + "─" * 78 + "┘\n"
        
        return frame
    
    def display_progress(self):
        """Display current progress."""
        print("\033[2J\033[H", end="")
        print(self.render_frame())


async def monitor_playwright_task(
    coroutine,
    monitor: BrowserMonitor | None = None,
) -> Any:
    """
    Wrap a Playwright coroutine with monitoring.
    
    Args:
        coroutine: The async Playwright task to monitor
        monitor: BrowserMonitor instance for logging
    
    Returns:
        Result of the coroutine
    """
    if monitor is None:
        monitor = BrowserMonitor()
    
    monitor.start()
    try:
        result = await coroutine
        monitor.end("success")
        return result
    except Exception as e:
        monitor.log_action(f"Error: {str(e)}", "error")
        monitor.end("failed")
        raise
