"""The turn as the remote-agent gateway runs it (design §3.2, §3.3, §3.5): the wake
and its lease, the read record, the ledger at ``done``, unsolicited sends, pause,
and what the gateway does with a client that misbehaves."""

from __future__ import annotations

from dataclasses import replace

import pytest

from coop.client import InProcessDoorbell
from coop.errors import InvalidReply, NoLiveLease, RefusedSend
from coop.room.model import Member, Reply
from tests.coop.helpers import FAST, Sandbox, Script, item, replies, reply
from tools.fakeagent.agent import Behaviour

# ── the wake and its lease ───────────────────────────────────────────────────


async def test_a_wake_carries_metadata_and_no_text_and_the_lease_starts_at_the_poll():
    sb = Sandbox(script=Script().on(reply(["H"], "hi"), bot="a"), duration=2)
    await sb.human("context first")
    post = await sb.human("@a hello")
    await sb.run(0)  # the in-process agent polls on the next clock event
    wake = sb.bot.turns[-1].wake
    assert wake.source == "addressed" and wake.room == "team" and wake.bot == "a"
    assert wake.trigger.post_id == post.id and wake.trigger.author_role == "owner" and wake.trigger.relayed is False
    assert wake.hwm == post.position and wake.rules_version == sb.gateway.rules_version == 1
    assert wake.lease_expires_at == 0.0 + FAST.lease_seconds  # polled at once, in process
    assert not hasattr(wake, "text") and [p.id for p in sb.bot.turns[-1].delivered] == ["p1", "p2"]
    assert sb.trace.last("lease_started", bot="a").wake == wake.wake_id
    await sb.run(2)
    assert sb.trace.last("turn_end", bot="a").outcome == "normal"


async def test_one_outstanding_wake_per_bot_a_second_poll_gets_nothing():
    sb = Sandbox(duration=5)
    await sb.human("@a one")
    await sb.run(0)
    doorbell = InProcessDoorbell(sb.gateway, "a")
    assert await doorbell.poll() is None  # the fake agent already holds the lease
    await sb.human("@a two")  # held by one_turn, not a second wake
    assert sb.trace.last("turn_held", bot="a").gate == "one_turn"


async def test_a_wake_nobody_polls_stays_pending_with_its_mark_and_the_cursor_stays():
    sb = Sandbox(bots=("a", "b"), hosted=["a"], agents=["a", "b"])
    sb.gateway.agents["a"].listeners.clear()  # the agent is not polling
    await sb.human("@a hello")
    await sb.human("later news")
    await sb.run(30)
    assert sb.gw.status()["bots"]["a"]["wake"]["leased"] is False and sb.gw.status()["bots"]["a"]["cursor"] is None
    wake = sb.gateway.poll("a")  # the agent comes back
    assert wake.hwm == 1 and wake.lease_expires_at == 30.0 + FAST.lease_seconds  # issued with the mark of its time
    _, posts = sb.gateway.read("a", wake.wake_id)
    assert [p.id for p in posts] == ["p1"]  # post 2 waits for the next sweep after done
    await sb.gateway.done("a", wake.wake_id, answered=("main",))
    assert sb.gw.status()["bots"]["a"]["cursor"] == 1
    await sb.run(60)  # post 2 addresses nobody: the periodic catch-up delivers it
    catch_up = sb.trace.last("turn_start", bot="a")
    assert catch_up.source == "catch-up" and catch_up.delivered == ["p2"]


async def test_a_lease_that_expires_is_a_backend_failure_and_two_in_a_row_double_the_backoff():
    params = replace(FAST, lease_seconds=5, failure_backoff_seconds=20)
    sb = Sandbox(params=params, script=Script().on(Behaviour(replies=[], skip_done=True), bot="a"))
    await sb.human("@a hello")
    await sb.run(5)
    end = sb.trace.last("turn_end", bot="a")
    assert end.outcome == "backend_failure" and end.error == "lease expired"
    assert sb.trace.last("backoff", bot="a").until == 25.0 and sb.gw.status()["bots"]["a"]["cursor"] is None
    await sb.run(25)  # the retry at 25 expires at 30: the backoff doubles
    assert sb.trace.last("backoff", bot="a").until == 70.0
    assert sb.trace.count("notice", bot="a", cause="backend failure") == 1


