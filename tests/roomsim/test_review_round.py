"""Defects the internal review of PR #202 found, each with the operator-visible outcome it
had: the same scenario tracing differently by hosting, a dead turn posting after a
restart, a human mention stuck behind a muted bot's mention."""

from __future__ import annotations

from dataclasses import replace

from tests.roomsim.helpers import FAST, Sandbox, Script, reply


async def test_own_post_counted_before_any_gateway_receives_it_so_hosting_does_not_change_the_trace():
    """a's reply reaches the one-post limit and mentions b: b must be held whether b sits
    on a's gateway or on another one (5.3, 8.3 "equal agent lists converge")."""
    gates = {}
    for topology in ("one gateway", "two gateways"):
        script = Script().on(reply(["b"], "@b over to you"), bot="a").on(reply(["H"], "b here"), bot="b")
        params = replace(FAST, rate_limit_posts=1)
        if topology == "one gateway":
            sb = Sandbox(params=params, script=script, bots=("a", "b"))
        else:
            sb = Sandbox(params=params, script=script, bots=("a", "b"), hosted=["a"], agents=["a", "b"])
            sb.add_gateway("g2", ["b"], ["a", "b"])
        await sb.human("@a go")
        await sb.run(2)
        gates[topology] = [e.gate for e in sb.trace.of("turn_held", bot="b")]
    assert gates["one gateway"] == gates["two gateways"] == ["rate_posts"]


async def test_restart_mid_turn_drops_the_dead_turn_and_the_replayed_one_answers_once():
    sb = Sandbox(script=Script().on(reply(["H"], "reply"), bot="a"))
    sb.bot.duration = 5
    await sb.human("@a go")
    await sb.run(1)
    await sb.gw.restart(replay_after=0)  # the process died mid-turn; the replay starts a new one
    await sb.run(10)
    assert sb.trace.count("turn_dropped", bot="a") == 1
    assert len(sb.posts_by("a")) == 1 and sb.trace.count("turn_start", bot="a") == 2


async def test_restart_mid_job_turn_puts_the_job_back_to_pending():
    script = Script().on({"replies": [{"done": True}], "schedule": [{"in_seconds": 2, "text": "check"}]},
                         bot="a", source="addressed", once=True)
    sb = Sandbox(script=script)
    await sb.human("@a wait")
    await sb.run(1.5)
    sb.bot.duration = 5
    await sb.run(1.5)  # the job fired at 3 and its turn is running
    assert sb.trace.count("turn_start", bot="a", source="job") == 1
    await sb.gw.restart(replay_after=sb.room.last_seq())
    await sb.run(15)
    assert sb.trace.count("turn_dropped", bot="a") == 1
    assert sb.trace.count("turn_start", bot="a", source="job") == 2  # the sweep found it pending again


async def test_a_human_mention_is_not_hidden_behind_a_held_bot_mention():
    sb = Sandbox(params=replace(FAST, sweep_interval_seconds=10), script=Script().on(reply(["H"], "x"), bot="a"))
    await sb.human("mute: @a")
    await sb.human("@a are you there")  # starts (exempt) and runs 1s
    await sb.sim.post("b", "@a hey")  # muted: held, and it is now the oldest addressing post
    await sb.human("@a second question")  # held by one_turn behind it
    await sb.run(5)
    assert sb.trace.count("turn_start", bot="a") == 2
    assert sb.gw.status()["bots"]["a"]["cursor"] == sb.room.last_seq() - 1  # a's own reply excluded


async def test_the_orchestrator_is_not_held_by_cooldown():
    sb = Sandbox(params=replace(FAST, cooldown_seconds=100), bots=("chair", "a"),
                 script=Script().on(reply(["H"], "chair speaks"), bot="chair"))
    await sb.human("orchestrator: @chair")
    await sb.human("@chair hi")
    await sb.run(2)
    await sb.sim.post("a", "@chair question")
    await sb.run(1)
    assert sb.trace.count("turn_held", bot="chair") == 0
    assert sb.trace.count("turn_start", bot="chair") == 2


async def test_an_owner_control_only_post_clears_the_window_and_releases_held_bots():
    sb = Sandbox(params=replace(FAST, rate_limit_posts=1), bots=("a", "b"), hosted=["a"], agents=["a", "b"],
                 script=Script().on(reply(["H"], "reply"), bot="a"))
    await sb.human("@a go")
    await sb.run(2)
    await sb.sim.post("b", "@a and?")  # held by the limit
    assert sb.trace.last("turn_held", bot="a").gate == "rate_posts"
    await sb.human("limit: 10 posts")  # control-only, still a human post
    await sb.run(0)
    assert sb.gw.status()["threads"]["main"]["recent_posts"] == 0
    assert sb.trace.count("turn_start", bot="a") == 2


async def test_a_thread_archived_during_a_turn_does_not_become_unfinished():
    sb = Sandbox()
    sb.bot.duration = 5
    await sb.human("mute: @a", "t2")
    await sb.human("news", "t2")
    await sb.human("@a hello")  # delivers t2 closed; the turn runs 5s
    await sb.run(1)
    await sb.sim.archive("t2")
    await sb.run(10)
    assert sb.gw.status()["bots"]["a"]["unfinished"] == []
