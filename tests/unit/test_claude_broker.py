"""Unit tests for ClaudePermissionBroker HTTP hook server and _http_utils.

Covers:
  - Guest tool call → auto-deny for unlisted tools
  - Guest tool call → auto-allow for explicitly allowed tools
  - Owner tool call → auto-approve for always-allowed tools
  - Owner tool call → creates PermissionRequest for tools needing approval
  - _http_utils: body size limit rejection
  - _http_utils: correct Content-Length with CJK content
  - Full HTTP round-trip: hook request → policy check → correct HTTP response

The policy tests exercise _handle_hook directly for fast, focused unit testing.
The TestHTTPServerIntegration suite verifies the full HTTP round-trip (including
Content-Length and JSON body correctness) to confirm that _handle_connection
correctly calls build_http_response(body) with the right argument order.

Run with:
    uv run python -m pytest tests/test_claude_broker.py -v
"""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock

from gateway.agents.claude._http_utils import (
    _MAX_HTTP_BODY,
    build_error_response,
    build_http_response,
    read_http_body,
)
from gateway.agents.claude.broker import ClaudePermissionBroker
from gateway.config import ToolRule
from gateway.core.permission import (
    ConnectorPermissionNotifier,
    PermissionRegistry,
)

# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_broker(
    *,
    session_room_map: dict[str, str] | None = None,
    session_role_map: dict[str, str] | None = None,
    session_connector_map: dict | None = None,
    owner_allowed_tools: list[str] | None = None,
    guest_allowed_tools: list[str] | None = None,
    timeout_seconds: int = 300,
    skip_owner_approval: bool = False,
    human_approval: bool = True,
) -> ClaudePermissionBroker:
    """Create a broker with mock notifier for testing."""
    registry = PermissionRegistry()
    connector = MagicMock()
    connector.send_text = AsyncMock()
    if session_connector_map is None:
        session_connector_map = {
            sid: connector for sid in (session_room_map or {})
        }
    notifier = ConnectorPermissionNotifier(session_connector_map)
    return ClaudePermissionBroker(
        registry=registry,
        notifier=notifier,
        session_room_map=session_room_map or {},
        session_role_map=session_role_map or {},
        owner_allowed_tools=[ToolRule(tool=t) for t in (owner_allowed_tools or [])],
        guest_allowed_tools=[ToolRule(tool=t) for t in (guest_allowed_tools or [])],
        timeout_seconds=timeout_seconds,
        skip_owner_approval=skip_owner_approval,
        human_approval=human_approval,
    )


def _hook_body(
    tool_name: str,
    tool_input: dict | None = None,
    session_id: str = "ses_test",
    cwd: str = "/tmp",
) -> str:
    """Build a JSON body mimicking Claude's PreToolUse hook POST."""
    return json.dumps({
        "tool_name": tool_name,
        "tool_input": tool_input or {},
        "session_id": session_id,
        "cwd": cwd,
    })


