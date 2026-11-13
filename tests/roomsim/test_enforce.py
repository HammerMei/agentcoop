"""The enforcement toggle: the same scenario under ``enforce=True`` and ``False``
yields the same opportunities, counted as blocked in one mode and complied or
violated in the other. Thresholds are untouched by the switch; ``0`` on a rate or
cooldown parameter still disables that gate in both modes."""

from __future__ import annotations

from dataclasses import replace

import pytest

from tests.roomsim.helpers import FAST, Sandbox, Script, item, replies, reply
from tools.roomsim.bots import fail

ON = FAST
OFF = replace(FAST, enforce=False)


def rules(sb):
    """(opportunities, complied, violated, rejected, held, held_complied, held_violated) per mechanism."""
    return {m: (c["opportunities"], c["complied"], c["violated"], c["rejected"], c["held"],
                c["held_complied"], c["held_violated"])
            for m, c in sb.gw.status()["rules"].items()}


async def test_a_muted_bot_mention_is_held_when_enforced_and_runs_closed_when_not():
    script = Script().on(reply(["b"], "I will answer anyway"), bot="a", source="addressed")
    on = Sandbox(params=ON, script=script)
    await on.human("mute: @a")
    await on.sim.post("b", "@a what do you think?")
    await on.run(2)
    assert on.trace.last("turn_held", bot="a").gate == "mute" and on.posts_by("a") == []
    assert rules(on)["mute"] == (1, 0, 0, 0, 1, 0, 0)  # held: the bot never chose

    off = Sandbox(params=OFF, script=script)
    await off.human("mute: @a")
    await off.sim.post("b", "@a what do you think?")
    await off.run(2)
    turn = [t for t in off.bot.turns if t.bot == "a"][0]  # a's reply then started b's turn
    assert turn.hint.group_of("main").group == "closed" and turn.hint.group_of("main").closed_by == ("mute",)
    assert len(off.posts_by("a")) == 1  # posted anyway: the counterpart of the enforced hold
    assert rules(off)["mute"] == (1, 0, 0, 0, 0, 0, 1)


async def test_a_compliant_bot_scores_complied_in_the_measure_only_mode():
    off = Sandbox(params=OFF)  # default script: done everywhere
    await off.human("mute: @a")
    await off.sim.post("b", "@a what do you think?")
    await off.run(2)
    assert off.posts_by("a") == []
    assert rules(off)["mute"] == (1, 0, 0, 0, 0, 1, 0)


async def test_rule_problems_are_posted_and_counted_when_not_enforced():
    script = Script().on(replies(item(control={"mute": ["b"]}), item(to=["a"], text="talking to myself")), bot="a")
    off = Sandbox(params=OFF, script=script)
    await off.human("@a go")
    await off.run(2)
    assert off.trace.last("turn_end", bot="a").outcome == "normal"
    assert [p.text for p in off.posts_by("a")] == ["mute: @b", "@a talking to myself"]
    assert rules(off)["authority"] == (1, 0, 1, 0, 0, 0, 0) and rules(off)["recipients"] == (1, 0, 1, 0, 0, 0, 0)
    on = Sandbox(params=ON, script=script)
    await on.human("@a go")
    await on.run(2)
    assert on.trace.last("turn_end", bot="a").outcome == "invalid" and on.posts_by("a") == []


async def test_structural_problems_are_still_rejected_when_not_enforced():
    off = Sandbox(params=OFF, script=Script().on("not json at all", bot="a"))
    await off.human("@a go")
    await off.run(2)
    assert off.trace.last("turn_end", bot="a").outcome == "invalid"


async def test_hard_holds_are_not_switched_off():
    off = Sandbox(params=OFF, script=Script().on(fail(), bot="a", once=True))
    await off.human("@a go")
    await off.run(2)
    await off.sim.post("b", "@a again")
    await off.run(1)
    assert off.trace.last("turn_held", bot="a").gate == "backoff"


async def test_rate_zero_still_disables_the_gate_in_both_modes():
    script = Script().on(reply(["H"], "reply"), bot="a")
    for params in (replace(ON, rate_limit_posts=0, rate_limit_words=0), replace(OFF, rate_limit_posts=0, rate_limit_words=0)):
        sb = Sandbox(params=params, script=script)
        for _ in range(3):
            await sb.human("@a go")
            await sb.run(2)
        assert "rate_posts" not in rules(sb) and sb.trace.count("turn_held") == 0


async def test_the_same_scenario_yields_comparable_counts_in_both_modes():
    script = Script().on(reply(["H"], "w " * 20), bot="a").on(reply(["H"], "me too"), bot="b", source="picked")
    from tools.roomsim.classifier import ScriptedClassifier
    from tools.roomsim.model import Decision
    counts = {}
    for name, params in (("on", replace(ON, rate_limit_posts=1)), ("off", replace(OFF, rate_limit_posts=1))):
        sb = Sandbox(params=params, script=script, classifier=ScriptedClassifier(default=Decision(add=("b",))))
        await sb.human("@a go")
        await sb.run(3)
        counts[name] = rules(sb)
    assert counts["on"]["rate_posts"] == (1, 0, 0, 0, 1, 0, 0)
    assert counts["off"]["rate_posts"] == (1, 0, 0, 0, 0, 0, 1)


