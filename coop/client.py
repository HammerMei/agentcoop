"""The doorbell as an agent sees it (design §3.2, §3.3): poll a wake, read, send,
done, bio. Everything agent-side — the fake agent, the ``coop read / send / done /
bio`` commands, the utility for CLI agents — talks to the gateway only through a
:class:`Doorbell`, so a gateway the fake agent can drive, a real client can too. The
errors a call can raise are in :mod:`coop.errors`.

:class:`InProcessDoorbell` calls a :class:`~coop.room.gateway.Gateway` directly (the
mechanism tests and the scenario runner). The HTTP client is
:class:`coop.agent.client.HttpDoorbell`.
"""

from __future__ import annotations

from typing import Protocol

from .room.gateway import Gateway
from .room.model import Post, Reply, Sent, Wake


class Doorbell(Protocol):
    async def poll(self, wait: float = 0) -> Wake | None:
        """The next pending wake, waiting up to ``wait`` seconds for one over HTTP; in
        process the wake is announced, so the wait is unused."""
        ...

    async def read(self, wake_id: str) -> tuple[Wake, list[Post]]:
        """The wake's hint and the posts after the cursor through its mark (``coop
        read --wake``)."""
        ...

    async def read_room(self, room: str, after: str | None = None) -> list[Post]:
        """The posts after the cursor, or after the post ``after`` names, through the
        latest, outside a wake (``coop read --room [--after ID]``)."""
        ...

    async def send(self, reply: Reply, room: str | None = None) -> Sent:
        """One reply. ``reply.wake_id`` names the wake it belongs to (its room follows);
        an unsolicited send (``wake_id=None``) names its room. The result carries the
        post and, where the room does not enforce, the rules the send broke."""
        ...

    async def done(self, wake_id: str, answered: tuple[str, ...] = (), failed: str | None = None) -> bool: ...

    async def bio(self, text: str) -> int: ...


class InProcessDoorbell:
    """One agent's doorbell onto a gateway in the same process."""

    def __init__(self, gateway: Gateway, agent: str) -> None:
        self.gateway = gateway
        self.agent = agent

    async def poll(self, wait: float = 0) -> Wake | None:
        return self.gateway.poll(self.agent)

    async def read(self, wake_id: str) -> tuple[Wake, list[Post]]:
        return self.gateway.read(self.agent, wake_id)

    async def read_room(self, room: str, after: str | None = None) -> list[Post]:
        return self.gateway.read_room(self.agent, room, after)

    async def send(self, reply: Reply, room: str | None = None) -> Sent:
        return await self.gateway.send(self.agent, reply, room)

    async def done(self, wake_id: str, answered: tuple[str, ...] = (), failed: str | None = None) -> bool:
        return await self.gateway.done(self.agent, wake_id, answered, failed)

    async def bio(self, text: str) -> int:
        return self.gateway.bio(self.agent, text)
