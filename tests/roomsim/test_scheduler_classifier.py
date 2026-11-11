"""Section 8.3, "Scheduler and classifier": FOLLOW_UP on the scheduler stand-in and
WHO_SPEAKS with its cache, failures and fallbacks."""

from __future__ import annotations

from dataclasses import replace

from tests.roomsim.helpers import FAST, Sandbox, Script, reply
from tools.roomsim.bots import fail
from tools.roomsim.classifier import ScriptedClassifier
from tools.roomsim.model import Decision

SCHEDULE = {"replies": [{"done": True}], "schedule": [{"in_seconds": 5, "text": "check on b"}]}


async def test_jobs_fire_in_their_thread_retry_failed_turns_and_stop_at_the_cap():
    script = Script().on(SCHEDULE, bot="a", source="addressed", once=True).on(fail(), bot="a", source="job")
    sb = Sandbox(params=replace(FAST, follow_up_retry_cap=2, failure_backoff_seconds=5), script=script)
    await sb.human("@a wait for b", "t1")
    await sb.run(2)
    await sb.run(5)  # fires at 6 in t1, fails: retry 1 under backoff
    fired = sb.trace.last("turn_start", bot="a", source="job")
    assert fired.thread == "t1" and sb.bot.turns[-1].job_text == "check on b"
    assert sb.trace.last("job_retry", bot="a").retries == 1
    await sb.run(40)  # two retries under the backoff fail: the cap of 2 retries is reached
    assert sb.trace.count("job_capped", bot="a") == 1
    assert sb.trace.count("turn_start", bot="a", source="job") == 3  # first firing + 2 retries
    assert sb.trace.count("notice", bot="a", cause="scheduled job gave up") == 1
    assert sb.gw.status()["bots"]["a"]["jobs"] == 0


async def test_a_cap_of_zero_never_gives_up():
    script = Script().on(SCHEDULE, bot="a", source="addressed", once=True).on(fail(), bot="a", source="job")
    sb = Sandbox(params=replace(FAST, follow_up_retry_cap=0, failure_backoff_seconds=5, failure_backoff_max_seconds=5),
                 script=script)
    await sb.human("@a wait")
    await sb.run(60)
    assert sb.trace.count("job_capped") == 0 and sb.trace.count("job_retry", bot="a") >= 5


async def test_a_degraded_bot_is_not_offered_to_the_classifier():
    classifier = ScriptedClassifier()
    sb = Sandbox(params=replace(FAST, consecutive_invalid_limit=1), classifier=classifier,
                 script=Script().on("garbage", bot="a"))
    await sb.human("@a go")
    await sb.run(3)
    assert sb.gw.status()["bots"]["a"]["degraded"]
    await sb.human("anyone?")
    await sb.run(1)
    assert [m.username for m in classifier.calls[-1].agents] == ["b", "c"]


async def test_a_dm_post_without_a_mention_addresses_the_bot():
    dm = Sandbox(bots=("a",), is_dm=True, script=Script().on(reply(["H"], "hi back"), bot="a"))
    await dm.human("hello there")
    await dm.run(2)
    assert dm.trace.last("turn_start", bot="a").exempt is True and len(dm.posts_by("a")) == 1


async def test_the_classifier_sees_the_thread_as_it_was_when_the_post_arrived():
    classifier = ScriptedClassifier(latency=5.0)
    sb = Sandbox(classifier=classifier)
    await sb.human("first")
    await sb.human("second, before the decision on the first")
    await sb.run(6)
    first_request = next(c for c in classifier.calls if c.post.seq == 1)
    assert [p.seq for p in first_request.history] == []


async def test_a_job_firing_is_not_a_post_but_its_visible_reply_is():
    script = Script().on(SCHEDULE, bot="a", source="addressed", once=True).on(reply(["H"], "b is late"), bot="a", source="job")
    sb = Sandbox(script=script)
    await sb.human("@a wait")
    await sb.run(2)
    assert sb.gw.status()["threads"]["main"]["recent_posts"] == 0
    await sb.run(6)
    assert sb.gw.status()["threads"]["main"]["recent_posts"] == 1
    assert sb.room.posts[-1].author == "a"


async def test_a_muted_job_waits_and_runs_on_the_unmute_sweep_in_both_orderings():
    # mute before the firing
    sb = Sandbox(script=Script().on(SCHEDULE, bot="a", source="addressed", once=True))
    await sb.human("@a wait")
    await sb.run(2)
    await sb.human("mute: @a")
    await sb.run(5)
    assert sb.trace.last("job_waiting", bot="a").gate == "mute"
    await sb.human("unmute: @a")
    await sb.run(1)
    assert sb.trace.count("turn_start", bot="a", source="job") == 1
    # mute during a retry
    script = Script().on(SCHEDULE, bot="a", source="addressed", once=True).on(fail(), bot="a", source="job", once=True)
    sb = Sandbox(params=replace(FAST, failure_backoff_seconds=5), script=script)
    await sb.human("@a wait")
    await sb.run(7)  # fired at 6, failed, backoff to 11
    await sb.human("mute: @a")
    await sb.run(10)  # backoff ended at 11 but the mute holds the retry
    assert sb.trace.last("job_waiting", bot="a").gate == "mute"
    assert sb.trace.count("turn_start", bot="a", source="job") == 1
    await sb.human("unmute: @a")
    await sb.run(1)
    assert sb.trace.count("turn_start", bot="a", source="job") == 2
    assert sb.trace.last("turn_end", bot="a").outcome == "normal"


