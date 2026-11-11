"""Section 8.3, "Delivery and envelope": cursor, failures, backoff, degraded state,
the failure notice, several threads in one turn, settings persistence."""

from __future__ import annotations

from dataclasses import replace

from tests.roomsim.helpers import FAST, Sandbox, Script, item, replies, reply
from tools.roomsim.bots import fail


async def test_cursor_saved_after_normal_end_and_after_partial_refusal():
    sb = Sandbox(script=Script().on(replies(item(to=["H"], text="t2 reply", thread="t2"),
                                            item(to=["H"], text="main reply")), bot="a"))
    await sb.human("context in main, nobody addressed")
    await sb.human("@a hello", thread="t2")  # delivers both threads in one turn
    sb.room.refusing.add("t2")  # the platform refuses a's reply there
    await sb.run(5)
    assert sb.trace.count("refused", bot="a", thread="t2") == 1
    assert sb.trace.last("cursor_saved", bot="a").seq == 2
    assert sb.trace.last("turn_end", bot="a").outcome == "normal"
    # the refusal is reported in the next hint
    await sb.human("@a again")
    await sb.run(2)
    assert sb.bot.turns[-1].hint.refused_last_turn == ("t2",)


async def test_cursor_not_saved_after_backend_failure_or_second_invalid():
    sb = Sandbox(script=Script().on(fail(), bot="a").on("garbage", bot="b"))
    await sb.human("@a @b go")
    await sb.run(5)
    assert sb.trace.count("cursor_saved") == 0
    assert sb.trace.last("turn_end", bot="a").outcome == "backend_failure"
    assert sb.trace.last("turn_end", bot="b").outcome == "invalid"
    # Spec as written: an invalid-envelope failure has no backoff, so the turn-end sweep
    # retries the mention at once; two failed turns reach consecutive_invalid_limit=2.
    assert sb.trace.count("invalid", bot="b") == 4
    assert sb.trace.count("degraded", bot="b") == 1


async def test_failed_turn_is_delivered_again_after_backoff_with_no_cap():
    script = Script().on(fail(), bot="a", once=True).on(fail(), bot="a", once=True)
    sb = Sandbox(script=script)
    await sb.human("@a go")
    await sb.run(200)
    starts = sb.turns("a")
    assert len(starts) == 3 and all(e.delivered == [1] for e in starts)
    assert sb.trace.last("turn_end", bot="a").outcome == "normal"
    # the same input delivered three times: the duplicate-reply risk is visible
    assert [e.time for e in starts] == [0.0, 21.0, 62.0]


async def test_backoff_doubles_to_the_cap_human_mention_still_starts_and_normal_end_clears():
    sb = Sandbox(params=replace(FAST, failure_backoff_seconds=20, failure_backoff_max_seconds=50),
                 script=Script().on(fail(), bot="a", once=True).on(fail(), bot="a", once=True)
                 .on(fail(), bot="a", once=True))
    await sb.human("@a go")
    await sb.run(1)
    assert sb.trace.last("backoff", bot="a").until == 21.0
    await sb.run(21)  # retry at 21 fails again → 40s
    assert sb.trace.last("backoff", bot="a").until == 62.0
    await sb.human("@a are you there?")  # human mention starts a turn inside the backoff
    assert sb.trace.last("turn_start", bot="a").time == 22.0
    await sb.run(1)
    assert sb.trace.last("backoff", bot="a").until == 23.0 + 50  # capped, not 80
    assert sb.trace.count("notice", bot="a", cause="backend failure") == 1  # one per episode
    await sb.run(100)
    assert sb.trace.last("turn_end", bot="a").outcome == "normal"
    assert sb.gw.status()["bots"]["a"]["retry_after"] == 0.0


