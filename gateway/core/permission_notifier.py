"""Permission notification delivery — transport with retry policy.

Separated from domain state so that retry policy, delivery transport,
and connector wiring do not touch the registry or formatting logic.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ..agents.response import AgentResponse

if TYPE_CHECKING:
    from .connector import Connector

logger = logging.getLogger("coop.permissions")


@runtime_checkable
class PermissionNotifier(Protocol):
    """Delivers permission-related messages to a chat room.

    Decouples permission state management from chat transport so brokers
    and the expiry task never touch Connector directly.
    """

    async def notify(
        self,
        session_id: str,
        room_id: str,
        text: str,
        thread_id: str | None = None,
    ) -> bool:
        """Post text to the room associated with session_id.

        Returns True on success, False if delivery failed after retries.
        """
        ...


class ConnectorPermissionNotifier:
    """Routes permission notifications via the session→connector map."""

    def __init__(self, session_connector_map: "dict[str, Connector]") -> None:
        self._session_connector_map = session_connector_map

    async def notify(
        self,
        session_id: str,
        room_id: str,
        text: str,
        thread_id: str | None = None,
        max_attempts: int = 3,
        retry_delay: float = 2.0,
    ) -> bool:
        """Post text to room via the connector associated with session_id."""
        connector = self._session_connector_map.get(session_id)
        if connector is None:
            logger.error(
                "No connector found for session %s — cannot post to room %s",
                session_id[:8], room_id,
            )
            return False
        for attempt in range(1, max_attempts + 1):
            try:
                await connector.send_text(
                    room_id, AgentResponse(text=text), thread_id=thread_id
                )
                return True
            except Exception as e:
                logger.warning(
                    "Failed to post message to room %s (attempt %d/%d): %s: %s",
                    room_id, attempt, max_attempts, type(e).__name__, e,
                )
                if attempt < max_attempts:
                    await asyncio.sleep(retry_delay)
        logger.error(
            "All %d attempts to post message to room %s failed",
            max_attempts, room_id,
        )
        return False
