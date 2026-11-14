"""The flagged catch-up experiment (``catch_up_policy: flagged``): a catch-up turn only
for a bot post the classifier flagged, every bot at once or one at a time, and the
queue dropped when a later post revises the thread."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from tests.roomsim.helpers import FAST, Sandbox, Script, reply
from tools.roomsim.classifier import JevClassifier, ScriptedClassifier, parse_decision
from tools.roomsim.model import Decision, Hint, Member, Params, Post, ThreadGroup
from tools.roomsim.prompt import render_hint
from tools.roomsim.report import flag_summary, transcript

FLAGGED = replace(FAST, catch_up_policy="flagged", catch_up_post_threshold=1, catch_up_age_seconds=20,
                  sweep_interval_seconds=10)
BOTS = ("a", "x", "y", "m")


def _flag_a(more: dict[int, Decision] | None = None) -> ScriptedClassifier:
    """a's reply (post 2) is flagged; anything else is a plain decision."""
    return ScriptedClassifier(decisions={2: Decision(flag=True, scores={"x": 0.1, "y": 0.4, "m": 0.2}), **(more or {})})


def _script() -> Script:
    return (Script()
            .on(reply(["H"], "the deploy did not touch nginx"), bot="a", source="addressed", once=True)
            .on(reply(["H", "a"], "correction: it did"), bot="m", source="catch-up", once=True))


async def _flagged(params: Params, classifier=None, script=None) -> Sandbox:
    sb = Sandbox(bots=BOTS, params=params, classifier=classifier or _flag_a(), script=script or _script())
    await sb.human("@a what happened to the deploy?")
    return sb


def _catch_ups(sb: Sandbox) -> list[tuple[float, str]]:
    return [(e.time, e.bot) for e in sb.trace.of("turn_start", source="catch-up")]


async def test_without_a_flag_nobody_gets_a_catch_up_turn():
    sb = await _flagged(FLAGGED, classifier=ScriptedClassifier())  # same thresholds, nothing flagged
    await sb.run(200)
    assert _catch_ups(sb) == [] and sb.posts_by("m") == []


async def test_a_flagged_post_gives_every_unaddressed_bot_a_catch_up_turn_at_once_under_parallel():
    sb = await _flagged(FLAGGED)
    await sb.run(200)
    flag = sb.trace.last("flag_set")
    assert flag.post == 2 and flag.queue == ["x", "y", "m"]  # a wrote it, H is not a bot
    starts = _catch_ups(sb)
    assert {b for _, b in starts} == {"x", "y", "m"} and len({t for t, _ in starts}) == 1
    assert starts[0][0] >= 1 + 20  # not before catch_up_age_seconds after the flagged post
    assert len(sb.posts_by("m")) == 1
    assert "flags: 1 set, 0 cleared by revision, 0 bots skipped; flagged catch-up turns: 3 (1 spoke, 2 done)" \
        in flag_summary(sb.sim)
    assert "⚑ post 2 flagged for checking; catch-up queue ['x', 'y', 'm']" in transcript(sb.sim)


async def test_roster_order_runs_one_bot_at_a_time_and_starts_the_next_at_the_turn_end():
    sb = await _flagged(replace(FLAGGED, catch_up_order="roster"))
    await sb.run(200)
    starts = _catch_ups(sb)
    assert [b for _, b in starts] == ["x", "y", "m"]
    ends = [e.time for e in sb.trace.of("turn_end") if e.bot in ("x", "y")]
    assert starts[1][0] == ends[0] and starts[2][0] == ends[1]  # on the turn's end, not the next sweep
    assert sb.trace.count("flag_done") == 1 and len(sb.posts_by("m")) == 1


async def test_score_order_follows_the_classifier_speak_scores():
    sb = await _flagged(replace(FLAGGED, catch_up_order="score"))
    await sb.run(200)
    assert sb.trace.last("flag_set").queue == ["y", "m", "x"]
    assert [b for _, b in _catch_ups(sb)] == ["y", "m", "x"]


async def test_on_revision_drops_the_rest_of_the_queue_once_a_post_revises_the_thread():
    classifier = _flag_a({3: Decision(revises=True)})  # m's correction is post 3 (x, y said nothing)
    sb = await _flagged(replace(FLAGGED, catch_up_order="roster", flag_clear="on-revision",
                                # m first so a bot is left behind it
                                ), classifier=classifier)
    sb.gw.hosted = ["m", "a", "x", "y"]  # roster order is the gateway's hosted order
    await sb.run(200)
    cleared = sb.trace.last("flag_cleared")
    assert cleared is not None and cleared.flags == [2] and cleared.skipped == ["x", "y"]
    assert [b for _, b in _catch_ups(sb)] == ["m"]


