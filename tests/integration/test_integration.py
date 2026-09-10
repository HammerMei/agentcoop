"""Phase 5: End-to-end integration tests for the Connector abstraction layer.

All tests use ScriptConnector (zero network calls) + MockAgentBackend (canned responses).
They exercise the full SessionManager → MessageProcessor → AgentBackend → Connector pipeline.

Run with:
    uv run python -m unittest tests.test_integration -v
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from gateway.agents.response import AgentResponse
from gateway.connectors.script import ScriptConnector
from gateway.core.config import CoreConfig
from gateway.core.connector import UserRole
from gateway.core.session_manager import SessionManager
from gateway.core.state import StateFilter
from tests.helpers import MockAgentBackend, make_manager, make_rule

# Patch load_state globally so tests never touch the live ~/.agentcoop/state.json.
# Each test creates a fresh SessionManager; we don't want persisted production state
# to bleed in and cause spurious "resume" attempts against a ScriptConnector.
_patch_load_state = patch("gateway.core.state_store.load_state", return_value=[])
_patch_save_state = patch("gateway.core.state_store.save_state")


# ── Isolated async base ────────────────────────────────────────────────────────



pytestmark = pytest.mark.integration

class IsolatedTestCase(unittest.IsolatedAsyncioTestCase):
    """Base: patches load_state/save_state so tests don't touch live ~/.agentcoop/."""

    def setUp(self):
        _patch_load_state.start()
        _patch_save_state.start()
        self.addCleanup(_patch_load_state.stop)
        self.addCleanup(_patch_save_state.stop)


# ── Mock agent backend ─────────────────────────────────────────────────────────


def _watcher_info(manager: SessionManager, name: str) -> dict | None:
    """Return the list_watchers() row for a watcher, or None.

    Asks for every state: `list`'s own default hides idle records, and a test
    checking what a row *says* should not also be exercising the default.
    """
    for w in manager.list_watchers(StateFilter.ALL):
        if w["watcher_name"] == name:
            return w
    return None




async def run_and_reply(
    connector: ScriptConnector,
    text: str,
    room: str = "script",
    role: UserRole = UserRole.OWNER,
    timeout: float = 5.0,
) -> str:
    """Inject a message and wait for the reply."""
    await connector.inject(text, room=room, role=role)
    return await connector.receive_reply(timeout=timeout)


# ── Test cases ─────────────────────────────────────────────────────────────────


class TestBasicEcho(IsolatedTestCase):
    """Single agent: inject → process → reply."""

    async def test_single_message_round_trip(self):
        connector = ScriptConnector()
        agent = MockAgentBackend(responses=["pong"])
        manager = make_manager(connector, agent, watcher_rules=[make_rule()])

        await manager.run_once()

        reply = await run_and_reply(connector, "ping")

        self.assertEqual(reply, "pong")
        # One context-inject send (empty — no context files) is skipped; only user msg sent
        user_msgs = [m for m in agent.sent_messages if m["prompt"] == "ping"]
        self.assertEqual(len(user_msgs), 1)

        await manager.shutdown()

    async def test_multiple_sequential_messages(self):
        connector = ScriptConnector()
        agent = MockAgentBackend(responses=["one", "two", "three"])
        manager = make_manager(connector, agent, watcher_rules=[make_rule()])

        await manager.run_once()

        results = []
        for text in ["a", "b", "c"]:
            await connector.inject(text)
            results.append(await connector.receive_reply(timeout=5.0))

        self.assertEqual(results, ["one", "two", "three"])

        await manager.shutdown()

    async def test_session_id_passed_to_agent(self):
        connector = ScriptConnector()
        agent = MockAgentBackend()
        manager = make_manager(connector, agent, watcher_rules=[make_rule()])

        await manager.run_once()

        await connector.inject("hello")
        await connector.receive_reply(timeout=5.0)

        # All sends share the same auto-created session ID
        session_ids = {m["session_id"] for m in agent.sent_messages}
        self.assertEqual(len(session_ids), 1)

        await manager.shutdown()

    async def test_a_persisted_session_id_skips_create_session(self):
        """An existing session is reused rather than recreated.

        This previously staged the same property with a config-pinned
        `WatcherConfig.session_id`. That field is removed, so the only remaining source
        of a pre-existing id is persisted state — and the property itself is unchanged
        and still worth pinning: reuse must not call `create_session`, or a restart
        would silently start a fresh conversation.
        """
        from gateway.config import AgentConfig
        from gateway.core.state import WatcherState, backend_identity

        # The identity is part of what makes the id reusable (§2.4), so it is derived
        # from the same AgentConfig `make_manager` builds rather than written as a
        # literal — a literal would keep passing while silently describing a session
        # that no longer resumes.
        cfg = AgentConfig()
        persisted = [
            WatcherState(
                # The derived label the eager loop recreates by (§2.4): the
                # record is the recreation source, so it must look like one
                # the manager persisted — rule-derived, config frozen.
                watcher_name="default:script",
                session_id="my-existing-session-abc123",
                room_id="script",
                room_type="script",
                context_injected=True,
                backend_identity=backend_identity(cfg.type, cfg.working_directory),
                connector="default",
                agent="default",
                rule_name="rule-script",
                rule={"session_idle_days": 15, "session_expire_days": 15},
                config={"name": "default:script", "connector": "default",
                        "room": "script", "agent": "default"},
            )
        ]
        connector = ScriptConnector()
        agent = MockAgentBackend()

        with patch("gateway.core.state_store.load_state", return_value=persisted):
            manager = make_manager(
                connector, agent, watcher_rules=[make_rule("script")]
            )
            await manager.run_once()

        await connector.inject("hi")
        await connector.receive_reply(timeout=5.0)

        # create_session must NOT have been called — the id came from state.
        self.assertEqual(agent.created_sessions, [])
        self.assertEqual(
            agent.sent_messages[-1]["session_id"], "my-existing-session-abc123"
        )

        await manager.shutdown()


class TestRoleHandling(IsolatedTestCase):
    """Owner vs Guest env injection via CoreConfig.env_for_role()."""

    async def asyncSetUp(self):
        self.connector = ScriptConnector()
        self.agent = MockAgentBackend()

        from gateway.config import AgentConfig

        agent_cfg = AgentConfig(
            guest_allowed_tools=["Read", "Grep"],
            timeout=10,
        )
        config = CoreConfig(agents={"default": agent_cfg})

        self.manager = SessionManager(
            self.connector,
            {"default": self.agent},
            config,
            watcher_rules=[make_rule()],
        )
        await self.manager.run_once()

    async def asyncTearDown(self):
        await self.manager.shutdown()

    async def test_owner_role_env(self):
        await self.connector.inject("hi", role=UserRole.OWNER)
        await self.connector.receive_reply(timeout=5.0)

        env = self.agent.sent_messages[-1]["env"]
        self.assertEqual(env.get("COOP_ROLE"), "owner")

    async def test_guest_role_env(self):
        await self.connector.inject("hi", role=UserRole.GUEST)
        await self.connector.receive_reply(timeout=5.0)

        env = self.agent.sent_messages[-1]["env"]
        self.assertEqual(env.get("COOP_ROLE"), "guest")
        # COOP_ALLOWED_TOOLS is only injected for opencode agents via sidecar env
        # (service.py), not via env_for_role(). The permission broker enforces
        # guest_allowed_tools via structured ToolRule lists, not env vars.
        self.assertNotIn("COOP_ALLOWED_TOOLS", env)


