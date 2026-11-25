"""The daemon over a fake connector: hosting by rule, the event path into the core
(a bot's own reply included), persistence across a restart, membership at runtime,
and a reload applied in place."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
import yaml

from coop.config import ConfigError, parse
from coop.connectors.base import MemberInfo, Membership, RoomInfo, handle_for
from coop.room.model import Reply
from coop.room.platform import PlatformError
from coop.service import Service

from .helpers import FakeConnector

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
    fc = FakeConnector()
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
    assert set(svc.rooms) == {"fake:team"} and fc.bots.keys() == {"a_bot", "b_bot"}
    core = svc.rooms["fake:team"].core
    assert core.hosted == ["a_bot", "b_bot"] and core.agents == ["a_bot", "b_bot", "x_bot"]  # the platform's names
    assert svc.gateway.username_of("fake:team", "a") == "a_bot" and svc.gateway.agent_of("fake:team", "b_bot") == "b"
    assert svc.tokens == {"agent-token-a": "a", "agent-token-b": "b"}
    await fc.human("c1", "H", "@a_bot hello")
    await settle()
    wake = svc.gateway.poll("a")
    assert wake is not None and wake.trigger.author == "H" and wake.trigger.author_role == "owner"
    await settle()
    assert fc.typed == [("a_bot", "c1", wake.starting_thread)]  # the pick-up of an addressed wake
    _, posts = svc.gateway.read("a", wake.wake_id)
    assert [p.text for p in posts] == ["@a_bot hello"] and wake.bot == "a_bot"  # the room's names, as written
    reply = Reply(thread=wake.starting_thread, to=["b_bot"], text="your turn", wake_id=wake.wake_id)
    post = (await svc.gateway.send("a", reply)).post
    assert fc.posts["c1"][-1].text == "@b_bot your turn" and post.author == "a_bot"
    await settle()
    wake_b = svc.gateway.poll("b")
    assert wake_b is not None and wake_b.trigger.author == "a_bot" and wake_b.trigger.author_role == "agent"
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
    assert tuple(saved["bots"]["a"]["cursor"]) == wake.hwm  # the file keys bots by agent name
    svc.gateway.bio("a", "I am a")
    assert json.loads((tmp_path / "state" / "agents.json").read_text())["agents"]["a"]["bio"] == "I am a"
    await svc.stop()
    fc.on_event = None
    missed = fc.make_post("c1", "H", "@b_bot while down")
    again = service(tmp_path, fc)
    await again.start(listen=False)
    core = again.rooms["fake:team"].core
    assert core.persisted["a_bot"].cursor == wake.hwm and core.persisted["b_bot"].cursor is None
    assert again.gateway.agents["a"].bio_version == 1
    await settle()
    wake_b = again.gateway.poll("b")
    assert wake_b is not None and wake_b.trigger.post_id == missed.id
    await again.stop()


async def test_a_bot_joining_a_room_at_runtime_is_hosted_at_the_latest_post_and_leaving_closes_it(running):
    svc, fc = running
    fc.add_room("c3", "team-2", [MemberInfo("H"), MemberInfo("a_bot", is_bot=True)])
    fc.make_post("c3", "H", "@b_bot before b was here")
    await fc.join("c3", "b_bot")
    await settle()
    run = svc.rooms["fake:team-2"]
    assert run.core.hosted == ["b_bot"] and run.started
    assert run.core.persisted["b_bot"].cursor == run.log.last_position()
    assert svc.gateway.poll("b") is None  # nothing before the join is a wake
    await fc.human("c3", "H", "@b_bot now")
    await settle()
    assert svc.gateway.poll("b").room == "fake:team-2"
    await fc.leave("c3", "b_bot")
    await settle()
    assert "fake:team-2" not in svc.rooms and svc.trace.last("turn_end", bot="b_bot").outcome == "removed"


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
    assert core.hosted == ["a_bot", "c_bot"] and core.agents == ["a_bot", "c_bot", "x_bot"] and core.params.enforce
    assert fc.bots.keys() == {"a_bot", "c_bot"} and "b_bot" not in fc.bots
    assert svc.tokens is tokens and tokens == {"agent-token-a": "a", "agent-token-c": "c"}
    assert svc.gateway.holds_lease("a") and core.volatile["a_bot"].turn.wake.wake_id == wake.wake_id
    assert core.persisted["c_bot"].cursor == svc.rooms["fake:team"].log.last_position()
    assert any("'c' connected" in n for n in notes) and any("'b' disconnected" in n for n in notes)
    with pytest.raises(ConfigError, match="needs a restart"):
        await svc.reload(config(tmp_path, added.replace("url: http://fake", "url: http://elsewhere")))
    assert core.hosted == ["a_bot", "c_bot"]


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
    assert svc.status()["rooms"]["fake:team"]["bots"]["a_bot"]["paused"]  # the operator names the agent
    svc.resume("a", "fake:team")
    svc.reset("a", "fake:team")
    assert svc.rooms["fake:team"].core.persisted["a_bot"].reset_pending
    with pytest.raises(KeyError, match="no hosted room"):
        svc.pause("a", "fake:nope")
    with pytest.raises(KeyError, match="not hosted"):
        svc.pause("x", "fake:team")
    status = svc.status()
    assert status["servers"] == {"fake": ["a", "b"]} and status["agents"]["a"]["online"] is False
    assert status["listening"] is None and status["rooms"]["fake:team"]["agents"] == ["a_bot", "b_bot", "x_bot"]


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
    while a room is being opened. ``failing_members`` is how many ``members()`` calls
    fail first, after the wait: an open that fails while others wait for it."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.gate = asyncio.Event()
        self.gate.set()
        self.failing_members = 0

    async def members(self, room_id):
        await self.gate.wait()
        if self.failing_members:
            self.failing_members -= 1
            self.gate.clear()  # the open that follows stalls too: its waiters have to wait again
            raise PlatformError("members down")
        return await super().members(room_id)

    async def history(self, room_id, after=None, before=None, limit=60):
        await self.gate.wait()
        return await super().history(room_id, after, before, limit)


async def test_a_post_arriving_while_the_room_is_being_opened_is_not_lost(tmp_path):
    fc = SlowHistory()
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


async def test_a_reload_connects_a_new_bot_under_its_username_and_its_posts_read_as_a_bots(running, tmp_path):
    svc, fc = running
    assert "c_bot" not in fc.bots
    added = CONFIG.replace("      x: {username: x_bot}", "      c: {username: c_bot, token_file: c.bot}\n      x: {username: x_bot}")
    added = added.replace("  b: {type: remote, token_file: b.agent}", "  b: {type: remote, token_file: b.agent}\n  c: {type: remote, token_file: c.agent}")
    added = added.replace("agents: [a, b, x]", "agents: [a, b, c, x]")
    fc.membership["c1"].append(MemberInfo("c_bot", is_bot=True))
    notes = await svc.reload(config(tmp_path, added))
    assert "c_bot" in fc.bots and any("'c' connected" in n for n in notes)
    post = await fc.human("c1", "c_bot", "hello from c")
    await settle()
    assert post.author == "c_bot" and post.kind == "bot"
    assert svc.rooms["fake:team"].log.members()["c_bot"].is_bot
    assert svc.gateway.username_of("fake:team", "c") == "c_bot"


async def test_a_bot_that_leaves_and_returns_starts_at_the_latest_post(running):
    svc, fc = running
    await fc.human("c1", "H", "@b_bot before")
    await settle()
    wake = svc.gateway.poll("b")
    svc.gateway.read("b", wake.wake_id)
    await svc.gateway.done("b", wake.wake_id)
    await fc.leave("c1", "b_bot")
    await settle()
    missed = await fc.human("c1", "H", "@b_bot while away")
    await settle()
    await fc.join("c1", "b_bot")
    await settle()
    core = svc.rooms["fake:team"].core
    assert core.persisted["b_bot"].cursor == missed.position and svc.gateway.poll("b") is None


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
        assert svc.rooms["fake:team"].core.persisted["a_bot"].cursor is None
        assert any("not a 2 room state document" in n for n in svc.notes)
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
    (tmp_path / "state" / "fake" / "c1.json").write_text('{"format": 2, "bots": {}, "threads": {"t1": {"bogus": 1}}}')
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
    assert fc.bots["b_bot"] == "bot-token-b"  # the connection kept the old one, as the note says
    # a username is the bot's name in every room it is in: changing it is refused, not warned
    with pytest.raises(ConfigError, match="changing a connected bot's username needs a restart"):
        await svc.reload(config(tmp_path, CONFIG.replace("username: b_bot", "username: b2_bot")))
    assert svc.rooms["fake:team"].core.hosted == ["a_bot", "b_bot"]


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
        '{"format": 2, "bots": {"a": {"cursor": null, "unfinished": [], "paused": false}}, "threads": {}}')
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
    fc = FlakyMembers()
    fc.add_room("c1", "team", PEOPLE)
    fc.add_room("c3", "team-2", PEOPLE)
    fc.failing = {"c1"}
    svc = service(tmp_path, fc)
    await svc.start(listen=False)
    try:
        assert set(svc.rooms) == {"fake:team-2"} and fc.bots.keys() == {"a_bot", "b_bot"}
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
    await fc.leave("c2", "a_bot")  # fake:other is not hosted; a is no longer in it
    await settle()
    notes = await svc.reload(config(tmp_path, CONFIG.replace('"fake:team*"', '"fake:*"')))
    assert "fake:other" in svc.rooms and svc.rooms["fake:other"].core.hosted == ["b_bot"]  # b is still a member
    assert not any("'a' hosted" in n and "other" in n for n in notes)


async def test_a_reload_that_adds_a_roster_only_bot_puts_it_in_the_room_under_its_username(running, tmp_path):
    svc, fc = running
    fc.membership["c1"].append(MemberInfo("y_bot", is_bot=True))
    added = CONFIG.replace("      x: {username: x_bot}", "      x: {username: x_bot}\n      y: {username: y_bot}")
    added = added.replace("agents: [a, b, x]", "agents: [a, b, x, y]")
    await svc.reload(config(tmp_path, added))
    log = svc.rooms["fake:team"].log
    assert "y_bot" in log.members() and log.members()["y_bot"].is_bot and "y" not in log.members()
    assert "y_bot" in svc.rooms["fake:team"].core._bots_in_room()


RELOAD_CASES = [
    # (change to the file, what the reload does: applied | refused | warned, how to tell)
    ("agents: [a, b, x]", "agents: [a, x]", "applied", lambda svc, fc, notes: svc.rooms["fake:team"].core.hosted == ["a_bot"]),
    ("token_file: b.agent", "token_file: a.agent", "refused", None),
    ("username: b_bot", "username: b2_bot", "refused", None),
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


async def test_a_room_reopened_in_the_same_daemon_never_reuses_a_wake_id(running, tmp_path):
    svc, fc = running
    await fc.human("c1", "H", "@a_bot one")
    await settle()
    first = svc.gateway.poll("a").wake_id
    await svc.reload(config(tmp_path, CONFIG.replace('"fake:team*"', '"fake:nothing"')))  # closed
    await svc.reload(config(tmp_path))  # reopened
    assert "fake:team" in svc.rooms
    await fc.human("c1", "H", "@a_bot two")
    await settle()
    second = svc.gateway.poll("a").wake_id
    assert second != first and int(second.rsplit("-", 1)[1]) > int(first.rsplit("-", 1)[1])


async def test_an_archived_room_is_not_hosted(tmp_path):
    fc = connector()
    fc.rooms["c1"] = RoomInfo("c1", "team", archived=True)
    svc = service(tmp_path, fc)
    await svc.start(listen=False)
    try:
        assert svc.rooms == {}
    finally:
        await svc.stop()


async def test_crossed_usernames_wake_by_binding_not_by_name(tmp_path):
    """Agent ``a``'s bot is called ``b`` and agent ``b``'s bot is called ``a``: a mention
    of ``@a`` wakes agent ``b``, and the state file is keyed by the room's names."""
    crossed = CONFIG.replace("a: {username: a_bot, token_file: a.bot}", "a: {username: b, token_file: a.bot}")
    crossed = crossed.replace("b: {username: b_bot, token_file: b.bot}", "b: {username: a, token_file: b.bot}")
    fc = FakeConnector()
    fc.add_room("c1", "team", [MemberInfo("H"), MemberInfo("a", is_bot=True), MemberInfo("b", is_bot=True)])
    svc = service(tmp_path, fc, crossed)
    await svc.start(listen=False)
    try:
        assert svc.gateway.username_of("fake:team", "a") == "b" and svc.gateway.username_of("fake:team", "b") == "a"
        await fc.human("c1", "H", "@a hello")
        await settle()
        assert svc.gateway.poll("a") is None
        wake = svc.gateway.poll("b")
        assert wake is not None and wake.bot == "a"
        _, posts = svc.gateway.read("b", wake.wake_id)
        post = (await svc.gateway.send("b", Reply(thread=wake.starting_thread, to=["H"], text="hi", wake_id=wake.wake_id))).post
        assert post.author == "a" and await svc.gateway.done("b", wake.wake_id)
        saved = json.loads((tmp_path / "state" / "fake" / "c1.json").read_text())
        assert set(saved["bots"]) == {"a", "b"} and tuple(saved["bots"]["b"]["cursor"]) == wake.hwm  # by agent
        svc.pause("b", "fake:team")
        assert svc.rooms["fake:team"].core.persisted["a"].paused
    finally:
        await svc.stop()


