"""The platform substitute: one room with server-ordered posts, members with roles,
threads, and the two platform behaviours the mechanisms depend on — history after a
post id (``CURSOR``) and refusing an outbound post (section 5.9, partial failure).

Mentions are what the platform records: ``@name`` of a member, or ``@all``.
"""

from __future__ import annotations

import re

from .controls import is_control_line, parse_controls
from .model import ALL, MAIN_THREAD, Member, Post, PostKind
from .words import count_words

_MENTION = re.compile(r"@([A-Za-z0-9._-]+)")
_ROOM_WIDE = {"all", "channel", "here"}


class Room:
    def __init__(self, name: str, members: list[Member], is_dm: bool = False) -> None:
        self.name = name
        self.is_dm = is_dm
        self.members: dict[str, Member] = {m.username: m for m in members}
        self.posts: list[Post] = []
        self.archived: set[str] = set()
        # Threads in which the platform refuses the next outbound post, by thread id.
        self.refusing: set[str] = set()
        self._seq = 0

    # ── membership ──────────────────────────────────────────────────────────

    def is_member(self, username: str) -> bool:
        return username in self.members

    def role_of(self, username: str) -> str | None:
        member = self.members.get(username)
        return member.role if member else None

    def bots(self) -> list[str]:
        return [m.username for m in self.members.values() if m.is_bot]

    def join(self, member: Member) -> None:
        self.members[member.username] = member

    # ── posting ─────────────────────────────────────────────────────────────

    def mentions_in(self, text: str) -> frozenset[str]:
        """Mentions the platform records, except names that appear only on a control
        line: ``mute: @a`` names a target, it does not address ``a``."""
        found = set()
        prose = "\n".join(line for line in text.splitlines() if not is_control_line(line))
        for name in _MENTION.findall(prose):
            if name.lower() in _ROOM_WIDE:
                found.add(ALL)
            elif name in self.members:
                found.add(name)
        return frozenset(found)

    def post(
        self,
        time: float,
        author: str,
        text: str,
        thread: str = MAIN_THREAD,
        kind: PostKind | None = None,
    ) -> Post:
        """Append a post in server order. Raises ``RefusedPost`` when the platform
        refuses it (an archived thread, or a thread marked refusing)."""
        if thread in self.archived or thread in self.refusing:
            self.refusing.discard(thread)
            raise RefusedPost(thread)
        self._seq += 1
        post = Post(
            seq=self._seq,
            time=time,
            thread=thread,
            author=author,
            text=text,
            kind=kind or self._kind_of(author),
            mentions=self.mentions_in(text),
            controls=parse_controls(text) if kind != "notice" else None,
            words=count_words(text),
        )
        self.posts.append(post)
        return post

    def _kind_of(self, author: str) -> PostKind:
        role = self.role_of(author)
        if role == "bot":
            return "bot"
        if role == "integration":
            return "integration"
        return "human"

    # ── history ─────────────────────────────────────────────────────────────

    def after(self, seq: int, through: int | None = None) -> list[Post]:
        """Posts after ``seq`` (exclusive), through ``through`` (inclusive) when given."""
        return [p for p in self.posts if p.seq > seq and (through is None or p.seq <= through)]

    def thread_posts(self, thread: str, newest_first: bool = False, limit: int | None = None):
        posts = [p for p in self.posts if p.thread == thread]
        if newest_first:
            posts.reverse()
        return posts[:limit] if limit is not None else posts

    def last_seq(self) -> int:
        return self._seq

    def by_seq(self, seq: int) -> Post:
        return self.posts[seq - 1]

    def archive(self, thread: str) -> None:
        self.archived.add(thread)


class RefusedPost(Exception):
    """The platform refused an outbound post in ``thread``."""

    def __init__(self, thread: str) -> None:
        super().__init__(f"platform refused a post in thread {thread!r}")
        self.thread = thread
