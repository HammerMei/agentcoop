"""E2E Test: Mattermost delivery tracks membership, not readability (§6.2).

The runtime leans on this. `MattermostConnector.supports_unsolicited_inbound()`
returns True on the grounds that one socket carries every channel the bot
belongs to "and only those", so the creation path performs **no REST
membership check** — a channel that produces an event is by definition one the
bot is in. If Mattermost ever delivered events for merely-readable channels,
AgentCoop would silently start answering in rooms nobody invited it to.

`scripts/probe_a2_mm.py` already verifies this at the PLATFORM level, and §6.2
records the result. This test is not a copy of that: it verifies the same
property **through the whole AgentCoop runtime** — a rule that matches the channel,
a poster on the allow-list, a bot that is mentioned — and the probe cannot,
because the probe does not run AgentCoop.

Three things make it a test rather than a sleep-and-hope:

1. **The rule claims the outside channel.** `acg-e2e-mm-*` matches it. So the
   rule declining is not on the list of explanations for silence.
2. **The poster is the allow-listed human, in both channels.** Posting as the
   admin would have been easier and would have quietly broken the test:
   `mmadmin` is not in `allowed_users.owners`, so the sender filter would
   explain the silence just as well as the missing event.
3. **A round trip in the member channel is the liveness control**, posted
   *after* the outside post. Waiting for that reply proves AgentCoop was alive and
   consuming events after the outside message — which is what makes this a
   causal bound instead of a magic sleep. A pure negative assertion would pass
   just as happily against a dead gateway.

There is deliberately **no MM warm-up fixture**, and this test absorbs the
Claude cold start inside its own 120s wait. Collection order puts this file
before `test_mm_ping_pong.py`, so it pays that cost rather than avoiding it.
That is a decision, not an oversight. `_warmup_agents` warms both runtimes,
but the one it exists for is OpenCode, which initialises its subprocess lazily
and can take 60–90s; Claude's cold start is a fraction of that and fits inside
the 120s wait with room. Both MM rules deliberately point at Claude, so this
suite never waits on the slow one. If a future MM rule uses OpenCode, the
warm-up ping belongs in the `mm_connected` fixture — not in `_warmup_agents`,
which would couple the Rocket.Chat path to Mattermost seeding.
"""
from __future__ import annotations

import time
from typing import Any

import pytest
from acg_container import MM_CONNECTOR_NAME, watcher_list
from mm_client import MMClient


