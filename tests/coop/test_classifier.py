"""Section 8.3, "Scheduler and classifier", the classifier half: WHO_SPEAKS with its
cache, failures and fallbacks. The scheduler tests are gone with FOLLOW_UP (design
§1.2); the model-backed classifiers' tests arrive with the daemon."""

from __future__ import annotations

from dataclasses import replace

import pytest

from coop.room.classifier import ClassifierRequest, parse_decision
from coop.room.model import Decision, Member, Params, Post
from tests.coop.helpers import FAST, InjectedFailures, Sandbox, Script, ScriptedClassifier, reply


def test_a_misspelt_classifier_fallback_is_rejected_at_construction():
    with pytest.raises(ValueError):
        Params(classifier_fallback="everybody")

async def test_a_degraded_bot_is_not_offered_to_the_classifier():
    classifier = ScriptedClassifier()
    sb = Sandbox(params=replace(FAST, consecutive_invalid_limit=1), classifier=classifier,
                 script=Script().on({"replies": []}, bot="a"))
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
    first_request = next(c for c in classifier.calls if c.post.id == "p1")
    assert [p.id for p in first_request.history] == []


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
    assert sb.trace.count("classifier_decision", post="p1") == 1
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
    classifier = ScriptedClassifier(decisions={"p1": Decision(add=("b",))}, latency=5.0)
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
    assert [p.id for p in request.history] == ["p3", "p2"] and request.omitted == 1
    agent = next(m for m in request.agents if m.username == "a")
    assert (agent.display_name, agent.nickname, agent.profile) == ("A", "a-nick", "a profile")


async def test_a_history_cap_of_zero_shows_the_whole_thread():
    classifier = ScriptedClassifier()
    sb = Sandbox(params=replace(FAST, classifier_posts=0), classifier=classifier)
    for i in range(4):
        await sb.human(f"post {i}")
    await sb.run(1)
    request = classifier.calls[-1]
    assert [p.id for p in request.history] == ["p3", "p2", "p1"] and request.omitted == 0


async def test_muted_bots_are_not_offered_to_the_classifier():
    classifier = ScriptedClassifier()
    sb = Sandbox(classifier=classifier)
    await sb.human("mute: @a")
    await sb.human("who?")
    await sb.run(1)
    assert [m.username for m in classifier.calls[-1].agents] == ["b", "c"]


async def test_timeout_retries_and_fallback_are_observable():
    classifier = ScriptedClassifier(failures={"p1": 2}, default=Decision(add=("b",)))
    sb = Sandbox(params=replace(FAST, classifier_retries=1, classifier_fallback="everyone"), classifier=classifier)
    await sb.human("question")
    await sb.run(2)
    assert sb.trace.count("classifier_retry", post="p1") == 1
    decision = sb.trace.last("classifier_decision", post="p1")
    assert decision.fallback == "everyone" and decision.add == ["a", "b", "c"]
    assert len(classifier.calls) == 2
    # a recovered retry is a decision with no fallback
    classifier.failures["p2"] = 1
    await sb.human("another")
    await sb.run(2)
    decision = sb.trace.last("classifier_decision", post="p2")
    assert decision.fallback is None and decision.add == ["b"]


async def test_a_classifier_slower_than_its_timeout_is_retried_at_the_timeout_then_falls_back():
    classifier = ScriptedClassifier(latency=30, default=Decision(add=("b",)))
    sb = Sandbox(params=replace(FAST, classifier_timeout_seconds=10, classifier_retries=1,
                               classifier_fallback="everyone"), classifier=classifier)
    await sb.human("question")
    await sb.run(21)
    retry = sb.trace.last("classifier_retry", post="p1")
    assert retry.time == 10 and "timeout" in retry.error
    decision = sb.trace.last("classifier_decision", post="p1")
    assert decision.time == 20 and decision.fallback == "everyone"
    assert len(classifier.calls) == 2  # asked twice; both answers arrived after the timeout and were dropped


