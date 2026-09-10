"""The mark that keeps a refused message reachable — the rule, not either connector.

`gateway/core/replay_window.py` is shared because the question is AgentCoop's own: both
connectors refuse messages when their queues are full, and refusing is only honest if
something remembers where to look. Everything platform-specific stays with the platform,
so this file tests the rule and the connector suites test the wiring.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass, field

from gateway.core.replay_window import ReplayWindow, just_before


@dataclass
class _Window(ReplayWindow):
    replay_boundary: str | None = None
    boundary_claims: int = 0
    promised_ids: set = field(default_factory=set)


class TestTheOldestCandidateWins(unittest.TestCase):
    """Not the first one offered — that put the mark past the message it was opened for.

    The hand-back sites offer the live watermark before the point just below the refused
    message, on the assumption that the watermark is older. Replay is not serialized
    against the per-room worker on either connector, so a newer message can be accepted
    while an older one is still in the handler, and then it is not.
    """

    def test_a_watermark_ahead_of_the_refused_message_does_not_win(self):
        w = _Window()

        # The concurrent worker already committed 900; the message being refused is 500.
        w.claim_boundary("900", just_before("500"))

        self.assertEqual(
            w.replay_boundary, "499",
            "a boundary above the refused message cannot recover it — the one thing it "
            "may never do",
        )

    def test_a_watermark_behind_it_still_wins(self):
        """The ordinary case, and the reason the watermark is offered at all: it is the
        older mark, so the window covers everything since."""
        w = _Window()

        w.claim_boundary("400", just_before("500"))

        self.assertEqual(w.replay_boundary, "400")

    def test_an_open_window_is_never_narrowed(self):
        w = _Window()
        w.claim_boundary("100")
        w.claim_boundary("400", just_before("500"))

        self.assertEqual(w.replay_boundary, "100", "the older mark covers both windows")

    def test_an_unparseable_candidate_sorts_oldest(self):
        """A bound too low costs a re-fetch dedup absorbs; too high loses a message."""
        w = _Window()

        w.claim_boundary("not-a-timestamp", just_before("500"))

        self.assertEqual(w.replay_boundary, "not-a-timestamp")

    def test_a_claim_with_nothing_to_point_at_writes_nothing(self):
        w = _Window()

        self.assertEqual(w.claim_boundary(None, ""), 0)
        self.assertIsNone(w.replay_boundary)


class TestOnlyTheClaimantThatReadItMayCloseIt(unittest.TestCase):
    def test_a_second_claim_counts_even_at_the_same_value(self):
        w = _Window()
        first = w.claim_boundary("100")
        second = w.claim_boundary("100")

        self.assertNotEqual(
            first, second,
            "two claimants wanting the same timestamp is the case a value cannot see",
        )

    def test_discharge_refuses_once_someone_else_has_claimed(self):
        w = _Window()
        at_entry = w.claim_boundary("100")
        w.claim_boundary("100")

        self.assertFalse(w.discharge_boundary(at_entry))
        self.assertEqual(w.replay_boundary, "100")

    def test_discharge_closes_the_window_it_read(self):
        w = _Window()
        at_entry = w.claim_boundary("100")

        self.assertTrue(w.discharge_boundary(at_entry))
        self.assertIsNone(w.replay_boundary)
        self.assertEqual(w.boundary_claims, 0, "a closed window owes nobody a read")

    def test_discarding_is_not_the_same_as_reading(self):
        """`discard_boundary` says the window must never be read — Rocket.Chat's
        membership removal — and does not care who else claimed it."""
        w = _Window()
        w.claim_boundary("100")
        w.claim_boundary("100")

        w.discard_boundary()

        self.assertIsNone(w.replay_boundary)
        self.assertEqual(w.boundary_claims, 0)


class TestJustBefore(unittest.TestCase):
    def test_strictly_below(self):
        self.assertEqual(just_before("500"), "499")

    def test_an_unparseable_timestamp_is_left_alone(self):
        """Better an unusable bound than a fabricated one."""
        self.assertEqual(just_before("not-a-time"), "not-a-time")
        self.assertEqual(just_before(""), "")


class TestClosingTheWindowLeavesThePromisesAlone(unittest.TestCase):
    """`promised_ids` is the third piece of the bookkeeping (see the class
    docstring): frames a creation episode handed back, whose boundary is
    claimed only if the filter rejects one. A promise is settled by the frame
    it names, at the filter — never by a replay closing its window. A replay
    cannot tell which promised frames its page covered: one promised DURING
    the replay is newer than the page, and one promised before it may sit
    below the watermark the page started from. Clearing on discharge lost
    both (Codex, PR #140 round 3). Only a removal voids them."""

    def test_discharge_leaves_a_promise_made_before_the_replay(self):
        w = _Window()
        w.promised_ids.add("below-the-watermark")
        n = w.claim_boundary("100")

        self.assertTrue(w.discharge_boundary(n))
        self.assertEqual(w.promised_ids, {"below-the-watermark"})

    def test_discharge_leaves_a_promise_made_during_the_replay(self):
        """The Codex scenario: a creation episode completes while the replay is
        awaiting dispatch; its promise does not move the claim count, so the
        discharge is accepted — and must not take the promise with it."""
        w = _Window()
        n = w.claim_boundary("100")
        w.promised_ids.add("handed-back-mid-replay")

        self.assertTrue(w.discharge_boundary(n))
        self.assertEqual(w.promised_ids, {"handed-back-mid-replay"})

    def test_a_refused_discharge_keeps_them(self):
        w = _Window()
        w.promised_ids.add("m1")
        n = w.claim_boundary("100")
        w.claim_boundary("50")

        self.assertFalse(w.discharge_boundary(n))
        self.assertEqual(w.promised_ids, {"m1"})

    def test_discard_clears_them(self):
        """Nobody is entitled to a removed room's frames — promises included."""
        w = _Window()
        w.promised_ids.add("m1")
        w.claim_boundary("100")

        w.discard_boundary()
        self.assertEqual(w.promised_ids, set())