@pytest.mark.e2e
def test_a_post_in_a_readable_unjoined_channel_produces_nothing(
    mm_connected: None,
    mm_setup: dict[str, Any],
    mm_test_client: MMClient,
    mm_admin_client: MMClient,
    mm_bot_client: MMClient,
) -> None:
    outside_id = mm_setup["outside_channel_id"]
    member_id = mm_setup["member_channel_id"]
    bot_id = mm_setup["bot_user_id"]

    # ── Preconditions, asserted rather than assumed ───────────────────────────
    # Without these, "no reply" is indistinguishable from "no access", and the
    # test would keep passing after someone joins the bot to the channel or
    # makes it private.
    assert not mm_admin_client.is_channel_member(outside_id, bot_id), (
        f"the bot is a MEMBER of #{mm_setup['outside_channel']} — this test "
        "requires it to be readable but unjoined; re-run mm_setup.py, which "
        "removes it"
    )
    assert mm_admin_client.is_channel_member(member_id, bot_id), (
        f"the bot is not in #{mm_setup['member_channel']}, so the liveness "
        "control below cannot work"
    )
    # No stale watcher for the outside channel BEFORE anything is posted. This
    # is a precondition, not a duplicate of the assertion at the end: without
    # it, a record left behind by an interrupted `make e2e-probe-mm` — which
    # drives the same bot account and joins it to this channel — surfaces at
    # the end of the test as "a watcher materialized for a channel the bot
    # never joined", blaming this run's delivery for last run's residue, and
    # the claim would be false as well as misdirected.
    outside_handle = f"{MM_CONNECTOR_NAME}:{mm_setup['outside_channel']}"
    assert outside_handle not in watcher_list(), (
        f"a watcher for {outside_handle!r} already exists before this test "
        "posted anything — most likely an interrupted 'make e2e-probe-mm' left "
        f"""it behind. Clear it inside the container:
    make e2e-acg C="expire '{outside_handle}'\""""
    )

    before_ts = int(time.time() * 1000)

    # ── 1. The message that must vanish ───────────────────────────────────────
    # `bait` doubles as the positive control for the read at step 3. §6.2
    # insists readability be established with the BOT's own token, and the
    # obvious way to write that — `assert isinstance(get_posts(...), list)` —
    # cannot fail: `get_posts` raises on a bad status and otherwise always
    # returns a list, so its carefully worded message was unreachable. Instead
    # this post, made by the human after `before_ts` in the channel under test,
    # MUST come back from the very query the negative assertion depends on.
    # That validates the channel id, the bot token's view of this channel, and
    # the `since - 1` boundary arithmetic in one shot — the three ways the
    # negative could otherwise pass while the bot really had posted.
    bait = mm_test_client.post_message(
        outside_id,
        f"@{mm_setup['bot_username']} respond with exactly the single word 'leak'",
    )

    # ── 2. The liveness control, posted afterwards ────────────────────────────
    mm_test_client.post_message(
        member_id,
        f"@{mm_setup['bot_username']} respond with exactly the single word 'pong'",
    )
    reply = mm_test_client.poll_for_message(
        member_id,
        before_ts,
        predicate=lambda p: (
            p.get("user_id") == bot_id and "pong" in p.get("message", "").lower()
        ),
        timeout=120,
    )
    assert "pong" in reply["message"].lower()

    # ── 3. Now the negatives mean something ───────────────────────────────────
    # Windowed on the BAIT POST'S OWN server timestamp, not on `before_ts`.
    # `before_ts` is the pytest runner's clock and `create_at` is the
    # container's, and `get_posts` filters with `since - 1`: if the Mattermost
    # container's clock trails the host's by more than a millisecond — Docker
    # Desktop's VM clock drifts across sleep/resume — the bait post falls
    # outside the window and this control fails while accusing the read of
    # being untrustworthy. Every other use of `before_ts` in this file absorbs
    # skew in tens of seconds of agent latency; this one had no margin at all.
    outside_posts = mm_bot_client.get_posts(outside_id, since_ms=bait["create_at"])
    assert any(p.get("id") == bait["id"] for p in outside_posts), (
        "the bot's own read of "
        f"#{mm_setup['outside_channel']} did not return the message this test "
        "just posted there, so it cannot be trusted to reveal a message from "
        "the bot either — the assertion below would pass for the wrong reason. "
        f"Read {len(outside_posts)} post(s) since ts={before_ts}."
    )
    leaked = [p for p in outside_posts if p.get("user_id") == bot_id]
    assert leaked == [], (
        f"the bot posted in #{mm_setup['outside_channel']}, a channel it is "
        f"not a member of: {[p.get('message') for p in leaked]!r}"
    )

    # A watcher for the outside channel would mean the event arrived even if
    # the reply did not — a different and worse failure than a missing reply,
    # because the room would then be tracked, persisted and woken on schedule.
    # It is also the check that makes the post read above need no margin for
    # reply latency: a watcher record is created when the event is received,
    # before any agent work, so an event that arrived at all is visible here
    # by the time the pong lands, whatever the outside turn was doing.
    watchers = watcher_list()
    member_handle = f"{MM_CONNECTOR_NAME}:{mm_setup['member_channel']}"
    # Guard on the guard: if the member watcher is missing, the listing is not
    # telling us what we think it is, and the absence below proves nothing.
    assert member_handle in watchers, (
        f"expected a watcher {member_handle!r} after the round trip above — "
        f"'list --all' says:\n{watchers}"
    )
    assert outside_handle not in watchers, (
        f"a watcher materialized for a channel the bot never joined "
        f"({outside_handle!r}) — 'list --all' says:\n{watchers}"
    )
