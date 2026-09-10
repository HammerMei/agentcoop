"""Core permission approval abstractions.

Split into three focused modules:
  - ``permission_state``     — PermissionRequest, PermissionRegistry, ID generation
  - ``permission_presenter`` — user-facing message formatting
  - ``permission_notifier``  — transport delivery with retry policy

This file re-exports all public symbols for backward compatibility and
contains the ``PermissionBroker`` ABC which orchestrates the above.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod

# Re-export notifier types
from .permission_notifier import (  # noqa: F401
    ConnectorPermissionNotifier,
    PermissionNotifier,
)

# Re-export presentation helpers
from .permission_presenter import (  # noqa: F401
    format_request_msg,
    format_timeout_msg,
)

# Re-export domain state types
from .permission_state import (  # noqa: F401
    PermissionNotificationError,
    PermissionRegistry,
    PermissionRequest,
    generate_request_id,
)

# Backward-compat aliases for code that imported the old private names
_format_request_msg = format_request_msg
_format_timeout_msg = format_timeout_msg
_generate_id = generate_request_id

logger = logging.getLogger("coop.permissions")


# ── PermissionBroker ABC ──────────────────────────────────────────────────────

class PermissionBroker(ABC):
    """Abstract base for backend-specific permission approval brokers.

    Concrete subclasses (ClaudePermissionBroker, OpenCodePermissionBroker)
    handle the transport details of intercepting tool calls from their
    respective backends.

    The shared logic here handles:
      - Generating request IDs
      - Registering in the PermissionRegistry
      - Posting notifications via the PermissionNotifier
      - Awaiting the asyncio.Future with timeout
    """

    def __init__(
        self,
        registry: PermissionRegistry,
        notifier: PermissionNotifier,
        timeout_seconds: int = 300,
    ) -> None:
        self._registry = registry
        self._notifier = notifier
        self._timeout_seconds = timeout_seconds

    @abstractmethod
    async def start(self) -> None:
        """Start any background servers/listeners needed by this broker."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Shut down background servers/listeners."""
        ...

    async def request_permission(
        self,
        tool_name: str,
        tool_input: dict,
        session_id: str,
        room_id: str,
        thread_id: str | None = None,
    ) -> bool:
        """Post a permission request and block until owner responds.

        Returns True if approved, False if denied or timed out.

        ``thread_id`` — when set, the notification is posted inside that
        thread so it appears alongside the conversation that triggered
        the tool call.
        """
        # Generate a collision-free 4-char ID
        req_id = generate_request_id()
        max_retries = 100
        retries = 0
        while self._registry.get(req_id):
            retries += 1
            if retries >= max_retries:
                raise RuntimeError(
                    f"Failed to generate a unique permission request ID after {max_retries} attempts"
                )
            req_id = generate_request_id()

        req = PermissionRequest(
            request_id=req_id,
            tool_name=tool_name,
            tool_input=tool_input,
            room_id=room_id,
            session_id=session_id,
            thread_id=thread_id,
            timeout_seconds=self._timeout_seconds,
        )
        self._registry.register(req)
        logger.info(
            "Permission request [%s] tool=%s session=%s room=%s thread=%s",
            req_id, tool_name, session_id[:8], room_id, thread_id,
        )

        posted = await self._notifier.notify(
            session_id, room_id, format_request_msg(req), thread_id=thread_id,
        )
        if not posted:
            self._registry.resolve(req_id, False)
            logger.warning(
                "Permission [%s] notification delivery failed — raising PermissionNotificationError",
                req_id,
            )
            raise PermissionNotificationError(
                f"[{req_id}] Could not deliver permission notification to room {room_id} "
                f"(connection error — owner was never asked)"
            )

        try:
            result = await asyncio.wait_for(
                req.future,
                timeout=self._timeout_seconds,
            )
            verb = "approved" if result else "denied"
            logger.info("Permission [%s] %s", req_id, verb)
            return result
        except asyncio.TimeoutError:
            self._registry.resolve(req_id, False)
            logger.warning("Permission [%s] timed out — auto-denied", req_id)
            # Best-effort delivery of the timeout notification — swallow all
            # errors so that a delivery failure (e.g. transient network error)
            # does not propagate as an unexpected exception to the caller.
            # The tool has already been denied; failing to post the timeout
            # message is cosmetic, not functional.
            try:
                await self._notifier.notify(
                    session_id, room_id, format_timeout_msg(req), thread_id=req.thread_id,
                )
            except Exception as e:
                logger.warning(
                    "Permission [%s] timeout notification delivery failed: %s", req_id, e
                )
            return False