async def test_a_done_for_a_wake_with_no_live_lease_moves_nothing():
    sb = Sandbox()
    await sb.human("@a hello")
    await sb.run(1)  # the turn ended normally
    wake = sb.bot.turns[-1].wake
    assert await sb.gateway.done("a", wake.wake_id, failed="too late") is False
    assert sb.trace.count("done_stale", bot="a") == 1 and sb.trace.count("backoff") == 0
    assert sb.gw.status()["bots"]["a"]["cursor"] == 1
    with pytest.raises(NoLiveLease):
        sb.gateway.read("a", wake.wake_id)


async def test_a_done_failed_leaves_the_cursor_alone_and_the_posts_are_delivered_again():
    sb = Sandbox(script=Script().on(Behaviour(failed="model down"), bot="a", once=True).on(reply(["H"], "ok"), bot="a"))
    await sb.human("@a hello")
    await sb.run(1)
    assert sb.trace.last("turn_end", bot="a").outcome == "backend_failure" and sb.trace.count("cursor_saved") == 0
    await sb.run(25)
    assert [e.delivered for e in sb.turns("a")] == [["p1"], ["p1"]]
    assert sb.trace.last("turn_end", bot="a").outcome == "normal"


# ── the read record ─────────────────────────────────────────────────────────


async def test_a_done_without_a_read_is_a_failed_turn():
    sb = Sandbox()
    sb.bot.skip_read = lambda wake: True
    await sb.human("@a hello")
    await sb.run(1)
    end = sb.trace.last("turn_end", bot="a")
    assert end.outcome == "backend_failure" and end.error == "no read"
    assert sb.gw.status()["bots"]["a"]["cursor"] is None


async def test_a_wake_with_nothing_to_read_records_an_empty_complete_read():
    """An unfinished-thread wake delivers nothing new: a client that does not read before
    ``done`` has skipped nothing (design §3.3)."""
    sb = Sandbox(params=replace(FAST, control_ttl_days=1 / 24, catch_up_idle_seconds=0))
    sb.bot.skip_read = lambda wake: wake.source == "unfinished"
    await sb.human("mute: @a")
    await sb.human("news in main")
    await sb.human("@a hello", "t2")
    await sb.run(2)
    assert sb.gw.status()["bots"]["a"]["unfinished"] == ["main"]
    await sb.run(3700)  # the mute expires with its TTL: main opens with no new post behind the cursor
    unfinished = sb.trace.last("turn_start", bot="a", source="unfinished")
    assert unfinished.delivered == [] and sb.trace.last("turn_end", bot="a").outcome == "normal"
    assert sb.gw.status()["bots"]["a"]["unfinished"] == []


# ── the ledger at done ───────────────────────────────────────────────────────


async def test_a_send_without_the_wake_id_covers_nothing_and_done_answered_closes_it_next_turn():
    seen = []

    def handler(turn):
        seen.append(turn)
        if len(seen) == 1:  # the reply is posted but unattributed: the turn fails at done
            return Behaviour(replies=[{"to": ["H"], "text": "answered, forgot the wake", "unsolicited": True}])
        return replies(item(thread="main", done=True))  # the reply is already in the room: close it

    sb = Sandbox(script=Script().on(handler, bot="a"))
    await sb.human("@a hello")
    await sb.run(1)
    assert sb.trace.last("turn_end", bot="a").outcome == "invalid" and sb.trace.count("send", bot="a", wake=None) == 1
    assert len(sb.posts_by("a")) == 1 and sb.gw.status()["bots"]["a"]["cursor"] is None
    await sb.run(1)  # the sweep's retry delivers through its mark: a's own post and the notice are behind the cursor now
    assert sb.trace.last("turn_end", bot="a").outcome == "normal"
    assert sb.gw.status()["bots"]["a"]["cursor"] == sb.room.last_position() == 3
    assert len(sb.posts_by("a")) == 1