class TestMultiRoomRouting(IsolatedTestCase):
    """Multiple rooms on the same connector route to the correct processor."""

    async def test_two_rooms_route_independently(self):
        # Two separate connectors simulating two independent rooms
        conn_a = ScriptConnector(name="conn-a")
        conn_b = ScriptConnector(name="conn-b")

        agent_a = MockAgentBackend(default_response="reply-from-a")
        agent_b = MockAgentBackend(default_response="reply-from-b")

        manager_a = make_manager(
            conn_a, agent_a, watcher_rules=[make_rule("room-a")]
        )
        manager_b = make_manager(
            conn_b, agent_b, watcher_rules=[make_rule("room-b")]
        )

        await manager_a.run_once()
        await manager_b.run_once()

        await conn_a.inject("msg-for-a", room="room-a")
        await conn_b.inject("msg-for-b", room="room-b")

        reply_a = await conn_a.receive_reply(timeout=5.0)
        reply_b = await conn_b.receive_reply(timeout=5.0)

        self.assertEqual(reply_a, "reply-from-a")
        self.assertEqual(reply_b, "reply-from-b")

        await manager_a.shutdown()
        await manager_b.shutdown()


class TestAgentToAgentPipe(IsolatedTestCase):
    """Pattern B: ScriptConnector.pipe_to() chains two agents."""

    async def test_two_agent_pipeline(self):
        # Agent A produces uppercase output; Agent B wraps in brackets
        conn_a = ScriptConnector(name="upper")
        conn_b = ScriptConnector(name="bracket")

        agent_a = MockAgentBackend(responses=["HELLO WORLD"])
        agent_b = MockAgentBackend(responses=["[HELLO WORLD]"])

        manager_a = make_manager(
            conn_a, agent_a, watcher_rules=[make_rule("pipeline")]
        )
        manager_b = make_manager(
            conn_b, agent_b, watcher_rules=[make_rule("pipeline")]
        )

        # Wire: A's output feeds B's input
        conn_a.pipe_to(conn_b)

        await manager_a.run_once()
        await manager_b.run_once()

        await conn_a.inject("hello world", room="pipeline")

        # A's reply goes to conn_a queue AND is piped to conn_b as input
        reply_a = await conn_a.receive_reply(timeout=5.0)
        # B processes A's output and produces the final reply
        reply_b = await conn_b.receive_reply(timeout=5.0)

        self.assertEqual(reply_a, "HELLO WORLD")
        self.assertEqual(reply_b, "[HELLO WORLD]")

        await manager_a.shutdown()
        await manager_b.shutdown()


class TestTimeoutHandling(IsolatedTestCase):
    """Agent timeout/error produces a human-readable error reply."""

    async def test_timeout_returns_error_message(self):
        connector = ScriptConnector()
        agent = MockAgentBackend()
        agent.side_effect = asyncio.TimeoutError

        manager = make_manager(
            connector, agent, timeout=1, watcher_rules=[make_rule()]
        )
        await manager.run_once()

        await connector.inject("slow query")
        reply = await connector.receive_reply(timeout=5.0)

        self.assertIn("timed out", reply.lower())

        await manager.shutdown()

    async def test_agent_error_returns_error_message(self):
        connector = ScriptConnector()
        agent = MockAgentBackend()
        agent.side_effect = RuntimeError

        manager = make_manager(connector, agent, watcher_rules=[make_rule()])
        await manager.run_once()

        await connector.inject("broken request")
        reply = await connector.receive_reply(timeout=5.0)

        self.assertIn("failed to process the request", reply.lower())

        await manager.shutdown()


