"""The core's only view of time: ``now()`` and "run this after a delay".

The simulator drives the same core on a discrete-event heap (``tests/coop/helpers.py``
``SimClock``); the daemon uses :class:`AsyncioClock`. Every time value the core keeps
(post times, ``idle_since``, ``retry_after``, lease expiry) is seconds on this clock.
"""

from __future__ import annotations

import asyncio
import logging
import time as _time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class Handle(Protocol):
    def cancel(self) -> None: ...


class Clock(Protocol):
    def now(self) -> float: ...

    def at(self, delay: float, fn: Callable[..., Awaitable[Any]], *args: Any) -> Handle:
        """Run ``fn(*args)`` ``delay`` seconds from now (``0`` = as soon as the running
        work yields). The handle cancels a run that has not started."""
        ...


class AsyncioClock:
    """Wall-clock time on the running event loop. ``now()`` is ``time.time()`` so that
    stored values (cursors, settings) compare across restarts; delays use the loop's
    monotonic timer."""

    def now(self) -> float:
        return _time.time()

    def at(self, delay: float, fn: Callable[..., Awaitable[Any]], *args: Any) -> Handle:
        loop = asyncio.get_running_loop()
        return loop.call_later(max(delay, 0.0), self._spawn, fn, args)

    @staticmethod
    def _spawn(fn: Callable[..., Awaitable[Any]], args: tuple) -> None:
        task = asyncio.ensure_future(fn(*args))
        task.add_done_callback(_log_failure)


def _log_failure(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("scheduled core work failed: %r", exc, exc_info=exc)
