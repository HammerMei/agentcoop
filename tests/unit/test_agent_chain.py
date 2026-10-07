"""Tests for agent-chain turn tracking and loop protection.

Covers:
  - TurnStore: check_and_increment within budget
  - TurnStore: check_and_increment at budget limit → denied
  - TurnStore: reset_sender allows fresh start
  - TurnStore: reset_all resets all senders for a thread
  - TurnStore: human message (non-agent) triggers reset_all via filter
  - TurnStore: reset_all logs at INFO only when a counter was spent (#187)
  - TurnStore: TTL GC removes expired entries
  - build_agent_chain_context: normal turn
  - build_agent_chain_context: penultimate turn has warning
  - build_agent_chain_context: final turn has closing + scheduler hint
  - filter_rc_message: agent sender passes through with is_agent_chain=True
  - filter_rc_message: agent sender at turn limit → dropped + counter reset
  - filter_rc_message: require_mention=False allows non-mentioned messages
  - filter_rc_message: unmentioned human post in a channel still resets counters (#187)
  - filter_rc_message: replayed (deduplicated) human post does not reset counters
  - filter_rc_message: filter_sender=False allows unknown senders
"""

from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from gateway.connectors.rocketchat.agent_chain import AGENT_CHAIN_TERMINATION_TOKEN, TurnStore
from gateway.connectors.rocketchat.config import AgentChainConfig, RocketChatConfig
from gateway.connectors.rocketchat.mentions import is_room_wide_mention
from gateway.connectors.rocketchat.normalize import filter_rc_message
from gateway.core.agent_chain import AGENT_CHAIN_TERMINATION_TOKEN as CORE_TOKEN
from gateway.core.agent_chain import build_agent_chain_context

# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_config(
    username: str = "bot",
    owners: list[str] | None = None,
    guests: list[str] | None = None,
    agent_usernames: list[str] | None = None,
    max_turns: int = 5,
    require_mention: bool = True,
    filter_sender: bool = True,
) -> RocketChatConfig:
    return RocketChatConfig(
        server_url="http://rc.test",
        username=username,
        password="secret",
        owners=owners or [],
        guests=guests or [],
        require_mention=require_mention,
        filter_sender=filter_sender,
        agent_chain=AgentChainConfig(
            agent_usernames=agent_usernames or [],
            max_turns=max_turns,
        ),
    )


def _make_doc(
    sender: str = "user1",
    rid: str = "room1",
    msg: str = "hello",
    tmid: str | None = None,
    mentions: list[dict] | None = None,
    ts: int = 1000,
) -> dict:
    doc: dict = {
        "u": {"username": sender, "_id": sender, "name": sender},
        "rid": rid,
        "msg": msg,
        "ts": ts,
    }
    if tmid:
        doc["tmid"] = tmid
    if mentions is not None:
        doc["mentions"] = mentions
    return doc


# ── TurnStore tests ────────────────────────────────────────────────────────────


