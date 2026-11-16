"""The simulation core: a simulated clock, an event heap ordered by (time, seq), one
:class:`~tools.roomsim.room.Room`, and the gateways that receive every post.

Everything that takes time is an event: a scripted human post, a turn ending after its
duration, a classifier decision arriving after its latency, a periodic sweep, a job
falling due. Events with equal times run in the order they were scheduled, so a run
is deterministic for scripted bots and reproducible for recorded ones.
"""

from __future__ import annotations

import heapq
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .model import MAIN_THREAD, Post, PostKind
from .room import Room
from .trace import Trace

if TYPE_CHECKING:
    from .gateway import SimGateway


@dataclass(order=True)
class _Event:
    time: float
    seq: int
    fn: Callable[..., Awaitable[Any]] = field(compare=False)
    args: tuple = field(compare=False, default=())


class Simulation:
    def __init__(self, room: Room, start_time: float = 0.0) -> None:
        self.room = room
        self.clock = start_time
        self.trace = Trace()
        self.gateways: list[SimGateway] = []
        self._heap: list[_Event] = []
        self._seq = 0

    # ── time ────────────────────────────────────────────────────────────────

    def now(self) -> float:
        return self.clock

    def at(self, delay: float, fn: Callable[..., Awaitable[Any]], *args: Any) -> None:
        """Run ``fn(*args)`` ``delay`` simulated seconds from now (``0`` = after the
        events already due now)."""
        self._seq += 1
        heapq.heappush(self._heap, _Event(self.clock + max(delay, 0.0), self._seq, fn, args))

    async def run_until(self, time: float) -> None:
        while self._heap and self._heap[0].time <= time:
            event = heapq.heappop(self._heap)
            self.clock = event.time
            await event.fn(*event.args)
        self.clock = max(self.clock, time)

    async def run_for(self, seconds: float) -> None:
        await self.run_until(self.clock + seconds)

    async def drain(self, horizon: float) -> None:
        """Run until no event is due before ``horizon`` (sweeps repeat forever, so a
        horizon is required)."""
        await self.run_until(horizon)

    # ── the room ────────────────────────────────────────────────────────────

    def add_gateway(self, gateway: SimGateway) -> None:
        self.gateways.append(gateway)
        gateway.start()

    def append(self, author: str, text: str, thread: str = MAIN_THREAD, kind: PostKind | None = None) -> Post:
        """Append a post in server order without delivering it yet. A gateway posting
        for its own bot counts the post before the other gateways see it (5.3)."""
        post = self.room.post(self.clock, author, text, thread, kind)
        self.trace.add(self.clock, "post", seq=post.seq, thread=thread, author=author,
                       post_kind=post.kind, mentions=sorted(post.mentions))
        return post

    async def deliver(self, post: Post) -> None:
        for gateway in self.gateways:
            await gateway.on_post(post)

    async def post(self, author: str, text: str, thread: str = MAIN_THREAD, kind: PostKind | None = None) -> Post:
        """Append a post and deliver it to every gateway."""
        post = self.append(author, text, thread, kind)
        await self.deliver(post)
        return post

    def human(self, delay: float, author: str, text: str, thread: str = MAIN_THREAD) -> None:
        """Scripted human event: ``author`` posts ``text`` ``delay`` seconds from now."""
        self.at(delay, self.post, author, text, thread)

    async def archive(self, thread: str) -> None:
        self.room.archive(thread)
        for gateway in self.gateways:
            gateway.on_thread_removed(thread)
