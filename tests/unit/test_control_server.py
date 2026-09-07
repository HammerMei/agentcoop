"""Unit tests for gateway.control.ControlServer.

Tests focus on the pure-logic layer (dispatch_command, _resolve_entry,
_handle_send, _handle_client) without binding real Unix domain sockets.
The start()/stop() socket-binding paths are covered by a lightweight
integration test that uses a temp directory.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.control import ControlServer

# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_entry(name: str, dispatch_result: dict | None = None,
                send_raises: Exception | None = None,
                watcher_names: list[str] | None = None) -> MagicMock:
    """Build a minimal ConnectorEntry-like mock.

    watcher_names: if provided, get_watcher_config() returns a truthy value
    only for names in the list (simulating globally-unique watcher ownership).
    """
    entry = MagicMock()
    entry.name = name
    entry.session_manager.dispatch_command = AsyncMock(
        return_value=dispatch_result or {"ok": True, "data": []}
    )
    if send_raises:
        entry.connector.send_to_room = AsyncMock(side_effect=send_raises)
    else:
        entry.connector.send_to_room = AsyncMock(return_value=None)
    if watcher_names is not None:
        from tests.helpers import make_rule_derived_record

        def _get_watcher_state(wname: str):
            # Ownership is the persisted record (§2.8) — the config lookup
            # died with the static shape. A real record shape, because
            # fetch-history reads room fields and config_from_record off it.
            if wname in watcher_names:
                return make_rule_derived_record(name=wname)
            return None
        entry.session_manager.get_watcher_state = MagicMock(side_effect=_get_watcher_state)
    return entry


def _make_server(*entries) -> ControlServer:
    return ControlServer(list(entries))


# ── _resolve_entry ────────────────────────────────────────────────────────────

class TestResolveEntry(unittest.IsolatedAsyncioTestCase):
    def test_no_entries_returns_error(self):
        server = _make_server()
        result = server._resolve_entry(None)
        self.assertIsInstance(result, dict)
        self.assertFalse(result["ok"])
        self.assertIn("No connectors", result["error"])

    def test_single_entry_no_name_returns_entry(self):
        e = _make_entry("rc")
        server = _make_server(e)
        result = server._resolve_entry(None)
        self.assertIs(result, e)

    def test_multiple_entries_no_name_returns_ambiguity_error(self):
        server = _make_server(_make_entry("rc"), _make_entry("slack"))
        result = server._resolve_entry(None)
        self.assertIsInstance(result, dict)
        self.assertFalse(result["ok"])
        self.assertIn("Multiple connectors", result["error"])

    def test_named_entry_found(self):
        e = _make_entry("rc")
        server = _make_server(e, _make_entry("slack"))
        result = server._resolve_entry("rc")
        self.assertIs(result, e)

    def test_named_entry_not_found_returns_error(self):
        server = _make_server(_make_entry("rc"))
        result = server._resolve_entry("unknown")
        self.assertIsInstance(result, dict)
        self.assertFalse(result["ok"])
        self.assertIn("Unknown connector", result["error"])


# ── dispatch_command ──────────────────────────────────────────────────────────

class TestDispatchCommand(unittest.IsolatedAsyncioTestCase):
    async def test_list_no_connector_aggregates_all(self):
        e1 = _make_entry("rc", dispatch_result={"ok": True, "data": [{"name": "w1"}]})
        e2 = _make_entry("slack", dispatch_result={"ok": True, "data": [{"name": "w2"}]})
        server = _make_server(e1, e2)
        result = await server.dispatch_command({"cmd": "list"})
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["data"]), 2)

    async def test_list_partial_failure_returns_degraded_envelope(self):
        """A failing connector returns ok=False with partial data and an errors list."""
        e1 = _make_entry("rc", dispatch_result={"ok": True, "data": [{"name": "w1"}]})
        e2 = _make_entry("slack", dispatch_result={"ok": False, "error": "connection lost"})
        server = _make_server(e1, e2)
        result = await server.dispatch_command({"cmd": "list"})
        # ok=False signals degraded result
        self.assertFalse(result["ok"])
        # Partial data from the healthy connector is still returned
        self.assertEqual(len(result["data"]), 1)
        self.assertEqual(result["data"][0]["name"], "w1")
        # errors list names the failing connector
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["connector"], "slack")
        self.assertIn("connection lost", result["errors"][0]["error"])

    async def test_list_all_fail_returns_empty_data_with_errors(self):
        """All connectors failing returns ok=False, empty data, full errors list."""
        e1 = _make_entry("rc", dispatch_result={"ok": False, "error": "err1"})
        e2 = _make_entry("slack", dispatch_result={"ok": False, "error": "err2"})
        server = _make_server(e1, e2)
        result = await server.dispatch_command({"cmd": "list"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["data"], [])
        self.assertEqual(len(result["errors"]), 2)

    async def test_instructions_returns_bundled_doc(self):
        """instructions is handled by the control server without connector routing."""
        server = _make_server(_make_entry("rc"))

        result = await server.dispatch_command({"cmd": "instructions", "name": "scheduling"})

        self.assertTrue(result["ok"])
        self.assertEqual(result["name"], "scheduling")
        self.assertIn("# ACG Scheduling Commands", result["text"])

    async def test_instructions_unknown_name_returns_error(self):
        """Unknown instruction names return a structured error."""
        server = _make_server(_make_entry("rc"))

        result = await server.dispatch_command({"cmd": "instructions", "name": "nope"})

        self.assertFalse(result["ok"])
        self.assertIn("Unknown instruction", result["error"])

    async def test_list_all_success_has_no_errors_key(self):
        """All connectors healthy → ok=True with no 'errors' key in response."""
        e1 = _make_entry("rc", dispatch_result={"ok": True, "data": [{"name": "w1"}]})
        e2 = _make_entry("slack", dispatch_result={"ok": True, "data": [{"name": "w2"}]})
        server = _make_server(e1, e2)
        result = await server.dispatch_command({"cmd": "list"})
        self.assertTrue(result["ok"])
        self.assertNotIn("errors", result)
        self.assertEqual(len(result["data"]), 2)

    async def test_non_list_command_routed_to_single_entry(self):
        e = _make_entry("rc", dispatch_result={"ok": True, "data": "done"})
        server = _make_server(e)
        result = await server.dispatch_command({"cmd": "status"})
        self.assertTrue(result["ok"])
        e.session_manager.dispatch_command.assert_called_once()

    async def test_command_with_connector_name_routes_correctly(self):
        e_rc = _make_entry("rc", dispatch_result={"ok": True, "data": "rc-result"})
        e_slack = _make_entry("slack", dispatch_result={"ok": True, "data": "slack-result"})
        server = _make_server(e_rc, e_slack)
        result = await server.dispatch_command({"cmd": "status", "connector": "slack"})
        self.assertEqual(result["data"], "slack-result")
        e_slack.session_manager.dispatch_command.assert_called_once()
        e_rc.session_manager.dispatch_command.assert_not_called()

    async def test_unknown_connector_returns_error(self):
        server = _make_server(_make_entry("rc"))
        result = await server.dispatch_command({"cmd": "status", "connector": "nope"})
        self.assertFalse(result["ok"])


# ── _handle_send ──────────────────────────────────────────────────────────────

class TestHandleSend(unittest.IsolatedAsyncioTestCase):
    async def test_send_success(self):
        e = _make_entry("rc")
        server = _make_server(e)
        result = await server.dispatch_command({
            "cmd": "send", "room": "general", "text": "hello"
        })
        self.assertTrue(result["ok"])
        e.connector.send_to_room.assert_called_once_with(
            "general", "hello", attachment_path=None
        )

    async def test_send_with_attachment(self):
        e = _make_entry("rc")
        server = _make_server(e)
        result = await server.dispatch_command({
            "cmd": "send", "room": "general", "text": "see file",
            "attachment_path": "/tmp/report.pdf",
        })
        self.assertTrue(result["ok"])
        e.connector.send_to_room.assert_called_once_with(
            "general", "see file", attachment_path="/tmp/report.pdf"
        )

    async def test_send_missing_room_returns_error(self):
        e = _make_entry("rc")
        server = _make_server(e)
        result = await server.dispatch_command({"cmd": "send", "text": "hello"})
        self.assertFalse(result["ok"])
        self.assertIn("room", result["error"])

    async def test_send_no_text_or_attachment_returns_error(self):
        e = _make_entry("rc")
        server = _make_server(e)
        result = await server.dispatch_command({"cmd": "send", "room": "general"})
        self.assertFalse(result["ok"])
        self.assertIn("Nothing to send", result["error"])

    async def test_send_connector_raises_returns_error(self):
        e = _make_entry("rc", send_raises=RuntimeError("room not found"))
        server = _make_server(e)
        result = await server.dispatch_command({
            "cmd": "send", "room": "general", "text": "hi"
        })
        self.assertFalse(result["ok"])
        self.assertIn("room not found", result["error"])


# ── _find_entry_for_watcher ───────────────────────────────────────────────────

class TestFindEntryForWatcher(unittest.IsolatedAsyncioTestCase):
    """_find_entry_for_watcher resolves the correct entry by watcher name."""

    def test_finds_entry_that_owns_watcher(self):
        e_rc = _make_entry("rc", watcher_names=["support"])
        e_slack = _make_entry("slack", watcher_names=["sales"])
        server = _make_server(e_rc, e_slack)

        result = server._find_entry_for_watcher("support")
        self.assertIs(result, e_rc)

        result = server._find_entry_for_watcher("sales")
        self.assertIs(result, e_slack)

    def test_unknown_watcher_returns_error(self):
        e = _make_entry("rc", watcher_names=["support"])
        server = _make_server(e)

        result = server._find_entry_for_watcher("nonexistent")
        self.assertIsInstance(result, dict)
        self.assertFalse(result["ok"])
        self.assertIn("nonexistent", result["error"])
        # The batch CLI keys on this to count "gone since the match set was
        # collected" as done rather than failed (#151). The text is free to
        # change; the code is a contract.
        self.assertEqual(result["code"], "unknown_watcher")

    def test_empty_name_is_not_reported_as_unknown_watcher(self):
        """An empty name is a malformed request, not a watcher that went away —
        it must not carry the code a batch run treats as a benign skip."""
        server = _make_server(_make_entry("rc", watcher_names=[]))
        result = server._find_entry_for_watcher("")
        self.assertNotEqual(result.get("code"), "unknown_watcher")

    def test_empty_watcher_name_returns_error(self):
        server = _make_server(_make_entry("rc", watcher_names=[]))
        result = server._find_entry_for_watcher("")
        self.assertIsInstance(result, dict)
        self.assertFalse(result["ok"])


# ── reset command routing ─────────────────────────────────────────────────────

class TestResetRouting(unittest.IsolatedAsyncioTestCase):
    """reset command auto-resolves connector from watcher name (no --connector needed)."""

    async def test_reset_routes_to_correct_entry_without_connector(self):
        """reset watcher_name routes to the entry that owns the watcher."""
        e_rc = _make_entry("rc", watcher_names=["support"])
        e_slack = _make_entry("slack", watcher_names=["sales"])
        server = _make_server(e_rc, e_slack)

        result = await server.dispatch_command({"cmd": "reset", "watcher_name": "support"})
        self.assertTrue(result["ok"])
        e_rc.session_manager.dispatch_command.assert_called_once()
        e_slack.session_manager.dispatch_command.assert_not_called()

    async def test_reset_unknown_watcher_returns_error(self):
        e = _make_entry("rc", watcher_names=["support"])
        server = _make_server(e)

        result = await server.dispatch_command({"cmd": "reset", "watcher_name": "unknown"})
        self.assertFalse(result["ok"])
        self.assertIn("unknown", result["error"])

    async def test_reset_with_explicit_connector_still_works(self):
        """Passing connector= explicitly in the request still routes via _resolve_entry."""
        e_rc = _make_entry("rc")
        server = _make_server(e_rc)

        result = await server.dispatch_command({
            "cmd": "reset", "watcher_name": "support", "connector": "rc"
        })
        self.assertTrue(result["ok"])
        e_rc.session_manager.dispatch_command.assert_called_once()

    async def test_send_unknown_connector_returns_error(self):
        server = _make_server(_make_entry("rc"))
        result = await server.dispatch_command({
            "cmd": "send", "connector": "unknown", "room": "general", "text": "hi"
        })
        self.assertFalse(result["ok"])


# ── _handle_client ────────────────────────────────────────────────────────────

class TestHandleClient(unittest.IsolatedAsyncioTestCase):
    async def _call_client(self, server: ControlServer, request: dict) -> dict:
        """Run _handle_client with mock reader/writer and return parsed response."""
        encoded = json.dumps(request).encode() + b"\n"
        reader = MagicMock()
        reader.readline = AsyncMock(return_value=encoded)

        written: list[bytes] = []
        writer = MagicMock()
        writer.write = MagicMock(side_effect=written.append)
        writer.drain = AsyncMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        await server._handle_client(reader, writer)
        full = b"".join(written)
        return json.loads(full.decode().strip())

    async def test_valid_request_returns_ok(self):
        e = _make_entry("rc")
        server = _make_server(e)
        resp = await self._call_client(server, {"cmd": "list"})
        self.assertTrue(resp["ok"])

    async def test_empty_read_closes_without_response(self):
        server = _make_server()
        reader = MagicMock()
        reader.readline = AsyncMock(return_value=b"")
        writer = MagicMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        await server._handle_client(reader, writer)
        # No write should have been called
        writer.write.assert_not_called() if hasattr(writer, "write") else None

    async def test_invalid_json_returns_error_response(self):
        server = _make_server()
        reader = MagicMock()
        reader.readline = AsyncMock(return_value=b"not valid json\n")
        written: list[bytes] = []
        writer = MagicMock()
        writer.write = MagicMock(side_effect=written.append)
        writer.drain = AsyncMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        await server._handle_client(reader, writer)
        full = b"".join(written)
        resp = json.loads(full.decode().strip())
        self.assertFalse(resp["ok"])
        self.assertIn("error", resp)


# ── start / stop (socket lifecycle) ──────────────────────────────────────────

class TestStartStop(unittest.IsolatedAsyncioTestCase):
    async def test_start_creates_socket_and_stop_removes_it(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "control.sock"
            with (
                patch("gateway.control.RUNTIME_DIR", Path(tmpdir)),
                patch("gateway.control.CONTROL_SOCK", sock_path),
            ):
                server = ControlServer([])
                await server.start()
                self.assertTrue(sock_path.exists())
                await server.stop()
                self.assertFalse(sock_path.exists())

    async def test_start_sets_socket_permissions_to_0o600(self):
        """E5: Control socket must be chmod 0o600 after start() so only the
        owner can connect — asyncio's default umask may leave it world-readable."""
        import stat
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "control.sock"
            with (
                patch("gateway.control.RUNTIME_DIR", Path(tmpdir)),
                patch("gateway.control.CONTROL_SOCK", sock_path),
            ):
                server = ControlServer([])
                await server.start()
                mode = sock_path.stat().st_mode
                # Check only the permission bits (ignore file type bits)
                perms = stat.S_IMODE(mode)
                self.assertEqual(
                    perms, 0o600,
                    f"Expected socket permissions 0o600, got 0o{perms:03o}"
                )
                await server.stop()

    async def test_start_stale_socket_is_replaced(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "control.sock"
            # Create a stale (non-listening) socket file
            sock_path.touch()
            with (
                patch("gateway.control.RUNTIME_DIR", Path(tmpdir)),
                patch("gateway.control.CONTROL_SOCK", sock_path),
            ):
                server = ControlServer([])
                # Should not raise — stale socket is removed and replaced
                await server.start()
                self.assertTrue(sock_path.exists())
                await server.stop()

    async def test_start_live_socket_raises(self):
        """If the socket is live (another gateway running), start() must raise."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "control.sock"
            with (
                patch("gateway.control.RUNTIME_DIR", Path(tmpdir)),
                patch("gateway.control.CONTROL_SOCK", sock_path),
            ):
                # Start a real listening server on the socket to simulate a live instance
                real_server = await asyncio.start_unix_server(
                    lambda r, w: w.close(), path=str(sock_path)
                )
                try:
                    second = ControlServer([])
                    with self.assertRaises(RuntimeError, msg="already running"):
                        await second.start()
                finally:
                    real_server.close()
                    await real_server.wait_closed()

    async def test_start_timeout_with_live_pid_raises(self):
        """Socket timeout + live PID → must raise (not silently unlink the socket)."""
        import os

        async def _timeout_connect(*args, **kwargs):
            raise asyncio.TimeoutError()

        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "control.sock"
            sock_path.touch()  # pre-existing socket file
            current_pid = os.getpid()
            with (
                patch("gateway.control.RUNTIME_DIR", Path(tmpdir)),
                patch("gateway.control.CONTROL_SOCK", sock_path),
                patch("gateway.control.runtime_lock.locked_pid", return_value=current_pid),
                # Simulate open_unix_connection raising TimeoutError mid-await
                patch("asyncio.open_unix_connection", new=_timeout_connect),
            ):
                server = ControlServer([])
                with self.assertRaises(RuntimeError) as ctx:
                    await server.start()
                self.assertIn("pid=", str(ctx.exception))
                # Socket must NOT have been unlinked when a live owner is found
                self.assertTrue(sock_path.exists())

    async def test_start_timeout_no_pid_unlinks_stale_socket(self):
        """Socket timeout + no live PID → treat as stale, unlink and proceed."""
        async def _timeout_connect(*args, **kwargs):
            raise asyncio.TimeoutError()

        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "control.sock"
            sock_path.touch()  # stale socket file
            with (
                patch("gateway.control.RUNTIME_DIR", Path(tmpdir)),
                patch("gateway.control.CONTROL_SOCK", sock_path),
                patch("gateway.control.runtime_lock.locked_pid", return_value=None),
                # Simulate open_unix_connection raising TimeoutError mid-await
                patch("asyncio.open_unix_connection", new=_timeout_connect),
            ):
                server = ControlServer([])
                await server.start()  # must not raise
                await server.stop()


# ── _handle_fetch_history ─────────────────────────────────────────────────────


def _make_history_entry(
    watcher_name: str,
    supports_history: bool = True,
    history_messages: list | None = None,
    resolve_room_raises: Exception | None = None,
    max_fetch_count: int = 200,
) -> MagicMock:
    """Build a ConnectorEntry mock suited for fetch-history tests."""
    from unittest.mock import AsyncMock, MagicMock

    from tests.helpers import make_rule_derived_record

    record = make_rule_derived_record(
        name=watcher_name, room_id="ROOM_ID", room_name="test-room",
        config={
            "name": watcher_name, "connector": "rc", "room": "#test-room",
            "agent": "default",
            "history_handoff": {"max_fetch_count": max_fetch_count},
        },
    )

    entry = MagicMock()
    entry.name = "rc"
    entry.connector.supports_history.return_value = supports_history

    entry.connector.fetch_room_history = AsyncMock(return_value=history_messages or [])
    if resolve_room_raises is not None:
        # The room is built from the record now, never resolved by name; a
        # caller that used to simulate a resolve failure simulates the fetch
        # failing instead — the same operator-visible outcome.
        entry.connector.fetch_room_history = AsyncMock(side_effect=resolve_room_raises)

    def _get_watcher_config(name):
        return record if name == watcher_name else None

    entry.session_manager.get_watcher_state = MagicMock(side_effect=_get_watcher_config)
    return entry


class TestHandleFetchHistory(unittest.IsolatedAsyncioTestCase):
    """Tests for ControlServer._handle_fetch_history."""

    async def test_unknown_watcher_returns_error(self):
        """A watcher name that belongs to no entry must return an error."""
        entry = _make_history_entry("known-watcher")
        server = _make_server(entry)
        result = await server.dispatch_command({
            "cmd": "fetch-history", "watcher": "unknown-watcher"
        })
        self.assertFalse(result["ok"])
        self.assertIn("unknown-watcher", result["error"])

    async def test_connector_no_history_support_returns_error(self):
        """fetch-history against a connector that doesn't support it must return an error."""
        entry = _make_history_entry("my-watcher", supports_history=False)
        server = _make_server(entry)
        result = await server.dispatch_command({
            "cmd": "fetch-history", "watcher": "my-watcher"
        })
        self.assertFalse(result["ok"])
        self.assertIn("does not support history", result["error"])

    async def test_count_clamped_adds_warning(self):
        """When --count exceeds max_fetch_count, response includes a clamping warning."""
        entry = _make_history_entry("my-watcher", max_fetch_count=10)
        server = _make_server(entry)
        result = await server.dispatch_command({
            "cmd": "fetch-history", "watcher": "my-watcher", "count": 999
        })
        self.assertTrue(result["ok"])
        # Use the exact phrase so we don't false-positive on timestamps like "10:00"
        self.assertIn("clamped from 999 to 10", result["history"])

    async def test_empty_channel_returns_ok_with_empty_history(self):
        """A channel with no messages returns ok=True and an empty history string."""
        entry = _make_history_entry("my-watcher", history_messages=[])
        server = _make_server(entry)
        result = await server.dispatch_command({
            "cmd": "fetch-history", "watcher": "my-watcher"
        })
        self.assertTrue(result["ok"])
        self.assertIsInstance(result["history"], str)

    async def test_resolve_room_failure_returns_error(self):
        """An exception from resolve_room must propagate as an error response."""
        entry = _make_history_entry(
            "my-watcher",
            resolve_room_raises=RuntimeError("room not found"),
        )
        server = _make_server(entry)
        result = await server.dispatch_command({
            "cmd": "fetch-history", "watcher": "my-watcher"
        })
        self.assertFalse(result["ok"])
        self.assertIn("room not found", result["error"])

    async def test_missing_watcher_field_returns_error(self):
        """Omitting the 'watcher' field must return a descriptive error."""
        server = _make_server(_make_history_entry("any"))
        result = await server.dispatch_command({"cmd": "fetch-history"})
        self.assertFalse(result["ok"])
        self.assertIn("watcher", result["error"].lower())

    async def test_invalid_count_returns_error(self):
        """A non-integer count must return a validation error."""
        entry = _make_history_entry("my-watcher")
        server = _make_server(entry)
        result = await server.dispatch_command({
            "cmd": "fetch-history", "watcher": "my-watcher", "count": "abc"
        })
        self.assertFalse(result["ok"])
        self.assertIn("count", result["error"])

    async def test_zero_count_returns_error(self):
        """count=0 must be rejected with a validation error."""
        entry = _make_history_entry("my-watcher")
        server = _make_server(entry)
        result = await server.dispatch_command({
            "cmd": "fetch-history", "watcher": "my-watcher", "count": 0
        })
        self.assertFalse(result["ok"])
        self.assertIn("count", result["error"])

    async def test_invalid_before_ts_returns_error(self):
        """A malformed before_ts must return an ISO 8601 validation error."""
        entry = _make_history_entry("my-watcher")
        server = _make_server(entry)
        result = await server.dispatch_command({
            "cmd": "fetch-history", "watcher": "my-watcher", "before_ts": "not-a-date"
        })
        self.assertFalse(result["ok"])
        self.assertIn("before_ts", result["error"])

    async def test_invalid_after_ts_returns_error(self):
        """A malformed after_ts must return an ISO 8601 validation error."""
        entry = _make_history_entry("my-watcher")
        server = _make_server(entry)
        result = await server.dispatch_command({
            "cmd": "fetch-history", "watcher": "my-watcher", "after_ts": "not-a-date"
        })
        self.assertFalse(result["ok"])
        self.assertIn("after_ts", result["error"])

    async def test_after_ts_is_converted_at_this_boundary(self):
        """The operator types ISO; the connector is handed epoch-ms (§5.2).

        This is the one place a human writes a timestamp, so it is the one
        place that converts — connector bounds are epoch-ms like every other
        timestamp inside ACG, and leaving the ISO to travel further is how a
        value that looked right met an interface that wanted the other form.
        """
        entry = _make_history_entry("my-watcher")
        server = _make_server(entry)
        await server.dispatch_command({
            "cmd": "fetch-history", "watcher": "my-watcher",
            "after_ts": "2026-05-10T19:25:00+08:00",
        })
        entry.connector.fetch_room_history.assert_called_once()
        _, kwargs = entry.connector.fetch_room_history.call_args
        self.assertEqual(kwargs.get("after_ts"), "1778412300000")

    async def test_combined_after_and_before_passed_to_connector(self):
        """Both after_ts and before_ts must be forwarded when a time window is specified."""
        entry = _make_history_entry("my-watcher")
        server = _make_server(entry)
        after = "2026-05-10T08:00:00+08:00"
        before = "2026-05-10T20:00:00+08:00"
        result = await server.dispatch_command({
            "cmd": "fetch-history",
            "watcher": "my-watcher",
            "after_ts": after,
            "before_ts": before,
        })
        self.assertTrue(result["ok"])
        entry.connector.fetch_room_history.assert_called_once()
        _, kwargs = entry.connector.fetch_room_history.call_args
        # Both converted at this boundary; the window is preserved.
        self.assertEqual(kwargs.get("after_ts"), "1778371200000")
        self.assertEqual(kwargs.get("before_ts"), "1778414400000")

    async def test_inverted_range_returns_error(self):
        """after_ts >= before_ts must return a clear error, not silently return empty results."""
        entry = _make_history_entry("my-watcher")
        server = _make_server(entry)
        result = await server.dispatch_command({
            "cmd": "fetch-history",
            "watcher": "my-watcher",
            "after_ts": "2026-05-10T20:00:00+08:00",   # later
            "before_ts": "2026-05-10T08:00:00+08:00",  # earlier
        })
        self.assertFalse(result["ok"])
        self.assertIn("after_ts", result["error"])
        self.assertIn("before_ts", result["error"])

    async def test_on_demand_header_in_output(self):
        """History text from fetch-history must use the on-demand header, not the startup header."""
        entry = _make_history_entry(
            "my-watcher",
            history_messages=[{
                "username": "alice", "role": "owner",
                "text": "hello", "ts": "2026-05-10T10:00:00+08:00", "room_name": "test-room",
            }],
        )
        server = _make_server(entry)
        result = await server.dispatch_command({
            "cmd": "fetch-history", "watcher": "my-watcher"
        })
        self.assertTrue(result["ok"])
        # on_demand=True must be wired: header starts with on-demand marker, not startup marker
        self.assertTrue(
            result["history"].startswith("[HISTORY FETCH — on-demand"),
            f"Expected on-demand header, got: {result['history'][:80]!r}",
        )
        self.assertNotIn("SESSION HISTORY", result["history"])


if __name__ == "__main__":
    unittest.main()


# ── Appended from test_round7_fixes.py ────────────────────────────────────────


class TestControlWriterTimeout(unittest.IsolatedAsyncioTestCase):
    """writer.wait_closed() must not hang indefinitely."""

    async def test_wait_closed_timeout_does_not_propagate(self):
        """A hung writer.wait_closed() must be caught and not leak the handler."""
        server = ControlServer.__new__(ControlServer)
        server._entries = []
        server._server = None

        reader = MagicMock()
        reader.readline = AsyncMock(return_value=b'{"cmd": "list"}\n')

        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock()
        writer.close = MagicMock()

        async def hung_wait_closed():
            await asyncio.sleep(9999)

        writer.wait_closed = hung_wait_closed

        with patch.object(server, "dispatch_command", new_callable=AsyncMock, return_value={"ok": True}):
            try:
                await asyncio.wait_for(server._handle_client(reader, writer), timeout=10.0)
            except asyncio.TimeoutError:
                self.fail("_handle_client hung: writer.wait_closed() timeout not applied")

        writer.close.assert_called_once()


# ── Appended from test_round13_fixes.py ───────────────────────────────────────


class TestControlDrainTimeout(unittest.IsolatedAsyncioTestCase):
    """writer.drain() must not block indefinitely when the client stops reading."""

    def _make_server(self):
        server = ControlServer.__new__(ControlServer)
        server._entries = []
        server._server = None
        return server

    async def test_drain_timeout_does_not_hang_handler(self):
        """writer.drain() must be called via asyncio.wait_for so it has a timeout."""
        server = self._make_server()

        reader = MagicMock()
        reader.readline = AsyncMock(return_value=b'{"cmd": "list"}\n')

        writer = MagicMock()
        writer.write = MagicMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()
        writer.drain = AsyncMock(side_effect=asyncio.TimeoutError("drain timed out"))

        with patch.object(
            server, "dispatch_command",
            new_callable=AsyncMock,
            return_value={"ok": True, "data": []},
        ):
            await server._handle_client(reader, writer)

        writer.close.assert_called_once()

    async def test_drain_timeout_in_error_path_does_not_hang(self):
        """Error-path drain() also times out correctly."""
        server = self._make_server()

        reader = MagicMock()
        reader.readline = AsyncMock(return_value=b'{"cmd": "list"}\n')

        writer = MagicMock()
        writer.write = MagicMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()
        writer.drain = AsyncMock(side_effect=asyncio.TimeoutError("drain timed out"))

        with patch.object(
            server, "dispatch_command",
            new_callable=AsyncMock,
            side_effect=RuntimeError("dispatch exploded"),
        ):
            await server._handle_client(reader, writer)

        writer.close.assert_called_once()

    async def test_drain_completes_normally_when_client_is_healthy(self):
        """Normal path: handler must still complete successfully."""
        server = self._make_server()

        reader = MagicMock()
        reader.readline = AsyncMock(return_value=b'{"cmd": "list"}\n')

        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        with patch.object(
            server, "dispatch_command",
            new_callable=AsyncMock,
            return_value={"ok": True, "data": [{"watcher_name": "support"}]},
        ):
            await server._handle_client(reader, writer)

        writer.drain.assert_awaited()
        writer.close.assert_called_once()


# ── Appended from test_round16_fixes.py ───────────────────────────────────────


class TestAggregateListHandlesExceptions(unittest.IsolatedAsyncioTestCase):
    """dispatch_command('list') must not abort on a single connector's exception."""

    def _make_server(self):
        server = ControlServer.__new__(ControlServer)
        server._entries = []
        server._server = None
        return server

    def _make_entry(self, name: str, watcher_data: list = None, raises=None):
        entry = MagicMock()
        entry.name = name
        if raises:
            entry.session_manager.dispatch_command = AsyncMock(side_effect=raises)
        else:
            entry.session_manager.dispatch_command = AsyncMock(
                return_value={"ok": True, "data": watcher_data or []}
            )
        entry.connector = MagicMock()
        return entry

    async def test_exception_in_one_connector_returns_partial_results(self):
        """An exception from one connector must not suppress another connector's watchers."""
        server = self._make_server()
        good_entry = self._make_entry("good", watcher_data=[{"watcher_name": "w1"}])
        bad_entry = self._make_entry("bad", raises=RuntimeError("internal error"))
        server._entries = [good_entry, bad_entry]

        result = await server.dispatch_command({"cmd": "list"})

        self.assertIn(
            {"watcher_name": "w1"},
            result.get("data", []),
        )
        errors = result.get("errors", [])
        self.assertTrue(
            any(e.get("connector") == "bad" for e in errors),
            f"Bad connector's error not captured: {errors}",
        )
        self.assertFalse(result.get("ok"))

    async def test_exception_response_includes_connector_attribution(self):
        """The error entry must include the connector name."""
        server = self._make_server()
        server._entries = [
            self._make_entry("failing-rc", raises=RuntimeError("session manager exploded")),
        ]

        result = await server.dispatch_command({"cmd": "list"})

        errors = result.get("errors", [])
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].get("connector"), "failing-rc")
        self.assertIn("session manager exploded", errors[0].get("error", ""))

    async def test_two_connectors_one_fails_one_succeeds(self):
        """Partial failure returns combined data."""
        server = self._make_server()
        server._entries = [
            self._make_entry("rc", watcher_data=[{"watcher_name": "support"}, {"watcher_name": "sales"}]),
            self._make_entry("slack", raises=KeyError("missing key")),
        ]

        result = await server.dispatch_command({"cmd": "list"})

        data = result.get("data", [])
        self.assertEqual(len(data), 2)
        errors = result.get("errors", [])
        self.assertTrue(any(e.get("connector") == "slack" for e in errors))

    async def test_both_connectors_succeed_no_errors(self):
        """When all connectors succeed, no errors key appears and ok=True."""
        server = self._make_server()
        server._entries = [
            self._make_entry("rc", watcher_data=[{"watcher_name": "w1"}]),
            self._make_entry("slack", watcher_data=[{"watcher_name": "w2"}]),
        ]

        result = await server.dispatch_command({"cmd": "list"})

        self.assertTrue(result.get("ok"))
        self.assertNotIn("errors", result)
        self.assertEqual(len(result.get("data", [])), 2)
