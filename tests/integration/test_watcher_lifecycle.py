"""Tests for WatcherLifecycle lock, attachment workspace, and sync behaviours.

Consolidates tests from:
  - test_round4_fixes.py: TestWatcherLifecycleLock
  - test_round6_fixes.py: TestAttachmentWorkspaceInThread
  - test_round8_fixes.py: TestAttachmentWorkspaceRollback
  - test_round9_fixes.py: TestContextInjectedResetOnSubscribeFailure
  - test_round14_fixes.py: TestSyncWatchersHoldsLock, TestGetWatcherLock
  - test_round16_fixes.py: TestSyncWatchersPreservesBlockedAgents
  - test_code_review_fixes.py: TestWatcherLifecycleHardening, TestUnavailableAgentsBlocksWatchers
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import AgentConfig, WatcherConfig
from gateway.connectors.script import ScriptConnector
from gateway.core.config import CoreConfig
from gateway.core.connector import Room
from gateway.core.state import WatcherState
from gateway.core.watcher_manager import config_from_record
from tests.helpers import (
    CleanupTrackingAgent,
    IsolatedTestCase,
    MockAgentBackend,
    install_record,
    make_lifecycle,
    make_manager,
    make_rule,
    make_rule_derived_record,
    start_watcher,
)

# Patch load_state/save_state globally so tests never touch live state files.
_patch_load_state = patch("gateway.core.state_store.load_state", return_value=[])
_patch_save_state = patch("gateway.core.state_store.save_state")


# ── Shared helpers ─────────────────────────────────────────────────────────────



pytestmark = pytest.mark.integration

class FailingUnsubscribeConnector(ScriptConnector):
    def __init__(self, failing_room: str):
        super().__init__()
        self.failing_room = failing_room
        self.unsubscribed_rooms = []

    async def unsubscribe_room(self, room_id: str, watcher_id: str = "") -> None:
        self.unsubscribed_rooms.append((room_id, watcher_id))
        if room_id == self.failing_room:
            raise RuntimeError(f"boom for {room_id}")


def _make_lifecycle_r14(watcher_names=None):
    """Build a minimal WatcherLifecycle with mocked collaborators."""
    from gateway.core.config import WatcherConfig as CoreWatcherConfig

    if watcher_names is None:
        watcher_names = ["support"]

    watcher_configs = []
    for name in watcher_names:
        wc = MagicMock(spec=CoreWatcherConfig)
        wc.name = name
        wc.room = f"#{name}"
        wc.connector = "rc"
        wc.agent = None
        watcher_configs.append(wc)

    connector = MagicMock()
    connector.resolve_room = AsyncMock(return_value=MagicMock(id="room_id", type="c"))
    connector.subscribe_room = AsyncMock()
    connector.unsubscribe_room = AsyncMock()
    connector.get_last_processed_ts = MagicMock(return_value=None)
    connector.update_last_processed_ts = MagicMock()

    agent = MagicMock()
    agent.create_session = AsyncMock(return_value="session-abc123")
    agent.delete_session = AsyncMock(return_value=True)

    config = MagicMock(spec=CoreConfig)
    agent_cfg = MagicMock()
    agent_cfg.working_directory = "/tmp"
    agent_cfg.session_prefix = None
    config.agent_config = MagicMock(return_value=agent_cfg)

    state_store = MagicMock()
    state_store.load = MagicMock(return_value={})
    state_store.save = MagicMock()

    dispatcher = MagicMock()
    dispatcher.add_processor = MagicMock()
    dispatcher.remove_processor = MagicMock()
    # No watcher holds the room. A bare MagicMock would answer `holder()` with a truthy
    # mock, which now means "another watcher already serves it" (§4.1).
    dispatcher.holder = MagicMock(return_value=None)

    injector = MagicMock()
    injector.build = AsyncMock(return_value="built content")
    injector.ensure = AsyncMock(return_value=None)
    injector.reset_session = MagicMock()
    injector.status_for = MagicMock(return_value=MagicMock(state="done"))

    maps = MagicMock()
    maps.role = {}
    maps.permission_thread = {}
    maps.bind_session = MagicMock()
    maps.remove_session = MagicMock()

    lifecycle = make_lifecycle()
    lifecycle._connector = connector
    lifecycle._agents = {"default": agent}
    lifecycle._default_agent = "default"
    lifecycle._config = config
    lifecycle._state_store = state_store
    lifecycle._dispatcher = dispatcher
    lifecycle._injector = injector
    lifecycle._permission_registry = None
    lifecycle._maps = maps

    workspace = MagicMock()
    workspace.setup = MagicMock(return_value="/tmp/attachments")
    lifecycle._attachment_workspace = workspace

    return lifecycle, watcher_configs, connector, agent


# ── Tests from test_round4_fixes.py ───────────────────────────────────────────


class TestWatcherLifecycleLock(unittest.IsolatedAsyncioTestCase):
    """pause/resume/reset must be serialized per watcher via _get_watcher_lock."""

    def _make_lifecycle(self):

        lc = make_lifecycle()
        lc._connector = MagicMock()
        lc._agents = {}
        lc._default_agent = "default"
        lc._config = MagicMock()
        lc._state_store = MagicMock()
        lc._dispatcher = MagicMock(holder=MagicMock(return_value=None))
        lc._injector = MagicMock()
        lc._permission_registry = None
        lc._maps = MagicMock()
        lc._attachment_workspace = MagicMock()
        return lc

    def test_get_watcher_lock_creates_lock_lazily(self):
        """_get_watcher_lock creates a new asyncio.Lock on first call."""
        lc = self._make_lifecycle()
        lock = lc._get_watcher_lock("watcher_a")
        self.assertIsInstance(lock, asyncio.Lock)
        self.assertIn("watcher_a", lc._watcher_locks)

    def test_get_watcher_lock_returns_same_lock_on_repeat(self):
        """_get_watcher_lock returns the same lock on subsequent calls."""
        lc = self._make_lifecycle()
        lock1 = lc._get_watcher_lock("watcher_a")
        lock2 = lc._get_watcher_lock("watcher_a")
        self.assertIs(lock1, lock2)

    def test_different_watchers_have_different_locks(self):
        """Two different watcher names must get independent locks."""
        lc = self._make_lifecycle()
        lock_a = lc._get_watcher_lock("watcher_a")
        lock_b = lc._get_watcher_lock("watcher_b")
        self.assertIsNot(lock_a, lock_b)

    async def test_concurrent_operations_on_same_watcher_are_serialized(self):
        """Two concurrent lifecycle ops on the same watcher must not interleave."""
        lc = self._make_lifecycle()
        execution_order = []

        async def op1():
            async with lc._get_watcher_lock("w1"):
                execution_order.append("op1_start")
                await asyncio.sleep(0)
                execution_order.append("op1_end")

        async def op2():
            async with lc._get_watcher_lock("w1"):
                execution_order.append("op2_start")
                execution_order.append("op2_end")

        await asyncio.gather(op1(), op2())

        self.assertEqual(
            execution_order,
            ["op1_start", "op1_end", "op2_start", "op2_end"],
        )

    async def test_concurrent_operations_on_different_watchers_run_in_parallel(self):
        """Two concurrent lifecycle ops on DIFFERENT watchers must not block each other."""
        lc = self._make_lifecycle()
        started = []

        async def op_a():
            async with lc._get_watcher_lock("watcher_a"):
                started.append("a")
                await asyncio.sleep(0)

        async def op_b():
            async with lc._get_watcher_lock("watcher_b"):
                started.append("b")
                await asyncio.sleep(0)

        await asyncio.gather(op_a(), op_b())
        self.assertIn("a", started)
        self.assertIn("b", started)


# ── Tests from test_round6_fixes.py ───────────────────────────────────────────


class TestAttachmentWorkspaceInThread(unittest.IsolatedAsyncioTestCase):
    """start_watcher(WatcherLifecycle, ) must call setup() via asyncio.to_thread."""

    async def test_setup_called_via_to_thread(self):
        """setup() must be wrapped in asyncio.to_thread(), not called directly."""

        lc = make_lifecycle()
        # See the note on the other hand-built lifecycle: `holder()` is consulted
        # before provisioning now, and a bare MagicMock answers it truthily.
        lc._dispatcher = MagicMock(holder=MagicMock(return_value=None))

        maps = MagicMock()
        maps.bind_session = MagicMock()
        maps.remove_session = MagicMock()
        lc._maps = maps

        room = MagicMock()
        room.id = "room_1"
        room.type = "dm"
        connector = MagicMock()
        connector.resolve_room = AsyncMock(return_value=room)
        lc._connector = connector

        injector = MagicMock()
        injector.build = AsyncMock(return_value="built content")
        injector.ensure = AsyncMock(return_value=None)
        injector.reset_session = MagicMock()
        lc._injector = injector

        workspace = MagicMock()
        workspace.setup.return_value = "/tmp/attachments"
        lc._attachment_workspace = workspace

        agent_cfg = AgentConfig(timeout=30, working_directory="/tmp/work")
        config = MagicMock()
        config.agent_config.return_value = agent_cfg
        lc._config = config

        agent = MagicMock()
        agent.create_session = AsyncMock(return_value="ses_123")
        lc._agents = {"default": agent}

        wc = WatcherConfig(
            name="test-watcher",
            connector="rc",
            room="general",
            agent="default",
        )

        to_thread_calls: list = []
        original_to_thread = asyncio.to_thread

        async def spy_to_thread(fn, *args, **kwargs):
            to_thread_calls.append(fn)
            if fn is workspace.setup:
                return "/tmp/attachments"
            return await original_to_thread(fn, *args, **kwargs)

        with (
            patch("gateway.core.watcher_lifecycle.asyncio.to_thread", side_effect=spy_to_thread),
            patch.object(lc, "_resolve_agent_name", return_value="default"),
            patch.object(lc, "_provision_session", new_callable=AsyncMock, return_value=("ses_123", True)),
            patch.object(lc, "_cleanup_startup_session_best_effort", new_callable=AsyncMock),
        ):
            try:
                await start_watcher(lc, wc, None)
            except Exception:
                pass

        setup_calls = [fn for fn in to_thread_calls if fn is workspace.setup]
        self.assertGreaterEqual(len(setup_calls), 1, "setup() must be called via asyncio.to_thread")


# ── Tests from test_round8_fixes.py ───────────────────────────────────────────


class TestAttachmentWorkspaceRollback(unittest.IsolatedAsyncioTestCase):
    """When setup() raises, _states and _maps must be rolled back."""

    async def test_states_and_maps_rolled_back_on_setup_failure(self):
        """If attachment_workspace.setup() raises, state and maps must be cleaned up."""

        lc = make_lifecycle()
        # See the note on the other hand-built lifecycle: `holder()` is consulted
        # before provisioning now, and a bare MagicMock answers it truthily.
        lc._dispatcher = MagicMock(holder=MagicMock(return_value=None))

        maps = MagicMock()
        maps.bind_session = MagicMock()
        maps.remove_session = MagicMock()
        lc._maps = maps

        room = MagicMock()
        room.id = "room_1"
        room.type = "dm"
        connector = MagicMock()
        connector.resolve_room = AsyncMock(return_value=room)
        lc._connector = connector

        injector = MagicMock()
        injector.build = AsyncMock(return_value="built content")
        injector.ensure = AsyncMock(return_value=None)
        injector.reset_session = MagicMock()
        lc._injector = injector

        workspace = MagicMock()
        workspace.setup.side_effect = OSError("permission denied")
        lc._attachment_workspace = workspace

        agent_cfg = AgentConfig(timeout=30, working_directory="/tmp/work")
        config = MagicMock()
        config.agent_config.return_value = agent_cfg
        lc._config = config

        agent = MagicMock()
        lc._agents = {"default": agent}

        wc = WatcherConfig(
            name="test-watcher", connector="rc", room="general", agent="default"
        )

        with (
            patch.object(lc, "_resolve_agent_name", return_value="default"),
            patch.object(lc, "_provision_session", new_callable=AsyncMock, return_value=("ses_123", True)),
            patch.object(lc, "_cleanup_startup_session_best_effort", new_callable=AsyncMock),
            patch("gateway.core.watcher_lifecycle.asyncio.to_thread", new_callable=AsyncMock,
                  side_effect=OSError("permission denied")),
        ):
            with self.assertRaises(OSError):
                await start_watcher(lc, wc, None)

        self.assertIsNone(lc.get_watcher_state("test-watcher"), "_states must be rolled back after setup() failure")
        maps.remove_session.assert_called_once()


# ── Tests from test_round9_fixes.py ───────────────────────────────────────────


class TestContextInjectedResetOnSubscribeFailure(unittest.IsolatedAsyncioTestCase):
    """ws.context_injected must be reset when session destroyed on subscribe failure."""

    async def test_context_injected_reset_when_new_session_destroyed(self):
        """If new session is destroyed after subscribe fails, context_injected must be False."""

        lc = make_lifecycle()
        lc._permission_registry = MagicMock()
        # Hand-built lifecycle: the dispatcher is consulted before the session is
        # provisioned now, to refuse a room another watcher already holds (§4.1).
        lc._dispatcher = MagicMock(holder=MagicMock(return_value=None))

        maps = MagicMock()
        maps.bind_session = MagicMock()
        maps.remove_session = MagicMock()
        lc._maps = maps

        room = MagicMock()
        room.id = "room_1"
        room.type = "dm"
        connector = MagicMock()
        connector.resolve_room = AsyncMock(return_value=room)
        connector.subscribe_room = AsyncMock(side_effect=RuntimeError("subscribe failed"))
        lc._connector = connector

        injector = MagicMock()
        injector.build = AsyncMock(return_value="built content")
        injector.ensure = AsyncMock(return_value=None)
        injector.reset_session = MagicMock()
        lc._injector = injector

        workspace = MagicMock()
        workspace.setup = MagicMock()
        lc._attachment_workspace = workspace

        agent_cfg = AgentConfig(timeout=30, working_directory="/tmp/work")
        config = MagicMock()
        config.agent_config.return_value = agent_cfg
        lc._config = config

        agent = MagicMock()
        lc._agents = {"default": agent}

        wc = WatcherConfig(
            name="test-watcher", connector="rc", room="general", agent="default"
        )

        with (
            patch.object(lc, "_resolve_agent_name", return_value="default"),
            patch.object(lc, "_provision_session", new_callable=AsyncMock,
                         return_value=("ses_new", True)),
            patch.object(lc, "_cleanup_startup_session_best_effort",
                         new_callable=AsyncMock, return_value=True),
            patch("gateway.core.watcher_lifecycle.asyncio.to_thread",
                  new_callable=AsyncMock, return_value=None),
        ):
            with self.assertRaises(RuntimeError):
                await start_watcher(lc, wc, None)

        saved_ws = lc.get_watcher_state("test-watcher")
        self.assertIsNotNone(saved_ws)
        self.assertFalse(saved_ws.context_injected)

    async def test_context_injected_preserved_when_no_new_session(self):
        """When no new session was created, the id must survive a failed startup.

        Previously staged with a config-pinned `wc.session_id`; that field is gone, so
        the scenario is now driven by the case that still produces
        `created_new_session=False` — a session reused from persisted state. The
        property under test is unchanged: `ws.session_id` must not be blanked when the
        session was not this startup's to destroy.
        """

        lc = make_lifecycle()
        lc._permission_registry = MagicMock()
        # Hand-built lifecycle: the dispatcher is consulted before the session is
        # provisioned now, to refuse a room another watcher already holds (§4.1).
        lc._dispatcher = MagicMock(holder=MagicMock(return_value=None))

        maps = MagicMock()
        maps.bind_session = MagicMock()
        maps.remove_session = MagicMock()
        lc._maps = maps

        room = MagicMock()
        room.id = "room_1"
        room.type = "dm"
        connector = MagicMock()
        connector.resolve_room = AsyncMock(return_value=room)
        connector.subscribe_room = AsyncMock(side_effect=RuntimeError("subscribe failed"))
        lc._connector = connector

        injector = MagicMock()
        injector.build = AsyncMock(return_value="built content")
        injector.ensure = AsyncMock(return_value=None)
        injector.reset_session = MagicMock()
        lc._injector = injector

        workspace = MagicMock()
        workspace.setup = MagicMock()
        lc._attachment_workspace = workspace

        agent_cfg = AgentConfig(timeout=30, working_directory="/tmp/work")
        config = MagicMock()
        config.agent_config.return_value = agent_cfg
        lc._config = config

        agent = MagicMock()
        lc._agents = {"default": agent}

        wc = WatcherConfig(
            name="test-watcher2", connector="rc", room="general", agent="default",
        )

        with (
            patch.object(lc, "_resolve_agent_name", return_value="default"),
            # False = the session was reused, not created here.
            patch.object(lc, "_provision_session", new_callable=AsyncMock,
                         return_value=("persisted-session", False)),
            patch.object(lc, "_cleanup_startup_session_best_effort",
                         new_callable=AsyncMock, return_value=True),
            patch("gateway.core.watcher_lifecycle.asyncio.to_thread",
                  new_callable=AsyncMock, return_value=None),
        ):
            with self.assertRaises(RuntimeError):
                await start_watcher(lc, wc, None)

        saved_ws = lc.get_watcher_state("test-watcher2")
        self.assertIsNotNone(saved_ws)
        self.assertEqual(saved_ws.session_id, "persisted-session")


# ── Tests from test_round14_fixes.py ──────────────────────────────────────────


class TestGetWatcherLock(unittest.IsolatedAsyncioTestCase):
    """_get_watcher_lock must create locks lazily and return the same lock each time."""

    async def test_returns_same_lock_on_repeated_calls(self):
        lifecycle, _, _, _ = _make_lifecycle_r14(["support"])
        lock1 = lifecycle._get_watcher_lock("support")
        lock2 = lifecycle._get_watcher_lock("support")
        self.assertIs(lock1, lock2)

    async def test_different_watchers_get_different_locks(self):
        lifecycle, _, _, _ = _make_lifecycle_r14(["alpha", "beta"])
        lock_a = lifecycle._get_watcher_lock("alpha")
        lock_b = lifecycle._get_watcher_lock("beta")
        self.assertIsNot(lock_a, lock_b)

    async def test_lock_is_asyncio_lock(self):
        lifecycle, _, _, _ = _make_lifecycle_r14(["support"])
        lock = lifecycle._get_watcher_lock("support")
        self.assertIsInstance(lock, asyncio.Lock)


# ── A verb retries an unavailable agent (#158) ───────────────────────────────


class TestVerbRetriesUnavailableAgent(unittest.IsolatedAsyncioTestCase):
    """resume/reset try to bring a blocked agent up through the service's
    `recover_agent` callback before refusing; every other gate site stays a
    pure check, so a broken backend is retried on an operator's word only."""

    def _lifecycle(self, recover=None, blocked=("opencode",)):
        lc = make_lifecycle(
            agents={"opencode": MagicMock()},
            state_store=MagicMock(load=MagicMock(return_value={}), save=MagicMock()),
            recover_agent=recover,
        )
        lc._blocked_agents = set(blocked)
        record = make_rule_derived_record("support", agent="opencode")
        install_record(lc, record)
        return lc, record

    async def test_reset_recovers_a_blocked_agent_and_proceeds(self):
        recovered: list[str] = []

        async def recover(name):
            recovered.append(name)
            return True

        lc, _ = self._lifecycle(recover)
        with patch.object(lc, "_reset_locked", new_callable=AsyncMock) as inner:
            await lc.reset_watcher("support")

        self.assertEqual(recovered, ["opencode"])
        inner.assert_awaited_once()
        self.assertNotIn("opencode", lc._blocked_agents)

    async def test_resume_recovers_too_and_a_sibling_watcher_is_unblocked(self):
        async def recover(name):
            return True

        lc, _ = self._lifecycle(recover)
        install_record(lc, make_rule_derived_record("sales", agent="opencode"))

        with patch.object(lc, "_resume_locked", new_callable=AsyncMock):
            await lc.resume_watcher("support")
        # The agent is shared: recovering it for one watcher clears the gate
        # for the other, with no second recovery attempt.
        with patch.object(lc, "_reset_locked", new_callable=AsyncMock) as inner:
            await lc.reset_watcher("sales")
        inner.assert_awaited_once()

    async def test_still_broken_agent_is_refused_with_the_remedy(self):
        async def recover(name):
            return False

        lc, _ = self._lifecycle(recover)
        with self.assertRaises(RuntimeError) as ctx:
            await lc.reset_watcher("support")

        msg = str(ctx.exception)
        self.assertIn("agent 'opencode' is unavailable", msg)
        self.assertIn("coop legacy status", msg)
        self.assertIn("config reload", msg)
        self.assertIn("opencode", lc._blocked_agents)

    async def test_no_callback_keeps_the_fail_closed_refusal(self):
        lc, _ = self._lifecycle(recover=None)
        with self.assertRaises(RuntimeError) as ctx:
            await lc.resume_watcher("support")
        self.assertIn("is unavailable", str(ctx.exception))

    async def test_disarmed_lifecycle_refuses_before_trying_to_recover(self):
        """The recovery awaits a backend start (up to its startup timeout);
        it must sit inside the verb's inflight accounting, after the disarm
        check, or shutdown's drain could complete around it."""
        async def recover(name):
            self.fail("recovery must not run once transitions are disarmed")

        lc, _ = self._lifecycle(recover)
        lc._disarmed = True
        with self.assertRaises(RuntimeError) as ctx:
            await lc.reset_watcher("support")
        self.assertIn("Cannot reset", str(ctx.exception))

    async def test_a_creation_start_does_not_try_to_recover(self):
        """start_watcher_in_room's step-0 gate is reached by message wakes and
        the eager boot loop; those keep refusing — retrying a broken backend
        per inbound message is not the increment (owner decision)."""
        async def recover(name):
            self.fail("step 0 must stay a pure check")

        lc, record = self._lifecycle(recover)
        wc = config_from_record(record)
        room = Room(id=record.room_id, name="support", type=record.room_kind or record.room_type)
        with self.assertRaises(RuntimeError) as ctx:
            await lc.start_watcher_in_room(wc, record, room)
        self.assertIn("is unavailable", str(ctx.exception))