class TestTurnStore(unittest.TestCase):
    def test_check_and_increment_within_budget(self):
        store = TurnStore()
        allowed, turn, _tok = store.check_and_increment("room1", None, "agentA", max_turns=5)
        self.assertTrue(allowed)
        self.assertEqual(turn, 1)

        allowed2, turn2, _tok = store.check_and_increment("room1", None, "agentA", max_turns=5)
        self.assertTrue(allowed2)
        self.assertEqual(turn2, 2)

    def test_check_and_increment_at_budget_limit_denied(self):
        store = TurnStore()
        for _ in range(3):
            store.check_and_increment("room1", None, "agentA", max_turns=3)

        allowed, turn, _tok = store.check_and_increment("room1", None, "agentA", max_turns=3)
        self.assertFalse(allowed)
        self.assertEqual(turn, 3)  # current count (not incremented)

    def test_reset_sender_allows_fresh_start(self):
        store = TurnStore()
        for _ in range(3):
            store.check_and_increment("room1", None, "agentA", max_turns=3)

        store.reset_sender("room1", None, "agentA")

        allowed, turn, _tok = store.check_and_increment("room1", None, "agentA", max_turns=3)
        self.assertTrue(allowed)
        self.assertEqual(turn, 1)

    def test_reset_all_resets_all_senders_for_thread(self):
        store = TurnStore()
        store.check_and_increment("room1", "thread1", "agentA", max_turns=5)
        store.check_and_increment("room1", "thread1", "agentB", max_turns=5)
        store.check_and_increment("room1", None, "agentA", max_turns=5)  # different thread

        store.reset_all("room1", "thread1")

        # agentA and agentB in thread1 reset
        self.assertEqual(store.current_turns("room1", "thread1", "agentA"), 0)
        self.assertEqual(store.current_turns("room1", "thread1", "agentB"), 0)
        # agentA in top-level (None thread) not affected
        self.assertEqual(store.current_turns("room1", None, "agentA"), 1)

    def test_reset_all_on_human_message_via_filter(self):
        """Non-agent message passing through filter triggers reset_all for that context."""
        store = TurnStore()
        store.check_and_increment("room1", None, "agentA", max_turns=5)

        config = _make_config(owners=["human1"], agent_usernames=["agentA"])
        doc = _make_doc(sender="human1", rid="room1", msg="@bot hi")

        # Human message in DM (room_type=dm) passes sender filter, triggers reset_all
        filter_rc_message(doc, config, "dm", None, turn_store=store)

        self.assertEqual(store.current_turns("room1", None, "agentA"), 0)

    def test_reset_all_logs_at_info_only_when_a_counter_was_spent(self):
        """#187: the reset line is the operator's clue why a room came back to
        life, so it is INFO — but gated on a counter actually being spent, since
        entries sit in the store at zero after a reset and a human post would
        otherwise log on every message in a room an agent has ever spoken in."""
        store = TurnStore()
        store.check_and_increment("room1", None, "agentA", max_turns=5)

        with self.assertLogs("coop.core.agent_chain", level="INFO") as cm:
            store.reset_all("room1", None)
        self.assertEqual(len(cm.output), 1)
        self.assertIn("Agent chain counters reset for room=room1", cm.output[0])
        self.assertIn("1 of 1 senders had turns", cm.output[0])

        with self.assertNoLogs("coop.core.agent_chain", level="INFO"):
            store.reset_all("room1", None)  # already at zero: nothing to report

    def test_ttl_gc_removes_expired_entries(self):
        store = TurnStore(ttl_seconds=1.0)
        store.check_and_increment("room1", None, "agentA", max_turns=5)
        self.assertEqual(store.current_turns("room1", None, "agentA"), 1)

        # Simulate time passing beyond TTL
        with patch("gateway.core.agent_chain.time.monotonic",
                   return_value=time.monotonic() + 2.0):
            store._gc()

        self.assertEqual(store.current_turns("room1", None, "agentA"), 0)

    def test_independent_counters_per_sender(self):
        store = TurnStore()
        store.check_and_increment("room1", None, "agentA", max_turns=5)
        store.check_and_increment("room1", None, "agentA", max_turns=5)
        store.check_and_increment("room1", None, "agentB", max_turns=5)

        self.assertEqual(store.current_turns("room1", None, "agentA"), 2)
        self.assertEqual(store.current_turns("room1", None, "agentB"), 1)

    def test_independent_counters_per_thread(self):
        store = TurnStore()
        store.check_and_increment("room1", "t1", "agentA", max_turns=5)
        store.check_and_increment("room1", "t2", "agentA", max_turns=5)
        store.check_and_increment("room1", "t2", "agentA", max_turns=5)

        self.assertEqual(store.current_turns("room1", "t1", "agentA"), 1)
        self.assertEqual(store.current_turns("room1", "t2", "agentA"), 2)

    def test_current_turns_returns_zero_for_unknown(self):
        store = TurnStore()
        self.assertEqual(store.current_turns("room1", None, "nobody"), 0)


# ── build_agent_chain_context tests ───────────────────────────────────────────


