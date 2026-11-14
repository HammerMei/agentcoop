"""Section 8.3, "Sweep and unfinished state": CATCH_UP, ONE_TURN, the decision cache,
HINT groups."""

from __future__ import annotations

from dataclasses import replace

from tests.roomsim.helpers import FAST, Sandbox, Script, item, replies, reply
from tools.roomsim.bots import fail
from tools.roomsim.classifier import ScriptedClassifier
from tools.roomsim.model import Decision


async def muted_delivery(script=None, params=FAST):
    """a is muted in main, a post lands there, a human mention in t2 delivers main closed."""
    sb = Sandbox(script=script, params=params)
    await sb.human("mute: @a")
    await sb.human("something in main")
    await sb.human("@a hello", "t2")
    await sb.run(2)
    return sb


async def test_closed_delivered_thread_becomes_unfinished_and_starts_when_it_opens():
    sb = await muted_delivery()
    assert sb.gw.status()["bots"]["a"]["unfinished"] == ["main"]
    await sb.human("unmute: @a")
    await sb.run(1)
    assert sb.trace.count("sweep", bot="a", reason="unmute") == 1
    turn = sb.trace.last("turn_start", bot="a")
    assert turn.source == "unfinished" and turn.thread == "main" and turn.delivered == [4]
    assert sb.trace.count("unfinished_removed", bot="a", thread="main") == 1


async def test_done_clears_a_thread_a_failed_turn_does_not_change_unfinished_state():
    done_in_main = replies(item(thread="t2", done=True), item(thread="main", done=True))
    sb = await muted_delivery(Script().on(done_in_main, bot="a", source="addressed"))
    assert sb.gw.status()["bots"]["a"]["unfinished"] == []  # done in the closed thread recorded nothing
    sb2 = await muted_delivery(Script().on(fail(), bot="a", once=True))
    assert sb2.gw.status()["bots"]["a"]["unfinished"] == []  # the failed turn committed nothing
    await sb2.run(30)  # retried after the backoff, answered with silence in main
    assert sb2.gw.status()["bots"]["a"]["unfinished"] == ["main"]


async def test_open_unfinished_thread_is_must_reply_in_another_turn_and_cleared_there():
    sb = await muted_delivery()
    await sb.human("unmute: @a")  # main opens; the sweep it requests is still queued...
    await sb.human("@a and now this", "t3")  # ...when this mention starts a turn in t3
    await sb.run(2)
    turn = [t for t in sb.bot.turns if t.starting_thread == "t3"][-1]
    assert turn.hint.must_reply == ("main", "t3")
    assert sb.trace.count("unfinished_removed", bot="a", thread="main") == 1


async def test_every_open_unfinished_thread_is_cleared_by_one_turn():
    sb = Sandbox()
    await sb.human("mute: @a")
    await sb.human("mute: @a", "t2")
    await sb.human("news", "main")
    await sb.human("news", "t2")
    await sb.human("@a hi", "t3")
    await sb.run(2)
    assert sb.gw.status()["bots"]["a"]["unfinished"] == ["main", "t2"]
    await sb.human("unmute: @a")
    await sb.human("unmute: @a", "t2")
    await sb.run(1)
    assert sb.trace.count("turn_start", bot="a", source="unfinished") == 1
    assert sb.gw.status()["bots"]["a"]["unfinished"] == []


async def test_restart_preserves_unfinished_state():
    sb = await muted_delivery()
    await sb.gw.restart(replay_after=sb.room.last_seq())
    assert sb.gw.status()["bots"]["a"]["unfinished"] == ["main"]


async def test_a_mention_before_a_later_mute_starts_nothing_while_muted():
    sb = Sandbox(script=Script().on(reply(["H"], "busy"), bot="a", once=True))
    await sb.human("@a first")  # a runs for 1s
    await sb.sim.post("b", "@a second")  # held: one turn
    await sb.human("mute: @a")  # arrives before a is free
    await sb.run(5)
    assert sb.trace.last("turn_held", bot="a").gate == "mute"
    assert sb.trace.count("turn_start", bot="a") == 1


async def test_becoming_free_starts_nothing_until_the_requested_sweep_runs():
    sb = Sandbox()
    await sb.human("@a one")
    await sb.human("@a two")  # held by one_turn
    await sb.run(1)
    kinds = [(e.kind, e.fields.get("bot")) for e in sb.trace.events if e.time == 1.0 and e.fields.get("bot") == "a"]
    assert kinds.index(("turn_end", "a")) < kinds.index(("sweep", "a")) < kinds.index(("turn_start", "a"))


async def test_a_sweep_fixes_its_candidate_list_once_and_its_high_water_mark_excludes_later_posts():
    sb = Sandbox(params=replace(FAST, catch_up_post_threshold=1, catch_up_age_seconds=0))
    await sb.human("first")
    await sb.run(10)  # periodic sweep at 10: catch-up turns for a, b, c with hwm 1
    sweep = sb.trace.last("sweep", bot="a", reason="periodic")
    assert sweep.candidates == ["catch-up:main"]
    await sb.human("second")  # at t=10, after the sweep fixed its mark
    assert sb.trace.last("turn_start", bot="a").delivered == [1]


