"""Tests for the history handoff feature (Phase 1).

Covers:
  - RocketChatConnector.fetch_room_history(): filtering, normalization, format
  - WatcherLifecycle: history inject on new session, skip on resume
  - History handoff delivery: sent via a plain one-time agent.send(), fully
    decoupled from InjectedContextBuilder.build()/ensure() (issue #52)
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tests.helpers import start_watcher

# ---------------------------------------------------------------------------
# Helpers shared across test classes
# ---------------------------------------------------------------------------


def _make_config(
    owners: list[str] | None = None,
    guests: list[str] | None = None,
    bot_username: str = "hammer-mei",
    timezone: str = "Asia/Taipei",
    peer_agents: list[str] | None = None,
):
    from gateway.config import AttachmentConfig
    from gateway.connectors.rocketchat.config import AgentChainConfig, RocketChatConfig

    return RocketChatConfig(
        server_url="http://chat.example.com",
        username=bot_username,
        password="pw",
        name="rc",
        owners=owners or ["alice"],
        guests=guests or ["bob"],
        attachments=AttachmentConfig(cache_dir_global="/tmp/rc-cache"),
        timezone=timezone,
        agent_chain=AgentChainConfig(agent_usernames=peer_agents or []),
    )


def _make_connector(
    owners: list[str] | None = None,
    guests: list[str] | None = None,
    bot_username: str = "hammer-mei",
    timezone: str = "Asia/Taipei",
    peer_agents: list[str] | None = None,
):
    from gateway.connectors.rocketchat.connector import RocketChatConnector

    connector = RocketChatConnector.__new__(RocketChatConnector)
    connector._config = _make_config(owners, guests, bot_username, timezone, peer_agents)
    return connector


def _make_room(name: str = "nest", room_type: str = "channel", room_id: str = "ROOM_ID"):
    from gateway.core.connector import Room

    return Room(id=room_id, name=name, type=room_type)


def _rc_msg(
    username: str,
    text: str,
    ts_date: int = 1_746_000_000_000,
    msg_type: str = "",
) -> dict:
    """Build a minimal RC REST message dict."""
    m: dict = {
        "_id": f"msg-{ts_date}",
        "msg": text,
        "ts": {"$date": ts_date},
        "u": {"_id": "uid", "username": username},
    }
    if msg_type:
        m["t"] = msg_type
    return m


# ---------------------------------------------------------------------------
# RocketChatConnector.fetch_room_history — filtering & normalization
# ---------------------------------------------------------------------------


class TestFetchRoomHistory(unittest.IsolatedAsyncioTestCase):
    """Unit tests for RocketChatConnector.fetch_room_history().

    The REST layer is mocked so we test only the filtering and normalization
    logic inside the connector method.
    """

    async def test_owner_message_included_with_correct_role(self):
        connector = _make_connector(owners=["alice"])
        connector._rest = AsyncMock()
        connector._rest.bot_username = None
        connector._rest.get_room_history = AsyncMock(return_value=[
            _rc_msg("alice", "owner message", ts_date=1_746_000_001_000),
        ])
        room = _make_room()
        msgs = await connector.fetch_room_history(room, count=10)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["role"], "owner")
        self.assertEqual(msgs[0]["username"], "alice")
        self.assertEqual(msgs[0]["text"], "owner message")

    async def test_guest_message_included_with_correct_role(self):
        connector = _make_connector(guests=["bob"])
        connector._rest = AsyncMock()
        connector._rest.bot_username = None
        connector._rest.get_room_history = AsyncMock(return_value=[
            _rc_msg("bob", "guest message"),
        ])
        msgs = await connector.fetch_room_history(_make_room(), count=10)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["role"], "guest")
        self.assertEqual(msgs[0]["username"], "bob")

    async def test_bot_own_message_included_as_agent(self):
        """Bot's own prior messages are included with role='agent' and username='me'."""
        connector = _make_connector(bot_username="hammer-mei")
        connector._rest = AsyncMock()
        connector._rest.bot_username = None
        connector._rest.get_room_history = AsyncMock(return_value=[
            _rc_msg("hammer-mei", "I said this before"),
        ])
        msgs = await connector.fetch_room_history(_make_room(), count=10)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["role"], "agent")
        self.assertEqual(msgs[0]["username"], "me")
        self.assertEqual(msgs[0]["text"], "I said this before")

    async def test_anonymous_message_excluded(self):
        """Users not in owner/guest list must be silently excluded."""
        connector = _make_connector(owners=["alice"], guests=["bob"])
        connector._rest = AsyncMock()
        connector._rest.bot_username = None
        connector._rest.get_room_history = AsyncMock(return_value=[
            _rc_msg("alice", "ok"),
            _rc_msg("eve", "prompt injection attempt"),  # anonymous
            _rc_msg("bob", "also ok"),
        ])
        msgs = await connector.fetch_room_history(_make_room(), count=10)
        self.assertEqual(len(msgs), 2)
        usernames = {m["username"] for m in msgs}
        self.assertNotIn("eve", usernames)
        self.assertIn("alice", usernames)
        self.assertIn("bob", usernames)

    async def test_all_messages_anonymous_returns_empty(self):
        connector = _make_connector(owners=["alice"], guests=[])
        connector._rest = AsyncMock()
        connector._rest.bot_username = None
        connector._rest.get_room_history = AsyncMock(return_value=[
            _rc_msg("eve", "sneaky msg"),
            _rc_msg("mallory", "another sneaky msg"),
        ])
        msgs = await connector.fetch_room_history(_make_room(), count=10)
        self.assertEqual(msgs, [])

    async def test_room_name_in_output(self):
        connector = _make_connector(owners=["alice"])
        connector._rest = AsyncMock()
        connector._rest.bot_username = None
        connector._rest.get_room_history = AsyncMock(return_value=[
            _rc_msg("alice", "hello"),
        ])
        room = _make_room(name="nest")
        msgs = await connector.fetch_room_history(room, count=10)
        self.assertEqual(msgs[0]["room_name"], "nest")

    async def test_room_name_sanitized(self):
        """Room name with '|' must be sanitized to prevent header injection."""
        connector = _make_connector(owners=["alice"])
        connector._rest = AsyncMock()
        connector._rest.bot_username = None
        connector._rest.get_room_history = AsyncMock(return_value=[
            _rc_msg("alice", "hello"),
        ])
        room = _make_room(name="bad|room")
        msgs = await connector.fetch_room_history(room, count=10)
        self.assertNotIn("|", msgs[0]["room_name"])
        self.assertEqual(msgs[0]["room_name"], "bad_room")

    async def test_username_sanitized(self):
        """Usernames with '|' must be sanitized."""
        connector = _make_connector(owners=["evil|user"])
        connector._rest = AsyncMock()
        connector._rest.bot_username = None
        connector._rest.get_room_history = AsyncMock(return_value=[
            _rc_msg("evil|user", "msg"),
        ])
        msgs = await connector.fetch_room_history(_make_room(), count=10)
        self.assertEqual(len(msgs), 1)
        self.assertNotIn("|", msgs[0]["username"])

    async def test_message_without_sender_skipped(self):
        """Messages with no 'u' field are skipped gracefully."""
        connector = _make_connector(owners=["alice"])
        connector._rest = AsyncMock()
        connector._rest.bot_username = None
        no_sender = {"_id": "x", "msg": "text", "ts": {"$date": 1000}}
        connector._rest.get_room_history = AsyncMock(return_value=[
            no_sender,
            _rc_msg("alice", "real msg"),
        ])
        msgs = await connector.fetch_room_history(_make_room(), count=10)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["text"], "real msg")

    async def test_rest_called_with_correct_args(self):
        connector = _make_connector()
        connector._rest = AsyncMock()
        connector._rest.bot_username = None
        connector._rest.get_room_history = AsyncMock(return_value=[])
        room = _make_room(room_id="CUSTOM_ID", room_type="group")
        await connector.fetch_room_history(room, count=42)
        connector._rest.get_room_history.assert_called_once_with(
            "CUSTOM_ID", "group", 42, before_ts=None, after_ts=None
        )

    async def test_before_ts_passed_through_to_rest(self):
        """before_ts is forwarded to get_room_history for backward pagination."""
        connector = _make_connector()
        connector._rest = AsyncMock()
        connector._rest.bot_username = None
        connector._rest.get_room_history = AsyncMock(return_value=[])
        room = _make_room(room_id="ROOM", room_type="channel")
        ts = "2026-05-10T10:00:00+08:00"
        await connector.fetch_room_history(room, count=50, before_ts=ts)
        connector._rest.get_room_history.assert_called_once_with(
            "ROOM", "channel", 50, before_ts=ts, after_ts=None
        )

    async def test_after_ts_passed_through_to_rest(self):
        """after_ts is forwarded to get_room_history for forward navigation."""
        connector = _make_connector()
        connector._rest = AsyncMock()
        connector._rest.bot_username = None
        connector._rest.get_room_history = AsyncMock(return_value=[])
        room = _make_room(room_id="ROOM", room_type="channel")
        ts = "2026-05-10T19:25:00+08:00"
        await connector.fetch_room_history(room, count=50, after_ts=ts)
        connector._rest.get_room_history.assert_called_once_with(
            "ROOM", "channel", 50, before_ts=None, after_ts=ts
        )

    async def test_timestamp_formatted_as_iso(self):
        """ts field must be a formatted ISO 8601 string, not raw epoch ms."""
        connector = _make_connector(owners=["alice"], timezone="Asia/Taipei")
        connector._rest = AsyncMock()
        connector._rest.bot_username = None
        connector._rest.get_room_history = AsyncMock(return_value=[
            _rc_msg("alice", "hello", ts_date=1_746_057_600_000),
        ])
        msgs = await connector.fetch_room_history(_make_room(), count=10)
        ts = msgs[0]["ts"]
        # Must be an ISO string with timezone offset, not a raw int/epoch
        self.assertIsInstance(ts, str)
        self.assertIn("T", ts)   # ISO 8601 date/time separator
        self.assertIn("+", ts)   # timezone offset

    async def test_message_with_no_timestamp_has_ts_none(self):
        """Messages with missing $date produce ts=None (graceful)."""
        connector = _make_connector(owners=["alice"])
        connector._rest = AsyncMock()
        connector._rest.bot_username = None
        no_ts = {"_id": "x", "msg": "no timestamp", "ts": {}, "u": {"username": "alice"}}
        connector._rest.get_room_history = AsyncMock(return_value=[no_ts])
        msgs = await connector.fetch_room_history(_make_room(), count=10)
        self.assertEqual(len(msgs), 1)
        self.assertIsNone(msgs[0]["ts"])

    async def test_empty_channel_returns_empty_list(self):
        connector = _make_connector()
        connector._rest = AsyncMock()
        connector._rest.bot_username = None
        connector._rest.get_room_history = AsyncMock(return_value=[])
        msgs = await connector.fetch_room_history(_make_room(), count=50)
        self.assertEqual(msgs, [])

    async def test_peer_agent_message_included_with_own_username(self):
        """Peer agents (agent_chain.agent_usernames) are included as role='agent'
        with their actual username — distinct from the bot's 'me' self-reference."""
        connector = _make_connector(
            owners=["alice"],
            peer_agents=["wavebro"],
        )
        connector._rest = AsyncMock()
        connector._rest.bot_username = None
        connector._rest.get_room_history = AsyncMock(return_value=[
            _rc_msg("alice", "owner msg"),
            _rc_msg("wavebro", "peer agent msg"),
        ])
        msgs = await connector.fetch_room_history(_make_room(), count=10)
        self.assertEqual(len(msgs), 2)
        peer_msg = next(m for m in msgs if m["username"] == "wavebro")
        self.assertEqual(peer_msg["role"], "agent")
        self.assertEqual(peer_msg["username"], "wavebro")
        self.assertEqual(peer_msg["text"], "peer agent msg")

    async def test_peer_agent_not_included_when_not_in_agent_chain(self):
        """A user not in owners, guests, or agent_chain must be excluded."""
        connector = _make_connector(
            owners=["alice"],
            guests=[],
            peer_agents=[],  # no peer agents configured
        )
        connector._rest = AsyncMock()
        connector._rest.bot_username = None
        connector._rest.get_room_history = AsyncMock(return_value=[
            _rc_msg("alice", "ok"),
            _rc_msg("wavebro", "not in any list"),
        ])
        msgs = await connector.fetch_room_history(_make_room(), count=10)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["username"], "alice")


