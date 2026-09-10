"""One representation inside, ISO only where a human reads it (§5.2).

Three review rounds in a row landed in the same subsystem, and the root was
that two timestamp representations were in play with nothing saying which
interface wanted which. A value that looked right crossed an interface that
wanted the other one; Rocket.Chat's bound normalizer tolerated both and
Mattermost's raised, so the same value worked on one connector and, on the
other, raised into a blanket `except` that logged "starting without history".

The rule: **every timestamp crossing an AgentCoop interface is epoch milliseconds as
a string.** ISO appears at two edges only — what an operator types into the
control socket, and the `ts` field of the dicts `fetch_room_history` hands an
agent to read.

These tests are deliberately cross-connector: a rule applied on one connector
and not the other is how the previous two rounds' defects survived a reading of
either file on its own.
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock

from gateway.core.adapter_utils import ts_gt, ts_to_float
from gateway.core.replay_window import just_before

_EPOCH = "1786874400000"
_ISO = "2026-08-16T10:00:00+00:00"


class TestTheOrderingPrimitivesCannotOrderISO(unittest.TestCase):
    """Why epoch-ms is the internal representation and ISO cannot be.

    Pinned rather than assumed: these are the measurements that decide the
    rule, and if any of them ever changes the rule should be revisited rather
    than silently relied on.
    """

    def test_iso_does_not_parse_numerically(self):
        self.assertIsNone(ts_to_float(_ISO))
        self.assertIsNotNone(ts_to_float(_EPOCH))

    def test_a_mixed_comparison_is_decided_by_string_order_not_time(self):
        """`ts_gt(iso, epoch)` is True for every pair a deployment can produce
        today — including when the ISO value is genuinely earlier — because the
        numeric path gives up and `"2026…" > "1786…"`. The accident expires
        when epoch-ms crosses "2000000000000" in 2033."""
        self.assertTrue(ts_gt(_ISO, _EPOCH), "same instant, reported greater")
        self.assertTrue(
            ts_gt("1999-01-01T00:00:00+00:00", _EPOCH),
            "twenty-seven years earlier, still reported greater",
        )

    def test_just_before_does_not_narrow_an_iso_bound(self):
        """It returns its argument unchanged, so an exclusive bound silently
        is not one."""
        self.assertEqual(just_before(_ISO), _ISO)
        self.assertEqual(just_before(_EPOCH), "1786874399999")


class TestBothConnectorsAgreeOnTheRepresentation(unittest.IsolatedAsyncioTestCase):
    """The asymmetry that caused the failure was per-connector, so every
    assertion here runs against both."""

    def _rc(self):
        from gateway.connectors.rocketchat.config import RocketChatConfig
        from gateway.connectors.rocketchat.connector import RocketChatConnector

        c = RocketChatConnector(RocketChatConfig(
            server_url="https://x", username="bot", password="pw", name="rc",
            owners=["glin"], timezone="UTC"))
        c._rest = MagicMock()
        # Pre-login: agent_username falls back to the configured spelling.
        c._rest.bot_username = None
        c._rest.get_room_history = AsyncMock(return_value=[])
        return c

    def _mm(self):
        from gateway.connectors.mattermost.config import MattermostConfig
        from gateway.connectors.mattermost.connector import MattermostConnector

        c = MattermostConnector(MattermostConfig(
            server_url="https://x", username="bot", password="pw", name="mm",
            team="eng", owners=["glin"], timezone="UTC"))
        c._rest = MagicMock()
        # Pre-login: agent_username falls back to the configured spelling.
        c._rest.bot_username = None
        c._rest.get_room_history = AsyncMock(return_value=[])
        c._rest.bot_username = "bot"
        return c

    def _triggers(self):
        """One trigger frame per connector, both naming the same instant."""
        return (
            (self._rc(), {"_id": "m1", "ts": {"$date": int(_EPOCH)}}),
            (self._mm(), {"post": {"id": "m1", "create_at": int(_EPOCH)}}),
        )

    def test_trigger_history_bound_is_epoch_ms_on_both(self):
        for connector, trigger in self._triggers():
            with self.subTest(connector=type(connector).__name__):
                bound = connector.trigger_history_bound(trigger)
                self.assertEqual(bound, _EPOCH)
                self.assertIsNotNone(
                    ts_to_float(bound),
                    "a bound its own consumer cannot compare numerically is "
                    "the defect this rule exists to prevent",
                )

    async def test_an_epoch_bound_reaches_fetch_room_history_on_both(self):
        """The crash site. Mattermost used to convert here and raise on
        epoch-ms; Rocket.Chat tolerated both and hid the divergence."""
        from gateway.core.connector import Room

        for connector in (self._rc(), self._mm()):
            with self.subTest(connector=type(connector).__name__):
                await connector.fetch_room_history(
                    Room(id="r1", name="eng", type="channel"),
                    10, before_ts=_EPOCH,
                )
                connector._rest.get_room_history.assert_awaited()


class TestTheAgentFacingHalfStaysISO(unittest.IsolatedAsyncioTestCase):
    """The deliberate asymmetry: bounds are compared by AgentCoop, the returned `ts`
    is read by an agent."""

    async def test_returned_messages_carry_iso_timestamps(self):
        from gateway.connectors.rocketchat.config import RocketChatConfig
        from gateway.connectors.rocketchat.connector import RocketChatConnector
        from gateway.core.connector import Room

        c = RocketChatConnector(RocketChatConfig(
            server_url="https://x", username="bot", password="pw", name="rc",
            owners=["glin"], timezone="UTC"))
        c._rest = MagicMock()
        # Pre-login: agent_username falls back to the configured spelling.
        c._rest.bot_username = None
        c._rest.get_room_history = AsyncMock(return_value=[
            {"_id": "m1", "msg": "hi", "u": {"username": "glin"},
             "ts": {"$date": int(_EPOCH)}},
        ])

        msgs = await c.fetch_room_history(
            Room(id="r1", name="eng", type="channel"), 10)

        self.assertEqual(msgs[0]["ts"], _ISO)


if __name__ == "__main__":
    unittest.main()