async def test_a_renamed_bot_keeps_its_cursor_and_pause_across_the_restart_the_refusal_asks_for(tmp_path):
    fc = connector()
    svc = service(tmp_path, fc)
    await svc.start(listen=False)
    await fc.human("c1", "H", "@b_bot hello")
    await settle()
    wake = svc.gateway.poll("b")
    svc.gateway.read("b", wake.wake_id)
    await svc.gateway.done("b", wake.wake_id)
    svc.pause("b", "fake:team")
    await svc.stop()
    fc.on_event = None
    fc.membership["c1"] = [m for m in fc.membership["c1"] if m.username != "b_bot"] + [MemberInfo("b2_bot", is_bot=True)]
    again = service(tmp_path, fc, CONFIG.replace("username: b_bot", "username: b2_bot"))
    await again.start(listen=False)
    try:
        core = again.rooms["fake:team"].core
        assert core.hosted == ["a_bot", "b2_bot"] and again.gateway.username_of("fake:team", "b") == "b2_bot"
        assert core.persisted["b2_bot"].cursor == wake.hwm and core.persisted["b2_bot"].paused
    finally:
        await again.stop()


async def test_a_bio_reaches_each_room_under_the_bots_username_and_a_restart_restores_it_there(tmp_path):
    fc = connector()
    svc = service(tmp_path, fc)
    await svc.start(listen=False)
    svc.gateway.bio("a", "I do graphs")
    core = svc.rooms["fake:team"].core
    assert core.bios == {"a_bot": "I do graphs"} and svc.gateway.bios == {"a": "I do graphs"}
    await svc.stop()
    again = service(tmp_path, fc)
    await again.start(listen=False)
    try:
        assert again.rooms["fake:team"].core.bios == {"a_bot": "I do graphs"}
    finally:
        await again.stop()