async def test_repeated_firings_while_muted_coalesce_to_one_pending_turn():
    script = Script().on({"replies": [{"done": True}], "schedule": [{"in_seconds": 2, "text": "x"}, {"in_seconds": 3, "text": "y"}]},
                         bot="a", source="addressed", once=True)
    sb = Sandbox(script=script)
    await sb.human("@a wait")
    await sb.run(1)
    await sb.human("mute: @a")
    await sb.run(5)
    assert sb.trace.count("job_waiting", bot="a") == 2
    await sb.human("unmute: @a")
    await sb.run(3)
    # one job per firing stays pending; each gets its own turn, one at a time
    assert sb.trace.count("turn_start", bot="a", source="job") == 2


async def test_no_classifier_call_for_dm_control_only_or_all_muted():
    dm = Sandbox(bots=("a",), is_dm=True)
    await dm.human("@a hi")
    assert dm.trace.count("classifier_call") == 0
    sb = Sandbox()
    await sb.human("mute: all")
    assert sb.trace.count("classifier_call") == 0  # control-only
    await sb.human("anyone?")
    assert sb.trace.count("classifier_call") == 0  # every bot muted
    await sb.human("unmute: @a")
    await sb.human("anyone now?")
    assert sb.trace.count("classifier_call") == 1


async def test_three_local_copies_produce_one_call_and_one_decision():
    classifier = ScriptedClassifier(default=Decision(add=("b", "c")))
    sb = Sandbox(classifier=classifier)
    await sb.human("who knows about nginx?")
    await sb.run(2)
    assert len(classifier.calls) == 1
    assert sb.trace.count("classifier_decision", post=1) == 1
    assert {e.bot for e in sb.trace.of("turn_start", source="picked")} == {"b", "c"}


async def test_addressed_bots_still_speak_when_listed_in_remove():
    classifier = ScriptedClassifier(default=Decision(remove=("a",), reasons={"a": "not needed"}))
    sb = Sandbox(classifier=classifier, script=Script().on(reply(["H"], "here"), bot="a"))
    await sb.human("@a go")
    await sb.run(2)
    assert len(sb.posts_by("a")) == 1
    assert sb.trace.last("classifier_decision").remove == ["a"]


async def test_a_decision_naming_a_bot_on_another_gateway_starts_no_local_turn():
    sb = Sandbox(bots=("a", "b"), hosted=["a"], agents=["a", "b"],
                 classifier=ScriptedClassifier(default=Decision(add=("b",))))
    await sb.human("question")
    await sb.run(2)
    assert sb.trace.last("classifier_decision").add == ["b"]
    assert sb.trace.count("turn_start") == 0


async def test_late_results_do_not_revive_delivered_posts():
    classifier = ScriptedClassifier(decisions={1: Decision(add=("b",))}, latency=5.0)
    sb = Sandbox(classifier=classifier)
    await sb.human("slow question")  # decision due at 5
    await sb.human("@b meanwhile")  # b's turn delivers post 1 at t=0
    await sb.human("newer unrelated post")
    await sb.run(6)
    assert sb.trace.count("turn_start", bot="b", source="picked") == 0


async def test_truncated_history_reports_the_omitted_count_and_profiles_reach_the_classifier():
    classifier = ScriptedClassifier()
    sb = Sandbox(params=replace(FAST, classifier_posts=2), classifier=classifier)
    for i in range(4):
        await sb.human(f"post {i}")
    await sb.run(1)  # decisions arrive after the classifier latency
    request = classifier.calls[-1]
    assert [p.seq for p in request.history] == [3, 2] and request.omitted == 1
    agent = next(m for m in request.agents if m.username == "a")
    assert (agent.display_name, agent.nickname, agent.profile) == ("A", "a-nick", "a profile")


async def test_muted_bots_are_not_offered_to_the_classifier():
    classifier = ScriptedClassifier()
    sb = Sandbox(classifier=classifier)
    await sb.human("mute: @a")
    await sb.human("who?")
    await sb.run(1)
    assert [m.username for m in classifier.calls[-1].agents] == ["b", "c"]


async def test_timeout_retries_and_fallback_are_observable():
    classifier = ScriptedClassifier(failures={1: 2}, default=Decision(add=("b",)))
    sb = Sandbox(params=replace(FAST, classifier_retries=1, classifier_fallback="everyone"), classifier=classifier)
    await sb.human("question")
    await sb.run(2)
    assert sb.trace.count("classifier_retry", post=1) == 1
    decision = sb.trace.last("classifier_decision", post=1)
    assert decision.fallback == "everyone" and decision.add == ["a", "b", "c"]
    assert len(classifier.calls) == 2
    # a recovered retry is a decision with no fallback
    classifier.failures[2] = 1
    await sb.human("another")
    await sb.run(2)
    decision = sb.trace.last("classifier_decision", post=2)
    assert decision.fallback is None and decision.add == ["b"]


async def test_addressed_or_everyone_fallback_matches_current_behaviour_for_bot_posts():
    classifier = ScriptedClassifier(failures={2: 5})
    sb = Sandbox(params=replace(FAST, classifier_retries=0, classifier_fallback="addressed-or-everyone"),
                 classifier=classifier, script=Script().on(reply(["H"], "a's remark"), bot="a", source="addressed"))
    await sb.human("@a go")
    await sb.run(2)
    decision = sb.trace.last("classifier_decision", post=2)
    assert decision.fallback == "addressed-or-everyone" and decision.add == ["b", "c"]
