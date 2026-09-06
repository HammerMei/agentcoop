"""Tests for AgentRuntimeManager (P2-4).

Covers:
  - Successful start: backends + brokers started in order
  - Backend failure: broker skipped, agent marked unavailable
  - Broker failure: agent marked unavailable
  - Mixed: some succeed, some fail
  - stop_all: brokers stopped before backends
  - has_active_brokers reflects reality
"""

from __future__ import annotations

import asyncio
import unittest
from types import MappingProxyType
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.agents import AgentBackend
from gateway.agents.response import AgentResponse
from gateway.core.permission import PermissionRegistry
from gateway.core.session_maps import SessionMaps
from gateway.service import AgentRuntimeManager

# ── Test backend ──────────────────────────────────────────────────────────────


class _TestBackend(AgentBackend):
    """Configurable backend for runtime manager tests."""

    def __init__(
        self,
        *,
        start_error: Exception | None = None,
        broker_error: Exception | None = None,
        has_broker: bool = False,
    ):
        self._start_error = start_error
        self._broker_error = broker_error
        self._has_broker = has_broker
        self.started = False
        self.stopped = False
        self._mock_broker: MagicMock | None = None
        self._captured_maps: tuple | None = None

    async def start(self) -> None:
        if self._start_error:
            raise self._start_error
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    def create_gateway_broker(
        self,
        registry,
        notifier,
        session_room_map,
        session_role_map,
        session_permission_thread_map,
    ):
        self._captured_maps = (
            session_room_map,
            session_role_map,
            session_permission_thread_map,
        )
        if not self._has_broker:
            return None
        if self._broker_error:
            raise self._broker_error
        broker = MagicMock()
        broker.start = AsyncMock()
        broker.stop = AsyncMock()
        self._mock_broker = broker
        return broker

    async def create_session(self, *a, **kw):
        return "ses_001"

    async def send(self, *a, **kw):
        return AgentResponse(text="ok")


class _BlockingBackend(_TestBackend):
    def __init__(self, gate: asyncio.Event):
        super().__init__()
        self._gate = gate
        self.started_event = asyncio.Event()

    async def start(self) -> None:
        self.started_event.set()
        await self._gate.wait()
        await super().start()