async def test_a_person_whose_username_is_an_agents_key_joining_a_room_hosts_nothing(running):
    svc, fc = running
    fc.add_room("c3", "team-2", [MemberInfo("H"), MemberInfo("b_bot", is_bot=True)])
    await fc.on_event(Membership("c3", "a", joined=True))  # a person called "a": not agent a's bot (a_bot)
    await settle()
    assert "fake:team-2" not in svc.rooms


async def test_a_second_bots_join_during_the_history_fetch_binds_once_and_a_reload_still_works(tmp_path):
    fc = SlowHistory()
    fc.add_room("c1", "team", PEOPLE)
    fc.add_room("c3", "team-2", [MemberInfo("H")])
    svc = service(tmp_path, fc)
    await svc.start(listen=False)
    try:
        fc.gate.clear()
        fc.membership["c3"].append(MemberInfo("b_bot", is_bot=True))
        await fc.human("c3", "H", "before the join")
        # the platform delivers the join on every member's connection: three _host calls,
        # the later ones while the first is still fetching the room's history; they wait
        # for the open, so their cursor is the full log's latest post, not the half-fetched
        # log's. The first open fails: the waiters find no run and the next one opens it
        fc.failing_members = 1
        first = asyncio.create_task(fc.on_event(Membership("c3", "b_bot", joined=True)))
        await settle()
        second = asyncio.create_task(fc.on_event(Membership("c3", "b_bot", joined=True)))
        third = asyncio.create_task(fc.on_event(Membership("c3", "b_bot", joined=True)))
        await settle()
        assert "fake:team-2" in svc.rooms and not svc.rooms["fake:team-2"].core.hosted
        fc.gate.set()  # the first open fails; the second starts and stalls on the gate
        await settle()
        assert any(n.startswith("[ERROR] fake:team-2: not opened") for n in svc.notes)
        assert "fake:team-2" in svc.rooms and not svc.rooms["fake:team-2"].core.hosted  # the waiters wait again
        fc.gate.set()
        await asyncio.gather(first, second, third)
        run = svc.rooms["fake:team-2"]
        assert run.core.hosted == ["b_bot"] and svc.gateway.username_of("fake:team-2", "b") == "b_bot"
        assert run.core.persisted["b_bot"].cursor == run.log.last_position() is not None
        notes = await svc.reload(svc.config)
        assert "fake:team-2" in svc.rooms and not any(n.startswith("[ERROR]") for n in notes)
    finally:
        await svc.stop()


