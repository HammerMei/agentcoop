"""Section 6 worked examples reproduced end to end, and section 8.1's replay contract."""

from __future__ import annotations

from dataclasses import replace

import pytest

from tests.helpers import MockAgentBackend
from tests.roomsim.helpers import FAST, Sandbox, Script, item, replies, reply
from tools.roomsim.bots import BackendBot, Mixed, Recorder, Replayer, ReplayMismatch, ScriptedBot
from tools.roomsim.classifier import ScriptedClassifier
from tools.roomsim.model import Decision, Hint, Member, ThreadGroup, TurnInput


def mentions_from(author):
    return lambda turn: any(p.author == author for p in turn.delivered)


async def test_6_1_handoff():
    script = (
        Script()
        .on({"replies": [{"done": True}], "schedule": [{"in_seconds": 1800, "text": "is a done?"}]},
            bot="b", source="addressed", once=True)
        .on(reply(["H", "b"], "migration complete"), bot="a", source="addressed", once=True)
        .on(reply(["H", "a"], "deployed"), bot="b", when=mentions_from("a"), once=True)
    )
    sb = Sandbox(params=replace(FAST, catch_up_post_threshold=3), script=script)
    await sb.human("@a run the migration; @b, when a is done, deploy.")
    await sb.run(0)
    assert {e.bot for e in sb.trace.of("turn_start") if e.time == 0.0} == {"a", "b"}  # in parallel
    await sb.run(5)
    posts = [(p.author, sorted(p.mentions)) for p in sb.room.posts if p.kind == "bot"]
    assert posts == [("a", ["H", "b"]), ("b", ["H", "a"])]
    assert sb.trace.count("job_scheduled", bot="b") == 1
    assert sb.trace.last("turn_end", bot="a").outcome == "normal"  # a returned done to b's report
    await sb.run(10)  # c receives the exchange through catch-up and returns done
    catch = sb.trace.last("turn_start", bot="c")
    assert catch.source == "catch-up" and catch.delivered == [1, 2, 3]
    assert sb.posts_by("c") == []


async def test_6_1_handoff_without_the_mention_the_classifier_infers_the_dependency():
    script = (
        Script()
        .on({"replies": [{"done": True}]}, bot="b", source="addressed", once=True)
        .on(reply(["H"], "migration complete"), bot="a", source="addressed", once=True)
        .on(reply(["H", "a"], "deployed"), bot="b", source="picked", once=True)
    )
    classifier = ScriptedClassifier(decisions={2: Decision(add=("b",), reasons={"b": "b was told to deploy after a"})})
    sb = Sandbox(script=script, classifier=classifier)
    await sb.human("@a run the migration; @b, when a is done, deploy.")
    await sb.run(5)
    assert sb.trace.count("turn_start", bot="b", source="picked") == 1
    assert [p.author for p in sb.room.posts if p.kind == "bot"] == ["a", "b"]


async def test_6_2_orchestrated_brainstorm():
    members = ("chair", "a", "b", "c", "d")
    script = (
        Script()
        .on(reply(["a", "b", "c", "d"], "one post each; address only me", control={"mute": "all"}),
            bot="chair", source="addressed", once=True)
        .on(lambda t: reply(["chair", "b"], f"{t.bot}'s idea, @b what about yours?"), bot="a", source="addressed", once=True)
        .on(lambda t: reply(["chair"], f"{t.bot}'s idea"), source="addressed", when=lambda t: t.bot in ("b", "c", "d"), once=False)
        .on(reply(["a", "b", "c", "d"], "summary: four ideas", control={"unmute": "all"}), bot="chair",
            when=mentions_from("d"), once=True)  # d reports last: synthesise and reopen the floor
        .on(lambda t: {"replies": [{"done": True}]}, bot="chair")  # still collecting
    )
    sb = Sandbox(bots=members, script=script)
    await sb.human("orchestrator: @chair")
    await sb.human("@chair run a brainstorm")
    await sb.run(1.5)
    chair_post = sb.posts_by("chair")[0]
    assert chair_post.text.startswith("mute: @a @b @c @d\n")
    assert sb.gw.status()["threads"]["main"]["muted"] == ["a", "b", "c", "d"]
    assert {e.bot for e in sb.trace.of("turn_start", exempt=True) if e.bot != "chair"} == {"a", "b", "c", "d"}
    await sb.run(1)
    # a's mention of b started no peer turn: b is muted, and b's own turn was the chair's mention
    assert sb.trace.count("turn_start", bot="b") == 1
    assert sb.trace.last("turn_held", bot="b").gate == "mute"
    await sb.run(30)
    # each report started a chair turn under rate and cooldown, as an addressed, non-exempt turn
    chair_turns = sb.turns("chair")
    assert len(chair_turns) >= 3 and all(not e.exempt for e in chair_turns[1:])
    # the chair's summary unmuted everyone, and its mention started their turns
    assert sb.gw.status()["threads"]["main"]["muted"] == []
    assert sb.trace.count("turn_start", bot="b") == 2
    assert sb.trace.last("control_applied", control="unmute").value == ["a", "b", "c", "d"]