# ── Tests from test_round16_fixes.py ──────────────────────────────────────────


class TestSyncWatchersPreservesBlockedAgents(unittest.IsolatedAsyncioTestCase):
    """sync_watchers(unavailable_agents=None) must not reset _blocked_agents."""

    def _make_lifecycle(self):

        lc = make_lifecycle()
        lc._connector = MagicMock()
        lc._agents = {"default": MagicMock()}
        lc._default_agent = "default"
        lc._config = MagicMock()
        lc._state_store = MagicMock()
        lc._state_store.load = MagicMock(return_value={})
        lc._state_store.save = MagicMock()
        lc._dispatcher = MagicMock(holder=MagicMock(return_value=None))
        lc._injector = MagicMock()
        lc._permission_registry = None
        lc._maps = MagicMock()
        lc._attachment_workspace = MagicMock()
        return lc

    async def test_none_unavailable_agents_preserves_blocked_set(self):
        """sync_watchers(None) must not overwrite a previously populated _blocked_agents."""
        lc = self._make_lifecycle()
        lc._blocked_agents = {"opencode"}

        await lc.sync_watchers(unavailable_agents=None)

        self.assertIn("opencode", lc._blocked_agents)

    async def test_explicit_empty_set_clears_blocked_agents(self):
        """sync_watchers(set()) explicitly clears blocked agents."""
        lc = self._make_lifecycle()
        lc._blocked_agents = {"opencode"}

        await lc.sync_watchers(unavailable_agents=set())

        self.assertNotIn("opencode", lc._blocked_agents)

    async def test_explicit_set_updates_blocked_agents(self):
        """sync_watchers({'agent-x'}) replaces _blocked_agents with the new set."""
        lc = self._make_lifecycle()
        lc._blocked_agents = {"old-agent"}

        await lc.sync_watchers(unavailable_agents={"new-agent"})

        self.assertNotIn("old-agent", lc._blocked_agents)
        self.assertIn("new-agent", lc._blocked_agents)

    async def test_first_call_with_none_starts_with_empty_set(self):
        """On first startup (previously empty), None leaves _blocked_agents empty."""
        lc = self._make_lifecycle()

        await lc.sync_watchers(unavailable_agents=None)

        self.assertEqual(lc._blocked_agents, set())