async def test_a_send_outside_the_hint_is_counted_never_refused():
    sb = Sandbox(script=Script().on(replies(item(thread="elsewhere", to=["H"], text="aside"), item(done=True)), bot="a"))
    await sb.human("@a hello")
    await sb.run(1)
    assert len(sb.posts_by("a", "elsewhere")) == 1 and sb.trace.last("turn_end", bot="a").outcome == "normal"
    counts = sb.gw.status()["rules"]["hinted_thread"]
    assert (counts["opportunities"], counts["violated"], counts["rejected"]) == (1, 1, 0)


# ── unsolicited sends (design §3.5) ─────────────────────────────────────────


async def test_an_unsolicited_send_posts_at_once_exempt_from_rate_and_cooldown_and_counts_in_the_window():
    sb = Sandbox(params=replace(FAST, rate_limit_posts=1, cooldown_seconds=100),
                 script=Script().on(reply(["H"], "reply"), bot="a"))
    await sb.human("@a go")
    await sb.run(2)  # the window is full and a is in cooldown
    doorbell = InProcessDoorbell(sb.gateway, "a")
    post = await doorbell.send(Reply("main", to=["H"], text="scheduled report"), room="team")
    assert post is not None and post.text == "@H scheduled report" and sb.room.posts[-1] is post
    assert sb.gw.status()["threads"]["main"]["recent_posts"] == 2  # it counts, so the other bots wait
    assert sb.trace.last("send", bot="a").wake is None


async def test_an_unsolicited_send_in_a_muted_thread_is_refused_with_the_reason_in_enforce_mode():
    sb = Sandbox()
    await sb.human("mute: @a")
    doorbell = InProcessDoorbell(sb.gateway, "a")
    with pytest.raises(RefusedSend, match="mute@main"):
        await doorbell.send(Reply("main", to=["H"], text="report"), room="team")
    assert sb.posts_by("a") == [] and sb.gw.status()["rules"]["mute"]["rejected"] == 1
    off = Sandbox(params=replace(FAST, enforce=False))
    await off.human("mute: @a")
    await InProcessDoorbell(off.gateway, "a").send(Reply("main", to=["H"], text="report"), room="team")
    assert len(off.posts_by("a")) == 1 and off.gw.status()["rules"]["mute"]["violated"] == 1


async def test_a_send_with_nothing_postable_is_a_usage_error_and_posts_nothing():
    sb = Sandbox()
    with pytest.raises(InvalidReply):
        await InProcessDoorbell(sb.gateway, "a").send(Reply("main"), room="team")
    assert sb.room.posts == [] and sb.gw.status()["rules"] == {}


async def test_a_send_naming_a_dead_wake_lands_unsolicited():
    sb = Sandbox()
    await sb.human("@a hello")
    await sb.run(1)
    wake = sb.bot.turns[-1].wake
    post = await InProcessDoorbell(sb.gateway, "a").send(Reply("main", to=["H"], text="late", wake_id=wake.wake_id))
    assert post is not None and sb.trace.last("send", bot="a").wake is None  # the id alone named the room
    with pytest.raises(NoLiveLease):
        await InProcessDoorbell(sb.gateway, "a").send(Reply("main", to=["H"], text="x", wake_id="w-nowhere-1"))


# ── pause ───────────────────────────────────────────────────────────────────