class TestBuildAgentChainContext(unittest.TestCase):
    def test_normal_turn_includes_turn_info_and_termination_hint(self):
        ctx = build_agent_chain_context(turn=2, max_turns=5)
        self.assertIn("[Agent chain: turn 2/5]", ctx)
        self.assertIn(CORE_TOKEN, ctx)
        self.assertNotIn("final turn", ctx)
        self.assertNotIn("next response will be your last", ctx)

    def test_loop_detection_hint_present_on_non_final_turns(self):
        """Non-final turns include the loop-detection instruction."""
        for turn in (1, 2, 4):  # all turns before max
            ctx = build_agent_chain_context(turn=turn, max_turns=5)
            self.assertIn("repeating without making progress", ctx)

    def test_loop_detection_hint_absent_on_final_turn(self):
        """Final turn uses graceful-closing wording instead of loop hint."""
        ctx = build_agent_chain_context(turn=5, max_turns=5)
        self.assertNotIn("repeating without making progress", ctx)

    def test_penultimate_turn_has_warning(self):
        ctx = build_agent_chain_context(turn=4, max_turns=5)
        self.assertIn("[Agent chain: turn 4/5]", ctx)
        self.assertIn("next response will be your last", ctx)
        self.assertIn(CORE_TOKEN, ctx)

    def test_final_turn_has_closing_and_scheduler_hint(self):
        ctx = build_agent_chain_context(turn=5, max_turns=5)
        self.assertIn("[Agent chain: turn 5/5]", ctx)
        self.assertIn("final turn", ctx)
        self.assertIn("scheduler tool", ctx)
        # At max_turns, no termination token hint (can't self-terminate on last turn)
        self.assertNotIn(CORE_TOKEN, ctx)

    def test_first_turn_normal(self):
        ctx = build_agent_chain_context(turn=1, max_turns=5)
        self.assertIn("[Agent chain: turn 1/5]", ctx)
        self.assertIn(CORE_TOKEN, ctx)
        self.assertNotIn("final", ctx)

    def test_tokens_match_between_core_and_connector(self):
        """The token defined in core and connector layers must be identical."""
        self.assertEqual(CORE_TOKEN, AGENT_CHAIN_TERMINATION_TOKEN)


# ── filter_rc_message agent chain tests ───────────────────────────────────────