async def test_a_dm_opened_by_a_join_delivers_the_post_that_landed_while_it_opened(tmp_path):
    """A person opens a DM and types at once: the platform created the room around the
    bot, the open's history fetch already holds the post, and the post's own frame comes
    after the open on the same connection, so the log drops it as a copy. The bot's cursor
    for a room created around it is before every post, so the replay at start delivers it."""
    fc = SlowHistory()
    fc.add_room("c1", "team", PEOPLE)
    svc = service(tmp_path, fc, CONFIG.replace('"fake:team*"', '"fake:*"'))
    await svc.start(listen=False)
    try:
        fc.add_room("d1", "H", [MemberInfo("H"), MemberInfo("a_bot", is_bot=True)], "dm")
        fc.gate.clear()
        joining = asyncio.create_task(fc.on_event(Membership("d1", "a_bot", joined=True, created=True)))
        await settle()
        await fc.human("d1", "H", "hi bot, new dm")  # in the fake's history and, later, a live copy
        fc.gate.set()
        await joining
        await settle()
        wake = svc.gateway.poll("a")
        assert wake is not None and wake.room == "fake:dm:a_bot:H" and wake.trigger.post_id == fc.posts["d1"][-1].id
        svc.gateway.read("a", wake.wake_id)
        await svc.gateway.done("a", wake.wake_id)
        cursor = svc.rooms["fake:dm:a_bot:H"].core.persisted["a_bot"].cursor
        await fc.on_event(Membership("d1", "a_bot", joined=True, created=True))  # the person reopened the DM
        await settle()
        assert svc.rooms["fake:dm:a_bot:H"].core.persisted["a_bot"].cursor == cursor and svc.gateway.poll("a") is None
    finally:
        await svc.stop()


