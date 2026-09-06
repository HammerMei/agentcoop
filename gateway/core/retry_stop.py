"""One way to stop something that may not stop the first time (#144).

`config reload` stops connectors, agent backends, permission brokers and
processors it is about to replace. A stop that raises is retried a couple of
times, a few seconds apart — a server mid-request, a socket still flushing —
and that is the whole recovery: no other way of killing it, no rollback. What
still will not stop after that is reported as degraded and left for the
operator, who knows why it is stuck; the code does not.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

logger = logging.getLogger("agent-chat-gateway.stop")

STOP_ATTEMPTS = 3
STOP_RETRY_DELAY = 2.0  # seconds between attempts


async def stop_with_retries(
    what: str,
    stop: Callable[[], Awaitable[object]],
    *,
    attempts: int | None = None,
    delay: float | None = None,
) -> BaseException | None:
    """Call `stop()` until it returns, up to `attempts` times.

    Returns None when a call returned, else the LAST exception — the caller
    decides what a thing that would not stop means for it. Never raises.
    """
    attempts = STOP_ATTEMPTS if attempts is None else attempts
    delay = STOP_RETRY_DELAY if delay is None else delay
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            await stop()
            if attempt > 1:
                logger.info("%s stopped on attempt %d", what, attempt)
            return None
        except Exception as exc:
            last = exc
            logger.warning("%s did not stop (attempt %d/%d): %s", what, attempt, attempts, exc)
            if attempt < attempts:
                await asyncio.sleep(delay)
    return last