class TestFilterRcMessageAgentChain(unittest.TestCase):
    def test_agent_sender_passes_with_is_agent_chain_true(self):
        config = _make_config(owners=["human1"], agent_usernames=["agentA"])
        store = TurnStore()
        doc = _make_doc(sender="agentA", rid="room1", msg="hello")

        result = filter_rc_message(doc, config, "channel", None, turn_store=store)

        self.assertTrue(result.accepted)
        self.assertTrue(result.is_agent_chain)
        self.assertEqual(result.agent_chain_turn, 1)
        self.assertEqual(result.agent_chain_max_turns, 5)

    def test_agent_sender_at_turn_limit_dropped_counter_stays_at_max(self):
        """Counter is NOT reset on force-drop — stays at max to prevent loop restart."""
        config = _make_config(owners=["human1"], agent_usernames=["agentA"], max_turns=3)
        store = TurnStore()
        # Exhaust budget
        for i in range(3):
            doc = _make_doc(sender="agentA", rid="room1", msg="msg", ts=i + 1)
            filter_rc_message(doc, config, "channel", None, turn_store=store)

        # 4th message: force-drop, counter stays at max
        doc = _make_doc(sender="agentA", rid="room1", msg="over limit", ts=100)
        result = filter_rc_message(doc, config, "channel", None, turn_store=store)

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "agent chain turn limit reached")
        # Counter stays at max (NOT reset) so subsequent messages stay blocked
        self.assertEqual(store.current_turns("room1", None, "agentA"), 3)

    def test_agent_sender_stays_blocked_after_turn_limit(self):
        """Once force-dropped, ALL subsequent messages are also dropped until reset."""
        config = _make_config(owners=["human1"], agent_usernames=["agentA"], max_turns=3)
        store = TurnStore()
        for i in range(3):
            doc = _make_doc(sender="agentA", rid="room1", msg="msg", ts=i + 1)
            filter_rc_message(doc, config, "channel", None, turn_store=store)

        # Every subsequent message is immediately force-dropped
        for ts in range(100, 105):
            doc = _make_doc(sender="agentA", rid="room1", msg="still trying", ts=ts)
            result = filter_rc_message(doc, config, "channel", None, turn_store=store)
            self.assertFalse(result.accepted)
            self.assertEqual(result.reason, "agent chain turn limit reached")

    def test_agent_bypasses_mention_requirement_in_channel(self):
        config = _make_config(owners=["human1"], agent_usernames=["agentA"], require_mention=True)
        store = TurnStore()
        # No @mention in the message
        doc = _make_doc(sender="agentA", rid="room1", msg="no mention here")

        result = filter_rc_message(doc, config, "channel", None, turn_store=store)

        self.assertTrue(result.accepted)
        self.assertTrue(result.is_agent_chain)

    def test_agent_not_in_allow_list_still_accepted(self):
        """Agent usernames bypass the allow-list / filter_sender check."""
        config = _make_config(
            owners=["human1"],
            guests=[],
            agent_usernames=["agentA"],
            filter_sender=True,  # strict allow-list mode
        )
        store = TurnStore()
        doc = _make_doc(sender="agentA", rid="room1", msg="hello")

        result = filter_rc_message(doc, config, "dm", None, turn_store=store)

        self.assertTrue(result.accepted)

    def test_unmentioned_human_post_in_channel_still_resets_counters(self):
        """#187: the reset runs before the mention gate. A human post that
        does not @mention this bot is still rejected, but it revives a chain
        that hit max_turns — the operator does not have to @mention anyone."""
        config = _make_config(owners=["human1"], agent_usernames=["agentA"], max_turns=2)
        store = TurnStore()
        for ts in (1000, 1001):
            filter_rc_message(
                _make_doc(sender="agentA", rid="room1", ts=ts), config, "channel", None,
                turn_store=store,
            )
        self.assertEqual(store.current_turns("room1", None, "agentA"), 2)

        human = filter_rc_message(
            _make_doc(sender="human1", rid="room1", msg="no mention here", ts=1002),
            config, "channel", None, turn_store=store,
        )
        self.assertFalse(human.accepted)
        self.assertEqual(human.reason, "bot not mentioned")
        self.assertEqual(store.current_turns("room1", None, "agentA"), 0)

        revived = filter_rc_message(
            _make_doc(sender="agentA", rid="room1", ts=1003), config, "channel", None,
            turn_store=store,
        )
        self.assertTrue(revived.accepted)
        self.assertEqual(revived.agent_chain_turn, 1)

    def test_replayed_human_post_does_not_reset_counters(self):
        """Dedup still runs before the reset: a human message the connector
        has already processed must not hand the agents a fresh budget."""
        config = _make_config(owners=["human1"], agent_usernames=["agentA"])
        store = TurnStore()
        filter_rc_message(
            _make_doc(sender="agentA", rid="room1", ts=2000), config, "channel", None,
            turn_store=store,
        )
        self.assertEqual(store.current_turns("room1", None, "agentA"), 1)

        replayed = filter_rc_message(
            _make_doc(sender="human1", rid="room1", msg="@bot hi", ts=1000),
            config, "channel", "2000", turn_store=store,
        )
        self.assertFalse(replayed.accepted)
        self.assertIn("already processed", replayed.reason)
        self.assertEqual(store.current_turns("room1", None, "agentA"), 1)

    def test_require_mention_false_allows_non_mentioned_human_messages(self):
        config = _make_config(owners=["human1"], require_mention=False)
        doc = _make_doc(sender="human1", rid="room1", msg="no mention here")

        result = filter_rc_message(doc, config, "channel", None)

        self.assertTrue(result.accepted)
        self.assertFalse(result.is_agent_chain)

    def test_require_mention_true_accepts_room_wide_all_mention(self):
        config = _make_config(owners=["human1"], require_mention=True)
        room_wide_username = "all"
        self.assertTrue(is_room_wide_mention(room_wide_username))
        doc = _make_doc(
            sender="human1",
            rid="room1",
            msg="@all everyone please check in",
            mentions=[{"username": room_wide_username}],
        )

        result = filter_rc_message(doc, config, "channel", None)

        self.assertTrue(result.accepted)
        self.assertFalse(result.is_agent_chain)

    def test_room_wide_all_mention_does_not_bypass_sender_filter(self):
        config = _make_config(owners=["human1"], require_mention=True, filter_sender=True)
        doc = _make_doc(
            sender="stranger",
            rid="room1",
            msg="@all everyone please check in",
            mentions=[{"username": "all"}],
        )

        result = filter_rc_message(doc, config, "channel", None)

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "sender not in allow-list")

    def test_filter_sender_false_allows_unknown_senders(self):
        config = _make_config(owners=["human1"], filter_sender=False)
        doc = _make_doc(sender="stranger", rid="room1", msg="hello")

        result = filter_rc_message(doc, config, "dm", None)

        self.assertTrue(result.accepted)
        self.assertFalse(result.is_agent_chain)

    def test_filter_sender_true_blocks_unknown_senders(self):
        config = _make_config(owners=["human1"], filter_sender=True)
        doc = _make_doc(sender="stranger", rid="room1", msg="hello")

        result = filter_rc_message(doc, config, "dm", None)

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "sender not in allow-list")

    def test_agent_in_thread_tracked_independently(self):
        config = _make_config(owners=["human1"], agent_usernames=["agentA"], max_turns=3)
        store = TurnStore()

        doc_t1 = _make_doc(sender="agentA", rid="room1", msg="msg", tmid="thread1", ts=1)
        doc_t2 = _make_doc(sender="agentA", rid="room1", msg="msg", tmid="thread2", ts=2)

        r1 = filter_rc_message(doc_t1, config, "channel", None, turn_store=store)
        r2 = filter_rc_message(doc_t2, config, "channel", None, turn_store=store)

        self.assertTrue(r1.accepted)
        self.assertTrue(r2.accepted)
        self.assertEqual(store.current_turns("room1", "thread1", "agentA"), 1)
        self.assertEqual(store.current_turns("room1", "thread2", "agentA"), 1)

    def test_no_turn_store_agent_passes_without_tracking(self):
        """When turn_store=None, agent messages pass without any turn tracking."""
        config = _make_config(owners=["human1"], agent_usernames=["agentA"])
        doc = _make_doc(sender="agentA", rid="room1", msg="hello")

        result = filter_rc_message(doc, config, "dm", None, turn_store=None)

        self.assertTrue(result.accepted)
        self.assertTrue(result.is_agent_chain)
        self.assertEqual(result.agent_chain_turn, 0)  # no tracking done