# ── Tests from test_code_review_fixes.py ──────────────────────────────────────


class TestWatcherLifecycleHardening(IsolatedTestCase):
    async def test_new_session_cleaned_up_when_context_injection_fails(self):
        connector = ScriptConnector()
        agent = CleanupTrackingAgent()
        manager = make_manager(connector, agent, watcher_rules=[make_rule()])

        with patch.object(
            manager._lifecycle._injector,
            "ensure",
            AsyncMock(side_effect=RuntimeError("inject failed")),
        ):
            errors = await manager.run_once()

        self.assertEqual(len(errors), 1)
        self.assertEqual(agent.deleted_sessions, ["mock-session-0001"])
        self.assertFalse(manager._lifecycle._processors)

        await manager.shutdown()

    async def test_shutdown_continues_when_one_unsubscribe_fails(self):
        connector = FailingUnsubscribeConnector(failing_room="room-a")
        agent = MockAgentBackend()
        manager = make_manager(
            connector,
            agent,
            watcher_rules=[
                make_rule("room-a"),
                make_rule("room-b"),
            ],
        )
        await manager.run_once()

        await manager.shutdown()

        self.assertEqual(manager._lifecycle._processors, {})
        # The watcher is identified to the connector by its room id, not its handle
        # (a handle follows a rename; the subscription must not).
        self.assertIn(("room-a", "room-a"), connector.unsubscribed_rooms)
        self.assertIn(("room-b", "room-b"), connector.unsubscribed_rooms)

    async def test_subscribe_failure_clears_deleted_fresh_session_from_state(self):
        connector = ScriptConnector()
        agent = CleanupTrackingAgent()
        manager = make_manager(connector, agent, watcher_rules=[make_rule()])

        with patch.object(
            connector,
            "subscribe_room",
            AsyncMock(side_effect=RuntimeError("subscribe failed")),
        ):
            errors = await manager.run_once()

        self.assertEqual(len(errors), 1)
        state = manager._lifecycle.get_watcher_state("default:script")
        self.assertIsNotNone(state)
        self.assertEqual(state.session_id, "")
        self.assertEqual(agent.deleted_sessions, ["mock-session-0001"])

        await manager.shutdown()


