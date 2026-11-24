"""What the core needs from the chat platform, for one room.

The core never talks to a connector. It reads the room's posts between two positions
(``CURSOR``), looks members and roles up, and posts on a bot's behalf. The test
platform (``tests/coop/helpers.py`` ``FakePlatform``) is an in-memory room; the
connectors of a later increment implement the same protocol over Mattermost and
Rocket.Chat, where a position is a ``(timestamp, post id)`` pair.
"""

from __future__ import annotations

from typing import Protocol

from .model import Member, Position, Post, PostKind


class PlatformError(Exception):
    """The platform did not answer, or failed: whether the call took effect is not
    known. The doorbell answers 502 and the agent reads the room before trying again
    (design §3.3)."""


class RefusedPost(Exception):
    """The platform refused an outbound post in ``thread``."""

    def __init__(self, thread: str) -> None:
        super().__init__(f"platform refused a post in thread {thread!r}")
        self.thread = thread


class Platform(Protocol):
    name: str
    is_dm: bool

    def members(self) -> dict[str, Member]: ...

    def is_archived(self, thread: str) -> bool: ...

    def posts_after(self, position: Position | None, through: Position | None = None) -> list[Post]:
        """Posts after ``position`` (exclusive; ``None`` = from the start), through
        ``through`` (inclusive) when given, in server order."""
        ...

    def thread_posts(self, thread: str, newest_first: bool = False) -> list[Post]: ...

    def position_of(self, post_id: str) -> Position | None:
        """The position of a post the room holds, or ``None``."""
        ...

    def last_position(self) -> Position | None:
        """The position of the newest post, or ``None`` in an empty room."""
        ...

    async def typing(self, author: str, thread: str) -> None:
        """The platform's typing indicator from ``author``'s account in ``thread``, best
        effort (design §3.2): sent when an agent's poll picks up an ``addressed`` wake.
        Mattermost shows it for a few seconds and clears it itself."""
        ...

    async def fetch_attachment(self, file_id: str) -> bytes:
        """The bytes of one attachment by its platform file id, fetched now with the
        bot's account (design §3.3: nothing is downloaded on receipt). Raises
        ``KeyError`` for an id the platform does not know."""
        ...

    async def post(self, author: str, text: str, thread: str | None, kind: PostKind | None = None) -> Post:
        """Post as ``author`` (a bot the gateway holds the account for); ``thread``
        ``None`` posts at the top level, and the returned post's ``thread`` is then its
        own id. Raises :class:`RefusedPost` when the platform refuses it. The post
        reaches the core again as an inbound post, like every other post in the room."""
        ...