async def test_6_3_one_turn_two_threads_open_and_closed():
    x_reply = replies(item(thread="B", to=["H"], text="the figure is 12"), item(thread="A", to=["b"], text="thanks, noted"))
    sb = Sandbox(bots=("x", "b"), hosted=["x"], agents=["x", "b"], script=Script().on(x_reply, bot="x"))
    await sb.sim.post("b", "x's figure should be 12, not 21", "A")  # classifier selects nobody
    await sb.run(1)
    await sb.human("@x what is the figure?", "B")
    await sb.run(2)
    assert len(sb.posts_by("x", "A")) == 1 and len(sb.posts_by("x", "B")) == 1
    # the closed variant: A is muted, the hint marks it closed, A becomes unfinished
    sb2 = Sandbox(bots=("x", "b"), hosted=["x"], agents=["x", "b"],
                  script=Script().on(replies(item(thread="B", to=["H"], text="12")), bot="x"))
    await sb2.human("mute: @x", "A")
    await sb2.sim.post("b", "x's figure should be 12, not 21", "A")
    await sb2.run(1)
    await sb2.human("@x what is the figure?", "B")
    await sb2.run(2)
    assert sb2.bot.turns[-1].hint.group_of("A").group == "closed"
    assert sb2.gw.status()["bots"]["x"]["unfinished"] == ["A"]
    await sb2.human("unmute: @x", "A")
    await sb2.run(1)
    assert sb2.trace.last("turn_start", bot="x").source == "unfinished"


async def test_6_4_two_gateways_classify_independently_and_agree_on_mute_all():
    g1_classifier = ScriptedClassifier(default=Decision(add=("a",)))
    g2_classifier = ScriptedClassifier(default=Decision(add=("a",)))  # each selects a: one answer
    sb = Sandbox(bots=("a", "b"), hosted=["a"], agents=["a", "b"], classifier=g1_classifier,
                 script=Script().on(reply(["H"], "a answers"), bot="a", source="picked", once=True))
    g2 = sb.add_gateway("g2", ["b"], ["a", "b"], classifier=g2_classifier)
    await sb.human("unaddressed question")
    await sb.run(2)
    assert sb.trace.count("classifier_call", post=1) == 2  # once per gateway
    assert [p.author for p in sb.room.posts if p.kind == "bot"] == ["a"]
    assert sb.gw.status()["threads"]["main"]["recent_posts"] == g2.status()["threads"]["main"]["recent_posts"] == 1
    await sb.human("mute: all")
    assert sb.gw.status()["threads"]["main"]["muted"] == g2.status()["threads"]["main"]["muted"] == ["a", "b"]


async def test_recorded_run_replays_to_the_same_trace_and_a_drifted_replay_fails_loudly():
    def build(runner):
        sb = Sandbox(bots=("a", "b"), script=None)
        sb.gw.runner = runner
        return sb

    script = Script().on(reply(["H", "b"], "@b over to you"), bot="a", source="addressed").on(reply(["H"], "done here"), bot="b")
    recorder = Recorder(ScriptedBot(script))
    live = build(recorder)
    await live.human("@a start")
    await live.run(5)
    replay = build(Replayer(recorder.records))
    await replay.human("@a start")
    await replay.run(5)
    strip = lambda tr: [str(e) for e in tr.events]  # noqa: E731
    assert strip(replay.trace) == strip(live.trace)
    drifted = build(Replayer(recorder.records))
    with pytest.raises(ReplayMismatch):
        await drifted.human("@a start, but differently")  # the first turn starts on the post
        await drifted.run(5)
    longer = Replayer(recorder.records + [recorder.records[-1]])  # a recording with one more turn than the run
    shorter = build(longer)
    await shorter.human("@a start")
    await shorter.run(5)
    assert longer.unused() == 1  # the CLI turns this into an [ERROR] and a non-zero exit


def test_turn_hash_covers_the_retry_error():
    from tools.roomsim.bots import turn_hash
    from tools.roomsim.model import Hint, TurnInput
    first = TurnInput("a", "addressed", "main", (), Hint("addressed", "main", ()))
    retry = replace(first, error="schema@main: done must be a boolean")
    assert turn_hash(first) != turn_hash(retry)


async def test_a_session_whose_first_send_fails_is_recreated_with_its_intro():
    backend = MockAgentBackend(default_response='{"replies": [{"done": true}]}')
    backend.side_effect = TimeoutError
    bot = BackendBot(backend, "/nowhere", 10, "team", [Member("H", "owner"), Member("a", "bot")])
    turn = TurnInput("a", "addressed", "main", (), Hint("addressed", "main", (ThreadGroup("main", "must-reply"),)))
    first = await bot.run(turn)
    backend.side_effect = None
    second = await bot.run(turn)
    assert first.failed and not second.failed
    assert len(backend.created_sessions) == 2  # the poisoned session was not kept
    assert all(m["prompt"].startswith("You are a") for m in backend.sent_messages)  # both sends carried the intro


async def test_a_mixed_runner_scripts_the_named_bots_and_sends_the_rest_to_the_backend():
    backend = MockAgentBackend(default_response='{"replies": [{"to": ["H"], "text": "live"}]}')
    members = [Member("H", "owner"), Member("a", "bot"), Member("b", "bot")]
    live = BackendBot(backend, "/nowhere", 10, "team", members)
    scripted = ScriptedBot(lambda turn: {"replies": [{"to": ["H"], "text": "scripted"}]})
    runner = Mixed(live, scripted, frozenset({"b"}))
    hint = Hint("addressed", "main", (ThreadGroup("main", "must-reply"),))
    out_a = await runner.run(TurnInput("a", "addressed", "main", (), hint))
    out_b = await runner.run(TurnInput("b", "addressed", "main", (), hint))
    assert "live" in out_a.text and "scripted" in out_b.text
    assert [m["prompt"].startswith("You are a") for m in backend.sent_messages] == [True]