# ---------------------------------------------------------------------------
# History handoff delivery — decoupled from InjectedContextBuilder (issue #52)
# ---------------------------------------------------------------------------
#
# history_context used to be woven into ContextInjector.inject()'s combined
# prompt, ordered after the identity header and before context files. Under
# the durable-system-prompt fix, history_context is genuinely one-time/
# volatile content (not protocol-critical like the identity/addressing
# header), so it is now sent directly by start_watcher(WatcherLifecycle, ) as
# a simple, separate, best-effort agent.send() call — fully decoupled from
# InjectedContextBuilder.build()/ensure(). There is no longer a combined
# "identity header before history" ordering to assert on a single prompt.


class TestHistoryHandoffSentSeparatelyFromHeader(unittest.IsolatedAsyncioTestCase):
    """Verify history_context is delivered via a plain one-time agent.send(),
    independent of the header/context-file delivery path."""

    def _build_lifecycle_with_history(self, raw_history_msgs):
        from gateway.core.config import CoreConfig, HistoryHandoffConfig, WatcherConfig
        from gateway.core.injected_context_builder import InjectedContextBuilder
        from gateway.core.session_maps import SessionMaps
        from gateway.core.watcher_lifecycle import WatcherLifecycle

        wc = WatcherConfig(
            name="w1",
            connector="rc",
            room="#nest",
            agent="claude",
            history_handoff=HistoryHandoffConfig(enabled=True, fetch_count=10, verbatim_tail=5),
        )
        config = CoreConfig()
        connector = AsyncMock()
        connector.agent_username = "hammer-mei"
        connector.resolve_room = AsyncMock(return_value=MagicMock(id="r1", name="nest", type="channel"))
        connector.subscribe_room = AsyncMock()
        connector.fetch_room_history = AsyncMock(return_value=raw_history_msgs)
        connector.get_last_processed_ts = MagicMock(return_value=None)
        connector.update_last_processed_ts = MagicMock()
        connector.attachment_cache_dir = MagicMock(return_value=None)

        agent = AsyncMock()
        agent.create_session = AsyncMock(return_value="new-sess-id")
        agent.send = AsyncMock(return_value=MagicMock(is_error=False, text="ok"))
        agent.ensure_durable_instructions = AsyncMock(return_value=None)
        agent.delete_session = AsyncMock(return_value=True)

        state_store = MagicMock()
        state_store.load = MagicMock(return_value={})
        state_store.save = MagicMock()
        dispatcher = MagicMock()
        dispatcher.add_processor = MagicMock()
        # No watcher holds the room: a bare MagicMock would answer this with a truthy
        # mock, which now reads as "another watcher is already serving it" (§4.1).
        dispatcher.holder = MagicMock(return_value=None)
        injector = InjectedContextBuilder(config)
        maps = SessionMaps()

        lifecycle = WatcherLifecycle(
            connector=connector,
            agents={"claude": agent},
            config=config,
            state_store=state_store,
            dispatcher=dispatcher,
            injector=injector,
            permission_registry=None,
            maps=maps,
        )
        lifecycle._attachment_workspace = MagicMock()
        lifecycle._attachment_workspace.setup = MagicMock(return_value="/tmp/fake")
        return lifecycle, connector, agent, wc

    async def test_history_context_sent_via_plain_agent_send(self):
        lifecycle, connector, agent, wc = self._build_lifecycle_with_history(
            [{"username": "alice", "role": "owner", "text": "hello", "ts": "2026-01-01T00:00:00+08:00"}]
        )

        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            await start_watcher(lifecycle, wc, state=None)

        # The header/context path goes through agent.ensure_durable_instructions(),
        # not agent.send() — so agent.send() must have been called exactly once,
        # and only for the history handoff.
        agent.send.assert_awaited_once()
        agent.ensure_durable_instructions.assert_awaited_once()
        sent_kwargs = agent.send.call_args.kwargs
        self.assertIn("hello", sent_kwargs["prompt"])
        self.assertNotIn("Coop Session Identity", sent_kwargs["prompt"])

    async def test_no_history_means_agent_send_not_called(self):
        """When fetch_room_history returns no usable messages, agent.send()
        must not be called at all (header delivery uses ensure_durable_instructions)."""
        lifecycle, connector, agent, wc = self._build_lifecycle_with_history([])

        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            await start_watcher(lifecycle, wc, state=None)

        agent.send.assert_not_called()
        agent.ensure_durable_instructions.assert_awaited_once()


