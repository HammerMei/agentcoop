"""Unit tests for OpenCodeBackend (HTTP adapter)."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from gateway.agents.errors import AgentExecutionError, AgentRateLimitedError, AgentUnavailableError
from gateway.agents.opencode import OpenCodeBackend
from gateway.agents.response import AgentEvent, AgentResponse


def _make_backend(**kwargs) -> OpenCodeBackend:
    defaults = dict(command="opencode", new_session_args=[], timeout=120)
    defaults.update(kwargs)
    return OpenCodeBackend(**defaults)


# ── start / stop lifecycle ────────────────────────────────────────────────────


class TestStart(unittest.IsolatedAsyncioTestCase):
    async def test_start_sets_base_url(self):
        """start() sets _base_url after the health check passes."""
        b = _make_backend()
        self.assertIsNone(b._base_url)

        mock_process = MagicMock()
        mock_process.returncode = None
        mock_process.pid = 12345

        mock_health_resp = MagicMock()
        mock_health_resp.status_code = 200

        with (
            patch("asyncio.create_subprocess_exec", return_value=mock_process),
            patch(
                "gateway.agents.opencode.adapter._find_free_port", return_value=54321
            ),
            patch("httpx.AsyncClient") as mock_cls,
        ):
            mock_client = AsyncMock()
            mock_cls.return_value.__aenter__.return_value = mock_client
            mock_client.get = AsyncMock(return_value=mock_health_resp)
            await b.start()

        self.assertEqual(b._base_url, "http://127.0.0.1:54321")
        self.assertIsNotNone(b._process)

    async def test_start_is_idempotent(self):
        """Calling start() a second time is a no-op (does not spawn a new process)."""
        b = _make_backend()
        b._base_url = "http://127.0.0.1:9999"  # simulate already started

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            await b.start()
            mock_exec.assert_not_called()

    async def test_start_raises_if_process_exits_early(self):
        """start() raises RuntimeError if the process exits before health check passes."""
        b = _make_backend()

        mock_process = MagicMock()
        mock_process.returncode = 1  # already exited

        with (
            patch("asyncio.create_subprocess_exec", return_value=mock_process),
            patch(
                "gateway.agents.opencode.adapter._find_free_port", return_value=54321
            ),
            patch("httpx.AsyncClient") as mock_cls,
        ):
            mock_client = AsyncMock()
            mock_cls.return_value.__aenter__.return_value = mock_client
            mock_client.get = AsyncMock(side_effect=ConnectionRefusedError())
            with self.assertRaises(RuntimeError):
                await b.start()

    async def test_wait_for_health_error_message_does_not_leak_host_port(self):
        """_wait_for_health() RuntimeError must not contain the sidecar host:port."""
        b = _make_backend()

        mock_process = MagicMock()
        mock_process.returncode = None
        mock_process.pid = 1

        with (
            patch("asyncio.create_subprocess_exec", return_value=mock_process),
            patch("gateway.agents.opencode.adapter._find_free_port", return_value=54321),
            patch("gateway.agents.opencode.adapter._STARTUP_TIMEOUT", 0.1),
            patch("httpx.AsyncClient") as mock_cls,
        ):
            mock_client = AsyncMock()
            mock_cls.return_value.__aenter__.return_value = mock_client
            # Simulate a ConnectError whose str() contains the internal URL
            mock_client.get = AsyncMock(
                side_effect=httpx.ConnectError(
                    "Connection refused to http://127.0.0.1:54321"
                )
            )
            with self.assertRaises(RuntimeError) as cm:
                await b.start()

        err = str(cm.exception)
        self.assertNotIn("127.0.0.1", err)
        self.assertNotIn("54321", err)

    async def test_start_uses_new_session_args(self):
        """start() appends new_session_args to the opencode serve command."""
        b = _make_backend(new_session_args=["--model", "anthropic/claude-sonnet-4-5"])

        mock_process = MagicMock()
        mock_process.returncode = None
        mock_process.pid = 1

        mock_health_resp = MagicMock()
        mock_health_resp.status_code = 200

        captured_cmd = []

        async def fake_exec(*cmd, **kwargs):
            captured_cmd.extend(cmd)
            return mock_process

        with (
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
            patch("gateway.agents.opencode.adapter._find_free_port", return_value=9000),
            patch("httpx.AsyncClient") as mock_cls,
        ):
            mock_client = AsyncMock()
            mock_cls.return_value.__aenter__.return_value = mock_client
            mock_client.get = AsyncMock(return_value=mock_health_resp)
            await b.start()

        self.assertIn("--model", captured_cmd)
        self.assertIn("anthropic/claude-sonnet-4-5", captured_cmd)

    async def test_start_injects_sidecar_env(self):
        """start() merges sidecar_env into the subprocess environment."""
        b = _make_backend(sidecar_env={"COOP_ROLE": "owner", "MY_VAR": "hello"})

        mock_process = MagicMock()
        mock_process.returncode = None
        mock_process.pid = 1

        mock_health_resp = MagicMock()
        mock_health_resp.status_code = 200

        captured_env = {}

        async def fake_exec(*cmd, env=None, **kwargs):
            captured_env.update(env or {})
            return mock_process

        with (
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
            patch("gateway.agents.opencode.adapter._find_free_port", return_value=9001),
            patch("httpx.AsyncClient") as mock_cls,
        ):
            mock_client = AsyncMock()
            mock_cls.return_value.__aenter__.return_value = mock_client
            mock_client.get = AsyncMock(return_value=mock_health_resp)
            await b.start()

        self.assertEqual(captured_env.get("COOP_ROLE"), "owner")
        self.assertEqual(captured_env.get("MY_VAR"), "hello")


class TestStop(unittest.IsolatedAsyncioTestCase):
    async def test_stop_is_idempotent_when_not_started(self):
        """stop() on a backend that was never started is a no-op."""
        b = _make_backend()
        await b.stop()  # should not raise

    async def test_stop_clears_base_url_and_process(self):
        """stop() resets _base_url and _process to None."""
        b = _make_backend()
        mock_process = MagicMock()
        mock_process.returncode = None
        mock_process.pid = 42
        mock_process.terminate = MagicMock()
        mock_process.wait = AsyncMock()
        b._base_url = "http://127.0.0.1:9999"
        b._process = mock_process

        await b.stop()

        self.assertIsNone(b._base_url)
        self.assertIsNone(b._process)
        mock_process.terminate.assert_called_once()

    async def test_stop_cleans_orphans_before_terminating_process(self):
        """Orphan cleanup must run while the sidecar is still reachable."""
        b = _make_backend()
        order = []

        mock_process = MagicMock()
        mock_process.returncode = None
        mock_process.pid = 42
        mock_process.terminate = MagicMock(
            side_effect=lambda: order.append("terminate")
        )
        mock_process.wait = AsyncMock()
        b._base_url = "http://127.0.0.1:9999"
        b._process = mock_process
        b._client = AsyncMock()

        async def fake_cleanup():
            order.append("cleanup")

        b._cleanup_orphan_sessions_best_effort = fake_cleanup

        await b.stop()

        self.assertEqual(order, ["cleanup", "terminate"])


# ── _require_base_url ─────────────────────────────────────────────────────────


class TestRequireBaseUrl(unittest.TestCase):
    def test_raises_when_not_set(self):
        b = _make_backend()
        with self.assertRaisesRegex(RuntimeError, "no base_url"):
            b._require_base_url()

    def test_no_raise_when_set(self):
        b = _make_backend()
        b._base_url = "http://127.0.0.1:9999"
        b._require_base_url()  # should not raise


# ── ensure_durable_instructions (issue #56 follow-up: real durability) ───────


class TestEnsureDurableInstructions(unittest.IsolatedAsyncioTestCase):
    """OpenCodeBackend writes durable content to a stable per-watcher file and
    returns its path — mirroring ClaudeBackend's contract exactly, replacing
    the old one-time-send-into-history fallback (pre-#52 behavior) which was
    NOT compaction-resistant. send()/stream() read this file's content fresh
    on every call and forward it as the per-request ``system`` field — see
    TestSend/TestStream for that half of the mechanism.
    """

    async def test_writes_file_and_returns_path(self):
        import tempfile
        from pathlib import Path

        backend = _make_backend()
        with tempfile.TemporaryDirectory() as tmp:
            with patch("gateway.agents.opencode.adapter.RUNTIME_DIR", Path(tmp)):
                path = await backend.ensure_durable_instructions(
                    "ses_001", "/unused", 10, "identity header content",
                    path_key="w1", already_delivered=False,
                )
            self.assertEqual(path, str(Path(tmp) / "system-prompts" / "w1.md"))

    async def test_writes_file_content_correctly(self):
        import tempfile
        from pathlib import Path

        backend = _make_backend()
        with tempfile.TemporaryDirectory() as tmp:
            with patch("gateway.agents.opencode.adapter.RUNTIME_DIR", Path(tmp)):
                content = "## Coop Session Identity\nhello world"
                path = await backend.ensure_durable_instructions(
                    "ses_001", "/unused", 10, content,
                    path_key="w1", already_delivered=False,
                )
                self.assertEqual(Path(path).read_text(), content)

    async def test_returns_non_none_path_when_already_delivered_true(self):
        """Resumed session (already_delivered=True) must still get a fresh
        path — writing is idempotent, no side effect to avoid repeating."""
        import tempfile
        from pathlib import Path

        backend = _make_backend()
        with tempfile.TemporaryDirectory() as tmp:
            with patch("gateway.agents.opencode.adapter.RUNTIME_DIR", Path(tmp)):
                path = await backend.ensure_durable_instructions(
                    "ses_001", "/unused", 10, "content",
                    path_key="w1", already_delivered=True,
                )
            self.assertIsNotNone(path)

    async def test_overwrites_existing_file_with_fresh_content(self):
        """A second call with different content overwrites the first."""
        import tempfile
        from pathlib import Path

        backend = _make_backend()
        with tempfile.TemporaryDirectory() as tmp:
            with patch("gateway.agents.opencode.adapter.RUNTIME_DIR", Path(tmp)):
                await backend.ensure_durable_instructions(
                    "ses_001", "/unused", 10, "old content",
                    path_key="w1", already_delivered=False,
                )
                path = await backend.ensure_durable_instructions(
                    "ses_001", "/unused", 10, "new content",
                    path_key="w1", already_delivered=False,
                )
                self.assertEqual(Path(path).read_text(), "new content")

    async def test_different_watchers_get_different_files(self):
        import tempfile
        from pathlib import Path

        backend = _make_backend()
        with tempfile.TemporaryDirectory() as tmp:
            with patch("gateway.agents.opencode.adapter.RUNTIME_DIR", Path(tmp)):
                path_a = await backend.ensure_durable_instructions(
                    "ses_a", "/unused", 10, "content A",
                    path_key="watcher-a", already_delivered=False,
                )
                path_b = await backend.ensure_durable_instructions(
                    "ses_b", "/unused", 10, "content B",
                    path_key="watcher-b", already_delivered=False,
                )
            self.assertNotEqual(path_a, path_b)
            self.assertEqual(Path(path_a).read_text(), "content A")
            self.assertEqual(Path(path_b).read_text(), "content B")


class TestReadDurableInstructions(unittest.IsolatedAsyncioTestCase):
    """_read_durable_instructions() reads back what ensure_durable_instructions()
    wrote — used by send()/stream() to forward content as the ``system`` field."""

    async def test_reads_existing_file(self):
        import tempfile
        from pathlib import Path

        backend = _make_backend()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "w1.md"
            path.write_text("some durable content")
            result = await backend._read_durable_instructions(str(path))
            self.assertEqual(result, "some durable content")

    async def test_missing_file_returns_none_not_raise(self):
        """A turn should proceed without the durable header rather than fail
        outright if the file was somehow deleted between ensure() and send()."""
        backend = _make_backend()
        result = await backend._read_durable_instructions("/nonexistent/path/w1.md")
        self.assertIsNone(result)


# ── create_session ────────────────────────────────────────────────────────────


class TestCreateSession(unittest.IsolatedAsyncioTestCase):
    async def test_raises_without_base_url(self):
        # After the shutdown-race fix, calling create_session() on a backend
        # that was never started raises AgentUnavailableError (the sidecar is
        # unavailable) rather than RuntimeError("call start() before...").
        # This is the correct caller-facing error: callers should handle
        # "unavailable" gracefully rather than treating it as a programming error.
        from gateway.agents.errors import AgentUnavailableError
        b = _make_backend()
        with self.assertRaises(AgentUnavailableError):
            await b.create_session("/tmp")

    async def test_posts_to_session_endpoint_and_returns_id(self):
        b = _make_backend()
        b._base_url = "http://127.0.0.1:57000"

        mock_session_resp = MagicMock()
        mock_session_resp.raise_for_status = MagicMock()
        mock_session_resp.json.return_value = {"id": "ses_abcdef"}

        mock_msg_resp = MagicMock()
        mock_msg_resp.raise_for_status = MagicMock()
        mock_msg_resp.json.return_value = {"parts": []}

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=[mock_session_resp, mock_msg_resp])
        b._client = mock_client
        session_id = await b.create_session("/workspace")

        self.assertEqual(session_id, "ses_abcdef")
        # First call: POST /session
        first_call = mock_client.post.call_args_list[0]
        self.assertEqual(first_call.args[0], "http://127.0.0.1:57000/session")
        self.assertEqual(first_call.kwargs["json"]["directory"], "/workspace")
        # Second call: init prompt
        second_call = mock_client.post.call_args_list[1]
        self.assertIn("/message", second_call.args[0])

    async def test_raises_if_no_session_id_returned(self):
        b = _make_backend()
        b._base_url = "http://127.0.0.1:57000"

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {}  # no "id" field

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        b._client = mock_client
        with self.assertRaisesRegex(RuntimeError, "no session id"):
            await b.create_session("/workspace")

    async def test_passes_title_when_provided(self):
        b = _make_backend()
        b._base_url = "http://127.0.0.1:57000"

        mock_session_resp = MagicMock()
        mock_session_resp.raise_for_status = MagicMock()
        mock_session_resp.json.return_value = {"id": "ses_123"}

        mock_msg_resp = MagicMock()
        mock_msg_resp.raise_for_status = MagicMock()
        mock_msg_resp.json.return_value = {"parts": []}

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=[mock_session_resp, mock_msg_resp])
        b._client = mock_client
        await b.create_session("/workspace", session_title="My Session")

        first_call = mock_client.post.call_args_list[0]
        self.assertEqual(first_call.kwargs["json"].get("title"), "My Session")

    async def test_init_failure_triggers_best_effort_cleanup(self):
        """If init prompt fails, the adapter attempts to delete the created session."""
        b = _make_backend()
        b._base_url = "http://127.0.0.1:57000"

        mock_session_resp = MagicMock()
        mock_session_resp.raise_for_status = MagicMock()
        mock_session_resp.json.return_value = {"id": "ses_cleanup"}

        mock_delete_resp = MagicMock()
        mock_delete_resp.status_code = 204

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(
            side_effect=[mock_session_resp, RuntimeError("init failed")]
        )
        mock_client.delete = AsyncMock(return_value=mock_delete_resp)
        b._client = mock_client

        with self.assertRaisesRegex(RuntimeError, "init failed"):
            await b.create_session("/workspace")

        mock_client.delete.assert_awaited_once_with(
            "http://127.0.0.1:57000/session/ses_cleanup"
        )
        self.assertNotIn("ses_cleanup", b._orphan_session_ids)

    async def test_init_failure_marks_orphan_when_cleanup_fails(self):
        """If cleanup is unavailable, the created session is tracked as an orphan."""
        b = _make_backend()
        b._base_url = "http://127.0.0.1:57000"

        mock_session_resp = MagicMock()
        mock_session_resp.raise_for_status = MagicMock()
        mock_session_resp.json.return_value = {"id": "ses_orphan"}

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(
            side_effect=[mock_session_resp, RuntimeError("init failed")]
        )
        mock_client.delete = AsyncMock(
            side_effect=httpx.ConnectError("delete unavailable")
        )
        b._client = mock_client

        with self.assertRaisesRegex(RuntimeError, "init failed"):
            await b.create_session("/workspace")

        self.assertIn("ses_orphan", b._orphan_session_ids)


# ── send ──────────────────────────────────────────────────────────────────────


class TestSend(unittest.IsolatedAsyncioTestCase):
    def _make_http_response(self, parts: list, duration_ms: int = 1500) -> dict:
        return {"parts": parts, "info": {"duration": duration_ms}}

    async def test_append_system_prompt_file_forwarded_as_system_field(self):
        """send() reads append_system_prompt_file's *content* fresh and
        forwards it as the per-request ``system`` field — this is how
        durable instructions survive OpenCode's own compaction (see
        ensure_durable_instructions())."""
        import tempfile
        from pathlib import Path

        b = _make_backend()
        b._base_url = "http://127.0.0.1:57000"
        b._ever_started = True

        response_data = self._make_http_response(
            [{"type": "text", "text": "Hello world"}]
        )
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = response_data

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        b._client = mock_client

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "w.md"
            path.write_text("## Coop Session Identity\nyou are watcher w")

            result = await b.send(
                "ses_abc", "Hello", "/workspace", timeout=60,
                append_system_prompt_file=str(path),
            )

        self.assertIsInstance(result, AgentResponse)
        body = mock_client.post.call_args.kwargs["json"]
        self.assertEqual(body["system"], "## Coop Session Identity\nyou are watcher w")

    async def test_missing_append_system_prompt_file_omits_system_field(self):
        """If the durable-instructions file is missing, send() still proceeds
        (no crash) and simply omits the ``system`` field rather than sending
        an empty/broken one."""
        b = _make_backend()
        b._base_url = "http://127.0.0.1:57000"
        b._ever_started = True

        response_data = self._make_http_response(
            [{"type": "text", "text": "Hello world"}]
        )
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = response_data

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        b._client = mock_client

        result = await b.send(
            "ses_abc", "Hello", "/workspace", timeout=60,
            append_system_prompt_file="/nonexistent/w.md",
        )

        self.assertIsInstance(result, AgentResponse)
        body = mock_client.post.call_args.kwargs["json"]
        self.assertNotIn("system", body)

    async def test_no_append_system_prompt_file_omits_system_field(self):
        """The common case (no durable instructions configured at all) must
        not add a ``system`` key to the request body."""
        b = _make_backend()
        b._base_url = "http://127.0.0.1:57000"
        b._ever_started = True

        response_data = self._make_http_response(
            [{"type": "text", "text": "Hello world"}]
        )
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = response_data

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        b._client = mock_client

        await b.send("ses_abc", "Hello", "/workspace", timeout=60)

        body = mock_client.post.call_args.kwargs["json"]
        self.assertNotIn("system", body)

    async def test_basic_send_returns_text(self):
        b = _make_backend()
        b._base_url = "http://127.0.0.1:57000"
        b._ever_started = True

        response_data = self._make_http_response(
            [
                {"type": "text", "text": "Hello world"},
            ]
        )
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = response_data

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        b._client = mock_client
        result = await b.send("ses_abc", "Hello", "/workspace", timeout=60)

        self.assertIsInstance(result, AgentResponse)
        self.assertEqual(result.text, "Hello world")
        self.assertEqual(result.session_id, "ses_abc")

    async def test_send_classifies_http_429_as_rate_limited(self):
        b = _make_backend()
        b._base_url = "http://127.0.0.1:57000"
        b._ever_started = True

        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "Too Many Requests",
                request=MagicMock(),
                response=mock_resp,
            )
        )
        b._client = mock_client

        with self.assertRaises(AgentRateLimitedError):
            await b.send("ses_rate", "Hi", "/workspace", timeout=60)

    async def test_404_propagates_as_sanitized_error(self):
        """send() must NOT silently create a new session on 404.

        The original httpx.HTTPStatusError must be caught and re-raised as a
        RuntimeError that does NOT include the raw URL or response body —
        those may contain internal server details unsuitable for end users.
        """
        b = _make_backend()
        b._base_url = "http://127.0.0.1:57000"
        b._ever_started = True

        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_client.post = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "Not Found",
                request=MagicMock(),
                response=mock_resp,
            )
        )
        b._client = mock_client
        with self.assertRaises(RuntimeError) as ctx:
            await b.send("ses_missing", "Hi", "/workspace", timeout=60)
        # Must mention status code but must NOT include raw URL/response body
        self.assertIn("404", str(ctx.exception))
        self.assertNotIn("127.0.0.1", str(ctx.exception))
        # Underlying httpx exception must NOT be chained (__cause__ should be None)
        self.assertIsNone(ctx.exception.__cause__)

    async def test_send_restarts_dead_sidecar_before_request(self):
        """If the sidecar died after startup, send() should restart it before use."""
        b = _make_backend()
        b._base_url = "http://127.0.0.1:57000"
        b._ever_started = True

        dead_process = MagicMock()
        dead_process.returncode = 7
        b._process = dead_process
        b._client = AsyncMock()

        new_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "parts": [{"type": "text", "text": "recovered"}],
            "info": {},
        }
        new_client.post = AsyncMock(return_value=mock_resp)

        async def fake_start():
            b._base_url = "http://127.0.0.1:58000"
            b._client = new_client
            b._process = MagicMock(returncode=None)
            b._ever_started = True

        b._start_inner = AsyncMock(side_effect=fake_start)

        result = await b.send("ses_abc", "Hello", "/workspace", timeout=60)

        b._start_inner.assert_awaited_once()
        self.assertEqual(result.text, "recovered")
        self.assertEqual(
            new_client.post.await_args.kwargs["json"]["parts"][0]["text"], "Hello"
        )


# ── _parse_http_response ──────────────────────────────────────────────────────


class TestParseHttpResponse(unittest.TestCase):
    def _backend(self) -> OpenCodeBackend:
        b = _make_backend()
        b._base_url = "http://127.0.0.1:57000"
        return b

    def test_extracts_text_parts(self):
        b = self._backend()
        data = {
            "parts": [
                {"type": "text", "text": "Hello "},
                {"type": "text", "text": "world"},
            ],
            "info": {},
        }
        r = b._parse_http_response(data, "ses_1")
        self.assertEqual(r.text, "Hello world")

    def test_extracts_token_usage_from_step_finish(self):
        b = self._backend()
        data = {
            "parts": [
                {"type": "text", "text": "Done"},
                {
                    "type": "step-finish",
                    "tokens": {
                        "input": 100,
                        "output": 50,
                        "reasoning": 10,
                        "cache": {"read": 20, "write": 5},
                    },
                    "cost": 0.002,
                },
            ],
            "info": {"duration": 3000},
        }
        r = b._parse_http_response(data, "ses_1")
        self.assertIsNotNone(r.usage)
        self.assertEqual(r.usage.input_tokens, 100)
        self.assertEqual(r.usage.output_tokens, 50)
        self.assertEqual(r.usage.reasoning_tokens, 10)
        self.assertEqual(r.usage.cache_read_tokens, 20)
        self.assertEqual(r.usage.cache_write_tokens, 5)
        self.assertAlmostEqual(r.cost_usd, 0.002)
        self.assertEqual(r.duration_ms, 3000)
        self.assertEqual(r.num_turns, 1)

    def test_aggregates_multiple_step_finish_parts(self):
        b = self._backend()
        data = {
            "parts": [
                {"type": "text", "text": "Part 1 "},
                {
                    "type": "step-finish",
                    "tokens": {"input": 40, "output": 20, "reasoning": 0, "cache": {}},
                    "cost": 0.001,
                },
                {"type": "text", "text": "Part 2"},
                {
                    "type": "step-finish",
                    "tokens": {"input": 60, "output": 30, "reasoning": 5, "cache": {}},
                    "cost": 0.002,
                },
            ],
            "info": {},
        }
        r = b._parse_http_response(data, "ses_1")
        self.assertEqual(r.text, "Part 1 Part 2")
        self.assertEqual(r.usage.input_tokens, 100)
        self.assertEqual(r.usage.output_tokens, 50)
        self.assertAlmostEqual(r.cost_usd, 0.003)
        self.assertEqual(r.num_turns, 2)

    def test_empty_response_returns_placeholder_with_is_error(self):
        """Empty responses must set is_error=True so context_injector can detect them."""
        b = self._backend()
        data = {"parts": [], "info": {}}
        r = b._parse_http_response(data, "ses_1")
        self.assertEqual(r.text, "(empty response)")
        self.assertTrue(r.is_error)

    def test_no_usage_when_no_step_finish(self):
        b = self._backend()
        data = {
            "parts": [{"type": "text", "text": "Hi"}],
            "info": {},
        }
        r = b._parse_http_response(data, "ses_1")
        self.assertIsNone(r.usage)
        self.assertIsNone(r.cost_usd)

    def test_step_finish_uses_hyphen_not_underscore(self):
        """Regression: ensure 'step-finish' (not 'step_finish') is recognised."""
        b = self._backend()
        data = {
            "parts": [
                {"type": "text", "text": "ok"},
                # underscore variant — must NOT be counted
                {
                    "type": "step_finish",
                    "tokens": {
                        "input": 999,
                        "output": 999,
                        "reasoning": 0,
                        "cache": {},
                    },
                    "cost": 9.99,
                },
            ],
            "info": {},
        }
        r = b._parse_http_response(data, "ses_1")
        self.assertIsNone(r.usage)  # step_finish ignored
        self.assertIsNone(r.num_turns)

    def test_duration_from_info(self):
        b = self._backend()
        data = {"parts": [{"type": "text", "text": "ok"}], "info": {"duration": 2500}}
        r = b._parse_http_response(data, "ses_1")
        self.assertEqual(r.duration_ms, 2500)

    def test_duration_none_when_missing(self):
        b = self._backend()
        data = {"parts": [{"type": "text", "text": "ok"}], "info": {}}
        r = b._parse_http_response(data, "ses_1")
        self.assertIsNone(r.duration_ms)

    def test_successful_response_is_not_error(self):
        """Normal text responses must have is_error=False."""
        b = self._backend()
        data = {"parts": [{"type": "text", "text": "Hello"}], "info": {}}
        r = b._parse_http_response(data, "ses_1")
        self.assertFalse(r.is_error)

    def test_step_finish_cache_null_does_not_crash(self):
        """Explicit JSON null for 'cache' field must not raise AttributeError."""
        b = self._backend()
        data = {
            "parts": [
                {"type": "text", "text": "done"},
                {
                    "type": "step-finish",
                    "tokens": {"input": 10, "output": 5, "reasoning": 0, "cache": None},
                    "cost": 0.001,
                },
            ],
            "info": {},
        }
        r = b._parse_http_response(data, "ses_1")
        # Must not raise; cache tokens default to 0
        self.assertEqual(r.usage.cache_read_tokens, 0)
        self.assertEqual(r.usage.cache_write_tokens, 0)

    def test_reasoning_only_produces_usage_object(self):
        """A turn with only reasoning tokens still produces a non-None usage object."""
        b = self._backend()
        data = {
            "parts": [
                {
                    "type": "step-finish",
                    "tokens": {"input": 0, "output": 0, "reasoning": 100, "cache": {}},
                    "cost": 0.0,
                },
            ],
            "info": {},
        }
        r = b._parse_http_response(data, "ses_1")
        self.assertIsNotNone(r.usage, "usage must not be None for reasoning-only turns")
        self.assertEqual(r.usage.reasoning_tokens, 100)

    def test_cache_only_turn_produces_usage_object(self):
        """A turn with only cache tokens (zero input/output/reasoning) produces a non-None usage object."""
        b = self._backend()
        data = {
            "parts": [
                {
                    "type": "step-finish",
                    "tokens": {"input": 0, "output": 0, "reasoning": 0,
                               "cache": {"read": 500, "write": 0}},
                    "cost": 0.0,
                },
            ],
            "info": {},
        }
        r = b._parse_http_response(data, "ses_1")
        self.assertIsNotNone(r.usage, "usage must not be None for cache-only turns")
        self.assertEqual(r.usage.cache_read_tokens, 500)

    def test_step_finish_tokens_null_does_not_crash(self):
        """Explicit JSON null for 'tokens' in HTTP step-finish must not crash."""
        b = self._backend()
        data = {
            "parts": [
                {"type": "text", "text": "done"},
                {"type": "step-finish", "tokens": None, "cost": 0.001},
            ],
            "info": {},
        }
        r = b._parse_http_response(data, "ses_1")
        # tokens null → all token counts default to 0 → no usage object
        self.assertIsNone(r.usage)

    def test_step_finish_cost_null_does_not_crash(self):
        """Explicit JSON null for 'cost' in HTTP step-finish must not crash."""
        b = self._backend()
        data = {
            "parts": [
                {"type": "text", "text": "done"},
                {
                    "type": "step-finish",
                    "tokens": {"input": 5, "output": 5, "reasoning": 0, "cache": {}},
                    "cost": None,
                },
            ],
            "info": {},
        }
        r = b._parse_http_response(data, "ses_1")
        # cost null → defaults to 0.0 → cost_usd is None (not positive)
        self.assertIsNone(r.cost_usd)

    def test_null_parts_does_not_crash(self):
        """Explicit JSON null for top-level 'parts' must not raise TypeError."""
        b = self._backend()
        data = {"parts": None, "info": {}}
        r = b._parse_http_response(data, "ses_1")
        self.assertEqual(r.text, "(empty response)")
        self.assertTrue(r.is_error)

    def test_null_info_does_not_crash(self):
        """Explicit JSON null for top-level 'info' must not raise AttributeError."""
        b = self._backend()
        data = {"parts": [{"type": "text", "text": "hello"}], "info": None}
        r = b._parse_http_response(data, "ses_1")
        self.assertEqual(r.text, "hello")
        self.assertIsNone(r.duration_ms)

    def test_null_element_in_parts_list_is_skipped(self):
        """null or non-dict elements inside parts list must be skipped without crashing."""
        b = self._backend()
        data = {
            "parts": [
                None,
                "unexpected string",
                {"type": "text", "text": "valid"},
                42,
            ],
            "info": {},
        }
        r = b._parse_http_response(data, "ses_1")
        self.assertEqual(r.text, "valid")

    def test_duration_zero_not_coerced_to_none(self):
        """info.duration == 0 must be returned as 0, not None."""
        b = self._backend()
        data = {"parts": [{"type": "text", "text": "ok"}], "info": {"duration": 0}}
        r = b._parse_http_response(data, "ses_1")
        self.assertEqual(r.duration_ms, 0)


# ── _post_message error sanitization (Issue 12.4) ─────────────────────────────


class TestPostMessageErrorSanitization(unittest.IsolatedAsyncioTestCase):
    def _backend(self) -> OpenCodeBackend:
        b = _make_backend()
        b._base_url = "http://127.0.0.1:57000"
        return b

    async def test_http_status_error_is_sanitized(self):
        """HTTPStatusError must be re-raised as RuntimeError without URL or body."""
        b = self._backend()
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "Internal Server Error",
                request=MagicMock(),
                response=mock_resp,
            )
        )
        b._client = mock_client
        with self.assertRaises(RuntimeError) as ctx:
            await b._post_message("ses_abc", "hello")
        # Status code must be mentioned
        self.assertIn("500", str(ctx.exception))
        # Raw URL and host must NOT leak into the message
        self.assertNotIn("127.0.0.1", str(ctx.exception))
        # Must not chain the original exception (__cause__ should be None due to `from None`)
        self.assertIsNone(ctx.exception.__cause__)

    async def test_500_on_send_raises_runtime_error(self):
        """send() propagates sanitized RuntimeError for 5xx responses."""
        b = self._backend()
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "Service Unavailable",
                request=MagicMock(),
                response=mock_resp,
            )
        )
        b._client = mock_client
        with self.assertRaises(RuntimeError) as ctx:
            await b.send("ses_abc", "hello", "/workspace", timeout=30)
        self.assertIn("503", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()


# ── Appended from test_round4_fixes.py ────────────────────────────────────────


class TestEnsureLiveRuntimeAfterFailedRestart(unittest.IsolatedAsyncioTestCase):
    """_ensure_live_runtime() must auto-recover even when _process is None after cleanup."""

    def _make_backend(self):
        """Return a minimal OpenCodeBackend with mocked internals."""
        from gateway.agents.opencode.adapter import OpenCodeBackend
        b = OpenCodeBackend.__new__(OpenCodeBackend)
        b._command = "opencode"
        b._new_session_args = []
        b.timeout = 10
        b._sidecar_env = {}
        b._sidecar_cwd = None
        b._broker_config = None
        b._base_url = None
        b._process = None
        b._stdout_drain = None
        b._stderr_drain = None
        b._client = None
        b._orphan_session_ids = set()
        b._restart_lock = asyncio.Lock()
        b._ever_started = False
        b._consecutive_restart_failures = 0
        return b

    async def test_ever_started_flag_set_on_successful_start(self):
        """_ever_started must be True after start() succeeds."""
        b = self._make_backend()
        self.assertFalse(b._ever_started)

        with (
            patch("gateway.agents.opencode.adapter._find_free_port", return_value=19999),
            patch("asyncio.create_subprocess_exec") as mock_exec,
            patch.object(b, "_wait_for_health", new_callable=AsyncMock),
            patch("httpx.AsyncClient"),
        ):
            mock_proc = MagicMock()
            mock_proc.stdout = MagicMock()
            mock_proc.stderr = MagicMock()
            mock_exec.return_value = mock_proc

            with patch("asyncio.create_task", return_value=MagicMock()):
                await b.start()

        self.assertTrue(b._ever_started)

    async def test_ever_started_false_allows_require_base_url_to_raise(self):
        """Before start(), _ever_started=False, so _ensure_live_runtime skips restart
        and _require_base_url() raises — correct behavior."""
        b = self._make_backend()
        # _ever_started=False, _base_url=None → raises AgentUnavailableError
        with self.assertRaises(AgentUnavailableError):
            await b._ensure_live_runtime()

    async def test_ever_started_true_triggers_restart_when_base_url_gone(self):
        """After a failed restart (_process=None, _base_url=None, _ever_started=True),
        _ensure_live_runtime() must attempt another restart."""
        b = self._make_backend()
        b._ever_started = True  # simulate post-failed-restart state

        start_called = []

        async def fake_start():
            start_called.append(True)
            b._base_url = "http://localhost:9999"
            b._client = MagicMock()

        with patch.object(b, "_invalidate_dead_runtime", new_callable=AsyncMock):
            with patch.object(b, "_start_inner", side_effect=fake_start):
                await b._ensure_live_runtime()

        self.assertTrue(start_called, "_ensure_live_runtime() must call _start_inner() to recover")
        self.assertIsNotNone(b._base_url)

    async def test_dead_process_still_triggers_restart(self):
        """Existing behavior preserved: dead process (returncode != None) triggers restart."""
        b = self._make_backend()
        b._ever_started = True
        b._base_url = "http://localhost:9999"
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        b._process = mock_proc

        start_called = []

        async def fake_start():
            start_called.append(True)
            b._base_url = "http://localhost:10000"
            b._process = MagicMock()
            b._process.returncode = None
            b._client = MagicMock()

        with patch.object(b, "_invalidate_dead_runtime", new_callable=AsyncMock):
            with patch.object(b, "_start_inner", side_effect=fake_start):
                await b._ensure_live_runtime()

        self.assertTrue(start_called)


# ── Appended from test_round6_fixes.py ────────────────────────────────────────


class TestCircuitBreakerFastFail(unittest.IsolatedAsyncioTestCase):
    """_ensure_live_runtime() must fast-fail after _MAX_RESTART_FAILURES failures."""

    def _make_backend(self):
        from gateway.agents.opencode.adapter import OpenCodeBackend
        b = OpenCodeBackend.__new__(OpenCodeBackend)
        b._command = "opencode"
        b._new_session_args = []
        b.timeout = 10
        b._sidecar_env = {}
        b._sidecar_cwd = None
        b._broker_config = None
        b._base_url = None
        b._process = None
        b._stdout_drain = None
        b._stderr_drain = None
        b._client = None
        b._orphan_session_ids = set()
        b._restart_lock = asyncio.Lock()
        b._ever_started = True
        b._consecutive_restart_failures = 0
        return b

    async def test_fast_fail_after_max_failures(self):
        """When consecutive failures >= _MAX_RESTART_FAILURES, raise immediately."""
        from gateway.agents.errors import AgentUnavailableError
        from gateway.agents.opencode.adapter import _MAX_RESTART_FAILURES
        b = self._make_backend()
        b._process = MagicMock()
        b._process.returncode = 1
        b._consecutive_restart_failures = _MAX_RESTART_FAILURES

        with self.assertRaises(AgentUnavailableError) as ctx:
            await b._ensure_live_runtime()

        self.assertIn("consecutive failed restart", str(ctx.exception))

    async def test_no_fast_fail_below_threshold(self):
        """Below threshold, the restart should still be attempted."""
        from gateway.agents.opencode.adapter import _MAX_RESTART_FAILURES
        b = self._make_backend()
        b._process = MagicMock()
        b._process.returncode = 1
        b._consecutive_restart_failures = _MAX_RESTART_FAILURES - 1

        restart_called = []

        async def fake_start():
            restart_called.append(True)
            b._base_url = "http://localhost:9999"
            b._process = MagicMock()
            b._process.returncode = None
            b._client = MagicMock()

        with patch.object(b, "_invalidate_dead_runtime", new_callable=AsyncMock):
            with patch.object(b, "_start_inner", side_effect=fake_start):
                await b._ensure_live_runtime()

        self.assertTrue(restart_called, "start() must be called when below threshold")

    async def test_failure_counter_increments_on_restart_error(self):
        """A failed restart must increment _consecutive_restart_failures."""
        b = self._make_backend()
        b._process = MagicMock()
        b._process.returncode = 1
        initial_failures = 0
        b._consecutive_restart_failures = initial_failures

        with patch.object(b, "_invalidate_dead_runtime", new_callable=AsyncMock):
            with patch.object(b, "_start_inner", side_effect=RuntimeError("health timeout")):
                with self.assertRaises(RuntimeError):
                    await b._ensure_live_runtime()

        self.assertEqual(b._consecutive_restart_failures, initial_failures + 1)

    async def test_failure_counter_reset_on_successful_restart(self):
        """A successful restart must reset _consecutive_restart_failures to 0."""
        from gateway.agents.opencode.adapter import _MAX_RESTART_FAILURES
        b = self._make_backend()
        b._process = MagicMock()
        b._process.returncode = 1
        b._consecutive_restart_failures = _MAX_RESTART_FAILURES - 1

        async def fake_start():
            b._base_url = "http://localhost:9999"
            b._process = MagicMock()
            b._process.returncode = None
            b._client = MagicMock()

        with patch.object(b, "_invalidate_dead_runtime", new_callable=AsyncMock):
            with patch.object(b, "_start_inner", side_effect=fake_start):
                await b._ensure_live_runtime()

        self.assertEqual(b._consecutive_restart_failures, 0)

    async def test_stop_resets_failure_counter(self):
        """Explicit stop() must clear the circuit-breaker counter."""
        from gateway.agents.opencode.adapter import _MAX_RESTART_FAILURES
        b = self._make_backend()
        b._consecutive_restart_failures = _MAX_RESTART_FAILURES

        # Build enough state for stop() to enter the non-trivial path:
        # _process must be non-None so it doesn't return via the fast path.
        mock_proc = AsyncMock()
        mock_proc.pid = 12345
        mock_proc.terminate = MagicMock()
        mock_proc.wait = AsyncMock(return_value=0)
        b._process = mock_proc
        b._client = AsyncMock(spec=httpx.AsyncClient)
        b._ever_started = True

        with patch.object(b, "_cleanup_orphan_sessions_best_effort", new_callable=AsyncMock):
            await b.stop()

        # stop() must have zeroed the counter in its finally block
        self.assertEqual(b._consecutive_restart_failures, 0)

    async def test_max_restart_failures_constant_at_least_2(self):
        """_MAX_RESTART_FAILURES should be >= 2 to allow at least one retry."""
        from gateway.agents.opencode.adapter import _MAX_RESTART_FAILURES
        self.assertGreaterEqual(_MAX_RESTART_FAILURES, 2)

    async def test_circuit_breaker_checked_inside_lock(self):
        """Counter incremented by a concurrent coroutine must be seen inside the lock."""
        from gateway.agents.opencode.adapter import _MAX_RESTART_FAILURES
        b = self._make_backend()
        b._process = MagicMock()
        b._process.returncode = 1
        b._consecutive_restart_failures = _MAX_RESTART_FAILURES - 1

        async def fail_start():
            b._consecutive_restart_failures = _MAX_RESTART_FAILURES
            raise RuntimeError("crash")

        with patch.object(b, "_invalidate_dead_runtime", new_callable=AsyncMock):
            with patch.object(b, "_start_inner", side_effect=fail_start):
                with self.assertRaises(RuntimeError):
                    await b._ensure_live_runtime()

        self.assertEqual(b._consecutive_restart_failures, _MAX_RESTART_FAILURES + 1)


# ── Appended from test_round9_fixes.py ────────────────────────────────────────


class TestOpenCodeStartLocking(unittest.IsolatedAsyncioTestCase):
    """Concurrent start() calls must only spawn one opencode serve process."""

    async def test_concurrent_start_spawns_only_one_process(self):
        """Two concurrent start() calls must result in at most one process spawned."""
        from gateway.agents.opencode.adapter import OpenCodeBackend

        backend = OpenCodeBackend.__new__(OpenCodeBackend)
        backend._base_url = None
        backend._command = "opencode"
        backend._new_session_args = []
        backend._sidecar_env = {}
        backend._sidecar_cwd = None
        backend._restart_lock = asyncio.Lock()
        backend._process = None
        backend._stdout_drain = None
        backend._stderr_drain = None
        backend._client = None
        backend._ever_started = False
        backend.timeout = 30

        spawn_count = 0

        async def fake_create_subprocess(*args, **kwargs):
            nonlocal spawn_count
            spawn_count += 1
            proc = MagicMock()
            proc.stdout = MagicMock()
            proc.stderr = MagicMock()
            return proc

        async def fake_drain_pipe(*args, **kwargs):
            pass

        async def fake_health(base_url):
            await asyncio.sleep(0)

        with (
            patch("asyncio.create_subprocess_exec", side_effect=fake_create_subprocess),
            patch.object(backend, "_drain_pipe", side_effect=fake_drain_pipe),
            patch.object(backend, "_wait_for_health", side_effect=fake_health),
            patch("httpx.AsyncClient", return_value=MagicMock()),
        ):
            await asyncio.gather(backend.start(), backend.start())

        self.assertEqual(
            spawn_count,
            1,
            f"Only one process should be spawned; got {spawn_count}",
        )

    async def test_second_start_returns_immediately_when_running(self):
        """start() must return immediately if _base_url is already set."""
        from gateway.agents.opencode.adapter import OpenCodeBackend

        backend = OpenCodeBackend.__new__(OpenCodeBackend)
        backend._base_url = "http://127.0.0.1:12345"
        backend._restart_lock = asyncio.Lock()

        spawn_count = 0

        async def fake_create_subprocess(*args, **kwargs):
            nonlocal spawn_count
            spawn_count += 1
            return MagicMock()

        with patch("asyncio.create_subprocess_exec", side_effect=fake_create_subprocess):
            await backend.start()

        self.assertEqual(spawn_count, 0, "No process should be spawned when already running")


# ── Appended from test_round10_fixes.py ───────────────────────────────────────


def _make_opencode_backend_r10():
    from gateway.agents.opencode.adapter import OpenCodeBackend

    backend = OpenCodeBackend.__new__(OpenCodeBackend)
    backend._command = "opencode"
    backend._new_session_args = []
    backend._sidecar_env = {}
    backend._sidecar_cwd = None
    backend._broker_config = None
    backend._base_url = None
    backend._process = None
    backend._stdout_drain = None
    backend._stderr_drain = None
    backend._client = None
    backend._ever_started = False
    backend._consecutive_restart_failures = 0
    backend._orphan_session_ids = set()
    backend.timeout = 30
    backend._restart_lock = asyncio.Lock()
    return backend


class TestOpenCodeStartInner(unittest.IsolatedAsyncioTestCase):
    """_start_inner() must not try to acquire _restart_lock (no deadlock)."""

    async def test_start_inner_does_not_acquire_lock(self):
        """Calling _start_inner() while holding _restart_lock must not deadlock."""
        backend = _make_opencode_backend_r10()

        proc = MagicMock()
        proc.stdout = MagicMock()
        proc.stderr = MagicMock()

        async def fake_drain(*args, **kwargs):
            pass

        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            patch.object(backend, "_drain_pipe", side_effect=fake_drain),
            patch.object(backend, "_wait_for_health", new_callable=AsyncMock),
            patch("httpx.AsyncClient", return_value=MagicMock()),
        ):
            async with backend._restart_lock:
                await backend._start_inner()

        self.assertIsNotNone(backend._base_url)
        self.assertTrue(backend._ever_started)

    async def test_start_calls_start_inner_via_lock(self):
        """start() must acquire the lock and delegate to _start_inner()."""
        backend = _make_opencode_backend_r10()
        start_inner_called = []

        async def fake_start_inner():
            start_inner_called.append(True)
            backend._base_url = "http://127.0.0.1:9999"

        with patch.object(backend, "_start_inner", side_effect=fake_start_inner):
            await backend.start()

        self.assertEqual(len(start_inner_called), 1)
        self.assertEqual(backend._base_url, "http://127.0.0.1:9999")

    async def test_ensure_live_runtime_calls_start_inner_not_start(self):
        """_ensure_live_runtime() must call _start_inner(), not start()."""
        backend = _make_opencode_backend_r10()
        backend._ever_started = True
        backend._base_url = None

        start_inner_calls = []
        start_calls = []

        async def fake_start_inner():
            start_inner_calls.append(True)
            backend._base_url = "http://127.0.0.1:9999"

        async def fake_start():
            start_calls.append(True)

        with (
            patch.object(backend, "_start_inner", side_effect=fake_start_inner),
            patch.object(backend, "start", side_effect=fake_start),
            patch.object(backend, "_invalidate_dead_runtime", new_callable=AsyncMock),
        ):
            await backend._ensure_live_runtime()

        self.assertEqual(len(start_inner_calls), 1, "_start_inner should be called once")
        self.assertEqual(len(start_calls), 0, "start() must NOT be called by _ensure_live_runtime")


class TestOpenCodeStopLock(unittest.IsolatedAsyncioTestCase):
    """stop() must hold _restart_lock to prevent races with _ensure_live_runtime()."""

    async def test_stop_is_serialized_with_ensure_live_runtime(self):
        """stop() and _ensure_live_runtime() must not run concurrently."""
        backend = _make_opencode_backend_r10()

        proc = MagicMock()
        proc.pid = 999
        proc.returncode = None
        proc.terminate = MagicMock()
        proc.wait = AsyncMock(return_value=0)
        backend._process = proc
        backend._base_url = "http://127.0.0.1:9999"
        backend._ever_started = True

        client_mock = MagicMock()
        client_mock.aclose = AsyncMock()
        backend._client = client_mock

        stop_held_lock = []
        ensure_ran_concurrently = []

        async def patched_stop():
            async with backend._restart_lock:
                stop_held_lock.append(True)
                await asyncio.sleep(0)
                backend._base_url = None
                backend._process = None
                backend._client = None
                stop_held_lock.append(False)

        async def check_lock_is_held():
            if not backend._restart_lock.locked():
                ensure_ran_concurrently.append("lock not held!")
            else:
                ensure_ran_concurrently.append("correctly blocked")

        with patch.object(backend, "_cleanup_orphan_sessions_best_effort", new_callable=AsyncMock):
            await asyncio.gather(
                patched_stop(),
                check_lock_is_held(),
            )

        self.assertIn("correctly blocked", ensure_ran_concurrently)

    async def test_stop_clears_state_atomically(self):
        """stop() must clear _base_url, _process, _client inside the lock."""
        backend = _make_opencode_backend_r10()

        proc = MagicMock()
        proc.pid = 888
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        proc.wait = AsyncMock(return_value=0)
        backend._process = proc
        backend._base_url = "http://127.0.0.1:8888"
        backend._ever_started = True

        client = MagicMock()
        client.aclose = AsyncMock()
        backend._client = client

        with patch.object(backend, "_cleanup_orphan_sessions_best_effort", new_callable=AsyncMock):
            await backend.stop()

        self.assertIsNone(backend._base_url)
        self.assertIsNone(backend._process)
        self.assertIsNone(backend._client)
        self.assertFalse(backend._ever_started)
        client.aclose.assert_called_once()


# ── Appended from test_round12_fixes.py ───────────────────────────────────────


class TestEnsureLiveRuntimeStopRace(unittest.IsolatedAsyncioTestCase):
    """_ensure_live_runtime must raise AgentUnavailableError (not RuntimeError)
    when a concurrent stop() raced ahead and cleared _ever_started.
    """

    def _make_backend(self):
        from gateway.agents.opencode.adapter import OpenCodeBackend

        b = OpenCodeBackend.__new__(OpenCodeBackend)
        b._base_url = None
        b._process = None
        b._client = None
        b._ever_started = False
        b._consecutive_restart_failures = 0
        b._restart_lock = asyncio.Lock()
        b._stdout_drain = None
        b._stderr_drain = None
        return b

    async def test_agent_unavailable_when_stop_cleared_ever_started(self):
        """Simulates the race: needs_restart was True but stop() ran first."""
        from gateway.agents.errors import AgentUnavailableError

        b = self._make_backend()
        b._ever_started = False
        b._base_url = None
        b._process = None

        with self.assertRaises(AgentUnavailableError) as cm:
            await b._ensure_live_runtime()

        self.assertIn("stopped", str(cm.exception).lower())

    async def test_agent_unavailable_when_never_started(self):
        """When _ever_started was never True, raises AgentUnavailableError."""
        from gateway.agents.errors import AgentUnavailableError

        b = self._make_backend()
        b._ever_started = False
        b._base_url = None
        b._process = None

        with self.assertRaises(AgentUnavailableError):
            await b._ensure_live_runtime()

    async def test_runtime_error_not_raised_on_shutdown_race(self):
        """The confusing RuntimeError('call start() before') must NOT be raised."""
        from gateway.agents.errors import AgentUnavailableError

        b = self._make_backend()
        b._ever_started = False
        b._base_url = None
        b._process = None

        try:
            await b._ensure_live_runtime()
            self.fail("Expected an exception to be raised")
        except AgentUnavailableError:
            pass
        except RuntimeError as e:
            self.fail(f"Got RuntimeError instead of AgentUnavailableError: {e}")

    async def test_ensure_live_ok_when_base_url_present(self):
        """No exception when _base_url is set (normal healthy state)."""
        b = self._make_backend()
        b._base_url = "http://127.0.0.1:9000"
        b._ever_started = True
        b._process = MagicMock()
        b._process.returncode = None

        await b._ensure_live_runtime()


# ── Appended from test_round13_fixes.py ───────────────────────────────────────


class TestGetClientRaisesAgentUnavailable(unittest.IsolatedAsyncioTestCase):
    """_get_client() must raise AgentUnavailableError (not RuntimeError) when
    _client is None.
    """

    def _make_backend(self):
        from gateway.agents.opencode.adapter import OpenCodeBackend

        b = OpenCodeBackend.__new__(OpenCodeBackend)
        b._base_url = "http://127.0.0.1:9000"
        b._client = None
        b._process = MagicMock()
        b._process.returncode = None
        b._ever_started = True
        b._consecutive_restart_failures = 0
        b._restart_lock = asyncio.Lock()
        b._stdout_drain = None
        b._stderr_drain = None
        return b

    def test_get_client_raises_agent_unavailable_when_client_none(self):
        """_get_client() must raise AgentUnavailableError, not RuntimeError."""
        from gateway.agents.errors import AgentUnavailableError

        b = self._make_backend()
        b._client = None

        with self.assertRaises(AgentUnavailableError) as cm:
            b._get_client()

        self.assertIn("stopped", str(cm.exception).lower())

    def test_get_client_does_not_raise_runtime_error(self):
        """The confusing RuntimeError('call start() first') must NOT be raised."""
        from gateway.agents.errors import AgentUnavailableError

        b = self._make_backend()
        b._client = None

        try:
            b._get_client()
            self.fail("Expected an exception to be raised")
        except AgentUnavailableError:
            pass
        except RuntimeError as e:
            self.fail(f"Got RuntimeError instead of AgentUnavailableError: {e}")

    def test_get_client_returns_client_when_set(self):
        """_get_client() must return the client when it is set (happy path)."""
        b = self._make_backend()
        mock_client = MagicMock(spec=httpx.AsyncClient)
        b._client = mock_client

        result = b._get_client()
        self.assertIs(result, mock_client)

    async def test_send_raises_agent_unavailable_on_stop_race(self):
        """send() must surface AgentUnavailableError when stop() races."""
        from gateway.agents.errors import AgentUnavailableError

        b = self._make_backend()
        b._base_url = "http://127.0.0.1:9000"
        b._client = None

        with self.assertRaises(AgentUnavailableError):
            await b.send(
                session_id="session-123",
                prompt="hello world",
                working_directory="/tmp",
                timeout=60,
            )


# ── Appended from test_round15_fixes.py ───────────────────────────────────────


class TestStartInnerCancelledErrorCleansUp(unittest.IsolatedAsyncioTestCase):
    """_start_inner must clean up the subprocess when cancelled during health poll."""

    def _make_backend(self):
        from gateway.agents.opencode.adapter import OpenCodeBackend

        b = OpenCodeBackend.__new__(OpenCodeBackend)
        b._base_url = None
        b._client = None
        b._process = None
        b._ever_started = False
        b._consecutive_restart_failures = 0
        b._restart_lock = asyncio.Lock()
        b._stdout_drain = None
        b._stderr_drain = None
        b._command = "opencode"
        b._new_session_args = []
        b._sidecar_env = {}
        b._sidecar_cwd = None
        return b

    def _mock_subprocess(self):
        proc = MagicMock()
        proc.returncode = None
        proc.stdout = MagicMock()
        proc.stderr = MagicMock()
        return proc

    async def test_cancelled_error_during_health_check_triggers_cleanup(self):
        """CancelledError during _wait_for_health must trigger _cleanup_partial_start."""
        b = self._make_backend()
        cleanup_called = False

        async def fake_cleanup():
            nonlocal cleanup_called
            cleanup_called = True

        proc = self._mock_subprocess()

        with (
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock,
                  return_value=proc),
            patch.object(b, "_wait_for_health", new_callable=AsyncMock,
                         side_effect=asyncio.CancelledError("shutdown")),
            patch.object(b, "_cleanup_partial_start", side_effect=fake_cleanup),
        ):
            with self.assertRaises((asyncio.CancelledError, BaseException)):
                await b._start_inner()

        self.assertTrue(
            cleanup_called,
            "_cleanup_partial_start was NOT called on CancelledError.",
        )

    async def test_regular_exception_still_triggers_cleanup(self):
        """Regular Exception during health check still triggers cleanup."""
        b = self._make_backend()
        cleanup_called = False

        async def fake_cleanup():
            nonlocal cleanup_called
            cleanup_called = True

        proc = self._mock_subprocess()

        with (
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock,
                  return_value=proc),
            patch.object(b, "_wait_for_health", new_callable=AsyncMock,
                         side_effect=RuntimeError("health failed")),
            patch.object(b, "_cleanup_partial_start", side_effect=fake_cleanup),
        ):
            with self.assertRaises(RuntimeError):
                await b._start_inner()

        self.assertTrue(cleanup_called, "_cleanup_partial_start not called on RuntimeError")

    async def test_cancelled_error_is_reraised_after_cleanup(self):
        """CancelledError must be re-raised (not swallowed) after cleanup."""
        b = self._make_backend()
        proc = self._mock_subprocess()

        with (
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock,
                  return_value=proc),
            patch.object(b, "_wait_for_health", new_callable=AsyncMock,
                         side_effect=asyncio.CancelledError("shutdown")),
            patch.object(b, "_cleanup_partial_start", new_callable=AsyncMock),
        ):
            try:
                await b._start_inner()
                self.fail("Expected CancelledError to propagate")
            except asyncio.CancelledError:
                pass
            except Exception as e:
                self.fail(f"Expected CancelledError, got {type(e).__name__}: {e}")


# ── Appended from test_round16_fixes.py ───────────────────────────────────────


class TestOrphanCleanupTimeout(unittest.IsolatedAsyncioTestCase):
    """stop() orphan session cleanup must not block gateway shutdown."""

    def _make_backend(self):
        from gateway.agents.opencode.adapter import OpenCodeBackend

        b = OpenCodeBackend.__new__(OpenCodeBackend)
        b._base_url = "http://127.0.0.1:9000"
        client = MagicMock()
        client.aclose = AsyncMock()
        b._client = client
        b._process = MagicMock()
        b._process.pid = 12345
        b._process.returncode = None
        b._process.terminate = MagicMock()
        b._process.kill = MagicMock()
        b._process.wait = AsyncMock(return_value=None)
        b._ever_started = True
        b._consecutive_restart_failures = 0
        b._restart_lock = asyncio.Lock()
        b._stdout_drain = None
        b._stderr_drain = None
        b._orphan_session_ids = {"sess-aaa", "sess-bbb"}
        return b

    async def test_orphan_cleanup_timeout_does_not_hang_stop(self):
        """If orphan cleanup hangs, stop() must complete within a few seconds."""
        b = self._make_backend()

        async def hanging_cleanup():
            await asyncio.sleep(9999)

        with patch.object(b, "_cleanup_orphan_sessions_best_effort",
                          side_effect=hanging_cleanup):
            try:
                await asyncio.wait_for(b.stop(), timeout=15.0)
            except asyncio.TimeoutError:
                self.fail(
                    "stop() hung for 15s — orphan session cleanup timeout is not working."
                )

    async def test_orphan_cleanup_timeout_logs_warning(self):
        """Timeout during orphan cleanup must log a warning."""
        b = self._make_backend()

        async def slow_cleanup():
            await asyncio.sleep(9999)

        with (
            patch.object(b, "_cleanup_orphan_sessions_best_effort",
                         side_effect=slow_cleanup),
            self.assertLogs("coop.agents.opencode", level="WARNING") as log_ctx,
        ):
            try:
                await asyncio.wait_for(b.stop(), timeout=15.0)
            except asyncio.TimeoutError:
                self.fail("stop() should complete within 15s with a 10s cleanup timeout")

        combined = " ".join(log_ctx.output)
        self.assertIn(
            "timed out",
            combined.lower(),
            f"Expected 'timed out' in warning log, got: {log_ctx.output}",
        )


# ── Appended from test_code_review_fixes.py ───────────────────────────────────


class TestSharedHttpxClient(unittest.IsolatedAsyncioTestCase):
    """Issue #11: RocketChatREST should use shared long-lived httpx clients."""

    async def test_rest_close_closes_both_clients(self):
        from gateway.connectors.rocketchat.rest import RocketChatREST

        rest = RocketChatREST("http://localhost:3000")

        self.assertIsNotNone(rest._client)
        self.assertIsNotNone(rest._download_client)

        await rest.close()

        self.assertTrue(rest._client.is_closed)
        self.assertTrue(rest._download_client.is_closed)

    def test_shared_client_initialized_in_constructor(self):
        """Shared clients must be created at init time, not per-request."""
        from gateway.connectors.rocketchat.rest import RocketChatREST

        rest = RocketChatREST("http://localhost:3000")

        self.assertIsInstance(rest._client, httpx.AsyncClient)
        self.assertIsInstance(rest._download_client, httpx.AsyncClient)


