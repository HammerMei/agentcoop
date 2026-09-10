"""ScriptConnector: in-process connector for scripting and agent-to-agent piping.

No network calls, no authentication, no platform dependencies.
Messages are injected programmatically via inject() and replies are readable
from receive_reply().  Connectors can be chained with pipe_to() so that one
agent's output becomes another agent's input.

Usage patterns
--------------

Pattern A — single agent test harness::

    import asyncio
    from gateway.connectors.script import ScriptConnector
    from gateway.core.session_manager import SessionManager
    from gateway.core.config import CoreConfig
    from gateway.agents.claude import ClaudeBackend

    async def main():
        connector = ScriptConnector()
        agent     = ClaudeBackend(command="claude", new_session_args=[], timeout=120)
        manager   = SessionManager(connector, agent, CoreConfig())

        await manager.run_once()   # connects without blocking forever

        await connector.inject("What is 2 + 2?")
        print(await connector.receive_reply())   # "4"

Pattern B — pipe two agents (A summarises → B translates)::

    a = ScriptConnector(name="summariser")
    b = ScriptConnector(name="translator")
    a.pipe_to(b)          # A's output becomes B's input automatically

    manager_a = SessionManager(a, backend_a, config)
    manager_b = SessionManager(b, backend_b, config)

        await asyncio.gather(manager_a.run_once(), manager_b.run_once())

        await a.inject("Summarise this huge doc...")
        final = await b.receive_reply(timeout=300)
    print(final)

Pattern C — batch processing::

    async def batch(items):
        connector = ScriptConnector()
        manager   = SessionManager(connector, agent, config)
        await manager.run_once()
        results = []
        for item in items:
            await connector.inject(item)
            results.append(await connector.receive_reply())
        return results
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING

from ...agents.response import AgentResponse
from ...core.connector import (
    Connector,
    IncomingMessage,
    MessageHandler,
    Room,
    User,
    UserRole,
)

if TYPE_CHECKING:
    from ...core.watcher_manager import RoomRef

logger = logging.getLogger("coop.connectors.script")

_DEFAULT_ROOM = Room(id="script-room", name="script", type="script")
_DEFAULT_USER = User(id="script-user", username="user")


class ScriptConnector(Connector):
    """In-process connector for scripting, testing, and agent-to-agent piping.

    All methods are no-ops or operate on in-memory queues.  There are no
    network calls, no authentication, and no platform dependencies.

    Thread-safety: designed for single-threaded asyncio use.
    """

    delivery_mode = "direct"

    def __init__(self, name: str = "script") -> None:
        """
        Args:
            name: Human-readable label for this connector (used in log messages
                  and as the sender username when piping).
        """
        self.name = name
        self._handler: MessageHandler | None = None
        self._reply_queue: asyncio.Queue[str] = asyncio.Queue()
        self._pipe_target: ScriptConnector | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """No-op: no network connection needed."""
        logger.debug("ScriptConnector '%s' connected (no-op)", self.name)

    async def disconnect(self) -> None:
        """No-op: nothing to tear down."""
        logger.debug("ScriptConnector '%s' disconnected (no-op)", self.name)

    # ── Inbound ───────────────────────────────────────────────────────────────

    def register_handler(self, handler: MessageHandler) -> None:
        self._handler = handler

    # ── Outbound ──────────────────────────────────────────────────────────────

    async def send_text(
        self,
        room_id: str,
        response: AgentResponse,
        thread_id: str | None = None,  # noqa: ARG002 — threading not applicable to script connector
    ) -> None:
        """Store the reply text in the local queue and optionally forward to a pipe target."""
        await self._reply_queue.put(response.text)
        logger.debug(
            "ScriptConnector '%s' → reply queued (%d chars)",
            self.name,
            len(response.text),
        )

        if self._pipe_target and self._pipe_target._handler:
            piped_msg = IncomingMessage(
                id=f"piped-{uuid.uuid4().hex[:8]}",
                timestamp="",
                room=Room(id=room_id, name=room_id, type="script"),
                sender=User(id=self.name, username=self.name),
                role=UserRole.OWNER,
                text=response.text,
            )
            await self._pipe_target._handler(piped_msg)
            logger.debug(
                "ScriptConnector '%s' piped reply to '%s'",
                self.name,
                self._pipe_target.name,
            )

    # ── Room resolution ───────────────────────────────────────────────────────

    async def resolve_room(self, room_name: str) -> Room:
        """Return an in-memory Room — no platform lookup needed."""
        return Room(id=room_name, name=room_name, type="script")

    def supports_room_lookup(self) -> bool:
        """See `Connector.supports_room_lookup` — the identity lookup below is an answer, not a shrug."""
        return True

    async def room_ref_by_id(self, room_id: str) -> "RoomRef | None":
        """See `Connector.room_ref_by_id`.

        Script has no room directory to consult: `resolve_room` returns a
        `Room` whose id IS its name, so the inverse is the same identity read the
        other way. Any id names a valid room here — rooms are in-memory names the driving script invents — so this never
        answers `None`. Kind is `CHANNEL`, which is what the inbound path assigns
        ('script' is not a `RoomKind` value, so `kind_for` falls back to CHANNEL).

        Without this, a scheduled job could not bring one of these watchers back
        after an `expire`: the base class answers `None` for a connector that
        cannot look a room up by id, and the fire then failed at every slot while
        logging that the room was "gone, in another team, or this account is no
        longer in it" — none of which can be true of a script room.
        """
        from ...core.watcher_manager import RoomRef
        from ...core.watcher_rule import RoomKind

        return RoomRef(id=room_id, kind=RoomKind.CHANNEL, name=room_id)

    # ── Scripting API ─────────────────────────────────────────────────────────

    def pipe_to(self, target: "ScriptConnector") -> None:
        """Forward this connector's output as input to ``target``.

        Enables multi-agent pipelines::

            a.pipe_to(b)   # A's reply → B's inbox
            a.pipe_to(b).pipe_to(c)  # chaining not yet supported; call separately
        """
        self._pipe_target = target
        logger.debug(
            "ScriptConnector '%s' will pipe output to '%s'", self.name, target.name
        )

    async def inject(
        self,
        text: str,
        room: str = "script",
        sender: str = "user",
        role: UserRole = UserRole.OWNER,
    ) -> None:
        """Inject a message as if it arrived from the platform.

        Must be called AFTER SessionManager.run_once().

        Args:
            text  : Message body to inject.
            room  : Room name (must match the configured watcher room).
            sender: Username of the simulated sender.
            role  : Role assigned to the sender (default: OWNER).
        """
        if not self._handler:
            raise RuntimeError(
                "No handler registered. Call SessionManager.run_once() first."
            )
        msg = IncomingMessage(
            id=f"inject-{uuid.uuid4().hex[:8]}",
            timestamp="",
            room=Room(id=room, name=room, type="script"),
            sender=User(id=sender, username=sender),
            role=role,
            text=text,
        )
        await self._handler(msg)

    async def receive_reply(self, timeout: float = 120.0) -> str:
        """Block until a reply is available and return its text.

        Args:
            timeout: Seconds to wait before raising asyncio.TimeoutError.

        Returns:
            The agent's reply as a plain string.
        """
        return await asyncio.wait_for(self._reply_queue.get(), timeout=timeout)
