"""The per-room log the room core reads: the ``Platform`` of design §3.1, kept in
memory from the connector's events and the platform's history, one entry per post
however many bot connections saw it (spec §5.1).

The core's platform calls are synchronous reads over this log plus two platform
calls, ``post`` and ``fetch_attachment``, which go to the connector. The log is
filled two ways: ``ingest`` for every live copy a connection delivers, and
``backfill`` for the history the daemon fetches at start and on reconnect, so the
range ``[cursor, hwm]`` a read needs is here before the wake is issued.
"""

from __future__ import annotations

import bisect
from collections.abc import Awaitable, Callable

from ..room.model import Member, Position, Post, PostKind, Role
from ..room.platform import RefusedPost
from ..room.roster import Roster
from .base import Connector, MemberInfo, RoomInfo


class RoomLog:
    """``hosted`` are the usernames of the bots this gateway posts as in the room; a
    file is fetched through the first of them, since any member's token can read it."""

    def __init__(self, connector: Connector, info: RoomInfo, roster: Roster, hosted: list[str]) -> None:
        self.connector = connector
        self.info = info
        self.roster = roster
        self.hosted = list(hosted)
        self.is_dm = info.is_dm
        self.posts: list[Post] = []
        self._keys: list[Position] = []
        self._ids: set[str] = set()
        self.archived: set[str] = set()
        self._members: dict[str, Member] = {}
        # Told of every post new to the log, the bot's own included: the core's ``on_post``.
        self.on_new: Callable[[Post], Awaitable[None]] | None = None

    @property
    def id(self) -> str:
        return self.info.id

    # ── filling ─────────────────────────────────────────────────────────────

    def ingest(self, post: Post) -> bool:
        """One copy of a post. Returns whether it was new to the log."""
        if post.id in self._ids:
            return False
        at = bisect.bisect_left(self._keys, post.position)
        self._keys.insert(at, post.position)
        self.posts.insert(at, post)
        self._ids.add(post.id)
        return True

    async def admit(self, post: Post) -> bool:
        """One copy of a post, delivered onward when it is new: a copy the log already
        holds, from another connection or the echo of the bot's own post, goes no further."""
        if not self.ingest(post):
            return False
        if self.on_new is not None:
            await self.on_new(post)
        return True

    def backfill(self, posts: list[Post]) -> int:
        """History the daemon fetched, into the log only; returns how many were new. The
        daemon replays what the core should see through the core's own replay path."""
        return sum(self.ingest(p) for p in posts)

    def set_members(self, members: list[MemberInfo]) -> None:
        """The room's membership by username: a bot the roster knows is a ``bot`` member,
        a listed owner an ``owner``, anyone else a ``guest``, and a platform-flagged
        account the roster does not list a ``bot``."""
        found: dict[str, Member] = {}
        for m in members:
            name = m.username
            kind: PostKind = self.roster.kind_of(name, "bot" if m.is_bot else None)
            role: Role = "bot" if kind == "bot" else ("owner" if any(
                name in people for people in self.roster.owners.values()) else "guest")
            found[name] = Member(name, role, display_name=m.display_name, nickname=m.nickname)
        self._members = found

    # ── Platform ────────────────────────────────────────────────────────────

    def members(self) -> dict[str, Member]:
        return self._members

    def is_archived(self, thread: str) -> bool:
        return thread in self.archived

    def posts_after(self, position: Position | None, through: Position | None = None) -> list[Post]:
        start = 0 if position is None else bisect.bisect_right(self._keys, position)
        stop = len(self._keys) if through is None else bisect.bisect_right(self._keys, through)
        return self.posts[start:stop]

    def thread_posts(self, thread: str, newest_first: bool = False) -> list[Post]:
        posts = [p for p in self.posts if p.thread == thread]
        if newest_first:
            posts.reverse()
        return posts

    def last_position(self) -> Position | None:
        return self._keys[-1] if self._keys else None

    def position_of(self, post_id: str) -> Position | None:
        post = self.by_id(post_id)
        return None if post is None else post.position

    def by_id(self, post_id: str) -> Post | None:
        if post_id not in self._ids:
            return None
        return next(p for p in self.posts if p.id == post_id)

    async def fetch_attachment(self, file_id: str) -> bytes:
        if not self.hosted:
            raise KeyError(file_id)
        return await self.connector.fetch_file(self.hosted[0], file_id)

    async def post(self, author: str, text: str, thread: str | None, kind: PostKind | None = None) -> Post:
        if thread is not None and thread in self.archived:
            raise RefusedPost(thread)
        post = await self.connector.post(author, self.id, text, thread)
        await self.admit(post)  # the core sees it now, as every other post; the echo is then a duplicate
        return post