async def test_classifier_reasons_reach_the_trace():
    classifier = ScriptedClassifier(default=Decision(add=("b",), reasons={"b": "asked for a reviewer"}))
    sb = Sandbox(classifier=classifier)
    await sb.human("question")
    await sb.run(1)
    assert sb.trace.last("classifier_decision", post="p1").reasons == {"b": "asked for a reviewer"}


async def test_addressed_or_everyone_fallback_matches_current_behaviour_for_bot_posts():
    classifier = ScriptedClassifier(failures={"p2": 5})
    sb = Sandbox(params=replace(FAST, classifier_retries=0, classifier_fallback="addressed-or-everyone"),
                 classifier=classifier, script=Script().on(reply(["H"], "a's remark"), bot="a", source="addressed"))
    await sb.human("@a go")
    await sb.run(2)
    decision = sb.trace.last("classifier_decision", post="p2")
    assert decision.fallback == "addressed-or-everyone" and decision.add == ["b", "c"]


async def test_replayed_posts_are_classified_with_the_history_that_preceded_them():
    cls = ScriptedClassifier(latency=0.5)
    sb = Sandbox(classifier=cls)
    sb.gw.set_offline()
    await sb.human("first")
    await sb.human("second")
    await sb.human("third")
    await sb.gw.set_online(replay_after=0)
    await sb.run(2)
    assert [(c.post.id, [p.id for p in c.history]) for c in cls.calls] == [("p1", []), ("p2", ["p1"]), ("p3", ["p2", "p1"])]


# ── the answer of a model-backed classifier ───────────────────────────────────

def _request(text="who can look at the flaky deploy?", addressed=(), history=(), omitted=0, author="H"):
    seq = len(history) + 1
    post = Post(id=f"p{seq}", position=seq, time=0.0, thread="main", author=author, text=text, kind="human")
    agents = (Member("a", "bot", "Alice", "al", "backend engineer"), Member("b", "bot", profile="frontend engineer"),
              Member("c", "bot"))
    return ClassifierRequest(post, tuple(addressed), tuple(history), omitted, agents)



def test_parse_decision_keeps_only_offered_names_in_the_right_list():
    request = _request(addressed=("b",))
    decision = parse_decision(
        '```json\n{"add": ["@a", "b", "zed", "H"], "remove": [{"username": "@b", "reason": "H is thinking aloud"}, "a"]}\n```',
        request,
    )
    assert decision.add == ("a",)  # b is addressed already, zed and H are not offered bots
    assert decision.remove == ("b",) and decision.reasons == {"b": "H is thinking aloud"}


async def test_injected_failures_wrap_a_classifier_for_the_failure_configuration():
    inner = ScriptedClassifier(default=Decision(add=("b",)))
    classifier = InjectedFailures(inner, failures={"p1": 1}, latency=0.5)
    sb = Sandbox(params=replace(FAST, classifier_retries=1), classifier=classifier)
    await sb.human("question")
    await sb.run(2)
    assert sb.trace.count("classifier_retry", post="p1") == 1 and len(inner.calls) == 1
    assert sb.trace.last("classifier_decision", post="p1").add == ["b"]


async def test_a_bio_under_a_roster_only_name_is_not_shown_to_the_classifier():
    """A roster-only bot is hosted elsewhere (design §3.1): the classifier sees it as
    ``no profile`` even when this gateway holds a bio under its name — a restored
    document from before the bot moved, or a push by that name."""
    sb = Sandbox(bots=("a", "b"), hosted=["a"], agents=["a", "b"])
    sb.gateway.bio("a", "I run the deploys")
    sb.gateway.bio("b", "stale: b moved to another gateway")
    await sb.human("who handles deploys?")
    await sb.run(0)
    offered = {m.username: m.profile for m in sb.classifier.calls[-1].agents}
    assert offered == {"a": "I run the deploys", "b": "b profile"}  # b: the roster's profile, not the gateway's bio
