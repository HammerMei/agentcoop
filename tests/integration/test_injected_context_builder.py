"""Tests for gateway.core.injected_context_builder.

Consolidates InjectedContextBuilder tests (formerly ContextInjector, see issue
#52 — durable system prompt).  The class split into two independently testable
halves:

  - ``build()``  — pure-ish file I/O, no agent calls. Covered by:
      TestBuildAllFilesOversized, TestBuildTOCTOU, TestBuildMissingFile,
      TestBuildNoContextFiles, TestInjectedContextBuilderHeader.
  - ``ensure()`` — retry bookkeeping around agent.ensure_durable_instructions().
      Covered by: TestConcurrentEnsureGuard, TestEnsurePendingReset,
      TestEnsureRetryCap, TestEnsureForwardsAlreadyDelivered.

Plus integration-level coverage against a real SessionManager:
  - TestContextInjectionOrdering (issue #9: maps registered before injection)
  - TestAsyncFileIOInContextInjection (issue #15: async file IO)

Run with:
    uv run python -m pytest tests/integration/test_injected_context_builder.py -v
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gateway.agents import AgentBackend
from gateway.agents.response import AgentResponse
from gateway.config import AgentConfig, WatcherConfig
from gateway.core.config import CoreConfig
from gateway.core.injected_context_builder import (
    _MAX_FILE_SIZE,
    _MAX_INJECT_ATTEMPTS,
    InjectedContextBuilder,
    InjectionStatus,
)
from gateway.state import WatcherState

pytestmark = pytest.mark.integration

# ── Shared helpers ────────────────────────────────────────────────────────────


def _make_injector() -> InjectedContextBuilder:
    config = CoreConfig(
        agents={"default": AgentConfig(timeout=10)},
    )
    return InjectedContextBuilder(config)


def _make_ws(context_injected: bool = False) -> WatcherState:
    return WatcherState(
        watcher_name="test", session_id="", room_id="r1",
        context_injected=context_injected,
    )


def _make_wc(ctx_files: list[str]) -> WatcherConfig:
    return WatcherConfig(
        name="test",
        connector="rc",
        room="general",
        agent="default",
        context_inject_files=ctx_files,
    )


async def _run_ensure(injector, ws, session_id, agent, content="context", watcher_name="test"):
    """Call ensure() with a pre-built content string (no file I/O involved).

    The path key is derived from the watcher name here purely so callers stay readable.
    In production they are unrelated: the name is a label, the key is a digest of
    (connector, room_id) — which is the whole point of the split (§2.3).
    """
    return await injector.ensure(
        ws, session_id, agent, "/tmp", 10,
        watcher_name=watcher_name, path_key=f"key-{watcher_name}", content=content,
    )


class _FakeAgent(AgentBackend):
    """AgentBackend that explicitly opts into the shared
    _send_once_as_durable_fallback() (mirroring OpenCodeBackend's own
    override), so these tests exercise the actual production fallback path
    rather than a hand-mocked substitute. ensure_durable_instructions() has
    no usable default (see AgentBackend) — this class must implement it."""

    def __init__(self, send_response: AgentResponse | None = None):
        self._send_response = send_response or AgentResponse(text="ok")
        self.send_calls: list[dict] = []

    async def create_session(self, *a, **kw):
        return "ses_001"

    async def send(
        self, session_id, prompt, working_directory, timeout,
        attachments=None, env=None, append_system_prompt_file=None,
    ):
        self.send_calls.append({"prompt": prompt, "session_id": session_id})
        return self._send_response

    async def ensure_durable_instructions(
        self, session_id, working_directory, timeout, content,
        *, path_key, already_delivered,
    ):
        return await self._send_once_as_durable_fallback(
            session_id, working_directory, timeout, content, already_delivered,
        )


# ── Tests: build() — oversized context files ─────────────────────────────────


class TestBuildAllFilesOversized(unittest.IsolatedAsyncioTestCase):
    """When all context files exceed the size limit, build() excludes their
    content but the identity header is still present (it is unconditional)."""

    async def test_all_oversized_files_excluded_but_header_present(self):
        injector = _make_injector()
        wc = _make_wc(["/tmp/ctx.md"])

        async def fake_to_thread(fn, *args, **kwargs):
            name = getattr(fn, "__name__", "")
            if "exists" in name:
                return True
            if "stat" in name:
                stat = MagicMock()
                stat.st_size = _MAX_FILE_SIZE + 1
                return stat
            return None

        with patch(
            "gateway.core.injected_context_builder.asyncio.to_thread",
            side_effect=fake_to_thread,
        ):
            content = await injector.build("default", "rc", wc)

        self.assertIn("## Coop Session Identity", content)

    async def test_partial_oversized_injects_small_files(self):
        """Files under the size limit are still included even if others are oversized."""
        injector = _make_injector()
        wc = _make_wc(["/tmp/big.md", "/tmp/small.md"])

        async def fake_to_thread(fn, *args, **kwargs):
            path_arg = args[0] if args else None
            name = getattr(fn, "__name__", "")
            if "exists" in name:
                return True
            if "stat" in name:
                stat = MagicMock()
                stat.st_size = _MAX_FILE_SIZE + 1 if "big" in str(path_arg) else 100
                return stat
            if "read_text" in name:
                return "small context content"
            return None

        with patch(
            "gateway.core.injected_context_builder.asyncio.to_thread",
            side_effect=fake_to_thread,
        ):
            content = await injector.build("default", "rc", wc)

        self.assertIn("small context content", content)
        self.assertIn("## Coop Session Identity", content)


# ── Tests: build() — TOCTOU re-validation ────────────────────────────────────


class TestBuildTOCTOU(unittest.IsolatedAsyncioTestCase):
    """After read_text(), content size must be re-validated to close TOCTOU window."""

    async def test_oversized_content_after_read_is_skipped(self):
        """Content that grew between stat() and read_text() must be rejected."""
        injector = _make_injector()
        wc = _make_wc(["/tmp/ctx.md"])

        oversized_content = "x" * (_MAX_FILE_SIZE + 1)

        async def fake_to_thread(fn, *args, **kwargs):
            name = getattr(fn, "__name__", str(fn))
            if "exists" in name:
                return True
            if "stat" in name:
                stat = MagicMock()
                stat.st_size = _MAX_FILE_SIZE - 1  # stat says ok
                return stat
            if "read_text" in name:
                return oversized_content  # content grew after stat
            return None

        with patch(
            "gateway.core.injected_context_builder.asyncio.to_thread",
            side_effect=fake_to_thread,
        ):
            content = await injector.build("default", "rc", wc)

        self.assertNotIn(oversized_content, content)
        self.assertIn("## Coop Session Identity", content)


# ── Tests: build() — missing file ────────────────────────────────────────────


class TestBuildMissingFile(unittest.IsolatedAsyncioTestCase):
    async def test_missing_file_raises(self):
        injector = _make_injector()
        wc = _make_wc(["/tmp/does-not-exist.md"])

        async def fake_to_thread(fn, *args, **kwargs):
            name = getattr(fn, "__name__", "")
            if "exists" in name:
                return False
            return None

        with patch(
            "gateway.core.injected_context_builder.asyncio.to_thread",
            side_effect=fake_to_thread,
        ):
            with self.assertRaises(FileNotFoundError):
                await injector.build("default", "rc", wc)


# ── Tests: build() — header is unconditional (issue #52 second bug) ─────────


class TestBuildNoContextFiles(unittest.IsolatedAsyncioTestCase):
    """Regression for issue #52's second bug: previously, watchers with neither
    context_inject_files nor history_context configured never received the
    identity/addressing header at all. build() must now always include it."""

    async def test_header_present_even_with_no_files_configured(self):
        injector = _make_injector()
        wc = _make_wc([])
        content = await injector.build("default", "rc", wc, agent_username="bot")
        self.assertIn("## Coop Session Identity", content)
        self.assertIn("## Multi-Agent Addressing", content)

    async def test_header_present_without_agent_username(self):
        injector = _make_injector()
        wc = _make_wc([])
        content = await injector.build("default", "rc", wc)
        self.assertIn("## Coop Session Identity", content)
        self.assertNotIn("## Multi-Agent Addressing", content)


class TestInjectedContextBuilderHeader(unittest.IsolatedAsyncioTestCase):
    """Identity header ordering relative to static context files in build()'s
    return value. Moved from the old agent.send()-capture-based assertions —
    build() is now pure I/O with no agent involvement at all."""

    async def test_header_prepended_before_file_content(self):
        injector = _make_injector()
        wc = WatcherConfig(
            name="my-watcher",
            connector="rc-home",
            room="general",
            agent="default",
            context_inject_files=["/tmp/ctx.md"],
        )

        async def fake_to_thread(fn, *args, **kwargs):
            name = getattr(fn, "__name__", str(fn))
            if "exists" in name:
                return True
            if "stat" in name:
                s = MagicMock()
                s.st_size = 50
                return s
            if "read_text" in name:
                return "# Static Context\nHello!"
            return None

        with patch(
            "gateway.core.injected_context_builder.asyncio.to_thread",
            side_effect=fake_to_thread,
        ):
            content = await injector.build(
                "default", "rc-home", wc, agent_username="bot"
            )

        header_pos = content.find("## Coop Session Identity")
        static_pos = content.find("# Static Context")
        self.assertGreater(header_pos, -1, "Header must be present")
        self.assertGreater(static_pos, -1, "Static file content must be present")
        self.assertLess(header_pos, static_pos, "Header must come before static content")
        self.assertIn("my-watcher", content)
        self.assertIn("rc-home", content)
        self.assertIn("to: @all", content)
        self.assertIn("broader fan-out is intentional", content)
        self.assertIn("priority responders", content)
        self.assertIn("ONLY `<end-of-agent-chain>`", content)


# ── Tests: ensure() — concurrent-call serialization ──────────────────────────


class TestConcurrentEnsureGuard(unittest.IsolatedAsyncioTestCase):
    """Concurrent ensure() calls for the SAME session_id must be serialized,
    not deduplicated. WatcherLifecycle already prevents the same watcher name
    from calling _start_watcher() concurrently with itself (per-watcher-name
    lock in watcher_lifecycle.py), so a session_id collision here only ever
    happens when two DIFFERENT watchers share a pinned session_id — each one
    genuinely needs its own agent.ensure_durable_instructions() call (its own
    watcher_name/content). An earlier "if already in-flight, bail with None"
    guard silently dropped the second watcher's durable content forever (no
    retry path once the per-message retry loop was removed) — this was a
    real regression caught in design review. Waiting instead of bailing
    fixes it."""

    async def test_second_call_waits_then_still_runs_its_own_attempt(self):
        """A call arriving while another is in-flight for the same session_id
        must wait, then still invoke the backend with ITS OWN watcher_name —
        never return None just because it was second."""
        injector = _make_injector()
        session_id = "ses_concurrent"
        injector._inject_status[session_id] = InjectionStatus(state="pending")

        ws = _make_ws()
        agent = _FakeAgent()

        result = await _run_ensure(
            injector, ws, session_id, agent, watcher_name="watcher-b"
        )

        # The stale "pending" left over from a (simulated) prior in-flight
        # call must NOT suppress this call — it should have run for real via
        # the default fallback's one-time send().
        self.assertEqual(len(agent.send_calls), 1)
        self.assertTrue(ws.context_injected)
        self.assertIsNone(result, "default fallback fully handles delivery — nothing to re-supply")

    async def test_concurrent_ensures_for_different_watchers_both_run(self):
        """Two concurrent ensure() calls for the SAME session_id but
        DIFFERENT watcher_name/content (simulating two watchers sharing a
        pinned session_id) must both invoke the backend — serialized, not
        dropped — each with its own arguments."""
        injector = _make_injector()
        session_id = "ses_race"
        ws1 = _make_ws()
        ws2 = _make_ws()

        seen: list[tuple[str, str]] = []

        class _SlowAgent(AgentBackend):
            async def create_session(self, *a, **kw):
                return "ses_001"

            async def ensure_durable_instructions(
                self, session_id, working_directory, timeout, content,
                *, path_key, already_delivered,
            ):
                await asyncio.sleep(0)
                # The backend sees the path key, not the display name — asserting on it
                # is what pins the new contract rather than the old one.
                seen.append((path_key, content))
                return f"/tmp/.acg-system-prompt/{path_key}.md"

            async def send(self, *a, **kw):
                raise NotImplementedError

        agent = _SlowAgent()
        results = await asyncio.gather(
            _run_ensure(injector, ws1, session_id, agent, content="watcher-a content", watcher_name="watcher-a"),
            _run_ensure(injector, ws2, session_id, agent, content="watcher-b content", watcher_name="watcher-b"),
        )

        self.assertEqual(len(seen), 2, "both watchers' calls must reach the backend — neither dropped")
        self.assertIn(("key-watcher-a", "watcher-a content"), seen)
        self.assertIn(("key-watcher-b", "watcher-b content"), seen)
        self.assertEqual(set(results), {
            "/tmp/.acg-system-prompt/key-watcher-a.md",
            "/tmp/.acg-system-prompt/key-watcher-b.md",
        })


# ── Tests: ensure() — pending status reset on unexpected exception ──────────


class TestEnsurePendingReset(unittest.IsolatedAsyncioTestCase):
    """ensure() must reset 'pending' -> 'not_started' on unexpected (non-
    AgentExecutionError) exceptions, and must NOT count them as a retry
    failure."""

    async def test_pending_reset_after_unexpected_exception(self):
        injector = _make_injector()
        session_id = "ses_io_error"
        ws = _make_ws()

        class _RaisingAgent(AgentBackend):
            async def create_session(self, *a, **kw):
                return "ses_001"

            async def ensure_durable_instructions(self, *a, **kw):
                raise OSError("permission denied")

            async def send(self, *a, **kw):
                raise NotImplementedError

        agent = _RaisingAgent()
        with self.assertRaises(OSError):
            await _run_ensure(injector, ws, session_id, agent)

        status = injector.status_for(session_id)
        self.assertNotEqual(status.state, "pending")
        self.assertEqual(status.state, "not_started")
        self.assertEqual(status.failure_count, 0, "Unexpected exceptions must not count as failures")

    async def test_retry_allowed_after_unexpected_exception(self):
        """A second ensure() call must NOT bail early after a first unexpected error."""
        injector = _make_injector()
        session_id = "ses_retry"
        ws = _make_ws()

        call_count = 0

        class _FlakyAgent(AgentBackend):
            async def create_session(self, *a, **kw):
                return "ses_001"

            async def ensure_durable_instructions(self, *a, **kw):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise OSError("transient error")
                return None

            async def send(self, *a, **kw):
                raise NotImplementedError

        agent = _FlakyAgent()
        with self.assertRaises(OSError):
            await _run_ensure(injector, ws, session_id, agent)

        await _run_ensure(injector, ws, session_id, agent)

        self.assertEqual(call_count, 2)
        self.assertTrue(ws.context_injected)


# ── Tests: ensure() — retry cap (via the real default fallback) ─────────────


class TestEnsureRetryCap(unittest.IsolatedAsyncioTestCase):
    """InjectedContextBuilder.ensure() gives up after _MAX_INJECT_ATTEMPTS
    persistent AgentExecutionErrors. These tests drive the REAL default
    AgentBackend.ensure_durable_instructions() fallback (which raises
    AgentExecutionError on an is_error response) rather than mocking the
    error path directly."""

    async def test_single_failure_does_not_mark_injected(self):
        injector = _make_injector()
        ws = _make_ws()
        agent = _FakeAgent(send_response=AgentResponse(text="error!", is_error=True))

        await _run_ensure(injector, ws, "ses_1", agent)

        self.assertFalse(ws.context_injected)
        status = injector.status_for("ses_1")
        self.assertEqual(status.failure_count, 1)
        self.assertEqual(status.state, "failed_retryable")

    async def test_failure_count_increments_per_attempt(self):
        injector = _make_injector()
        ws = _make_ws()
        agent = _FakeAgent(send_response=AgentResponse(text="error!", is_error=True))

        for i in range(1, _MAX_INJECT_ATTEMPTS):
            await _run_ensure(injector, ws, "ses_1", agent)
            self.assertEqual(injector.status_for("ses_1").failure_count, i)
            self.assertFalse(ws.context_injected)

    async def test_max_attempts_marks_session_degraded_without_fake_success(self):
        injector = _make_injector()
        ws = _make_ws()
        agent = _FakeAgent(send_response=AgentResponse(text="persistent error", is_error=True))

        for _ in range(_MAX_INJECT_ATTEMPTS):
            await _run_ensure(injector, ws, "ses_1", agent)

        self.assertTrue(
            ws.context_injected,
            "Degraded state still sets context_injected=True to stop retry storms",
        )
        status = injector.status_for("ses_1")
        self.assertEqual(status.state, "failed_degraded")
        self.assertEqual(status.failure_count, _MAX_INJECT_ATTEMPTS)

    async def test_success_clears_failure_counter(self):
        injector = _make_injector()
        ws = _make_ws()
        agent = _FakeAgent(send_response=AgentResponse(text="error!", is_error=True))

        await _run_ensure(injector, ws, "ses_1", agent)
        self.assertEqual(injector.status_for("ses_1").failure_count, 1)

        agent._send_response = AgentResponse(text="ok")
        await _run_ensure(injector, ws, "ses_1", agent)

        self.assertTrue(ws.context_injected)
        self.assertEqual(injector.status_for("ses_1").state, "injected")
        self.assertEqual(injector.status_for("ses_1").failure_count, 0)

    async def test_failure_counters_are_independent_per_session(self):
        injector = _make_injector()
        ws1 = _make_ws()
        ws2 = _make_ws()
        agent = _FakeAgent(send_response=AgentResponse(text="error!", is_error=True))

        for _ in range(_MAX_INJECT_ATTEMPTS):
            await _run_ensure(injector, ws1, "ses_1", agent)

        self.assertEqual(injector.status_for("ses_1").state, "failed_degraded")
        self.assertFalse(ws2.context_injected)
        self.assertEqual(injector.status_for("ses_2").state, "not_started")


# ── Tests: ensure() — already_delivered forwarding (design-review blocker) ──


class TestEnsureForwardsAlreadyDelivered(unittest.IsolatedAsyncioTestCase):
    """ensure() must be called UNCONDITIONALLY on every watcher start, and must
    forward ws.context_injected as already_delivered — never short-circuit at
    the top based on ws.context_injected. This was a real bug caught in design
    review: without it, Claude's ensure_durable_instructions() (no side effect,
    must return a fresh value every call) would stop returning a path for
    resumed sessions after a gateway restart."""

    async def test_already_delivered_forwarded_from_ws_context_injected(self):
        injector = _make_injector()
        ws = _make_ws(context_injected=True)
        captured: dict = {}

        class _CapturingAgent(AgentBackend):
            async def create_session(self, *a, **kw):
                return "ses_001"

            async def ensure_durable_instructions(self, *a, **kw):
                captured.update(kw)
                return None

            async def send(self, *a, **kw):
                raise NotImplementedError

        agent = _CapturingAgent()
        await _run_ensure(injector, ws, "ses_1", agent)

        self.assertTrue(captured.get("already_delivered"))

    async def test_ensure_called_even_when_already_delivered(self):
        """A resumed session (ws.context_injected already True) must still get
        a fresh, non-None value back from a backend with no side effect."""
        injector = _make_injector()
        ws = _make_ws(context_injected=True)
        call_count = 0

        class _CountingAgent(AgentBackend):
            async def create_session(self, *a, **kw):
                return "ses_001"

            async def ensure_durable_instructions(self, *a, **kw):
                nonlocal call_count
                call_count += 1
                return "/tmp/.acg-system-prompt/w.md"

            async def send(self, *a, **kw):
                raise NotImplementedError

        agent = _CountingAgent()
        to_repeat = await _run_ensure(injector, ws, "ses_1", agent)

        self.assertEqual(call_count, 1, "ensure() must not skip calling the backend")
        self.assertEqual(to_repeat, "/tmp/.acg-system-prompt/w.md")


# ── Tests: context injection ordering (integration, issue #9) ───────────────


class TestContextInjectionOrdering(unittest.IsolatedAsyncioTestCase):
    """Issue #9: session maps must be registered BEFORE context injection."""

    async def _run_test(self, watcher_rules, check_fn):
        from gateway.agents import AgentBackend
        from gateway.agents.response import AgentResponse
        from gateway.config import AgentConfig
        from gateway.connectors.script import ScriptConnector
        from gateway.core.config import CoreConfig
        from gateway.core.session_manager import SessionManager
        from gateway.core.session_maps import SessionMaps

        _patch_load = patch("gateway.core.state_store.load_state", return_value=[])
        _patch_save = patch("gateway.core.state_store.save_state")
        _patch_load.start()
        _patch_save.start()
        try:
            class MockAgent(AgentBackend):
                def __init__(self):
                    self._session_counter = 0
                    self.sent_messages = []

                async def create_session(self, working_directory, extra_args=None, session_title=None):
                    self._session_counter += 1
                    return f"mock-session-{self._session_counter:04d}"

                async def send(self, session_id, prompt, working_directory, timeout,
                               attachments=None, env=None, append_system_prompt_file=None):
                    self.sent_messages.append({"prompt": prompt})
                    return AgentResponse(text="ok")

            connector = ScriptConnector()
            agent = MockAgent()
            maps = SessionMaps()
            agent_cfg = AgentConfig(timeout=10, context_inject_files=[])
            config = CoreConfig(agents={"default": agent_cfg})
            manager = SessionManager(
                connector, {"default": agent}, config,
                watcher_rules=watcher_rules, session_maps=maps,
            )
            await check_fn(manager, connector, agent, maps)
            await manager.shutdown()
        finally:
            _patch_load.stop()
            _patch_save.stop()

    async def test_maps_registered_before_injection(self):
        """session maps must be populated before ensure() runs."""

        from tests.helpers import make_rule

        maps_at_injection: dict = {}
        wc = make_rule("script")

        async def check_fn(manager, connector, agent, maps):
            original_ensure = manager._lifecycle._injector.ensure

            async def capturing_ensure(*args, **kwargs):
                maps_at_injection["room_map_keys"] = list(maps.room.keys())
                maps_at_injection["connector_map_keys"] = list(maps.connector.keys())
                return await original_ensure(*args, **kwargs)

            manager._lifecycle._injector.ensure = capturing_ensure
            await manager.run_once()

        await self._run_test([wc], check_fn)

        self.assertTrue(
            len(maps_at_injection.get("room_map_keys", [])) > 0,
            "session room map was empty when ensure() ran",
        )
        self.assertTrue(
            len(maps_at_injection.get("connector_map_keys", [])) > 0,
            "session connector map was empty when ensure() ran",
        )

    async def test_injection_failure_rolls_back_maps(self):
        """If build()/ensure() fails, session maps must be cleaned up."""
        from gateway.core.session_maps import SessionMaps

        _patch_load = patch("gateway.core.state_store.load_state", return_value=[])
        _patch_save = patch("gateway.core.state_store.save_state")
        _patch_load.start()
        _patch_save.start()
        try:
            from gateway.agents import AgentBackend
            from gateway.agents.response import AgentResponse
            from gateway.config import AgentConfig
            from gateway.connectors.script import ScriptConnector
            from gateway.core.config import CoreConfig
            from gateway.core.session_manager import SessionManager

            class MockAgent(AgentBackend):
                def __init__(self):
                    self._session_counter = 0

                async def create_session(self, working_directory, extra_args=None, session_title=None):
                    self._session_counter += 1
                    return f"mock-session-{self._session_counter:04d}"

                async def send(self, session_id, prompt, working_directory, timeout,
                               attachments=None, env=None, append_system_prompt_file=None):
                    return AgentResponse(text="ok")

            connector = ScriptConnector()
            agent = MockAgent()
            maps = SessionMaps()

            from tests.helpers import make_rule
            rule = make_rule("script",
                             context_inject_files=["/nonexistent/context.md"])
            agent_cfg = AgentConfig(timeout=10, context_inject_files=[])
            config = CoreConfig(agents={"default": agent_cfg})
            manager = SessionManager(
                connector, {"default": agent}, config,
                watcher_rules=[rule], session_maps=maps,
            )

            errors = await manager.run_once()
            self.assertTrue(len(errors) > 0)
            self.assertEqual(len(maps.room), 0, "session room map not cleaned up")
            self.assertEqual(len(maps.connector), 0, "session connector map not cleaned up")

            await manager.shutdown()
        finally:
            _patch_load.stop()
            _patch_save.stop()


