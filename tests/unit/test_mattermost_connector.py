"""Unit tests for MattermostConnector.

Covers:
  - format_prompt_prefix: unsafe-character sanitization, day/ts/to fields
  - _compute_to_field: the addressing vocabulary (me / @agent / me+@agent / @all / *)
  - subscribe_room / unsubscribe_room: local bookkeeping + refcounting (no
    wire-protocol call — MattermostWebSocketClient.register_channel/
    unregister_channel are asserted, not a subscribe confirmation)
  - _on_posted_event: own-message skip by ID, system-message skip, seen-id
    dedup, watermark advance only after handler acceptance, busy-notification
    suppressed during replay, single dispatch under concurrent delivery of
    the same message id (code-review regression test)
  - agent_username fallback (rest.bot_username vs config.username)
  - supports_attachments / supports_history / attachment_cache_dir
  - on_agent_chain_drop resets the turn store
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.agents.response import AgentResponse
from gateway.connectors.mattermost.config import MattermostConfig
from gateway.connectors.mattermost.connector import MattermostConnector
from gateway.core.agent_chain import AgentChainConfig
from gateway.core.connector import IncomingMessage, Room, RoomCapacity, User, UserRole
from gateway.core.replay_window import just_before
from gateway.core.watcher_rule import RoomKind


def _config(**overrides) -> MattermostConfig:
    defaults = dict(
        server_url="https://x", team="t", username="hammer.mei", password="pw",
        name="mm-test", owners=["glin"],
    )
    defaults.update(overrides)
    return MattermostConfig(**defaults)


def _make_connector(**config_overrides) -> MattermostConnector:
    connector = MattermostConnector(_config(**config_overrides))
    connector._rest.bot_username = "hammer.mei"
    connector._rest.bot_user_id = "bot-id-1"

    # Replay asks for a *page* so it can tell an empty window from a page the server
    # filled with system posts before AgentCoop filtered them. Derived from whatever a test
    # sets on `get_room_history`, so a test that does not care about that distinction
    # keeps expressing itself in messages — and one that does care sets the page mock
    # directly and overrides this.
    async def _page(channel_id, count=50, before_ts=None, after_ts=None):
        from gateway.core.connector import HistoryPage

        kw = {"count": count}
        if before_ts is not None:
            kw["before_ts"] = before_ts
        if after_ts is not None:
            kw["after_ts"] = after_ts
        msgs = await connector._rest.get_room_history(channel_id, **kw)
        return HistoryPage(messages=msgs, raw_count=len(msgs), limit=count)

    connector._rest.get_room_history = AsyncMock(return_value=[])
    connector._rest.get_room_history_page = _page
    return connector


def _msg(**overrides) -> IncomingMessage:
    defaults = dict(
        id="m1", timestamp="1700000000000",
        room=Room(id="chan1", name="general", type="channel"),
        sender=User(id="u1", username="alice", display_name="alice"),
        role=UserRole.OWNER, text="hi",
    )
    defaults.update(overrides)
    return IncomingMessage(**defaults)


# ── format_prompt_prefix ──────────────────────────────────────────────────────


class TestFormatPromptPrefixSanitization(unittest.TestCase):
    def test_strips_unsafe_characters_from_room_and_user(self):
        connector = _make_connector()
        msg = _msg(
            room=Room(id="c1", name="gen|eral", type="channel"),
            sender=User(id="u1", username="al|ce", display_name="x"),
        )
        prefix = connector.format_prompt_prefix(msg)
        self.assertNotIn("|ce", prefix.split("from:")[1].split("|")[0])
        self.assertIn("gen_eral", prefix)
        self.assertIn("al_ce", prefix)

    def test_strips_brackets_and_newlines(self):
        connector = _make_connector()
        msg = _msg(
            room=Room(id="c1", name="gen]eral\n", type="channel"),
            sender=User(id="u1", username="a[b", display_name="x"),
        )
        prefix = connector.format_prompt_prefix(msg)
        self.assertNotIn("]", prefix.split("|")[0])
        self.assertNotIn("[", prefix.split("from:")[1])


class TestFormatPromptPrefixFields(unittest.TestCase):
    def test_includes_platform_name_and_role(self):
        connector = _make_connector()
        msg = _msg()
        prefix = connector.format_prompt_prefix(msg)
        self.assertTrue(prefix.startswith("[Mattermost #general | from: alice | role: owner"))

    def test_includes_day_and_ts_when_timestamp_parseable(self):
        connector = _make_connector()
        msg = _msg(timestamp="1700000000000")
        prefix = connector.format_prompt_prefix(msg)
        self.assertIn("day:", prefix)
        self.assertIn("ts:", prefix)

    def test_dm_to_field_is_me(self):
        connector = _make_connector()
        msg = _msg(room=Room(id="dm1", name="@alice", type="dm"))
        prefix = connector.format_prompt_prefix(msg)
        self.assertIn("to: me", prefix)


class TestComputeToField(unittest.TestCase):
    def _connector(self):
        return _make_connector(
            agent_chain=AgentChainConfig(agent_usernames=["wavebro"])
        )

    def test_dm_is_always_me(self):
        connector = self._connector()
        msg = _msg(room=Room(id="dm1", name="@alice", type="dm"), mentions=[])
        self.assertEqual(connector._compute_to_field(msg), "to: me")

    def test_no_mention_is_broadcast(self):
        connector = self._connector()
        msg = _msg(mentions=[])
        self.assertEqual(connector._compute_to_field(msg), "to: *")

    def test_a_scheduled_injection_is_addressed_to_me_not_broadcast(self):
        """The message a scheduled job injects has no mentions, so mention
        arithmetic rendered it `to: *` — and the routing rules for a broadcast in
        a channel say "be conservative; if you decide not to respond, output ONLY
        the termination token". Measured on a live deployment: the agent did
        exactly that, every minute, and a working job posted nothing. A job is
        addressed to the agent it was created against."""
        import dataclasses

        from gateway.core.connector import SCHEDULER_SENDER_ID, User

        connector = self._connector()
        msg = dataclasses.replace(
            _msg(mentions=[]),
            sender=User(id=SCHEDULER_SENDER_ID, username=SCHEDULER_SENDER_ID,
                        display_name="Scheduler"),
        )

        self.assertEqual(connector._compute_to_field(msg), "to: me")
        prefix = connector.format_prompt_prefix(msg)
        self.assertIn("from: scheduler", prefix)
        self.assertIn("to: me", prefix)

    def test_bot_mentioned_directly(self):
        connector = self._connector()
        msg = _msg(mentions=["hammer.mei"])
        self.assertEqual(connector._compute_to_field(msg), "to: me")

    def test_other_agent_mentioned_not_bot(self):
        connector = self._connector()
        msg = _msg(mentions=["wavebro"])
        self.assertEqual(connector._compute_to_field(msg), "to: @wavebro")

    def test_bot_and_other_agent_mentioned(self):
        connector = self._connector()
        msg = _msg(mentions=["hammer.mei", "wavebro"])
        self.assertEqual(connector._compute_to_field(msg), "to: me+@wavebro")

    def test_room_wide_mention(self):
        connector = self._connector()
        msg = _msg(mentions=["all"])
        self.assertEqual(connector._compute_to_field(msg), "to: @all")

    def test_non_agent_user_mention_ignored(self):
        connector = self._connector()
        msg = _msg(mentions=["random_human"])
        self.assertEqual(connector._compute_to_field(msg), "to: *")


# ── subscribe_room / unsubscribe_room ─────────────────────────────────────────


class TestSubscribeUnsubscribe(unittest.IsolatedAsyncioTestCase):
    async def test_subscribe_registers_local_state_and_channel(self):
        connector = _make_connector()
        connector._ws.register_channel = MagicMock()
        room = Room(id="chan1", name="general", type="channel")

        await connector.subscribe_room(room, watcher_id="w1")

        self.assertIn("chan1", connector._channels)
        connector._ws.register_channel.assert_called_once_with("chan1")

    async def test_second_watcher_does_not_reregister_channel(self):
        connector = _make_connector()
        connector._ws.register_channel = MagicMock()
        room = Room(id="chan1", name="general", type="channel")

        await connector.subscribe_room(room, watcher_id="w1")
        await connector.subscribe_room(room, watcher_id="w2")

        connector._ws.register_channel.assert_called_once()  # only on first subscribe
        self.assertEqual(connector._channels["chan1"].watcher_ids, {"w1", "w2"})

    async def test_unsubscribe_keeps_state_while_watchers_remain(self):
        connector = _make_connector()
        connector._ws.register_channel = MagicMock()
        connector._ws.unregister_channel = MagicMock()
        room = Room(id="chan1", name="general", type="channel")
        await connector.subscribe_room(room, watcher_id="w1")
        await connector.subscribe_room(room, watcher_id="w2")

        await connector.unsubscribe_room("chan1", watcher_id="w1")

        self.assertIn("chan1", connector._channels)
        connector._ws.unregister_channel.assert_not_called()

    async def test_unsubscribe_last_watcher_removes_state(self):
        connector = _make_connector()
        connector._ws.register_channel = MagicMock()
        connector._ws.unregister_channel = MagicMock()
        room = Room(id="chan1", name="general", type="channel")
        await connector.subscribe_room(room, watcher_id="w1")

        await connector.unsubscribe_room("chan1", watcher_id="w1")

        self.assertNotIn("chan1", connector._channels)
        connector._ws.unregister_channel.assert_called_once_with("chan1")

    async def test_unsubscribe_unknown_channel_is_noop(self):
        connector = _make_connector()
        connector._ws.unregister_channel = MagicMock()
        await connector.unsubscribe_room("nonexistent", watcher_id="w1")
        connector._ws.unregister_channel.assert_not_called()


# ── send_text / send_media ────────────────────────────────────────────────────


class TestSendTextAndMedia(unittest.IsolatedAsyncioTestCase):
    async def test_send_text_forwards_root_id_as_thread_id(self):
        connector = _make_connector()
        connector._rest.post_message = AsyncMock()

        await connector.send_text("chan1", AgentResponse(text="hi", session_id=""), thread_id="root1")

        connector._rest.post_message.assert_called_once_with("chan1", "hi", root_id="root1")

    async def test_send_media_uploads_then_posts_with_file_ids(self):
        connector = _make_connector()
        connector._rest.upload_file = AsyncMock(return_value=["f1", "f2"])
        connector._rest.post_message = AsyncMock()

        await connector.send_media("chan1", "/tmp/f.txt", caption="a file")

        connector._rest.upload_file.assert_called_once_with("chan1", "/tmp/f.txt")
        connector._rest.post_message.assert_called_once_with("chan1", "a file", file_ids=["f1", "f2"])


class TestSendToRoom(unittest.IsolatedAsyncioTestCase):
    """Regression tests for a bug found post-review: send_to_room() (the
    CLI `coop send <room> --attach ...` path, per
    mm-gateway-context.md) discarded upload_file()'s returned file_ids and
    called post_message() without them — the uploaded file never got linked
    to any post and rendered as an invisible orphan in the channel."""

    async def test_attachment_with_caption_links_file_ids_to_post(self):
        connector = _make_connector()
        connector._rest.resolve_room = AsyncMock(return_value={"id": "chan1", "name": "general", "type": "channel"})
        connector._rest.upload_file = AsyncMock(return_value=["f1"])
        connector._rest.post_message = AsyncMock()

        await connector.send_to_room("general", "a caption", attachment_path="/tmp/f.txt")

        connector._rest.upload_file.assert_called_once_with("chan1", "/tmp/f.txt")
        connector._rest.post_message.assert_called_once_with("chan1", "a caption", file_ids=["f1"])

    async def test_attachment_with_no_caption_still_posts_with_file_ids(self):
        """Previously: an empty caption meant post_message() was never
        called at all, so the uploaded file was never linked to any post."""
        connector = _make_connector()
        connector._rest.resolve_room = AsyncMock(return_value={"id": "chan1", "name": "general", "type": "channel"})
        connector._rest.upload_file = AsyncMock(return_value=["f1"])
        connector._rest.post_message = AsyncMock()

        await connector.send_to_room("general", "", attachment_path="/tmp/f.txt")

        connector._rest.post_message.assert_called_once_with("chan1", "", file_ids=["f1"])

    async def test_text_only_posts_without_file_ids(self):
        connector = _make_connector()
        connector._rest.resolve_room = AsyncMock(return_value={"id": "chan1", "name": "general", "type": "channel"})
        connector._rest.upload_file = AsyncMock()
        connector._rest.post_message = AsyncMock()

        await connector.send_to_room("general", "hello")

        connector._rest.upload_file.assert_not_called()
        connector._rest.post_message.assert_called_once_with("chan1", "hello")

    async def test_room_not_found_falls_back_to_raw_id(self):
        from gateway.connectors.mattermost.rest import RoomNotFoundError

        connector = _make_connector()
        connector._rest.resolve_room = AsyncMock(side_effect=RoomNotFoundError("nope"))
        connector._rest.post_message = AsyncMock()

        await connector.send_to_room("chan-raw-id", "hello")

        connector._rest.post_message.assert_called_once_with("chan-raw-id", "hello")


# ── agent_username fallback ───────────────────────────────────────────────────


class TestAgentUsername(unittest.TestCase):
    def test_falls_back_to_config_username_before_connect(self):
        connector = MattermostConnector(_config(username="hammer.mei", password="pw"))
        self.assertEqual(connector.agent_username, "hammer.mei")

    def test_uses_rest_bot_username_after_connect(self):
        connector = MattermostConnector(
            _config(token="tok", username="", password="", server_url="https://x")
        )
        connector._rest.bot_username = "resolved.bot"
        self.assertEqual(connector.agent_username, "resolved.bot")


# ── capability flags ──────────────────────────────────────────────────────────


class TestCapabilityFlags(unittest.TestCase):
    def test_supports_attachments(self):
        self.assertTrue(_make_connector().supports_attachments())

    def test_supports_history(self):
        self.assertTrue(_make_connector().supports_history())

    def test_delivery_mode_is_gateway(self):
        self.assertEqual(_make_connector().delivery_mode, "gateway")

    def test_attachment_cache_dir_namespaced_by_connector_and_channel(self):
        connector = _make_connector()
        cache_dir = connector.attachment_cache_dir("chan1")
        self.assertIn("mm-test", cache_dir)
        self.assertIn("chan1", cache_dir)


# ── on_agent_chain_drop ───────────────────────────────────────────────────────


class TestOnAgentChainDrop(unittest.TestCase):
    def test_resets_turn_store_when_configured(self):
        connector = _make_connector(
            agent_chain=AgentChainConfig(agent_usernames=["peer"], max_turns=3)
        )
        connector._turn_store.check_and_increment("chan1", None, "peer", max_turns=3)
        self.assertEqual(connector._turn_store.current_turns("chan1", None, "peer"), 1)

        connector.on_agent_chain_drop("chan1", None, "peer")

        self.assertEqual(connector._turn_store.current_turns("chan1", None, "peer"), 0)

    def test_noop_when_no_turn_store(self):
        connector = _make_connector()  # no agent_chain configured -> no TurnStore
        self.assertIsNone(connector._turn_store)
        connector.on_agent_chain_drop("chan1", None, "peer")  # must not raise


# ── _on_posted_event ──────────────────────────────────────────────────────────


class TestOnPostedEvent(unittest.IsolatedAsyncioTestCase):
    async def _connector_with_channel(self, **config_overrides):
        connector = _make_connector(**config_overrides)
        room = Room(id="chan1", name="general", type="channel")
        connector._ws.register_channel = MagicMock()
        await connector.subscribe_room(room, watcher_id="w1")
        return connector

    async def test_a_frame_with_a_new_channel_name_renames_the_channel_state(self):
        """The frame's `channel_name` is the channel as it is NOW; the state's copy
        was taken at subscribe. The message built from the state then carries the
        new name to the lifecycle, which re-derives the handle (design §2.3)."""
        connector = await self._connector_with_channel()
        connector._rest.resolve_username = AsyncMock(return_value="glin")   # an owner: passes the allow-list
        handler = AsyncMock(return_value=True)
        connector.register_handler(handler)
        await connector._on_posted_event({
            "post": {"id": "p1", "channel_id": "chan1", "user_id": "u1",
                     "message": "@hammer.mei hi", "root_id": "", "type": "", "create_at": 1},
            "mentions": ["bot-id-1"], "channel_type": "O", "channel_name": "general-new",
        })
        self.assertEqual(connector._channels["chan1"].room.name, "general-new")
        self.assertEqual(connector._channels["chan1"].room.type, "channel")
        handler.assert_awaited_once()
        self.assertEqual(handler.await_args.args[0].room.name, "general-new",
                         "the message carries the current name to the lifecycle")

    async def test_a_dm_is_not_renamed_by_its_opaque_channel_name(self):
        connector = _make_connector()
        connector._ws.register_channel = MagicMock()
        await connector.subscribe_room(Room(id="dm1", name="alice", type="dm"), watcher_id="w1")
        connector._rest.resolve_username = AsyncMock(return_value="alice")
        connector.register_handler(AsyncMock(return_value=True))
        await connector._on_posted_event({
            "post": {"id": "p1", "channel_id": "dm1", "user_id": "u1", "message": "hi",
                     "root_id": "", "type": "", "create_at": 1},
            "mentions": [], "channel_type": "D", "channel_name": "u1__bot-id-1",
        })
        self.assertEqual(connector._channels["dm1"].room.name, "alice")

    async def test_a_replayed_post_carries_no_name_and_renames_nothing(self):
        connector = await self._connector_with_channel()
        connector.register_handler(AsyncMock(return_value=True))
        decoded = connector._synthesize_decoded_for_replay(
            {"id": "p1", "channel_id": "chan1", "user_id": "u1", "message": "hi",
             "root_id": "", "type": "", "create_at": 1})
        await connector._on_posted_event(decoded, is_replay=True, replay_after_ts="0")
        self.assertEqual(connector._channels["chan1"].room.name, "general")

    async def test_ignores_unsubscribed_channel(self):
        connector = _make_connector()
        received = []
        connector.register_handler(AsyncMock(side_effect=lambda m: received.append(m) or True))

        await connector._on_posted_event({
            "post": {"id": "p1", "channel_id": "unknown-chan", "user_id": "u1", "message": "@hammer.mei hi", "root_id": "", "type": "", "create_at": 1},
            "mentions": ["bot-id-1"],
        })

        self.assertEqual(received, [])

    async def test_a_system_message_in_an_unknown_channel_is_dropped(self):
        """Written before hoisting the system and own-message checks above the state
        lookup, so the reorder is demonstrably behaviour-preserving rather than
        argued to be.

        Today the state lookup discards this first; afterwards the type check does. The
        observable outcome — nothing dispatched, no username resolved — must not change.
        """
        connector = await self._connector_with_channel()
        handler = AsyncMock(return_value=True)
        connector.register_handler(handler)
        connector._rest.resolve_username = AsyncMock(
            side_effect=AssertionError("should not resolve for a system message"))

        await connector._on_posted_event({
            "post": {"channel_id": "unknown-chan", "id": "m1", "type": "system_join_channel",
                     "user_id": "u1", "message": "joined", "create_at": 1},
            "mentions": [], "channel_type": "O", "channel_name": "elsewhere", "team_id": "t1",
        })

        handler.assert_not_awaited()

    async def test_an_own_message_in_an_unknown_channel_is_dropped(self):
        connector = await self._connector_with_channel()
        handler = AsyncMock(return_value=True)
        connector.register_handler(handler)
        connector._rest.resolve_username = AsyncMock(
            side_effect=AssertionError("should not resolve own message"))

        await connector._on_posted_event({
            "post": {"channel_id": "unknown-chan", "id": "m1", "type": "",
                     "user_id": connector._rest.bot_user_id, "message": "hi", "create_at": 1},
            "mentions": [], "channel_type": "O", "channel_name": "elsewhere", "team_id": "t1",
        })

        handler.assert_not_awaited()

    async def test_own_message_skipped_before_resolve(self):
        connector = await self._connector_with_channel()
        connector._rest.resolve_username = AsyncMock(side_effect=AssertionError("should not resolve own message"))
        handler = AsyncMock(return_value=True)
        connector.register_handler(handler)

        await connector._on_posted_event({
            "post": {"id": "p1", "channel_id": "chan1", "user_id": "bot-id-1", "message": "pong", "root_id": "", "type": "", "create_at": 1},
            "mentions": [],
        })

        handler.assert_not_called()

    async def test_system_message_skipped(self):
        connector = await self._connector_with_channel()
        connector._rest.resolve_username = AsyncMock(side_effect=AssertionError("should not resolve system message"))
        handler = AsyncMock(return_value=True)
        connector.register_handler(handler)

        await connector._on_posted_event({
            "post": {"id": "p1", "channel_id": "chan1", "user_id": "u1", "message": "joined", "root_id": "", "type": "system_join_channel", "create_at": 1},
            "mentions": [],
        })

        handler.assert_not_called()

    async def test_accepted_message_dispatched_and_watermark_advanced(self):
        connector = await self._connector_with_channel(owners=["alice"])
        connector._rest.resolve_username = AsyncMock(return_value="alice")
        received = []

        async def handler(msg):
            received.append(msg)
            return True
        connector.register_handler(handler)

        await connector._on_posted_event({
            "post": {"id": "p1", "channel_id": "chan1", "user_id": "u1", "message": "@hammer.mei hi", "root_id": "", "type": "", "create_at": 12345},
            "mentions": ["bot-id-1"],
        })

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].text, "hi")
        self.assertEqual(connector.get_last_processed_ts("chan1"), "12345")

    async def test_dropped_message_leaves_an_owed_mark_not_an_advance(self):
        """A queue-full hand-back must not ADVANCE the watermark past the
        refused message — and since round 26 the getter answers the oldest
        OWED mark, so the hand-back's claimed window (just below the refused
        post) is what a shutdown would persist: the next boot replays it
        instead of losing it."""
        connector = await self._connector_with_channel(owners=["alice"])
        connector._rest.resolve_username = AsyncMock(return_value="alice")
        handler = AsyncMock(return_value=False)  # queue full
        connector.register_handler(handler)

        await connector._on_posted_event({
            "post": {"id": "p1", "channel_id": "chan1", "user_id": "u1", "message": "@hammer.mei hi", "root_id": "", "type": "", "create_at": 12345},
            "mentions": ["bot-id-1"],
        })

        self.assertEqual(connector.get_last_processed_ts("chan1"), "12344",
                         "the owed window, never a mark at-or-above the "
                         "refused message")
        state = connector._channels["chan1"]
        self.assertIsNone(state.last_processed_ts,
                          "the PROCESSED mark itself did not advance")

    async def test_duplicate_message_id_skipped(self):
        connector = await self._connector_with_channel(owners=["alice"])
        connector._rest.resolve_username = AsyncMock(return_value="alice")
        handler = AsyncMock(return_value=True)
        connector.register_handler(handler)

        post = {"id": "p1", "channel_id": "chan1", "user_id": "u1", "message": "@hammer.mei hi", "root_id": "", "type": "", "create_at": 12345}
        await connector._on_posted_event({"post": post, "mentions": ["bot-id-1"]})
        await connector._on_posted_event({"post": post, "mentions": ["bot-id-1"]})

        self.assertEqual(handler.call_count, 1)

    async def test_busy_notification_suppressed_during_replay(self):
        connector = await self._connector_with_channel(owners=["alice"])
        connector._rest.resolve_username = AsyncMock(return_value="alice")
        connector._rest.post_message = AsyncMock()
        connector.register_handler(AsyncMock(return_value=True))
        connector.register_capacity_check(lambda room_id: RoomCapacity.FULL)

        await connector._on_posted_event(
            {
                "post": {"id": "p1", "channel_id": "chan1", "user_id": "u1", "message": "@hammer.mei hi", "root_id": "", "type": "", "create_at": 1},
                "mentions": ["bot-id-1"],
            },
            is_replay=True,
        )

        connector._rest.post_message.assert_not_called()

    async def test_busy_notification_sent_when_not_replay(self):
        connector = await self._connector_with_channel(owners=["alice"])
        connector._rest.resolve_username = AsyncMock(return_value="alice")
        connector._rest.post_message = AsyncMock()
        connector.register_handler(AsyncMock(return_value=True))
        connector.register_capacity_check(lambda room_id: RoomCapacity.FULL)

        await connector._on_posted_event({
            "post": {"id": "p1", "channel_id": "chan1", "user_id": "u1", "message": "@hammer.mei hi", "root_id": "", "type": "", "create_at": 1},
            "mentions": ["bot-id-1"],
        })

        connector._rest.post_message.assert_called_once()

    async def test_concurrent_delivery_of_same_message_dispatches_once(self):
        """Regression test for a race fixed in code review: resolve_username's
        await used to sit between the seen_ids dedup check and registration,
        so two concurrent deliveries of the identical message (e.g. a live WS
        event racing a reconnect-replay of the same post) both passed the
        dedup check and both reached the handler. Registration now happens
        before that await, so a real yield point inside resolve_username
        (forced here via asyncio.sleep) must not let the second call through.
        """
        connector = await self._connector_with_channel(owners=["alice"])

        async def slow_resolve_username(user_id):
            await asyncio.sleep(0.01)  # force a genuine yield point
            return "alice"

        connector._rest.resolve_username = AsyncMock(side_effect=slow_resolve_username)
        received = []

        async def handler(msg):
            received.append(msg)
            return True

        connector.register_handler(handler)
        # Requires a mention to pass filter_mm_message's require_mention gate
        # (default True), so mentions=["bot-id-1"] is needed — but that also
        # makes normalize_mm_message call resolve_username("bot-id-1") once
        # (unrelated: resolving the mention for msg.mentions), separately
        # from resolve_username("u1") for the sender. Only the latter is what
        # the race duplicates, so assert on calls-for-the-sender-id
        # specifically rather than total call_count.
        decoded = {
            "post": {"id": "p1", "channel_id": "chan1", "user_id": "u1", "message": "@hammer.mei hi", "root_id": "", "type": "", "create_at": 1},
            "mentions": ["bot-id-1"],
        }

        await asyncio.gather(
            connector._on_posted_event(decoded),
            connector._on_posted_event(decoded, is_replay=True, replay_after_ts=None),
        )

        self.assertEqual(len(received), 1, "message must dispatch exactly once under concurrent delivery")
        sender_resolutions = [
            c for c in connector._rest.resolve_username.call_args_list if c.args == ("u1",)
        ]
        self.assertEqual(
            len(sender_resolutions), 1,
            "sender identity must be resolved exactly once, not once per concurrent delivery",
        )


# ── _on_ws_reconnect (history replay) ─────────────────────────────────────────


class TestOnWsReconnect(unittest.IsolatedAsyncioTestCase):
    async def _connector_with_channel(self, watermark=None, **config_overrides):
        connector = _make_connector(**config_overrides)
        room = Room(id="chan1", name="general", type="channel")
        connector._ws.register_channel = MagicMock()
        await connector.subscribe_room(room, watcher_id="w1")
        if watermark is not None:
            connector.update_last_processed_ts("chan1", watermark)
        return connector

    async def test_skips_channel_with_no_watermark(self):
        connector = await self._connector_with_channel(watermark=None)
        connector._rest.get_room_history = AsyncMock()

        await connector._on_ws_reconnect()

        connector._rest.get_room_history.assert_not_called()

    async def test_replays_missed_messages_via_rest_history(self):
        connector = await self._connector_with_channel(watermark="1000", owners=["alice"])
        connector._rest.resolve_username = AsyncMock(return_value="alice")
        connector._rest.get_room_history = AsyncMock(return_value=[
            {"id": "p1", "channel_id": "chan1", "user_id": "u1", "message": "@hammer.mei hi", "root_id": "", "type": "", "create_at": 1500},
        ])
        received = []

        async def handler(msg):
            received.append(msg)
            return True

        connector.register_handler(handler)

        await connector._on_ws_reconnect()

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].text, "hi")

    async def test_history_fetched_with_raw_epoch_ms_watermark_not_iso(self):
        """The internal replay watermark is an epoch-ms string (matching
        post['create_at']'s native units) — it must be passed to
        get_room_history() untouched, NOT converted to/from ISO."""
        connector = await self._connector_with_channel(watermark="1234567890")
        connector._rest.get_room_history = AsyncMock(return_value=[])

        await connector._on_ws_reconnect()

        connector._rest.get_room_history.assert_called_once_with(
            "chan1", count=connector._REPLAY_HISTORY_COUNT, after_ts="1234567890"
        )

    async def test_replay_uses_snapshotted_watermark_not_live_one(self):
        """If state.last_processed_ts changes while get_room_history() is
        in flight (e.g. a concurrent live message advances it), the replay
        loop must keep filtering against the watermark it captured at the
        start of the iteration — not the mutated live value — so in-window
        replayed messages aren't wrongly rejected as already-processed."""
        connector = await self._connector_with_channel(watermark="1000", owners=["alice"])
        connector._rest.resolve_username = AsyncMock(return_value="alice")

        async def mutate_watermark_then_return(*args, **kwargs):
            # Simulate a concurrent live message advancing the watermark
            # while this REST call is "in flight".
            connector.update_last_processed_ts("chan1", "9999999999")
            return [
                {"id": "p1", "channel_id": "chan1", "user_id": "u1", "message": "@hammer.mei hi", "root_id": "", "type": "", "create_at": 1500},
            ]

        connector._rest.get_room_history = AsyncMock(side_effect=mutate_watermark_then_return)
        received = []

        async def handler(msg):
            received.append(msg)
            return True

        connector.register_handler(handler)

        await connector._on_ws_reconnect()

        self.assertEqual(
            len(received), 1,
            "message must still be replayed using the snapshotted watermark, "
            "not incorrectly filtered against the mutated live watermark",
        )

    async def test_stops_replaying_channel_unsubscribed_mid_loop(self):
        connector = await self._connector_with_channel(watermark="1000", owners=["alice"])
        connector._rest.resolve_username = AsyncMock(return_value="alice")
        connector._rest.get_room_history = AsyncMock(return_value=[
            {"id": "p1", "channel_id": "chan1", "user_id": "u1", "message": "@hammer.mei one", "root_id": "", "type": "", "create_at": 1500},
            {"id": "p2", "channel_id": "chan1", "user_id": "u1", "message": "@hammer.mei two", "root_id": "", "type": "", "create_at": 1600},
        ])
        received = []

        async def handler(msg):
            received.append(msg)
            connector._channels.pop("chan1", None)  # simulate unsubscribe mid-replay
            return True

        connector.register_handler(handler)

        await connector._on_ws_reconnect()

        self.assertEqual(len(received), 1)  # second message's replay was skipped


# ── fetch_room_history: ISO <-> epoch-ms boundary ────────────────────────────


class TestFetchRoomHistoryTimestampWiring(unittest.IsolatedAsyncioTestCase):
    async def test_epoch_ms_bounds_reach_rest_unchanged(self):
        """Inverted from "ISO converted to epoch-ms" (§5.2).

        The bounds are epoch milliseconds, like every timestamp inside AgentCoop, and
        that is what this REST client wants natively — so nothing converts. The
        old conversion *raised* on an epoch-ms value, and the caller's blanket
        `except` turned that into "starting without history": the same bound
        that worked on Rocket.Chat, whose normalizer tolerates both forms,
        silently cost every Mattermost recreation its handoff.
        """
        connector = _make_connector()
        connector._rest.get_room_history = AsyncMock(return_value=[])

        await connector.fetch_room_history(
            Room(id="chan1", name="general", type="channel"),
            count=10,
            before_ts="1767225600000",
            after_ts="1767139200000",
        )

        connector._rest.get_room_history.assert_called_once_with(
            "chan1", 10,
            before_ts="1767225600000",
            after_ts="1767139200000",
        )

    async def test_none_before_after_passed_through_as_none(self):
        connector = _make_connector()
        connector._rest.get_room_history = AsyncMock(return_value=[])

        await connector.fetch_room_history(
            Room(id="chan1", name="general", type="channel"), count=10,
        )

        connector._rest.get_room_history.assert_called_once_with(
            "chan1", 10, before_ts=None, after_ts=None,
        )

    async def test_history_results_mapped_with_role_and_display_username(self):
        connector = _make_connector(owners=["alice"], guests=["bob"])
        connector._rest.get_room_history = AsyncMock(return_value=[
            {"user_id": "u-bot", "create_at": 1000, "message": "pong"},
            {"user_id": "u-alice", "create_at": 2000, "message": "hi"},
            {"user_id": "u-mallory", "create_at": 3000, "message": "spam"},
        ])
        connector._rest.resolve_username = AsyncMock(side_effect=lambda uid: {
            "u-bot": "hammer.mei", "u-alice": "alice", "u-mallory": "mallory",
        }[uid])

        result = await connector.fetch_room_history(
            Room(id="chan1", name="general", type="channel"), count=10,
        )

        usernames = {r["username"] for r in result}
        self.assertIn("me", usernames)     # bot's own message
        self.assertIn("alice", usernames)  # owner
        self.assertNotIn("mallory", usernames)  # unlisted sender excluded


if __name__ == "__main__":
    unittest.main()


class TestRoutingUntrackedChannels(unittest.IsolatedAsyncioTestCase):
    """"Unknown channel" stops being the end of the road (§2.2).

    The connector used to discard an event for a channel no watcher tracked. It now offers
    a `RoomRef` to a router, when one is registered — deciding whether a watcher should
    exist is the core's business, and creating one is a later increment's.
    """

    async def _connector(self, register_router=True):
        connector = _make_connector()
        # The team this connector serves. Left as a MagicMock the gate correctly refuses
        # everything, which is how the first run of these tests failed.
        connector._rest.team_id = "team-1"
        # An allow-listed sender, resolved. Without this the sender gate refuses — which
        # is the point of the gate, and has its own test below.
        connector._rest.resolve_username = AsyncMock(return_value="glin")
        connector._ws.register_channel = MagicMock()
        connector._ws.unregister_channel = MagicMock()
        connector.register_handler(AsyncMock(return_value=True))
        self.offered = []
        self.triggers = []
        if register_router:
            connector.register_router(AsyncMock(side_effect=self._record_offer))
        return connector

    def _record_offer(self, room, trigger):
        # The router contract is `router(room, trigger)` — the trigger is the decoded
        # event, kept so the creation path can bound history handoff by its timestamp.
        self.offered.append(room)
        self.triggers.append(trigger)

    async def _drain(self, connector):
        """Wait out the off-handler routing tasks the offer spawned."""
        while connector._routing_tasks:
            await asyncio.gather(*list(connector._routing_tasks))

    def _event(self, **overrides):
        event = {
            "post": {"channel_id": "chan-new", "id": "m1", "type": "",
                     "user_id": "u1", "message": "hello", "create_at": 1},
            "mentions": [],
            "channel_type": "O",
            "channel_name": "incident-42",
            "channel_display_name": "Incident 42",
            "team_id": "team-1",
        }
        event.update(overrides)
        return event

    async def test_an_untracked_channel_is_offered_to_the_router(self):
        connector = await self._connector()
        event = self._event()
        await connector._on_posted_event(event)
        await self._drain(connector)

        self.assertEqual(len(self.offered), 1)
        room = self.offered[0]
        self.assertEqual(room.id, "chan-new")
        self.assertEqual(room.kind, RoomKind.CHANNEL)
        self.assertEqual(room.name, "incident-42")
        # The trigger rides along, so the creation path can bound history handoff
        # by its timestamp — same contract as Rocket.Chat's router.
        self.assertIs(self.triggers[0], event)

    async def test_the_offer_does_not_hold_the_handler_path(self):
        """The router runs off the handler (§2.7 step 3): the channel worker holds the
        connector-wide permit for the whole handler call, so a creation awaited inside
        it would stall delivery for every channel."""
        connector = await self._connector()
        release = asyncio.Event()

        async def slow_router(room, trigger):
            await release.wait()

        connector.register_router(slow_router)

        # Returns while the router is still blocked — the offer was spawned, not awaited.
        await asyncio.wait_for(connector._on_posted_event(self._event()), timeout=1)
        self.assertEqual(len(connector._routing_tasks), 1)

        release.set()
        await self._drain(connector)

    async def test_a_second_frame_during_a_creation_is_dropped_not_reoffered(self):
        """Single-flight per channel: an offer in flight means a second offer would
        create a second watcher. The dropped frame is the stated residue of this
        coalescing — same rule as RC's `_on_unrouted_message`."""
        connector = await self._connector()
        release = asyncio.Event()
        calls = []

        async def slow_router(room, trigger):
            calls.append(room)
            await release.wait()

        connector.register_router(slow_router)

        await connector._on_posted_event(self._event())
        await connector._on_posted_event(
            self._event(post={"channel_id": "chan-new", "id": "m2", "type": "",
                              "user_id": "u1", "message": "second", "create_at": 2}))
        release.set()
        await self._drain(connector)

        self.assertEqual(len(calls), 1)

    async def test_the_trigger_is_handed_back_once_the_channel_is_tracked(self):
        """A brand-new channel has no watermark for a replay to fetch from, so the
        message that caused the watcher to exist would otherwise be the one message
        it never sees. Handed back through the channel's own queue, so every gate a
        tracked message passes applies to it too."""
        connector = await self._connector()
        connector._ws.deliver_to_channel = MagicMock()
        event = self._event()

        async def creating_router(room, trigger):
            # What a real creation does that matters here: the channel becomes tracked.
            connector._channels["chan-new"] = MagicMock()

        connector.register_router(creating_router)
        await connector._on_posted_event(event)
        await self._drain(connector)

        connector._ws.deliver_to_channel.assert_called_once_with(event)

    async def test_a_handed_back_frame_is_promised_not_claimed_and_a_rejection_claims(self):
        """Same contract as Rocket.Chat's creation path. The episode used to claim
        a boundary below the frames it handed back; the claim was never
        discharged when they were accepted, so shutdown persisted a boundary at
        the channel's creation and every restart replayed — and re-answered —
        everything since (measured: three rooms). Now: promised on hand-back,
        claimed only when the filter rejects one as already processed."""
        from gateway.connectors.mattermost.connector import _ChannelState

        connector = await self._connector()
        connector._ws.deliver_to_channel = MagicMock()
        # Mentioning the bot: the mention gate runs BEFORE the timestamp dedup,
        # and a frame it would refuse anyway is no loss and claims nothing.
        event = self._event(mentions=["bot-id-1"])

        async def creating_router(room, trigger):
            state = _ChannelState(room=Room(id="chan-new", name="incident-42", type="channel"))
            state.last_processed_ts = "9999"   # a live message got in first
            connector._channels["chan-new"] = state

        connector.register_router(creating_router)
        await connector._on_posted_event(event)
        await self._drain(connector)
        state = connector._channels["chan-new"]

        self.assertIn("m1", state.promised_ids)
        self.assertIsNone(state.replay_boundary)
        self.assertEqual(state.boundary_claims, 0)

        # The queue worker delivers the handed-back frame; it falls below the
        # watermark and is rejected — the window opens now.
        handed_back = connector._ws.deliver_to_channel.call_args.args[0]
        await connector._on_posted_event(handed_back)

        self.assertEqual(state.replay_boundary, "0", "just below the frame's create_at=1")
        self.assertEqual(state.boundary_claims, 1)
        self.assertNotIn("m1", state.promised_ids)

    async def test_an_accepted_hand_back_leaves_the_live_watermark_for_shutdown(self):
        """The regression: creation, frames accepted, no replay — and
        `get_last_processed_ts` (what shutdown persists) must be the live mark,
        not a boundary at the channel's creation."""
        from gateway.connectors.mattermost.connector import _ChannelState

        connector = await self._connector()
        connector._ws.deliver_to_channel = MagicMock()

        async def creating_router(room, trigger):
            connector._channels["chan-new"] = _ChannelState(
                room=Room(id="chan-new", name="incident-42", type="channel"))

        connector.register_router(creating_router)
        await connector._on_posted_event(self._event(mentions=["bot-id-1"]))
        await self._drain(connector)
        state = connector._channels["chan-new"]
        self.assertIn("m1", state.promised_ids)

        handed_back = connector._ws.deliver_to_channel.call_args.args[0]
        await connector._on_posted_event(handed_back)   # no watermark to fall below: accepted

        self.assertEqual(state.promised_ids, set())
        self.assertEqual(state.boundary_claims, 0)
        self.assertTrue(state.last_processed_ts, "the accepted frame advanced the mark")
        self.assertEqual(connector.get_last_processed_ts("chan-new"), state.last_processed_ts)

    async def test_no_hand_back_when_the_router_declines(self):
        """A router that creates nothing (no rule matched) leaves the frame dropped —
        delivering it would mean delivering to nobody."""
        connector = await self._connector()
        connector._ws.deliver_to_channel = MagicMock()

        await connector._on_posted_event(self._event())
        await self._drain(connector)

        connector._ws.deliver_to_channel.assert_not_called()

    async def test_frames_arriving_during_creation_drain_in_arrival_order(self):
        """Same episode rule as RC's (§2.2): buffered while open, drained
        trigger-first onto the channel's own queue when it becomes tracked."""
        connector = await self._connector()
        connector._ws.deliver_to_channel = MagicMock()
        release = asyncio.Event()
        entered = asyncio.Event()

        async def creating_router(room, trigger):
            entered.set()
            await release.wait()
            connector._channels["chan-new"] = MagicMock()

        connector.register_router(creating_router)

        def _evt(msg_id, text):
            return self._event(post={"channel_id": "chan-new", "id": msg_id,
                                     "type": "", "user_id": "u1",
                                     "message": text, "create_at": 1})

        await connector._on_posted_event(_evt("m1", "first"))
        await entered.wait()
        await connector._on_posted_event(_evt("m2", "second"))
        await connector._on_posted_event(_evt("m2", "second-copy"))  # duplicate
        await connector._on_posted_event(_evt("m3", "third"))
        release.set()
        await self._drain(connector)

        delivered = [c.args[0]["post"]["id"]
                     for c in connector._ws.deliver_to_channel.call_args_list]
        self.assertEqual(delivered, ["m1", "m2", "m3"])

    async def test_a_full_buffer_is_audible_in_the_room_once(self):
        connector = await self._connector()
        connector._PENDING_BUFFER_DEPTH = 1
        connector.send_text = AsyncMock()
        release = asyncio.Event()
        entered = asyncio.Event()

        async def slow_router(room, trigger):
            entered.set()
            await release.wait()

        connector.register_router(slow_router)

        def _evt(msg_id):
            return self._event(post={"channel_id": "chan-new", "id": msg_id,
                                     "type": "", "user_id": "u1",
                                     "message": "x", "create_at": 1})

        await connector._on_posted_event(_evt("m1"))
        await entered.wait()
        await connector._on_posted_event(_evt("m2"))  # full
        await connector._on_posted_event(_evt("m3"))  # full again — no second notice
        release.set()
        await self._drain(connector)

        connector.send_text.assert_awaited_once()
        self.assertEqual(connector.send_text.await_args.args[0], "chan-new")

    async def test_a_transient_creation_failure_recovers_in_place(self):
        connector = await self._connector()
        connector._ROUTE_RETRY_DELAYS = (0,)
        connector._ws.deliver_to_channel = MagicMock()
        calls = []

        async def flaky_router(room, trigger):
            calls.append(1)
            if len(calls) < 2:
                raise RuntimeError("backend hiccup")
            connector._channels["chan-new"] = MagicMock()

        connector.register_router(flaky_router)
        await connector._on_posted_event(self._event())
        await self._drain(connector)

        self.assertEqual(len(calls), 2)
        self.assertEqual(connector._ws.deliver_to_channel.call_count, 1,
                         "the trigger survived the first failure")

    async def test_a_failed_offer_leaves_the_channel_offerable_again(self):
        """The single-flight reservation is released whatever happened — holding it
        would make one transient failure permanent for that channel."""
        connector = await self._connector()
        connector._ROUTE_RETRY_DELAYS = ()
        connector.register_router(AsyncMock(side_effect=RuntimeError("boom")))

        with self.assertLogs("coop.connectors.mattermost", "WARNING"):
            await connector._on_posted_event(self._event())
            await self._drain(connector)
        self.assertEqual(connector._pending_routes, {})

        # And the next message for the same channel is offered again.
        connector.register_router(AsyncMock(side_effect=self._record_offer))
        await connector._on_posted_event(self._event())
        await self._drain(connector)
        self.assertEqual(len(self.offered), 1)

    async def test_with_no_router_registered_nothing_changes(self):
        """What keeps this branch runnable while creation is still driven by static
        config: the connector behaves exactly as it did before."""
        connector = await self._connector(register_router=False)
        await connector._on_posted_event(self._event())
        self.assertEqual(self.offered, [])

    async def test_a_dm_is_described_by_its_display_name(self):
        """`channel_name` is the opaque `<userid>__<userid>` form; the display name is the
        counterpart (§6.2) — and it arrives `@`-prefixed, which is dropped.

        This test fed `channel_display_name="alice"` until a live `list --all` showed the
        watcher was really called `mm-e2e:dm:%40test_user`. Mattermost sends `@alice` here;
        the bare form the test used never comes off the wire, so both the assertion and the
        code it guarded described something that does not happen.
        """
        connector = await self._connector()
        await connector._on_posted_event(
            self._event(channel_type="D", channel_name="u1__u2",
                        channel_display_name="@alice", team_id=""))
        await self._drain(connector)

        room = self.offered[0]
        self.assertEqual(room.kind, RoomKind.DM)
        self.assertEqual(room.participants, ("alice",))
        self.assertEqual(room.name, "", "a DM has no usable platform name")

    async def test_a_dm_with_no_display_name_has_no_counterpart(self):
        """The case the membership-add path really produces.

        An earlier version of this test fed `channel_display_name="alice"` and
        justified it as what REST supplies — but REST's `display_name` for a direct
        channel is the EMPTY string, so a bare name on a DM has no producer at all.
        Empty is the value to cover, and it must yield no participant rather than one
        empty participant: `("",)` would make `room_label` emit a nameless `dm:`
        instead of falling back to the room-id digest.
        """
        connector = await self._connector()
        await connector._on_posted_event(
            self._event(channel_type="D", channel_name="u1__u2",
                        channel_display_name="", team_id=""))
        await self._drain(connector)
        self.assertEqual(self.offered[0].participants, ())

    async def test_a_dm_whose_display_name_is_only_the_prefix_has_no_counterpart(self):
        """Guarding the pre-strip value would pass `"@"` through as `("",)`. Unreachable
        for a real username, but it is the difference between testing the transformed
        value and the raw one."""
        connector = await self._connector()
        await connector._on_posted_event(
            self._event(channel_type="D", channel_name="u1__u2",
                        channel_display_name="@", team_id=""))
        await self._drain(connector)
        self.assertEqual(self.offered[0].participants, ())

    async def test_a_dm_joined_by_membership_event_has_no_counterpart(self):
        """`_room_ref_from_event` has TWO callers and every other DM test here
        drives only the websocket one.

        `_handle_membership_add` builds its own `decoded` dict from a REST
        `get_channel()` response, mapping `channel_display_name` from the
        channel's `display_name` — which is the EMPTY string for a direct
        channel. Swap that mapping to `chan["name"]` and a DM joined this way
        registers as `dm:<userid>__<userid>`, an opaque id pair standing in for
        a person, persisted and operable. Nothing caught that: the membership
        tests mock `get_channel` as a public channel only.
        """
        connector = await self._connector()
        room = connector._room_ref_from_event(
            "dm-chan-id",
            {
                "channel_type": "D",
                "channel_name": "u1__u2",
                "channel_display_name": "",
                "team_id": "",
            },
        )
        self.assertIsNotNone(room)
        self.assertEqual(room.kind, RoomKind.DM)
        self.assertEqual(
            room.participants, (),
            "the opaque channel_name must not become the counterpart",
        )
        self.assertEqual(room.name, "", "a DM has no usable platform name")

    async def test_a_prefixed_group_dm_entry_would_still_be_stripped(self):
        """Defensive, and labelled as such: real group DM events are NOT
        prefixed (verified on 11.7.0, §6.4), so `bare_handle` is a no-op on
        that branch and its removal is invisible to every other test. This pins
        the intent — if Mattermost ever changes its mind, or a future edit
        'simplifies' the comprehension, the two branches must not diverge.

        The input here is hypothetical. That is stated rather than implied,
        because asserting it as an observation is the mistake this whole area
        already made once.
        """
        connector = await self._connector()
        room = connector._room_ref_from_event(
            "gdm-id",
            {
                "channel_type": "G",
                "channel_name": "1b4c4b32",
                "channel_display_name": "@glin, @alice",
                "team_id": "",
            },
        )
        self.assertEqual(room.participants, ("glin", "alice"))

    async def test_a_group_dm_carries_its_members(self):
        """Group DM display names are NOT `@`-prefixed, unlike DMs — Mattermost is
        inconsistent between the two kinds, and this is the form observed on 11.7.0
        (§6.4). A previous version of this test asserted the prefixed form, which no
        group DM event has ever sent."""
        connector = await self._connector()
        await connector._on_posted_event(
            self._event(channel_type="G", channel_name="1b4c4b32",
                        channel_display_name="glin, probe-bot, alice", team_id=""))
        await self._drain(connector)

        room = self.offered[0]
        self.assertEqual(room.kind, RoomKind.GROUP_DM)
        self.assertEqual(room.participants, ("glin", "probe-bot", "alice"))

    async def test_another_team_is_not_offered(self):
        """The socket spans every team the account belongs to (§6.3), so a connector
        scoped to one team discards the others itself."""
        connector = await self._connector()
        connector._rest.team_id = "team-1"
        await connector._on_posted_event(self._event(team_id="team-2"))
        self.assertEqual(self.offered, [])

    async def test_a_dm_passes_the_team_gate(self):
        """A DM belongs to no team, so gating on equality would disable DM support by way
        of a team filter — a silent loss."""
        connector = await self._connector()
        connector._rest.team_id = "team-1"
        await connector._on_posted_event(
            self._event(channel_type="D", channel_display_name="alice", team_id=""))
        await self._drain(connector)
        self.assertEqual(len(self.offered), 1)

    async def test_a_replayed_event_is_not_offered_and_not_swallowed(self):
        """The trap this PR had to avoid.

        The reconnect path synthesizes events from REST history, which carries bare posts:
        `channel_type`, `channel_name` and `team_id` are all None. A team gate reading
        those would drop the entire replay window, and a router asked to judge one would be
        judging None. Replayed posts belong to channels already tracked, so they reach the
        normal path — this only asserts they are never mistaken for a routing candidate.
        """
        connector = await self._connector()
        connector._rest.team_id = "team-1"
        await connector._on_posted_event(
            self._event(channel_type=None, channel_name=None,
                        channel_display_name=None, team_id=None),
            is_replay=True,
        )
        self.assertEqual(self.offered, [])

    async def test_a_system_message_is_never_offered(self):
        """An unknown channel is now a routing candidate, which is exactly why these two
        checks moved above the channel lookup: a join notification is not a reason to
        create a watcher."""
        connector = await self._connector()
        await connector._on_posted_event(
            self._event(post={"channel_id": "chan-new", "id": "m1",
                              "type": "system_join_channel", "user_id": "u1",
                              "message": "joined", "create_at": 1}))
        self.assertEqual(self.offered, [])

    async def test_an_own_message_is_never_offered(self):
        connector = await self._connector()
        await connector._on_posted_event(
            self._event(post={"channel_id": "chan-new", "id": "m1", "type": "",
                              "user_id": connector._rest.bot_user_id,
                              "message": "hi", "create_at": 1}))
        self.assertEqual(self.offered, [])

    async def test_a_sender_outside_the_allow_list_cannot_cause_creation(self):
        """§2.7 step 1 puts the sender allow-list among the cheap rejects, above the
        room-state lookup, and this is why: someone who cannot start a turn must not be
        able to cause a watcher and a backend session to exist.

        Note what this does *not* claim. Creating a watcher early is not itself a fault —
        an agent invited to a room will be spoken to eventually, and an existing watcher
        makes that first real request faster. The objection is specifically to a sender the
        operator excluded being able to cause it.
        """
        connector = await self._connector()
        connector._rest.resolve_username = AsyncMock(return_value="stranger")

        await connector._on_posted_event(self._event())

        self.assertEqual(self.offered, [])

    async def test_an_unresolvable_sender_does_not_cause_creation(self):
        """Fail closed: if the username cannot be resolved, the allow-list cannot be
        applied, and an unapplied allow-list must not read as permission."""
        connector = await self._connector()
        connector._rest.resolve_username = AsyncMock(side_effect=RuntimeError("api down"))

        await connector._on_posted_event(self._event())

        self.assertEqual(self.offered, [])

    async def test_an_agent_sender_bypasses_the_allow_list(self):
        """An agent-to-agent chain is authorised by being in `agent_usernames`, not by
        appearing in a human allow-list."""
        from gateway.core.agent_chain import AgentChainConfig

        connector = _make_connector(
            agent_chain=AgentChainConfig(agent_usernames=["other-bot"]))
        connector._rest.team_id = "team-1"
        connector._rest.resolve_username = AsyncMock(return_value="other-bot")
        connector._ws.register_channel = MagicMock()
        connector.register_handler(AsyncMock(return_value=True))
        offered = []
        connector.register_router(
            AsyncMock(side_effect=lambda room, trigger: offered.append(room)))

        await connector._on_posted_event(self._event())
        while connector._routing_tasks:
            await asyncio.gather(*list(connector._routing_tasks))

        self.assertEqual(len(offered), 1)

    async def test_a_router_failure_does_not_break_delivery(self):
        connector = await self._connector()
        connector._ROUTE_RETRY_DELAYS = ()
        connector.register_router(AsyncMock(side_effect=RuntimeError("boom")))
        with self.assertLogs("coop.connectors.mattermost", "WARNING"):
            await connector._on_posted_event(self._event())
            await self._drain(connector)


class TestProbeMissedSince(unittest.IsolatedAsyncioTestCase):
    """Same two confounders as Rocket.Chat's probe: the bot's own posts (which
    history includes by design, while the watermark only advances on accepted
    inbound) and the inclusive boundary message."""

    def _connector(self, posts, *, was_full=False):
        from gateway.core.connector import HistoryPage

        connector = _make_connector()
        connector._rest.get_room_history_page = AsyncMock(return_value=HistoryPage(
            messages=posts,
            raw_count=connector._REPLAY_HISTORY_COUNT if was_full else len(posts),
            limit=connector._REPLAY_HISTORY_COUNT,
        ))
        return connector

    _ROOM = Room(id="c1", name="eng", type="channel")

    def _post(self, ms, user_id="u1"):
        return {"id": f"m{ms}", "user_id": user_id, "message": "hi", "create_at": ms}

    async def test_the_bots_own_post_above_the_watermark_is_not_a_gap(self):
        connector = self._connector([self._post(2000, user_id="bot-id-1")])
        self.assertFalse(await connector.probe_missed_since(self._ROOM, "1000"))

    async def test_the_boundary_post_itself_is_not_a_gap(self):
        connector = self._connector([self._post(1000)])
        self.assertFalse(await connector.probe_missed_since(self._ROOM, "1000"))

    async def test_a_real_user_post_above_the_watermark_is_a_gap(self):
        connector = self._connector([self._post(1000), self._post(2000)])
        self.assertTrue(await connector.probe_missed_since(self._ROOM, "1000"))

    async def test_a_page_filled_by_system_posts_is_a_gap(self):
        """Same trap as Rocket.Chat's: the count is applied before the filter."""
        connector = self._connector([], was_full=True)
        self.assertTrue(await connector.probe_missed_since(self._ROOM, "1000"))


class TestTriggerHistoryBound(unittest.TestCase):
    """Mattermost's trigger is the decoded event; the bound is post.create_at."""

    def test_create_at_reads_through_as_epoch_ms(self):
        """`create_at` is already the internal representation (§5.2), so this
        reads it rather than converting it."""
        connector = _make_connector(timezone="UTC")
        bound = connector.trigger_history_bound(
            {"post": {"id": "m1", "create_at": 1786874400000}})
        self.assertEqual(bound, "1786874400000")

    def test_a_missing_or_garbled_frame_answers_none(self):
        connector = _make_connector(timezone="UTC")
        self.assertIsNone(connector.trigger_history_bound({"post": {}}))
        self.assertIsNone(connector.trigger_history_bound({}))
        self.assertIsNone(connector.trigger_history_bound(None))
        # A non-numeric create_at is not something this connector can produce;
        # it is passed through as the string it is rather than guessed at.
        self.assertEqual(
            connector.trigger_history_bound({"post": {"create_at": "soon"}}), "soon")


class TestReaping(unittest.IsolatedAsyncioTestCase):
    """Reaping is the room going away; unsubscribing is a watcher releasing it."""

    async def _connector_with_channel(self):
        connector = _make_connector()
        connector._ws.register_channel = MagicMock()
        connector._ws.unregister_channel = MagicMock()
        await connector.subscribe_room(
            Room(id="chan1", name="general", type="channel"), watcher_id="w1")
        await connector.subscribe_room(
            Room(id="chan1", name="general", type="channel"), watcher_id="w2")
        return connector

    async def test_reaping_drops_the_state_even_with_watchers_holding_it(self):
        """`unsubscribe_room` returns early while another watcher holds the room, which is
        the right answer for a release and the wrong one for a room that is gone."""
        connector = await self._connector_with_channel()
        connector.reap_room("chan1")

        self.assertNotIn("chan1", connector._channels)
        connector._ws.unregister_channel.assert_called_once_with("chan1")

    async def test_reaping_an_unknown_channel_is_not_an_error(self):
        """The caller is reacting to an event that may arrive more than once."""
        connector = await self._connector_with_channel()
        connector.reap_room("never-seen")
        connector._ws.unregister_channel.assert_not_called()
        self.assertIn("chan1", connector._channels)


class TestRoomTypeMapping(unittest.TestCase):
    """All four Mattermost channel types, because the previous mapping knew one.

    It recognised `P` and called everything else a channel, which was harmless while only
    the `@username` path could produce a DM — and would stop being harmless the moment an
    id-based lookup existed, since a DM typed as a channel inverts every `type == "dm"`
    gate: the mention requirement would start applying to DMs, which §2.7 records as making
    the agent answer every message from anyone in the room.
    """

    def test_each_type_maps_to_its_own_room_type(self):
        from gateway.connectors.mattermost.rest import room_type_for

        self.assertEqual(room_type_for("O"), "channel")
        self.assertEqual(room_type_for("P"), "group")
        self.assertEqual(room_type_for("D"), "dm")
        self.assertEqual(room_type_for("G"), "group_dm")

    def test_an_unknown_type_falls_back_to_channel(self):
        """The conservative answer: a channel requires a mention where a DM does not, so
        guessing `channel` cannot turn a quiet room into a talkative one."""
        from gateway.connectors.mattermost.rest import room_type_for

        for value in (None, "", "X", "unknown"):
            with self.subTest(value=value):
                self.assertEqual(room_type_for(value), "channel")


class TestResolveRoomById(unittest.IsolatedAsyncioTestCase):
    """Boot and recreation resolve by id, never by a persisted name (§2.3).

    A name freed by a rename can be reused by a different room, so resolving by name would
    bind an existing session to the wrong one.
    """

    def _connector(self, channel, members=(), *, member_of=None):
        connector = _make_connector()
        connector._rest.team_id = channel.get("team_id", "")
        connector._rest.get_channel = AsyncMock(return_value=channel)
        connector._rest.channel_member_usernames = AsyncMock(return_value=list(members))
        # Membership is checked on this path now — a channel the bot was removed
        # from still resolves by id on Mattermost, so resolution asks. Defaults
        # to "a member of the channel under test" so the cases below stay about
        # naming; `member_of=set()` exercises the refusal.
        ids = {channel["id"]} if member_of is None else member_of
        connector._rest.get_member_channel_ids = AsyncMock(return_value=ids)
        return connector

    async def test_a_channel_keeps_its_platform_name(self):
        connector = self._connector(
            {"id": "c1", "name": "incident-42", "display_name": "Incident 42",
             "type": "channel"})
        room = await connector.resolve_room_by_id("c1")

        self.assertEqual((room.id, room.name, room.type), ("c1", "incident-42", "channel"))

    async def test_a_dm_is_described_by_its_members(self):
        """A DM's platform name is the opaque `<userid>__<userid>` form, and its REST
        `display_name` is **empty** — the counterpart handle Mattermost puts on a WebSocket
        event is viewer-specific and is not part of the channel object. So the members
        supply the description, which reaches the prompt prefix and the history header."""
        connector = self._connector(
            {"id": "d1", "name": "u1__u2", "display_name": "", "type": "dm"},
            members=["alice"])
        room = await connector.resolve_room_by_id("d1")

        self.assertEqual(room.name, "alice")
        self.assertEqual(room.type, "dm")

    async def test_a_group_dm_uses_its_display_name(self):
        connector = self._connector(
            {"id": "g1", "name": "1b4c4b32", "display_name": "", "type": "group_dm"},
            members=["glin", "alice"])
        room = await connector.resolve_room_by_id("g1")

        self.assertEqual(room.name, "glin, alice")
        self.assertEqual(room.type, "group_dm")

    async def test_a_channel_in_another_team_is_refused(self):
        """A channel id is globally unique, so this can reach a channel in a team the
        connector no longer serves — the bot account may still belong to both.

        Refused rather than answered, because the caller is usually boot recreating a
        persisted record: recreating one outside the configured team means answering in a
        room this connector was reconfigured away from, and nothing else would notice.

        `resolve_room_by_id` still raises, because its callers expect a `Room` or
        an error. The refusal itself moved into `_resolved_channel`, which
        answers `None` so that `room_ref_by_id` can honour its own contract —
        `None` for every PERMANENT absence, a raise only for a transport failure
        the caller could usefully retry.
        """
        from gateway.connectors.mattermost.rest import RoomNotFoundError

        connector = self._connector(
            {"id": "c1", "name": "eng", "display_name": "", "type": "channel",
             "team_id": "team-old"})
        connector._rest.team_id = "team-new"

        with self.assertRaises(RoomNotFoundError):
            await connector.resolve_room_by_id("c1")
        # And the by-id RoomRef path answers, rather than raising.
        self.assertIsNone(await connector.room_ref_by_id("c1"))

    async def test_a_channel_in_the_configured_team_is_allowed(self):
        connector = self._connector(
            {"id": "c1", "name": "eng", "display_name": "", "type": "channel",
             "team_id": "team-new"})
        connector._rest.team_id = "team-new"
        room = await connector.resolve_room_by_id("c1")
        self.assertEqual(room.name, "eng")

    async def test_a_dm_is_exempt_from_the_team_check(self):
        """A DM belongs to no team (§6.3), so there is nothing to compare — and comparing
        anyway would refuse every direct message on the recreation path."""
        connector = self._connector(
            {"id": "d1", "name": "u1__u2", "display_name": "", "type": "dm",
             "team_id": ""},
            members=["alice"])
        connector._rest.team_id = "team-new"
        room = await connector.resolve_room_by_id("d1")
        self.assertEqual(room.name, "alice")

    async def test_a_nameless_channel_falls_back_to_its_id(self):
        connector = self._connector(
            {"id": "c9", "name": "", "display_name": "", "type": "channel"})
        room = await connector.resolve_room_by_id("c9")

        self.assertEqual(room.name, "c9")


class TestAHandedBackPostGivesItsTurnBack(unittest.IsolatedAsyncioTestCase):
    """The filter spends a turn before anything knows the post can be delivered.

    Mattermost has a hand-back path — the `not accepted` branch forgets the id so a retry
    can bring the post back — and a comment in `normalize.py` asserted it did not. Without
    the release, every retry spends another turn; once the budget is gone the filter
    rejects the post as complete, so the message the retry exists for is the one it can
    never deliver.
    """

    async def _connector(self):
        connector = _make_connector(
            owners=["alice"],
            agent_chain=AgentChainConfig(agent_usernames=["peer"], max_turns=2),
        )
        room = Room(id="chan1", name="general", type="channel")
        connector._ws.register_channel = MagicMock()
        await connector.subscribe_room(room, watcher_id="w1")
        connector._rest.resolve_username = AsyncMock(return_value="peer")
        return connector

    def _post(self, post_id):
        return {
            "post": {"id": post_id, "channel_id": "chan1", "user_id": "u1",
                     "message": "@hammer.mei hi", "root_id": "", "type": "",
                     "create_at": 12345},
            "mentions": ["bot-id-1"],
        }

    async def test_a_rejected_post_does_not_spend_a_turn(self):
        connector = await self._connector()
        connector.register_handler(AsyncMock(return_value=False))   # queue full

        await connector._on_posted_event(self._post("p1"))

        self.assertEqual(
            connector._turn_store.current_turns("chan1", None, "peer"), 0,
            "the post was never delivered, so it took no turn",
        )

    async def test_retrying_past_the_budget_still_reaches_the_handler(self):
        """The failure this prevents: max_turns=2, so a third attempt used to be refused
        by the filter before the handler ever saw it."""
        connector = await self._connector()
        handler = AsyncMock(return_value=False)
        connector.register_handler(handler)

        for i in range(5):
            await connector._on_posted_event(self._post(f"p{i}"))

        self.assertEqual(handler.await_count, 5)

    async def test_a_delivered_post_keeps_its_turn(self):
        """The near miss: releasing unconditionally would uncap the chain."""
        connector = await self._connector()
        connector.register_handler(AsyncMock(return_value=True))

        await connector._on_posted_event(self._post("p1"))

        self.assertEqual(connector._turn_store.current_turns("chan1", None, "peer"), 1)


class TestAReplayedPostRejectedForCapacityStaysReplayable(unittest.IsolatedAsyncioTestCase):
    """Rocket.Chat has two hand-back sites; the parity sweep carried one.

    The preflight registers the message id before its `resolve_username` await, and the
    busy notice is suppressed for replays — so a replayed post rejected here is skipped by
    the next recovery's dedup check, the window is reported read, and nobody was told.
    """

    async def _connector(self, capacity):
        connector = _make_connector(
            owners=["alice"],
            agent_chain=AgentChainConfig(agent_usernames=["peer"], max_turns=5),
        )
        room = Room(id="chan1", name="general", type="channel")
        connector._ws.register_channel = MagicMock()
        await connector.subscribe_room(room, watcher_id="w1")
        connector._rest.resolve_username = AsyncMock(return_value="peer")
        connector.register_capacity_check(lambda *a, **kw: capacity)
        # Without this `_on_posted_event` returns on its first line and every assertion
        # below is vacuous. Two of these tests passed that way when first written — the
        # id was "forgotten" because it was never remembered, and the turn was "given
        # back" because it was never taken. Only the one asserting a *positive* fact
        # caught it, which is what the near-miss tests are for.
        connector.register_handler(AsyncMock(return_value=True))
        return connector

    def _event(self, post_id="p1"):
        return {"post": {"id": post_id, "channel_id": "chan1", "user_id": "u1",
                         "message": "@hammer.mei hi", "root_id": "", "type": "",
                         "create_at": 12345},
                "mentions": ["bot-id-1"]}

    async def test_the_id_is_forgotten_so_a_later_replay_can_bring_it_back(self):
        connector = await self._connector(RoomCapacity.FULL)
        state = connector._channels["chan1"]

        await connector._on_posted_event(
            self._event(), is_replay=True, replay_after_ts="1")

        self.assertNotIn(
            "p1", state.seen_ids_set,
            "a remembered id makes the next recovery skip it and close the window",
        )

    async def test_the_turn_it_spent_is_given_back(self):
        connector = await self._connector(RoomCapacity.FULL)

        await connector._on_posted_event(
            self._event(), is_replay=True, replay_after_ts="1")

        self.assertEqual(
            connector._turn_store.current_turns("chan1", None, "peer"), 0)

    async def test_a_live_rejection_keeps_its_id_but_still_returns_the_turn(self):
        """The sender was told and can resend, so the id stays — the turn does not."""
        connector = await self._connector(RoomCapacity.FULL)
        connector._rest.post_message = AsyncMock()
        state = connector._channels["chan1"]

        await connector._on_posted_event(self._event())

        self.assertIn("p1", state.seen_ids_set)
        self.assertTrue(connector._rest.post_message.await_count)
        self.assertEqual(
            connector._turn_store.current_turns("chan1", None, "peer"), 0)


class TestTheMattermostWatermarkNeverMovesBackwards(unittest.IsolatedAsyncioTestCase):
    """Replay calls `_on_posted_event` directly, so it is not serialized against live.

    A replayed post awaiting its attachment download can be overtaken by live traffic that
    commits a newer cursor; an unconditional assignment then rewinds it. `seen_ids` hides
    that in memory and not across a restart.
    """

    async def _connector(self):
        connector = _make_connector(owners=["alice"])
        room = Room(id="chan1", name="general", type="channel")
        connector._ws.register_channel = MagicMock()
        await connector.subscribe_room(room, watcher_id="w1")
        connector._rest.resolve_username = AsyncMock(return_value="alice")
        connector.register_handler(AsyncMock(return_value=True))
        return connector

    def _event(self, post_id, ts):
        return {"post": {"id": post_id, "channel_id": "chan1", "user_id": "u1",
                         "message": "@hammer.mei hi", "root_id": "", "type": "",
                         "create_at": ts},
                "mentions": ["bot-id-1"]}

    async def test_a_slow_replay_does_not_rewind_a_newer_live_cursor(self):
        connector = await self._connector()
        state = connector._channels["chan1"]

        await connector._on_posted_event(self._event("live", 900))
        self.assertEqual(state.last_processed_ts, "900")

        # The replayed post is older, and its filter timestamp is pinned below it — which
        # is what lets it through the filter at all.
        await connector._on_posted_event(
            self._event("replayed", 500), is_replay=True, replay_after_ts="1")

        self.assertEqual(
            state.last_processed_ts, "900",
            "the cursor may not go below a message that was already delivered",
        )

    async def test_an_ordinary_post_still_advances_it(self):
        connector = await self._connector()
        state = connector._channels["chan1"]

        await connector._on_posted_event(self._event("p1", 500))
        await connector._on_posted_event(self._event("p2", 900))

        self.assertEqual(state.last_processed_ts, "900")


class TestARejectedPostStaysReachableAcrossReconnects(unittest.IsolatedAsyncioTestCase):
    """Forgetting the id is only half of it — the watermark still has to point below it.

    Mattermost gets the hand-back half of `core.replay_window` and not the outage half:
    one connection resumes every channel at once, so there is no staggered-resubscribe
    race to capture a window for. This mark exists purely because AgentCoop refuses messages
    when its own queues are full.
    """

    async def _connector(self):
        connector = _make_connector(owners=["alice"])
        room = Room(id="chan1", name="general", type="channel")
        connector._ws.register_channel = MagicMock()
        await connector.subscribe_room(room, watcher_id="w1")
        connector._rest.resolve_username = AsyncMock(return_value="alice")
        connector.register_handler(AsyncMock(return_value=True))
        return connector

    def _event(self, post_id, ts):
        return {"post": {"id": post_id, "channel_id": "chan1", "user_id": "u1",
                         "message": "@hammer.mei hi", "root_id": "", "type": "",
                         "create_at": ts},
                "mentions": ["bot-id-1"]}

    async def test_a_later_success_cannot_strand_a_handed_back_post(self):
        connector = await self._connector()
        state = connector._channels["chan1"]
        state.last_processed_ts = "400"

        # The post at 500 is refused: the queues are full.
        connector.register_handler(AsyncMock(return_value=False))
        await connector._on_posted_event(self._event("p500", 500))
        # Capacity frees up and a newer post is accepted, moving the watermark past it.
        connector.register_handler(AsyncMock(return_value=True))
        await connector._on_posted_event(self._event("p900", 900))

        self.assertEqual(state.last_processed_ts, "900")
        self.assertEqual(
            state.replay_boundary, "400",
            "the next reconnect must fetch from below the refused post, not from 900",
        )

    async def test_the_reconnect_fetches_from_the_boundary(self):
        connector = await self._connector()
        state = connector._channels["chan1"]
        state.last_processed_ts = "900"
        state.claim_boundary("400")
        connector._rest.get_room_history = AsyncMock(return_value=[])

        await connector._on_ws_reconnect()

        self.assertEqual(
            connector._rest.get_room_history.await_args.kwargs["after_ts"], "400",
            "the watermark alone would skip everything the refused post is behind",
        )

    async def test_a_read_window_is_closed_again(self):
        """The near miss: never closing leaves every channel replaying from the same
        point for the life of the process."""
        connector = await self._connector()
        state = connector._channels["chan1"]
        state.last_processed_ts = "900"
        state.claim_boundary("400")
        connector._rest.get_room_history = AsyncMock(
            return_value=[{"id": "p500", "channel_id": "chan1", "user_id": "u1",
                           "message": "hi", "root_id": "", "type": "", "create_at": 500}])

        await connector._on_ws_reconnect()

        self.assertIsNone(state.replay_boundary)

    async def test_a_window_claimed_mid_batch_is_left_open(self):
        """A hand-back during the batch claims the window this batch is reading, and
        writes back the same timestamp — which is why the count decides, not the value."""
        connector = await self._connector()
        state = connector._channels["chan1"]
        state.last_processed_ts = "900"
        state.claim_boundary("400")
        connector._rest.get_room_history = AsyncMock(
            return_value=[{"id": "p500", "channel_id": "chan1", "user_id": "u1",
                           "message": "hi", "root_id": "", "type": "", "create_at": 500}])

        async def _dispatch(decoded, **kw):
            # Exactly what a live hand-back does, same call, same arguments.
            state.claim_boundary(state.last_processed_ts, just_before("450"))

        connector._on_posted_event = _dispatch

        with self.assertLogs("coop.connectors.mattermost", "INFO"):
            await connector._on_ws_reconnect()

        self.assertEqual(
            state.replay_boundary, "400",
            "the live hand-back still depends on the window this batch did not read",
        )

    async def test_an_empty_fetch_closes_the_window_it_came_in_for(self):
        connector = await self._connector()
        state = connector._channels["chan1"]
        state.last_processed_ts = "900"
        state.claim_boundary("400")
        connector._rest.get_room_history = AsyncMock(return_value=[])

        await connector._on_ws_reconnect()

        self.assertIsNone(state.replay_boundary)


class TestEveryWayOfNotDeliveringGivesTheTurnBack(unittest.IsolatedAsyncioTestCase):
    """The same surface as Rocket.Chat's twin, enumerated for the same reason.

    Review found one un-released path on Rocket.Chat; sweeping both connectors found three
    more there and three here. The budget belongs to AgentCoop, not to either platform, so the
    rule is the same on both — and the enumeration is what stops the next one being found
    in a review round instead of locally.
    """

    async def _connector(self, capacity=RoomCapacity.AVAILABLE):
        connector = _make_connector(
            filter_sender=False,
            agent_chain=AgentChainConfig(agent_usernames=["peer"], max_turns=3),
        )
        connector._config.require_mention = False
        room = Room(id="chan1", name="general", type="dm")
        connector._ws.register_channel = MagicMock()
        await connector.subscribe_room(room, watcher_id="w1")
        connector._rest.resolve_username = AsyncMock(return_value="peer")
        connector.register_capacity_check(lambda *a, **kw: capacity)
        connector.register_handler(AsyncMock(return_value=True))
        connector._rest.post_message = AsyncMock()
        return connector

    def _event(self, post_id="p1"):
        return {"post": {"id": post_id, "channel_id": "chan1", "user_id": "u1",
                         "message": "hi", "root_id": "", "type": "", "create_at": 500},
                "mentions": []}

    def _turns(self, c):
        return c._turn_store.current_turns("chan1", None, "peer")

    async def test_no_watcher_for_the_channel(self):
        c = await self._connector(RoomCapacity.UNROUTED)
        await c._on_posted_event(self._event())
        self.assertEqual(self._turns(c), 0)

    async def test_the_live_capacity_preflight(self):
        c = await self._connector(RoomCapacity.FULL)
        await c._on_posted_event(self._event())
        self.assertEqual(self._turns(c), 0)

    async def test_the_replay_capacity_preflight(self):
        c = await self._connector(RoomCapacity.FULL)
        await c._on_posted_event(self._event(), is_replay=True, replay_after_ts="1")
        self.assertEqual(self._turns(c), 0)

    async def test_normalization_failing(self):
        c = await self._connector()
        with patch("gateway.connectors.mattermost.connector.normalize_mm_message",
                   side_effect=RuntimeError("boom")):
            await c._on_posted_event(self._event())
        self.assertEqual(self._turns(c), 0)

    async def test_the_handler_raising(self):
        c = await self._connector()
        c.register_handler(AsyncMock(side_effect=RuntimeError("boom")))
        await c._on_posted_event(self._event())
        self.assertEqual(self._turns(c), 0)

    async def test_the_handler_refusing(self):
        c = await self._connector()
        c.register_handler(AsyncMock(return_value=False))
        await c._on_posted_event(self._event())
        self.assertEqual(self._turns(c), 0)

    async def test_a_delivered_post_keeps_its_turn(self):
        """The near miss: releasing unconditionally would uncap the chain."""
        c = await self._connector()
        await c._on_posted_event(self._event())
        self.assertEqual(self._turns(c), 1)


class TestHostileChannelIdsAreContained(unittest.TestCase):
    """Rocket.Chat's twin — same fence, same reason (§2.3): the character
    sanitize alone lets `..` through, and expiry `rmtree`s this path."""

    def test_dot_components_and_empties_are_refused_outright(self):
        connector = _make_connector()
        for cid in ("..", ".", ""):
            with self.subTest(cid=cid):
                self.assertIsNone(connector.attachment_cache_dir(cid))

    def test_no_id_resolves_outside_the_cache_base(self):
        from pathlib import Path

        connector = _make_connector()
        base = connector._attachments_cache_base.resolve()
        for cid in ("a/../b", "../../etc", "x\\..\\y", "chan123"):
            with self.subTest(cid=cid):
                p = connector.attachment_cache_dir(cid)
                if p is None:
                    continue  # refused is also contained
                resolved = Path(p).resolve()
                self.assertTrue(
                    resolved.is_relative_to(base) and resolved != base,
                    f"{cid!r} escaped to {resolved}",
                )


class TestAnIdleChannelWakesOnItsNextMessage(unittest.IsolatedAsyncioTestCase):
    """The wake (§2.5), Mattermost's twin of Rocket.Chat's.

    The idle drop keeps the channel's local state (§2.2), so its next message takes
    the tracked path — where the UNROUTED arm used to drop it with a warning. The arm
    now offers the channel back through the same episode funnel an untracked one goes
    through. Mattermost's one wrinkle: the tracked path registers the post's id
    *before* its first await, so the wake must forget it or the episode's redelivery
    dies at the dedup check.
    """

    def setUp(self):
        self.delivered: list[str] = []
        self.offered: list = []
        self.served = False

    async def _connector(self):
        connector = _make_connector(filter_sender=False)
        connector._config.require_mention = False
        room = Room(id="chan1", name="general", type="channel")
        connector._ws.register_channel = MagicMock()
        await connector.subscribe_room(room, watcher_id="w1")
        connector._rest.resolve_username = AsyncMock(return_value="alice")
        connector.register_capacity_check(
            lambda *a, **kw:
                RoomCapacity.AVAILABLE if self.served else RoomCapacity.UNROUTED)
        connector.register_handler(AsyncMock(return_value=True))
        connector._ws.deliver_to_channel = MagicMock(
            side_effect=lambda decoded: self.delivered.append(decoded["post"]["id"]))

        async def _router(room_ref, trigger):
            self.offered.append(room_ref)
            self.served = True

        connector.register_router(_router)
        return connector

    def _event(self, post_id="p1", create_at=500):
        return {"post": {"id": post_id, "channel_id": "chan1", "user_id": "u1",
                         "message": "hi", "root_id": "", "type": "",
                         "create_at": create_at},
                "mentions": []}

    async def _settle(self, connector):
        from tests.helpers import settle_routing_tasks

        await settle_routing_tasks(connector)

    async def test_the_wake_offers_the_channel_and_redelivers_the_trigger(self):
        from gateway.core.watcher_manager import RoomRef

        connector = await self._connector()
        state = connector._channels["chan1"]

        await connector._on_posted_event(self._event())
        await self._settle(connector)

        self.assertEqual(len(self.offered), 1, "the channel was offered exactly once")
        self.assertIsInstance(self.offered[0], RoomRef)
        self.assertEqual(self.offered[0].id, "chan1")
        self.assertEqual(self.delivered, ["p1"])
        # The optimistic registration was undone, or this redelivery would have
        # died at the dedup check; and the watermark never moved.
        self.assertNotIn("p1", state.seen_ids_set)
        self.assertFalse(state.last_processed_ts)

    async def test_a_replayed_post_wakes_too(self):
        """The outage wake. A replayed event carries no channel metadata — the
        untracked path's `_room_ref_from_event` answers None for every one of them —
        so the wake must resolve the room from the tracked state instead."""
        connector = await self._connector()

        await connector._on_posted_event(
            self._event(), is_replay=True, replay_after_ts="100")
        await self._settle(connector)

        self.assertEqual(len(self.offered), 1)
        self.assertEqual(self.delivered, ["p1"])


class TestADeclinedChannelWakeDoesNotLoop(unittest.IsolatedAsyncioTestCase):
    """The drain decides deliver-vs-drop on *served*, not on *tracked* — see
    Rocket.Chat's twin for the loop this closes."""

    def setUp(self):
        self.delivered: list[str] = []
        self.offers = 0

    async def _connector(self):
        connector = _make_connector(filter_sender=False)
        connector._config.require_mention = False
        room = Room(id="chan1", name="general", type="channel")
        connector._ws.register_channel = MagicMock()
        await connector.subscribe_room(room, watcher_id="w1")
        connector._rest.resolve_username = AsyncMock(return_value="alice")
        connector.register_capacity_check(lambda *a, **kw: RoomCapacity.UNROUTED)
        connector.register_handler(AsyncMock(return_value=True))
        connector._ws.deliver_to_channel = MagicMock(
            side_effect=lambda decoded: self.delivered.append(decoded["post"]["id"]))

        async def _declining_router(room_ref, trigger):
            self.offers += 1

        connector.register_router(_declining_router)
        return connector

    def _event(self, post_id="p1", create_at=500):
        return {"post": {"id": post_id, "channel_id": "chan1", "user_id": "u1",
                         "message": "hi", "root_id": "", "type": "",
                         "create_at": create_at},
                "mentions": []}

    async def _settle(self, connector):
        from tests.helpers import settle_routing_tasks

        await settle_routing_tasks(connector)

    async def test_declined_frames_are_dropped_and_remembered_not_redelivered(self):
        connector = await self._connector()
        state = connector._channels["chan1"]

        await connector._on_posted_event(self._event())
        await self._settle(connector)

        self.assertEqual(self.offers, 1)
        self.assertEqual(self.delivered, [],
                         "a declined wake's frames must not go back on the tracked path")
        self.assertIn("p1", state.seen_ids_set,
                      "re-remembered, so reconnect replays do not re-offer it forever")
        self.assertFalse(state.last_processed_ts, "the watermark is left where it is")

    async def test_a_redelivered_duplicate_does_not_reopen_an_episode(self):
        connector = await self._connector()

        await connector._on_posted_event(self._event())
        await self._settle(connector)
        await connector._on_posted_event(self._event())
        await self._settle(connector)

        self.assertEqual(self.offers, 1, "the remembered id stops the re-offer")

    async def test_a_new_message_retries_the_wake_once(self):
        connector = await self._connector()

        await connector._on_posted_event(self._event("p1", 500))
        await self._settle(connector)
        await connector._on_posted_event(self._event("p2", 600))
        await self._settle(connector)

        self.assertEqual(self.offers, 2,
                         "each new message re-asks once — bounded, not a loop")


class TestAParkedChannelWakeStaysRecoverable(unittest.IsolatedAsyncioTestCase):
    """A park is not a decline — see Rocket.Chat's twin. The wake arm forgot
    the optimistically-registered id (`_keep_replayable`), and a park must
    leave it forgotten: the next wake's replay from the record watermark is
    the recovery, and a re-remembered id has it die at the dedup check."""

    def setUp(self):
        self.delivered: list[str] = []
        self.offers = 0

    async def _connector(self):
        connector = _make_connector(filter_sender=False)
        connector._config.require_mention = False
        connector._ROUTE_RETRY_DELAYS = ()  # park after one attempt
        room = Room(id="chan1", name="general", type="channel")
        connector._ws.register_channel = MagicMock()
        await connector.subscribe_room(room, watcher_id="w1")
        connector._rest.resolve_username = AsyncMock(return_value="alice")
        connector.register_capacity_check(lambda *a, **kw: RoomCapacity.UNROUTED)
        connector.register_handler(AsyncMock(return_value=True))
        connector._ws.deliver_to_channel = MagicMock(
            side_effect=lambda decoded: self.delivered.append(decoded["post"]["id"]))

        async def _parking_router(room_ref, trigger):
            self.offers += 1
            raise RuntimeError("backend down")

        connector.register_router(_parking_router)
        return connector

    def _event(self, post_id="p1", create_at=500):
        return {"post": {"id": post_id, "channel_id": "chan1", "user_id": "u1",
                         "message": "hi", "root_id": "", "type": "",
                         "create_at": create_at},
                "mentions": []}

    async def _settle(self, connector):
        from tests.helpers import settle_routing_tasks

        await settle_routing_tasks(connector)

    async def test_parked_frames_are_not_re_remembered(self):
        connector = await self._connector()
        state = connector._channels["chan1"]

        await connector._on_posted_event(self._event())
        await self._settle(connector)

        self.assertEqual(self.offers, 1)
        self.assertEqual(self.delivered, [])
        self.assertNotIn(
            "p1", state.seen_ids_set,
            "a re-remembered id would have the recovery replay die at dedup",
        )
        self.assertTrue(state.replay_boundary,
                        "the wake arm's claim holds the window open")

    async def test_the_same_post_can_reopen_an_episode_after_a_park(self):
        connector = await self._connector()

        await connector._on_posted_event(self._event())
        await self._settle(connector)
        await connector._on_posted_event(
            self._event(), is_replay=True, replay_after_ts="400")
        await self._settle(connector)

        self.assertEqual(self.offers, 2,
                         "the parked frame is re-offerable, not suppressed")


class TestAPageOfSystemPostsIsNotAnEmptyWindow(unittest.IsolatedAsyncioTestCase):
    """`per_page` is applied before AgentCoop filters system posts out.

    So an empty filtered list can mean "the newest 200 entries are all joins, and every
    user post you are looking for is behind them". Reporting the outage as read there
    loses all of them. Rocket.Chat gained this distinction in this PR; Mattermost did not,
    and its REST client discarded the raw count so the caller structurally could not ask.
    """

    async def _connector(self):
        connector = _make_connector(owners=["alice"])
        room = Room(id="chan1", name="general", type="channel")
        connector._ws.register_channel = MagicMock()
        await connector.subscribe_room(room, watcher_id="w1")
        connector.register_handler(AsyncMock(return_value=True))
        state = connector._channels["chan1"]
        state.last_processed_ts = "900"
        state.claim_boundary("400")
        return connector, state

    def _page(self, messages, raw_count, limit=200):
        from gateway.core.connector import HistoryPage

        return HistoryPage(messages=messages, raw_count=raw_count, limit=limit)

    async def test_a_full_page_that_filtered_to_nothing_keeps_the_window_open(self):
        connector, state = await self._connector()
        connector._rest.get_room_history_page = AsyncMock(
            return_value=self._page([], raw_count=200))

        with self.assertLogs("coop.connectors.mattermost", "WARNING"):
            await connector._on_ws_reconnect()

        self.assertEqual(
            state.replay_boundary, "400",
            "200 system posts is not evidence the window was read",
        )

    async def test_a_genuinely_empty_window_is_still_closed(self):
        """The near miss: never closing would leave every channel replaying forever."""
        connector, state = await self._connector()
        connector._rest.get_room_history_page = AsyncMock(
            return_value=self._page([], raw_count=0))

        await connector._on_ws_reconnect()

        self.assertIsNone(state.replay_boundary)

    async def test_the_max_page_warning_counts_what_the_server_applied(self):
        """A page of 200 that filtered down to 3 has still hidden older posts — the
        warning must fire on the raw count, not the survivors."""
        connector, _state = await self._connector()
        posts = [{"id": f"p{i}", "channel_id": "chan1", "user_id": "u1",
                  "message": "hi", "root_id": "", "type": "", "create_at": 500 + i}
                 for i in range(3)]
        connector._rest.get_room_history_page = AsyncMock(
            return_value=self._page(posts, raw_count=200))

        with self.assertLogs("coop.connectors.mattermost", "WARNING") as cm:
            await connector._on_ws_reconnect()

        self.assertTrue(any("maximum" in m for m in cm.output))


class TestATransientLookupFailureDoesNotSuppressAPost(unittest.IsolatedAsyncioTestCase):
    """The id is registered before `resolve_username`, deliberately — so the failure path
    has to undo it.

    That REST call fires once per replayed post immediately after a reconnect, which is
    exactly when REST is least reliable. Left recorded, the id makes every later replay
    skip the post at the dedup check: one transient 502 suppresses it for good.
    """

    async def _connector(self):
        connector = _make_connector(owners=["alice"])
        room = Room(id="chan1", name="general", type="channel")
        connector._ws.register_channel = MagicMock()
        await connector.subscribe_room(room, watcher_id="w1")
        connector.register_handler(AsyncMock(return_value=True))
        connector._channels["chan1"].last_processed_ts = "400"
        return connector

    def _event(self):
        return {"post": {"id": "p1", "channel_id": "chan1", "user_id": "u1",
                         "message": "hi", "root_id": "", "type": "", "create_at": 500},
                "mentions": ["bot-id-1"]}

    async def test_the_id_is_forgotten_so_a_retry_can_bring_it_back(self):
        connector = await self._connector()
        connector._rest.resolve_username = AsyncMock(side_effect=RuntimeError("502"))

        await connector._on_posted_event(self._event())

        self.assertNotIn("p1", connector._channels["chan1"].seen_ids_set)

    async def test_a_mark_is_left_below_it(self):
        """Forgetting the id alone only helps until the next accepted post."""
        connector = await self._connector()
        connector._rest.resolve_username = AsyncMock(side_effect=RuntimeError("502"))

        await connector._on_posted_event(self._event())

        self.assertEqual(connector._channels["chan1"].replay_boundary, "400")
