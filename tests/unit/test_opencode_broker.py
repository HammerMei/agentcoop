"""Unit tests for OpenCodePermissionBroker: SSE parsing and guest enforcement."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.agents.opencode.broker import OpenCodePermissionBroker
from gateway.config import ToolRule
from gateway.core.permission import PermissionRegistry


def _make_broker(
    *,
    base_url: str = "http://127.0.0.1:57000",
    session_room_map: dict | None = None,
    session_role_map: dict | None = None,
    session_connector_map: dict | None = None,
    owner_allowed_tools: list[str] | None = None,
    guest_allowed_tools: list[str] | None = None,
    skip_owner_approval: bool = False,
    human_approval: bool = True,
) -> OpenCodePermissionBroker:
    from gateway.core.permission import ConnectorPermissionNotifier

    registry = PermissionRegistry()
    connector = MagicMock()
    # Default: map every session in session_room_map to the mock connector
    if session_connector_map is None:
        session_connector_map = {sid: connector for sid in (session_room_map or {})}
    notifier = ConnectorPermissionNotifier(session_connector_map)
    return OpenCodePermissionBroker(
        registry=registry,
        notifier=notifier,
        opencode_base_url=base_url,
        session_room_map=session_room_map or {},
        session_role_map=session_role_map or {},
        owner_allowed_tools=[ToolRule(tool=t) for t in (owner_allowed_tools or [])],
        guest_allowed_tools=[ToolRule(tool=t) for t in (guest_allowed_tools or [])],
        skip_owner_approval=skip_owner_approval,
        human_approval=human_approval,
    )


def _sse_line(payload: dict) -> str:
    return f"data: {json.dumps(payload)}"


# ── SSE parsing ───────────────────────────────────────────────────────────────


class TestSSEParsing(unittest.IsolatedAsyncioTestCase):
    """_handle_sse_line correctly parses permission.asked events."""

    async def test_ignores_non_data_lines(self):
        broker = _make_broker()
        broker._reply_to_opencode = AsyncMock()
        for line in ("", ": keep-alive", "event: ping", "id: 42"):
            await broker._handle_sse_line(line)
        broker._reply_to_opencode.assert_not_called()

    async def test_ignores_non_permission_event_type(self):
        broker = _make_broker()
        broker._reply_to_opencode = AsyncMock()
        payload = {"type": "session.updated", "properties": {"id": "per_abc"}}
        await broker._handle_sse_line(_sse_line(payload))
        broker._reply_to_opencode.assert_not_called()

    async def test_ignores_malformed_json(self):
        broker = _make_broker()
        broker._reply_to_opencode = AsyncMock()
        await broker._handle_sse_line("data: {not valid json")
        broker._reply_to_opencode.assert_not_called()

    async def test_auto_denies_unknown_session(self):
        """No room_id for session → auto-deny (fail closed) without asking owner."""
        broker = _make_broker(session_room_map={})
        broker._reply_to_opencode = AsyncMock()

        payload = {
            "type": "permission.asked",
            "properties": {
                "id": "per_xyz",
                "permission": "bash",
                "sessionID": "ses_unknown",
                "patterns": ["ls"],
                "metadata": {},
            },
        }
        await broker._handle_sse_line(_sse_line(payload))
        # No-room-id path dispatches a background task — drain it before asserting.
        await asyncio.gather(*list(broker._pending_tasks), return_exceptions=True)
        broker._reply_to_opencode.assert_called_once_with("per_xyz", approved=False)

    async def test_extracts_fields_from_properties_subdict(self):
        """All permission fields must be read from properties, not top level."""
        broker = _make_broker(
            session_room_map={"ses_abc": "room_1"},
            session_role_map={"ses_abc": "owner"},
        )
        broker._handle_permission_request = AsyncMock()

        payload = {
            "type": "permission.asked",
            "properties": {
                "id": "per_abc123",
                "permission": "write",
                "sessionID": "ses_abc",
                "patterns": ["/tmp/out.txt"],
                "metadata": {},
            },
        }
        await broker._handle_sse_line(_sse_line(payload))
        # Give create_task a chance to run
        await asyncio.sleep(0)
        broker._handle_permission_request.assert_called_once()
        call_kwargs = broker._handle_permission_request.call_args.kwargs
        self.assertEqual(call_kwargs["opencode_req_id"], "per_abc123")
        self.assertEqual(call_kwargs["tool_name"], "write")
        self.assertEqual(call_kwargs["session_id"], "ses_abc")
        self.assertEqual(call_kwargs["room_id"], "room_1")
        # patterns fall back to tool_input["commands"] when metadata is empty
        self.assertEqual(call_kwargs["tool_input"], {"commands": ["/tmp/out.txt"]})

    async def test_tool_input_uses_metadata_when_populated(self):
        """metadata takes priority over patterns when non-empty."""
        broker = _make_broker(
            session_room_map={"ses_abc": "room_1"},
            session_role_map={"ses_abc": "owner"},
        )
        broker._handle_permission_request = AsyncMock()

        payload = {
            "type": "permission.asked",
            "properties": {
                "id": "per_meta",
                "permission": "bash",
                "sessionID": "ses_abc",
                "patterns": ["ls"],
                "metadata": {"filePath": "/etc/passwd"},
            },
        }
        await broker._handle_sse_line(_sse_line(payload))
        await asyncio.sleep(0)
        call_kwargs = broker._handle_permission_request.call_args.kwargs
        # metadata wins over patterns
        self.assertEqual(call_kwargs["tool_input"], {"filePath": "/etc/passwd"})


# ── Guest enforcement ─────────────────────────────────────────────────────────


class TestGuestEnforcement(unittest.IsolatedAsyncioTestCase):
    """Guest sessions: auto-deny unlisted tools, auto-approve listed tools."""

    async def test_guest_auto_denies_unlisted_tool(self):
        broker = _make_broker(
            session_room_map={"ses_g": "room_g"},
            session_role_map={"ses_g": "guest"},
            guest_allowed_tools=["Read", "Grep"],
        )
        broker._reply_to_opencode = AsyncMock()

        payload = {
            "type": "permission.asked",
            "properties": {
                "id": "per_deny",
                "permission": "bash",
                "sessionID": "ses_g",
                "patterns": ["rm -rf /"],
                "metadata": {},
            },
        }
        await broker._handle_sse_line(_sse_line(payload))
        # Guest auto-deny is dispatched as a background task — drain before asserting.
        await asyncio.gather(*list(broker._pending_tasks), return_exceptions=True)
        broker._reply_to_opencode.assert_called_once_with("per_deny", approved=False)

    async def test_guest_bash_denied_when_only_the_gateway_split_sees_the_substitution(self):
        """#175: opencode's own grammar emits one pattern for `$(…)` on an indented heredoc line.

        Matching the sidecar's patterns alone approves it under the built-in
        `coop legacy fetch-history` guest rule; the broker also splits
        ``metadata.command`` itself, which returns the substitution from its
        opener, and that string matches nothing.
        """
        from gateway.core.config import AgentConfig

        broker = _make_broker(
            session_room_map={"ses_g": "room_g"},
            session_role_map={"ses_g": "guest"},
        )
        broker._guest_allowed_tools = AgentConfig().effective_guest_allowed_tools()
        broker._reply_to_opencode = AsyncMock()

        cmd = "coop legacy fetch-history --room r <<EOF\n\t$(rm -rf /tmp/x)\nEOF"
        payload = {
            "type": "permission.asked",
            "properties": {
                "id": "per_gap",
                "permission": "bash",
                "sessionID": "ses_g",
                "patterns": [cmd],  # what opencode 1.18.13 emits for this form
                "metadata": {"command": cmd},
            },
        }
        await broker._handle_sse_line(_sse_line(payload))
        await asyncio.gather(*list(broker._pending_tasks), return_exceptions=True)
        broker._reply_to_opencode.assert_called_once_with("per_gap", approved=False)

    async def test_guest_bash_plain_fetch_history_still_approved_with_command(self):
        from gateway.core.config import AgentConfig

        broker = _make_broker(
            session_room_map={"ses_g": "room_g"},
            session_role_map={"ses_g": "guest"},
        )
        broker._guest_allowed_tools = AgentConfig().effective_guest_allowed_tools()
        broker._reply_to_opencode = AsyncMock()

        cmd = "coop legacy fetch-history --room r --limit 5"
        payload = {
            "type": "permission.asked",
            "properties": {
                "id": "per_plain",
                "permission": "bash",
                "sessionID": "ses_g",
                "patterns": [cmd],
                "metadata": {"command": cmd},
            },
        }
        await broker._handle_sse_line(_sse_line(payload))
        await asyncio.gather(*list(broker._pending_tasks), return_exceptions=True)
        broker._reply_to_opencode.assert_called_once_with("per_plain", approved=True)

    async def test_bash_ask_without_metadata_command_warns_and_matches_patterns_only(self):
        """No metadata.command → status quo (sidecar patterns) plus a warning naming the request."""
        broker = _make_broker(
            session_room_map={"ses_g": "room_g"},
            session_role_map={"ses_g": "guest"},
            guest_allowed_tools=["bash"],
        )
        broker._reply_to_opencode = AsyncMock()

        payload = {
            "type": "permission.asked",
            "properties": {
                "id": "per_nometa",
                "permission": "bash",
                "sessionID": "ses_g",
                "patterns": ["ls"],
                "metadata": {},
            },
        }
        with self.assertLogs("coop.permissions.opencode", level="WARNING") as logs:
            await broker._handle_sse_line(_sse_line(payload))
        await asyncio.gather(*list(broker._pending_tasks), return_exceptions=True)
        self.assertTrue(any("metadata.command" in line and "per_nometa" in line for line in logs.output), logs.output)
        broker._reply_to_opencode.assert_called_once_with("per_nometa", approved=True)

    async def test_guest_auto_approves_listed_tool(self):
        broker = _make_broker(
            session_room_map={"ses_g": "room_g"},
            session_role_map={"ses_g": "guest"},
            guest_allowed_tools=["Read", "Grep", "Glob"],
        )
        broker._reply_to_opencode = AsyncMock()

        payload = {
            "type": "permission.asked",
            "properties": {
                "id": "per_ok",
                "permission": "Read",
                "sessionID": "ses_g",
                "patterns": ["/src/main.py"],
                "metadata": {},
            },
        }
        await broker._handle_sse_line(_sse_line(payload))
        # Guest auto-approve is dispatched as a background task — drain before asserting.
        await asyncio.gather(*list(broker._pending_tasks), return_exceptions=True)
        broker._reply_to_opencode.assert_called_once_with("per_ok", approved=True)

    async def test_guest_fnmatch_wildcard(self):
        """Wildcard patterns in guest_allowed_tools use regex syntax (re.fullmatch)."""
        broker = _make_broker(
            session_room_map={"ses_g": "room_g"},
            session_role_map={"ses_g": "guest"},
            guest_allowed_tools=["mcp__rocketchat__.*"],
        )
        broker._reply_to_opencode = AsyncMock()

        payload = {
            "type": "permission.asked",
            "properties": {
                "id": "per_mcp",
                "permission": "mcp__rocketchat__send_message",
                "sessionID": "ses_g",
                "patterns": [],
                "metadata": {},
            },
        }
        await broker._handle_sse_line(_sse_line(payload))
        # Guest auto-approve is dispatched as a background task — drain before asserting.
        await asyncio.gather(*list(broker._pending_tasks), return_exceptions=True)
        broker._reply_to_opencode.assert_called_once_with("per_mcp", approved=True)

    async def test_owner_session_delegates_to_handle_permission_request(self):
        """Owner sessions go through the full request_permission flow, not auto-approve/deny."""
        broker = _make_broker(
            session_room_map={"ses_owner": "room_1"},
            session_role_map={"ses_owner": "owner"},
            guest_allowed_tools=["Read"],
        )
        broker._reply_to_opencode = AsyncMock()
        broker._handle_permission_request = AsyncMock()

        payload = {
            "type": "permission.asked",
            "properties": {
                "id": "per_owner",
                "permission": "bash",
                "sessionID": "ses_owner",
                "patterns": ["make"],
                "metadata": {},
            },
        }
        await broker._handle_sse_line(_sse_line(payload))
        await asyncio.sleep(0)
        broker._reply_to_opencode.assert_not_called()
        broker._handle_permission_request.assert_called_once()

    async def test_missing_role_defaults_to_guest_fail_closed(self):
        """Sessions not in session_role_map default to 'guest' (fail-closed).

        A missing role mapping must NOT silently grant owner-level permissions.
        The broker must auto-deny the tool call without posting an RC notification,
        exactly as it would for a known guest session with a disallowed tool.
        """
        broker = _make_broker(
            session_room_map={"ses_new": "room_1"},
            session_role_map={},
            guest_allowed_tools=["Read"],  # bash is NOT in guest_allowed_tools
        )
        broker._reply_to_opencode = AsyncMock()
        broker._handle_permission_request = AsyncMock()

        payload = {
            "type": "permission.asked",
            "properties": {
                "id": "per_new",
                "permission": "bash",
                "sessionID": "ses_new",
                "patterns": [],
                "metadata": {},
            },
        }
        await broker._handle_sse_line(_sse_line(payload))
        # Guest auto-deny is dispatched as a background task — drain before asserting.
        await asyncio.gather(*list(broker._pending_tasks), return_exceptions=True)
        # Must be auto-denied (guest path) — NOT delegated to handle_permission_request (owner path)
        broker._reply_to_opencode.assert_called_once_with("per_new", approved=False)
        broker._handle_permission_request.assert_not_called()


# ── skip_owner_approval tests ─────────────────────────────────────────────────


class TestSkipOwnerApproval(unittest.IsolatedAsyncioTestCase):
    """skip_owner_approval=True bypasses all owner checks and auto-approves every tool."""

    async def test_owner_any_tool_auto_approved_when_flag_set(self):
        """With skip_owner_approval, owner tool calls are approved without RC notification."""
        broker = _make_broker(
            session_room_map={"ses_o": "room_o"},
            session_role_map={"ses_o": "owner"},
            owner_allowed_tools=[],          # empty allow-list — would normally trigger ask
            skip_owner_approval=True,
        )
        broker._reply_to_opencode = AsyncMock()
        broker._handle_permission_request = AsyncMock()

        payload = {
            "type": "permission.asked",
            "properties": {
                "id": "per_skip",
                "permission": "bash",
                "sessionID": "ses_o",
                "patterns": ["rm -rf /"],
                "metadata": {},
            },
        }
        await broker._handle_sse_line(_sse_line(payload))
        await asyncio.gather(*list(broker._pending_tasks), return_exceptions=True)

        broker._reply_to_opencode.assert_called_once_with("per_skip", approved=True)
        broker._handle_permission_request.assert_not_called()

    async def test_guest_still_blocked_when_skip_owner_approval_set(self):
        """skip_owner_approval only affects owners — guests remain subject to their allow-list."""
        broker = _make_broker(
            session_room_map={"ses_g": "room_g"},
            session_role_map={"ses_g": "guest"},
            guest_allowed_tools=["Read"],
            skip_owner_approval=True,
        )
        broker._reply_to_opencode = AsyncMock()

        payload = {
            "type": "permission.asked",
            "properties": {
                "id": "per_guest_block",
                "permission": "bash",
                "sessionID": "ses_g",
                "patterns": ["ls"],
                "metadata": {},
            },
        }
        await broker._handle_sse_line(_sse_line(payload))
        await asyncio.gather(*list(broker._pending_tasks), return_exceptions=True)

        broker._reply_to_opencode.assert_called_once_with("per_guest_block", approved=False)

    async def test_guest_allowed_tool_unaffected_when_skip_owner_approval_set(self):
        """guest_allowed_tools still works normally when skip_owner_approval is enabled."""
        broker = _make_broker(
            session_room_map={"ses_g": "room_g"},
            session_role_map={"ses_g": "guest"},
            guest_allowed_tools=["Read", "Grep"],
            skip_owner_approval=True,
        )
        broker._reply_to_opencode = AsyncMock()

        payload = {
            "type": "permission.asked",
            "properties": {
                "id": "per_guest_ok",
                "permission": "Read",
                "sessionID": "ses_g",
                "patterns": ["/src/main.py"],
                "metadata": {},
            },
        }
        await broker._handle_sse_line(_sse_line(payload))
        await asyncio.gather(*list(broker._pending_tasks), return_exceptions=True)

        broker._reply_to_opencode.assert_called_once_with("per_guest_ok", approved=True)

    async def test_owner_no_room_mapping_still_allowed_when_flag_set(self):
        """skip_owner_approval owner with no room mapping is auto-approved (no RC needed).

        This mirrors ClaudePermissionBroker behaviour: the room-mapping guard is
        bypassed for owners when skip_owner_approval=True because no RC notification
        is ever sent in this mode.
        """
        broker = _make_broker(
            session_room_map={},             # no room mapping — would normally block
            session_role_map={"ses_o": "owner"},
            skip_owner_approval=True,
        )
        broker._reply_to_opencode = AsyncMock()

        payload = {
            "type": "permission.asked",
            "properties": {
                "id": "per_noroom",
                "permission": "bash",
                "sessionID": "ses_o",
                "patterns": ["ls"],
                "metadata": {},
            },
        }
        await broker._handle_sse_line(_sse_line(payload))
        await asyncio.gather(*list(broker._pending_tasks), return_exceptions=True)

        broker._reply_to_opencode.assert_called_once_with("per_noroom", approved=True)

    async def test_skip_owner_approval_false_preserves_normal_ask_flow(self):
        """When skip_owner_approval is False (default), unlisted owner tools go through ask flow."""
        broker = _make_broker(
            session_room_map={"ses_o": "room_o"},
            session_role_map={"ses_o": "owner"},
            owner_allowed_tools=[],
            skip_owner_approval=False,
        )
        broker._reply_to_opencode = AsyncMock()
        broker._handle_permission_request = AsyncMock()

        payload = {
            "type": "permission.asked",
            "properties": {
                "id": "per_ask",
                "permission": "bash",
                "sessionID": "ses_o",
                "patterns": ["make build"],
                "metadata": {},
            },
        }
        await broker._handle_sse_line(_sse_line(payload))
        await asyncio.sleep(0)

        # Should have gone through handle_permission_request (not auto-approved)
        broker._handle_permission_request.assert_called_once()
        broker._reply_to_opencode.assert_not_called()


# ── Reply API ─────────────────────────────────────────────────────────────────


class TestReplyToOpencode(unittest.IsolatedAsyncioTestCase):
    """_reply_to_opencode posts correct payload to the reply endpoint."""

    async def test_approve_sends_once(self):
        broker = _make_broker(base_url="http://127.0.0.1:57001")
        # Return a plain MagicMock so raise_for_status() is synchronous
        # (matches real httpx behavior, avoids "coroutine never awaited" warning).
        mock_response = MagicMock()
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        broker._reply_client = mock_client
        await broker._reply_to_opencode("per_abc", approved=True)
        mock_client.post.assert_called_once_with(
            "http://127.0.0.1:57001/permission/per_abc/reply",
            json={"reply": "once"},
        )

    async def test_deny_sends_reject(self):
        broker = _make_broker(base_url="http://127.0.0.1:57001")
        mock_response = MagicMock()
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        broker._reply_client = mock_client
        await broker._reply_to_opencode("per_abc", approved=False)
        mock_client.post.assert_called_once_with(
            "http://127.0.0.1:57001/permission/per_abc/reply",
            json={"reply": "reject"},
        )

    async def test_no_op_for_empty_request_id(self):
        broker = _make_broker()
        with patch("httpx.AsyncClient") as mock_client_cls:
            await broker._reply_to_opencode("", approved=True)
            mock_client_cls.assert_not_called()


# ── SSE connect timeout (Issue 10.1) ──────────────────────────────────────────


class TestSSEConnectTimeout(unittest.TestCase):
    """_listen_sse uses a connect timeout to avoid hanging forever."""

    def test_sse_client_uses_connect_timeout(self):
        """The SSE client must specify a connect timeout (not timeout=None)."""
        import inspect

        import gateway.agents.opencode.broker as broker_mod

        source = inspect.getsource(broker_mod.OpenCodePermissionBroker._listen_sse)
        # Must NOT use timeout=None (would hang forever on connect)
        self.assertNotIn("timeout=None", source)
        # Must use an httpx.Timeout with a connect value
        self.assertIn("httpx.Timeout", source)
        self.assertIn("connect=", source)
        self.assertIn("response.raise_for_status()", source)


# ── _queue_auto_reply (Issue 10.3) ─────────────────────────────────────────────


class TestQueueAutoReply(unittest.IsolatedAsyncioTestCase):
    """Auto-reply dispatches a background task rather than blocking the SSE loop."""

    async def test_auto_reply_creates_tracked_task(self):
        """_queue_auto_reply adds a task to _pending_tasks."""
        broker = _make_broker()
        broker._reply_to_opencode = AsyncMock()

        self.assertEqual(len(broker._pending_tasks), 0)
        broker._queue_auto_reply("per_test", approved=True)
        # Task is created and tracked
        self.assertEqual(len(broker._pending_tasks), 1)
        # Drain the task
        await asyncio.gather(*list(broker._pending_tasks), return_exceptions=True)
        broker._reply_to_opencode.assert_called_once_with("per_test", approved=True)

    async def test_auto_reply_task_removes_itself_on_done(self):
        """Completed tasks are removed from _pending_tasks automatically."""
        broker = _make_broker()
        broker._reply_to_opencode = AsyncMock()

        broker._queue_auto_reply("per_test", approved=False)
        await asyncio.gather(*list(broker._pending_tasks), return_exceptions=True)
        # Task should have removed itself via done callback
        self.assertEqual(len(broker._pending_tasks), 0)


# ── start / stop lifecycle ─────────────────────────────────────────────────────


class TestStartStop(unittest.IsolatedAsyncioTestCase):
    """start() and stop() manage the reply client and SSE listener task."""

    async def test_start_creates_reply_client_and_sse_task(self):
        broker = _make_broker()
        stop_event = asyncio.Event()

        async def fake_listen_sse():
            await stop_event.wait()

        with patch.object(broker, "_listen_sse", side_effect=fake_listen_sse):
            await broker.start()
            try:
                self.assertIsNotNone(broker._reply_client)
                self.assertIsNotNone(broker._sse_task)
                self.assertFalse(broker._sse_task.done())
            finally:
                stop_event.set()
                await broker.stop()

    async def test_stop_clears_sse_task_to_none(self):
        """stop() must clear _sse_task to None even when the task is cancelled."""
        broker = _make_broker()

        async def fake_listen_sse():
            await asyncio.sleep(100)

        with patch.object(broker, "_listen_sse", side_effect=fake_listen_sse):
            await broker.start()
            self.assertIsNotNone(broker._sse_task)
            await broker.stop()
            self.assertIsNone(broker._sse_task)

    async def test_stop_closes_reply_client(self):
        """stop() must close the httpx reply client."""
        broker = _make_broker()

        async def fake_listen_sse():
            await asyncio.sleep(100)

        with patch.object(broker, "_listen_sse", side_effect=fake_listen_sse):
            await broker.start()
            await broker.stop()
            self.assertIsNone(broker._reply_client)

    async def test_stop_cancels_pending_tasks(self):
        """stop() drains and cancels any queued auto-reply tasks."""
        broker = _make_broker()
        broker._reply_to_opencode = AsyncMock()

        async def fake_listen_sse():
            await asyncio.sleep(100)

        with patch.object(broker, "_listen_sse", side_effect=fake_listen_sse):
            await broker.start()
            # Queue a background task but don't drain it
            broker._queue_auto_reply("per_test", approved=True)
            self.assertEqual(len(broker._pending_tasks), 1)
            await broker.stop()
            self.assertEqual(len(broker._pending_tasks), 0)

    async def test_stop_rejects_cancelled_permission_request_task(self):
        """A cancelled in-flight permission request must still send reject to opencode."""
        broker = _make_broker(
            session_room_map={"ses_owner": "room_1"},
            session_role_map={"ses_owner": "owner"},
        )
        gate = asyncio.Event()

        async def blocking_request_permission(**kwargs):
            await gate.wait()
            return True

        async def fake_listen_sse():
            await asyncio.sleep(100)

        broker.request_permission = AsyncMock(side_effect=blocking_request_permission)
        broker._reply_to_opencode = AsyncMock()

        with patch.object(broker, "_listen_sse", side_effect=fake_listen_sse):
            await broker.start()
            payload = {
                "type": "permission.asked",
                "properties": {
                    "id": "per_stop",
                    "permission": "bash",
                    "sessionID": "ses_owner",
                    "patterns": ["make"],
                    "metadata": {},
                },
            }
            await broker._handle_sse_line(_sse_line(payload))
            await asyncio.sleep(0)

            await broker.stop()

        broker._reply_to_opencode.assert_any_call("per_stop", approved=False)

    async def test_stop_does_not_send_duplicate_reject_for_cancelled_auto_reply(self):
        """Cancelling an auto-reply task must not append an extra reject reply."""
        broker = _make_broker()
        gate = asyncio.Event()

        async def slow_reply(*args, **kwargs):
            await gate.wait()

        async def fake_listen_sse():
            await asyncio.sleep(100)

        broker._reply_to_opencode = AsyncMock(side_effect=slow_reply)

        with patch.object(broker, "_listen_sse", side_effect=fake_listen_sse):
            await broker.start()
            broker._queue_auto_reply("per_auto", approved=True)
            await asyncio.sleep(0)

            await broker.stop()

        self.assertEqual(broker._reply_to_opencode.await_count, 1)


# ── owner_allowed_tools auto-approve ──────────────────────────────────────────


class TestOwnerAllowedTools(unittest.IsolatedAsyncioTestCase):
    """Owner sessions with matching owner_allowed_tools are auto-approved without RC notification."""

    def _make_owner_broker(self, owner_tools: list[str]) -> OpenCodePermissionBroker:
        from gateway.core.permission import ConnectorPermissionNotifier

        registry = PermissionRegistry()
        notifier = ConnectorPermissionNotifier({"ses_owner": MagicMock()})
        return OpenCodePermissionBroker(
            registry=registry,
            notifier=notifier,
            opencode_base_url="http://127.0.0.1:57000",
            session_room_map={"ses_owner": "room_1"},
            session_role_map={"ses_owner": "owner"},
            owner_allowed_tools=[ToolRule(tool=t) for t in owner_tools],
        )

    async def test_owner_auto_approves_matching_tool(self):
        broker = self._make_owner_broker(["Read", "Glob"])
        broker._reply_to_opencode = AsyncMock()
        broker._handle_permission_request = AsyncMock()

        payload = {
            "type": "permission.asked",
            "properties": {
                "id": "per_owner_auto",
                "permission": "Read",
                "sessionID": "ses_owner",
                "patterns": ["/tmp/file.txt"],
                "metadata": {},
            },
        }
        await broker._handle_sse_line(_sse_line(payload))
        await asyncio.gather(*list(broker._pending_tasks), return_exceptions=True)

        broker._reply_to_opencode.assert_called_once_with(
            "per_owner_auto", approved=True
        )
        broker._handle_permission_request.assert_not_called()

    async def test_owner_routes_unmatched_tool_to_handle_permission_request(self):
        """Tools not in owner_allowed_tools go through the full RC approval flow."""
        broker = self._make_owner_broker(["Read"])
        broker._reply_to_opencode = AsyncMock()
        broker._handle_permission_request = AsyncMock()

        payload = {
            "type": "permission.asked",
            "properties": {
                "id": "per_owner_bash",
                "permission": "bash",
                "sessionID": "ses_owner",
                "patterns": ["make build"],
                "metadata": {},
            },
        }
        await broker._handle_sse_line(_sse_line(payload))
        await asyncio.sleep(0)

        broker._reply_to_opencode.assert_not_called()
        broker._handle_permission_request.assert_called_once()


# ── _handle_permission_request ────────────────────────────────────────────────


class TestHandlePermissionRequest(unittest.IsolatedAsyncioTestCase):
    """_handle_permission_request calls request_permission and replies accordingly."""

    async def test_approved_replies_true(self):
        broker = _make_broker(
            session_room_map={"ses_1": "room_1"},
            session_role_map={"ses_1": "owner"},
        )
        broker._reply_to_opencode = AsyncMock()
        broker.request_permission = AsyncMock(return_value=True)

        await broker._handle_permission_request(
            opencode_req_id="per_001",
            tool_name="bash",
            tool_input={"command": "ls"},
            session_id="ses_1",
            room_id="room_1",
        )

        broker._reply_to_opencode.assert_called_once_with("per_001", True)

    async def test_denied_replies_false(self):
        broker = _make_broker(
            session_room_map={"ses_1": "room_1"},
            session_role_map={"ses_1": "owner"},
        )
        broker._reply_to_opencode = AsyncMock()
        broker.request_permission = AsyncMock(return_value=False)

        await broker._handle_permission_request(
            opencode_req_id="per_002",
            tool_name="bash",
            tool_input={},
            session_id="ses_1",
            room_id="room_1",
        )

        broker._reply_to_opencode.assert_called_once_with("per_002", False)

    async def test_notification_failure_auto_denies(self):
        """PermissionNotificationError from request_permission → auto-deny."""
        from gateway.core.permission import PermissionNotificationError

        broker = _make_broker(
            session_room_map={"ses_1": "room_1"},
            session_role_map={"ses_1": "owner"},
        )
        broker._reply_to_opencode = AsyncMock()
        broker.request_permission = AsyncMock(
            side_effect=PermissionNotificationError("RC down")
        )

        await broker._handle_permission_request(
            opencode_req_id="per_003",
            tool_name="bash",
            tool_input={},
            session_id="ses_1",
            room_id="room_1",
        )

        broker._reply_to_opencode.assert_called_once_with("per_003", False)


# ── _reply_to_opencode error paths ────────────────────────────────────────────


class TestReplyToOpencodeErrors(unittest.IsolatedAsyncioTestCase):
    """Error branches in _reply_to_opencode must not raise to the caller."""

    async def test_no_reply_client_returns_without_error(self):
        """When broker is not started (_reply_client is None), log and return cleanly."""
        broker = _make_broker()
        # _reply_client is None (broker.start() was never called)
        # Should not raise
        await broker._reply_to_opencode("per_abc", approved=True)

    async def test_http_error_is_logged_not_raised(self):
        """HTTP errors from the reply API are logged and swallowed."""
        broker = _make_broker()
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = Exception(
            "HTTP 500 Internal Server Error"
        )
        mock_client.post.return_value = mock_response
        broker._reply_client = mock_client

        # Should not raise
        await broker._reply_to_opencode("per_abc", approved=True)


# ── _listen_sse reconnect ─────────────────────────────────────────────────────


class TestListenSseReconnect(unittest.IsolatedAsyncioTestCase):
    """_listen_sse reconnects after a connection error (sleeps 3s then retries)."""

    async def test_reconnects_after_connection_error(self):
        """A ConnectionError causes a 3s sleep then a reconnect attempt."""
        broker = _make_broker()

        sleep_calls: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)
            raise asyncio.CancelledError()  # abort after first reconnect sleep

        class FakeClientCtx:
            async def __aenter__(self):
                raise ConnectionError("connection refused")

            async def __aexit__(self, *args):
                pass

        with (
            patch("httpx.AsyncClient", return_value=FakeClientCtx()),
            patch("asyncio.sleep", side_effect=fake_sleep),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await broker._listen_sse()

        self.assertEqual(sleep_calls, [3])

    async def test_cancelled_error_not_swallowed(self):
        """asyncio.CancelledError propagates out of _listen_sse immediately."""
        broker = _make_broker()

        class FakeClientCtx:
            async def __aenter__(self):
                raise asyncio.CancelledError()

            async def __aexit__(self, *args):
                pass

        with patch("httpx.AsyncClient", return_value=FakeClientCtx()):
            with self.assertRaises(asyncio.CancelledError):
                await broker._listen_sse()


if __name__ == "__main__":
    unittest.main()


class TestHumanApprovalDisabled(unittest.IsolatedAsyncioTestCase):
    """permissions.enabled: false → owner asks outside the allow-list are
    rejected at once through the reply API; nothing is posted to chat. The
    allow-lists (incl. the built-in owner rules) still approve (ADR-0002, #165)."""

    def _payload(self, req_id: str, permission: str, session: str, patterns: list[str]) -> dict:
        return {
            "type": "permission.asked",
            "properties": {
                "id": req_id, "permission": permission, "sessionID": session,
                "patterns": patterns, "metadata": {},
            },
        }

    async def _run(self, broker, payload) -> None:
        broker._reply_to_opencode = AsyncMock()
        broker._handle_permission_request = AsyncMock()
        await broker._handle_sse_line(_sse_line(payload))
        await asyncio.gather(*list(broker._pending_tasks), return_exceptions=True)

    async def test_owner_unlisted_bash_is_rejected_without_asking(self):
        broker = _make_broker(
            session_room_map={"ses_o": "room_o"}, session_role_map={"ses_o": "owner"},
            owner_allowed_tools=[], human_approval=False,
        )
        await self._run(broker, self._payload("per_1", "bash", "ses_o", ["rm -rf /"]))
        broker._reply_to_opencode.assert_called_once_with("per_1", approved=False)
        broker._handle_permission_request.assert_not_called()

    async def test_opencode_internal_permission_is_rejected_the_same_way(self):
        """external_directory / doom_loop / a .env read / edit (write, edit,
        multiedit) / webfetch / websearch arrive as permission names; unlisted,
        they are rejected — never left waiting (the #165 hang)."""
        broker = _make_broker(
            session_room_map={"ses_o": "room_o"}, session_role_map={"ses_o": "owner"},
            human_approval=False,
        )
        for perm in ("external_directory", "doom_loop", "read", "edit", "webfetch", "websearch"):
            await self._run(broker, self._payload(f"per_{perm}", perm, "ses_o", ["/etc/*"]))
            broker._reply_to_opencode.assert_called_once_with(f"per_{perm}", approved=False)
            broker._handle_permission_request.assert_not_called()

    async def test_owner_allow_listed_tool_is_approved(self):
        broker = _make_broker(
            session_room_map={"ses_o": "room_o"}, session_role_map={"ses_o": "owner"},
            owner_allowed_tools=["read"], human_approval=False,
        )
        await self._run(broker, self._payload("per_2", "read", "ses_o", ["a.txt"]))
        broker._reply_to_opencode.assert_called_once_with("per_2", approved=True)

    async def test_builtin_owner_rule_still_approves_coop_send(self):
        from gateway.core.config import AgentConfig

        broker = _make_broker(
            session_room_map={"ses_o": "room_o"}, session_role_map={"ses_o": "owner"},
            human_approval=False,
        )
        broker._owner_allowed_tools = AgentConfig().effective_owner_allowed_tools()
        await self._run(broker, self._payload("per_3", "bash", "ses_o", ["coop legacy send --room r hi"]))
        broker._reply_to_opencode.assert_called_once_with("per_3", approved=True)

    async def test_guest_policy_unchanged(self):
        broker = _make_broker(
            session_room_map={"ses_g": "room_g"}, session_role_map={"ses_g": "guest"},
            guest_allowed_tools=["read"], human_approval=False,
        )
        await self._run(broker, self._payload("per_4", "read", "ses_g", ["a"]))
        broker._reply_to_opencode.assert_called_once_with("per_4", approved=True)
        await self._run(broker, self._payload("per_5", "bash", "ses_g", ["ls"]))
        broker._reply_to_opencode.assert_called_once_with("per_5", approved=False)

    async def test_skip_owner_approval_wins_over_disabled_approval(self):
        broker = _make_broker(
            session_room_map={"ses_o": "room_o"}, session_role_map={"ses_o": "owner"},
            human_approval=False, skip_owner_approval=True,
        )
        await self._run(broker, self._payload("per_6", "bash", "ses_o", ["rm -rf /"]))
        broker._reply_to_opencode.assert_called_once_with("per_6", approved=True)

