"""The daemon over a fake connector: hosting by rule, the event path into the core
(a bot's own reply included), persistence across a restart, membership at runtime,
and a reload applied in place."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml

from coop.config import ConfigError, parse
from coop.connectors.base import MemberInfo
from coop.room.model import Reply
from coop.service import Service

from .helpers import FakeConnector

NAMES = {"a": "a_bot", "b": "b_bot", "c": "c_bot", "x": "x_bot"}
PEOPLE = [MemberInfo("H"), MemberInfo("G"), MemberInfo("a_bot", is_bot=True), MemberInfo("b_bot", is_bot=True),
          MemberInfo("x_bot", is_bot=True)]
ROOM = "sweep_interval_seconds: 0, catch_up_idle_seconds: 0, classifier_enabled: false, lease_seconds: 60"
CONFIG = f"""
servers:
  fake:
    type: rocketchat
    url: http://fake
    owners: [H]
    guests: [G]
    bots:
      a: {{username: a_bot, token_file: a.bot}}
      b: {{username: b_bot, token_file: b.bot}}
      x: {{username: x_bot}}
agents:
  a: {{type: remote, token_file: a.agent}}
  b: {{type: remote, token_file: b.agent}}
rooms:
  "fake:team*": {{agents: [a, b, x], {ROOM}}}
