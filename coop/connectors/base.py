"""What a connector is to the daemon (design §5): one object per chat server that
holds the gateway's bot accounts there, delivers every post it sees as a normalised
:class:`~coop.room.model.Post`, and does the platform calls the room core needs.

The daemon drives it through this protocol only, so a new platform is a new
implementation of it and nothing else. The core never sees a platform username: a
post's ``author`` is the agent name when the author is a bot the configuration
knows, and the username otherwise. The connector maps in both directions, inbound
on every post and outbound on every ``@name`` it posts.

Events reach the daemon through one callback, in the order the connector saw
them. A ``Posted`` is one copy of one post, from one bot's connection: with N bots
in a room the daemon receives N copies and the room log keeps one.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from ..room.model import Position, Post
from ..room.roster import Roster


@dataclass(frozen=True)
class RoomInfo:
    """A room as the platform describes it. ``name`` is what ``rooms:`` patterns match,
    after the server prefix; a DM's is the counterpart's username."""

    id: str
    name: str
    is_dm: bool = False
    archived: bool = False


@dataclass(frozen=True)
class MemberInfo:
    """A room member as the platform describes it. ``username`` is the platform's;
    ``is_bot`` is the platform's hint, the last word in deciding who is a bot."""

    username: str
    display_name: str = ""
    nickname: str = ""
    is_bot: bool = False


@dataclass(frozen=True)
class Posted:
    """One copy of a post, as seen by ``seen_by``'s connection."""

    room_id: str
    post: Post
    seen_by: str  # agent name


@dataclass(frozen=True)
class Membership:
    """A hosted bot joined or left a room (design §3.6 step 4)."""

    room_id: str
    agent: str
    joined: bool


Event = Posted | Membership
OnEvent = Callable[[Event], Awaitable[None]]


class Connector(Protocol):
    """One chat server. The daemon adds bots (at start and on a reload), watches rooms
    with the roster that decides each author's kind, and receives events."""

    name: str  # the server's name in the configuration

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def add_bot(self, agent: str, token: str) -> list[RoomInfo]:
        """Connect ``agent``'s bot account with ``token`` and return the rooms it is a
        member of. Opening a connection at runtime is the same call as at start."""
        ...

    async def remove_bot(self, agent: str) -> None: ...

    def watch(self, room_id: str, roster: Roster) -> None:
        """From now on, posts in the room are normalised with ``roster``: it decides each
        author's kind and which usernames map to agent names."""
        ...

    def unwatch(self, room_id: str) -> None: ...

    async def room(self, room_id: str) -> RoomInfo: ...

    async def members(self, room_id: str) -> list[MemberInfo]: ...

    async def history(self, room_id: str, after: Position | None = None, before: Position | None = None,
                      limit: int = 60) -> list[Post]:
        """One page of the room's posts, oldest first, strictly inside ``(after, before)``
        and nearest ``before`` (or the latest) when there are more than ``limit``. The
        caller pages by passing the oldest returned position as the next ``before``."""
        ...

    async def post(self, agent: str, room_id: str, text: str, thread: str | None) -> Post:
        """Post as ``agent``'s bot; ``thread`` ``None`` is a new top-level post. Raises
        :class:`~coop.room.platform.RefusedPost`."""
        ...

    async def fetch_file(self, agent: str, file_id: str) -> bytes: ...

    async def typing(self, agent: str, room_id: str, thread: str | None) -> None:
        """Best effort; never raises."""
        ...
