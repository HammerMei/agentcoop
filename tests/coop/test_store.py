"""The persisted state round-trips through JSON and restores a room that behaves as
the one that saved it (spec 5.4 settings persistence, 5.6 cursor, design §3.1)."""

from __future__ import annotations

import json

import pytest

from coop.room.core import RoomCore
from coop.room.model import Reply
from coop.room.store import dump_agents, dump_room, load_agents, load_room
from tests.coop.helpers import Sandbox, Script, reply


async def _saved_sandbox() -> Sandbox:
    sb = Sandbox(script=Script().on(reply(["H"], "x"), bot="a"), bots=("chair", "a", "b"))
    await sb.human("orchestrator: @chair\nlimit: 2 posts\nmute: @b @zed")
    await sb.human("mute: @a", "t2")
    await sb.human("news in t2", "t2")
    await sb.human("@a hello")  # delivers t2 closed: unfinished
    await sb.run(2)
    sb.gw.pause("b")
    return sb


async def test_the_document_is_json_and_restores_cursor_unfinished_paused_and_settings():
    sb = await _saved_sandbox()
    data = json.loads(json.dumps(dump_room(sb.gw)))
    fresh = Sandbox(bots=("chair", "a", "b")).gw
    load_room(fresh, data)
    assert fresh.persisted["a"].cursor == 4 and fresh.persisted["a"].unfinished == {"t2"}
    assert fresh.persisted["b"].paused and not fresh.persisted["a"].paused
    assert fresh.settings["main"].orchestrator == "chair" and fresh.settings["main"].muted == {"b": 1}
    assert fresh.settings["main"].limit_posts == 2 and fresh.settings["main"].unknown_mutes == ["zed"]
    assert fresh.settings["t2"].muted == {"a": 2}
    assert dump_room(fresh) == dump_room(sb.gw)


async def test_a_restored_room_keeps_enforcing_its_settings_and_its_cursor():
    sb = await _saved_sandbox()
    data = json.loads(json.dumps(dump_room(sb.gw)))
    restored = Sandbox(bots=("chair", "a", "b"), script=Script().on(reply(["H"], "x"), bot="a"))
    load_room(restored.gw, data)
    for post in sb.room.posts:  # the same room history, replayed into the restored core
        restored.room.posts.append(post)
        restored.room._seq = post.position
    await restored.gw.restart(replay_after=4)
    await restored.run(1)
    assert restored.trace.count("turn_start") == 0  # nothing after the cursor: no turn
    await restored.post("chair", "@a still muted in t2?", "t2")
    await restored.run(1)
    assert restored.trace.last("turn_held", bot="a").gate == "mute"


def test_tuple_positions_survive_json():
    core = Sandbox().gw
    core.persisted["a"].cursor = (1700000000.5, "abc")
    core._thread("main").muted = {"b": (1700000000.25, "aaa")}
    data = json.loads(json.dumps(dump_room(core)))
    fresh: RoomCore = Sandbox().gw
    load_room(fresh, data)
    assert fresh.persisted["a"].cursor == (1700000000.5, "abc")
    assert fresh.settings["main"].muted["b"] < fresh.persisted["a"].cursor


def test_an_unknown_format_or_setting_is_refused():
    core = Sandbox().gw
    with pytest.raises(ValueError, match="format"):
        load_room(core, {"format": 99})
    with pytest.raises(ValueError, match="unknown settings"):
        load_room(core, {"format": 2, "threads": {"main": {"mode": "strict"}}})


def test_a_bot_the_room_no_longer_hosts_is_left_out():
    core = Sandbox(bots=("a",)).gw
    load_room(core, {"format": 2, "bots": {"zed": {"cursor": 9}}})
    assert list(core.persisted) == ["a"]


async def test_the_agents_document_restores_bios_and_versions_into_the_dict_the_cores_read():
    sb = Sandbox(bots=("a", "b"))
    sb.gateway.bio("a", "I run the deploys")
    sb.gateway.bio("a", "I run the deploys and the on-call")
    data = json.loads(json.dumps(dump_agents(sb.gateway)))
    assert data["agents"] == {"a": {"bio": "I run the deploys and the on-call", "bio_version": 2}}  # b never pushed one

    fresh = Sandbox(bots=("a", "b"))
    load_agents(fresh.gateway, data)
    assert fresh.gateway.status()["agents"]["a"] == {"bio_version": 2, "last_seen": None}
    assert fresh.gateway.bios == {"a": "I run the deploys and the on-call"}
    assert fresh.gw.bios == {"a": "I run the deploys and the on-call"}  # the room's copy, under the bot's username
    assert fresh.gateway.bio("a", "now the backups too") == 3  # the version continues, it does not restart
    with pytest.raises(ValueError, match="format"):
        load_agents(fresh.gateway, {"format": 0, "agents": {}})


async def test_threads_no_control_touched_are_neither_saved_nor_kept():
    sb = Sandbox(bots=("a", "b"))
    for i in range(5):
        await sb.gateway.send("a", Reply(thread=None, to=["H"], text=f"topic {i}", wake_id=None), room="team")
    await sb.human("mute: @b", "t1")
    assert len(sb.gw.settings) >= 6 and len(sb.gw.thread_state) >= 5
    assert set(dump_room(sb.gw)["threads"]) == {"t1"}
    sb.gw.prune_threads()
    assert set(sb.gw.settings) == {"t1"}
    assert "t1" not in sb.gw.thread_state or not sb.gw.thread_state["t1"].is_empty
    assert sb.gw.is_muted("b", "t1") and not sb.gw.is_muted("a", "p1")  # a dropped entry answers the same


async def test_a_turns_commit_prunes_the_threads_it_no_longer_needs():
    sb = Sandbox(bots=("a", "b"))
    await sb.gateway.send("a", Reply(thread=None, to=["H"], text="topic", wake_id=None), room="team")
    await sb.human("@a hello", "t1")
    await sb.run(3)
    assert sb.trace.last("turn_end", bot="a").outcome == "normal"
    assert all(not s.is_default for s in sb.gw.settings.values())