# ── OpenCodeBackend.stream() — SSE-based streaming ───────────────────────────


def _make_started_backend() -> OpenCodeBackend:
    """Backend pre-configured as if start() already succeeded."""
    b = OpenCodeBackend(command="opencode", new_session_args=[], timeout=120)
    b._base_url = "http://127.0.0.1:54321"
    b._ever_started = True
    b._client = AsyncMock(spec=httpx.AsyncClient)
    return b


def _sse_line(session_id: str, event_type: str, **props) -> str:
    """Build a ``data:`` SSE line for the given event type and properties."""
    payload = {
        "type": event_type,
        "properties": {"sessionID": session_id, **props},
    }
    return f"data: {json.dumps(payload)}"


class TestPostMessageAsync(unittest.IsolatedAsyncioTestCase):
    """_post_message_async() targets the correct endpoint."""

    async def test_posts_to_prompt_async_endpoint(self):
        b = _make_started_backend()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        b._client.post = AsyncMock(return_value=mock_resp)

        await b._post_message_async("sess-1", "hello world")

        b._client.post.assert_awaited_once()
        url = b._client.post.call_args[0][0]
        self.assertIn("/session/sess-1/prompt_async", url)

    async def test_posts_correct_body(self):
        b = _make_started_backend()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        b._client.post = AsyncMock(return_value=mock_resp)

        await b._post_message_async("sess-1", "my prompt")

        body = b._client.post.call_args[1]["json"]
        self.assertEqual(body, {"parts": [{"type": "text", "text": "my prompt"}]})

    async def test_system_kwarg_included_when_given(self):
        b = _make_started_backend()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        b._client.post = AsyncMock(return_value=mock_resp)

        await b._post_message_async("sess-1", "my prompt", system="durable header")

        body = b._client.post.call_args[1]["json"]
        self.assertEqual(
            body,
            {"parts": [{"type": "text", "text": "my prompt"}], "system": "durable header"},
        )

    async def test_system_kwarg_omitted_when_none(self):
        b = _make_started_backend()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        b._client.post = AsyncMock(return_value=mock_resp)

        await b._post_message_async("sess-1", "my prompt", system=None)

        body = b._client.post.call_args[1]["json"]
        self.assertNotIn("system", body)

    async def test_uses_internal_timeout_constant(self):
        """The internal _PROMPT_ASYNC_POST_TIMEOUT constant is used, not a caller-supplied value."""
        from gateway.agents.opencode.adapter import _PROMPT_ASYNC_POST_TIMEOUT
        b = _make_started_backend()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        b._client.post = AsyncMock(return_value=mock_resp)

        await b._post_message_async("sess-1", "text")

        kwargs = b._client.post.call_args[1]
        self.assertEqual(kwargs["timeout"], _PROMPT_ASYNC_POST_TIMEOUT)

    async def test_http_error_mapped_to_agent_error(self):
        b = _make_started_backend()
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        b._client.post = AsyncMock(
            side_effect=httpx.HTTPStatusError("rate limited", request=MagicMock(), response=mock_resp)
        )

        from gateway.agents.errors import AgentRateLimitedError
        with self.assertRaises(AgentRateLimitedError):
            await b._post_message_async("sess-1", "hi")

    async def test_404_raises_agent_execution_error(self):
        """HTTP 404 (session not found) maps to AgentExecutionError with (prompt_async) label."""
        from gateway.agents.errors import AgentExecutionError
        b = _make_started_backend()
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        b._client.post = AsyncMock(
            side_effect=httpx.HTTPStatusError("not found", request=MagicMock(), response=mock_resp)
        )
        with self.assertRaises(AgentExecutionError) as cm:
            await b._post_message_async("sess-1", "hi")
        self.assertIn("404", str(cm.exception))
        self.assertIn("prompt_async", str(cm.exception))

    async def test_http_error_message_contains_prompt_async_label(self):
        """Error messages for HTTP failures include the '(prompt_async)' label."""
        from gateway.agents.errors import AgentExecutionError
        b = _make_started_backend()
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        b._client.post = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "server error", request=MagicMock(), response=mock_resp
            )
        )
        with self.assertRaises(AgentExecutionError) as cm:
            await b._post_message_async("sess-1", "hi")
        self.assertIn("prompt_async", str(cm.exception))

    async def test_503_raises_agent_unavailable(self):
        """HTTP 503 (sidecar crashed mid-turn) maps to AgentUnavailableError."""
        b = _make_started_backend()
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        b._client.post = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "service unavailable", request=MagicMock(), response=mock_resp
            )
        )
        with self.assertRaises(AgentUnavailableError):
            await b._post_message_async("sess-1", "hi")

    async def test_connect_error_raises_agent_unavailable(self):
        """httpx.ConnectError (sidecar unreachable) maps to AgentUnavailableError."""
        b = _make_started_backend()
        b._client.post = AsyncMock(
            side_effect=httpx.ConnectError("connection refused")
        )
        with self.assertRaises(AgentUnavailableError):
            await b._post_message_async("sess-1", "hi")

    async def test_connect_error_message_sanitized(self):
        """ConnectError message must not leak host:port from the raw httpx exception."""
        b = _make_started_backend()
        b._client.post = AsyncMock(
            side_effect=httpx.ConnectError("Connection refused to http://localhost:3000")
        )
        with self.assertRaises(AgentUnavailableError) as cm:
            await b._post_message_async("sess-1", "hi")
        err = str(cm.exception)
        self.assertNotIn("localhost", err)
        self.assertNotIn("3000", err)
        self.assertIn("prompt_async", err)

    async def test_timeout_error_raises_agent_unavailable(self):
        """httpx.TimeoutException maps to AgentUnavailableError."""
        b = _make_started_backend()
        b._client.post = AsyncMock(
            side_effect=httpx.TimeoutException("timed out")
        )
        with self.assertRaises(AgentUnavailableError):
            await b._post_message_async("sess-1", "hi")


