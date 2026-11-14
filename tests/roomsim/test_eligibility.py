"""Section 8.3, "Eligibility, mute, and rate": the turn table of 4.1, MUTE, AUTHORITY,
RATE_LIMIT and its exemptions."""

from __future__ import annotations

from dataclasses import replace

from tests.roomsim.helpers import FAST, Sandbox, Script, item, replies, reply
from tools.roomsim.model import Decision


async def test_addressed_only_fallback_an_unaddressed_bot_post_starts_no_turn():
    sb = Sandbox(params=replace(FAST, classifier_enabled=False, classifier_fallback="addressed-only"),
                 script=Script().on(reply(["H"], "unaddressed remark"), bot="a", source="addressed"))
    await sb.human("@a say something")
    await sb.run(3)
    assert len(sb.posts_by("a")) == 1
    assert sb.trace.count("turn_start", bot="b") == 0 and sb.trace.count("turn_start", bot="c") == 0


async def test_under_mute_all_bot_mentions_start_nothing_human_and_orchestrator_still_do():
    sb = Sandbox(script=Script().on(reply(["b"], "@b your turn"), bot="a", source="addressed", once=True)
                 .on(reply(["b"], "@b report"), bot="chair", source="addressed"),
                 bots=("chair", "a", "b"))
    await sb.human("orchestrator: @chair\nmute: all")
    assert sb.gw.status()["threads"]["main"]["muted"] == ["a", "b"]
    await sb.human("@a go")  # human mention of a muted bot starts it
    await sb.run(2)
    assert sb.trace.count("turn_start", bot="a") == 1
    assert sb.trace.last("turn_held", bot="b").gate == "mute"  # a's mention of b started nothing
    await sb.human("@chair ask b")
    await sb.run(2)
    assert sb.trace.count("turn_start", bot="b", source="addressed") == 1  # orchestrator mention did


async def test_under_mute_all_a_bot_mention_of_the_orchestrator_starts_its_turn():
    sb = Sandbox(script=Script().on(reply(["chair"], "@chair here is my answer"), bot="a", source="addressed"),
                 bots=("chair", "a"))
    await sb.human("orchestrator: @chair\nmute: all")
    await sb.human("@a answer the chair")
    await sb.run(3)
    assert sb.trace.count("turn_start", bot="chair", source="addressed") == 1
    assert sb.trace.last("turn_start", bot="chair").exempt is False  # under rate and cooldown, not muted


async def test_a_muted_bot_may_answer_the_human_who_addressed_it():
    sb = Sandbox(script=Script().on(reply(["H"], "yes?"), bot="a"))
    await sb.human("mute: @a")
    await sb.human("@a are you there?")
    await sb.run(2)
    assert len(sb.posts_by("a")) == 1
    hint = sb.bot.turns[-1].hint
    assert hint.group_of("main").group == "must-reply"  # opened by the human's mention


async def test_after_restart_mutes_are_reconstructed_from_persisted_settings():
    sb = Sandbox()
    await sb.human("mute: @a")
    await sb.gw.restart(replay_after=1)
    await sb.sim.post("b", "@a what do you think?")
    await sb.run(2)
    assert sb.trace.last("turn_held", bot="a").gate == "mute"


async def test_orchestrator_none_from_an_owner_clears_it_a_mute_does_not():
    sb = Sandbox(humans=(("H", "owner"), ("G", "guest")), bots=("chair", "a"))
    await sb.human("orchestrator: @chair")
    await sb.human("mute: @chair")
    assert sb.gw.status()["threads"]["main"]["orchestrator"] == "chair"
    await sb.human("orchestrator: none", author="G")  # a guest cannot
    assert sb.gw.status()["threads"]["main"]["orchestrator"] == "chair"
    assert sb.trace.last("control_ignored").by == "G"
    await sb.human("orchestrator: none")
    assert sb.gw.status()["threads"]["main"]["orchestrator"] is None


async def test_human_and_orchestrator_mentions_bypass_rate_and_cooldown():
    sb = Sandbox(params=replace(FAST, rate_limit_posts=1, cooldown_seconds=100),
                 script=Script().on(reply(["H"], "reply"), bot="a").on(reply(["a"], "@a again"), bot="chair"),
                 bots=("chair", "a"))
    await sb.human("orchestrator: @chair")
    await sb.human("@a one")
    await sb.run(2)
    await sb.human("@a two")  # thread at its post limit and a in cooldown: human mention still starts
    await sb.run(2)
    assert sb.trace.count("turn_start", bot="a", exempt=True) == 2
    await sb.human("@chair poke a")
    await sb.run(3)
    assert sb.trace.count("turn_start", bot="a", exempt=True) == 3
    assert sb.trace.count("turn_held", bot="a") == 0


async def test_a_scheduled_job_starts_in_its_thread_despite_the_rate_limit():
    sb = Sandbox(params=replace(FAST, rate_limit_posts=1),
                 script=Script().on({"replies": [{"done": True}], "schedule": [{"in_seconds": 5, "text": "check"}]},
                                    bot="a", source="addressed").on(reply(["H"], "still waiting"), bot="b"))
    await sb.human("@a schedule; @b fill the limit")
    await sb.run(2)
    assert sb.gw.status()["threads"]["main"]["recent_posts"] == 1  # at the limit
    await sb.run(5)
    job_turn = sb.trace.last("turn_start", bot="a", source="job")
    assert job_turn is not None and job_turn.exempt is True