class TestUnavailableAgentsBlocksWatchers(IsolatedTestCase):
    """P0-2: sync_watchers() must skip watchers whose agent's broker failed."""

    async def test_unavailable_agent_skips_watcher_with_error(self):
        """Watcher using a permission-broker-failed agent must not start."""
        connector = ScriptConnector()
        agent = MockAgentBackend()

        manager = make_manager(
            connector, agent, watcher_rules=[make_rule()]
        )

        errors = await manager.run_once(unavailable_agents={"default"})

        self.assertIsNone(manager.get_processor("default:script"))
        self.assertTrue(
            any("default" in e for e in errors),
            f"Expected 'default' in errors: {errors}",
        )

        await manager.shutdown()

    async def test_non_blocked_agent_starts_normally(self):
        """Watchers using an agent with a healthy broker must start as usual."""
        connector = ScriptConnector()
        agent = MockAgentBackend()

        manager = make_manager(
            connector, agent, watcher_rules=[make_rule()]
        )

        errors = await manager.run_once(unavailable_agents={"other-agent"})

        self.assertIsNotNone(manager.get_processor("default:script"))
        self.assertEqual(errors, [], f"Unexpected errors: {errors}")

        await manager.shutdown()

    async def test_empty_unavailable_agents_no_effect(self):
        """Empty unavailable_agents set must not affect normal startup."""
        connector = ScriptConnector()
        agent = MockAgentBackend()

        manager = make_manager(
            connector, agent, watcher_rules=[make_rule()]
        )

        errors = await manager.run_once(unavailable_agents=set())

        self.assertIsNotNone(manager.get_processor("default:script"))
        self.assertEqual(errors, [])

        await manager.shutdown()