async def test_a_paused_bot_gets_no_wake_of_any_source_and_resume_delivers_the_backlog():
    sb = Sandbox(params=replace(FAST, catch_up_idle_seconds=10), bots=("a", "b"))
    sb.gw.pause("a")
    await sb.human("@a hello")
    await sb.post("b", "@a and me")
    await sb.run(30)
    assert sb.trace.count("turn_start", bot="a") == 0 and sb.trace.last("turn_held", bot="a").gate == "paused"
    assert sb.gw.status()["bots"]["a"]["paused"] and sb.gw.status()["bots"]["a"]["cursor"] is None
    sb.gw.resume("a")
    await sb.run(0)
    start = sb.trace.last("turn_start", bot="a")
    assert start.source == "addressed" and start.delivered == ["p1", "p2"]


# ── the gateway over its rooms ──────────────────────────────────────────────


async def test_poll_is_the_liveness_signal_and_the_bio_reaches_the_classifier():
    sb = Sandbox()
    assert sb.gateway.agents["a"].last_seen is None
    await sb.run(3)
    assert sb.gateway.poll("a") is None and sb.gateway.agents["a"].last_seen == 3.0
    doorbell = InProcessDoorbell(sb.gateway, "a")
    assert await doorbell.bio("I run the deploys") == 1 and await doorbell.bio("I run the deploys and the on-call") == 2
    await sb.human("who handles deploys?")
    await sb.run(0)
    agent = next(m for m in sb.classifier.calls[-1].agents if m.username == "a")
    assert agent.profile == "I run the deploys and the on-call"
    assert sb.gateway.status()["agents"]["a"] == {"bio_version": 2, "last_seen": 3.0}


# ── what the internal review found (PR #208) ────────────────────────────────


async def test_a_reply_the_platform_refused_still_covers_its_thread():
    """The agent gave its reply; the platform refused it. That is not the agent's rule
    violation: the turn ends normally, the cursor moves, and the next hint names the
    refusal (5.6, 5.9; the simulator's ``_commit`` counted a refused item as answered)."""
    for enforce in (True, False):
        sb = Sandbox(params=replace(FAST, enforce=enforce), script=Script().on(reply(["H"], "hi"), bot="a"))
        await sb.human("@a hello")
        sb.room.refusing.add("main")
        await sb.run(2)
        assert sb.trace.last("turn_end", bot="a").outcome == "normal" and sb.gw.status()["bots"]["a"]["cursor"] == 1
        assert sb.trace.count("platform_refused", bot="a") == 1 and "must_reply" not in {
            m for m, c in sb.gw.status()["rules"].items() if c["rejected"] or c["violated"]}
        await sb.human("@a again")
        await sb.run(2)
        assert sb.bot.turns[-1].hint.refused_last_turn == ("main",)


async def test_the_sends_of_a_turn_that_ends_in_a_backend_failure_are_counted():
    """An agent that sends and then never calls ``done`` (the utility reports the failure,
    design §3.4): what it posted is in the room and in the table, both modes."""
    for enforce, column in ((True, "rejected"), (False, "violated")):
        sb = Sandbox(params=replace(FAST, enforce=enforce, lease_seconds=5),
                     script=Script().on(Behaviour(replies=[{"thread": "main", "to": ["H"], "text": "ok"},
                                                           {"thread": "t2", "to": ["H"], "text": "sneak"}],
                                                  skip_done=True), bot="a"))
        await sb.human("mute: @a", "t2")
        await sb.human("fyi", "t2")
        await sb.human("@a go")
        await sb.run(6)
        assert sb.trace.last("turn_end", bot="a").outcome == "backend_failure"
        assert sb.gw.status()["rules"]["mute"][column] == 1


async def test_a_muted_thread_outside_the_hint_is_counted_under_mute_not_only_hinted_thread():
    """A thread the bot closed with ``--answered`` in an earlier turn is in no later hint;
    a reply there while it is muted is an off-hint send in a muted thread: both counters."""
    script = (Script()
              .on(replies(item(done=True), item(thread="elsewhere", done=True)), bot="a", once=True)
              .on(replies(item(done=True), item(thread="elsewhere", to=["H"], text="aside")), bot="a"))
    off = Sandbox(params=replace(FAST, enforce=False), script=script)
    await off.human("mute: @a", "elsewhere")
    await off.human("@a hello")  # delivers elsewhere closed; the turn closes it with --answered
    await off.run(2)
    assert off.gw.status()["bots"]["a"]["unfinished"] == []
    await off.human("@a again")  # elsewhere is in no hint now
    await off.run(2)
    assert off.bot.turns[-1].hint.group_of("elsewhere") is None
    assert off.gw.status()["rules"]["mute"]["violated"] == 1 and off.gw.status()["rules"]["hinted_thread"]["violated"] == 1