class TestParseSSEEvents(unittest.IsolatedAsyncioTestCase):
    """_parse_sse_events() maps OpenCode SSE events to AgentEvents.

    Tests drive the parser directly via a pre-populated asyncio.Queue,
    avoiding any HTTP layer.
    """

    def _deadline(self, seconds: int = 5) -> float:
        """Default 5 s so a missing idle event fails fast instead of hanging 60 s."""
        return asyncio.get_running_loop().time() + seconds

    async def _collect(self, session_id: str, lines: list[str]) -> list[AgentEvent]:
        b = _make_started_backend()
        queue: asyncio.Queue = asyncio.Queue()
        for line in lines:
            await queue.put(line)
        return [
            e async for e in b._parse_sse_events(
                session_id, queue, self._deadline(), 60
            )
        ]

    async def test_tool_running_yields_tool_call(self):
        events = await self._collect("s1", [
            _sse_line("s1", "message.part.updated",
                      part={"id": "p1", "type": "tool", "tool": "Bash",
                            "state": {"status": "running"}}),
            _sse_line("s1", "session.status", status={"type": "idle"}),
        ])
        tool_calls = [e for e in events if e.kind == "tool_call"]
        self.assertEqual(len(tool_calls), 1)
        self.assertIn("Bash", tool_calls[0].text)

    async def test_tool_completed_yields_tool_result(self):
        events = await self._collect("s1", [
            _sse_line("s1", "message.part.updated",
                      part={"id": "p1", "type": "tool", "tool": "Read",
                            "state": {"status": "completed"}}),
            _sse_line("s1", "session.status", status={"type": "idle"}),
        ])
        results = [e for e in events if e.kind == "tool_result"]
        self.assertEqual(len(results), 1)
        self.assertIn("Read", results[0].text)

    async def test_tool_error_state_yields_tool_result(self):
        """error state is treated the same as completed for UX purposes."""
        events = await self._collect("s1", [
            _sse_line("s1", "message.part.updated",
                      part={"id": "p1", "type": "tool", "tool": "Bash",
                            "state": {"status": "error"}}),
            _sse_line("s1", "session.status", status={"type": "idle"}),
        ])
        results = [e for e in events if e.kind == "tool_result"]
        self.assertEqual(len(results), 1)

    async def test_reasoning_part_yields_thinking(self):
        events = await self._collect("s1", [
            _sse_line("s1", "message.part.updated",
                      part={"id": "r1", "type": "reasoning", "text": "Let me think..."}),
            _sse_line("s1", "session.status", status={"type": "idle"}),
        ])
        thinking = [e for e in events if e.kind == "thinking"]
        self.assertEqual(len(thinking), 1)
        self.assertIn("💭", thinking[0].text)

    async def test_reasoning_thinking_emitted_only_once(self):
        """Multiple updates to the same reasoning part yield only one thinking event."""
        events = await self._collect("s1", [
            _sse_line("s1", "message.part.updated",
                      part={"id": "r1", "type": "reasoning", "text": "first chunk"}),
            _sse_line("s1", "message.part.updated",
                      part={"id": "r1", "type": "reasoning", "text": "longer text now"}),
            _sse_line("s1", "session.status", status={"type": "idle"}),
        ])
        thinking = [e for e in events if e.kind == "thinking"]
        self.assertEqual(len(thinking), 1)

    async def test_text_deltas_accumulated_in_final_response(self):
        events = await self._collect("s1", [
            _sse_line("s1", "message.part.updated",
                      part={"id": "t1", "type": "text"}),
            _sse_line("s1", "message.part.delta",
                      partID="t1", field="text", delta="Hello "),
            _sse_line("s1", "message.part.delta",
                      partID="t1", field="text", delta="world"),
            _sse_line("s1", "session.status", status={"type": "idle"}),
        ])
        final = events[-1]
        self.assertEqual(final.kind, "final")
        self.assertEqual(final.response.text, "Hello world")

    async def test_reasoning_deltas_excluded_from_final_text(self):
        """Deltas for reasoning parts must not bleed into the final response text."""
        events = await self._collect("s1", [
            _sse_line("s1", "message.part.updated",
                      part={"id": "r1", "type": "reasoning", "text": "thinking"}),
            _sse_line("s1", "message.part.delta",
                      partID="r1", field="text", delta="internal reasoning"),
            _sse_line("s1", "message.part.updated",
                      part={"id": "t1", "type": "text"}),
            _sse_line("s1", "message.part.delta",
                      partID="t1", field="text", delta="actual answer"),
            _sse_line("s1", "session.status", status={"type": "idle"}),
        ])
        final = events[-1]
        self.assertEqual(final.response.text, "actual answer")
        self.assertNotIn("internal reasoning", final.response.text)

    async def test_step_finish_contributes_usage_to_final(self):
        events = await self._collect("s1", [
            _sse_line("s1", "message.part.updated",
                      part={"id": "sf1", "type": "step-finish",
                            "tokens": {"input": 100, "output": 50, "reasoning": 10,
                                       "cache": {"read": 20, "write": 5}},
                            "cost": 0.001}),
            _sse_line("s1", "session.status", status={"type": "idle"}),
        ])
        final = events[-1]
        self.assertIsNotNone(final.response.usage)
        self.assertEqual(final.response.usage.input_tokens, 100)
        self.assertEqual(final.response.usage.output_tokens, 50)
        self.assertEqual(final.response.usage.cache_read_tokens, 20)
        self.assertEqual(final.response.usage.cache_write_tokens, 5)
        self.assertAlmostEqual(final.response.cost_usd, 0.001)

    async def test_other_session_events_filtered_out(self):
        """Events for a different sessionID must be ignored."""
        events = await self._collect("s1", [
            _sse_line("other-sess", "message.part.updated",
                      part={"id": "p1", "type": "tool", "tool": "Bash",
                            "state": {"status": "running"}}),
            _sse_line("s1", "session.status", status={"type": "idle"}),
        ])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "final")

    async def test_session_error_raises_agent_execution_error(self):
        b = _make_started_backend()
        queue: asyncio.Queue = asyncio.Queue()
        await queue.put(
            _sse_line("s1", "session.error", error={"message": "tool failed"})
        )
        with self.assertRaises(AgentExecutionError):
            async for _ in b._parse_sse_events(
                "s1", queue, self._deadline(), 60
            ):
                pass

    async def test_malformed_json_lines_silently_skipped(self):
        events = await self._collect("s1", [
            "data: {not valid json",
            "not even data prefix",
            "data:",
            _sse_line("s1", "session.status", status={"type": "idle"}),
        ])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "final")

    async def test_empty_response_placeholder_when_no_text(self):
        events = await self._collect("s1", [
            _sse_line("s1", "session.status", status={"type": "idle"}),
        ])
        self.assertEqual(events[0].response.text, "(empty response)")

    async def test_infrastructure_events_ignored(self):
        """server.heartbeat and session.updated events are silently ignored."""
        events = await self._collect("s1", [
            _sse_line("s1", "server.heartbeat"),
            _sse_line("s1", "session.updated", info={}),
            _sse_line("s1", "session.status", status={"type": "idle"}),
        ])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "final")

    async def test_sse_exception_in_queue_raises_unavailable(self):
        """An Exception object in the queue raises AgentUnavailableError."""
        b = _make_started_backend()
        queue: asyncio.Queue = asyncio.Queue()
        await queue.put(ConnectionError("SSE dropped"))
        with self.assertRaises(AgentUnavailableError):
            async for _ in b._parse_sse_events(
                "s1", queue, self._deadline(), 60
            ):
                pass

    async def test_full_turn_sequence_tool_then_text(self):
        """Full realistic sequence: tool call → tool result → text → final."""
        events = await self._collect("s1", [
            _sse_line("s1", "message.part.updated",
                      part={"id": "p1", "type": "tool", "tool": "Bash",
                            "state": {"status": "running"}}),
            _sse_line("s1", "message.part.updated",
                      part={"id": "p1", "type": "tool", "tool": "Bash",
                            "state": {"status": "completed"}}),
            _sse_line("s1", "message.part.updated",
                      part={"id": "t1", "type": "text"}),
            _sse_line("s1", "message.part.delta",
                      partID="t1", field="text", delta="Done!"),
            _sse_line("s1", "session.status", status={"type": "idle"}),
        ])
        kinds = [e.kind for e in events]
        self.assertEqual(kinds, ["tool_call", "tool_result", "final"])
        self.assertEqual(events[-1].response.text, "Done!")

    async def test_is_error_false_when_text_present(self):
        """is_error is False when the final response contains text."""
        events = await self._collect("s1", [
            _sse_line("s1", "message.part.updated", part={"id": "t1", "type": "text"}),
            _sse_line("s1", "message.part.delta",
                      partID="t1", field="text", delta="hello"),
            _sse_line("s1", "session.status", status={"type": "idle"}),
        ])
        final = events[-1].response
        self.assertFalse(final.is_error)
        self.assertEqual(final.text, "hello")

    async def test_is_error_false_when_step_finish_present(self):
        """is_error is False when step-finish events arrived (non-empty turn)."""
        events = await self._collect("s1", [
            _sse_line("s1", "message.part.updated",
                      part={"id": "sf1", "type": "step-finish",
                            "tokens": {"input": 10, "output": 5}, "cost": 0.001}),
            _sse_line("s1", "session.status", status={"type": "idle"}),
        ])
        final = events[-1].response
        self.assertFalse(final.is_error)

    async def test_is_error_true_when_no_text_and_no_step_finish(self):
        """is_error is True when neither text nor step-finish events arrived."""
        events = await self._collect("s1", [
            _sse_line("s1", "session.status", status={"type": "idle"}),
        ])
        final = events[-1].response
        self.assertTrue(final.is_error)
        self.assertEqual(final.text, "(empty response)")

    async def test_multi_part_text_order_preserved(self):
        """Text from multiple parts is concatenated in arrival order."""
        events = await self._collect("s1", [
            # Register p1 as text, accumulate "Hello "
            _sse_line("s1", "message.part.updated", part={"id": "p1", "type": "text"}),
            _sse_line("s1", "message.part.delta",
                      partID="p1", field="text", delta="Hello "),
            # Register p2 as text, accumulate "world"
            _sse_line("s1", "message.part.updated", part={"id": "p2", "type": "text"}),
            _sse_line("s1", "message.part.delta",
                      partID="p2", field="text", delta="world"),
            # Interleaved second chunk for p1
            _sse_line("s1", "message.part.delta",
                      partID="p1", field="text", delta="— "),
            _sse_line("s1", "session.status", status={"type": "idle"}),
        ])
        # p1 was seen first, so its text ("Hello — ") comes before p2's ("world")
        self.assertEqual(events[-1].response.text, "Hello — world")

    async def test_delta_before_part_registered_is_skipped(self):
        """Deltas for unregistered (unknown-type) parts are silently dropped."""
        events = await self._collect("s1", [
            # Delta arrives before the corresponding message.part.updated
            _sse_line("s1", "message.part.delta",
                      partID="unknown-p", field="text", delta="leaked reasoning"),
            # Register as text too late; no delta was accumulated
            _sse_line("s1", "message.part.updated", part={"id": "unknown-p", "type": "text"}),
            _sse_line("s1", "session.status", status={"type": "idle"}),
        ])
        # The early delta must not appear in final text
        self.assertNotIn("leaked reasoning", events[-1].response.text)

    async def test_duplicate_tool_running_yields_only_one_tool_call(self):
        """Repeated running-status updates for the same tool part emit only one tool_call."""
        events = await self._collect("s1", [
            _sse_line("s1", "message.part.updated",
                      part={"id": "p1", "type": "tool", "tool": "Bash",
                            "state": {"status": "running"}}),
            # Second running update for the same part (state refresh)
            _sse_line("s1", "message.part.updated",
                      part={"id": "p1", "type": "tool", "tool": "Bash",
                            "state": {"status": "running"}}),
            _sse_line("s1", "session.status", status={"type": "idle"}),
        ])
        tool_calls = [e for e in events if e.kind == "tool_call"]
        self.assertEqual(len(tool_calls), 1, "Expected exactly one tool_call event")

    async def test_duplicate_tool_completed_yields_only_one_tool_result(self):
        """Repeated completed-status updates for the same tool part emit only one tool_result."""
        events = await self._collect("s1", [
            _sse_line("s1", "message.part.updated",
                      part={"id": "p1", "type": "tool", "tool": "Read",
                            "state": {"status": "running"}}),
            _sse_line("s1", "message.part.updated",
                      part={"id": "p1", "type": "tool", "tool": "Read",
                            "state": {"status": "completed"}}),
            # Duplicate completed update
            _sse_line("s1", "message.part.updated",
                      part={"id": "p1", "type": "tool", "tool": "Read",
                            "state": {"status": "completed"}}),
            _sse_line("s1", "session.status", status={"type": "idle"}),
        ])
        tool_results = [e for e in events if e.kind == "tool_result"]
        self.assertEqual(len(tool_results), 1, "Expected exactly one tool_result event")

    async def test_duplicate_step_finish_does_not_double_count_tokens(self):
        """Repeated message.part.updated for same step-finish part counts tokens only once."""
        events = await self._collect("s1", [
            _sse_line("s1", "message.part.updated",
                      part={"id": "sf1", "type": "step-finish",
                            "tokens": {"input": 100, "output": 50, "reasoning": 0, "cache": {}},
                            "cost": 0.01}),
            # Duplicate (e.g. state finalization event)
            _sse_line("s1", "message.part.updated",
                      part={"id": "sf1", "type": "step-finish",
                            "tokens": {"input": 100, "output": 50, "reasoning": 0, "cache": {}},
                            "cost": 0.01}),
            _sse_line("s1", "session.status", status={"type": "idle"}),
        ])
        final = events[-1].response
        self.assertEqual(final.num_turns, 1, "num_turns should be 1, not 2")
        self.assertEqual(final.usage.input_tokens, 100, "input tokens must not be doubled")
        self.assertAlmostEqual(final.cost_usd, 0.01, places=6)

    async def test_null_properties_does_not_crash(self):
        """An SSE event with 'properties': null is silently skipped (not a crash)."""
        import json as _json
        b = _make_started_backend()
        queue: asyncio.Queue = asyncio.Queue()
        # Craft a raw data line with null properties
        null_props_line = 'data: ' + _json.dumps({"type": "server.heartbeat", "properties": None})
        await queue.put(null_props_line)
        await queue.put(_sse_line("s1", "session.status", status={"type": "idle"}))
        events = [e async for e in b._parse_sse_events("s1", queue, self._deadline(), 60)]
        self.assertEqual(events[-1].kind, "final")

    async def test_null_part_in_message_part_updated_does_not_crash(self):
        """A message.part.updated event with 'part': null is silently skipped."""
        import json as _json
        b = _make_started_backend()
        queue: asyncio.Queue = asyncio.Queue()
        null_part_line = 'data: ' + _json.dumps({
            "type": "message.part.updated",
            "properties": {"sessionID": "s1", "part": None},
        })
        await queue.put(null_part_line)
        await queue.put(_sse_line("s1", "session.status", status={"type": "idle"}))
        events = [e async for e in b._parse_sse_events("s1", queue, self._deadline(), 60)]
        self.assertEqual(events[-1].kind, "final")

    async def test_null_tokens_in_step_finish_does_not_crash(self):
        """A step-finish event with 'tokens': null does not raise AttributeError."""
        import json as _json
        b = _make_started_backend()
        queue: asyncio.Queue = asyncio.Queue()
        null_tokens_line = 'data: ' + _json.dumps({
            "type": "message.part.updated",
            "properties": {
                "sessionID": "s1",
                "part": {"id": "sf1", "type": "step-finish", "tokens": None, "cost": 0.001},
            },
        })
        await queue.put(null_tokens_line)
        await queue.put(_sse_line("s1", "session.status", status={"type": "idle"}))
        events = [e async for e in b._parse_sse_events("s1", queue, self._deadline(), 60)]
        final = events[-1].response
        self.assertEqual(final.num_turns, 1)
        self.assertIsNone(final.usage)  # no tokens → no usage object

    async def test_reasoning_only_turn_produces_usage_object(self):
        """A turn with only reasoning tokens still produces a non-None usage object."""
        events = await self._collect("s1", [
            _sse_line("s1", "message.part.updated",
                      part={"id": "sf1", "type": "step-finish",
                            "tokens": {"input": 0, "output": 0, "reasoning": 50, "cache": {}},
                            "cost": 0.0}),
            _sse_line("s1", "session.status", status={"type": "idle"}),
        ])
        final = events[-1].response
        self.assertIsNotNone(final.usage, "usage must not be None for reasoning-only turns")
        self.assertEqual(final.usage.reasoning_tokens, 50)

    async def test_cache_only_turn_produces_usage_object_sse(self):
        """A turn with only cache tokens (zero input/output/reasoning) produces a non-None usage object."""
        events = await self._collect("s1", [
            _sse_line("s1", "message.part.updated",
                      part={"id": "sf1", "type": "step-finish",
                            "tokens": {"input": 0, "output": 0, "reasoning": 0,
                                       "cache": {"read": 300, "write": 0}},
                            "cost": 0.0}),
            _sse_line("s1", "session.status", status={"type": "idle"}),
        ])
        final = events[-1].response
        self.assertIsNotNone(final.usage, "usage must not be None for cache-only turns")
        self.assertEqual(final.usage.cache_read_tokens, 300)

    async def test_string_status_in_session_status_does_not_crash(self):
        """session.status with a plain string value is gracefully ignored."""
        import json as _json
        b = _make_started_backend()
        queue: asyncio.Queue = asyncio.Queue()
        # Simulate server sending "status": "idle" as a string instead of a dict
        string_status_line = 'data: ' + _json.dumps({
            "type": "session.status",
            "properties": {"sessionID": "s1", "status": "idle"},
        })
        await queue.put(string_status_line)
        # Follow up with the proper dict-shaped idle so the parser can terminate
        await queue.put(_sse_line("s1", "session.status", status={"type": "idle"}))
        events = [e async for e in b._parse_sse_events("s1", queue, self._deadline(), 60)]
        self.assertEqual(events[-1].kind, "final")

    async def test_step_finish_cost_null_does_not_crash(self):
        """Explicit JSON null for 'cost' in step-finish must not raise TypeError."""
        import json as _json
        b = _make_started_backend()
        queue: asyncio.Queue = asyncio.Queue()
        null_cost_line = 'data: ' + _json.dumps({
            "type": "message.part.updated",
            "properties": {
                "sessionID": "s1",
                "part": {"id": "sf1", "type": "step-finish",
                         "tokens": {"input": 5, "output": 5, "reasoning": 0, "cache": {}},
                         "cost": None},
            },
        })
        await queue.put(null_cost_line)
        await queue.put(_sse_line("s1", "session.status", status={"type": "idle"}))
        events = [e async for e in b._parse_sse_events("s1", queue, self._deadline(), 60)]
        # Must not raise; cost defaults to 0
        self.assertIsNone(events[-1].response.cost_usd)

    async def test_non_dict_json_payload_is_skipped(self):
        """SSE lines with valid but non-dict JSON (null, array, string) are skipped."""
        import json as _json
        b = _make_started_backend()
        queue: asyncio.Queue = asyncio.Queue()
        for non_dict_val in [None, [], [1, 2], "hello", 42]:
            await queue.put(f"data: {_json.dumps(non_dict_val)}")
        await queue.put(_sse_line("s1", "session.status", status={"type": "idle"}))
        # None of the non-dict lines should crash; parser terminates normally
        events = [e async for e in b._parse_sse_events("s1", queue, self._deadline(), 60)]
        self.assertEqual(events[-1].kind, "final")

    async def test_session_error_with_message_key_uses_it(self):
        """session.error with a 'message' key produces a readable error string."""
        b = _make_started_backend()
        queue: asyncio.Queue = asyncio.Queue()
        await queue.put(_sse_line("s1", "session.error",
                                  error={"message": "tool execution failed", "code": 500}))
        with self.assertRaises(AgentExecutionError) as ctx:
            async for _ in b._parse_sse_events("s1", queue, self._deadline(), 5):
                pass
        self.assertIn("tool execution failed", str(ctx.exception))

    async def test_session_error_null_error_field_does_not_crash(self):
        """session.error with error=null produces a clean 'unknown error' message."""
        import json as _json
        b = _make_started_backend()
        queue: asyncio.Queue = asyncio.Queue()
        null_error_line = 'data: ' + _json.dumps({
            "type": "session.error",
            "properties": {"sessionID": "s1", "error": None},
        })
        await queue.put(null_error_line)
        with self.assertRaises(AgentExecutionError) as ctx:
            async for _ in b._parse_sse_events("s1", queue, self._deadline(), 5):
                pass
        self.assertIn("unknown error", str(ctx.exception))

    async def test_session_error_from_other_session_is_ignored(self):
        """session.error for a different sessionID must not affect the listener."""
        events = await self._collect("s1", [
            # Error from session "other" — should be silently ignored
            _sse_line("other", "session.error", error={"message": "other session crashed"}),
            # Our session completes normally
            _sse_line("s1", "session.status", status={"type": "idle"}),
        ])
        self.assertEqual(events[-1].kind, "final")

    async def test_tool_result_without_prior_tool_call_still_emits(self):
        """tool_result is emitted even if the running state was never seen.

        Documents intended behaviour: when a server skips the running→completed
        transition and sends only a completed-status update, one tool_result
        event (and no tool_call) should be yielded.
        """
        events = await self._collect("s1", [
            # No running update — server jumps straight to completed
            _sse_line("s1", "message.part.updated",
                      part={"id": "p1", "type": "tool", "tool": "Read",
                            "state": {"status": "completed"}}),
            _sse_line("s1", "session.status", status={"type": "idle"}),
        ])
        tool_calls = [e for e in events if e.kind == "tool_call"]
        tool_results = [e for e in events if e.kind == "tool_result"]
        self.assertEqual(len(tool_calls), 0, "no running update → no tool_call")
        self.assertEqual(len(tool_results), 1, "completed update → one tool_result")
        self.assertIn("Read", tool_results[0].text)

    async def test_string_typed_cost_does_not_crash(self):
        """String-typed cost field must be treated as 0.0, not cause TypeError."""
        events = await self._collect("s1", [
            _sse_line("s1", "message.part.updated",
                      part={"id": "sf1", "type": "step-finish",
                            "tokens": {"input": 5, "output": 5, "reasoning": 0, "cache": {}},
                            "cost": "0.01"}),  # string instead of float
            _sse_line("s1", "session.status", status={"type": "idle"}),
        ])
        final = events[-1].response
        # String cost is skipped → defaults to 0 → cost_usd is None
        self.assertIsNone(final.cost_usd)

    async def test_string_typed_tokens_do_not_crash(self):
        """String-typed token fields must be treated as 0, not cause TypeError."""
        events = await self._collect("s1", [
            _sse_line("s1", "message.part.updated",
                      part={"id": "sf1", "type": "step-finish",
                            "tokens": {"input": "100", "output": "50",
                                       "reasoning": 0, "cache": {}},
                            "cost": 0.0}),
            _sse_line("s1", "session.status", status={"type": "idle"}),
        ])
        final = events[-1].response
        # String token values are skipped → no usage object
        self.assertIsNone(final.usage)