async def test_a_group_created_around_two_bots_delivers_its_first_post_to_both_at_once(tmp_path):
    """The first bot's join opens and starts the room and the replay at start reaches it
    alone; the second is hosted after that with the post above its cursor, and its live
    copy is a duplicate to the log. Its catch-up is requested on hosting, not left to the
    periodic sweep, which this configuration has off."""
    fc = SlowHistory()
    fc.add_room("c1", "team", PEOPLE)
    svc = service(tmp_path, fc, CONFIG.replace('"fake:team*"', '"fake:gdm:*"'))
    await svc.start(listen=False)
    try:
        fc.add_room("g1", "H, a_bot, b_bot", [MemberInfo("H"), MemberInfo("a_bot", is_bot=True), MemberInfo("b_bot", is_bot=True)], "group")
        fc.gate.clear()
        joins = [asyncio.create_task(fc.on_event(Membership("g1", bot, joined=True, created=True))) for bot in ("a_bot", "b_bot")]
        await settle()
        await fc.human("g1", "H", "@a_bot @b_bot hello both")
        fc.gate.set()
        await asyncio.gather(*joins)
        await settle()
        handle = next(h for h in svc.rooms if h.startswith("fake:gdm:"))
        assert svc.rooms[handle].core.hosted == ["a_bot", "b_bot"]
        wakes = {agent: svc.gateway.poll(agent) for agent in ("a", "b")}
        assert all(w is not None and w.trigger.post_id == fc.posts["g1"][-1].id for w in wakes.values()), wakes
    finally:
        await svc.stop()


