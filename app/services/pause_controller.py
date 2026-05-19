"""
Pause/Resume controller for Playwright automation tasks.

Implements cooperative pause using asyncio.Event, bridging sync API threads
and async Playwright event loops. Allows pause/resume at action-level granularity.
"""

import asyncio
import logging
from typing import Optional, Callable

logger = logging.getLogger(__name__)


class PauseController:
    """
    Manages pause state for async Playwright automation.
    
    Uses asyncio.Event for efficient cross-thread signaling:
    - Thread-safe: API threads call on_pause()/on_resume()
    - Async-aware: Playwright coroutines await wait_if_paused()
    - Instant: No polling; Event.wait() blocks only while paused
    
    Typical flow:
    1. Create with running asyncio loop: controller = PauseController(loop)
    2. Register callbacks: logger.on_pause_callback = controller.on_pause
    3. In Playwright batch: await controller.wait_if_paused()
    4. On API pause: ExecutionLogger.pause() triggers on_pause callback
    5. On API resume: ExecutionLogger.resume() triggers on_resume callback
    """

    def __init__(self, loop: asyncio.AbstractEventLoop):
        """
        Initialize PauseController for given event loop.
        
        Args:
            loop: The asyncio event loop where Playwright runs
        """
        self._loop = loop
        self._may_run = asyncio.Event()
        self._may_run.set()  # Running by default
        self._is_paused = False

    def on_pause(self) -> None:
        """
        Called when pause is requested (from sync API thread).
        
        Thread-safe. Clears the Event, blocking all wait_if_paused() calls.
        """
        try:
            self._is_paused = True
            self._loop.call_soon_threadsafe(self._may_run.clear)
            logger.debug("PauseController: pause signal queued")
        except Exception as e:
            logger.error(f"PauseController.on_pause error: {e}")

    def on_resume(self) -> None:
        """
        Called when resume is requested (from sync API thread).
        
        Thread-safe. Sets the Event, unblocking all wait_if_paused() calls.
        """
        try:
            self._is_paused = False
            self._loop.call_soon_threadsafe(self._may_run.set)
            logger.debug("PauseController: resume signal queued")
        except Exception as e:
            logger.error(f"PauseController.on_resume error: {e}")

    async def wait_if_paused(self) -> None:
        """
        Pause execution at this point until resumed.
        
        Call before major Playwright actions (goto, click, fill, save).
        Blocks instantly if paused; continues if running.
        
        Usage:
            await pause_controller.wait_if_paused()
            await page.click(selector)
        """
        await self._may_run.wait()

    def is_paused(self) -> bool:
        """
        Check if pause is currently active (non-blocking).
        
        Useful for logging or short-circuit decisions.
        """
        return self._is_paused

    def reset(self) -> None:
        """Reset to running state (for cleanup/reuse)."""
        try:
            self._is_paused = False
            self._loop.call_soon_threadsafe(self._may_run.set)
        except Exception:
            pass