class TestStream(unittest.IsolatedAsyncioTestCase):
    """stream() end-to-end: SSE handshake, prompt posting, event lifecycle."""

    def _make_sse_response(self, lines: list[str]) -> AsyncMock:
        """Build a mock httpx streaming response that yields the given lines."""
        async def _aiter_lines():
            for line in lines:
                yield line

        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.aiter_lines = _aiter_lines
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        return mock_resp

    async def test_stream_yields_final_event(self):
        """stream() yields a final AgentEvent after a minimal SSE sequence."""
        b = _make_started_backend()

        # Patch _ensure_live_runtime so we don't need a running process
        b._ensure_live_runtime = AsyncMock()

        # Patch _post_message_async so no real HTTP call is made
        b._post_message_async = AsyncMock()

        sse_lines = [
            _sse_line("sess-1", "message.part.updated", part={"id": "t1", "type": "text"}),
            _sse_line("sess-1", "message.part.delta",
                      partID="t1", field="text", delta="Hi there!"),
            _sse_line("sess-1", "session.status", status={"type": "idle"}),
        ]
        mock_sse_resp = self._make_sse_response(sse_lines)

        mock_sse_client = AsyncMock()
        mock_sse_client.stream = MagicMock(return_value=mock_sse_resp)
        mock_sse_client.__aenter__ = AsyncMock(return_value=mock_sse_client)
        mock_sse_client.__aexit__ = AsyncMock(return_value=False)

        with patch("gateway.agents.opencode.adapter.httpx.AsyncClient",
                   return_value=mock_sse_client):
            events = [e async for e in b.stream(
                "sess-1", "hello", "/tmp", timeout=30
            )]

        self.assertTrue(len(events) >= 1)
        final = events[-1]
        self.assertEqual(final.kind, "final")
        self.assertEqual(final.response.text, "Hi there!")
        # Verify _post_message_async received the correct arguments. system=None
        # because this test doesn't pass append_system_prompt_file to stream().
        b._post_message_async.assert_awaited_once_with("sess-1", "hello", system=None)

    async def test_stream_reads_durable_instructions_and_forwards_as_system(self):
        """append_system_prompt_file's *content* (read fresh) is forwarded to
        _post_message_async as the system kwarg — the stream()-side half of
        the durable-instructions mechanism (see ensure_durable_instructions())."""
        import tempfile
        from pathlib import Path

        b = _make_started_backend()
        b._ensure_live_runtime = AsyncMock()
        b._post_message_async = AsyncMock()

        sse_lines = [
            _sse_line("sess-1", "session.status", status={"type": "idle"}),
        ]
        mock_sse_resp = self._make_sse_response(sse_lines)
        mock_sse_client = AsyncMock()
        mock_sse_client.stream = MagicMock(return_value=mock_sse_resp)
        mock_sse_client.__aenter__ = AsyncMock(return_value=mock_sse_client)
        mock_sse_client.__aexit__ = AsyncMock(return_value=False)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "w.md"
            path.write_text("## Coop Session Identity\nyou are watcher w")

            with patch("gateway.agents.opencode.adapter.httpx.AsyncClient",
                       return_value=mock_sse_client):
                async for _ in b.stream(
                    "sess-1", "hello", "/tmp", timeout=30,
                    append_system_prompt_file=str(path),
                ):
                    pass

        b._post_message_async.assert_awaited_once_with(
            "sess-1", "hello", system="## Coop Session Identity\nyou are watcher w",
        )

    async def test_stream_cancels_sse_task_on_completion(self):
        """The SSE background task is cancelled after stream() completes."""
        b = _make_started_backend()
        b._ensure_live_runtime = AsyncMock()
        b._post_message_async = AsyncMock()

        sse_lines = [
            _sse_line("sess-1", "session.status", status={"type": "idle"}),
        ]
        mock_sse_resp = self._make_sse_response(sse_lines)
        mock_sse_client = AsyncMock()
        mock_sse_client.stream = MagicMock(return_value=mock_sse_resp)
        mock_sse_client.__aenter__ = AsyncMock(return_value=mock_sse_client)
        mock_sse_client.__aexit__ = AsyncMock(return_value=False)

        sse_tasks_created = []
        original_create_task = asyncio.create_task

        def _tracking_create_task(coro, **kwargs):
            task = original_create_task(coro, **kwargs)
            sse_tasks_created.append(task)
            return task

        with patch("gateway.agents.opencode.adapter.httpx.AsyncClient",
                   return_value=mock_sse_client):
            with patch("asyncio.create_task", side_effect=_tracking_create_task):
                async for _ in b.stream("sess-1", "hi", "/tmp", timeout=30):
                    pass

        # The SSE task should be done (cancelled or finished) after stream exits
        for task in sse_tasks_created:
            self.assertTrue(task.done(), "SSE task should be done after stream() exits")

    async def test_stream_raises_unavailable_on_sse_connect_failure(self):
        """AgentUnavailableError raised when SSE connection fails immediately."""
        b = _make_started_backend()
        b._ensure_live_runtime = AsyncMock()

        mock_sse_client = AsyncMock()
        # Simulate connection error during SSE setup — raw httpx error includes host:port
        mock_sse_client.stream = MagicMock(
            side_effect=httpx.ConnectError("Connection refused to http://127.0.0.1:54321/event")
        )
        mock_sse_client.__aenter__ = AsyncMock(return_value=mock_sse_client)
        mock_sse_client.__aexit__ = AsyncMock(return_value=False)

        with patch("gateway.agents.opencode.adapter.httpx.AsyncClient",
                   return_value=mock_sse_client):
            with self.assertRaises(AgentUnavailableError) as cm:
                async for _ in b.stream("sess-1", "hi", "/tmp", timeout=30):
                    pass

        # Error message must be sanitized — host:port must not leak to caller
        err = str(cm.exception)
        self.assertNotIn("127.0.0.1", err)
        self.assertNotIn("54321", err)

    async def test_stream_tool_only_turn_is_error_false(self):
        """stream() final event for a tool-only turn (step-finish, no text) is not an error."""
        b = _make_started_backend()
        b._ensure_live_runtime = AsyncMock()
        b._post_message_async = AsyncMock()

        sse_lines = [
            _sse_line("sess-1", "message.part.updated",
                      part={"id": "p1", "type": "tool", "tool": "Bash",
                            "state": {"status": "running"}}),
            _sse_line("sess-1", "message.part.updated",
                      part={"id": "p1", "type": "tool", "tool": "Bash",
                            "state": {"status": "completed"}}),
            _sse_line("sess-1", "message.part.updated",
                      part={"id": "sf1", "type": "step-finish",
                            "tokens": {"input": 10, "output": 5, "reasoning": 0, "cache": None},
                            "cost": 0.001}),
            _sse_line("sess-1", "session.status", status={"type": "idle"}),
        ]
        mock_sse_resp = self._make_sse_response(sse_lines)
        mock_sse_client = AsyncMock()
        mock_sse_client.stream = MagicMock(return_value=mock_sse_resp)
        mock_sse_client.__aenter__ = AsyncMock(return_value=mock_sse_client)
        mock_sse_client.__aexit__ = AsyncMock(return_value=False)

        with patch("gateway.agents.opencode.adapter.httpx.AsyncClient",
                   return_value=mock_sse_client):
            events = [e async for e in b.stream("sess-1", "run tool", "/tmp", timeout=30)]

        final = events[-1]
        self.assertEqual(final.kind, "final")
        # step-finish was seen so is_error must be False
        self.assertFalse(final.response.is_error)
        # Also exercises the cache=null guard: no crash
        self.assertEqual(final.response.num_turns, 1)

    async def test_stream_propagates_post_message_failure(self):
        """If _post_message_async raises, stream() propagates the error and cancels SSE task."""
        from gateway.agents.errors import AgentUnavailableError as _AUE
        b = _make_started_backend()
        b._ensure_live_runtime = AsyncMock()
        b._post_message_async = AsyncMock(
            side_effect=_AUE("sidecar rejected prompt with 503")
        )

        # SSE connects successfully but _post_message_async blows up
        sse_lines: list[str] = []  # never gets consumed
        mock_sse_resp = self._make_sse_response(sse_lines)
        mock_sse_client = AsyncMock()
        mock_sse_client.stream = MagicMock(return_value=mock_sse_resp)
        mock_sse_client.__aenter__ = AsyncMock(return_value=mock_sse_client)
        mock_sse_client.__aexit__ = AsyncMock(return_value=False)

        with patch("gateway.agents.opencode.adapter.httpx.AsyncClient",
                   return_value=mock_sse_client):
            with self.assertRaises(_AUE):
                async for _ in b.stream("sess-1", "hi", "/tmp", timeout=30):
                    pass

    async def test_stream_timeout_raises_asyncio_timeout_error(self):
        """stream() raises asyncio.TimeoutError when the deadline elapses mid-stream."""
        b = _make_started_backend()
        b._ensure_live_runtime = AsyncMock()
        b._post_message_async = AsyncMock()

        # SSE stream that never delivers session.status idle — just hangs
        async def _hanging_lines():
            # Yield one event then block forever
            yield _sse_line("sess-1", "message.part.updated",
                            part={"id": "t1", "type": "text"})
            # simulate a very long wait by just not yielding anything else
            await asyncio.sleep(999)

        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.aiter_lines = _hanging_lines
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_sse_client = AsyncMock()
        mock_sse_client.stream = MagicMock(return_value=mock_resp)
        mock_sse_client.__aenter__ = AsyncMock(return_value=mock_sse_client)
        mock_sse_client.__aexit__ = AsyncMock(return_value=False)

        with patch("gateway.agents.opencode.adapter.httpx.AsyncClient",
                   return_value=mock_sse_client):
            # Use a very short timeout so the test doesn't actually wait long
            with self.assertRaises(asyncio.TimeoutError):
                async for _ in b.stream("sess-1", "hi", "/tmp", timeout=1):
                    pass

    async def test_stream_raises_unavailable_on_sse_eof_before_idle(self):
        """AgentUnavailableError is raised when the SSE stream closes before session.status idle."""
        b = _make_started_backend()
        b._ensure_live_runtime = AsyncMock()
        b._post_message_async = AsyncMock()

        # SSE stream that closes immediately after sending one event (no idle)
        async def _eof_lines():
            yield _sse_line("sess-1", "message.part.updated",
                            part={"id": "t1", "type": "text"})
            # Generator returns here — natural stream close

        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.aiter_lines = _eof_lines
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_sse_client = AsyncMock()
        mock_sse_client.stream = MagicMock(return_value=mock_resp)
        mock_sse_client.__aenter__ = AsyncMock(return_value=mock_sse_client)
        mock_sse_client.__aexit__ = AsyncMock(return_value=False)

        with patch("gateway.agents.opencode.adapter.httpx.AsyncClient",
                   return_value=mock_sse_client):
            with self.assertRaises(AgentUnavailableError):
                async for _ in b.stream("sess-1", "hi", "/tmp", timeout=30):
                    pass

    async def test_stream_raises_unavailable_on_sse_http_error(self):
        """AgentUnavailableError raised when GET /event returns an HTTP error status."""
        b = _make_started_backend()
        b._ensure_live_runtime = AsyncMock()

        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "503", request=MagicMock(),
                response=MagicMock(status_code=503)
            )
        )
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_sse_client = AsyncMock()
        mock_sse_client.stream = MagicMock(return_value=mock_resp)
        mock_sse_client.__aenter__ = AsyncMock(return_value=mock_sse_client)
        mock_sse_client.__aexit__ = AsyncMock(return_value=False)

        with patch("gateway.agents.opencode.adapter.httpx.AsyncClient",
                   return_value=mock_sse_client):
            with self.assertRaises(AgentUnavailableError):
                async for _ in b.stream("sess-1", "hi", "/tmp", timeout=30):
                    pass

    async def test_stream_injects_attachments_into_prompt(self):
        """Attachments are injected into the prompt text before posting."""
        b = _make_started_backend()
        b._ensure_live_runtime = AsyncMock()
        b._post_message_async = AsyncMock()

        sse_lines = [
            _sse_line("sess-1", "session.status", status={"type": "idle"}),
        ]
        mock_sse_resp = self._make_sse_response(sse_lines)
        mock_sse_client = AsyncMock()
        mock_sse_client.stream = MagicMock(return_value=mock_sse_resp)
        mock_sse_client.__aenter__ = AsyncMock(return_value=mock_sse_client)
        mock_sse_client.__aexit__ = AsyncMock(return_value=False)

        with patch("gateway.agents.opencode.adapter.httpx.AsyncClient",
                   return_value=mock_sse_client):
            async for _ in b.stream(
                "sess-1", "check this file", "/workspace",
                timeout=30, attachments=["/workspace/file.txt"]
            ):
                pass

        # The prompt passed to _post_message_async must include attachment content
        actual_prompt = b._post_message_async.call_args[0][1]
        self.assertIn("file.txt", actual_prompt)
        self.assertNotEqual(actual_prompt, "check this file")

    async def test_stream_sse_connect_timeout_capped_by_deadline(self):
        """SSE connect timeout is capped to remaining deadline, not always 15s.

        The test blocks inside the streaming context manager __aenter__ so that
        _SSE_READY is never put into the queue. This is the only way to exercise
        the asyncio.wait_for(queue.get(), timeout=sse_connect_timeout) branch.
        Blocking inside aiter_lines() would be too late — _SSE_READY is enqueued
        before aiter_lines() is called.
        """
        b = _make_started_backend()
        b._ensure_live_runtime = AsyncMock()

        # Block inside the stream() context manager so _SSE_READY is never queued
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()

        async def _slow_enter(_self=None):
            await asyncio.sleep(999)  # blocks before _SSE_READY is enqueued
            return mock_resp

        mock_resp.__aenter__ = _slow_enter
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_sse_client = AsyncMock()
        mock_sse_client.stream = MagicMock(return_value=mock_resp)
        mock_sse_client.__aenter__ = AsyncMock(return_value=mock_sse_client)
        mock_sse_client.__aexit__ = AsyncMock(return_value=False)

        import time
        start = time.monotonic()
        with patch("gateway.agents.opencode.adapter.httpx.AsyncClient",
                   return_value=mock_sse_client):
            with self.assertRaises(AgentUnavailableError):
                async for _ in b.stream("sess-1", "hi", "/tmp", timeout=1):
                    pass
        elapsed = time.monotonic() - start
        # With timeout=1, the connect wait is capped to ~1s, not the full 15s
        self.assertLess(elapsed, 5.0, "SSE connect should be capped by the caller timeout")

    async def test_stream_sse_task_cancelled_on_early_break(self):
        """SSE background task is cancelled when caller breaks out of stream() early."""
        b = _make_started_backend()
        b._ensure_live_runtime = AsyncMock()
        b._post_message_async = AsyncMock()

        # SSE stream that keeps yielding tool_call events (different part IDs
        # to bypass dedup) so _parse_sse_events always has events to yield.
        async def _infinite_lines():
            i = 0
            while True:
                yield _sse_line("sess-1", "message.part.updated",
                                part={"id": f"tool-{i}", "type": "tool",
                                      "tool": "Bash", "state": {"status": "running"}})
                i += 1
                await asyncio.sleep(0)

        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.aiter_lines = _infinite_lines
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_sse_client = AsyncMock()
        mock_sse_client.stream = MagicMock(return_value=mock_resp)
        mock_sse_client.__aenter__ = AsyncMock(return_value=mock_sse_client)
        mock_sse_client.__aexit__ = AsyncMock(return_value=False)

        sse_tasks_seen: list[asyncio.Task] = []
        original_create_task = asyncio.create_task

        def _tracking_create_task(coro, **kwargs):
            task = original_create_task(coro, **kwargs)
            sse_tasks_seen.append(task)
            return task

        with patch("gateway.agents.opencode.adapter.httpx.AsyncClient",
                   return_value=mock_sse_client):
            with patch("asyncio.create_task", side_effect=_tracking_create_task):
                gen = b.stream("sess-1", "hi", "/tmp", timeout=30)
                # Consume the first event then break early (AgentTurnRunner pattern)
                async for event in gen:
                    break
                await gen.aclose()

        # The SSE task must be done after early exit
        for task in sse_tasks_seen:
            self.assertTrue(task.done(), "SSE task should be done after early break")