async def test_an_unfinished_thread_in_an_empty_room_wakes_without_crashing_the_periodic_sweep():
    sb = Sandbox()
    sb.gw.persisted["a"].unfinished.add("t9")  # restored state; the platform's history is empty
    await sb.run(25)
    start = sb.trace.last("turn_start", bot="a", source="unfinished")
    assert start is not None and start.delivered == [] and sb.trace.last("turn_end", bot="a").outcome == "normal"
    assert sb.trace.count("sweep", bot="a", reason="periodic") == 2  # the timer kept going


async def test_pause_withdraws_a_wake_not_yet_polled_and_refuses_unsolicited_sends():
    sb = Sandbox()
    sb.gateway.agents["a"].listeners.clear()
    await sb.human("@a hello")
    assert sb.gw.status()["bots"]["a"]["wake"]["leased"] is False
    sb.gw.pause("a")
    assert sb.gw.status()["bots"]["a"]["wake"] is None and sb.trace.last("turn_end", bot="a").outcome == "withdrawn"
    assert sb.gateway.poll("a") is None
    with pytest.raises(RefusedSend, match="paused"):
        await InProcessDoorbell(sb.gateway, "a").send(Reply("main", to=["H"], text="report"), room="team")
    sb.gw.resume("a")
    await sb.run(0)
    assert sb.trace.last("turn_start", bot="a").delivered == ["p1"]


async def test_a_lease_of_zero_never_expires():
    sb = Sandbox(params=replace(FAST, lease_seconds=0), duration=500)
    await sb.human("@a hello")
    await sb.run(0)
    assert sb.bot.turns[-1].wake.lease_expires_at is None
    await sb.run(600)
    assert sb.trace.last("turn_end", bot="a").outcome == "normal"


async def test_the_end_token_in_a_reply_body_is_a_usage_error():
    sb = Sandbox()
    with pytest.raises(InvalidReply, match="call done instead"):
        await InProcessDoorbell(sb.gateway, "a").send(Reply("main", to=["H"], text="ok <end-of-agent-chain>"), room="team")


async def test_unsolicited_sends_are_counted_apart_and_read_room_shows_the_backlog():
    sb = Sandbox()
    await sb.human("news")
    doorbell = InProcessDoorbell(sb.gateway, "a")
    await doorbell.send(Reply("main", to=["H"], text="report"), room="team")
    assert sb.gw.status()["bots"]["a"]["unsolicited_sends"] == 1
    assert [p.id for p in await doorbell.read_room("team")] == ["p1", "p2"]  # its own post included, cursor unmoved
    assert sb.gw.status()["bots"]["a"]["cursor"] is None


async def test_a_hosted_bot_that_left_the_room_is_still_woken_but_loudly(caplog):
    """Membership is the roster's concern (a later increment); until then the core says
    so in status and in the log rather than deciding by itself (spec 5.14)."""
    sb = Sandbox(bots=("a", "b"))
    del sb.room._members["a"]
    assert sb.gw.status()["hosted_not_members"] == ["a"]
    with caplog.at_level("WARNING", logger="coop.room.core"):
        await sb.human("@all anyone?")
    assert sb.trace.count("turn_start", bot="a") == 1
    assert any("a is hosted but not a member" in r.message for r in caplog.records)


