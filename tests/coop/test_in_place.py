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
    sb.gateway.bind("team", "c", "c")  # the daemon binds the agent right after hosting its bot
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


async def test_a_reload_that_turns_sweeps_and_the_classifier_on_starts_them_in_place():
    sb = Sandbox(bots=("a", "b"), params=Params(**{**vars(FAST), "sweep_interval_seconds": 0}), attach_agent=False)
    marker = object()
    sb.gw.update(sb.gw.roster, Params(**{**vars(FAST), "sweep_interval_seconds": 5}), classifier=marker)
    assert sb.gw.classifier is marker
    await sb.run(11)
    assert sb.trace.last("sweep", reason="periodic") is not None


async def test_reset_stays_pending_until_the_agent_polls_the_wake():
    sb = Sandbox(bots=("a", "b"), attach_agent=False)
    sb.gw.reset("a")
    await sb.human("@a hello", "t1")
    assert sb.gw.persisted["a"].reset_pending  # issued, not yet polled: a restart would reissue it with reset
    assert sb.gateway.poll("a").reset and not sb.gw.persisted["a"].reset_pending


async def test_a_reset_wake_serves_the_unfinished_threads_posts_again():
    sb = Sandbox(bots=("a", "b"), attach_agent=False)
    await sb.human("mute: @a", "t2")
    news = await sb.human("news in t2", "t2")
    await sb.human("@a hello")  # delivers t2 closed: unfinished after the turn
    wake = sb.gateway.poll("a")
    sb.gateway.read("a", wake.wake_id)
    await sb.gateway.done("a", wake.wake_id, answered=("main",))  # main answered, t2 closed: unfinished
    assert sb.gw.persisted["a"].unfinished == {"t2"}
    sb.gw.reset("a")
    await sb.human("@a again", "t3")
    wake = sb.gateway.poll("a")
    _, posts = sb.gateway.read("a", wake.wake_id)
    assert wake.reset and news.id in [p.id for p in posts]  # before the cursor, served again
    assert [p.position for p in posts] == sorted(p.position for p in posts)
    await sb.gateway.done("a", wake.wake_id, answered=("t2",))
    await sb.human("@a once more", "t3")
    _, posts = sb.gateway.read("a", sb.gateway.poll("a").wake_id)
    assert news.id not in [p.id for p in posts]  # a plain wake: from the cursor only


async def test_a_post_arriving_below_a_cursor_or_a_wakes_mark_is_logged(caplog):
    import logging

    from coop.room.model import Post

    sb = Sandbox(bots=("a", "b"), attach_agent=False)
    await sb.human("@a one", "t1")
    wake = sb.gateway.poll("a")
    late = Post(id="late", position=0, time=0.0, thread="t1", author="H", text="late", kind="human")
    with caplog.at_level(logging.WARNING, logger="coop.room.core"):
        await sb.gw.on_post(late)
    assert any("below the mark of a's wake" in r.message and r.message.count("late") for r in caplog.records)
    sb.gateway.read("a", wake.wake_id)
    await sb.gateway.done("a", wake.wake_id, answered=("t1",))  # a normal turn: the cursor moves
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="coop.room.core"):
        await sb.gw.on_post(Post(id="later", position=0, time=0.0, thread="t1", author="H", text="x", kind="human"))
    assert any("below a's cursor" in r.message for r in caplog.records)


async def test_a_bot_hosted_before_start_gets_one_periodic_timer_not_two():
    sb = Sandbox(bots=("a", "b"), hosted=["b"], agents=["a", "b"], attach_agent=False,
                 params=Params(**{**vars(FAST), "sweep_interval_seconds": 10}))
    core = RoomCore("ops", sb.room, sb.clock, sb.trace, [], sb.gw.roster, sb.gw.params, name="ops")  # as the daemon builds it
    core.add_hosted("a", None)
    core.start()
    sb.room.cores.append(core)
    await sb.run(31)
    assert sb.trace.count("sweep", gateway="ops", bot="a", reason="periodic") == 3  # 10, 20, 30: one timer
    core.add_hosted("b", None)  # joining a started room: scheduled now, once
    await sb.run(10)
    assert sb.trace.count("sweep", gateway="ops", bot="b", reason="periodic") == 1


async def test_the_binding_is_per_room_and_strict_an_unbound_bot_of_an_agents_name_is_not_the_agents():
    """Agent ``a`` is ``b`` in the first room; the second room hosts a bot called ``a``
    that belongs to agent ``x``. Nothing ``a`` does reaches that bot: the gateway
    translates by binding, never by name."""
    sb = Sandbox(bots=("b",), attach_agent=False)
    sb.gateway.unbind("team", "b")
    sb.gateway.bind("team", "a", "b")
    other = sb.add_room("other", ["a"])
    sb.gateway.unbind("other", "a")
    sb.gateway.bind("other", "x", "a")
    await sb.human("@b hello", "t1")
    await other.post("H", "@a hello")
    await sb.run(1)
    wake = sb.gateway.poll("a")
    assert wake is not None and wake.room == "team" and wake.bot == "b"
    assert sb.gateway.poll("a") is None  # the wake in ``other`` is x's
    assert sb.gateway.poll("x").room == "other"
    with pytest.raises(PermissionError):
        sb.gateway.read_room("a", "other")
    assert not sb.gateway.hosts("a", "other") and sb.gateway.hosts("x", "other")
    with pytest.raises(ValueError, match="is 'x'\\'s bot"):
        sb.gateway.bind("other", "a", "a")
    assert Roster(("a",)).kind_of("a") == "bot"  # the roster is in the room's namespace too


async def test_an_agent_already_bound_in_a_room_is_not_rebound_under_another_username():
    sb = Sandbox(bots=("a", "b"), attach_agent=False)
    sb.gateway.bind("team", "a", "a")  # the same binding again: fine (a second _host for the same join)
    sb.gateway.unbind("team", "b")
    with pytest.raises(ValueError, match="already 'a'"):
        sb.gateway.bind("team", "a", "b")
    assert sb.gateway.username_of("team", "a") == "a" and sb.gateway.agent_of("team", "b") is None
