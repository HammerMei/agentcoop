"""MessageDispatcher: routes inbound messages to the processor for a room.

Owns the room→processor dispatch index and permission command interception.
Extracted from SessionManager to keep dispatch logic focused and testable.

**One room, one processor** (design §4.1). The index used to hold a list and fan out to
every entry, which turned a duplicate registration into two agents answering every
message — silently, because appending always succeeds. The index is now a single slot per
room, so the invariant is structural rather than checked: there is no state in which two
processors serve one room.

This does not constrain the multi-agent model, which gives each agent its own bot account
and therefore its own connector — and each connector its own dispatcher. What it removes
is two watchers on *one* account in one room, where the agents cannot see each other's
replies anyway (each connector filters its own messages).
"""

from __future__ import annotations

import logging
import re

from ..agents.response import AgentResponse
from .connector import Connector, IncomingMessage, RoomCapacity, UserRole
from .message_processor import MessageProcessor
from .permission import PermissionRegistry

logger = logging.getLogger("coop.core.dispatch")


class RoomAlreadyRoutedError(RuntimeError):
    """Two watchers tried to serve one room on one connector (§4.1)."""

_PERMISSION_CMD_RE = re.compile(
    r"^(approve|deny)\s+([a-z0-9]+)$", re.IGNORECASE
)


class MessageDispatcher:
    """Routes IncomingMessages to MessageProcessors by room ID.

    Permission commands (approve/deny) from owners are intercepted before routing,
    so they are handled by the registry rather than reaching a processor at all.

    Usage::

        dispatcher = MessageDispatcher(connector, permission_registry)
        connector.register_handler(dispatcher.dispatch)

        # When watchers start/stop:
        dispatcher.add_processor("room-123", processor)
        dispatcher.remove_processor("room-123", processor)
    """

    def __init__(
        self,
        connector: Connector,
        permission_registry: PermissionRegistry | None = None,
    ) -> None:
        self._connector = connector
        self._permission_registry = permission_registry
        self._room_processor: dict[str, MessageProcessor] = {}

    # ── Dispatch ──────────────────────────────────────────────────────────────

    async def dispatch(self, msg: IncomingMessage) -> bool:
        """Route an IncomingMessage to the processor for its room.

        Permission commands (approve/deny) from owners are intercepted here, before
        routing, so they are handled by the registry rather than reaching a processor.

        Returns:
            True if the processor accepted the message (or it was a permission
            command handled inline).  False if it was dropped — the connector must
            NOT advance the dedup watermark, so the message can be re-delivered on
            reconnect.
        """
        # --- Intercept permission commands at room level (before routing) ---
        if self._permission_registry and msg.role == UserRole.OWNER:
            m = _PERMISSION_CMD_RE.match(msg.text.strip())
            if m:
                await self._handle_permission_command(msg, m)
                return True  # permission commands are always accepted

        processor = self._room_processor.get(msg.room.id)
        if processor is None:
            logger.warning("No processor found for room_id=%s", msg.room.id)
            return False
        return await processor.enqueue(msg)

    def capacity(self, room_id: str) -> RoomCapacity:
        """Whether this room can take a message now, and if not, why not.

        A bool conflated two answers a caller has to act on differently, and both
        connectors got it wrong in the same way: a room with **no** processor was
        reported exactly like a room whose queue was **full**, so a message for an
        unrouted room made an idle gateway post "server busy" into it. Backpressure and
        a routing miss are different events (§2.7).
        """
        processor = self._room_processor.get(room_id)
        if processor is None:
            return RoomCapacity.UNROUTED
        return RoomCapacity.AVAILABLE if processor.is_accepting else RoomCapacity.FULL

    # ── Index management ──────────────────────────────────────────────────────

    def holder(self, room_id: str) -> str | None:
        """Which watcher serves this room, if any.

        A read for callers that want to refuse *early*: claiming the room is the last
        step of starting a watcher, and discovering the collision there means a session
        was created, context injected and the room subscribed first, all to be undone.
        The claim in `add_processor` stays the authoritative one — this is a cheaper
        look, not a substitute for it.
        """
        processor = self._room_processor.get(room_id)
        return processor.watcher_id if processor is not None else None

    def add_processor(self, room_id: str, processor: MessageProcessor) -> None:
        """Claim the room for this processor. Replaces its own; refuses another's.

        The two cases are genuinely different. A watcher restarting — reset, or a
        recreation — registers again for a room it already owns, and taking the slot is
        the correct, idempotent outcome. A *different* watcher arriving means two
        watchers believe they own one room, which under fan-out meant both answering
        every message; there is no outcome here that serves the operator, so it raises
        rather than picking one silently.

        Raised, not logged and skipped: the caller has already subscribed to the room
        and provisioned a session, and a warning would leave that half-built watcher
        looking healthy while never receiving anything.
        """
        current = self._room_processor.get(room_id)
        if current is not None and current.watcher_id != processor.watcher_id:
            raise RoomAlreadyRoutedError(
                f"Room '{room_id}' is already served by watcher "
                f"'{current.watcher_id}', so watcher '{processor.watcher_id}' cannot "
                f"also take it: both would answer every message, and neither would see "
                f"the other's reply. One room on one bot account belongs to one "
                f"watcher; give the second one its own connector (its own bot account) "
                f"or its own room."
            )
        self._room_processor[room_id] = processor

    def remove_processor(self, room_id: str, processor: MessageProcessor) -> None:
        """Release the room, if this processor is the one holding it.

        Identity-checked so a watcher that failed to claim the room cannot unroute the
        watcher that holds it — its teardown runs the same removal path.
        """
        if self._room_processor.get(room_id) is processor:
            del self._room_processor[room_id]

    # ── Permission command handling ───────────────────────────────────────────

    async def _handle_permission_command(
        self, msg: IncomingMessage, match: re.Match
    ) -> None:
        """Resolve an approve/deny permission command (room-level, before routing)."""
        action = match.group(1).lower()
        req_id = match.group(2).lower()

        if len(req_id) != 4:
            reply = (
                f"⚠️ Invalid ID `{req_id}` — "
                f"expected 4 characters (e.g. `{action} a3k9`)."
            )
        else:
            approved = action == "approve"
            resolved = self._permission_registry.resolve(  # type: ignore[union-attr]
                req_id, approved, from_room_id=msg.room.id, from_thread_id=msg.thread_id
            )
            if resolved:
                icon = "✅" if approved else "❌"
                verb = "approved" if approved else "denied"
                reply = f"{icon} Permission `{req_id}` {verb}."
            else:
                reply = f"⚠️ No pending permission request with ID `{req_id}`."

        await self._connector.send_text(
            msg.room.id, AgentResponse(text=reply), thread_id=msg.thread_id
        )