async def test_a_reply_in_a_thread_open_at_turn_start_still_posts_when_a_mute_arrives_mid_turn():
    sb = Sandbox(script=Script().on(reply(["H"], "late but allowed"), bot="a"))
    await sb.human("@a go")  # turn runs for 1s
    await sb.human("mute: @a")  # arrives while the turn runs
    await sb.run(2)
    assert len(sb.posts_by("a")) == 1
    assert sb.trace.last("turn_end", bot="a").outcome == "normal"


async def test_post_word_and_cooldown_limits_bind_independently_and_zero_disables_one():
    script = Script().on(reply(["H"], "w " * 50), bot="a").on(reply(["H"], "ok"), bot="b")
    sb = Sandbox(params=replace(FAST, rate_limit_posts=0, rate_limit_words=40, cooldown_seconds=30), script=script)
    await sb.human("@a go")
    await sb.run(2)  # a posted 51 words: word gate holds, post gate disabled
    await sb.sim.post("c", "@b your view?")  # c is hosted, so its post goes through a turn? no: direct post
    await sb.run(1)
    assert sb.trace.last("turn_held", bot="b").gate == "rate_words"
    await sb.human("thanks")  # human post clears the window
    await sb.run(1)
    assert sb.trace.count("turn_start", bot="b") == 1
    await sb.sim.post("c", "@b and again?")
    await sb.run(1)
    assert sb.trace.last("turn_held", bot="b").gate == "cooldown"  # b posted 1s ago


async def test_a_thread_below_its_post_limit_but_above_its_word_limit_holds_a_turn():
    sb = Sandbox(params=replace(FAST, rate_limit_posts=10, rate_limit_words=5),
                 script=Script().on(reply(["H"], "one two three four five six"), bot="a"))
    await sb.human("@a go")
    await sb.run(2)
    await sb.sim.post("b", "@a more?")
    await sb.run(1)
    assert sb.trace.last("turn_held", bot="a").gate == "rate_words"


async def test_human_post_clears_the_count_and_releases_the_held_turn_age_lift_waits_for_the_sweep():
    sb = Sandbox(params=replace(FAST, rate_limit_posts=1, rate_limit_seconds=50, sweep_interval_seconds=30),
                 script=Script().on(reply(["H"], "reply"), bot="a"), bots=("a", "b"), hosted=["a"], agents=["a", "b"])
    await sb.human("@a go")
    await sb.run(2)
    await sb.sim.post("b", "@a and?")  # held: one recent agent post already
    assert sb.trace.last("turn_held", bot="a").gate == "rate_posts"
    await sb.human("carry on")  # clears the window and requests a sweep
    await sb.run(0)  # the requested sweep is a zero-delay event
    assert sb.trace.last("sweep", bot="a").reason == "human post"
    assert sb.trace.count("turn_start", bot="a") == 2
    assert 3 in sb.trace.last("turn_start", bot="a").delivered  # with the human post in context
    # age-based lift: a's post at 3 leaves the window at 53; the periodic sweep finds it at 60
    await sb.run(2)
    await sb.sim.post("b", "@a once more?")
    assert sb.trace.last("turn_held", bot="a").gate == "rate_posts"
    await sb.run(200)
    third = sb.turns("a")[2]
    assert third.time == 60.0 and third.delivered == [6]


async def test_equal_agent_lists_converge_unequal_lists_show_in_status():
    sb = Sandbox(bots=("a", "b"), hosted=["a"], agents=["a", "b"],
                 script=Script().on(reply(["H"], "x"), bot="a").on(reply(["H"], "y"), bot="b"))
    g2 = sb.add_gateway("g2", ["b"], ["a", "b"])
    await sb.human("@a @b go")
    await sb.run(3)
    assert sb.gw.status()["threads"]["main"]["recent_posts"] == g2.status()["threads"]["main"]["recent_posts"] == 2
    g3 = sb.add_gateway("g3", [], ["a"])  # a gateway that does not list b
    assert g3.status()["bot_like_members_missing"] == ["b"]


async def test_classifier_picked_turn_is_not_exempt():
    from tools.roomsim.classifier import ScriptedClassifier
    sb = Sandbox(params=replace(FAST, rate_limit_posts=1),
                 classifier=ScriptedClassifier(default=Decision(add=("b",))),
                 script=Script().on(reply(["H"], "a answers"), bot="a"))
    await sb.human("@a go")
    await sb.run(2)
    assert sb.trace.last("turn_held", bot="b").gate == "rate_posts"
    assert sb.trace.last("turn_held", bot="b").source == "picked"


async def test_guest_and_ordinary_bot_controls_change_nothing():
    sb = Sandbox(humans=(("H", "owner"), ("G", "guest")),
                 script=Script().on(replies(item(control={"mute": ["b"]}), item(done=True)), bot="a"))
    await sb.human("mute: @a", author="G")
    assert sb.gw.status()["threads"]["main"]["muted"] == []
    await sb.human("@a try to mute b")
    await sb.run(3)
    assert sb.trace.last("turn_end", bot="a").outcome == "invalid"  # enforced: control from a non-orchestrator
    assert sb.gw.status()["threads"]["main"]["muted"] == []