class TestWatcherLifecycle(IsolatedTestCase):
    """list_watchers / pause_watcher / resume_watcher / reset_watcher."""

    async def test_list_watchers_reports_the_record(self):
        """Renamed from `..._shows_config`: rows come from records now (§2.8),
        and a name teaching the old model is worse than no name."""
        from gateway.core.connector import Room

        connector = ScriptConnector()
        agent = MockAgentBackend()
        manager = make_manager(
            connector,
            agent,
            # The include pattern matches the room's PLATFORM name (what
            # resolve returns), which this test makes distinct from the id.
            watcher_rules=[make_rule("#my-room")],
        )

        async def resolve(room_name):
            # Distinct id and name: ScriptConnector returns the same string for
            # both, which would let `room_name` be satisfied by the room-id
            # fallback and hide whether the name was recorded at all.
            return Room(id="rid-my-room", name="#my-room", type="script")

        with patch.object(connector, "resolve_room", side_effect=resolve):
            await manager.run_once()

        watchers = manager.list_watchers()
        self.assertEqual(len(watchers), 1)
        # The derived label (§2.3): connector + the room's encoded name.
        # Names are no longer operator-chosen.
        self.assertEqual(watchers[0]["watcher_name"], "default:%23my-room")
        self.assertEqual(watchers[0]["room_name"], "#my-room")
        self.assertEqual(watchers[0]["room_id"], "rid-my-room")
        self.assertEqual(watchers[0]["state"], "active")
        # `state` is derived from the record plus residency, so the processor
        # check the old `["active"]` assertion provided is kept explicitly.
        self.assertIsNotNone(manager.get_processor("default:%23my-room"))
        # When no context_inject_files are configured, inject() marks the session
        # as "injected" immediately to prevent per-message retry loops.
        self.assertEqual(watchers[0]["context_injection_state"], "injected")

        await manager.shutdown()

    async def test_pause_stops_message_processing(self):
        connector = ScriptConnector()
        agent = MockAgentBackend(responses=["before-pause"])
        manager = make_manager(connector, agent, watcher_rules=[make_rule()])

        await manager.run_once()

        # Verify normal processing works
        await connector.inject("msg1")
        reply = await connector.receive_reply(timeout=5.0)
        self.assertEqual(reply, "before-pause")

        await manager.pause_watcher("default:script")
        self.assertIsNone(manager.get_processor("default:script"))
        self.assertEqual(_watcher_info(manager, "default:script")["state"], "paused")

        await manager.shutdown()

    async def test_resume_restarts_processing(self):
        connector = ScriptConnector()
        agent = MockAgentBackend(responses=["after-resume"])
        manager = make_manager(connector, agent, watcher_rules=[make_rule()])

        await manager.run_once()
        await manager.pause_watcher("default:script")
        await manager.resume_watcher("default:script")

        self.assertIsNotNone(manager.get_processor("default:script"))
        self.assertEqual(_watcher_info(manager, "default:script")["state"], "active")

        await connector.inject("msg-after-resume")
        reply = await connector.receive_reply(timeout=5.0)
        self.assertEqual(reply, "after-resume")

        await manager.shutdown()

    async def test_reset_creates_new_session(self):
        """Reset clears auto-created session ID so a fresh session is created."""
        connector = ScriptConnector()
        agent = MockAgentBackend()
        manager = make_manager(connector, agent, watcher_rules=[make_rule()])

        await manager.run_once()
        first_session_id = agent.created_sessions[0]["session_id"]

        await manager.reset_watcher("default:script")
        self.assertEqual(len(agent.created_sessions), 2)
        second_session_id = agent.created_sessions[1]["session_id"]
        self.assertNotEqual(first_session_id, second_session_id)

        await manager.shutdown()

    async def test_reset_has_no_sticky_exemption_any_more(self):
        """Inverted: reset now clears every watcher's session, with no exception.

        This asserted the one behaviour that genuinely changed with the removal of
        `watchers[].session_id` — a pinned watcher used to keep its session across
        `reset`. No config can express that any more (the loader refuses the key), so
        the exemption is unreachable rather than merely unused, and the honest test is
        that reset is now unconditional.

        Kept as a test rather than deleted, because "reset clears the session" is
        exactly the invariant the removed branch used to break.
        """
        connector = ScriptConnector()
        agent = MockAgentBackend()
        manager = make_manager(
            connector, agent, watcher_rules=[make_rule("script")]
        )

        await manager.run_once()
        self.assertEqual(len(agent.created_sessions), 1)
        first = agent.created_sessions[0]["session_id"]

        await manager.reset_watcher("default:script")
        self.assertEqual(len(agent.created_sessions), 2, "reset did not create a session")
        second = agent.created_sessions[1]["session_id"]
        self.assertNotEqual(first, second)

        await connector.inject("hi")
        await connector.receive_reply(timeout=5.0)
        self.assertEqual(agent.sent_messages[-1]["session_id"], second)

        await manager.shutdown()

    async def test_unknown_watcher_raises(self):
        connector = ScriptConnector()
        agent = MockAgentBackend()
        manager = make_manager(connector, agent)
        await manager.run_once()

        with self.assertRaises(RuntimeError):
            await manager.pause_watcher("nonexistent")

        await manager.shutdown()

    async def test_resume_refuses_unavailable_agent(self):
        connector = ScriptConnector()
        agent = MockAgentBackend()
        manager = make_manager(connector, agent, watcher_rules=[make_rule()])

        errors = await manager.run_once(unavailable_agents={"default"})
        self.assertEqual(len(errors), 1)
        self.assertIn("unavailable", errors[0])

        with self.assertRaisesRegex(RuntimeError, "No watcher named"):
            await manager.resume_watcher("default:script")

        self.assertIsNone(manager.get_processor("default:script"))

        await manager.shutdown()

    async def test_reset_refuses_unavailable_agent(self):
        connector = ScriptConnector()
        agent = MockAgentBackend()
        manager = make_manager(connector, agent, watcher_rules=[make_rule()])

        errors = await manager.run_once(unavailable_agents={"default"})
        self.assertEqual(len(errors), 1)
        self.assertIn("unavailable", errors[0])

        # The eager start already refused fail-closed, so no record exists —
        # and a reset without a record is refused too, one door earlier.
        with self.assertRaisesRegex(RuntimeError, "No watcher named"):
            await manager.reset_watcher("default:script")

        self.assertIsNone(manager.get_processor("default:script"))

        await manager.shutdown()


class TestBatchProcessing(IsolatedTestCase):
    """Pattern C: multiple messages processed sequentially, replies in order."""

    async def test_batch_preserves_order(self):
        connector = ScriptConnector()
        responses = [f"reply-{i}" for i in range(5)]
        agent = MockAgentBackend(responses=responses[:])
        manager = make_manager(connector, agent, watcher_rules=[make_rule()])

        await manager.run_once()

        results = []
        for i in range(5):
            await connector.inject(f"msg-{i}")
            results.append(await connector.receive_reply(timeout=5.0))

        self.assertEqual(results, responses)

        user_prompts = [
            m["prompt"] for m in agent.sent_messages if m["prompt"].startswith("msg-")
        ]
        self.assertEqual(user_prompts, [f"msg-{i}" for i in range(5)])

        await manager.shutdown()


class TestConnectorFormatPrefix(IsolatedTestCase):
    """ScriptConnector.format_prompt_prefix() returns empty string."""

    async def test_no_prefix_injected_by_script_connector(self):
        connector = ScriptConnector()
        agent = MockAgentBackend()
        manager = make_manager(connector, agent, watcher_rules=[make_rule()])

        await manager.run_once()

        await connector.inject("bare message")
        await connector.receive_reply(timeout=5.0)

        # ScriptConnector returns "" from format_prompt_prefix — prompt must be unchanged
        user_msgs = [m for m in agent.sent_messages if m["prompt"] == "bare message"]
        self.assertEqual(len(user_msgs), 1)

        await manager.shutdown()


# ── Regression tests ───────────────────────────────────────────────────────────


