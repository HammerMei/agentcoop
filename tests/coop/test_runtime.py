"""The core under the real event loop: the room lock serialises concurrent entries from
different tasks (one event queue per room), and :class:`AsyncioClock` schedules and
cancels on wall-clock time. Everything else in ``tests/coop`` runs on the simulated
clock in one task, where the lock is only ever re-entered."""

from __future__ import annotations

import asyncio

from coop.room.clock import AsyncioClock
from coop.room.core import RoomCore
from coop.room.gateway import Gateway
from coop.room.model import MAIN_THREAD, Post, Reply
from coop.room.trace import Trace
from tests.coop.helpers import FAST, members
from tools.fakeagent.sim import FakePlatform, ScriptedClassifier, SimClock


class SlowPlatform(FakePlatform):
    """A platform whose ``post`` yields to the loop before and after appending, and
    records which entries overlapped."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.inside = 0
        self.overlaps = 0

    async def post(self, author: str, text: str, thread: str = MAIN_THREAD, kind=None) -> Post:
        mine = self.role_of(author) == "bot"  # the core's own posts; a human's post is the platform's business
        if mine:
            self.inside += 1
            self.overlaps += self.inside - 1
        await asyncio.sleep(0.01)
        post = self.append(author, text, thread, kind)
        await asyncio.sleep(0.01)
        if mine:
            self.inside -= 1
        return post


async def test_concurrent_sends_and_posts_from_different_tasks_do_not_interleave_inside_the_room():
    clock, trace = SimClock(), Trace()
    room = SlowPlatform("team", members(("a", "b")), clock, trace)
    core = RoomCore("team", room, clock, trace, ["a", "b"], ["a", "b"], FAST, ScriptedClassifier())
    gateway = Gateway(clock, trace)
    gateway.add_room(core)
    sends = [gateway.send(bot, Reply("main", to=["H"], text=f"{bot} {i}"), room="team")
             for i in range(5) for bot in ("a", "b")]
    posts = [room.post("H", f"human {i}") for i in range(5)]
    await asyncio.gather(*sends, *posts)
    assert room.overlaps == 0  # no two sends were inside the platform at once
    assert len([p for p in room.posts if p.kind == "bot"]) == 10 and len(room.posts) == 15
    assert trace.count("send") == 10 and all(e.post is not None for e in trace.of("send"))


async def test_asyncio_clock_runs_after_the_delay_and_a_cancelled_run_never_happens():
    clock = AsyncioClock()
    ran: list[str] = []

    async def mark(name: str) -> None:
        ran.append(name)

    before = clock.now()
    clock.at(0.02, mark, "kept")
    handle = clock.at(0.02, mark, "cancelled")
    handle.cancel()
    await asyncio.sleep(0.05)
    assert ran == ["kept"] and clock.now() >= before + 0.05
