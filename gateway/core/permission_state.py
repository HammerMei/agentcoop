"""Permission domain state: request data, registry, and ID generation.

Pure domain logic with no transport or presentation concerns.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import string
import time
from dataclasses import dataclass, field

logger = logging.getLogger("coop.permissions")


# ── Exceptions ────────────────────────────────────────────────────────────────

class PermissionNotificationError(Exception):
    """Raised when a permission notification could not be delivered to the chat room.

    This is distinct from a denial — the owner was never asked.
    Callers should surface a 'connection error, please retry' message rather
    than a permanent 'denied' message.
    """


# ── ID generation ─────────────────────────────────────────────────────────────

_ID_ALPHABET = string.ascii_lowercase + string.digits


def generate_request_id() -> str:
    """Generate a 4-char lowercase alphanumeric ID, e.g. 'a3k9'.

    Uses ``secrets.choice`` (cryptographically secure RNG) so that permission
    request IDs cannot be guessed by an observer watching the chat room.
    """
    return "".join(secrets.choice(_ID_ALPHABET) for _ in range(4))


# ── PermissionRequest ─────────────────────────────────────────────────────────

@dataclass
class PermissionRequest:
    request_id: str
    tool_name: str
    tool_input: dict
    room_id: str
    session_id: str
    thread_id: str | None = None
    timeout_seconds: int = 300
    created_at: float = field(default_factory=time.monotonic)
    # _future is created lazily on first access via the `future` property.
    # Using init=False + default=None allows PermissionRequest to be constructed
    # outside an asyncio event loop (e.g., in synchronous test helpers) without
    # raising RuntimeError: "no running event loop".
    _future: asyncio.Future | None = field(default=None, init=False, repr=False)

    @property
    def future(self) -> asyncio.Future:
        """Return the asyncio Future, creating it on first access."""
        if self._future is None:
            self._future = asyncio.get_running_loop().create_future()
        return self._future


# ── PermissionRegistry ────────────────────────────────────────────────────────

class PermissionRegistry:
    """In-process store for all pending permission requests.

    Shared across all brokers and MessageProcessor instances.
    Safe for asyncio single-threaded event loop use (no locking needed).
    """

    def __init__(self) -> None:
        self._requests: dict[str, PermissionRequest] = {}

    def register(self, req: PermissionRequest) -> None:
        self._requests[req.request_id] = req

    def resolve(
        self,
        request_id: str,
        approved: bool,
        *,
        from_room_id: str | None = None,
        from_thread_id: str | None = None,
    ) -> bool:
        """Resolve a pending request. Returns False if not found, already resolved,
        or the approving message came from a different room/thread than the request.

        Args:
            request_id: The 4-character ID of the pending request.
            approved: True to approve, False to deny.
            from_room_id: When provided, the request is only resolved if its
                ``room_id`` matches.  A mismatch (cross-room attempt) leaves the
                request pending and returns False — same as "not found" so as not
                to leak that a request with this ID exists in another room.
            from_thread_id: When provided AND the request also has a ``thread_id``,
                both must match.  A mismatch (cross-thread attempt within the same
                room) leaves the request pending and returns False.
                Non-threaded approval commands (``from_thread_id=None``) are always
                allowed through this check — owners may approve from the room-level
                input box even when the request originated in a thread.
        """
        req = self._requests.get(request_id)
        if req is None or req.future.done():
            return False
        if from_room_id is not None and req.room_id != from_room_id:
            return False  # Cross-room approval rejected; leave request pending
        # Thread-level check: only enforce when BOTH sides carry a thread_id.
        #
        # Design intent — room-level approvals (from_thread_id=None) are intentionally
        # allowed to resolve threaded requests.  This is NOT a security gap:
        #
        #   • Rocket.Chat (and every major platform: Slack, Discord, Teams, Telegram)
        #     does NOT enforce thread-level permissions distinct from the parent room.
        #     Any owner who can see the room can already read every thread in it.
        #   • The real trust boundary is the room, guarded by the from_room_id check
        #     above.  Threads are just reply chains, not isolated namespaces.
        #   • Requiring owners to be inside the exact thread to approve would hurt UX
        #     with no security benefit on platforms that share room-level roles across
        #     all threads.
        #
        # The guard below still rejects approvals sent from a *different* thread
        # (from_thread_id != req.thread_id), which prevents casual cross-thread
        # confusion when both the approver and the request are inside threads.
        if from_thread_id is not None and req.thread_id is not None and req.thread_id != from_thread_id:
            return False  # Cross-thread approval rejected; leave request pending
        self._requests.pop(request_id)
        req.future.set_result(approved)
        return True

    def get(self, request_id: str) -> PermissionRequest | None:
        return self._requests.get(request_id)

    def expire_old(self) -> list[PermissionRequest]:
        """Auto-deny all requests that have exceeded their own configured timeout."""
        now = time.monotonic()
        expired = [
            req for req in list(self._requests.values())
            if (now - req.created_at) >= req.timeout_seconds
        ]
        for req in expired:
            self.resolve(req.request_id, False)
        return expired

    def pending_for_session(self, session_id: str) -> list[PermissionRequest]:
        """Return all pending requests for a given session."""
        return [r for r in self._requests.values() if r.session_id == session_id]

    def cancel_session(self, session_id: str) -> None:
        """Auto-deny all pending requests for a session (e.g. on session stop)."""
        for req in self.pending_for_session(session_id):
            self.resolve(req.request_id, False)