async def test_never_keeps_the_queue_after_a_revision():
    classifier = _flag_a({3: Decision(revises=True)})
    sb = await _flagged(replace(FLAGGED, catch_up_order="roster"), classifier=classifier)
    sb.gw.hosted = ["m", "a", "x", "y"]
    await sb.run(200)
    assert sb.trace.count("flag_cleared") == 0 and [b for _, b in _catch_ups(sb)] == ["m", "x", "y"]


async def test_a_revising_post_is_not_itself_flagged_and_an_addressed_bot_is_not_queued():
    # m's correction addresses a: a answers as addressed, so it is not queued for post 2
    # either way; the revising post 3 opens no flag even though it was graded.
    classifier = _flag_a({3: Decision(flag=True, revises=True)})
    sb = await _flagged(replace(FLAGGED, catch_up_order="roster"), classifier=classifier)
    await sb.run(200)
    assert [e.post for e in sb.trace.of("flag_set")] == [2]
    assert "a" not in sb.trace.last("flag_set").queue


async def test_a_bot_that_read_past_the_post_leaves_the_queue():
    # x is addressed by H after the flag: its addressed turn moves its cursor past post 2.
    sb = await _flagged(replace(FLAGGED, catch_up_order="roster"))
    await sb.run(5)
    await sb.human("@x unrelated: can you check the banner?")
    await sb.run(200)
    assert [b for _, b in _catch_ups(sb)] == ["y", "m"]


async def test_a_head_bot_the_sweep_cannot_start_is_skipped():
    sb = await _flagged(replace(FLAGGED, catch_up_order="roster"))
    await sb.run(5)
    await sb.human("mute: @x")
    await sb.run(200)
    skipped = sb.trace.last("flag_skipped")
    assert skipped is not None and skipped.bot == "x"
    assert [b for _, b in _catch_ups(sb)] == ["y", "m"]


async def test_the_hint_names_the_flagged_posts_only_on_the_catch_up_turn():
    seen: list[Hint] = []
    script = (Script().on(lambda turn: seen.append(turn.hint) or {"replies": [{"done": True}]}, source="catch-up")
              .on(reply(["H"], "the deploy did not touch nginx"), bot="a", source="addressed", once=True))
    sb = await _flagged(FLAGGED, script=script)
    await sb.run(200)
    assert seen and all(h.flagged == (2,) for h in seen)
    text = render_hint(seen[0])
    assert "Flagged for checking in main: post #2" in text and "reply done" in text
    assert "Flagged" not in render_hint(Hint("addressed", "main", (ThreadGroup("main", "must-reply"),)))


def test_scenario_params_accept_overrides_and_reject_unknown_values():
    from tools.roomsim.scenario import _params
    params = _params({"catch_up_policy": "flagged"}, None, {"catch_up_order": "score", "catch_up_age_seconds": "5"})
    assert params.catch_up_policy == "flagged" and params.catch_up_order == "score" and params.catch_up_age_seconds == 5
    with pytest.raises(ValueError, match="catch_up_order"):
        _params(None, None, {"catch_up_order": "random"})
    with pytest.raises(ValueError, match="unknown param"):
        _params(None, None, {"catch_up": "1"})


def _request():
    from tools.roomsim.classifier import ClassifierRequest
    agents = tuple(Member(b, "bot", profile=f"{b} profile") for b in ("a", "b"))
    post = Post(2, 1.0, "main", "a", "the ORM recreates the schema", "bot")
    return ClassifierRequest(post, (), (), 0, agents)


async def test_jev_flags_a_high_stakes_or_wrong_post_unless_it_revises_and_keeps_the_scores():
    answers = []

    async def run(argv, stdin):
        body = json.loads(stdin)
        assert {"stakes", "wrong", "revises"} <= set(body["questions"])
        return json.dumps({"answers": answers.pop(0), "usage": {}})

    def grades(stakes, wrong, revises):
        return {"speak_b": {"noul": 0.3}, "stakes": {"noul": stakes}, "wrong": {"noul": wrong}, "revises": {"noul": revises}}

    classifier = JevClassifier("team", run=run, flag_threshold=0.8)
    answers += [grades(0.9, 0.1, 0.1), grades(0.1, 0.85, 0.1), grades(0.9, 0.9, 0.95), grades(0.5, 0.5, 0.1)]
    first = (await classifier.classify(_request())).decision
    assert first.flag and not first.revises and first.scores == {"b": 0.3}
    assert (await classifier.classify(_request())).decision.flag  # wrong alone is enough
    revising = (await classifier.classify(_request())).decision
    assert revising.revises and not revising.flag  # a correction does not open a flag
    assert not (await classifier.classify(_request())).decision.flag


def test_the_json_contract_carries_flag_and_revises():
    decision = parse_decision('{"add": [], "remove": [], "flag": true, "revises": false}', _request())
    assert decision.flag and not decision.revises
    assert not parse_decision('{"add": []}', _request()).flag