async def test_job_due_during_backoff_waits_and_is_not_a_retry():
    sb = Sandbox(script=Script()
                 .on({"replies": [{"done": True}], "schedule": [{"in_seconds": 5, "text": "check"}]},
                     bot="a", source="addressed", once=True)
                 .on(fail(), bot="a", once=True))
    await sb.human("@a schedule it")
    await sb.run(2)
    assert sb.trace.count("job_scheduled", bot="a") == 1
    await sb.human("@a fail now")
    await sb.run(1)  # backoff until 24
    await sb.run(5)  # job due at 6, inside the backoff
    assert sb.trace.last("job_waiting", bot="a").gate == "backoff"
    assert sb.trace.count("job_retry") == 0
    await sb.run(30)
    assert sb.trace.count("turn_start", bot="a", source="job") == 1


async def test_degraded_after_the_limit_probed_by_human_mention_cleared_by_normal_end():
    script = Script().on("garbage", bot="a", once=True).on("garbage", bot="a", once=True) \
        .on("garbage", bot="a", once=True).on("garbage", bot="a", once=True)
    sb = Sandbox(params=replace(FAST, consecutive_invalid_limit=2), script=script)
    await sb.human("@a one")
    await sb.run(5)  # turn 1 twice invalid, re-swept immediately: turn 2 twice invalid → degraded
    assert sb.trace.count("degraded", bot="a") == 1
    assert sb.trace.count("notice", bot="a", cause="invalid reply") == 1
    assert sb.trace.count("notice", bot="a", cause="degraded") == 1
    assert sb.gw.status()["bots"]["a"]["degraded"]
    # muted column: a bot mention starts nothing, the sweeper skips it
    await sb.sim.post("b", "@a what do you think?")
    await sb.run(15)
    assert sb.trace.last("turn_held", bot="a").gate == "degraded"
    starts_before = len(sb.turns("a"))
    await sb.human("@a are you ok?")  # the probe
    await sb.run(3)
    assert len(sb.turns("a")) == starts_before + 1
    assert sb.trace.last("turn_end", bot="a").outcome == "normal"
    assert not sb.gw.status()["bots"]["a"]["degraded"]
    assert sb.gw.status()["bots"]["a"]["invalid_streak"] == 0


async def test_backend_failures_do_not_change_the_invalid_count():
    script = Script().on("garbage", bot="a", once=True).on("garbage", bot="a", once=True) \
        .on(fail(), bot="a", once=True)
    sb = Sandbox(params=replace(FAST, consecutive_invalid_limit=3), script=script)
    await sb.human("@a go")
    await sb.run(5)
    assert sb.gw.status()["bots"]["a"]["invalid_streak"] == 1
    assert sb.trace.last("turn_end", bot="a").outcome == "backend_failure"


async def test_limit_zero_counts_the_streak_but_degrades_nobody():
    sb = Sandbox(params=replace(FAST, consecutive_invalid_limit=0), script=Script().on("garbage", bot="a"))
    await sb.human("@a go")
    await sb.run(12)
    assert sb.gw.status()["bots"]["a"]["invalid_streak"] >= 3
    assert sb.trace.count("degraded") == 0
    assert sb.trace.count("notice", bot="a") == 1  # first twice-invalid turn of the streak only


async def test_failure_notice_identity():
    """Starts no turn, no classifier call, counts for catch-up, not for the rate limit,
    not delivered to its own bot."""
    sb = Sandbox(params=replace(FAST, catch_up_post_threshold=1, catch_up_age_seconds=0),
                 script=Script().on(fail(), bot="a", once=True))
    await sb.human("@a go")
    await sb.run(1)
    notice = sb.trace.last("notice", bot="a")
    assert notice is not None
    assert sb.trace.count("classifier_call", post=notice.seq) == 0
    assert sb.trace.count("turn_start", thread="main", source="addressed") == 1
    assert sb.gw.status()["threads"]["main"]["recent_posts"] == 0
    await sb.run(10)  # periodic sweep: b and c catch up on the notice; a does not see it
    catch_ups = sb.trace.of("turn_start", source="catch-up")
    assert {e.bot for e in catch_ups} == {"b", "c"}
    assert all(notice.seq in e.delivered for e in catch_ups)
    assert all(notice.seq not in e.delivered for e in sb.turns("a"))