if __name__ == "__main__":
    unittest.main()


class TestReleasingATurnTakenByAMessageNotDelivered(unittest.TestCase):
    """The budget counts turns an agent *took*, not messages the filter looked at.

    The filter spends a turn before anything knows whether the message can be delivered,
    and a message handed back for a later retry re-enters it. Without a release, retrying
    the same document exhausts a budget it never used — after which the filter rejects it
    as complete, the replay reports success, and the window closes over a message nobody
    saw.
    """

    def setUp(self):
        from gateway.core.agent_chain import TurnStore

        self.store = TurnStore()

    def test_a_released_turn_is_available_again(self):
        allowed, turn, _tok = self.store.check_and_increment("r1", None, "agent-a", max_turns=2)
        self.assertTrue(allowed)
        self.assertEqual(turn, 1)

        self.assertEqual(self.store.release_turn("r1", None, "agent-a", turn, 0), 0)

        allowed, turn, _tok = self.store.check_and_increment("r1", None, "agent-a", max_turns=2)
        self.assertTrue(allowed)
        self.assertEqual(turn, 1, "the retry takes the same turn, not the next one")

    def test_retrying_a_handed_back_message_does_not_exhaust_the_budget(self):
        """The scenario, in the small: the same document retried more times than the
        budget allows must still be deliverable."""
        for _ in range(5):
            allowed, _, tok = self.store.check_and_increment(
                "r1", None, "agent-a", max_turns=2)
            self.assertTrue(allowed, "a retried message must never be refused for budget")
            self.store.release_turn("r1", None, "agent-a", tok, 0)

        allowed, turn, _tok = self.store.check_and_increment("r1", None, "agent-a", max_turns=2)
        self.assertTrue(allowed)
        self.assertEqual(turn, 1)

    def test_a_second_release_of_the_same_turn_does_nothing(self):
        """A caller bug must not corrupt the counter — the second release no longer owns
        the number it names."""
        _, taken, _tok = self.store.check_and_increment("r1", None, "agent-a", max_turns=2)
        self.store.release_turn("r1", None, "agent-a", taken, 0)
        self.store.release_turn("r1", None, "agent-a", taken, 0)

        self.assertEqual(self.store.current_turns("r1", None, "agent-a"), 0)

    def test_an_earlier_delivery_can_release_after_a_later_one_took_its_turn(self):
        """Overlapping deliveries, no reset — and the earlier one must still be able to
        hand its turn back.

        This assertion used to say the opposite, on the reasoning that a moved counter
        meant the release no longer owned anything. That reasoning loses the message: the
        budget stays spent on something never sent, `check_and_increment` then refuses the
        retry as exhausted, the filter reports it complete, and the replay closes its
        window over it. A turn is owned by the delivery that took it, not by the position
        of the count.
        """
        gen = self.store.generation("r1", None, "agent-a")
        _, _, mine = self.store.check_and_increment("r1", None, "agent-a", max_turns=5)
        self.store.check_and_increment("r1", None, "agent-a", max_turns=5)  # a later one

        self.store.release_turn("r1", None, "agent-a", mine, gen)

        self.assertEqual(
            self.store.current_turns("r1", None, "agent-a"), 1,
            "the later delivery keeps its turn; only the earlier one is given back",
        )

    def test_a_released_token_cannot_be_released_twice(self):
        gen = self.store.generation("r1", None, "agent-a")
        _, _, mine = self.store.check_and_increment("r1", None, "agent-a", max_turns=5)
        self.store.check_and_increment("r1", None, "agent-a", max_turns=5)

        self.store.release_turn("r1", None, "agent-a", mine, gen)
        self.store.release_turn("r1", None, "agent-a", mine, gen)

        self.assertEqual(self.store.current_turns("r1", None, "agent-a"), 1)

    def test_a_budget_full_of_handed_back_turns_still_admits_a_retry(self):
        """The failure the finding describes, end to end: every turn taken and handed
        back, then a retry must still be allowed."""
        gen = self.store.generation("r1", None, "agent-a")
        tokens = []
        for _ in range(2):
            allowed, _, tok = self.store.check_and_increment(
                "r1", None, "agent-a", max_turns=2)
            self.assertTrue(allowed)
            tokens.append(tok)
        for tok in tokens:
            self.store.release_turn("r1", None, "agent-a", tok, gen)

        allowed, _, _ = self.store.check_and_increment("r1", None, "agent-a", max_turns=2)
        self.assertTrue(allowed, "a budget spent only on undelivered messages is not spent")

    def test_a_reset_between_take_and_release_is_respected(self):
        """The case a turn number alone cannot see.

        A reset starts the next count at one again, so an in-flight delivery holding
        "I took turn 1" matches a turn 1 that belongs to a different message. The numbers
        are deliberately identical here; only the generation separates them.
        """
        gen = self.store.generation("r1", None, "agent-a")
        _, mine, _tok = self.store.check_and_increment("r1", None, "agent-a", max_turns=5)
        self.assertEqual(mine, 1)

        self.store.reset_all("r1", None)                       # a human spoke
        _, theirs, _tok = self.store.check_and_increment("r1", None, "agent-a", max_turns=5)
        self.assertEqual(theirs, 1, "the fresh count starts at the same number")

        self.store.release_turn("r1", None, "agent-a", mine, gen)

        self.assertEqual(
            self.store.current_turns("r1", None, "agent-a"), 1,
            "taking this turn away would let the chain run past max_turns on a count "
            "that belongs to a delivery which succeeded",
        )

    def test_a_sender_reset_between_take_and_release_is_respected(self):
        """`reset_sender` is the other reset, and it had the same hole."""
        gen = self.store.generation("r1", None, "agent-a")
        _, mine, _tok = self.store.check_and_increment("r1", None, "agent-a", max_turns=5)

        self.store.reset_sender("r1", None, "agent-a")
        self.store.check_and_increment("r1", None, "agent-a", max_turns=5)

        self.store.release_turn("r1", None, "agent-a", mine, gen)

        self.assertEqual(self.store.current_turns("r1", None, "agent-a"), 1)

    def test_releasing_an_unknown_sender_is_a_no_op(self):
        self.assertEqual(self.store.release_turn("r1", None, "nobody", 1, 0), 0)

    def test_a_delivered_turn_is_not_released(self):
        """The near miss: releasing unconditionally would make the budget unenforceable."""
        for expected in (1, 2):
            allowed, turn, _tok = self.store.check_and_increment(
                "r1", None, "agent-a", max_turns=2)
            self.assertTrue(allowed)
            self.assertEqual(turn, expected)

        allowed, _, _tok = self.store.check_and_increment("r1", None, "agent-a", max_turns=2)
        self.assertFalse(allowed, "the budget still stops a genuine chain")


