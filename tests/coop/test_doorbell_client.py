"""The fake agent drives the gateway through :class:`HttpDoorbell` (design §3.3): the
same agent loop that runs in process, over a real port."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from coop.agent.client import HttpDoorbell
from coop.errors import DoorbellError, InvalidReply, LostResponse, NoLiveLease, RefusedSend
from coop.room.clock import AsyncioClock
from coop.room.model import Reply
from tools.fakeagent.agent import FakeAgent, Script

from .helpers import FAST, Doorbell, Sandbox, reply

TOKENS = {"tok-a": "a", "tok-b": "b", "tok-c": "c", "tok-z": "z"}


async def until(condition, seconds: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "timed out waiting"
        await asyncio.sleep(0.02)


@pytest.fixture
async def http_sandbox():
    """A sandbox on the event loop, the fake agent serving every bot over HTTP."""
    script = (Script()
              .on(reply(["H", "b"], "done"), bot="a", source="addressed", once=True)
              .on(reply(["H", "a"], "deployed"), bot="b", when=lambda t: any(p.author == "a" for p in t.delivered)))
    sb = Sandbox(clock=AsyncioClock(), attach_agent=False, params=FAST, script=script, duration=0.02)
    async with Doorbell(sb.gateway, TOKENS) as doorbell:
        agent = FakeAgent(sb.clock, script, duration=0.02)
        doorbells = {bot: HttpDoorbell(doorbell.url, f"tok-{bot}") for bot in ("a", "b", "c")}
        for bot, db in doorbells.items():
            agent.attach(bot, db)
        tasks = [asyncio.ensure_future(agent.serve(bot, wait=2)) for bot in doorbells]
        sb.agent, sb.doorbells = agent, doorbells
        try:
            yield sb
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for db in doorbells.values():
                await db.close()


async def test_the_handoff_runs_over_http(http_sandbox):
    sb = http_sandbox
    await sb.human("@a run the migration; @b deploy after")
    await until(lambda: len(sb.posts_by("b")) == 1)
    assert [p.text for p in sb.posts_by("a")] == ["@H @b done"]
    assert [p.text for p in sb.posts_by("b")] == ["@H @a deployed"]
    ends = [e for e in sb.trace.of("turn_end") if e.outcome != "normal"]
    assert ends == [], [(e.bot, e.outcome) for e in ends]
    # every bot's turn read before it sent (design §3.3)
    assert {e.bot for e in sb.trace.of("read")} >= {"a", "b"}
    assert all(sb.gateway.online(bot, 2) for bot in ("a", "b", "c"))


async def test_the_client_maps_the_gateways_errors(http_sandbox):
    sb = http_sandbox
    a, z = sb.doorbells["a"], HttpDoorbell(sb.doorbells["a"].address, "tok-z")
    try:
        with pytest.raises(NoLiveLease):
            await a.read("w-nope")
        with pytest.raises(InvalidReply):
            await a.send(Reply(thread="main", wake_id=None), room="team")
        await sb.human("mute: @a")
        with pytest.raises(RefusedSend, match="mute"):
            await a.send(Reply(thread="main", text="x", wake_id=None), room="team")
        with pytest.raises(DoorbellError) as exc:
            await z.read_room("team")
        assert exc.value.status == 403
        assert await a.bio("graphs") == 1
        assert (await a.rules())[1] == "rules"
    finally:
        await z.close()


async def test_a_send_the_gateway_does_not_answer_is_lost_not_retried(http_sandbox, monkeypatch):
    a = http_sandbox.doorbells["a"]
    calls = []

    async def hang(*args, **kwargs):
        calls.append(1)
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(await a._http(), "post", hang)
    with pytest.raises(LostResponse, match="may have been posted"):
        await a.send(Reply(thread="main", text="x", wake_id=None), room="team")
    assert len(calls) == 1