async def _send_hook_request(port: int, body: str) -> tuple[int, dict]:
    """Send an HTTP POST to the broker hook server and return (status, json_body).

    Uses raw asyncio streams to avoid extra dependencies.
    """
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    body_bytes = body.encode()
    request = (
        f"POST /hook HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body_bytes)}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode() + body_bytes
    writer.write(request)
    await writer.drain()

    # Read status line
    status_line = await reader.readline()
    status_code = int(status_line.decode().split(" ", 2)[1])

    # Read headers
    headers: dict[str, str] = {}
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        if b":" in line:
            key, value = line.decode().split(":", 1)
            headers[key.strip().lower()] = value.strip()

    # Read body
    content_length = int(headers.get("content-length", "0"))
    if content_length > 0:
        response_body = await reader.readexactly(content_length)
    else:
        response_body = await reader.read()
    writer.close()
    await writer.wait_closed()

    return status_code, json.loads(response_body.decode())


# ── Guest policy tests (via _handle_hook) ────────────────────────────────────

class TestGuestAutoDeny(unittest.IsolatedAsyncioTestCase):
    """Guest sessions: unlisted tools are auto-denied."""

    async def test_guest_unlisted_tool_blocked(self):
        """Guest calling an unlisted tool receives a block decision."""
        broker = _make_broker(
            session_room_map={"ses_g": "room_g"},
            session_role_map={"ses_g": "guest"},
            guest_allowed_tools=["Read", "Grep"],
        )
        body = _hook_body("Bash", {"command": "rm -rf /"}, session_id="ses_g")
        result = json.loads(await broker._handle_hook(body))
        self.assertEqual(result["decision"], "block")
        self.assertIn("not permitted", result["reason"])

    async def test_guest_unknown_session_defaults_to_guest_block(self):
        """A session not in session_role_map defaults to guest → block."""
        broker = _make_broker(
            session_room_map={},
            session_role_map={},
            guest_allowed_tools=["Read"],
        )
        body = _hook_body("Bash", {"command": "ls"}, session_id="ses_unknown")
        result = json.loads(await broker._handle_hook(body))
        self.assertEqual(result["decision"], "block")

    async def test_missing_role_does_not_grant_owner(self):
        """Fail-closed: missing role must NOT silently elevate to owner.

        Even if the session has a room mapping, an absent role must default
        to 'guest' and deny tools not in guest_allowed_tools.
        """
        broker = _make_broker(
            session_room_map={"ses_new": "room_1"},
            session_role_map={},
            guest_allowed_tools=["Read"],
        )
        body = _hook_body("Bash", {"command": "ls"}, session_id="ses_new")
        result = json.loads(await broker._handle_hook(body))
        self.assertEqual(result["decision"], "block")


class TestGuestAutoAllow(unittest.IsolatedAsyncioTestCase):
    """Guest sessions: explicitly allowed tools are auto-approved."""

    async def test_guest_allowed_tool_approved(self):
        """Guest calling a tool in guest_allowed_tools receives allow."""
        broker = _make_broker(
            session_room_map={"ses_g": "room_g"},
            session_role_map={"ses_g": "guest"},
            guest_allowed_tools=["Read", "Grep", "Glob"],
        )
        body = _hook_body("Read", {"file_path": "/src/main.py"}, session_id="ses_g")
        result = json.loads(await broker._handle_hook(body))
        self.assertEqual(result["decision"], "allow")

    async def test_guest_wildcard_tool_approved(self):
        """Guest tool rules support regex patterns (e.g. 'mcp__rocketchat__.*')."""
        broker = _make_broker(
            session_room_map={"ses_g": "room_g"},
            session_role_map={"ses_g": "guest"},
            guest_allowed_tools=["mcp__rocketchat__.*"],
        )
        body = _hook_body("mcp__rocketchat__send_message", {}, session_id="ses_g")
        result = json.loads(await broker._handle_hook(body))
        self.assertEqual(result["decision"], "allow")

    async def test_guest_allowed_tool_name_case_insensitive(self):
        """Tool name matching is case-insensitive (Read == read)."""
        broker = _make_broker(
            session_room_map={"ses_g": "room_g"},
            session_role_map={"ses_g": "guest"},
            guest_allowed_tools=["read"],
        )
        body = _hook_body("Read", {"file_path": "/src/main.py"}, session_id="ses_g")
        result = json.loads(await broker._handle_hook(body))
        self.assertEqual(result["decision"], "allow")


# ── Owner policy tests (via _handle_hook) ────────────────────────────────────

class TestOwnerAutoApprove(unittest.IsolatedAsyncioTestCase):
    """Owner sessions: tools in owner_allowed_tools are auto-approved."""

    async def test_owner_allowed_tool_approved(self):
        """Owner calling a tool in owner_allowed_tools gets instant allow."""
        broker = _make_broker(
            session_room_map={"ses_o": "room_o"},
            session_role_map={"ses_o": "owner"},
            owner_allowed_tools=["Read", "Grep", "Glob", "Bash"],
        )
        body = _hook_body("Bash", {"command": "ls -la"}, session_id="ses_o")
        result = json.loads(await broker._handle_hook(body))
        self.assertEqual(result["decision"], "allow")

    async def test_owner_unlisted_tool_not_auto_approved(self):
        """Owner calling a tool NOT in owner_allowed_tools does not get instant allow."""
        broker = _make_broker(
            session_room_map={"ses_o": "room_o"},
            session_role_map={"ses_o": "owner"},
            owner_allowed_tools=["Read"],
        )
        # _handle_hook will call request_permission which blocks — mock it
        broker.request_permission = AsyncMock(return_value=True)
        body = _hook_body("Bash", {"command": "make build"}, session_id="ses_o")
        result = json.loads(await broker._handle_hook(body))
        # Should have called request_permission (not auto-approved)
        broker.request_permission.assert_called_once()
        self.assertEqual(result["decision"], "allow")


class TestOwnerPermissionRequest(unittest.IsolatedAsyncioTestCase):
    """Owner sessions: unlisted tools create a PermissionRequest and block."""

    async def test_owner_unlisted_tool_creates_permission_request(self):
        """Owner calling unlisted tool → request_permission is invoked with correct args."""
        broker = _make_broker(
            session_room_map={"ses_o": "room_o"},
            session_role_map={"ses_o": "owner"},
            owner_allowed_tools=["Read"],
            timeout_seconds=5,
        )
        broker.request_permission = AsyncMock(return_value=True)

        body = _hook_body("Bash", {"command": "make build"}, session_id="ses_o")
        result = json.loads(await broker._handle_hook(body))

        self.assertEqual(result["decision"], "allow")
        broker.request_permission.assert_called_once()
        call_kwargs = broker.request_permission.call_args.kwargs
        self.assertEqual(call_kwargs["tool_name"], "Bash")
        self.assertEqual(call_kwargs["session_id"], "ses_o")
        self.assertEqual(call_kwargs["room_id"], "room_o")

    async def test_owner_denied_tool_returns_block(self):
        """Owner denying a tool call → block decision returned."""
        broker = _make_broker(
            session_room_map={"ses_o": "room_o"},
            session_role_map={"ses_o": "owner"},
            owner_allowed_tools=[],
            timeout_seconds=5,
        )
        broker.request_permission = AsyncMock(return_value=False)

        body = _hook_body("Write", {"file_path": "/etc/passwd", "content": "x"}, session_id="ses_o")
        result = json.loads(await broker._handle_hook(body))
        self.assertEqual(result["decision"], "block")

    async def test_owner_no_room_mapping_blocks(self):
        """Owner session with no room_id mapping → block (can't post notification)."""
        broker = _make_broker(
            session_room_map={},
            session_role_map={"ses_o": "owner"},
            owner_allowed_tools=[],
        )
        body = _hook_body("Bash", {"command": "ls"}, session_id="ses_o")
        result = json.loads(await broker._handle_hook(body))
        self.assertEqual(result["decision"], "block")
        self.assertIn("no room mapping", result["reason"].lower())

    async def test_notification_failure_returns_retry_message(self):
        """If notification delivery fails, the hook returns a 'retry' block."""
        from gateway.core.permission import PermissionNotificationError

        broker = _make_broker(
            session_room_map={"ses_o": "room_o"},
            session_role_map={"ses_o": "owner"},
            owner_allowed_tools=[],
        )
        broker.request_permission = AsyncMock(
            side_effect=PermissionNotificationError("delivery failed")
        )

        body = _hook_body("Bash", {"command": "ls"}, session_id="ses_o")
        result = json.loads(await broker._handle_hook(body))
        self.assertEqual(result["decision"], "block")
        self.assertIn("retry", result["reason"].lower())


# ── skip_owner_approval tests ────────────────────────────────────────────────

class TestSkipOwnerApproval(unittest.IsolatedAsyncioTestCase):
    """skip_owner_approval=True bypasses all owner checks and auto-approves every tool."""

    async def test_owner_any_tool_auto_approved_when_flag_set(self):
        """With skip_owner_approval, owner tool calls are allowed without RC notification."""
        broker = _make_broker(
            session_room_map={"ses_o": "room_o"},
            session_role_map={"ses_o": "owner"},
            owner_allowed_tools=[],          # empty allow-list — would normally trigger ask
            skip_owner_approval=True,
        )
        broker.request_permission = AsyncMock(return_value=True)

        body = _hook_body("Bash", {"command": "rm -rf /"}, session_id="ses_o")
        result = json.loads(await broker._handle_hook(body))

        self.assertEqual(result["decision"], "allow")
        broker.request_permission.assert_not_called()

    async def test_owner_no_room_mapping_still_allowed_when_flag_set(self):
        """skip_owner_approval bypasses the room-mapping check that would normally block."""
        broker = _make_broker(
            session_room_map={},             # no room mapping — would normally block
            session_role_map={"ses_o": "owner"},
            owner_allowed_tools=[],
            skip_owner_approval=True,
        )
        broker.request_permission = AsyncMock(return_value=True)

        body = _hook_body("Write", {"file_path": "/tmp/out.txt"}, session_id="ses_o")
        result = json.loads(await broker._handle_hook(body))

        self.assertEqual(result["decision"], "allow")
        broker.request_permission.assert_not_called()

    async def test_guest_still_blocked_when_skip_owner_approval_set(self):
        """skip_owner_approval only affects owners — guests remain subject to their allow-list."""
        broker = _make_broker(
            session_room_map={"ses_g": "room_g"},
            session_role_map={"ses_g": "guest"},
            guest_allowed_tools=["Read"],
            skip_owner_approval=True,
        )

        body = _hook_body("Bash", {"command": "ls"}, session_id="ses_g")
        result = json.loads(await broker._handle_hook(body))

        self.assertEqual(result["decision"], "block")
        self.assertIn("not permitted", result["reason"])

    async def test_guest_allowed_tool_unaffected_when_skip_owner_approval_set(self):
        """guest_allowed_tools still works normally when skip_owner_approval is enabled."""
        broker = _make_broker(
            session_room_map={"ses_g": "room_g"},
            session_role_map={"ses_g": "guest"},
            guest_allowed_tools=["Read", "Grep"],
            skip_owner_approval=True,
        )

        body = _hook_body("Read", {"file_path": "/src/main.py"}, session_id="ses_g")
        result = json.loads(await broker._handle_hook(body))

        self.assertEqual(result["decision"], "allow")

    async def test_skip_owner_approval_false_preserves_normal_ask_flow(self):
        """When skip_owner_approval is False (default), unlisted owner tools still trigger ask."""
        broker = _make_broker(
            session_room_map={"ses_o": "room_o"},
            session_role_map={"ses_o": "owner"},
            owner_allowed_tools=[],
            skip_owner_approval=False,
        )
        broker.request_permission = AsyncMock(return_value=False)

        body = _hook_body("Bash", {"command": "ls"}, session_id="ses_o")
        result = json.loads(await broker._handle_hook(body))

        # request_permission called (not skipped), returned False → block
        broker.request_permission.assert_called_once()
        self.assertEqual(result["decision"], "block")


# ── Meta-tool auto-allow tests ───────────────────────────────────────────────

class TestMetaToolAutoAllow(unittest.IsolatedAsyncioTestCase):
    """Claude Code meta-tools (e.g. ToolSearch) are always auto-allowed.

    ToolSearch loads deferred tool schemas and has no side-effects.  It must
    never surface as a spurious approval request regardless of role, allow-list
    configuration, or skip_owner_approval setting.
    """

    async def test_toolsearch_auto_allowed_for_owner(self):
        """ToolSearch is allowed for owner even when not in owner_allowed_tools."""
        broker = _make_broker(
            session_room_map={"ses_o": "room_o"},
            session_role_map={"ses_o": "owner"},
            owner_allowed_tools=[],   # empty — would normally trigger "ask"
        )
        broker.request_permission = AsyncMock(return_value=True)

        body = _hook_body(
            "ToolSearch",
            {"query": "select:WebSearch", "max_results": 1},
            session_id="ses_o",
        )
        result = json.loads(await broker._handle_hook(body))

        self.assertEqual(result["decision"], "allow")
        broker.request_permission.assert_not_called()

    async def test_toolsearch_auto_allowed_for_guest(self):
        """ToolSearch is allowed for guest even when not in guest_allowed_tools."""
        broker = _make_broker(
            session_room_map={"ses_g": "room_g"},
            session_role_map={"ses_g": "guest"},
            guest_allowed_tools=["Read"],   # ToolSearch not listed → would block
        )
        body = _hook_body(
            "ToolSearch",
            {"query": "select:WebSearch", "max_results": 1},
            session_id="ses_g",
        )
        result = json.loads(await broker._handle_hook(body))

        self.assertEqual(result["decision"], "allow")

    async def test_toolsearch_auto_allowed_no_room_mapping(self):
        """ToolSearch is allowed even when the session has no room mapping.

        Without this, a guest/owner with no room would hit the 'no room mapping'
        block path instead of auto-allowing the meta-tool.
        """
        broker = _make_broker(
            session_room_map={},
            session_role_map={"ses_o": "owner"},
            owner_allowed_tools=[],
        )
        body = _hook_body("ToolSearch", {"query": "select:Bash"}, session_id="ses_o")
        result = json.loads(await broker._handle_hook(body))

        self.assertEqual(result["decision"], "allow")

    async def test_toolsearch_auto_allowed_with_skip_owner_approval(self):
        """ToolSearch remains allowed when skip_owner_approval is also set."""
        broker = _make_broker(
            session_room_map={"ses_o": "room_o"},
            session_role_map={"ses_o": "owner"},
            owner_allowed_tools=[],
            skip_owner_approval=True,
        )
        body = _hook_body("ToolSearch", {"query": "select:Grep"}, session_id="ses_o")
        result = json.loads(await broker._handle_hook(body))

        self.assertEqual(result["decision"], "allow")

    async def test_non_meta_tool_not_auto_allowed(self):
        """Non-meta tools (e.g. WebSearch) are NOT auto-allowed by the meta-tool path."""
        broker = _make_broker(
            session_room_map={"ses_g": "room_g"},
            session_role_map={"ses_g": "guest"},
            guest_allowed_tools=["Read"],
        )
        body = _hook_body("WebSearch", {"query": "hello"}, session_id="ses_g")
        result = json.loads(await broker._handle_hook(body))

        # WebSearch not in guest_allowed_tools → block (meta-tool path not triggered)
        self.assertEqual(result["decision"], "block")


# ── Malformed input handling ─────────────────────────────────────────────────

class TestMalformedInput(unittest.IsolatedAsyncioTestCase):
    """The hook handler handles malformed input gracefully."""

    async def test_malformed_json_returns_block(self):
        """Invalid JSON body → block decision."""
        broker = _make_broker()
        result = json.loads(await broker._handle_hook("{not valid json"))
        self.assertEqual(result["decision"], "block")

    async def test_empty_body_returns_block(self):
        """Empty body → defaults to empty dict, tool_name='' → guest block."""
        broker = _make_broker()
        result = json.loads(await broker._handle_hook(""))
        self.assertEqual(result["decision"], "block")


# ── HTTP server integration ──────────────────────────────────────────────────

class TestHTTPServerIntegration(unittest.IsolatedAsyncioTestCase):
    """Tests that exercise the actual HTTP server (start/stop/handle_connection)."""

    async def test_server_starts_on_random_port(self):
        """start() binds to a free port and sets broker._port."""
        broker = _make_broker()
        await broker.start()
        try:
            self.assertGreater(broker._port, 0)
        finally:
            await broker.stop()

    async def test_guest_allowed_tool_returns_allow_over_http(self):
        """Guest allowed tool call returns an allow decision end-to-end over HTTP.

        Verifies the full HTTP round-trip: hook request → policy check → HTTP
        response with correct Content-Length and JSON body.
        """
        broker = _make_broker(
            session_room_map={"ses_g": "room_g"},
            session_role_map={"ses_g": "guest"},
            guest_allowed_tools=["Read"],
        )
        await broker.start()
        try:
            body = _hook_body("Read", {"file_path": "/src/main.py"}, session_id="ses_g")
            status, resp = await _send_hook_request(broker._port, body)
            self.assertEqual(status, 200)
            self.assertEqual(resp["decision"], "allow")
        finally:
            await broker.stop()

    async def test_malformed_json_over_http(self):
        """Malformed JSON sent via HTTP returns a block decision.

        The error path uses build_error_response which has correct args,
        so this works end-to-end.
        """
        broker = _make_broker()
        await broker.start()
        try:
            status, resp = await _send_hook_request(broker._port, "{bad json")
            self.assertEqual(status, 200)
            self.assertEqual(resp["decision"], "block")
        finally:
            await broker.stop()


# ── _http_utils direct tests ─────────────────────────────────────────────────

class TestReadHttpBody(unittest.IsolatedAsyncioTestCase):
    """Test the shared read_http_body utility."""

    async def _make_reader(self, raw: bytes) -> asyncio.StreamReader:
        """Create a StreamReader pre-loaded with raw HTTP request bytes."""
        reader = asyncio.StreamReader()
        reader.feed_data(raw)
        reader.feed_eof()
        return reader

    async def test_body_size_limit_rejection(self):
        """Content-Length exceeding _MAX_HTTP_BODY raises ValueError."""
        fake_length = _MAX_HTTP_BODY + 1
        raw = (
            f"POST /hook HTTP/1.1\r\n"
            f"Content-Length: {fake_length}\r\n"
            f"\r\n"
        ).encode()
        reader = await self._make_reader(raw)

        with self.assertRaises(ValueError) as ctx:
            await read_http_body(reader)
        self.assertIn("too large", str(ctx.exception))

    async def test_normal_body_read(self):
        """A normal-sized body is read correctly."""
        body = '{"tool_name": "Read"}'
        body_bytes = body.encode()
        raw = (
            f"POST /hook HTTP/1.1\r\n"
            f"Content-Length: {len(body_bytes)}\r\n"
            f"\r\n"
        ).encode() + body_bytes
        reader = await self._make_reader(raw)

        result = await read_http_body(reader)
        self.assertEqual(result, body)

    async def test_cjk_body_read_correctly(self):
        """CJK content is read correctly when Content-Length uses byte count."""
        body = '{"reason": "工具未被允許"}'
        body_bytes = body.encode()
        raw = (
            f"POST /hook HTTP/1.1\r\n"
            f"Content-Length: {len(body_bytes)}\r\n"
            f"\r\n"
        ).encode() + body_bytes
        reader = await self._make_reader(raw)

        result = await read_http_body(reader)
        self.assertEqual(result, body)

    async def test_empty_request_raises(self):
        """Empty request (no data) raises ConnectionError."""
        reader = await self._make_reader(b"")
        with self.assertRaises(ConnectionError):
            await read_http_body(reader)

    async def test_zero_content_length_returns_empty(self):
        """Content-Length: 0 returns an empty string."""
        raw = (
            "POST /hook HTTP/1.1\r\n"
            "Content-Length: 0\r\n"
            "\r\n"
        ).encode()
        reader = await self._make_reader(raw)
        result = await read_http_body(reader)
        self.assertEqual(result, "")

    async def test_max_body_size_exactly_at_limit(self):
        """Content-Length == _MAX_HTTP_BODY is allowed (boundary check)."""
        raw = (
            f"POST /hook HTTP/1.1\r\n"
            f"Content-Length: {_MAX_HTTP_BODY}\r\n"
            f"\r\n"
        ).encode()
        # We don't actually provide the body bytes — just verify no ValueError
        reader = asyncio.StreamReader()
        reader.feed_data(raw)
        # Feed enough data to satisfy readexactly (just a small amount for test speed)
        # This will raise IncompleteReadError, not ValueError — that's what we want
        reader.feed_eof()
        with self.assertRaises(asyncio.IncompleteReadError):
            await read_http_body(reader)
        # The key point: ValueError was NOT raised for exactly-at-limit


class TestBuildHttpResponse(unittest.TestCase):
    """Test the shared build_http_response utility."""

    def test_correct_content_length_with_ascii(self):
        """ASCII body: byte length == character length."""
        body = '{"decision": "allow"}'
        result = build_http_response(body)
        header_part = result.split(b"\r\n\r\n")[0].decode()
        for line in header_part.split("\r\n"):
            if line.lower().startswith("content-length"):
                cl = int(line.split(":")[1].strip())
                self.assertEqual(cl, len(body.encode()))
                break

    def test_correct_content_length_with_cjk(self):
        """CJK body: byte length > character length. Content-Length must use bytes.

        This verifies the fix for code review finding #2: the old code used
        ``len(response_body)`` (character count) instead of byte count,
        which would truncate CJK responses.
        """
        body = '{"reason": "工具未被允許"}'
        body_bytes = body.encode()
        self.assertGreater(len(body_bytes), len(body))

        result = build_http_response(body)
        header_part = result.split(b"\r\n\r\n")[0].decode()
        for line in header_part.split("\r\n"):
            if line.lower().startswith("content-length"):
                cl = int(line.split(":")[1].strip())
                self.assertEqual(cl, len(body_bytes))
                break

        # Verify the full body is present after the header separator
        actual_body = result.split(b"\r\n\r\n", 1)[1]
        self.assertEqual(actual_body, body_bytes)

    def test_response_status_code(self):
        """build_http_response includes the correct status line."""
        result = build_http_response('{}', status=200)
        self.assertTrue(result.startswith(b"HTTP/1.1 200 OK\r\n"))

    def test_custom_status_code(self):
        """build_http_response supports custom status codes."""
        result = build_http_response('{}', status=500, status_text="Internal Server Error")
        self.assertTrue(result.startswith(b"HTTP/1.1 500 Internal Server Error\r\n"))

    def test_connection_close_header(self):
        """Response includes Connection: close."""
        result = build_http_response('{}')
        self.assertIn(b"Connection: close", result)


class TestBuildErrorResponse(unittest.TestCase):
    """Test the shared build_error_response utility."""

    def test_error_response_is_block_decision(self):
        """Error responses return a block decision with reason."""
        result = build_error_response("something went wrong")
        body = result.split(b"\r\n\r\n", 1)[1]
        parsed = json.loads(body)
        self.assertEqual(parsed["decision"], "block")
        self.assertIn("something went wrong", parsed["reason"])

    def test_error_response_is_valid_http(self):
        """Error response starts with a valid HTTP/1.1 status line."""
        result = build_error_response("test error")
        self.assertTrue(result.startswith(b"HTTP/1.1 200 OK\r\n"))


if __name__ == "__main__":
    unittest.main()


# ── Appended from test_round9_fixes.py ────────────────────────────────────────


class TestBrokerParseOffloadedToThread(unittest.IsolatedAsyncioTestCase):
    """get_param_strings_for_claude must be called via asyncio.to_thread."""

    async def test_handle_hook_uses_to_thread_for_param_strings(self):
        """Verify asyncio.to_thread is used for get_param_strings_for_claude."""
        import asyncio
        import json
        from unittest.mock import patch

        from gateway.agents.claude.broker import ClaudePermissionBroker
        from gateway.core.permission import PermissionNotifier, PermissionRegistry

        registry = MagicMock(spec=PermissionRegistry)
        notifier = MagicMock(spec=PermissionNotifier)
        broker = ClaudePermissionBroker(
            registry=registry,
            notifier=notifier,
            session_room_map={},
        )

        to_thread_fns = []
        original_to_thread = asyncio.to_thread

        async def spy_to_thread(fn, *args, **kwargs):
            to_thread_fns.append(getattr(fn, "__name__", str(fn)))
            return await original_to_thread(fn, *args, **kwargs)

        body = json.dumps({
            "tool_name": "Bash",
            "tool_input": {"command": "echo hello"},
            "session_id": "ses_abc123",
            "cwd": "/tmp",
        })

        with patch("gateway.agents.claude.broker.asyncio.to_thread", side_effect=spy_to_thread):
            await broker._handle_hook(body)

        self.assertTrue(
            any("get_param_strings_for_claude" in fn for fn in to_thread_fns),
            f"get_param_strings_for_claude must be called via asyncio.to_thread; "
            f"to_thread called with: {to_thread_fns}",
        )


class TestBrokerStopCancelsConnections(unittest.IsolatedAsyncioTestCase):
    """stop() must cancel all in-flight _handle_connection tasks."""

    async def test_stop_cancels_pending_connection_task(self):
        """A connection task blocked at request_permission must be cancelled by stop()."""
        import asyncio

        from gateway.agents.claude.broker import ClaudePermissionBroker
        from gateway.core.permission import PermissionNotifier, PermissionRegistry

        registry = MagicMock(spec=PermissionRegistry)
        notifier = MagicMock(spec=PermissionNotifier)
        broker = ClaudePermissionBroker(
            registry=registry,
            notifier=notifier,
            session_room_map={},
        )
        broker._server = None

        cancelled = []

        async def long_running():
            task = asyncio.current_task()
            broker._connection_tasks.add(task)
            try:
                await asyncio.sleep(300)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
            finally:
                broker._connection_tasks.discard(task)

        task = asyncio.create_task(long_running())
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        await broker.stop()

        self.assertTrue(task.cancelled() or task.done(), "Task must be done after stop()")
        self.assertEqual(cancelled, [True], "Task must have received CancelledError")

    async def test_connection_tasks_empty_after_stop(self):
        """_connection_tasks must be empty after stop() completes."""
        import asyncio

        from gateway.agents.claude.broker import ClaudePermissionBroker
        from gateway.core.permission import PermissionNotifier, PermissionRegistry

        registry = MagicMock(spec=PermissionRegistry)
        notifier = MagicMock(spec=PermissionNotifier)
        broker = ClaudePermissionBroker(
            registry=registry,
            notifier=notifier,
            session_room_map={},
        )
        broker._server = None

        async def dummy():
            task = asyncio.current_task()
            broker._connection_tasks.add(task)
            try:
                await asyncio.sleep(300)
            except asyncio.CancelledError:
                raise
            finally:
                broker._connection_tasks.discard(task)

        task = asyncio.create_task(dummy())  # noqa: F841 — used inside dummy() closure
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        await broker.stop()
        self.assertEqual(len(broker._connection_tasks), 0)


# ── Unit tests for _decide() — pure policy, no async I/O needed ──────────────


def _make_decide_broker(
    owner_tools: list[str] | None = None,
    guest_tools: list[str] | None = None,
    room_id: str = "room_123",
    session_id: str = "ses_test",
) -> ClaudePermissionBroker:
    """Minimal broker for _decide() unit tests — no notifier/registry needed."""
    from unittest.mock import AsyncMock, MagicMock

    from gateway.core.permission import ConnectorPermissionNotifier, PermissionRegistry

    registry = PermissionRegistry()
    connector = MagicMock()
    connector.send_text = AsyncMock()
    notifier = ConnectorPermissionNotifier({session_id: connector})
    return ClaudePermissionBroker(
        registry=registry,
        notifier=notifier,
        session_room_map={session_id: room_id},
        session_role_map={session_id: "owner"},
        owner_allowed_tools=[ToolRule(tool=t) for t in (owner_tools or [])],
        guest_allowed_tools=[ToolRule(tool=t) for t in (guest_tools or [])],
    )


class TestDecideGuestPolicy(unittest.TestCase):
    """_decide(): guest role policy — synchronous, no I/O."""

    def test_guest_allowed_tool_returns_allow(self):
        broker = _make_decide_broker(guest_tools=["Read", "Grep"])
        action, reason = broker._decide("Read", ["some/path"], "guest", "room_1")
        self.assertEqual(action, "allow")
        self.assertEqual(reason, "")

    def test_guest_unlisted_tool_returns_block(self):
        broker = _make_decide_broker(guest_tools=["Read"])
        action, reason = broker._decide("Bash", ["rm -rf /"], "guest", "room_1")
        self.assertEqual(action, "block")
        self.assertIn("Bash", reason)
        self.assertIn("not permitted", reason)

    def test_guest_block_includes_tool_name(self):
        broker = _make_decide_broker(guest_tools=[])
        action, reason = broker._decide("WebFetch", ["http://x.com"], "guest", "room_1")
        self.assertEqual(action, "block")
        self.assertIn("WebFetch", reason)

    def test_guest_empty_allow_list_blocks_everything(self):
        """Empty guest_allowed_tools must block all guest tool calls."""
        broker = _make_decide_broker(guest_tools=[])
        for tool in ("Read", "Bash", "Write", "WebFetch"):
            action, _ = broker._decide(tool, ["x"], "guest", "room_1")
            self.assertEqual(action, "block", f"Expected block for {tool}")

    def test_guest_wildcard_tool_allows(self):
        broker = _make_decide_broker(guest_tools=[".*"])
        action, _ = broker._decide("AnyTool", ["x"], "guest", "room_1")
        self.assertEqual(action, "allow")

    def test_guest_room_id_irrelevant(self):
        """Guest decision must not depend on room_id — guests are always blocked or
        auto-allowed, never escalated to owner approval."""
        broker = _make_decide_broker(guest_tools=[])
        action_with_room, _ = broker._decide("Bash", ["x"], "guest", "room_1")
        action_no_room, _ = broker._decide("Bash", ["x"], "guest", "")
        self.assertEqual(action_with_room, "block")
        self.assertEqual(action_no_room, "block")


class TestDecideOwnerPolicy(unittest.TestCase):
    """_decide(): owner role policy — synchronous, no I/O."""

    def test_owner_allowed_tool_returns_allow(self):
        broker = _make_decide_broker(owner_tools=["Read", "Grep"])
        action, reason = broker._decide("Read", ["file.py"], "owner", "room_1")
        self.assertEqual(action, "allow")
        self.assertEqual(reason, "")

    def test_owner_unlisted_tool_with_room_returns_ask(self):
        """Unlisted owner tool + valid room → escalate to owner approval."""
        broker = _make_decide_broker(owner_tools=["Read"])
        action, reason = broker._decide("Bash", ["dangerous cmd"], "owner", "room_1")
        self.assertEqual(action, "ask")
        self.assertEqual(reason, "")

    def test_owner_unlisted_tool_no_room_returns_block(self):
        """Unlisted owner tool + no room mapping → fail-closed block."""
        broker = _make_decide_broker(owner_tools=[])
        action, reason = broker._decide("Bash", ["cmd"], "owner", "")
        self.assertEqual(action, "block")
        self.assertIn("no room mapping", reason.lower())

    def test_owner_empty_allow_list_always_asks(self):
        """No owner_allowed_tools → every tool goes to owner for approval."""
        broker = _make_decide_broker(owner_tools=[])
        action, _ = broker._decide("Write", ["/etc/hosts"], "owner", "room_1")
        self.assertEqual(action, "ask")

    def test_owner_wildcard_auto_approves_all(self):
        """owner_allowed_tools=['.*'] → all tools auto-approved without asking."""
        broker = _make_decide_broker(owner_tools=[".*"])
        for tool in ("Bash", "Write", "WebFetch"):
            action, _ = broker._decide(tool, ["x"], "owner", "room_1")
            self.assertEqual(action, "allow", f"Expected allow for {tool}")


class TestDecideParamMatching(unittest.TestCase):
    """_decide(): param regex matching is correctly forwarded."""

    def test_guest_param_mismatch_blocks(self):
        """Guest allow rule with params regex that doesn't match → block."""
        broker = _make_decide_broker(guest_tools=[])
        # Manually set a rule with a restrictive params regex
        broker._guest_allowed_tools = [ToolRule(tool="Read", params=r"/safe/.*")]
        action, _ = broker._decide("Read", ["/etc/passwd"], "guest", "room_1")
        self.assertEqual(action, "block")

    def test_guest_param_match_allows(self):
        """Guest allow rule with params regex that matches → allow."""
        broker = _make_decide_broker()
        broker._guest_allowed_tools = [ToolRule(tool="Read", params=r"/safe/.*")]
        action, _ = broker._decide("Read", ["/safe/file.txt"], "guest", "room_1")
        self.assertEqual(action, "allow")

    def test_owner_param_mismatch_escalates_to_ask(self):
        """Owner allow rule with params regex that doesn't match → ask (not block)."""
        broker = _make_decide_broker()
        broker._owner_allowed_tools = [ToolRule(tool="Bash", params=r"git .*")]
        action, _ = broker._decide("Bash", ["rm -rf /"], "owner", "room_1")
        self.assertEqual(action, "ask")

    def test_all_param_strings_must_match(self):
        """ALL param strings (e.g. compound bash sub-commands) must match for allow.
        A single non-matching param string causes block/ask."""
        broker = _make_decide_broker()
        broker._owner_allowed_tools = [ToolRule(tool="Bash", params=r"git .*")]
        # Two param strings: first matches, second doesn't
        action, _ = broker._decide("Bash", ["git status", "rm -rf /"], "owner", "room_1")
        self.assertEqual(action, "ask")  # not auto-allowed → escalates


class TestHumanApprovalDisabled(unittest.IsolatedAsyncioTestCase):
    """permissions.enabled: false → the broker still runs and still applies the
    allow-lists; what would have gone to a human is blocked with a reason the
    model reads. Nothing is ever posted to chat (ADR-0002, #165)."""

    def _broker(self, **kw) -> ClaudePermissionBroker:
        return _make_broker(
            session_room_map={"ses_o": "room_o", "ses_g": "room_g"},
            session_role_map={"ses_o": "owner", "ses_g": "guest"},
            human_approval=False,
            **kw,
        )

    async def test_owner_unlisted_tool_is_blocked_with_reason(self):
        broker = self._broker(owner_allowed_tools=["Read"])
        broker.request_permission = AsyncMock()
        result = json.loads(await broker._handle_hook(_hook_body("Write", session_id="ses_o")))
        self.assertEqual(result["decision"], "block")
        self.assertIn("permissions.enabled: false", result["reason"])
        self.assertIn("Write", result["reason"])
        broker.request_permission.assert_not_called()

    async def test_owner_allow_listed_tool_runs(self):
        broker = self._broker(owner_allowed_tools=["Read"])
        result = json.loads(await broker._handle_hook(_hook_body("Read", session_id="ses_o")))
        self.assertEqual(result["decision"], "allow")

    async def test_builtin_owner_rule_still_allows_coop_send(self):
        """The gateway's own commands must not be denied when approval is off —
        that is why the broker stays and the ruleset is not removed."""
        from gateway.core.config import AgentConfig

        broker = _make_broker(
            session_room_map={"ses_o": "room_o"},
            session_role_map={"ses_o": "owner"},
            human_approval=False,
        )
        broker._owner_allowed_tools = AgentConfig().effective_owner_allowed_tools()
        body = _hook_body("Bash", tool_input={"command": "coop legacy send --room r hello"}, session_id="ses_o")
        result = json.loads(await broker._handle_hook(body))
        self.assertEqual(result["decision"], "allow")
        body = _hook_body("Bash", tool_input={"command": "rm -rf /"}, session_id="ses_o")
        result = json.loads(await broker._handle_hook(body))
        self.assertEqual(result["decision"], "block")
        self.assertIn("permissions.enabled: false", result["reason"])

    async def test_meta_tool_still_allowed(self):
        broker = self._broker()
        result = json.loads(await broker._handle_hook(_hook_body("ToolSearch", session_id="ses_o")))
        self.assertEqual(result["decision"], "allow")

    async def test_guest_policy_unchanged(self):
        broker = self._broker(guest_allowed_tools=["Read"])
        allowed = json.loads(await broker._handle_hook(_hook_body("Read", session_id="ses_g")))
        self.assertEqual(allowed["decision"], "allow")
        blocked = json.loads(await broker._handle_hook(_hook_body("Write", session_id="ses_g")))
        self.assertEqual(blocked["decision"], "block")
        self.assertIn("Guest", blocked["reason"])
        self.assertNotIn("permissions.enabled", blocked["reason"])

    def test_deny_precedes_the_room_check(self):
        """No room mapping + approval off → the approval-off reason, not the
        room one: the answer does not depend on where a request could be posted."""
        broker = _make_broker(session_role_map={"ses_o": "owner"}, human_approval=False)
        action, reason = broker._decide("Write", [""], "owner", "")
        self.assertEqual(action, "block")
        self.assertIn("permissions.enabled: false", reason)

    def test_skip_owner_approval_wins_over_disabled_approval(self):
        """Config rejects this pair; the dataclass does not. If it is built
        anyway, skip is checked first and approves — pinned so nobody expects
        the deny to override it."""
        broker = _make_broker(
            session_role_map={"ses_o": "owner"}, human_approval=False, skip_owner_approval=True,
        )
        self.assertEqual(broker._decide("Write", [""], "owner", "room_o"), ("allow", ""))