def _make_notifier():
    n = MagicMock()
    n.notify = AsyncMock(return_value=True)
    return n


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestAgentRuntimeManager(unittest.IsolatedAsyncioTestCase):
    async def test_successful_start_no_brokers(self):
        """Agents without brokers start cleanly with no errors."""
        backend = _TestBackend()
        mgr = AgentRuntimeManager({"default": backend})

        errors = await mgr.start_all(
            registry=PermissionRegistry(),
            notifier=_make_notifier(),
            maps=SessionMaps(),
        )

        self.assertEqual(errors, [])
        self.assertTrue(backend.started)
        self.assertEqual(mgr.unavailable_agents, set())
        self.assertFalse(mgr.has_active_brokers)

    async def test_successful_start_with_broker(self):
        """Agent with permission broker: both backend and broker started."""
        backend = _TestBackend(has_broker=True)
        mgr = AgentRuntimeManager({"default": backend})

        errors = await mgr.start_all(
            registry=PermissionRegistry(),
            notifier=_make_notifier(),
            maps=SessionMaps(),
        )

        self.assertEqual(errors, [])
        self.assertTrue(backend.started)
        self.assertTrue(mgr.has_active_brokers)
        self.assertEqual(mgr.unavailable_agents, set())
        backend._mock_broker.start.assert_called_once()
        self.assertIsInstance(backend._captured_maps[0], MappingProxyType)
        self.assertIsInstance(backend._captured_maps[1], MappingProxyType)
        self.assertIsInstance(backend._captured_maps[2], MappingProxyType)

    async def test_backend_failure_skips_broker_and_marks_unavailable(self):
        """Failed backend → broker not attempted, agent unavailable."""
        backend = _TestBackend(
            start_error=RuntimeError("opencode serve died"),
            has_broker=True,
        )
        mgr = AgentRuntimeManager({"broken": backend})

        errors = await mgr.start_all(
            registry=PermissionRegistry(),
            notifier=_make_notifier(),
            maps=SessionMaps(),
        )

        self.assertEqual(len(errors), 1)
        self.assertIn("broken", errors[0])
        self.assertIn("broken", mgr.unavailable_agents)
        self.assertFalse(mgr.has_active_brokers)
        # Broker should never have been created
        self.assertIsNone(backend._mock_broker)

    async def test_broker_failure_marks_agent_unavailable(self):
        """Backend OK but broker fails → agent unavailable."""
        backend = _TestBackend(
            has_broker=True,
            broker_error=RuntimeError("broker SSE failed"),
        )
        mgr = AgentRuntimeManager({"perm_agent": backend})

        errors = await mgr.start_all(
            registry=PermissionRegistry(),
            notifier=_make_notifier(),
            maps=SessionMaps(),
        )

        self.assertEqual(len(errors), 1)
        self.assertIn("perm_agent", errors[0])
        self.assertIn("perm_agent", mgr.unavailable_agents)
        self.assertTrue(backend.started)
        self.assertTrue(backend.stopped)
        self.assertFalse(mgr.has_active_brokers)

    async def test_mixed_agents_isolate_failures(self):
        """One agent fails, another succeeds — only the failing one is unavailable."""
        good = _TestBackend(has_broker=True)
        bad = _TestBackend(start_error=RuntimeError("boom"))
        mgr = AgentRuntimeManager({"good": good, "bad": bad})

        errors = await mgr.start_all(
            registry=PermissionRegistry(),
            notifier=_make_notifier(),
            maps=SessionMaps(),
        )

        self.assertEqual(len(errors), 1)
        self.assertIn("bad", mgr.unavailable_agents)
        self.assertNotIn("good", mgr.unavailable_agents)
        self.assertTrue(good.started)
        self.assertTrue(mgr.has_active_brokers)

    async def test_stop_all_stops_brokers_and_backends(self):
        """stop_all stops active brokers and all backends."""
        backend = _TestBackend(has_broker=True)
        mgr = AgentRuntimeManager({"default": backend})

        await mgr.start_all(
            registry=PermissionRegistry(),
            notifier=_make_notifier(),
            maps=SessionMaps(),
        )
        broker = backend._mock_broker
        self.assertIsNotNone(broker)

        await mgr.stop_all()

        broker.stop.assert_called_once()
        self.assertTrue(backend.stopped)
        self.assertFalse(mgr.has_active_brokers)

    async def test_stop_all_tolerates_broker_stop_error(self):
        """Broker stop error is logged, not raised — backends still stopped.
        The stop is retried (#144) and the broker kept as a leftover."""
        backend = _TestBackend(has_broker=True)
        mgr = AgentRuntimeManager({"default": backend})

        await mgr.start_all(
            registry=PermissionRegistry(),
            notifier=_make_notifier(),
            maps=SessionMaps(),
        )
        backend._mock_broker.stop.side_effect = RuntimeError("stop failed")

        # Must not raise
        with patch("gateway.core.retry_stop.STOP_RETRY_DELAY", 0.0):
            await mgr.stop_all()

        # Backend still stopped despite broker error
        self.assertTrue(backend.stopped)
        self.assertEqual([(n, k) for n, k, _ in mgr.leftovers], [("default", "permission broker")])

    async def test_backend_start_phase_runs_agents_in_parallel(self):
        """Independent backend startups should overlap instead of running serially."""
        gate = asyncio.Event()
        a = _BlockingBackend(gate)
        b = _BlockingBackend(gate)
        mgr = AgentRuntimeManager({"a": a, "b": b})

        task = asyncio.create_task(
            mgr.start_all(
                registry=PermissionRegistry(),
                notifier=_make_notifier(),
                maps=SessionMaps(),
            )
        )

        await asyncio.wait_for(a.started_event.wait(), timeout=1.0)
        await asyncio.wait_for(b.started_event.wait(), timeout=1.0)
        gate.set()
        errors = await asyncio.wait_for(task, timeout=1.0)

        self.assertEqual(errors, [])
        self.assertTrue(a.started)
        self.assertTrue(b.started)

    async def test_stop_all_logs_backend_stop_exceptions_but_continues(self):
        backend = _TestBackend()
        backend.stop = AsyncMock(side_effect=RuntimeError("stop boom"))
        mgr = AgentRuntimeManager({"default": backend})

        with patch("gateway.core.retry_stop.STOP_RETRY_DELAY", 0.0):
            await mgr.stop_all()

        # Retried a few times (#144), then kept as a leftover — nothing raised.
        self.assertEqual(backend.stop.await_count, 3 + 3, "stop_some's attempts, then retry_leftovers'")
        self.assertEqual([(n, k) for n, k, _ in mgr.leftovers], [("default", "backend")])


if __name__ == "__main__":
    unittest.main()
