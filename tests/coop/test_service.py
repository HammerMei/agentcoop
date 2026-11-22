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
from coop.room.platform import PlatformError
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
        fc.configure(server)  # the daemon's view of the bots, as with the real connectors
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


class SlowHistory(FakeConnector):
    """History that waits for the test's signal: the window in which a post can arrive
    while a room is being opened."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.gate = asyncio.Event()
        self.gate.set()

    async def history(self, room_id, after=None, before=None, limit=60):
        await self.gate.wait()
        return await super().history(room_id, after, before, limit)


async def test_a_post_arriving_while_the_room_is_being_opened_is_not_lost(tmp_path):
    fc = SlowHistory(names=NAMES)
    fc.add_room("c1", "team", PEOPLE)
    first = service(tmp_path, fc)
    await first.start(listen=False)  # a saved cursor: a restart replays what came after it
    await fc.human("c1", "H", "@a_bot earlier")
    await settle()
    wake = first.gateway.poll("a")
    first.gateway.read("a", wake.wake_id)
    await first.gateway.done("a", wake.wake_id)
    await first.stop()
    svc = service(tmp_path, fc)
    fc.gate.clear()
    starting = asyncio.create_task(svc.start(listen=False))
    await settle()
    assert "fake:team" in svc.rooms and not svc.rooms["fake:team"].started
    await fc.human("c1", "H", "@a_bot while opening")  # the fake's history holds it too: the dedup covers that
    fc.gate.set()
    await starting
    await settle()
    wake = svc.gateway.poll("a")
    assert wake is not None and wake.trigger.post_id == fc.posts["c1"][-1].id
    await svc.stop()


async def test_a_reload_gives_the_connector_the_new_bots_usernames(running, tmp_path):
    svc, fc = running
    assert fc.names.username_of("c") == "c"  # not a bot the connector knows yet
    added = CONFIG.replace("      x: {username: x_bot}", "      c: {username: c_bot, token_file: c.bot}\n      x: {username: x_bot}")
    added = added.replace("  b: {type: remote, token_file: b.agent}", "  b: {type: remote, token_file: b.agent}\n  c: {type: remote, token_file: c.agent}")
    added = added.replace("agents: [a, b, x]", "agents: [a, b, c, x]")
    fc.membership["c1"].append(MemberInfo("c_bot", is_bot=True))
    notes = await svc.reload(config(tmp_path, added))
    assert fc.names.username_of("c") == "c_bot" and any("'c' connected" in n for n in notes)
    post = await fc.human("c1", "c_bot", "hello from c")
    await settle()
    assert post.author == "c" and post.kind == "bot"
    assert svc.rooms["fake:team"].log.names.username_of("c") == "c_bot"
    assert svc.rooms["fake:team"].log.members()["c"].is_bot


async def test_a_bot_that_leaves_and_returns_starts_at_the_latest_post(running):
    svc, fc = running
    await fc.human("c1", "H", "@b_bot before")
    await settle()
    wake = svc.gateway.poll("b")
    svc.gateway.read("b", wake.wake_id)
    await svc.gateway.done("b", wake.wake_id)
    await fc.leave("c1", "b")
    await settle()
    missed = await fc.human("c1", "H", "@b_bot while away")
    await settle()
    await fc.join("c1", "b")
    await settle()
    core = svc.rooms["fake:team"].core
    assert core.persisted["b"].cursor == missed.position and svc.gateway.poll("b") is None


async def test_two_rooms_with_one_handle_are_noted_and_the_daemon_still_starts(tmp_path):
    fc = connector()
    fc.add_room("c9", "team", PEOPLE)  # a second room named like the first
    svc = service(tmp_path, fc)
    await svc.start(listen=False)
    try:
        assert set(svc.rooms) == {"fake:team"} and svc.rooms["fake:team"].info.id == "c1"
        assert any("share this name" in n for n in svc.notes)
    finally:
        await svc.stop()


async def test_a_state_file_of_another_format_is_noted_and_the_room_starts_fresh(tmp_path):
    fc = connector()
    (tmp_path / "state" / "fake").mkdir(parents=True)
    (tmp_path / "state" / "fake" / "c1.json").write_text('{"bots": {"a": {"cursor": [1, "x"]}}}')
    svc = service(tmp_path, fc)
    await svc.start(listen=False)
    try:
        assert svc.rooms["fake:team"].core.persisted["a"].cursor is None
        assert any("not a 1 room state document" in n for n in svc.notes)
    finally:
        await svc.stop()


async def test_an_attachment_is_fetched_through_a_hosted_bot(running):
    svc, fc = running
    post = await fc.human("c1", "H", "@a_bot see this", files=(("a.txt", "text/plain", b"hello"),))
    await settle()
    wake = svc.gateway.poll("a")
    _, posts = svc.gateway.read("a", wake.wake_id)
    delivered = svc.gateway.deliverable("a", "fake:team", next(p for p in posts if p.id == post.id))
    handle = delivered.attachments[0].id  # the handle a read over the doorbell prints
    found, data = await svc.gateway.fetch_attachment("a", handle)
    assert found.name == "a.txt" and data == b"hello"


async def test_malformed_saved_threads_start_the_room_fresh_loudly(tmp_path):
    fc = connector()
    (tmp_path / "state" / "fake").mkdir(parents=True)
    (tmp_path / "state" / "fake" / "c1.json").write_text('{"format": 1, "bots": {}, "threads": {"t1": {"bogus": 1}}}')
    svc = service(tmp_path, fc)
    await svc.start(listen=False)
    try:
        assert "fake:team" in svc.rooms and svc.rooms["fake:team"].core.settings == {}
        assert any("malformed room state" in n for n in svc.notes)
    finally:
        await svc.stop()


async def test_a_reload_that_changes_a_connected_bots_credentials_says_a_restart_is_needed(running, tmp_path):
    svc, fc = running
    (tmp_path / "b2.bot").write_text("rotated\n")
    notes = await svc.reload(config(tmp_path, CONFIG.replace("token_file: b.bot", "token_file: b2.bot")))
    assert any("'b' has new credentials" in n and "restart" in n for n in notes)
    assert fc.bots["b"] == "bot-token-b"  # the connection kept the old one, as the note says


async def test_two_agents_with_one_token_refuse_the_configuration(tmp_path):
    fc = connector()
    svc = service(tmp_path, fc)
    (tmp_path / "b.agent").write_text("agent-token-a\n")
    with pytest.raises(ConfigError, match="hold the same token"):
        await svc.start(listen=False)
    await svc.stop()


async def test_a_saved_null_cursor_backfills_to_the_start_of_history(tmp_path):
    fc = connector()
    (tmp_path / "state" / "fake").mkdir(parents=True)
    (tmp_path / "state" / "fake" / "c1.json").write_text(
        '{"format": 1, "bots": {"a": {"cursor": null, "unfinished": [], "paused": false}}, "threads": {}}')
    for i in range(130):
        fc.make_post("c1", "H", f"@a_bot n{i}" if i == 0 else f"n{i}")
    svc = service(tmp_path, fc)
    await svc.start(listen=False)
    try:
        assert len(svc.rooms["fake:team"].log.posts) == 130
        await settle()
        assert svc.gateway.poll("a").trigger.post_id == fc.posts["c1"][0].id  # the oldest post, replayed
    finally:
        await svc.stop()


async def test_malformed_agents_state_is_noted_and_bios_start_empty(tmp_path):
    fc = connector()
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "agents.json").write_text('{"format": 1, "agents": []}')
    svc = service(tmp_path, fc)
    await svc.start(listen=False)
    try:
        assert any("unreadable agents state" in n for n in svc.notes) and svc.gateway.bios == {}
    finally:
        await svc.stop()


async def test_a_reload_that_changes_the_listener_says_a_restart_is_needed(running, tmp_path):
    svc, fc = running
    notes = await svc.reload(config(tmp_path, CONFIG + "gateway:\n  listen: 127.0.0.1:9999\n"))
    assert any("gateway.listen/tls changed" in n and "restart" in n for n in notes)


class FlakyMembers(FakeConnector):
    """``members()`` fails for one room: the platform did not answer."""

    failing: set[str] = set()

    async def members(self, room_id):
        if room_id in self.failing:
            raise PlatformError(f"fake: members of {room_id}: down")
        return await super().members(room_id)


async def test_one_room_that_cannot_open_is_noted_and_the_bots_other_rooms_are_hosted(tmp_path):
    fc = FlakyMembers(names=NAMES)
    fc.add_room("c1", "team", PEOPLE)
    fc.add_room("c3", "team-2", PEOPLE)
    fc.failing = {"c1"}
    svc = service(tmp_path, fc)
    await svc.start(listen=False)
    try:
        assert set(svc.rooms) == {"fake:team-2"} and fc.bots.keys() == {"a", "b"}
        assert any("fake:team: not opened" in n for n in svc.notes)
        fc.failing = set()
        notes = await svc.reload(svc.config)  # the next reload hosts it: the bot's rooms were kept
        assert "fake:team" in svc.rooms and any("fake:team: 'a' hosted" in n for n in notes)
    finally:
        await svc.stop()


async def test_a_newly_hosted_bots_cursor_is_on_disk_before_any_turn(tmp_path):
    fc = connector()
    fc.make_post("c1", "H", "before the daemon")
    svc = service(tmp_path, fc)
    await svc.start(listen=False)
    saved = json.loads((tmp_path / "state" / "fake" / "c1.json").read_text())
    assert tuple(saved["bots"]["a"]["cursor"]) == fc.posts["c1"][-1].position
    await svc.stop()


async def test_a_leave_from_an_unhosted_room_is_forgotten_so_a_later_rule_does_not_host_it(running, tmp_path):
    svc, fc = running
    await fc.leave("c2", "a")  # fake:other is not hosted; a is no longer in it
    await settle()
    notes = await svc.reload(config(tmp_path, CONFIG.replace('"fake:team*"', '"fake:*"')))
    assert "fake:other" in svc.rooms and svc.rooms["fake:other"].core.hosted == ["b"]  # b is still a member
    assert not any("'a' hosted" in n and "other" in n for n in notes)


async def test_a_reload_that_adds_a_roster_only_bot_renormalises_the_members(running, tmp_path):
    svc, fc = running
    fc.membership["c1"].append(MemberInfo("y_bot", is_bot=True))
    added = CONFIG.replace("      x: {username: x_bot}", "      x: {username: x_bot}\n      y: {username: y_bot}")
    added = added.replace("agents: [a, b, x]", "agents: [a, b, x, y]")
    await svc.reload(config(tmp_path, added))
    log = svc.rooms["fake:team"].log
    assert "y" in log.members() and log.members()["y"].is_bot and "y_bot" not in log.members()
    assert "y" in svc.rooms["fake:team"].core._bots_in_room()


RELOAD_CASES = [
    # (change to the file, what the reload does: applied | refused | warned, how to tell)
    ("agents: [a, b, x]", "agents: [a, x]", "applied", lambda svc, fc, notes: svc.rooms["fake:team"].core.hosted == ["a"]),
    ("token_file: b.agent", "token_file: a.agent", "refused", None),
    ("url: http://fake", "url: http://other", "refused", None),
    ("token_file: b.bot", "token_file: b2.bot", "warned", lambda svc, fc, notes: any("new credentials" in n for n in notes)),
    ("rooms:", "gateway: {listen: 127.0.0.1:1}\nrooms:", "warned", lambda svc, fc, notes: any("gateway.listen/tls" in n for n in notes)),
    ("rooms:", "classifier: {command: [echo]}\nrooms:", "applied", lambda svc, fc, notes: svc.config.classifier.command == ("echo",)),
    ("guests: [G]", "guests: [G, K]", "applied", lambda svc, fc, notes: "K" in svc.rooms["fake:team"].core.roster.guests),
    ('"fake:team*"', '"fake:nothing"', "applied", lambda svc, fc, notes: svc.rooms == {}),
]


@pytest.mark.parametrize("old, new, outcome, check", RELOAD_CASES, ids=[c[1][:24] for c in RELOAD_CASES])
async def test_every_configuration_section_is_applied_refused_or_said_on_reload(running, tmp_path, old, new, outcome, check):
    svc, fc = running
    (tmp_path / "b2.bot").write_text("rotated\n")
    text = CONFIG.replace(old, new, 1)
    assert text != CONFIG
    if outcome == "refused":
        before = svc.config
        with pytest.raises(ConfigError):
            await svc.reload(config(tmp_path, text))
        assert svc.config is before
        return
    notes = await svc.reload(config(tmp_path, text))
    assert check(svc, fc, notes), notes
