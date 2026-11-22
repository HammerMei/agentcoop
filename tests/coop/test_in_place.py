"""Changes a running room takes in place (a reload, a bot joining or leaving, an
operator command): nothing about the other bots moves, the daemon is told when the
persisted state changed, and ids survive a restart."""

import pytest

from coop.room.core import RoomCore
from coop.room.model import Params
from coop.room.roster import Roster
from coop.room.store import dump_room, load_room
from coop.wire import decode_wake, encode_wake

from .helpers import FAST, FakePlatform, Sandbox, members


async def test_a_bot_added_in_place_starts_at_the_latest_post_and_the_others_keep_their_leases():
    sb = Sandbox(bots=("a", "b", "c"), hosted=["a", "b"], agents=["a", "b", "c"], attach_agent=False)
    await sb.human("@a hello", "t1")
    await sb.human("later")
    assert sb.gateway.poll("a") is not None
    sb.gw.add_hosted("c", sb.room.last_position())
    assert sb.gw.volatile["a"].turn is not None  # a's lease is untouched
    assert sb.gw.persisted["c"].cursor == sb.room.last_position()
    assert sb.room.posts_after(sb.gw.persisted["c"].cursor) == []  # nothing old is c's first wake
    assert sb.trace.last("hosted_added").bot == "c"
    await sb.human("@c now you")
    await sb.run(1)
    assert sb.gateway.poll("c") is not None


async def test_a_bot_removed_in_place_ends_its_lease_as_removed_and_its_timers_stop():
    sb = Sandbox(bots=("a", "b"), attach_agent=False)
    await sb.human("@a hello", "t1")
    await sb.human("@b you too", "t2")
    wa, wb = sb.gateway.poll("a"), sb.gateway.poll("b")
    assert wa is not None and wb is not None
    await sb.gw.remove_hosted("a")
    assert sb.trace.last("turn_end", bot="a").outcome == "removed"
    assert "a" not in sb.gw.hosted and "a" not in sb.gw.persisted and "a" not in sb.gw.volatile
    assert sb.gw.volatile["b"].turn is not None and sb.gw.volatile["b"].turn.wake.wake_id == wb.wake_id
    await sb.run(FAST.sweep_interval_seconds * 3 + 1)  # the periodic sweep of a does not raise
    assert sb.gw.status()["bots"].keys() == {"b"}
    await sb.gw.remove_hosted("a")  # idempotent


async def test_a_reload_updates_roster_and_params_in_place():
    sb = Sandbox(bots=("a", "b"), attach_agent=False)
    roster = Roster(("a", "b"), {"a": frozenset({"K"}), "b": frozenset({"H"})})
    sb.gw.update(roster, Params(**{**vars(FAST), "enforce": True}))
    assert sb.gw.params.enforce and sb.gw.role_label("a", "H") == "guest" and sb.gw.role_label("b", "H") == "owner"
    with pytest.raises(ValueError, match="not in the roster"):
        sb.gw.add_hosted("zed", None)


async def test_reset_rides_on_the_next_wake_once_and_is_persisted_until_then():
    sb = Sandbox(bots=("a", "b"), attach_agent=False)
    changes = []
    sb.gw.on_change = lambda: changes.append(dump_room(sb.gw))
    sb.gw.reset("a")
    assert changes[-1]["bots"]["a"]["reset_pending"] is True
    fresh = Sandbox(bots=("a", "b"), attach_agent=False).gw
    load_room(fresh, changes[-1])
    assert fresh.persisted["a"].reset_pending
    await sb.human("@a hello", "t1")
    wake = sb.gateway.poll("a")
    assert wake.reset and decode_wake(encode_wake(wake)).reset
    assert changes[-1]["bots"]["a"]["reset_pending"] is False
    await sb.gateway.done("a", wake.wake_id, failed="x")
    await sb.human("@a again", "t1")
    assert not sb.gateway.poll("a").reset


async def test_the_daemon_is_told_after_a_commit_a_control_a_pause_and_a_resume():
    sb = Sandbox(bots=("a", "b"))
    seen = []
    sb.gw.on_change = lambda: seen.append(len(seen))
    await sb.human("mute: @b")
    assert len(seen) == 1  # the control
    sb.gw.pause("a")
    sb.gw.resume("a")
    assert len(seen) == 3
    await sb.human("@a hello", "t1")
    await sb.run(3)
    assert sb.trace.last("turn_end", bot="a").outcome == "normal"
    assert len(seen) >= 4  # the commit


async def test_wake_ids_count_from_the_seed_so_two_runs_never_share_one():
    sb = Sandbox(bots=("a", "b"), attach_agent=False)
    ops = FakePlatform("ops", members(("a",)), sb.clock, sb.trace)
    core = RoomCore("ops", ops, sb.clock, sb.trace, ["a"], Roster.from_members(members(("a",)), ["a"]),
                    FAST, wake_seed=1_700_000_000_000)
    sb.gateway.add_room(core)
    ops.cores.append(core)
    core.start()
    await ops.post("H", "@a hi")
    wake = sb.gateway.poll("a")
    assert wake.wake_id == "w-ops-1700000000001"
    with pytest.raises(ValueError, match="already on this gateway"):
        sb.gateway.add_room(core)
    assert sb.gateway.remove_room("ops") is core and sb.gateway.remove_room("ops") is None


async def test_a_bio_push_tells_the_daemon():
    sb = Sandbox(bots=("a", "b"), attach_agent=False)
    seen = []
    sb.gateway.on_bio = lambda: seen.append(1)
    assert sb.gateway.bio("a", "I do things") == 1 and seen == [1]