async def test_a_turn_that_fails_on_a_rule_is_counted_as_rejected():
    """The worst offender must show in the table: enforced, a bot that keeps writing in a
    closed thread fails its turn and still counts one rejected opportunity per cause."""
    script = Script().on(replies(item(thread="main", to=["H"], text="still talking"), item(thread="t2", done=True)), bot="a")
    on = Sandbox(params=ON, script=script)
    await on.human("mute: @a")
    await on.human("news in main")
    await on.human("@a hello", "t2")  # delivers main closed
    await on.run(3)
    assert on.trace.last("turn_end", bot="a").outcome == "invalid"
    assert rules(on)["mute"] == (1, 0, 0, 1, 0, 0, 0)
    assert rules(on)["must_reply"][3] == 0  # t2 was answered


async def test_a_control_rejected_on_the_first_attempt_is_counted_even_when_dropped_on_the_second():
    script = Script().on(replies(item(control={"mute": ["b"]}), item(done=True)), bot="a", once=True) \
        .on(replies(item(done=True)), bot="a")
    on = Sandbox(params=ON, script=script)
    await on.human("@a go")
    await on.run(3)
    assert on.trace.last("turn_end", bot="a").outcome == "normal"
    assert rules(on)["authority"] == (1, 0, 0, 1, 0, 0, 0)


async def test_text_without_recipients_is_posted_bare_and_counted_when_not_enforced():
    off = Sandbox(params=OFF, script=Script().on(replies(item(text="to nobody")), bot="a"))
    await off.human("@a go")
    await off.run(2)
    assert [p.text for p in off.posts_by("a")] == ["to nobody"]
    assert rules(off)["recipients"] == (1, 0, 1, 0, 0, 0, 0)


async def test_a_closed_thread_is_no_catch_up_or_unfinished_candidate_in_either_mode():
    """Spec 5.7: catch-up needs an open thread and an unfinished thread is a candidate once
    it has become open. Both modes therefore see the same (empty) candidate list, and
    the measure-only mode does not loop on a closed unfinished thread."""
    for params in (replace(ON, catch_up_post_threshold=1, catch_up_age_seconds=0),
                   replace(OFF, catch_up_post_threshold=1, catch_up_age_seconds=0)):
        sb = Sandbox(params=params, bots=("a", "b"))
        await sb.human("mute: @a")
        await sb.human("news for a")
        await sb.sim.post("b", "@a what do you think?")  # the one opportunity: a bot mention
        await sb.run(25)
        assert sb.trace.count("turn_start", bot="a") == (0 if params.enforce else 1)
        assert "mute" in rules(sb) and rules(sb)["mute"][0] == 1
        assert sb.trace.count("turn_start", bot="a", source="unfinished") == 0


@pytest.mark.parametrize("hosted", [None, ["a"]])  # a gateway hosting only the author counts it too
async def test_replay_of_an_already_delivered_mention_starts_no_turn_and_own_posts_seed_the_window(hosted):
    script = Script().on(reply(["H"], "reply from a"), bot="a")
    sb = Sandbox(params=ON, script=script, hosted=hosted)
    await sb.human("@a go")
    await sb.run(2)
    before = sb.trace.count("turn_start", bot="a")
    await sb.gw.restart(replay_after=0)  # replays the mention a already answered, and a's own reply
    await sb.run(2)
    assert sb.trace.count("turn_start", bot="a") == before
    assert sb.gw.status()["threads"]["main"]["recent_posts"] == 1  # a's own post seeded the window


async def test_a_rejected_first_attempt_is_counted_when_the_retry_fails_at_the_backend():
    attempts = []

    def handler(turn):
        attempts.append(turn)
        if len(attempts) == 1:
            return replies(item(thread="main", to=["H"], text="hi"), item(thread="t2", to=["H"], text="sneak"))
        return fail()

    sb = Sandbox(script=Script().on(handler, bot="a"))
    await sb.human("mute: @a", thread="t2")
    await sb.human("fyi", thread="t2")
    await sb.human("@a go")  # t2 is delivered closed; the text there is rejected, the retry fails
    await sb.run(5)
    assert [e.outcome for e in sb.trace.of("turn_end", bot="a")] == ["backend_failure"]
    assert sb.gw.rules["mute"].rejected == 1


async def test_a_first_attempt_backend_failure_counts_no_rule():
    sb = Sandbox(script=Script().on(fail(), bot="a"))
    await sb.human("@a go")
    await sb.run(5)
    assert sb.gw.rules == {}
