"""Background task that auto-denies stale permission requests.

Runs every 30 seconds. Any PermissionRequest older than its configured
timeout is denied and a timeout notice is posted to the originating RC room.
This handles cases where the broker's own asyncio.wait_for doesn't fire
(e.g. the broker was stopped before the timeout).
"""

from __future__ import annotations

import asyncio
import logging

from .permission import PermissionNotifier, PermissionRegistry, _format_timeout_msg

logger = logging.getLogger("coop.permissions.expiry")

_CHECK_INTERVAL = 30  # seconds between expiry sweeps


async def run_expiry_task(
    registry: PermissionRegistry,
    notifier: PermissionNotifier,
) -> None:
    """Periodically expire stale permission requests and notify the chat room.

    Each request carries its own timeout_seconds, so expiry is evaluated
    per-request rather than against a single global minimum.

    Uses a PermissionNotifier to route timeout notices to the correct
    platform when multiple connectors are configured.
    """
    while True:
        try:
            await asyncio.sleep(_CHECK_INTERVAL)
            expired = registry.expire_old()
            for req in expired:
                logger.warning(
                    "Expiry task: auto-denied permission [%s] tool=%s",
                    req.request_id, req.tool_name,
                )
                success = await notifier.notify(
                    req.session_id,
                    req.room_id,
                    _format_timeout_msg(req),
                    thread_id=req.thread_id,
                )
                if not success:
                    logger.error(
                        "Expiry task: failed to post timeout notice for [%s]",
                        req.request_id,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Expiry task error: %s", e)
