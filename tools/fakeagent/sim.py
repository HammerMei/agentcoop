"""The simulated environment the fake agent runs the core in: a discrete-event clock,
an in-memory room that delivers posts inline, and a scripted classifier. The
mechanism tests (``tests/coop``) and the scenario runner (``python -m tools.fakeagent``)
build the same pieces; nothing here is a test helper that the tool package would
have to import back from the tests.
"""

from __future__ import annotations

import heapq
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from coop.room.classifier import Classifier, ClassifierRequest, ClassifierResult
from coop.room.controls import is_control_line, parse_controls
from coop.room.core import RoomCore
from coop.room.model import ALL, MAIN_THREAD, Attachment, Decision, Member, Post, PostKind
from coop.room.platform import RefusedPost
from coop.room.trace import Trace
from coop.room.words import count_words

# ── the clock ───────────────────────────────────────────────────────────────


@dataclass(order=True)
class _Event:
    time: float
    seq: int
    fn: Callable[..., Awaitable[Any]] = field(compare=False)
    args: tuple = field(compare=False, default=())
    cancelled: bool = field(compare=False, default=False)

    def cancel(self) -> None:
        self.cancelled = True


class SimClock:
    """A simulated clock and an event heap ordered by (time, seq). Events with equal
    times run in the order they were scheduled, so a run is deterministic."""

    def __init__(self, start_time: float = 0.0) -> None:
        self.clock = start_time
        self._heap: list[_Event] = []
        self._seq = 0

    def now(self) -> float:
        return self.clock

    def at(self, delay: float, fn: Callable[..., Awaitable[Any]], *args: Any) -> _Event:
        self._seq += 1
        event = _Event(self.clock + max(delay, 0.0), self._seq, fn, args)
        heapq.heappush(self._heap, event)
        return event

    async def run_until(self, time: float) -> None:
        while self._heap and self._heap[0].time <= time:
            event = heapq.heappop(self._heap)
            if event.cancelled:
                continue
            self.clock = event.time
            await event.fn(*event.args)
        self.clock = max(self.clock, time)

    async def run_for(self, seconds: float) -> None:
        await self.run_until(self.clock + seconds)


# ── the platform ────────────────────────────────────────────────────────────

_MENTION = re.compile(r"@([A-Za-z0-9._-]+)")
_ROOM_WIDE = {"all", "channel", "here"}