# ── Tests: async file IO in context injection (integration, issue #15) ──────


class TestAsyncFileIOInContextInjection(unittest.IsolatedAsyncioTestCase):
    """Issue #15: file reads in build() should use asyncio.to_thread."""

    async def test_context_injection_reads_files_via_to_thread(self):
        """Verify build() uses asyncio.to_thread (non-blocking I/O)."""
        from gateway.connectors.script import ScriptConnector
        from gateway.core.session_manager import SessionManager

        _patch_load = patch("gateway.core.state_store.load_state", return_value=[])
        _patch_save = patch("gateway.core.state_store.save_state")
        _patch_load.start()
        _patch_save.start()
        try:
            from gateway.agents import AgentBackend

            class MockAgent(AgentBackend):
                def __init__(self):
                    self._session_counter = 0

                async def create_session(self, working_directory, extra_args=None, session_title=None):
                    self._session_counter += 1
                    return f"mock-session-{self._session_counter:04d}"

                async def send(self, session_id, prompt, working_directory, timeout,
                               attachments=None, env=None, append_system_prompt_file=None):
                    return AgentResponse(text="ok")

            connector = ScriptConnector()
            agent = MockAgent()

            with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
                f.write("test context content")
                ctx_file = f.name

            WatcherConfig(
                name="script",
                connector="script",
                room="script",
                agent="default",
                context_inject_files=[ctx_file],
            )
            agent_cfg = AgentConfig(timeout=10)
            config = CoreConfig(agents={"default": agent_cfg})

            from tests.helpers import make_rule
            rule = make_rule("script", context_inject_files=[ctx_file])
            manager = SessionManager(
                connector, {"default": agent}, config, watcher_rules=[rule]
            )

            to_thread_calls = []
            original_to_thread = asyncio.to_thread

            async def tracking_to_thread(func, *args):
                to_thread_calls.append(
                    func.__name__ if hasattr(func, "__name__") else str(func)
                )
                return await original_to_thread(func, *args)

            with patch(
                "gateway.core.injected_context_builder.asyncio.to_thread",
                side_effect=tracking_to_thread,
            ):
                await manager.run_once()

            self.assertTrue(
                len(to_thread_calls) >= 2,
                f"Expected >= 2 to_thread calls, got {to_thread_calls}",
            )

            await manager.shutdown()
            Path(ctx_file).unlink(missing_ok=True)
        finally:
            _patch_load.stop()
            _patch_save.stop()


if __name__ == "__main__":
    unittest.main()