async def test_a_join_to_a_room_the_platform_cannot_describe_is_an_error_line_not_a_crash(running):
    svc, fc = running

    async def room(room_id):
        raise PlatformError("down")
    fc.room = room
    await fc.on_event(Membership("c2", "a_bot", joined=True))
    await settle()
    assert "fake:other" not in svc.rooms and any(n.startswith("[ERROR] fake: room c2") for n in svc.notes)
    assert fc.bots  # the connection is still up: the next event is handled


async def test_a_join_the_platform_could_not_describe_is_asked_again_on_reload(running, tmp_path):
    """§14.3: the degraded part is retried on a later reload, not only by a restart
    (#214 R5). A retry that fails again is said again and does not fail the reload."""
    svc, fc = running
    fc.add_room("c3", "late", PEOPLE)  # a room the bot was not in at start

    async def room(room_id):
        raise PlatformError("down")
    fc.room = room
    await fc.on_event(Membership("c3", "a_bot", joined=True))
    await settle()
    assert any(n.startswith("[ERROR] fake: room c3") and "until a reload" in n for n in svc.notes)
    every = CONFIG.replace('"fake:team*"', '"fake:*"')
    notes = await svc.reload(config(tmp_path, every))  # still down: said again, nothing hosted, no raise
    assert "fake:late" not in svc.rooms and any("room c3" in n and "still not described" in n for n in notes)
    del fc.room  # the platform answers again
    notes = await svc.reload(config(tmp_path, every))
    assert svc.rooms["fake:late"].core.hosted == ["a_bot"] and "fake:late: 'a' hosted" in notes
    assert any(n.startswith("[WARNING] fake: room c3") and "not delivered" in n for n in notes)  # said, not silent
    assert svc.rooms["fake:late"].core.persisted["a_bot"].cursor == svc.rooms["fake:late"].log.last_position()
    notes = await svc.reload(config(tmp_path, every))
    assert not any("room c3" in n for n in notes)  # described once, not asked again


async def test_a_join_announced_again_and_described_now_is_not_asked_about_on_reload(running, tmp_path):
    """A reopened DM is announced again (Mattermost): the second announcement, described,
    clears the first's failure, so a reload says nothing about a room that is hosted."""
    svc, fc = running
    fc.add_room("d1", "H", [MemberInfo("H"), MemberInfo("a_bot", is_bot=True)], "dm")

    async def room(room_id):
        raise PlatformError("down")
    fc.room = room
    await fc.on_event(Membership("d1", "a_bot", joined=True, created=True))
    await settle()
    del fc.room
    await fc.on_event(Membership("d1", "a_bot", joined=True))
    await settle()
    fc.room = room  # flaky again at reload time
    notes = await svc.reload(config(tmp_path, CONFIG.replace('"fake:team*"', '"fake:*"')))
    assert "fake:dm:a_bot:H" in svc.rooms and not any("room d1" in n for n in notes)


def test_a_rooms_handle_is_platform_names_only_by_kind():
    assert handle_for("fake", RoomInfo("c1", "team"), "a_bot") == "fake:team"
    assert handle_for("fake", RoomInfo("d1", "H", "dm"), "a_bot") == "fake:dm:a_bot:H"
    group = handle_for("fake", RoomInfo("g1", "H, a_bot, b_bot", "group"), "a_bot")
    assert group == "fake:gdm:" + hashlib.sha256(b"g1").hexdigest()[:16]
    assert handle_for("fake", RoomInfo("g1", "a_bot, H, b_bot", "group"), "b_bot") == group  # whoever lists it


async def test_a_dm_is_hosted_under_the_bots_username_and_two_bots_dms_with_one_person_are_two_rooms(tmp_path):
    fc = FakeConnector()
    fc.add_room("c1", "team", PEOPLE)
    fc.add_room("d1", "H", [MemberInfo("H"), MemberInfo("a_bot", is_bot=True)], "dm")
    fc.add_room("d2", "H", [MemberInfo("H"), MemberInfo("b_bot", is_bot=True)], "dm")
    svc = service(tmp_path, fc, CONFIG.replace('"fake:team*"', '"fake:dm:*:H"'))
    await svc.start(listen=False)
    try:
        assert set(svc.rooms) == {"fake:dm:a_bot:H", "fake:dm:b_bot:H"}
        assert svc.rooms["fake:dm:a_bot:H"].core.hosted == ["a_bot"] and svc.rooms["fake:dm:b_bot:H"].core.hosted == ["b_bot"]
        assert svc.rooms["fake:dm:a_bot:H"].log.is_dm
    finally:
        await svc.stop()


