from __future__ import annotations

import asyncio
import signal
import threading
import time
from collections.abc import Awaitable
from types import FrameType
from typing import TypeVar


T = TypeVar("T")


class RunInterrupted(Exception):
    """Raised after the user confirms a safe execution stop."""


class ConfirmedInterrupt:
    """Require two Ctrl+C presses before stopping a long-running operation."""

    def __init__(self, display, *, confirmation_seconds: float = 5.0):
        self.display = display
        self.confirmation_seconds = confirmation_seconds
        self._deadline = 0.0
        self._confirmed = False
        self._state_version = 0
        self._displayed_version = -1
        self._previous_handler = None
        self._installed = False

    @property
    def confirmed(self) -> bool:
        return self._confirmed

    def __enter__(self):
        if threading.current_thread() is threading.main_thread():
            self._previous_handler = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, self._handle_interrupt)
            self._installed = True
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        if self._installed:
            signal.signal(signal.SIGINT, self._previous_handler)

    def _handle_interrupt(self, _signum: int, _frame: FrameType | None) -> None:
        now = time.monotonic()
        if self._deadline > now:
            self._confirmed = True
        else:
            self._deadline = now + self.confirmation_seconds
            self._confirmed = False
        self._state_version += 1

    def progress(self, *args, **kwargs) -> None:
        if self._confirmed:
            raise RunInterrupted("stop confirmed by user")
        self.display.update(*args, **kwargs)

    async def run(self, operation: Awaitable[T]) -> T:
        task = asyncio.create_task(operation)
        while not task.done():
            self._refresh_notice()
            await asyncio.wait({task}, timeout=0.1)
        self._refresh_notice()
        return await task

    def _refresh_notice(self) -> None:
        now = time.monotonic()
        if not self._confirmed and self._deadline and now >= self._deadline:
            self._deadline = 0.0
            self._state_version += 1
        if self._displayed_version == self._state_version:
            return
        self._displayed_version = self._state_version
        if self._confirmed:
            self.display.set_interrupt_notice("Stop confirmed. Finishing the current safe step...")
        elif self._deadline:
            seconds = max(1, round(self._deadline - now))
            self.display.set_interrupt_notice(
                f"Press Ctrl+C again within {seconds} seconds to confirm exit."
            )
        else:
            self.display.set_interrupt_notice("")