async def test_one_turn_posts_into_two_threads_counted_each_in_its_own():
    sb = Sandbox(script=Script().on(replies(item(to=["H"], text="answer in B", thread="B"),
                                            item(to=["b"], text="ack in A", thread="A")), bot="x"),
                 bots=("x", "b"), hosted=["x"], agents=["x", "b"])  # b is on another gateway
    await sb.sim.post("b", "x's figure is 12, not 21", "A")  # a bare name is not an address
    await sb.run(0.5)
    await sb.human("@x what is the figure?", "B")
    await sb.run(3)
    status = sb.gw.status()["threads"]
    assert status["A"]["recent_posts"] == 2 and status["B"]["recent_posts"] == 1
    assert len(sb.posts_by("x", "A")) == 1 and len(sb.posts_by("x", "B")) == 1


async def test_enforce_closed_threads_and_canonical_to():
    sb = Sandbox(script=Script().on(reply(["H", "b"], "@c says hi @b"), bot="a"), bots=("a", "b", "c"))
    await sb.human("@a go")
    await sb.run(2)
    post = sb.posts_by("a")[0]
    assert post.mentions == {"H", "b"} and post.text == "@H c says hi @b"


async def test_done_with_to_posts_nothing_and_control_without_to_is_accepted():
    sb = Sandbox(script=Script().on(replies(item(control={"limit": {"posts": 2}}), item(done=True, to=["H"])), bot="chair"),
                 bots=("chair", "a"))
    await sb.human("orchestrator: @chair\n@chair set a limit")
    await sb.run(2)
    assert len(sb.posts_by("chair")) == 1 and sb.posts_by("chair")[0].text == "limit: 2 posts"


async def test_control_plus_text_renders_control_lines_first():
    sb = Sandbox(script=Script().on(reply(["a"], "one post each", control={"mute": ["a"]}), bot="chair"),
                 bots=("chair", "a"))
    await sb.human("orchestrator: @chair\n@chair start")
    await sb.run(2)
    assert sb.posts_by("chair")[0].text == "mute: @a\n@a one post each"
    assert sb.gw.status()["threads"]["main"]["muted"] == ["a"]


async def test_settings_persist_expire_and_reject_replayed_older_controls():
    sb = Sandbox(params=replace(FAST, control_ttl_days=1 / 24))
    await sb.human("mute: @a")
    await sb.human("unmute: @a")
    assert sb.gw.status()["threads"]["main"]["muted"] == []
    # replaying the older mute (same seq) must not restore it
    await sb.gw.on_post(sb.room.by_seq(1))
    assert sb.gw.status()["threads"]["main"]["muted"] == []
    await sb.human("mute: @a")
    assert sb.gw.status()["threads"]["main"]["muted"] == ["a"]
    await sb.run(3700)  # the thread goes silent past the TTL
    assert sb.gw.status()["threads"]["main"]["muted"] == []


async def test_ttl_expiry_keeps_unfinished_state_and_archive_removes_both():
    sb = Sandbox(params=replace(FAST, control_ttl_days=1 / 24, catch_up_post_threshold=1,
                                catch_up_age_seconds=0))
    await sb.human("mute: @a")
    await sb.human("question for the room", "main")  # a is muted: the sweeper skips it here
    await sb.human("@a hello over here", "t2")  # ...so a turn elsewhere delivers main as closed
    await sb.run(3)
    assert sb.trace.count("unfinished_added", bot="a", thread="main") == 1
    await sb.run(3700)  # TTL: mute gone, the thread opens, the sweeper starts the owed turn
    assert sb.trace.count("turn_start", bot="a", source="unfinished") == 1
    assert sb.trace.count("unfinished_removed", bot="a", thread="main") == 1
    # archive removes unfinished state outright
    await sb.human("mute: @b", "t2")
    await sb.human("question in t2", "t2")
    await sb.human("@b hello in main")  # delivers t2 as closed context to b
    await sb.run(3)
    assert "t2" in sb.gw.status()["bots"]["b"]["unfinished"]
    await sb.sim.archive("t2")
    assert sb.gw.status()["bots"]["b"]["unfinished"] == []