class TestExpiryDoesNotReissueAnInFlightIdentity(unittest.TestCase):
    """A context can be garbage-collected while a delivery still holds a turn from it.

    Nothing bounds a delivery by the store's TTL — normalization, an attachment download
    and a handler are not on that clock — so the context can expire, a later message can
    recreate the key, and the old delivery's release would then land on a fresh count.
    This is the reset case arriving through expiry, and the generation has to survive the
    entry it belonged to.
    """

    def setUp(self):
        from gateway.core.agent_chain import TurnStore

        self.store = TurnStore(ttl_seconds=0.0)   # everything is expired on the next call

    def test_a_token_from_an_expired_context_is_not_honoured(self):
        gen = self.store.generation("r1", None, "agent-a")
        _, _, mine = self.store.check_and_increment("r1", None, "agent-a", max_turns=5)

        # A later message: `_gc` drops the quiet context, and this recreates the key.
        self.store.check_and_increment("r1", None, "agent-a", max_turns=5)

        self.store.release_turn("r1", None, "agent-a", mine, gen)

        self.assertEqual(
            self.store.current_turns("r1", None, "agent-a"), 1,
            "the fresh count belongs to the message that started it",
        )

    def test_the_generation_keeps_climbing_across_expiry(self):
        first = self.store.generation("r1", None, "agent-a")
        self.store.check_and_increment("r1", None, "agent-a", max_turns=5)
        self.store.check_and_increment("r1", None, "agent-a", max_turns=5)   # gc + recreate

        self.assertGreater(
            self.store.generation("r1", None, "agent-a"), first,
            "a number that resets is a number that can be matched by a stale delivery",
        )

    def test_a_live_delivery_can_still_release_within_one_generation(self):
        """The near miss: bumping on every call would make every release a no-op."""
        from gateway.core.agent_chain import TurnStore

        store = TurnStore()          # a normal TTL: nothing expires here
        gen = store.generation("r1", None, "agent-a")
        _, _, mine = store.check_and_increment("r1", None, "agent-a", max_turns=5)

        store.release_turn("r1", None, "agent-a", mine, gen)

        self.assertEqual(store.current_turns("r1", None, "agent-a"), 0)