async def test_the_trigger_role_follows_the_configured_roster_not_the_platform():
    sb = Sandbox(bots=("a", "b"), extra_members=(Member("zed", "bot"),))
    await sb.post("zed", "@a do this")
    await sb.run(0)
    assert sb.bot.turns[-1].wake.trigger.author_role == "guest"  # a bot the configuration does not list
    await sb.run(1)
    await sb.post("b", "@a and this")
    await sb.run(0)
    assert sb.bot.turns[-1].wake.trigger.author_role == "agent"

# ── own posts are context, never cause (design 5.6 row) ─────────────────────


async def test_read_returns_the_bots_own_posts_in_the_range_once_and_the_gateway_decides_from_the_others():
    sb = Sandbox(bots=("a", "b"), script=Script().on(reply(["H"], "x"), bot="a"))
    doorbell = InProcessDoorbell(sb.gateway, "a")
    await sb.human("@a one")  # p1
    await sb.run(2)  # a answered with p2, done
    await doorbell.send(Reply("t1", to=["H"], text="scheduled report"), room="team")  # p3, unsolicited
    await sb.run(100)  # sweeps and the catch-up window pass
    assert len(sb.turns("a")) == 1  # its own report woke nobody, and was no catch-up candidate
    await sb.human("@a two")  # p4
    await sb.run(0)
    wake = sb.trace.last("turn_start", bot="a")
    assert wake.delivered == ["p4"]  # the decision list: the posts of others
    _, posts = sb.gateway.read("a", wake.wake)
    assert [p.id for p in posts] == ["p2", "p3", "p4"]  # its last reply, its report, the trigger
    assert sb.trace.last("read", bot="a").own == ["p2", "p3"]
    await sb.run(2)  # a answered with p5, done: the cursor is past p4
    await sb.human("@a three")  # p6
    await sb.run(0)
    _, posts = sb.gateway.read("a", sb.trace.last("turn_start", bot="a").wake)
    assert [p.id for p in posts] == ["p5", "p6"]  # own posts are delivered once, like any other


async def test_in_a_dm_the_bots_own_unsolicited_post_does_not_wake_it():
    """Every post in a DM addresses its bot; its own must not, or it would answer itself
    until the rate limit stopped it."""
    sb = Sandbox(bots=("a",), is_dm=True, script=Script().on(reply(["H"], "x"), bot="a"))
    await sb.human("hi")  # p1
    await sb.run(2)  # p2
    await InProcessDoorbell(sb.gateway, "a").send(Reply("main", to=["H"], text="report"), room="team")  # p3
    await sb.run(200)
    assert len(sb.turns("a")) == 1 and [p.id for p in sb.posts_by("a")] == ["p2", "p3"]
    await sb.human("again")  # p4
    await sb.run(0)
    _, posts = sb.gateway.read("a", sb.trace.last("turn_start", bot="a").wake)
    assert [p.id for p in posts] == ["p2", "p3", "p4"]


async def test_an_own_post_alone_in_a_rate_closed_thread_makes_it_neither_unfinished_nor_must_reply():
    sb = Sandbox(bots=("a", "b"), script=Script().on(reply(["H"], "x"), bot="a"))
    await sb.human("limit: 1 posts\ntopic for later", "t1")  # p1
    await sb.human("@a hi")  # p2: a's turn delivers t1's post; its cursor moves past it
    await sb.run(5)  # p3
    await InProcessDoorbell(sb.gateway, "a").send(Reply("t1", to=["H"], text="report"), room="team")  # p4 fills t1's window
    await sb.human("@a hello")  # p5, while t1 is closed to a by rate
    await sb.run(0)
    run = sb.gw.volatile["a"].turn
    assert [g.thread for g in run.wake.hint.threads] == ["main"]  # t1 is not in the hint: only a's own post is new there
    assert [p.id for p in run.payload] == ["p3", "p4", "p5"]
    await sb.run(400)  # the window passes
    assert not [e for e in sb.trace.of("unfinished_added") if e.bot == "a"]
    assert all(e.thread == "main" for e in sb.turns("a"))  # never woken to answer its own report