async def test_bot_authored_all_resolves_before_post_and_human_all_on_read():
    sb = Sandbox(script=Script().on(replies(item(control={"mute": "all"}), item(done=True)), bot="chair"),
                 bots=("chair", "a", "b"))
    await sb.human("orchestrator: @chair\n@chair mute everyone")
    await sb.run(2)
    assert sb.posts_by("chair")[0].text == "mute: @a @b"
    await sb.human("unmute: all")
    assert sb.gw.status()["threads"]["main"]["muted"] == []
    assert sb.trace.last("control_applied", control="unmute").value == ["a", "b"]


async def test_bot_joining_after_mute_all_is_not_muted_and_offline_gateway_may_speak_once():
    sb = Sandbox(bots=("a", "b"), hosted=["a"], agents=["a", "b", "c"])
    g2 = sb.add_gateway("g2", ["b"], ["a", "b", "c"])
    g3 = sb.add_gateway("g3", ["c"], ["a", "b", "c"])
    from tools.roomsim.model import Member
    g3.set_offline()
    await sb.human("mute: all")
    sb.room.join(Member("c", "bot"))
    await g3.set_online(replay_after=1)  # the mute is outside the replay window
    assert sb.trace.last("control_applied", control="mute").age == 0  # a replayed control would show its age
    assert sb.gw.status()["threads"]["main"]["muted"] == ["a", "b"]
    assert g3.status()["threads"].get("main", {}).get("muted", []) == []
    await sb.sim.post("a", "@c your view?")
    await sb.run(2)
    assert len(sb.trace.of("turn_start", bot="c")) == 1  # c spoke once; g2 did not start b
    assert g2.status()["threads"]["main"]["muted"] == ["a", "b"]


async def test_mute_all_resolving_to_nobody_is_omitted_and_nothing_remains_means_done():
    sb = Sandbox(script=Script().on(replies(item(control={"mute": "all"})), bot="chair"), bots=("chair",))
    await sb.human("orchestrator: @chair\n@chair go")
    await sb.run(2)
    assert sb.posts_by("chair") == []
    assert sb.trace.last("turn_end", bot="chair").outcome == "normal"


async def test_human_mute_of_a_non_bot_is_ignored_and_shown_in_status():
    sb = Sandbox()
    await sb.human("mute: @H @zed @a")
    assert sb.gw.status()["threads"]["main"]["muted"] == ["a"]
    assert sb.gw.status()["threads"]["main"]["unknown_mutes"] == ["H", "zed"]


async def test_orchestrator_control_in_text_applies_once_visible():
    sb = Sandbox(script=Script().on(reply(["a"], "mute: @a\none post each"), bot="chair"), bots=("chair", "a"))
    await sb.human("orchestrator: @chair\n@chair run it")
    await sb.run(2)
    assert sb.trace.count("control_applied", control="mute", by="chair") == 1
    assert sb.gw.status()["threads"]["main"]["muted"] == ["a"]


async def test_gateway_counts_its_own_successful_post():
    sb = Sandbox(script=Script().on(reply(["H"], "four words right here"), bot="a"))
    await sb.human("@a go")
    await sb.run(2)
    assert sb.gw.status()["threads"]["main"] == {
        "orchestrator": None, "muted": [], "limits": (4, 400), "recent_posts": 1, "recent_words": 5,
        "unknown_mutes": []}