class FakePlatform:
    """One room with server-ordered posts, members with roles, threads, and the
    platform behaviours the mechanisms depend on: history after a position (CURSOR)
    and refusing an outbound post. A post is delivered to every attached core inline.
    Positions are the seq; ids are ``p<seq>``."""

    def __init__(self, name: str, members: list[Member], clock: SimClock, trace: Trace, is_dm: bool = False) -> None:
        self.name = name
        self.is_dm = is_dm
        self.clock = clock
        self.trace = trace
        self._members: dict[str, Member] = {m.username: m for m in members}
        self.posts: list[Post] = []
        self.archived: set[str] = set()
        # Threads in which the platform refuses the next outbound post.
        self.refusing: set[str] = set()
        self.cores: list[RoomCore] = []
        self._seq = 0
        # Attachment bytes by platform file id (``f<n>``).
        self.files: dict[str, bytes] = {}

    # membership
    def members(self) -> dict[str, Member]:
        return self._members

    def role_of(self, username: str) -> str | None:
        member = self._members.get(username)
        return member.role if member else None

    def join(self, member: Member) -> None:
        self._members[member.username] = member

    def bots(self) -> list[str]:
        return [m.username for m in self._members.values() if m.is_bot]

    def is_archived(self, thread: str) -> bool:
        return thread in self.archived

    # posting
    def mentions_in(self, text: str) -> frozenset[str]:
        """Mentions the platform records, except names that appear only on a control
        line: ``mute: @a`` names a target, it does not address ``a``."""
        found = set()
        prose = "\n".join(line for line in text.splitlines() if not is_control_line(line))
        for name in _MENTION.findall(prose):
            if name.lower() in _ROOM_WIDE:
                found.add(ALL)
            elif name in self._members:
                found.add(name)
        return frozenset(found)

    def append(self, author: str, text: str, thread: str | None = MAIN_THREAD, kind: PostKind | None = None,
               attachments: tuple[tuple[str, str, bytes], ...] = ()) -> Post:
        """Append a post in server order without delivering it. Raises ``RefusedPost``
        when the platform refuses it (an archived thread, or a thread marked refusing).
        ``thread`` ``None`` is a top-level post that opens its own thread, as the real
        platforms do (scenario posts stay in ``main``, the simulator's one top level).
        ``attachments`` are ``(name, content type, bytes)`` files stored by file id."""
        if thread in self.archived or thread in self.refusing:
            self.refusing.discard(thread)
            raise RefusedPost(thread)
        self._seq += 1
        if thread is None:
            thread = f"p{self._seq}"
        files = []
        for name, content_type, data in attachments:
            file_id = f"f{len(self.files) + 1}"
            self.files[file_id] = data
            files.append(Attachment(file_id, name, content_type, len(data)))
        post = Post(
            id=f"p{self._seq}",
            position=self._seq,
            time=self.clock.now(),
            thread=thread,
            author=author,
            text=text,
            kind=kind or self._kind_of(author),
            mentions=self.mentions_in(text),
            controls=parse_controls(text) if kind != "notice" else None,
            words=count_words(text),
            author_id=f"u_{author}",
            attachments=tuple(files),
        )
        self.posts.append(post)
        self.trace.add(self.clock.now(), "post", id=post.id, thread=thread, author=author,
                       post_kind=post.kind, mentions=sorted(post.mentions))
        return post

    async def deliver(self, post: Post) -> None:
        for core in self.cores:
            await core.on_post(post)

    async def post(self, author: str, text: str, thread: str | None = MAIN_THREAD, kind: PostKind | None = None,
                   attachments: tuple[tuple[str, str, bytes], ...] = ()) -> Post:
        post = self.append(author, text, thread, kind, attachments)
        await self.deliver(post)
        return post

    async def fetch_attachment(self, file_id: str) -> bytes:
        return self.files[file_id]

    def _kind_of(self, author: str) -> PostKind:
        role = self.role_of(author)
        if role == "bot":
            return "bot"
        if role == "integration":
            return "integration"
        return "human"

    # history
    def posts_after(self, position, through=None) -> list[Post]:
        return [p for p in self.posts if (position is None or p.position > position)
                and (through is None or p.position <= through)]

    def thread_posts(self, thread: str, newest_first: bool = False) -> list[Post]:
        posts = [p for p in self.posts if p.thread == thread]
        if newest_first:
            posts.reverse()
        return posts

    def last_position(self):
        return self._seq or None

    def by_id(self, post_id: str) -> Post:
        return next(p for p in self.posts if p.id == post_id)

    def position_of(self, post_id: str):
        return next((p.position for p in self.posts if p.id == post_id), None)

    async def archive(self, thread: str) -> None:
        self.archived.add(thread)
        for core in self.cores:
            core.on_thread_removed(thread)


# ── the classifier doubles ──────────────────────────────────────────────────

ClassifierScript = Decision | Callable[[ClassifierRequest], Decision]


@dataclass
class ScriptedClassifier:
    """Answers by post id (``decisions``), else ``default``. ``failures`` maps a post id
    to the number of consecutive calls for that post that fail. ``latency`` is the
    simulated seconds before a result arrives."""

    decisions: dict[str, ClassifierScript] = field(default_factory=dict)
    default: ClassifierScript = Decision()
    failures: dict[str, int] = field(default_factory=dict)
    latency: float = 0.5
    calls: list[ClassifierRequest] = field(default_factory=list)

    async def classify(self, request: ClassifierRequest) -> ClassifierResult:
        self.calls.append(request)
        post_id = request.post.id
        if self.failures.get(post_id, 0) > 0:
            self.failures[post_id] -= 1
            return ClassifierResult(error=f"injected failure for post {post_id}", latency=self.latency)
        script = self.decisions.get(post_id, self.default)
        return ClassifierResult(script(request) if callable(script) else script, latency=self.latency)


@dataclass
class InjectedFailures:
    """Fails ``failures[id]`` consecutive calls for a post without asking ``inner``."""

    inner: Classifier
    failures: dict[str, int] = field(default_factory=dict)
    latency: float = 0.5

    async def classify(self, request: ClassifierRequest) -> ClassifierResult:
        post_id = request.post.id
        if self.failures.get(post_id, 0) > 0:
            self.failures[post_id] -= 1
            return ClassifierResult(error=f"injected failure for post {post_id}", latency=self.latency)
        return await self.inner.classify(request)