class TestRCRefcount(unittest.IsolatedAsyncioTestCase):
    """Issue 1: Reference counting for same-room multi-watcher subscribe/unsubscribe."""

    def _make_connector(self):
        """Return a RocketChatConnector with all network deps mocked out."""
        from unittest.mock import AsyncMock, MagicMock

        from gateway.connectors.rocketchat.config import RocketChatConfig
        from gateway.connectors.rocketchat.connector import RocketChatConnector

        config = MagicMock(spec=RocketChatConfig)
        config.server_url = "http://localhost:3000"
        config.username = "bot"
        config.password = "secret"

        connector = RocketChatConnector.__new__(RocketChatConnector)
        # `__init__` never runs here, so delivery-mode state has to be set explicitly.
        # Per-room is what these tests exercise.
        connector._router = None
        connector._subscribe_all = False
        connector._dm_kinds = {}
        connector._config = config
        connector._rest = MagicMock()
        connector._ws = MagicMock()
        # Asked, not remembered: a bare MagicMock answers truthily and would skip every
        # subscription. These tests exercise per-room delivery.
        connector._ws.stream_active = False
        connector._ws.subscribe_room = AsyncMock()
        connector._ws.unsubscribe_room = AsyncMock()
        connector._handler = None
        connector._capacity_check = None
        connector._rooms = {}
        connector._watcher_contexts = {}
        connector._room_membership_gen = {}
        connector._room_refcount = {}
        connector._attachments_cache_base = Path("/tmp/acg-test-attachments/rc-test")
        connector._turn_store = None
        return connector

    async def test_refcount_increments_on_duplicate_subscribe(self):
        """Subscribing the same room twice increments refcount but only calls DDP once."""
        from gateway.core.connector import Room

        connector = self._make_connector()
        room = Room(id="room-abc", name="general", type="channel")

        await connector.subscribe_room(room, watcher_id="w1", working_directory="/tmp")
        await connector.subscribe_room(room, watcher_id="w2", working_directory="/tmp")

        self.assertEqual(connector._room_refcount["room-abc"], 2)
        connector._ws.subscribe_room.assert_called_once()

    async def test_unsubscribe_only_when_last_watcher_leaves(self):
        """DDP unsubscribe is deferred until the last watcher unsubscribes."""
        from gateway.core.connector import Room

        connector = self._make_connector()
        room = Room(id="room-xyz", name="dev", type="channel")

        await connector.subscribe_room(room, watcher_id="w1", working_directory="/tmp")
        await connector.subscribe_room(room, watcher_id="w2", working_directory="/tmp")

        # First unsubscribe: room stays subscribed
        await connector.unsubscribe_room("room-xyz", watcher_id="w1")
        self.assertIn("room-xyz", connector._rooms)
        self.assertEqual(connector._room_refcount["room-xyz"], 1)
        connector._ws.unsubscribe_room.assert_not_called()

        # Second unsubscribe: room is fully removed
        await connector.unsubscribe_room("room-xyz", watcher_id="w2")
        self.assertNotIn("room-xyz", connector._rooms)
        connector._ws.unsubscribe_room.assert_called_once()


