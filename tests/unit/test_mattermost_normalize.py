"""Unit tests for gateway.connectors.mattermost.normalize.

Covers:
  - filter_mm_message: own-message (by ID), system-message skip, sender
    allow-list, @mention gate (via the ID-based mentions array, not text),
    room-wide mention (@channel/@all/@here, text-based), timestamp dedup,
    agent-chain turn budget + reset-on-human-message
  - normalize_mm_message: text extraction (leading mention strip vs raw DM),
    mention-id-to-username resolution, thread_id from root_id
  - text_mentions_bot / mentions.text_has_room_wide_mention
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from gateway.connectors.mattermost.config import MattermostConfig
from gateway.connectors.mattermost.mentions import (
    is_room_wide_mention,
    text_has_room_wide_mention,
)
from gateway.connectors.mattermost.normalize import (
    filter_mm_message,
    normalize_mm_message,
    text_mentions_bot,
)
from gateway.core.agent_chain import AgentChainConfig, TurnStore
from gateway.core.connector import Room

BOT_ID = "bot-id-1"


def _config(**overrides) -> MattermostConfig:
    defaults = dict(
        server_url="https://x", team="t", token="tok",
        owners=["alice"], guests=["bob"],
        require_mention=True, filter_sender=True,
    )
    defaults.update(overrides)
    return MattermostConfig(**defaults)


def _post(**overrides) -> dict:
    defaults = dict(
        id="post1", create_at=1000, user_id="user-1",
        channel_id="chan1", root_id="", message="hello", type="",
        file_ids=[],
    )
    defaults.update(overrides)
    return defaults


# ── filter_mm_message ─────────────────────────────────────────────────────────


class TestOwnMessageFilter(unittest.TestCase):
    def test_own_message_by_id_is_rejected(self):
        result = filter_mm_message(
            post=_post(user_id=BOT_ID), mentions=[], sender_username="hammer.mei",
            config=_config(), room_type="channel", last_processed_ts=None,
            bot_user_id=BOT_ID,
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "own message")


class TestSystemMessageFilter(unittest.TestCase):
    def test_system_message_is_rejected(self):
        result = filter_mm_message(
            post=_post(type="system_join_channel"), mentions=[BOT_ID],
            sender_username="alice", config=_config(), room_type="channel",
            last_processed_ts=None, bot_user_id=BOT_ID,
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "system message")


class TestSenderAllowList(unittest.TestCase):
    def test_unlisted_sender_rejected_when_filter_sender_true(self):
        result = filter_mm_message(
            post=_post(), mentions=[BOT_ID], sender_username="mallory",
            config=_config(filter_sender=True), room_type="dm",
            last_processed_ts=None, bot_user_id=BOT_ID,
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "sender not in allow-list")

    def test_unlisted_sender_accepted_when_filter_sender_false(self):
        result = filter_mm_message(
            post=_post(), mentions=[BOT_ID], sender_username="mallory",
            config=_config(filter_sender=False), room_type="dm",
            last_processed_ts=None, bot_user_id=BOT_ID,
        )
        self.assertTrue(result.accepted)

    def test_owner_accepted(self):
        result = filter_mm_message(
            post=_post(), mentions=[BOT_ID], sender_username="alice",
            config=_config(), room_type="dm", last_processed_ts=None,
            bot_user_id=BOT_ID,
        )
        self.assertTrue(result.accepted)


class TestMentionGate(unittest.TestCase):
    def test_dm_bypasses_mention_requirement(self):
        result = filter_mm_message(
            post=_post(message="no mention here"), mentions=[],
            sender_username="alice", config=_config(), room_type="dm",
            last_processed_ts=None, bot_user_id=BOT_ID,
        )
        self.assertTrue(result.accepted)

    def test_channel_requires_mention_by_id(self):
        result = filter_mm_message(
            post=_post(message="no mention"), mentions=[],
            sender_username="alice", config=_config(), room_type="channel",
            last_processed_ts=None, bot_user_id=BOT_ID,
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "bot not mentioned")

    def test_channel_accepted_when_bot_id_in_mentions(self):
        result = filter_mm_message(
            post=_post(message="@hammer.mei hi"), mentions=[BOT_ID],
            sender_username="alice", config=_config(), room_type="channel",
            last_processed_ts=None, bot_user_id=BOT_ID,
        )
        self.assertTrue(result.accepted)

    def test_channel_accepted_on_room_wide_text_mention(self):
        result = filter_mm_message(
            post=_post(message="@channel heads up"), mentions=[],
            sender_username="alice", config=_config(), room_type="channel",
            last_processed_ts=None, bot_user_id=BOT_ID,
        )
        self.assertTrue(result.accepted)

    def test_require_mention_false_bypasses_gate(self):
        result = filter_mm_message(
            post=_post(message="no mention"), mentions=[],
            sender_username="alice", config=_config(require_mention=False),
            room_type="channel", last_processed_ts=None, bot_user_id=BOT_ID,
        )
        self.assertTrue(result.accepted)

    def test_group_dm_requires_mention(self):
        """A group DM goes down the mention-required side (§2.7, §6.4).

        `require_mention` skips 1:1 DMs only. A group DM classified as a plain
        DM would make the agent answer every message from anyone in the group —
        which is why `room_type_for` maps "G" to "group_dm", not "dm", and why
        this gate must never test `kind is direct` instead of `== "dm"`.
        """
        result = filter_mm_message(
            post=_post(message="no mention here"), mentions=[],
            sender_username="alice", config=_config(), room_type="group_dm",
            last_processed_ts=None, bot_user_id=BOT_ID,
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "bot not mentioned")

    def test_group_dm_accepted_when_bot_mentioned(self):
        result = filter_mm_message(
            post=_post(message="@hammer.mei hi"), mentions=[BOT_ID],
            sender_username="alice", config=_config(), room_type="group_dm",
            last_processed_ts=None, bot_user_id=BOT_ID,
        )
        self.assertTrue(result.accepted)

    def test_agent_sender_bypasses_mention_gate(self):
        cfg = _config(agent_chain=AgentChainConfig(agent_usernames=["peer"]))
        result = filter_mm_message(
            post=_post(message="no mention"), mentions=[],
            sender_username="peer", config=cfg, room_type="channel",
            last_processed_ts=None, bot_user_id=BOT_ID,
        )
        self.assertTrue(result.accepted)
        self.assertTrue(result.is_agent_chain)


class TestTimestampDedup(unittest.TestCase):
    def test_older_or_equal_message_rejected(self):
        result = filter_mm_message(
            post=_post(create_at=1000), mentions=[BOT_ID], sender_username="alice",
            config=_config(), room_type="dm", last_processed_ts="1000",
            bot_user_id=BOT_ID,
        )
        self.assertFalse(result.accepted)
        self.assertIn("already processed", result.reason)

    def test_newer_message_accepted(self):
        result = filter_mm_message(
            post=_post(create_at=2000), mentions=[BOT_ID], sender_username="alice",
            config=_config(), room_type="dm", last_processed_ts="1000",
            bot_user_id=BOT_ID,
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.msg_ts, "2000")


class TestAgentChainTurnBudget(unittest.TestCase):
    def _cfg(self):
        return _config(
            agent_chain=AgentChainConfig(agent_usernames=["peer"], max_turns=2, ttl_seconds=60)
        )

    def test_turn_budget_enforced_then_dropped(self):
        store = TurnStore()
        cfg = self._cfg()
        r1 = filter_mm_message(
            post=_post(id="p1"), mentions=[], sender_username="peer", config=cfg,
            room_type="channel", last_processed_ts=None, bot_user_id=BOT_ID, turn_store=store,
        )
        r2 = filter_mm_message(
            post=_post(id="p2"), mentions=[], sender_username="peer", config=cfg,
            room_type="channel", last_processed_ts=None, bot_user_id=BOT_ID, turn_store=store,
        )
        r3 = filter_mm_message(
            post=_post(id="p3"), mentions=[], sender_username="peer", config=cfg,
            room_type="channel", last_processed_ts=None, bot_user_id=BOT_ID, turn_store=store,
        )
        self.assertTrue(r1.accepted)
        self.assertEqual(r1.agent_chain_turn, 1)
        self.assertTrue(r2.accepted)
        self.assertEqual(r2.agent_chain_turn, 2)
        self.assertFalse(r3.accepted)
        self.assertEqual(r3.reason, "agent chain turn limit reached")

    def test_human_message_resets_all_agent_counters(self):
        store = TurnStore()
        cfg = self._cfg()
        filter_mm_message(
            post=_post(id="p1"), mentions=[], sender_username="peer", config=cfg,
            room_type="channel", last_processed_ts=None, bot_user_id=BOT_ID, turn_store=store,
        )
        self.assertEqual(store.current_turns("chan1", None, "peer"), 1)

        filter_mm_message(
            post=_post(id="p2", user_id="human-1"), mentions=[BOT_ID],
            sender_username="alice", config=cfg, room_type="channel",
            last_processed_ts=None, bot_user_id=BOT_ID, turn_store=store,
        )
        self.assertEqual(store.current_turns("chan1", None, "peer"), 0)

    def test_unmentioned_human_post_in_channel_still_resets_counters(self):
        """#187: the reset runs before the mention gate. A human post that
        does not @mention this bot is still rejected, but it revives a chain
        that hit max_turns — the operator does not have to @mention anyone."""
        store = TurnStore()
        cfg = self._cfg()
        for pid in ("p1", "p2"):
            filter_mm_message(
                post=_post(id=pid), mentions=[], sender_username="peer", config=cfg,
                room_type="channel", last_processed_ts=None, bot_user_id=BOT_ID,
                turn_store=store,
            )
        self.assertEqual(store.current_turns("chan1", None, "peer"), 2)

        human = filter_mm_message(
            post=_post(id="p3", user_id="human-1", message="no mention here"),
            mentions=[], sender_username="alice", config=cfg, room_type="channel",
            last_processed_ts=None, bot_user_id=BOT_ID, turn_store=store,
        )
        self.assertFalse(human.accepted)
        self.assertEqual(human.reason, "bot not mentioned")
        self.assertEqual(store.current_turns("chan1", None, "peer"), 0)

        revived = filter_mm_message(
            post=_post(id="p4"), mentions=[], sender_username="peer", config=cfg,
            room_type="channel", last_processed_ts=None, bot_user_id=BOT_ID,
            turn_store=store,
        )
        self.assertTrue(revived.accepted)
        self.assertEqual(revived.agent_chain_turn, 1)

    def test_replayed_human_post_does_not_reset_counters(self):
        """Dedup still runs before the reset: a human post the connector has
        already processed must not hand the agents a fresh budget."""
        store = TurnStore()
        cfg = self._cfg()
        filter_mm_message(
            post=_post(id="p1", create_at=2000), mentions=[], sender_username="peer",
            config=cfg, room_type="channel", last_processed_ts=None, bot_user_id=BOT_ID,
            turn_store=store,
        )
        self.assertEqual(store.current_turns("chan1", None, "peer"), 1)

        replayed = filter_mm_message(
            post=_post(id="p0", create_at=1000, user_id="human-1"), mentions=[],
            sender_username="alice", config=cfg, room_type="channel",
            last_processed_ts="2000", bot_user_id=BOT_ID, turn_store=store,
        )
        self.assertFalse(replayed.accepted)
        self.assertIn("already processed", replayed.reason)
        self.assertEqual(store.current_turns("chan1", None, "peer"), 1)


# ── text_mentions_bot / room-wide mention helpers ────────────────────────────


class TestTextMentionsBot(unittest.TestCase):
    def test_detects_standalone_mention(self):
        self.assertTrue(text_mentions_bot("@hammer.mei ping", "hammer.mei"))

    def test_no_match_without_mention(self):
        self.assertFalse(text_mentions_bot("just chatting", "hammer.mei"))

    def test_empty_bot_username_returns_false(self):
        self.assertFalse(text_mentions_bot("@hammer.mei ping", ""))

    def test_does_not_match_substring_username(self):
        self.assertFalse(text_mentions_bot("@hammer.meister hi", "hammer.mei"))


class TestRoomWideMention(unittest.TestCase):
    def test_channel_all_here_detected(self):
        for kw in ("channel", "all", "here"):
            self.assertTrue(text_has_room_wide_mention(f"@{kw} attention"))

    def test_no_room_wide_mention(self):
        self.assertFalse(text_has_room_wide_mention("@alice hi"))

    def test_is_room_wide_mention_username_check(self):
        self.assertTrue(is_room_wide_mention("all"))
        self.assertFalse(is_room_wide_mention("alice"))


# ── normalize_mm_message ──────────────────────────────────────────────────────


class TestNormalizeMmMessage(unittest.IsolatedAsyncioTestCase):
    def _rest(self, bot_username="hammer.mei"):
        rest = MagicMock()
        rest.bot_username = bot_username
        rest.resolve_username = AsyncMock(side_effect=lambda uid: {"u1": "alice"}.get(uid, uid))
        return rest

    async def test_strips_leading_mention_in_channel(self):
        room = Room(id="chan1", name="general", type="channel")
        msg = await normalize_mm_message(
            post=_post(message="@hammer.mei hello there"), mentions=[],
            room=room, sender_username="alice", sender_id="u1", msg_ts="1000",
            config=_config(), rest=self._rest(), cache_dir=Path("/tmp/mm-test-cache"),
        )
        self.assertEqual(msg.text, "hello there")

    async def test_dm_text_not_stripped(self):
        room = Room(id="dm1", name="@alice", type="dm")
        msg = await normalize_mm_message(
            post=_post(message="hello there"), mentions=[],
            room=room, sender_username="alice", sender_id="u1", msg_ts="1000",
            config=_config(), rest=self._rest(), cache_dir=Path("/tmp/mm-test-cache"),
        )
        self.assertEqual(msg.text, "hello there")

    async def test_empty_message_falls_back_to_placeholder(self):
        room = Room(id="dm1", name="@alice", type="dm")
        msg = await normalize_mm_message(
            post=_post(message=""), mentions=[],
            room=room, sender_username="alice", sender_id="u1", msg_ts="1000",
            config=_config(), rest=self._rest(), cache_dir=Path("/tmp/mm-test-cache"),
        )
        self.assertEqual(msg.text, "(empty message)")

    async def test_mentions_resolved_to_usernames(self):
        room = Room(id="chan1", name="general", type="channel")
        msg = await normalize_mm_message(
            post=_post(message="@hammer.mei hi"), mentions=["u1"],
            room=room, sender_username="alice", sender_id="u1", msg_ts="1000",
            config=_config(), rest=self._rest(), cache_dir=Path("/tmp/mm-test-cache"),
        )
        self.assertEqual(msg.mentions, ["alice"])

    async def test_room_wide_mention_added_as_all(self):
        room = Room(id="chan1", name="general", type="channel")
        msg = await normalize_mm_message(
            post=_post(message="@channel hi"), mentions=[],
            room=room, sender_username="alice", sender_id="u1", msg_ts="1000",
            config=_config(), rest=self._rest(), cache_dir=Path("/tmp/mm-test-cache"),
        )
        self.assertIn("all", msg.mentions)

    async def test_thread_id_from_root_id(self):
        room = Room(id="chan1", name="general", type="channel")
        msg = await normalize_mm_message(
            post=_post(root_id="root-post-1"), mentions=[],
            room=room, sender_username="alice", sender_id="u1", msg_ts="1000",
            config=_config(), rest=self._rest(), cache_dir=Path("/tmp/mm-test-cache"),
        )
        self.assertEqual(msg.thread_id, "root-post-1")

    async def test_no_thread_id_when_root_id_empty(self):
        room = Room(id="chan1", name="general", type="channel")
        msg = await normalize_mm_message(
            post=_post(root_id=""), mentions=[],
            room=room, sender_username="alice", sender_id="u1", msg_ts="1000",
            config=_config(), rest=self._rest(), cache_dir=Path("/tmp/mm-test-cache"),
        )
        self.assertIsNone(msg.thread_id)

    async def test_role_resolved_from_config(self):
        room = Room(id="chan1", name="general", type="channel")
        msg = await normalize_mm_message(
            post=_post(message="@hammer.mei hi"), mentions=[],
            room=room, sender_username="alice", sender_id="u1", msg_ts="1000",
            config=_config(), rest=self._rest(), cache_dir=Path("/tmp/mm-test-cache"),
        )
        self.assertEqual(msg.role.value, "owner")

    async def test_agent_chain_context_stored(self):
        room = Room(id="chan1", name="general", type="channel")
        msg = await normalize_mm_message(
            post=_post(), mentions=[],
            room=room, sender_username="peer", sender_id="u1", msg_ts="1000",
            config=_config(), rest=self._rest(), cache_dir=Path("/tmp/mm-test-cache"),
            is_agent_chain=True, agent_chain_turn=2, agent_chain_max_turns=5,
        )
        self.assertTrue(msg.extra_context["is_agent_chain"])
        self.assertEqual(msg.extra_context["agent_chain_turn"], 2)
        self.assertEqual(msg.extra_context["agent_chain_max_turns"], 5)

    async def test_no_attachments_when_no_file_ids(self):
        room = Room(id="chan1", name="general", type="channel")
        msg = await normalize_mm_message(
            post=_post(file_ids=[]), mentions=[],
            room=room, sender_username="alice", sender_id="u1", msg_ts="1000",
            config=_config(), rest=self._rest(), cache_dir=Path("/tmp/mm-test-cache"),
        )
        self.assertEqual(msg.attachments, [])
        self.assertEqual(msg.warnings, [])


if __name__ == "__main__":
    unittest.main()