class TestReleasedTokenBookkeepingIsBounded(unittest.TestCase):
    """A retry loop hands the same message back indefinitely, and the TTL cannot help.

    Every attempt takes a fresh token and gives it back, and every attempt's *increment*
    refreshes `last_updated` — so `_gc` never reclaims the context and the bookkeeping
    would grow for as long as the processor stays full.

    The first version of this test released every token in order, which is the one
    interleaving a prefix counter handles — so it passed while the mechanism was broken
    for every real chain. The interleaving that matters has a **delivered** token in it:
    delivered tokens are never released, and a prefix stops dead at the first one.
    """

    def setUp(self):
        from gateway.core.agent_chain import TurnStore

        self.store = TurnStore()

    def _ctx(self):
        return self.store._store[self.store._key("r1", None, "agent-a")]

    def _take(self):
        return self.store.check_and_increment("r1", None, "agent-a", max_turns=5)[2]

    def _give_back(self, token):
        return self.store.release_turn("r1", None, "agent-a", token, generation=0)

    def test_a_retry_loop_behind_a_delivered_turn_stays_bounded(self):
        """The case the prefix could not reach: token 1 is delivered and never comes back."""
        from gateway.core.agent_chain import _RELEASED_TOKENS_REMEMBERED

        self._take()                                  # token 1 — delivered, never released
        for _ in range(_RELEASED_TOKENS_REMEMBERED * 3):
            self._give_back(self._take())

        ctx = self._ctx()
        self.assertEqual(ctx.issued, _RELEASED_TOKENS_REMEMBERED * 3 + 1)
        self.assertLessEqual(len(ctx.released), _RELEASED_TOKENS_REMEMBERED)
        self.assertEqual(
            len(ctx.released_set), len(ctx.released),
            "the index must not outlive what the queue remembers",
        )

    def test_a_retry_loop_never_exhausts_the_budget(self):
        """What the release is *for*, independent of how it is remembered."""
        for _ in range(500):
            token = self._take()
            self.assertNotEqual(token, 0, "each release frees the turn the retry takes")
            self._give_back(token)

        self.assertEqual(self.store.current_turns("r1", None, "agent-a"), 0)

    def test_a_recently_released_token_is_refused_a_second_release(self):
        self._take()                                  # delivered
        token = self._take()
        self._give_back(token)
        live = self.store.current_turns("r1", None, "agent-a")

        self._give_back(token)

        self.assertEqual(
            self.store.current_turns("r1", None, "agent-a"), live,
            "a second release may not take a turn a live delivery is using",
        )

    def test_an_out_of_order_release_is_still_refused_twice(self):
        """Releases do not arrive in issue order — replay and live traffic overlap."""
        a, b, c = self._take(), self._take(), self._take()
        self._give_back(c)
        self._give_back(a)
        after = self.store.current_turns("r1", None, "agent-a")

        self._give_back(c)
        self._give_back(a)

        self.assertEqual(self.store.current_turns("r1", None, "agent-a"), after)
        self.assertEqual(after, 1, "only b's turn is still held")
        self.assertTrue(self._ctx().is_released(b) is False)

    def test_a_reset_forgets_every_token_of_the_previous_count(self):
        for _ in range(5):
            self._give_back(self._take())

        self.store.reset_sender("r1", None, "agent-a")
        gen = self._ctx().generation
        token = self._take()

        self.assertEqual(token, 1, "a fresh count reissues from one")
        self.assertEqual(
            self.store.release_turn("r1", None, "agent-a", token, generation=gen), 0,
            "stale bookkeeping would report this token as already given back",
        )

    def test_reset_all_forgets_them_too(self):
        for _ in range(5):
            self._give_back(self._take())

        self.store.reset_all("r1", None)
        gen = self._ctx().generation
        token = self._take()

        self.assertEqual(
            self.store.release_turn("r1", None, "agent-a", token, generation=gen), 0,
            "the room-wide reset carries the same rule as the per-sender one",
        )


