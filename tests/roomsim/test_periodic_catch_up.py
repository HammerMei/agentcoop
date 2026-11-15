"""Periodic catch-up (spec 5.7): a bot that has ended no turn for ``catch_up_idle_seconds``
gets one turn for the posts waiting behind its cursor, and asks itself only whether it
has something to add or revise."""

from __future__ import annotations

from dataclasses import replace

import pytest

from tests.roomsim.helpers import FAST, Sandbox, Script, reply
from tools.roomsim.bots import fail
from tools.roomsim.classifier import ScriptedClassifier
from tools.roomsim.model import Decision, Hint, ThreadGroup
from tools.roomsim.prompt import render_hint
from tools.roomsim.scenario import _params, load, run

IDLE = replace(FAST, catch_up_idle_seconds=30, sweep_interval_seconds=10)


def _catch_ups(sb: Sandbox, bot: str | None = None) -> list[float]:
    return [e.time for e in sb.trace.of("turn_start", source="catch-up") if bot is None or e.bot == bot]


async def test_an_idle_bot_with_waiting_posts_gets_one_catch_up_turn_after_catch_up_idle_seconds():
    sb = Sandbox(params=IDLE)
    await sb.human("news for the room")  # addresses nobody; the scripted classifier adds nobody
    await sb.run(25)
    assert _catch_ups(sb) == []
    await sb.run(10)  # the sweep at 30: every bot has been idle since the gateway started
    assert {e.bot for e in sb.trace.of("turn_start", source="catch-up")} == {"a", "b", "c"}
    assert all(e.time == 30.0 and e.delivered == [1] for e in sb.trace.of("turn_start", source="catch-up"))
    await sb.run(100)
    assert len(_catch_ups(sb)) == 3  # nothing new to deliver: no further turn


async def test_zero_disables_periodic_catch_up_and_an_empty_backlog_starts_nothing():
    sb = Sandbox(params=replace(IDLE, catch_up_idle_seconds=0))
    await sb.human("news for the room")
    await sb.run(300)
    assert _catch_ups(sb) == []
    sb = Sandbox(params=IDLE)
    await sb.run(300)  # idle for ten intervals with nothing waiting
    assert sb.trace.count("turn_start") == 0


async def test_the_timer_counts_from_the_bot_s_last_normal_turn_end():
    sb = Sandbox(params=IDLE, script=Script().on(reply(["H"], "a answers"), bot="a", source="addressed"))
    await sb.human("@a go")  # a's turn ends at 1
    await sb.run(2)
    await sb.human("news for the room")
    await sb.run(40)
    assert _catch_ups(sb, "b") == [30.0] and _catch_ups(sb, "c") == [30.0]  # idle since the start
    assert _catch_ups(sb, "a") == [40.0]  # idle since its turn ended at 1: due at 31, next sweep at 40


async def test_a_failed_catch_up_turn_leaves_the_timer_alone_and_the_retry_follows_the_backoff():
    attempts: list[int] = []
    script = Script().on(lambda turn: attempts.append(1) or (fail() if len(attempts) == 1 else {"replies": [{"done": True}]}),
                         bot="a", source="catch-up")
    sb = Sandbox(params=IDLE, script=script)
    await sb.human("news for the room")
    await sb.run(60)
    assert _catch_ups(sb, "a") == [30.0, 51.0]  # failed at 31, backoff 20: retried at 51, not 30 s later
    assert sb.trace.last("turn_end", bot="a").outcome == "normal"


async def test_a_restart_resets_the_timer_so_a_restart_wakes_nobody_at_once():
    sb = Sandbox(params=IDLE)
    await sb.human("news for the room")
    await sb.run(20)
    await sb.gw.restart(replay_after=0)  # the replayed post addresses nobody: no turn
    await sb.run(20)
    assert _catch_ups(sb) == []  # at 30 the bots have been idle only since the restart at 20
    await sb.run(20)
    assert set(_catch_ups(sb)) == {50.0}