class TestMultiWatcherDispatch(unittest.IsolatedAsyncioTestCase):
    """Regression: a single DDP message with N watcher contexts on the same room
    must invoke the handler exactly once — not once per watcher context.

    Background: Fix 3 introduced per-watcher _WatcherRoomContext to separate
    room-level and watcher-level state.  An earlier version of that fix
    accidentally looped over contexts and called self._handler() N times,
    causing each agent to reply N times (observed as 4 replies for 2 watchers).
    """

    def _make_connector(self):
        from unittest.mock import AsyncMock, MagicMock

        from gateway.connectors.rocketchat.config import RocketChatConfig
        from gateway.connectors.rocketchat.connector import RocketChatConnector

        config = MagicMock(spec=RocketChatConfig)
        config.server_url = "http://localhost:3000"
        config.username = "bot"
        config.password = "secret"
        config.reply_in_thread = False
        config.permission_reply_in_thread = False
        config.attachments = MagicMock()
        config.attachments.cache_dir = "agent-chat.cache"

        connector = RocketChatConnector.__new__(RocketChatConnector)
        # `__init__` never runs here, so delivery-mode state has to be set explicitly.
        # Per-room is what these tests exercise.
        connector._router = None
        connector._subscribe_all = False
        connector._dm_kinds = {}
        connector._config = config
        connector._rest = MagicMock()
        connector._ws = MagicMock()
        # Asked, not remembered: a bare MagicMock answers truthily and would skip every
        # subscription. These tests exercise per-room delivery.
        connector._ws.stream_active = False
        connector._ws.subscribe_room = AsyncMock()
        connector._ws.unsubscribe_room = AsyncMock()
        connector._handler = None
        connector._capacity_check = None
        connector._rooms = {}
        connector._watcher_contexts = {}
        connector._room_membership_gen = {}
        connector._room_refcount = {}
        connector._attachments_cache_base = Path("/tmp/acg-test-attachments/rc-test")
        connector._turn_store = None
        return connector

    async def test_handler_called_once_for_two_watcher_contexts(self):
        """One DDP message → handler called exactly once, regardless of watcher count."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from gateway.core.connector import IncomingMessage, Room, User, UserRole

        connector = self._make_connector()
        room = Room(id="room-multi", name="general", type="channel")

        # Subscribe two watchers to the same room
        await connector.subscribe_room(
            room, watcher_id="w1", working_directory="/tmp/w1"
        )
        await connector.subscribe_room(
            room, watcher_id="w2", working_directory="/tmp/w2"
        )
        self.assertEqual(len(connector._watcher_contexts["room-multi"]), 2)

        # Register a mock handler and track call count
        handler = AsyncMock()
        connector.register_handler(handler)

        # Build a minimal normalized IncomingMessage to return from normalize mock
        fake_msg = IncomingMessage(
            id="msg-001",
            timestamp="1000000000.001",
            room=room,
            sender=User(id="u1", username="alice"),
            role=UserRole.OWNER,
            text="hello",
        )

        raw_doc = {
            "_id": "msg-001",
            "msg": "hello",
            "u": {"username": "alice"},
            "ts": {"$date": 1000000000001},
        }

        filter_result = MagicMock()
        filter_result.accepted = True
        filter_result.sender = "alice"
        filter_result.msg_ts = "1000000000.001"
        filter_result.reason = ""

        with (
            patch(
                "gateway.connectors.rocketchat.connector.filter_rc_message",
                return_value=filter_result,
            ),
            patch(
                "gateway.connectors.rocketchat.connector.normalize_rc_message",
                AsyncMock(return_value=fake_msg),
            ),
        ):
            await connector._on_raw_ddp_message("room-multi", raw_doc)

        # Handler must be called exactly once — SessionManager does the fan-out,
        # not the connector.
        handler.assert_called_once()

    async def test_handler_called_once_for_single_watcher(self):
        """Baseline: single watcher also results in exactly one handler call."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from gateway.core.connector import IncomingMessage, Room, User, UserRole

        connector = self._make_connector()
        room = Room(id="room-single", name="dev", type="channel")

        await connector.subscribe_room(
            room, watcher_id="w1", working_directory="/tmp/w1"
        )

        handler = AsyncMock()
        connector.register_handler(handler)

        fake_msg = IncomingMessage(
            id="msg-002",
            timestamp="1000000000.002",
            room=room,
            sender=User(id="u1", username="bob"),
            role=UserRole.OWNER,
            text="hi",
        )

        raw_doc = {
            "_id": "msg-002",
            "msg": "hi",
            "u": {"username": "bob"},
            "ts": {"$date": 1000000000002},
        }

        filter_result = MagicMock()
        filter_result.accepted = True
        filter_result.sender = "bob"
        filter_result.msg_ts = "1000000000.002"
        filter_result.reason = ""

        with (
            patch(
                "gateway.connectors.rocketchat.connector.filter_rc_message",
                return_value=filter_result,
            ),
            patch(
                "gateway.connectors.rocketchat.connector.normalize_rc_message",
                AsyncMock(return_value=fake_msg),
            ),
        ):
            await connector._on_raw_ddp_message("room-single", raw_doc)

        handler.assert_called_once()

    async def test_handler_not_called_when_message_filtered(self):
        """Filtered messages must never reach the handler, regardless of watcher count."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from gateway.core.connector import Room

        connector = self._make_connector()
        room = Room(id="room-filtered", name="spam", type="channel")

        await connector.subscribe_room(
            room, watcher_id="w1", working_directory="/tmp/w1"
        )
        await connector.subscribe_room(
            room, watcher_id="w2", working_directory="/tmp/w2"
        )

        handler = AsyncMock()
        connector.register_handler(handler)

        filter_result = MagicMock()
        filter_result.accepted = False
        filter_result.sender = "spammer"
        filter_result.reason = "not in allowlist"

        with patch(
            "gateway.connectors.rocketchat.connector.filter_rc_message",
            return_value=filter_result,
        ):
            await connector._on_raw_ddp_message("room-filtered", {"msg": "spam"})

        handler.assert_not_called()

    async def test_ddp_callback_forwards_directly_to_connector_processing(self):
        """Callback should hand the raw doc straight to connector processing."""
        connector = self._make_connector()
        room_id = "room-queue"
        callback = connector._make_ddp_callback(room_id)

        processed: list[tuple[str, str]] = []

        async def slow_process(
            process_room_id: str, doc: dict, *, access: dict | None = None, **kwargs
        ) -> None:
            # The transport now hands the per-delivery access object down with the doc.
            processed.append((process_room_id, doc["_id"]))

        connector._on_raw_ddp_message = slow_process  # type: ignore[method-assign]

        await callback({"_id": "msg-1"})
        await callback({"_id": "msg-2"})

        self.assertEqual(processed, [(room_id, "msg-1"), (room_id, "msg-2")])

    async def test_send_text_splits_long_messages_using_connector_chunk_limit(self):
        """RocketChatConnector.send_text should pass text_chunk_limit through outbound helper."""
        from unittest.mock import AsyncMock


        connector = self._make_connector()
        connector._rest.post_message = AsyncMock()

        long_text = "A" * (connector.text_chunk_limit + 25)
        await connector.send_text("room-chunk", AgentResponse(text=long_text))

        self.assertEqual(connector._rest.post_message.await_count, 2)


class RoomStateTrackingConnector(ScriptConnector):
    """A ScriptConnector that models the real per-room watermark storage.

    The stock ScriptConnector inherits the base no-op ``get_last_processed_ts``,
    which returns None unconditionally — so no test built on it can distinguish
    "the watermark was read too late" from "there was never a watermark".  That
    is why the ordering bug in ``_stop_processor`` went unnoticed.

    This mirrors what Rocket.Chat and Mattermost actually do: the watermark
    lives *inside* the per-room entry, and unsubscribing the last watcher pops
    that entry — after which the getter can only return None.
    """

    def __init__(self):
        super().__init__()
        self.rooms: dict[str, str] = {}          # room_id -> last_processed_ts
        self.read_order: list[tuple[str, str | None]] = []

    def seed(self, room_id: str, ts: str) -> None:
        self.rooms[room_id] = ts

    async def unsubscribe_room(self, room_id: str, watcher_id: str = "") -> None:
        await super().unsubscribe_room(room_id, watcher_id)
        self.rooms.pop(room_id, None)           # the pop that broke the read

    def get_last_processed_ts(self, room_id: str) -> str | None:
        ts = self.rooms.get(room_id)
        self.read_order.append((room_id, ts))
        return ts

    def update_last_processed_ts(self, room_id: str, ts: str) -> None:
        if room_id in self.rooms:
            self.rooms[room_id] = ts


class TestWatermarkCapturedBeforeUnsubscribe(IsolatedTestCase):
    """The watermark must be read while the connector still holds the room.

    Unsubscribing the last watcher pops the connector's per-room entry, and the
    watermark lives in it.  Reading afterwards returned None *silently* — dict
    lookups do not raise — so the stale value was persisted and every message
    since it was redelivered on the next start.
    """

    async def test_pause_persists_the_live_watermark(self):
        connector = RoomStateTrackingConnector()
        agent = MockAgentBackend()
        manager = make_manager(connector, agent, watcher_rules=[make_rule("w1")])
        await manager.run_once()

        room_id = manager._lifecycle.get_watcher_state("default:w1").room_id
        connector.seed(room_id, "2025-06-06T06:06:06Z")

        await manager.dispatch_command({"cmd": "pause", "watcher_name": "default:w1"})

        self.assertEqual(
            manager._lifecycle.get_watcher_state("default:w1").last_processed_ts,
            "2025-06-06T06:06:06Z",
            "watermark was read after the unsubscribe popped the room entry",
        )
        await manager.shutdown()

    async def test_the_watermark_is_read_before_the_room_is_popped(self):
        """Pins the ordering directly, not just its outcome."""
        connector = RoomStateTrackingConnector()
        agent = MockAgentBackend()
        manager = make_manager(connector, agent, watcher_rules=[make_rule("w1")])
        await manager.run_once()

        room_id = manager._lifecycle.get_watcher_state("default:w1").room_id
        connector.seed(room_id, "2025-06-06T06:06:06Z")
        # Startup's own save already polled this room (before the seed), so
        # observe only the reads the pause itself performs.
        connector.read_order.clear()

        await manager.dispatch_command({"cmd": "pause", "watcher_name": "default:w1"})

        reads = [ts for (rid, ts) in connector.read_order if rid == room_id]
        self.assertTrue(reads, "the watermark was never read at all")
        self.assertEqual(
            reads[0], "2025-06-06T06:06:06Z",
            "the first read returned None — it happened after the unsubscribe",
        )
        await manager.shutdown()

    async def test_reset_also_persists_the_live_watermark(self):
        connector = RoomStateTrackingConnector()
        agent = MockAgentBackend()
        manager = make_manager(connector, agent, watcher_rules=[make_rule("w1")])
        await manager.run_once()

        room_id = manager._lifecycle.get_watcher_state("default:w1").room_id
        connector.seed(room_id, "2025-07-07T07:07:07Z")

        await manager.dispatch_command({"cmd": "reset", "watcher_name": "default:w1"})

        # reset restarts the watcher, so the surviving record is the new one —
        # it must have inherited the watermark rather than resetting to empty.
        self.assertEqual(
            manager._lifecycle.get_watcher_state("default:w1").last_processed_ts,
            "2025-07-07T07:07:07Z",
        )
        await manager.shutdown()

    async def test_a_stale_held_value_is_not_overwritten_when_there_is_no_live_one(self):
        """No live watermark must leave the known value alone, not blank it."""
        connector = RoomStateTrackingConnector()
        agent = MockAgentBackend()
        manager = make_manager(connector, agent, watcher_rules=[make_rule("w1")])
        await manager.run_once()

        manager._lifecycle.get_watcher_state("default:w1").last_processed_ts = "known"
        # connector.rooms deliberately left empty — nothing live to report.

        await manager.dispatch_command({"cmd": "pause", "watcher_name": "default:w1"})

        self.assertEqual(manager._lifecycle.get_watcher_state("default:w1").last_processed_ts, "known")
        await manager.shutdown()


class TestWatermarkPersistence(IsolatedTestCase):
    """Issue 2: Watermark (last_processed_ts) is pulled from connector on save
    and restored into connector on startup."""

    async def test_watermark_pulled_from_connector_on_save(self):
        """_save_state() reads the live ts from the connector before serializing."""
        from unittest.mock import patch

        connector = ScriptConnector()
        agent = MockAgentBackend()
        manager = make_manager(connector, agent, watcher_rules=[make_rule()])
        await manager.run_once()

        room_id = "script"
        # Manually set up watcher state with a known room_id
        manager.get_watcher_state("default:script").room_id = room_id

        # Mock get_last_processed_ts to return a specific timestamp
        with patch.object(
            connector, "get_last_processed_ts", return_value="1234567890.000001"
        ):
            manager._lifecycle.save_state()

        # The saved WatcherState should carry the mocked timestamp
        saved_ws = manager.get_watcher_state("default:script")
        self.assertEqual(saved_ws.last_processed_ts, "1234567890.000001")

        await manager.shutdown()

    async def test_watermark_restored_into_connector_on_start(self):
        """On startup, last_processed_ts from persisted state is pushed into the connector."""
        from unittest.mock import patch

        from gateway.config import AgentConfig
        from gateway.core.state import backend_identity
        from gateway.state import WatcherState

        persisted_ts = "1234567890.000001"
        cfg = AgentConfig()
        persisted = [
            WatcherState(
                watcher_name="default:script",
                session_id="existing-session",
                room_id="script",
                room_type="script",
                context_injected=True,
                paused=False,
                connector="default",
                agent="default",
                rule_name="rule-script",
                rule={"session_idle_days": 15, "session_expire_days": 15},
                config={"name": "default:script", "connector": "default",
                        "room": "script", "agent": "default"},
                last_processed_ts=persisted_ts,
                # The watermark is restored either way, so this test passed while
                # quietly exercising a *fresh* session — the one thing its scenario
                # ("restart with persisted state") does not describe.
                backend_identity=backend_identity(cfg.type, cfg.working_directory),
            )
        ]

        connector = ScriptConnector()
        agent = MockAgentBackend()

        update_calls = []

        original_update = connector.update_last_processed_ts

        def capture_update(room_id, ts):
            update_calls.append((room_id, ts))
            return original_update(room_id, ts)

        connector.update_last_processed_ts = capture_update

        with patch("gateway.core.state_store.load_state", return_value=persisted):
            manager = make_manager(
                connector,
                agent,
                watcher_rules=[make_rule("script")],
            )
            await manager.run_once()

        self.assertIn(("script", persisted_ts), update_calls)

        await manager.shutdown()


class TestDeferredRegistration(IsolatedTestCase):
    """Issue 3: A startup failure in _inject_context or subscribe_room must leave
    _states and _processors empty (no partial registration)."""

    async def test_startup_failure_in_inject_context_leaves_no_state(self):
        """If _inject_context raises, the watcher is not registered in _states or _processors."""

        connector = ScriptConnector()
        agent = MockAgentBackend()

        # Give the watcher a context_inject_files entry so _inject_context is
        # actually called (non-empty files list), then make it explode.
        rule = make_rule("script", context_inject_files=["nonexistent-context.md"])
        manager = make_manager(connector, agent, watcher_rules=[rule])

        errors = await manager.run_once()

        # Startup must have failed (error reported) but no partial state stored
        self.assertTrue(len(errors) > 0)
        # No watchers should have started or left partial state
        self.assertIsNone(manager.get_processor("ctx-fail"))
        self.assertEqual(manager.list_watchers(StateFilter.ALL), [])
        self.assertIsNone(manager.get_watcher_state("ctx-fail"))

        await manager.shutdown()

    async def test_startup_failure_in_subscribe_room_leaves_no_state(self):
        """If subscribe_room raises, _processors is cleared but _states retains the
        partial WatcherState so that context_injected is preserved on retry."""
        from unittest.mock import patch

        connector = ScriptConnector()
        agent = MockAgentBackend()
        manager = make_manager(connector, agent, watcher_rules=[make_rule()])

        async def failing_subscribe(*args, **kwargs):
            raise RuntimeError("DDP subscription failed")

        with patch.object(connector, "subscribe_room", side_effect=failing_subscribe):
            errors = await manager.run_once()

        self.assertTrue(len(errors) > 0)
        # No active processor for the failed watcher.
        self.assertIsNone(manager.get_processor("default:script"))
        # State retains a partial entry so context_injected is not lost on retry.
        self.assertIsNotNone(manager.get_watcher_state("default:script"))
        self.assertFalse(manager.get_watcher_state("default:script").paused)

        await manager.shutdown()


class TestStartupRaceRollback(IsolatedTestCase):
    """Fix 1: processor and session maps are committed before subscribe, and fully
    rolled back if subscribe_room raises."""

    def _make_manager_with_maps(self, connector, agent, watcher_rules):
        """Build a SessionManager with live session maps."""
        from gateway.config import AgentConfig
        from gateway.core.session_maps import SessionMaps

        agent_cfg = AgentConfig(timeout=10)
        config = CoreConfig(agents={"default": agent_cfg})
        maps = SessionMaps()
        manager = SessionManager(
            connector,
            {"default": agent},
            config,
            watcher_rules=watcher_rules,
            session_maps=maps,
        )
        return manager, maps.room, maps.connector

    async def test_subscribe_failure_rollback_cleans_session_maps(self):
        """If subscribe_room raises, session_room_map and session_connector_map
        must be empty — no dangling routing entries left behind."""
        from unittest.mock import patch

        connector = ScriptConnector()
        agent = MockAgentBackend()
        manager, session_room_map, session_connector_map = self._make_manager_with_maps(
            connector, agent, watcher_rules=[make_rule()]
        )

        async def failing_subscribe(*args, **kwargs):
            raise RuntimeError("DDP subscription failed")

        with patch.object(connector, "subscribe_room", side_effect=failing_subscribe):
            errors = await manager.run_once()

        self.assertTrue(len(errors) > 0)
        # No active processor — subscribe failed.
        self.assertIsNone(manager.get_processor("default:script"))
        # Routing maps must be cleaned — no dangling session→room or session→connector entries.
        self.assertEqual(
            session_room_map, {}, "session_room_map must be cleaned on rollback"
        )
        self.assertEqual(
            session_connector_map,
            {},
            "session_connector_map must be cleaned on rollback",
        )
        # State retains a partial entry so context_injected is preserved on retry.
        self.assertIsNotNone(manager.get_watcher_state("default:script"))

        await manager.shutdown()

    async def test_processor_registered_before_subscribe_is_called(self):
        """_processors[watcher_name] must exist at the moment subscribe_room is called,
        so _dispatch() is fully armed as soon as DDP messages can arrive."""
        from unittest.mock import patch

        connector = ScriptConnector()
        agent = MockAgentBackend()
        manager = make_manager(connector, agent, watcher_rules=[make_rule()])

        processor_ready_at_subscribe_time: list[bool] = []
        original_subscribe = connector.subscribe_room

        async def check_then_subscribe(*args, **kwargs):
            # At the moment subscribe is called, processor must already be registered
            processor_ready_at_subscribe_time.append(
                manager.get_processor("default:script") is not None
            )
            return await original_subscribe(*args, **kwargs)

        with patch.object(
            connector, "subscribe_room", side_effect=check_then_subscribe
        ):
            await manager.run_once()

        self.assertEqual(
            processor_ready_at_subscribe_time,
            [True],
            "_processors must be populated before subscribe_room is called",
        )

        await manager.shutdown()

    async def test_dispatcher_not_populated_until_after_subscribe(self):
        """Issue 6.1: dispatcher must NOT receive the processor until subscribe succeeds.

        The old code added the processor to the dispatcher BEFORE subscribe_room,
        creating a window where incoming DDP messages could reach a processor that
        would be torn down immediately after a subscribe failure.  The fix moves
        add_processor to AFTER subscribe_room returns successfully.
        """
        from unittest.mock import patch

        connector = ScriptConnector()
        agent = MockAgentBackend()
        manager, _, _ = self._make_manager_with_maps(
            connector, agent, watcher_rules=[make_rule()]
        )

        dispatcher_populated_before_subscribe: list[bool] = []
        original_subscribe = connector.subscribe_room

        async def check_then_subscribe(*args, **kwargs):
            # Capture dispatcher state at the moment subscribe is called.
            lc = manager._lifecycle
            has_processor = bool(lc._dispatcher._room_processor)
            dispatcher_populated_before_subscribe.append(has_processor)
            return await original_subscribe(*args, **kwargs)

        with patch.object(
            connector, "subscribe_room", side_effect=check_then_subscribe
        ):
            await manager.run_once()

        self.assertEqual(
            dispatcher_populated_before_subscribe,
            [False],
            "Dispatcher must NOT have processors registered before subscribe_room returns",
        )

        await manager.shutdown()

    async def test_dispatcher_empty_after_subscribe_failure(self):
        """Issue 6.1: if subscribe_room fails, the dispatcher must not retain the processor."""
        from unittest.mock import patch

        connector = ScriptConnector()
        agent = MockAgentBackend()
        manager, _, _ = self._make_manager_with_maps(
            connector, agent, watcher_rules=[make_rule()]
        )

        async def failing_subscribe(*args, **kwargs):
            raise RuntimeError("subscribe failed")

        with patch.object(connector, "subscribe_room", side_effect=failing_subscribe):
            errors = await manager.run_once()

        self.assertTrue(len(errors) > 0)
        # The dispatcher must be empty — no processor should survive a subscribe failure.
        lc = manager._lifecycle
        self.assertFalse(
            bool(lc._dispatcher._room_processor),
            "Dispatcher must be empty after subscribe_room failure (no orphaned processors)",
        )

        await manager.shutdown()


class TestDuplicateSessionIdValidation(unittest.TestCase):
    """Fix 2A, superseded: the duplicate-sticky-session_id check is gone with the field.

    `watchers[].session_id` is removed, so two watchers cannot share one and the
    cross-watcher pass has nothing to compare. The hazard it guarded — two watchers
    on one id silently overwriting the session→room / session→connector routing maps,
    so permission notifications land in the wrong room — now cannot arise from config
    at all. These cases are kept, inverted, because the property that matters is
    unchanged: such a config must be refused at load rather than accepted.
    """

    def test_duplicate_sticky_session_id_raises_at_config_load(self):
        """Still a load error — now because the field itself is refused, per entry."""
        import tempfile
        import textwrap

        from gateway.config import GatewayConfig

        cfg = textwrap.dedent("""\
            connectors:
              - name: rc
                type: rocketchat
                server:
                  url: http://localhost:3000
                  username: bot
                  password: pw
            agents:
              default:
                type: claude
                working_directory: /tmp
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
                session_id: shared-session-id
              - name: w2
                connector: rc
                agent: default
                rooms:
                  include: [lobby]
                session_id: shared-session-id
        """)

        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(cfg)
            tmp_path = f.name

        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(tmp_path)

        # `session_id` has no dedicated rejection path any more — the closed
        # rule key set reports it like any other non-key.
        self.assertIn("session_id", str(ctx.exception))
        self.assertIn("unknown key(s)", str(ctx.exception))

    def test_unique_sticky_session_ids_are_now_refused_too(self):
        """Inverted: uniqueness used to make them legal. The field is gone, so being
        distinct no longer helps — each entry is refused on its own."""
        import tempfile
        import textwrap

        from gateway.config import GatewayConfig

        cfg = textwrap.dedent("""\
            connectors:
              - name: rc
                type: rocketchat
                server:
                  url: http://localhost:3000
                  username: bot
                  password: pw
            agents:
              default:
                type: claude
                working_directory: /tmp
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
                session_id: session-aaa
              - name: w2
                connector: rc
                agent: default
                rooms:
                  include: [lobby]
                session_id: session-bbb
        """)

        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(cfg)
            tmp_path = f.name

        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(tmp_path)
        # `session_id` has no dedicated rejection path any more — the closed
        # rule key set reports it like any other non-key.
        self.assertIn("session_id", str(ctx.exception))
        self.assertIn("unknown key(s)", str(ctx.exception))

    def test_no_session_id_watchers_do_not_raise(self):
        """Watchers without sticky session_ids (auto-create) must never trigger the check."""
        import tempfile
        import textwrap

        from gateway.config import GatewayConfig

        cfg = textwrap.dedent("""\
            connectors:
              - name: rc
                type: rocketchat
                server:
                  url: http://localhost:3000
                  username: bot
                  password: pw
            agents:
              default:
                type: claude
                working_directory: /tmp
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
              - name: w2
                connector: rc
                agent: default
                rooms:
                  include: [lobby]
        """)

        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(cfg)
            tmp_path = f.name

        config = GatewayConfig.from_file(tmp_path)
        self.assertEqual(len(config.watcher_rules), 2)


class TestAttachmentCachePath(unittest.IsolatedAsyncioTestCase):
    """Fix 3: attachment cache uses global base dir + connector name + room_id,
    and dest filenames use file_id for uniqueness."""

    def _make_connector(self):
        from unittest.mock import AsyncMock, MagicMock

        from gateway.connectors.rocketchat.config import RocketChatConfig
        from gateway.connectors.rocketchat.connector import RocketChatConnector

        config = MagicMock(spec=RocketChatConfig)
        config.server_url = "http://localhost:3000"
        config.username = "bot"
        config.password = "secret"
        config.reply_in_thread = False
        config.permission_reply_in_thread = False
        config.attachments = MagicMock()
        config.attachments.cache_dir = "agent-chat.cache"

        connector = RocketChatConnector.__new__(RocketChatConnector)
        # `__init__` never runs here, so delivery-mode state has to be set explicitly.
        # Per-room is what these tests exercise.
        connector._router = None
        connector._subscribe_all = False
        connector._dm_kinds = {}
        connector._config = config
        connector._rest = MagicMock()
        connector._ws = MagicMock()
        # Asked, not remembered: a bare MagicMock answers truthily and would skip every
        # subscription. These tests exercise per-room delivery.
        connector._ws.stream_active = False
        connector._ws.subscribe_room = AsyncMock()
        connector._ws.unsubscribe_room = AsyncMock()
        connector._handler = None
        connector._capacity_check = None
        connector._rooms = {}
        connector._watcher_contexts = {}
        connector._room_membership_gen = {}
        connector._room_refcount = {}
        connector._attachments_cache_base = Path("/tmp/acg-test/rc-home")
        connector._turn_store = None
        return connector

    async def test_normalize_receives_global_cache_dir_with_room_id(self):
        """normalize_rc_message must be called with cache_dir = _attachments_cache_base / room_id."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from gateway.core.connector import IncomingMessage, Room, User, UserRole

        connector = self._make_connector()
        room = Room(id="ROOM-XYZ", name="general", type="channel")
        await connector.subscribe_room(
            room, watcher_id="w1", working_directory="/tmp/w1"
        )

        connector.register_handler(AsyncMock())

        fake_msg = IncomingMessage(
            id="msg-001",
            timestamp="1000.001",
            room=room,
            sender=User(id="u1", username="alice"),
            role=UserRole.OWNER,
            text="hi",
        )
        filter_result = MagicMock(
            accepted=True, sender="alice", msg_ts="1000.001", reason=""
        )

        normalize_mock = AsyncMock(return_value=fake_msg)

        with (
            patch(
                "gateway.connectors.rocketchat.connector.filter_rc_message",
                return_value=filter_result,
            ),
            patch(
                "gateway.connectors.rocketchat.connector.normalize_rc_message",
                normalize_mock,
            ),
        ):
            await connector._on_raw_ddp_message(
                "ROOM-XYZ",
                {
                    "_id": "msg-001",
                    "msg": "hi",
                    "u": {"username": "alice"},
                    "ts": {"$date": 1000001},
                },
            )

        _, kwargs = normalize_mock.call_args
        expected_cache_dir = Path("/tmp/acg-test/rc-home") / "ROOM-XYZ"
        self.assertEqual(
            kwargs["cache_dir"],
            expected_cache_dir,
            "cache_dir must be global_base / room_id",
        )

    async def test_download_dest_path_uses_file_id(self):
        """_download_attachments must use file_id (not idx) as the filename key."""
        import tempfile
        from unittest.mock import AsyncMock, MagicMock

        from gateway.config import AttachmentConfig
        from gateway.connectors.rocketchat.normalize import _download_attachments

        config = MagicMock()
        config.attachments = AttachmentConfig(
            max_file_size_mb=10.0,
            download_timeout=30,
        )

        rest = MagicMock()
        rest.download_file = AsyncMock()

        doc = {
            "files": [
                {
                    "_id": "fileid-abc123",
                    "name": "photo.jpg",
                    "size": 100,
                    "type": "image/jpeg",
                },
                {
                    "_id": "fileid-xyz789",
                    "name": "doc.pdf",
                    "size": 200,
                    "type": "application/pdf",
                },
            ],
            "attachments": [
                {"title_link": "/file-upload/fileid-abc123/photo.jpg"},
                {"title_link": "/file-upload/fileid-xyz789/doc.pdf"},
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            await _download_attachments(doc, config, rest, cache_dir)

        # Verify file_id is present in each dest path
        downloaded_paths = [call.args[1] for call in rest.download_file.call_args_list]
        self.assertTrue(
            any("fileid-abc123" in p for p in downloaded_paths),
            f"file_id 'fileid-abc123' not found in dest paths: {downloaded_paths}",
        )
        self.assertTrue(
            any("fileid-xyz789" in p for p in downloaded_paths),
            f"file_id 'fileid-xyz789' not found in dest paths: {downloaded_paths}",
        )
        # Also verify original name is preserved as suffix (human readability)
        self.assertTrue(
            any("photo.jpg" in p for p in downloaded_paths),
            "Original filename 'photo.jpg' should be preserved as suffix",
        )

    async def test_two_files_same_name_get_different_paths(self):
        """Two uploads with the same filename but different file_ids must land on
        different dest paths — no silent overwrite."""
        import tempfile
        from unittest.mock import AsyncMock, MagicMock

        from gateway.config import AttachmentConfig
        from gateway.connectors.rocketchat.normalize import _download_attachments

        config = MagicMock()
        config.attachments = AttachmentConfig(
            max_file_size_mb=10.0, download_timeout=30
        )

        rest = MagicMock()
        rest.download_file = AsyncMock()

        doc = {
            "files": [
                {
                    "_id": "id-AAAA",
                    "name": "report.pdf",
                    "size": 10,
                    "type": "application/pdf",
                },
                {
                    "_id": "id-BBBB",
                    "name": "report.pdf",
                    "size": 10,
                    "type": "application/pdf",
                },
            ],
            "attachments": [
                {"title_link": "/file-upload/id-AAAA/report.pdf"},
                {"title_link": "/file-upload/id-BBBB/report.pdf"},
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            await _download_attachments(doc, config, rest, cache_dir)

        dest_paths = [call.args[1] for call in rest.download_file.call_args_list]
        self.assertEqual(len(dest_paths), 2)
        self.assertNotEqual(
            dest_paths[0],
            dest_paths[1],
            "Same filename with different file_ids must produce different dest paths",
        )


if __name__ == "__main__":
    unittest.main()
