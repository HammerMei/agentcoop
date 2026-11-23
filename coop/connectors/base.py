"""What a connector is to the daemon (design §5): one object per chat server that
holds the gateway's bot accounts there, delivers every post it sees as a normalised
:class:`~coop.room.model.Post`, and does the platform calls the room core needs.

The daemon drives it through this protocol only, so a new platform is a new
implementation of it and nothing else. A room has one namespace, the platform's
(design §2): a post's ``author`` is its username, a bot is its account's username,
and text goes in and out as written. A connector maps nothing; the agent an
account belongs to is the gateway's binding, and the daemon names bots to the
connector by username.

Events reach the daemon through one callback, in the order the connector saw
them. A ``Posted`` is one copy of one post, from one bot's connection: with N bots
in a room the daemon receives N copies and the room log keeps one.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from ..config import Server
from ..room.model import Position, Post
from ..room.roster import Roster

RoomKind = Literal["channel", "dm", "group"]

# the length of a group message's handle digest; the legacy gateway's choice, kept so a
# room's handle reads the same under both
_DIGEST_DIGITS = 16


@dataclass(frozen=True)
class RoomInfo:
    """A room as the platform describes it. ``name`` is a channel's name, a 1:1 DM's
    the counterpart's username, a group message's whatever the platform shows; the
    room's handle is :func:`handle_for`."""

    id: str
    name: str
    kind: RoomKind = "channel"
    archived: bool = False

    @property
    def is_dm(self) -> bool:
        """A 1:1 DM, where a post addresses the bot by itself (§3.1). A group message
        is a room: a post there addresses nobody until it mentions someone."""
        return self.kind == "dm"


def handle_for(server: str, info: RoomInfo, username: str) -> str:
    """The room's handle: what ``rooms:`` patterns match, what ``coop list`` shows and
    what ``pause / resume / reset`` take. Every segment is a platform name.

    | kind | handle |
    |---|---|
    | channel | ``<server>:<channel name>`` |
    | 1:1 DM | ``<server>:dm:<bot username>:<counterpart username>``: the bot's own DMs are
      distinct rooms, so the bot ``username`` that lists the room is in the handle |
    | group message | ``<server>:gdm:<16 hex of the room id's digest>``: the member list
      moves, includes the bot and has no fixed order, so nobody's name is in it |

    The ``dm:`` and ``gdm:`` prefixes cannot be forged by a channel: Mattermost and
    Rocket.Chat channel names are slugs without ``:``.
    """
    if info.kind == "dm":
        return f"{server}:dm:{username}:{info.name}"
    if info.kind == "group":
        return f"{server}:gdm:{_digest(info.id)}"
    return f"{server}:{info.name}"


def _digest(room_id: str) -> str:
    return hashlib.sha256(room_id.encode()).hexdigest()[:_DIGEST_DIGITS]


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
    seen_by: str  # the bot's username


@dataclass(frozen=True)
class Membership:
    """A hosted bot joined or left a room (design §3.6 step 4)."""

    room_id: str
    username: str
    joined: bool


Event = Posted | Membership
OnEvent = Callable[[Event], Awaitable[None]]


class Connector(Protocol):
    """One chat server. The daemon adds bots (at start and on a reload), watches rooms
    with the roster that decides each author's kind, and receives events."""

    name: str  # the server's name in the configuration

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def configure(self, server: Server) -> None:
        """A reload's view of the server: the bots it holds and their usernames. Called
        before any bot the new configuration adds is connected."""
        ...

    async def add_bot(self, username: str, token: str) -> list[RoomInfo]:
        """Connect the bot account ``username`` with ``token`` and return the rooms it
        is a member of. Opening a connection at runtime is the same call as at start.
        A connector knows bots by their usernames; the agent an account belongs to is
        the gateway's binding (design §2)."""
        ...

    async def remove_bot(self, username: str) -> None: ...

    def watch(self, room_id: str, roster: Roster) -> None:
        """From now on, posts in the room are normalised with ``roster``: it decides each
        author's kind."""
        ...

    def unwatch(self, room_id: str) -> None: ...

    async def room(self, room_id: str) -> RoomInfo: ...

    async def members(self, room_id: str) -> list[MemberInfo]: ...

    async def history(self, room_id: str, after: Position | None = None, before: Position | None = None,
                      limit: int = 60) -> list[Post]:
        """One page of the room's posts, oldest first, strictly inside ``(after, before)``
        and nearest ``before`` (or the latest) when there are more than ``limit``. The
        caller pages by passing the oldest returned position as the next ``before``. An
        empty page means the platform has nothing more in that direction, never that a
        page held only posts the core does not see (system posts)."""
        ...

    async def post(self, username: str, room_id: str, text: str, thread: str | None) -> Post:
        """Post as the bot ``username``; ``thread`` ``None`` is a new top-level post. Raises
        :class:`~coop.room.platform.RefusedPost` when the platform refuses it and
        :class:`~coop.room.platform.PlatformError` when it did not answer."""
        ...

    async def fetch_file(self, username: str, file_id: str) -> bytes:
        """``KeyError`` for a file the platform does not have, ``PlatformError`` when it
        did not answer."""
        ...

    async def typing(self, username: str, room_id: str, thread: str | None) -> None:
        """Best effort; never raises."""
        ...