async def test_a_thread_with_a_post_still_being_classified_is_not_due_so_the_pick_is_not_lost():
    # The second post lands just before the sweep at 30 with its decision 2 s away: the
    # catch-up waits, b is picked at 31.9 with both posts, the others catch up at 40.
    classifier = ScriptedClassifier(decisions={2: Decision(add=("b",))}, latency=2.0)
    sb = Sandbox(params=IDLE, classifier=classifier)
    await sb.human("news for the room")
    await sb.run(29.9)
    await sb.human("a question for whoever knows")
    await sb.run(20.1)
    assert _catch_ups(sb) == [40.0, 40.0]  # a and c, not at 30
    picked = sb.trace.last("turn_start", bot="b")
    assert picked.source == "picked" and picked.time == 31.9 and picked.delivered == [1, 2]
    assert _catch_ups(sb, "b") == []


async def test_a_post_being_classified_in_another_thread_holds_the_whole_catch_up():
    # a has old backlog in t2; a post in main is being classified when the sweep at 30 runs.
    # The catch-up turn would deliver both threads, so it waits; the pick runs at 31.9.
    classifier = ScriptedClassifier(decisions={2: Decision(add=("a",))}, latency=2.0)
    sb = Sandbox(params=IDLE, classifier=classifier)
    await sb.human("old news", "t2")
    await sb.run(29.9)
    await sb.human("a question for whoever knows")
    await sb.run(20.1)
    picked = sb.trace.last("turn_start", bot="a")
    assert picked.source == "picked" and picked.time == 31.9 and sorted(picked.delivered) == [1, 2]
    assert _catch_ups(sb, "a") == [] and _catch_ups(sb) == [40.0, 40.0]


async def test_a_failure_notice_alone_makes_nothing_due_and_rides_along_with_the_next_turn():
    sb = Sandbox(params=IDLE, script=Script().on(fail(), bot="a", once=True))
    await sb.human("@a @b @c go")  # b and c answer; a fails at 1 and posts a notice: their only backlog
    await sb.run(40)
    assert sb.trace.count("notice", bot="a") == 1 and _catch_ups(sb) == []
    await sb.human("news for the room")
    await sb.run(40)
    delivered = [e.delivered for e in sb.trace.of("turn_start", source="catch-up") if e.bot == "b"]
    assert delivered and sb.trace.last("notice", bot="a").seq in delivered[0]


async def test_a_thread_closed_to_the_bot_is_not_due():
    sb = Sandbox(params=IDLE)
    await sb.human("mute: @a")
    await sb.human("news for the room")
    await sb.run(100)
    assert _catch_ups(sb, "a") == [] and _catch_ups(sb, "b") == [30.0]


async def test_the_hint_asks_for_additions_or_revisions_on_a_catch_up_turn_only():
    seen: list[Hint] = []
    sb = Sandbox(params=IDLE, script=Script().on(lambda turn: seen.append(turn.hint) or {"replies": [{"done": True}]}))
    await sb.human("news for the room")
    await sb.run(30)
    assert seen and all(h.source == "catch-up" for h in seen)
    text = render_hint(seen[0])
    assert "Catch-up in main: you were not addressed" in text and "otherwise reply done" in text
    assert "Catch-up" not in render_hint(Hint("addressed", "main", (ThreadGroup("main", "must-reply"),)))


async def test_the_catch_up_corrections_scenario_corrects_both_claims_through_periodic_catch_up():
    sim = await run(load("tools/roomsim/scenarios/catch_up_corrections.yaml"))
    by_author = {}
    for post in sim.room.posts:
        by_author.setdefault(post.author, []).append(post)
    assert len(by_author["m"]) == 1 and len(by_author["v"]) == 1 and "x" not in by_author and "y" not in by_author
    catch_ups = [e for e in sim.trace.of("turn_start") if e.source == "catch-up"]
    assert {e.bot for e in catch_ups if e.time == 60.0} == {"x", "y", "m", "v"}  # idle since the start
    assert [e.bot for e in catch_ups if e.time == 90.0] == ["b"]  # idle since its turn at 2: sees the corrections
    assert {e.bot for e in catch_ups if e.time == 150.0} == {"x", "y", "m", "v"}  # second wave: done


def test_scenario_params_accept_overrides_and_reject_unknown_values():
    assert _params({"catch_up_idle_seconds": 120}, None, {"catch_up_idle_seconds": "5"}).catch_up_idle_seconds == 5
    with pytest.raises(ValueError, match="unknown param"):
        _params(None, None, {"catch_up_order": "roster"})
    with pytest.raises(ValueError, match="true or false"):
        _params(None, None, {"classifier_enabled": "treu"})
    assert _params(None, None, {"classifier_enabled": "False"}).classifier_enabled is False