async def test_held_turns_are_reconstructed_in_event_order_with_all_meanwhile_posts():
    sb = Sandbox()
    await sb.human("@a one")
    await sb.human("meanwhile")
    await sb.human("@a three")
    await sb.run(2)
    second = sb.turns("a")[1]
    assert second.delivered == [2, 3] and second.thread == "main" and second.source == "addressed"


async def test_rate_limited_bot_mention_starts_on_a_later_sweep_unless_another_turn_delivered_it():
    params = replace(FAST, rate_limit_posts=1, rate_limit_seconds=15, sweep_interval_seconds=10)
    sb = Sandbox(params=params, script=Script().on(reply(["H"], "a answers"), bot="a"),
                 bots=("a", "b"), hosted=["a"], agents=["a", "b"])
    await sb.human("@a go")
    await sb.run(2)
    await sb.sim.post("b", "@a thoughts?")  # held by rate
    assert sb.trace.last("turn_held", bot="a").gate == "rate_posts"
    await sb.run(20)  # window expires at 16; the sweep at 20 starts it
    assert sb.trace.last("turn_start", bot="a").time == 20.0
    # the other case: a human mention delivers b's post first
    await sb.run(2)
    await sb.sim.post("b", "@a again?")
    await sb.human("@a human here")
    await sb.run(10)
    starts = sb.turns("a")
    assert starts[-1].time == 24.0 and 5 in starts[-1].delivered
    assert len(starts) == 3  # no extra turn for b's post on the sweep at 30


async def test_cached_classifier_choice_starts_a_held_bot_later_and_expires_into_catch_up():
    params = replace(FAST, rate_limit_posts=1, rate_limit_seconds=15, classifier_decision_ttl_seconds=12,
                     sweep_interval_seconds=10, catch_up_post_threshold=10, catch_up_age_seconds=100)
    classifier = ScriptedClassifier(decisions={1: Decision(add=("b",))}, latency=2.0)
    sb = Sandbox(params=params, classifier=classifier, script=Script().on(reply(["H"], "a answers"), bot="a"))
    await sb.human("@a go")  # a answers at 1; the decision adding b arrives at 2: held by the limit
    await sb.run(2)
    assert sb.trace.last("turn_held", bot="b").gate == "rate_posts"
    await sb.run(8)  # sweep at 10: still held (window to 16), decision still valid (ttl to 14)
    assert sb.trace.last("turn_held", bot="b", source="picked").time == 10.0
    await sb.run(10)  # sweep at 20: window clear, decision expired → counted, no picked turn
    assert sb.trace.count("classifier_expired", bot="b", post=1) == 1
    assert sb.trace.count("turn_start", bot="b", source="picked") == 0
    assert sb.gw.status()["bots"]["b"]["expired_decisions"] == 1
    await sb.run(100)  # ordinary catch-up by age at the sweep after 100s
    assert sb.trace.last("turn_start", bot="b").source == "catch-up"


async def test_hint_states_the_turn_source_and_the_three_groups():
    sb = Sandbox(params=replace(FAST, catch_up_post_threshold=1, catch_up_age_seconds=0))
    await sb.human("mute: @a", "closed")
    await sb.human("in closed", "closed")
    await sb.human("in optional", "optional")
    await sb.human("@a in start", "start")
    await sb.run(2)
    hint = sb.bot.turns[-1].hint
    assert hint.source == "addressed" and hint.starting_thread == "start"
    assert {g.thread: g.group for g in hint.threads} == {
        "closed": "closed", "optional": "optional", "start": "must-reply"}
    assert hint.group_of("closed").closed_by == ("mute",)
    await sb.run(10)  # b catches up
    catch = [t for t in sb.bot.turns if t.bot == "b"][-1]
    assert catch.source == "catch-up"


async def test_one_room_wide_delivery_satisfies_several_qualifying_threads():
    sb = Sandbox(params=replace(FAST, catch_up_post_threshold=1, catch_up_age_seconds=0))
    for thread in ("t1", "t2", "t3"):
        await sb.human("news", thread)
    await sb.run(10)
    sweep = sb.trace.last("sweep", bot="a", reason="periodic")
    assert sorted(sweep.candidates) == ["catch-up:t1", "catch-up:t2", "catch-up:t3"]
    await sb.run(1)
    assert sb.trace.count("turn_start", bot="a") == 1  # one turn delivered all three
    assert sb.trace.last("sweep", bot="a", reason="turn end").candidates == []


async def test_unmute_requests_an_immediate_sweep_for_a_waiting_job():
    sb = Sandbox(script=Script().on({"replies": [{"done": True}], "schedule": [{"in_seconds": 3, "text": "check"}]},
                                    bot="a", source="addressed", once=True))
    await sb.human("@a schedule")
    await sb.run(2)
    await sb.human("mute: @a")
    await sb.run(3)  # the job fires at 3 while muted
    assert sb.trace.last("job_waiting", bot="a").gate == "mute"
    await sb.human("unmute: @a")
    await sb.run(1)
    assert sb.trace.last("turn_start", bot="a").source == "job"