class TestGenerationTombstonesAreBounded(unittest.TestCase):
    """The key includes a thread id, so "one per room" was never the bound.

    A long-lived connector meets an unbounded number of threads, and a tombstone per
    sender in each would outlive every context and defeat the reclamation the TTL exists
    for. A tombstone only has to outlive a delivery still in flight.
    """

    def test_tombstones_do_not_accumulate_for_dead_threads(self):
        import time as _time

        from gateway.core.agent_chain import TurnStore

        store = TurnStore(ttl_seconds=0.0)
        for i in range(50):
            store.check_and_increment("r1", f"thread-{i}", "agent-a", max_turns=5)

        # Every context has expired, and so has every tombstone's grace period.
        _time.sleep(0.01)
        store.check_and_increment("r1", "thread-final", "agent-a", max_turns=5)

        self.assertLessEqual(
            len(store._generations), 2,
            "a tombstone per thread, kept forever, is the leak this replaced",
        )

    def test_a_tombstone_survives_long_enough_to_matter(self):
        """The near miss: pruning immediately would put the expiry hole straight back.

        The context has to actually **expire** for this to test anything. An earlier
        version used `reset_all`, which leaves the context in `_store` — so `release_turn`
        found it and compared generations off the live object, and `_generations` was never
        read at all. It passed with `_TOMBSTONE_TTL_FACTOR = 0`, which is literally the
        thing its own docstring says it guards against.
        """
        from unittest.mock import patch

        from gateway.core.agent_chain import _TOMBSTONE_TTL_FACTOR, TurnStore

        ttl = 60.0
        store = TurnStore(ttl_seconds=ttl)
        t0 = time.monotonic()

        with patch("gateway.core.agent_chain.time.monotonic", return_value=t0):
            gen = store.generation("r1", None, "agent-a")
            _, _, mine = store.check_and_increment("r1", None, "agent-a", max_turns=5)

        # Long enough for the context to expire and be deleted, but inside the tombstone's
        # grace period — the window this delivery is still in flight in.
        mid = t0 + ttl * (1 + _TOMBSTONE_TTL_FACTOR) / 2
        with patch("gateway.core.agent_chain.time.monotonic", return_value=mid):
            store._gc()
            self.assertNotIn(
                store._key("r1", None, "agent-a"), store._store,
                "the context must be gone, or the tombstone is not what answers",
            )
            # A new chain starts under the same key and takes a turn of its own.
            store.check_and_increment("r1", None, "agent-a", max_turns=5)
            # The in-flight delivery from the dead context finally comes back.
            store.release_turn("r1", None, "agent-a", mine, gen)

        self.assertEqual(
            store.current_turns("r1", None, "agent-a"), 1,
            "a token from an expired context may not take the new chain's turn",
        )