# ---------------------------------------------------------------------------
# WatcherLifecycle — history handoff trigger logic
# ---------------------------------------------------------------------------


class TestWatcherLifecycleHistoryHandoff(unittest.IsolatedAsyncioTestCase):
    """Integration-level tests for history handoff in _start_watcher.

    The connector, agent, and injector are all mocked — we only verify
    that fetch_room_history is (or is not) called under the right conditions.
    """

    def _make_lifecycle(self, history_enabled: bool = True, fetch_count: int = 10, verbatim_tail: int = 5):
        """Build a WatcherLifecycle with history handoff configured."""
        from gateway.core.config import CoreConfig, HistoryHandoffConfig, WatcherConfig
        from gateway.core.injected_context_builder import InjectedContextBuilder
        from gateway.core.session_maps import SessionMaps
        from gateway.core.watcher_lifecycle import WatcherLifecycle

        wc = WatcherConfig(
            name="w1",
            connector="rc",
            room="#nest",
            agent="claude",
            history_handoff=HistoryHandoffConfig(
                enabled=history_enabled,
                fetch_count=fetch_count,
                verbatim_tail=verbatim_tail,
            ),
        )

        config = CoreConfig()
        connector = AsyncMock()
        connector.agent_username = "hammer-mei"
        connector.resolve_room = AsyncMock(return_value=MagicMock(id="r1", name="nest", type="channel"))
        connector.subscribe_room = AsyncMock()
        connector.fetch_room_history = AsyncMock(return_value=[])
        connector.get_last_processed_ts = MagicMock(return_value=None)
        connector.update_last_processed_ts = MagicMock()
        connector.attachment_cache_dir = MagicMock(return_value=None)

        agent = AsyncMock()
        agent.create_session = AsyncMock(return_value="new-sess-id")
        agent.send = AsyncMock(return_value=MagicMock(is_error=False, text="ok"))
        agent.delete_session = AsyncMock(return_value=True)

        state_store = MagicMock()
        state_store.load = MagicMock(return_value={})
        state_store.save = MagicMock()

        dispatcher = MagicMock()
        dispatcher.add_processor = MagicMock()
        dispatcher.holder = MagicMock(return_value=None)

        injector = InjectedContextBuilder(config)

        maps = SessionMaps()

        lifecycle = WatcherLifecycle(
            connector=connector,
            agents={"claude": agent},
            config=config,
            state_store=state_store,
            dispatcher=dispatcher,
            injector=injector,
            permission_registry=None,
            maps=maps,
        )
        # Patch AttachmentWorkspace.setup to avoid filesystem calls
        lifecycle._attachment_workspace = MagicMock()
        lifecycle._attachment_workspace.setup = MagicMock(return_value="/tmp/fake")

        return lifecycle, connector, wc

    async def test_fetch_room_history_called_for_new_session(self):
        """fetch_room_history must be called when a new session is created and enabled=True."""
        lifecycle, connector, wc = self._make_lifecycle(history_enabled=True, fetch_count=20)

        # Patch MessageProcessor.start to avoid consumer loop startup
        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            await start_watcher(lifecycle, wc, state=None)

        connector.fetch_room_history.assert_called_once()
        call_args = connector.fetch_room_history.call_args
        # count argument must match fetch_count config
        self.assertEqual(call_args[0][1], 20)

    async def test_fetch_room_history_not_called_when_disabled(self):
        """fetch_room_history must NOT be called when history_handoff.enabled=False."""
        lifecycle, connector, wc = self._make_lifecycle(history_enabled=False)

        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            await start_watcher(lifecycle, wc, state=None)

        connector.fetch_room_history.assert_not_called()

    async def test_fetch_room_history_not_called_when_session_reused(self):
        """When a session is reused (not newly created), history must NOT be injected."""
        from gateway.core.state import WatcherState, backend_identity

        lifecycle, connector, wc = self._make_lifecycle(history_enabled=True)
        # Simulate an existing session — _provision_session will return created_new_session=False.
        # The identity has to match the agent config for the session to be reusable at
        # all (§2.4): a record that cannot be verified against the current backend is not
        # a reused session, it is a fresh one, and it *should* get history. This fixture
        # asserted "no history" while omitting the identity, so it began describing the
        # opposite case the moment the comparison landed.
        agent_cfg = lifecycle._config.agent_config(
            lifecycle._resolve_agent_name(wc.agent))
        existing_state = WatcherState(
            watcher_name="w1",
            session_id="existing-session-id",
            room_id="r1",
            context_injected=True,
            backend_identity=backend_identity(
                agent_cfg.type, agent_cfg.working_directory),
        )

        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            await start_watcher(lifecycle, wc, state=existing_state)

        connector.fetch_room_history.assert_not_called()

    async def test_history_bounded_by_trigger_timestamp(self):
        """The creation path's trigger timestamp bounds the fetch (§2.7).

        The trigger is buffered and replayed into the new processor as the live
        prompt, so history must fetch strictly older than it — otherwise the
        newest-history block contains the very message that triggered creation
        and the agent receives it twice.
        """
        lifecycle, connector, wc = self._make_lifecycle(history_enabled=True)

        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            await start_watcher(lifecycle,
                wc,
                state=None,
                history_before_ts="2026-08-16T10:00:00+00:00",
            )

        connector.fetch_room_history.assert_called_once()
        self.assertEqual(
            connector.fetch_room_history.call_args.kwargs.get("before_ts"),
            "2026-08-16T10:00:00+00:00",
        )

    async def test_static_start_fetches_unbounded_history(self):
        """A static start has no trigger, so the fetch carries no upper bound."""
        lifecycle, connector, wc = self._make_lifecycle(history_enabled=True)

        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            await start_watcher(lifecycle, wc, state=None)

        connector.fetch_room_history.assert_called_once()
        self.assertIsNone(
            connector.fetch_room_history.call_args.kwargs.get("before_ts"))

    async def test_fetch_failure_does_not_block_startup(self):
        """If fetch_room_history raises, the watcher must still start successfully."""
        lifecycle, connector, wc = self._make_lifecycle(history_enabled=True)
        connector.fetch_room_history = AsyncMock(side_effect=RuntimeError("network error"))

        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            # Must not raise
            await start_watcher(lifecycle, wc, state=None)

        # Processor was still started despite the fetch failure
        MockProc.return_value.start.assert_called_once()