# ── _build_safe_opencode_config ───────────────────────────────────────────────


class TestBuildSafeOpencodeConfig(unittest.TestCase):
    """Tests for _build_safe_opencode_config() and its integration in __init__."""

    def setUp(self):
        from gateway.agents.opencode.adapter import (
            _DEFAULT_BASH_ALLOW_PATTERNS,
            _build_safe_opencode_config,
        )
        self.fn = _build_safe_opencode_config
        self.default_patterns = _DEFAULT_BASH_ALLOW_PATTERNS

    # ── no existing config ────────────────────────────────────────────────────

    def test_empty_env_injects_full_defaults(self):
        """No OPENCODE_CONFIG_CONTENT → full default bash config is injected."""
        result = self.fn({})
        self.assertIsNotNone(result)
        config = json.loads(result)
        bash = config["permission"]["bash"]
        self.assertEqual(bash["*"], "ask")
        for p in self.default_patterns:
            self.assertEqual(bash[p], "allow", f"Expected '{p}' to be 'allow'")

    def test_empty_string_value_treated_as_no_config(self):
        """OPENCODE_CONFIG_CONTENT='' is equivalent to no config."""
        result = self.fn({"OPENCODE_CONFIG_CONTENT": ""})
        self.assertIsNotNone(result)
        config = json.loads(result)
        self.assertEqual(config["permission"]["bash"]["*"], "ask")

    # ── user has bash key but no "*" catch-all ────────────────────────────────

    def test_user_bash_without_catchall_gets_catchall_appended(self):
        """Existing bash rules preserved; missing '*' catch-all is appended."""
        existing = json.dumps({"permission": {"bash": {"git log *": "allow"}}})
        result = self.fn({"OPENCODE_CONFIG_CONTENT": existing})
        self.assertIsNotNone(result)
        config = json.loads(result)
        bash = config["permission"]["bash"]
        self.assertEqual(bash["*"], "ask")
        # User's original rule must be preserved
        self.assertEqual(bash["git log *"], "allow")

    def test_user_bash_patterns_not_overwritten_by_defaults(self):
        """If user already has a default pattern set, AgentCoop must not overwrite it."""
        existing = json.dumps({
            "permission": {"bash": {"git log *": "deny"}}  # user explicitly denies
        })
        result = self.fn({"OPENCODE_CONFIG_CONTENT": existing})
        self.assertIsNotNone(result)
        config = json.loads(result)
        bash = config["permission"]["bash"]
        # User's "deny" must not be overwritten to "allow"
        self.assertEqual(bash["git log *"], "deny")
        self.assertEqual(bash["*"], "ask")

    def test_default_allow_patterns_added_when_absent(self):
        """Default allow patterns are added when not present in user config."""
        existing = json.dumps({"permission": {"bash": {"my-custom *": "allow"}}})
        result = self.fn({"OPENCODE_CONFIG_CONTENT": existing})
        self.assertIsNotNone(result)
        config = json.loads(result)
        bash = config["permission"]["bash"]
        # All default patterns injected
        for p in self.default_patterns:
            self.assertIn(p, bash, f"Expected default pattern '{p}' to be injected")
        # Custom user pattern preserved
        self.assertEqual(bash["my-custom *"], "allow")

    # ── user has "*" catch-all — no injection ─────────────────────────────────

    def test_user_catchall_allow_respected(self):
        """bash['*'] = 'allow' → user's explicit choice, return None."""
        existing = json.dumps({"permission": {"bash": {"*": "allow"}}})
        result = self.fn({"OPENCODE_CONFIG_CONTENT": existing})
        self.assertIsNone(result)

    def test_user_catchall_ask_respected(self):
        """bash['*'] = 'ask' already set → already secure, return None."""
        existing = json.dumps({"permission": {"bash": {"*": "ask", "ls *": "allow"}}})
        result = self.fn({"OPENCODE_CONFIG_CONTENT": existing})
        self.assertIsNone(result)

    def test_user_catchall_deny_respected(self):
        """bash['*'] = 'deny' → user's explicit choice, return None."""
        existing = json.dumps({"permission": {"bash": {"*": "deny"}}})
        result = self.fn({"OPENCODE_CONFIG_CONTENT": existing})
        self.assertIsNone(result)

    # ── user has no bash key but has other permission keys ────────────────────

    def test_other_permission_keys_preserved(self):
        """Existing non-bash permission config is preserved when merging."""
        existing = json.dumps({"permission": {"read": {"*": "allow"}}})
        result = self.fn({"OPENCODE_CONFIG_CONTENT": existing})
        self.assertIsNotNone(result)
        config = json.loads(result)
        # Existing read config preserved
        self.assertEqual(config["permission"]["read"]["*"], "allow")
        # bash defaults injected
        self.assertEqual(config["permission"]["bash"]["*"], "ask")

    def test_non_permission_keys_preserved(self):
        """Top-level keys other than 'permission' are left untouched."""
        existing = json.dumps({"model": "anthropic/claude-sonnet", "theme": "dark"})
        result = self.fn({"OPENCODE_CONFIG_CONTENT": existing})
        self.assertIsNotNone(result)
        config = json.loads(result)
        self.assertEqual(config["model"], "anthropic/claude-sonnet")
        self.assertEqual(config["theme"], "dark")
        self.assertEqual(config["permission"]["bash"]["*"], "ask")

    # ── error handling ────────────────────────────────────────────────────────

    def test_malformed_json_raises_valueerror(self):
        """Invalid JSON in OPENCODE_CONFIG_CONTENT must raise ValueError."""
        with self.assertRaises(ValueError) as cm:
            self.fn({"OPENCODE_CONFIG_CONTENT": "{not valid json"})
        self.assertIn("invalid JSON", str(cm.exception))

    def test_malformed_json_error_does_not_silently_drop_config(self):
        """ValueError message must hint at the fix, not just say 'error'."""
        with self.assertRaises(ValueError) as cm:
            self.fn({"OPENCODE_CONFIG_CONTENT": "!!!"})
        msg = str(cm.exception)
        self.assertIn("OPENCODE_CONFIG_CONTENT", msg)

    # ── __init__ integration ──────────────────────────────────────────────────

    def test_init_injects_config_when_no_existing_env(self):
        """OpenCodeBackend.__init__ injects OPENCODE_CONFIG_CONTENT by default."""
        b = _make_backend()
        self.assertIn("OPENCODE_CONFIG_CONTENT", b._sidecar_env)
        config = json.loads(b._sidecar_env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["permission"]["bash"]["*"], "ask")

    def test_init_merges_with_existing_sidecar_env(self):
        """__init__ merges injection with other sidecar_env vars."""
        b = _make_backend(sidecar_env={"COOP_ROLE": "owner"})
        self.assertEqual(b._sidecar_env["COOP_ROLE"], "owner")
        self.assertIn("OPENCODE_CONFIG_CONTENT", b._sidecar_env)

    def test_init_respects_user_catchall_in_sidecar_env(self):
        """__init__ does not overwrite user's explicit '*' catch-all."""
        user_config = json.dumps({"permission": {"bash": {"*": "allow"}}})
        b = _make_backend(sidecar_env={"OPENCODE_CONFIG_CONTENT": user_config})
        stored = json.loads(b._sidecar_env["OPENCODE_CONFIG_CONTENT"])
        # Must not have been changed — user chose "allow"
        self.assertEqual(stored["permission"]["bash"]["*"], "allow")

    def test_init_raises_on_malformed_opencode_config_content(self):
        """__init__ raises ValueError immediately if OPENCODE_CONFIG_CONTENT is invalid JSON."""
        with self.assertRaises(ValueError):
            _make_backend(sidecar_env={"OPENCODE_CONFIG_CONTENT": "{bad json"})