class TestSyncWatchersPruneSemantics(unittest.IsolatedAsyncioTestCase):
    """sync_watchers() must name what it deletes, and delete only that.

    Post-cutover the rule is the record's own shape: a static-era record (no
    `rule_name`) has no owner left — config.yaml cannot name it, no rule
    recreates it — and is pruned; a rule-derived record is never pruned by
    this loop, whatever its state, because "absent from config" is its normal
    condition (§2.4)."""

    async def test_a_static_era_record_is_pruned(self):
        lc, _wcs, connector, _agent = _make_lifecycle_r14()
        lc._state_store.load = MagicMock(
            return_value={
                "old-static": WatcherState(
                    watcher_name="old-static", session_id="s2", room_id="r2"),
            }
        )

        await lc.sync_watchers()

        prune = lc._state_store.save.call_args.kwargs["prune"]
        self.assertEqual(prune, {"old-static"})

    async def test_a_rule_derived_record_is_never_pruned(self):
        from tests.helpers import make_rule_derived_record

        lc, _wcs, connector, _agent = _make_lifecycle_r14()
        record = make_rule_derived_record(name="kept")
        lc._state_store.load = MagicMock(return_value={"kept": record})

        await lc.sync_watchers()

        self.assertEqual(lc._state_store.save.call_args.kwargs["prune"], set())
        self.assertIs(lc.get_watcher_state("kept"), record, "hydrated, not forgotten")


if __name__ == "__main__":
    unittest.main()