async def test_a_group_message_is_one_room_under_a_digest_whichever_bot_lists_it(tmp_path):
    fc = FakeConnector()
    fc.add_room("c1", "team", PEOPLE)
    fc.add_room("g1", "H, a_bot, b_bot", PEOPLE, "group")
    svc = service(tmp_path, fc, CONFIG.replace('"fake:team*"', '"fake:gdm:*"'))
    await svc.start(listen=False)
    try:
        handle = "fake:gdm:" + hashlib.sha256(b"g1").hexdigest()[:16]
        assert set(svc.rooms) == {handle}
        run = svc.rooms[handle]
        assert run.core.hosted == ["a_bot", "b_bot"] and not run.log.is_dm
        assert svc.gateway.username_of(handle, "b") == "b_bot" and not any(n.startswith("[ERROR]") for n in svc.notes)
    finally:
        await svc.stop()


async def test_a_direct_channel_between_two_bots_is_not_hosted(tmp_path):
    """Each bot names the channel after the other. Nobody human is there and only the
    platform's admin can see it: a bot-to-bot DM is not supported, at start, on a join,
    and on a reload that lists the counterpart as a bot."""
    fc = FakeConnector()
    fc.add_room("c1", "team", PEOPLE)
    seen_as = {"a_bot": "b_bot", "b_bot": "a_bot"}

    class Named(FakeConnector):
        async def add_bot(self, username, token):
            rooms = await super().add_bot(username, token)
            return [RoomInfo(r.id, seen_as[username], "dm") if r.id == "d1" else r for r in rooms]

        async def room(self, room_id):
            return RoomInfo("d1", "a_bot", "dm") if room_id == "d1" else await super().room(room_id)
    nc = Named()
    nc.rooms, nc.membership, nc.posts = fc.rooms, fc.membership, fc.posts
    nc.add_room("d1", "dm", [MemberInfo("a_bot", is_bot=True), MemberInfo("x_bot", is_bot=True)], "dm")
    nc.add_room("d2", "x_bot", [MemberInfo("a_bot", is_bot=True), MemberInfo("x_bot", is_bot=True)], "dm")  # a roster-only bot
    nc.add_room("d3", "y_bot", [MemberInfo("a_bot", is_bot=True), MemberInfo("y_bot", is_bot=True)], "dm")  # a bot the file does not list
    svc = service(tmp_path, nc, CONFIG.replace('"fake:team*"', '"fake:*"'))
    await svc.start(listen=False)
    try:
        assert set(svc.rooms) == {"fake:team", "fake:dm:a_bot:y_bot"} and not any(n.startswith("[ERROR]") for n in svc.notes)
        nc.membership["d1"].append(MemberInfo("b_bot", is_bot=True))
        await nc.on_event(Membership("d1", "b_bot", joined=True))
        await settle()
        assert set(svc.rooms) == {"fake:team", "fake:dm:a_bot:y_bot"}
        notes = await svc.reload(config(tmp_path, CONFIG.replace('"fake:team*"', '"fake:*"')
                                        .replace("x: {username: x_bot}", "x: {username: x_bot}\n      y: {username: y_bot}")))
        assert set(svc.rooms) == {"fake:team"} and "fake:dm:a_bot:y_bot: no rule matches now; closed" in notes
    finally:
        await svc.stop()


async def test_a_reload_renaming_a_roster_only_bot_refreshes_the_members(running, tmp_path):
    svc, fc = running
    fc.membership["c1"] = [m for m in fc.membership["c1"] if m.username != "x_bot"] + [MemberInfo("x2_bot", is_bot=True)]
    await svc.reload(config(tmp_path, CONFIG.replace("x: {username: x_bot}", "x: {username: x2_bot}")))
    core = svc.rooms["fake:team"].core
    assert "x2_bot" in core._bots_in_room() and "x_bot" not in svc.rooms["fake:team"].log.members()