class TestOpenCodeBackendTypicalSessionRetentionDays(unittest.TestCase):
    """docs/design/dynamic-watcher-design.md: OpenCode has no automatic session expiry."""

    def test_returns_none(self):
        backend = _make_backend()
        self.assertIsNone(backend.typical_session_retention_days())


class TestDurableInstructionsAreReclaimedOnExpiry(unittest.IsolatedAsyncioTestCase):
    """`ensure_durable_instructions` writes `RUNTIME_DIR/system-prompts/<key>.md`;
    expiry calls `reclaim_durable_instructions` to remove it. OpenCode had no
    override, so the base no-op ran and every expired OpenCode watcher left its
    prompt file — identity and context included — behind for good (Codex, PR
    #140 round 2). Mirrors `ClaudeBackend`: same directory, same containment
    check, idempotent."""

    async def test_the_file_is_removed_and_a_second_call_is_fine(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        import gateway.agents.opencode.adapter as mod

        runtime = Path(tempfile.mkdtemp())
        (runtime / "system-prompts").mkdir()
        (runtime / "system-prompts" / "abc123.md").write_text("durable")

        with patch.object(mod, "RUNTIME_DIR", runtime):
            await _make_backend().reclaim_durable_instructions("abc123")
            self.assertFalse((runtime / "system-prompts" / "abc123.md").exists())
            await _make_backend().reclaim_durable_instructions("abc123")   # idempotent

    async def test_a_key_that_escapes_the_directory_is_refused(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        import gateway.agents.opencode.adapter as mod

        runtime = Path(tempfile.mkdtemp())
        (runtime / "system-prompts").mkdir()
        with patch.object(mod, "RUNTIME_DIR", runtime):
            with self.assertRaises(Exception):
                await _make_backend().reclaim_durable_instructions("../escape")