"""


def config(tmp_path: Path, text: str = CONFIG):
    for name in ("a", "b", "c"):
        (tmp_path / f"{name}.bot").write_text(f"bot-token-{name}\n")
        (tmp_path / f"{name}.agent").write_text(f"agent-token-{name}\n")
    return parse(yaml.safe_load(text), tmp_path)


def connector() -> FakeConnector:
    fc = FakeConnector(names=NAMES)
    fc.add_room("c1", "team", PEOPLE)
    fc.add_room("c2", "other", PEOPLE)
    return fc


def service(tmp_path: Path, fc: FakeConnector, text: str = CONFIG) -> Service:
    def factory(server, on_event):
        fc.on_event = on_event
        return fc
    return Service(config(tmp_path, text), tmp_path / "state", factory, wake_seed=0)


async def settle() -> None:
    for _ in range(3):
        await asyncio.sleep(0.02)


@pytest.fixture
async def running(tmp_path):
    fc = connector()
    svc = service(tmp_path, fc)
    await svc.start(listen=False)
    yield svc, fc
    await svc.stop()


async def test_startup_hosts_the_ruled_room_and_a_bots_reply_wakes_the_other(running):
    svc, fc = running
    assert set(svc.rooms) == {"fake:team"} and fc.bots.keys() == {"a", "b"}
    assert svc.rooms["fake:team"].core.hosted == ["a", "b"] and svc.rooms["fake:team"].core.agents == ["a", "b", "x"]
    assert svc.tokens == {"agent-token-a": "a", "agent-token-b": "b"}
    await fc.human("c1", "H", "@a_bot hello")
    await settle()
    wake = svc.gateway.poll("a")
    assert wake is not None and wake.trigger.author == "H" and wake.trigger.author_role == "owner"
    _, posts = svc.gateway.read("a", wake.wake_id)
    assert [p.text for p in posts] == ["@a hello"]  # the bot's username became its agent name
    reply = Reply(thread=wake.starting_thread, to=["b"], text="your turn", wake_id=wake.wake_id)
    post = await svc.gateway.send("a", reply)
    assert fc.posts["c1"][-1].text == "@b your turn" and post.author == "a"
    await settle()
    wake_b = svc.gateway.poll("b")
    assert wake_b is not None and wake_b.trigger.author == "a" and wake_b.trigger.author_role == "agent"
    assert svc.gateway.poll("a") is None  # the echo of a's own post woke nobody twice
    assert await svc.gateway.done("a", wake.wake_id)
    assert svc.gateway.poll("b") is None and svc.gateway.holds_lease("b")


async def test_a_room_no_rule_names_is_not_hosted_and_a_guest_reads_as_guest(running):
    svc, fc = running
    assert "fake:other" not in svc.rooms
    await fc.human("c2", "H", "@a_bot anyone?")
    await settle()
    assert svc.gateway.poll("a") is None
    post = await fc.human("c1", "G", "@b_bot please")
    await settle()
    assert svc.gateway.deliverable("b", "fake:team", post).author_role == "guest"


async def test_state_is_saved_on_commit_and_a_restart_replays_what_it_missed(tmp_path):
    fc = connector()
    svc = service(tmp_path, fc)
    await svc.start(listen=False)
    await fc.human("c1", "H", "@a_bot hello")
    await settle()
    wake = svc.gateway.poll("a")
    svc.gateway.read("a", wake.wake_id)  # a done without a read is a failed turn, which saves nothing
    await svc.gateway.done("a", wake.wake_id)
    saved = json.loads((tmp_path / "state" / "fake" / "c1.json").read_text())
    assert tuple(saved["bots"]["a"]["cursor"]) == wake.hwm
    svc.gateway.bio("a", "I am a")
    assert json.loads((tmp_path / "state" / "agents.json").read_text())["agents"]["a"]["bio"] == "I am a"
    await svc.stop()
    fc.on_event = None
    missed = fc.make_post("c1", "H", "@b_bot while down")
    again = service(tmp_path, fc)
    await again.start(listen=False)
    core = again.rooms["fake:team"].core
    assert core.persisted["a"].cursor == wake.hwm and core.persisted["b"].cursor is None
    assert again.gateway.agents["a"].bio_version == 1
    await settle()
    wake_b = again.gateway.poll("b")
    assert wake_b is not None and wake_b.trigger.post_id == missed.id
    await again.stop()


async def test_a_bot_joining_a_room_at_runtime_is_hosted_at_the_latest_post_and_leaving_closes_it(running):
    svc, fc = running
    fc.add_room("c3", "team-2", [MemberInfo("H"), MemberInfo("a_bot", is_bot=True)])
    fc.make_post("c3", "H", "@b_bot before b was here")
    await fc.join("c3", "b")
    await settle()
    run = svc.rooms["fake:team-2"]
    assert run.core.hosted == ["b"] and run.started
    assert run.core.persisted["b"].cursor == run.log.last_position()
    assert svc.gateway.poll("b") is None  # nothing before the join is a wake
    await fc.human("c3", "H", "@b_bot now")
    await settle()
    assert svc.gateway.poll("b").room == "fake:team-2"
    await fc.leave("c3", "b")
    await settle()
    assert "fake:team-2" not in svc.rooms and svc.trace.last("turn_end", bot="b").outcome == "removed"


async def test_a_reload_adds_and_removes_bots_in_place_without_touching_another_bots_lease(running, tmp_path):
    svc, fc = running
    await fc.human("c1", "H", "@a_bot hello")
    await settle()
    wake = svc.gateway.poll("a")
    assert svc.gateway.holds_lease("a")
    fc.membership["c1"].append(MemberInfo("c_bot", is_bot=True))
    # b goes, c comes, and the room enforces from now on
    added = CONFIG.replace("      b: {username: b_bot, token_file: b.bot}", "      c: {username: c_bot, token_file: c.bot}")
    added = added.replace("  b: {type: remote, token_file: b.agent}", "  c: {type: remote, token_file: c.agent}")
    added = added.replace("agents: [a, b, x]", "agents: [a, c, x], enforce: true")
    tokens = svc.tokens
    notes = await svc.reload(config(tmp_path, added))
    core = svc.rooms["fake:team"].core
    assert core.hosted == ["a", "c"] and core.agents == ["a", "c", "x"] and core.params.enforce
    assert fc.bots.keys() == {"a", "c"} and "b" not in fc.bots
    assert svc.tokens is tokens and tokens == {"agent-token-a": "a", "agent-token-c": "c"}
    assert svc.gateway.holds_lease("a") and core.volatile["a"].turn.wake.wake_id == wake.wake_id
    assert core.persisted["c"].cursor == svc.rooms["fake:team"].log.last_position()
    assert any("'c' connected" in n for n in notes) and any("'b' disconnected" in n for n in notes)
    with pytest.raises(ConfigError, match="needs a restart"):
        await svc.reload(config(tmp_path, added.replace("url: http://fake", "url: http://elsewhere")))
    assert core.hosted == ["a", "c"]


async def test_a_reload_that_drops_the_rule_closes_the_room_and_one_that_adds_it_opens_it(running, tmp_path):
    svc, fc = running
    none = CONFIG.replace('"fake:team*"', '"fake:nothing"')
    notes = await svc.reload(config(tmp_path, none))
    assert svc.rooms == {} and any("closed" in n for n in notes)
    both = CONFIG.replace('"fake:team*"', '"fake:*"')
    notes = await svc.reload(config(tmp_path, both))
    assert set(svc.rooms) == {"fake:team", "fake:other"} and all(r.started for r in svc.rooms.values())
    await fc.human("c2", "H", "@a_bot anyone?")
    await settle()
    assert svc.gateway.poll("a").room == "fake:other"


async def test_operator_commands_and_status(running):
    svc, fc = running
    svc.pause("a", "fake:team")
    assert svc.status()["rooms"]["fake:team"]["bots"]["a"]["paused"]
    svc.resume("a", "fake:team")
    svc.reset("a", "fake:team")
    assert svc.rooms["fake:team"].core.persisted["a"].reset_pending
    with pytest.raises(KeyError, match="no hosted room"):
        svc.pause("a", "fake:nope")
    with pytest.raises(KeyError, match="not hosted"):
        svc.pause("x", "fake:team")
    status = svc.status()
    assert status["servers"] == {"fake": ["a", "b"]} and status["agents"]["a"]["online"] is False
    assert status["listening"] is None and status["rooms"]["fake:team"]["agents"] == ["a", "b", "x"]


async def test_the_doorbell_listens_with_the_configured_address(tmp_path):
    import httpx

    fc = connector()
    svc = service(tmp_path, fc, CONFIG + "gateway:\n  listen: 127.0.0.1:0\n")
    await svc.start()
    try:
        host, port = svc.listening
        async with httpx.AsyncClient() as client:
            r = await client.get(f"http://{host}:{port}/v1/rules", headers={"Authorization": "Bearer agent-token-a"})
            assert r.status_code == 200 and "Room rules" in r.json()["text"]
            r = await client.get(f"http://{host}:{port}/v1/rules", headers={"Authorization": "Bearer nope"})
            assert r.status_code == 401
    finally:
        await svc.stop()
