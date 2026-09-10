"""Unit tests for ClaudeBackend (claude CLI adapter).

Focuses on the subprocess I/O contract:
  - stdin must receive the full prompt bytes
  - stdout is parsed as stream-json into an AgentResponse
  - stderr is captured for error reporting
  - timeout kills the process and propagates TimeoutError
"""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.agents.claude.adapter import ClaudeBackend
from gateway.agents.errors import AgentRateLimitedError
from gateway.agents.response import AgentResponse

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_backend(**kwargs) -> ClaudeBackend:
    defaults = dict(command="claude", new_session_args=[], timeout=30)
    defaults.update(kwargs)
    return ClaudeBackend(**defaults)


def _stream_json_output(text: str, session_id: str = "sess-abc") -> bytes:
    """Build minimal stream-json output: one assistant event + one result event."""
    lines = [
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": text}]},
            }
        ),
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "session_id": session_id,
                "is_error": False,
                "result": text,
            }
        ),
    ]
    return "\n".join(lines).encode()


def _make_reader(data: bytes) -> MagicMock:
    """Synchronously build a mock StreamReader that emits lines then EOF.

    Supports two consumption patterns:
      - readline(): returns each line in turn, then b"" for EOF.
      - read(n):    returns the full data on the first call, then b"" for EOF.

    This mirrors asyncio.StreamReader semantics.  readline() is used by the
    stdout incremental parser; read(n) is used by the bounded stderr reader.
    """
    lines: list[bytes] = [line + b"\n" for line in data.splitlines()] + [b""]
    read_chunks: list[bytes] = [data, b""] if data else [b""]
    reader = MagicMock()
    reader.readline = AsyncMock(side_effect=lines)
    reader.read = AsyncMock(side_effect=read_chunks)
    return reader


def _make_stdin() -> MagicMock:
    """Build a mock StreamWriter for stdin."""
    stdin = MagicMock()
    stdin.write = MagicMock()  # synchronous write()
    stdin.drain = AsyncMock()
    stdin.close = MagicMock()
    stdin.wait_closed = AsyncMock()
    return stdin


def _make_mock_process(
    stdout_bytes: bytes = b"",
    stderr_bytes: bytes = b"",
    returncode: int = 0,
) -> MagicMock:
    """Return a mock asyncio.Process whose streams yield the given bytes."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdin = _make_stdin()
    proc.stdout = _make_reader(stdout_bytes)
    proc.stderr = _make_reader(stderr_bytes)
    proc.wait = AsyncMock(return_value=returncode)
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    return proc


# ── send(): stdin receives the prompt ─────────────────────────────────────────


class TestClaudeBackendStdin(unittest.IsolatedAsyncioTestCase):
    """The prompt must be written to stdin; missing it causes an empty response."""

    async def test_send_writes_prompt_to_stdin(self):
        """send() must write the full prompt bytes to proc.stdin."""
        backend = _make_backend()
        prompt = "Hello, Claude!"
        proc = _make_mock_process(stdout_bytes=_stream_json_output("Hi there!"))

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            response = await backend.send(
                session_id="sess-1",
                prompt=prompt,
                working_directory="/tmp",
                timeout=10,
            )

        # stdin.write must have been called with the prompt encoded as bytes
        proc.stdin.write.assert_called_once_with(prompt.encode())
        # stdin must be closed to signal EOF to the subprocess
        proc.stdin.close.assert_called_once()
        self.assertEqual(response.text, "Hi there!")

    async def test_send_stdin_closed_after_write(self):
        """stdin.close() and wait_closed() must be called so the subprocess sees EOF."""
        backend = _make_backend()
        proc = _make_mock_process(stdout_bytes=_stream_json_output("pong"))

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            await backend.send("sess-x", "ping", "/tmp", timeout=10)

        proc.stdin.close.assert_called_once()
        proc.stdin.wait_closed.assert_called_once()


# ── send(): stdout parsing ─────────────────────────────────────────────────────


class TestClaudeBackendResponseParsing(unittest.IsolatedAsyncioTestCase):
    """stream-json stdout must be parsed into a correct AgentResponse."""

    async def test_send_returns_agent_response(self):
        proc = _make_mock_process(
            stdout_bytes=_stream_json_output("Hello world", session_id="sess-42")
        )

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            response = await _make_backend().send("sess-42", "hi", "/tmp", timeout=10)

        self.assertIsInstance(response, AgentResponse)
        self.assertEqual(response.text, "Hello world")
        self.assertEqual(response.session_id, "sess-42")
        self.assertFalse(response.is_error)

    async def test_send_empty_stdout_returns_fallback_text(self):
        """When stdout is empty, _parse_response returns the fallback placeholder."""
        proc = _make_mock_process(stdout_bytes=b"")

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            response = await _make_backend().send("sess-z", "?", "/tmp", timeout=10)

        self.assertEqual(response.text, "(empty response)")

    async def test_send_multi_line_assistant_blocks_concatenated(self):
        """Multiple assistant content blocks should all appear in the response text."""
        lines = [
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "line one"}]},
                }
            ),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "line two"}]},
                }
            ),
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "session_id": "s",
                    "is_error": False,
                }
            ),
        ]
        proc = _make_mock_process(stdout_bytes="\n".join(lines).encode())

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            response = await _make_backend().send("s", "q", "/tmp", timeout=10)

        self.assertIn("line one", response.text)
        self.assertIn("line two", response.text)


# ── send(): error handling ─────────────────────────────────────────────────────


class TestClaudeBackendErrors(unittest.IsolatedAsyncioTestCase):
    async def test_non_zero_exit_raises_runtime_error(self):
        """A non-zero returncode must raise RuntimeError containing stderr content."""
        proc = _make_mock_process(
            stdout_bytes=b"",
            stderr_bytes=b"something went wrong",
            returncode=1,
        )

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with self.assertRaises(RuntimeError) as ctx:
                await _make_backend().send("s", "hi", "/tmp", timeout=10)

        self.assertIn("something went wrong", str(ctx.exception))

    async def test_non_zero_exit_classifies_rate_limit_errors(self):
        """Claude usage-limit failures should surface as AgentRateLimitedError."""
        error_line = json.dumps(
            {
                "type": "result",
                "subtype": "error",
                "is_error": True,
                "result": "Usage limit reached for this account",
            }
        ).encode()
        proc = _make_mock_process(stdout_bytes=error_line, returncode=1)

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with self.assertRaises(AgentRateLimitedError):
                await _make_backend().send("s", "hi", "/tmp", timeout=10)

    async def test_timeout_kills_process_and_raises(self):
        """asyncio.TimeoutError must send SIGTERM (then SIGKILL if needed) and re-raise.

        When ``proc.wait()`` resolves immediately (process exited cleanly after
        SIGTERM), SIGKILL must *not* be sent — graceful shutdown is complete.
        """
        stdin = _make_stdin()

        # stdout/stderr readers that block forever (simulate a hung subprocess)
        async def _hang():
            await asyncio.sleep(9999)

        stdout_reader = MagicMock()
        stdout_reader.readline = AsyncMock(side_effect=_hang)
        stdout_reader.read = AsyncMock(return_value=b"")
        stderr_reader = MagicMock()
        stderr_reader.readline = AsyncMock(side_effect=_hang)
        stderr_reader.read = AsyncMock(return_value=b"")

        proc = MagicMock()
        proc.stdin = stdin
        proc.stdout = stdout_reader
        proc.stderr = stderr_reader
        proc.returncode = None
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        # proc.wait() returns immediately — process exited cleanly after SIGTERM
        proc.wait = AsyncMock(return_value=-15)

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with self.assertRaises(asyncio.TimeoutError):
                await _make_backend().send("s", "slow", "/tmp", timeout=0.01)

        # SIGTERM must be sent first.
        proc.terminate.assert_called_once()
        # proc.wait() resolves immediately → process exited cleanly → no SIGKILL needed.
        proc.kill.assert_not_called()
        # proc.wait() must be called at least once in the cleanup branch.
        proc.wait.assert_called()


# ── create_session(): stdin receives the init prompt ──────────────────────────


class TestClaudeBackendCreateSession(unittest.IsolatedAsyncioTestCase):
    async def test_create_session_writes_init_prompt_to_stdin(self):
        """create_session() must pass the init prompt via stdin (not as a CLI arg)."""
        backend = _make_backend()
        session_output = json.dumps({"session_id": "new-sess-001"}).encode()

        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(session_output, b""))

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            session_id = await backend.create_session(working_directory="/tmp")

        self.assertEqual(session_id, "new-sess-001")
        # communicate() must be called with the init prompt as the `input` keyword arg.
        proc.communicate.assert_called_once()
        _, kwargs = proc.communicate.call_args
        init_bytes = kwargs.get("input")
        self.assertIsNotNone(
            init_bytes, "communicate() must be called with input=<prompt bytes>"
        )
        self.assertIn(b"Chat session initialized", init_bytes)


if __name__ == "__main__":
    unittest.main()


# ── Appended from test_round10_fixes.py ───────────────────────────────────────


class TestClaudeAdapterCancelledError(unittest.IsolatedAsyncioTestCase):
    """send() and create_session() must kill the subprocess on CancelledError."""

    def _make_backend(self):
        from gateway.agents.claude.adapter import ClaudeBackend

        return ClaudeBackend(
            command="claude",
            new_session_args=[],
            timeout=60,
        )

    def _make_proc(self):
        """Return a mock subprocess whose streams do nothing."""
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdin.close = MagicMock()
        proc.stdin.wait_closed = AsyncMock()
        proc.stdout = MagicMock()
        proc.stdout.readline = AsyncMock(return_value=b"")
        proc.stdout.read = AsyncMock(return_value=b"")
        proc.stderr = MagicMock()
        proc.stderr.read = AsyncMock(return_value=b"")
        proc.stderr.readline = AsyncMock(return_value=b"")
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        proc.wait = AsyncMock(return_value=0)
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        proc.pid = 12345
        return proc

    async def test_send_kills_subprocess_on_cancelled_error(self):
        """CancelledError inside send() must send SIGTERM then escalate to SIGKILL.

        Because ``asyncio.wait_for`` is fully stubbed to always raise, the inner
        ``proc.wait()`` inside ``_terminate_gracefully`` also raises, triggering
        the SIGKILL escalation path.
        """
        backend = self._make_backend()
        proc = self._make_proc()

        async def _wait_for_that_raises(*args, **kwargs):
            raise asyncio.CancelledError()

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with patch("asyncio.wait_for", side_effect=_wait_for_that_raises):
                with self.assertRaises(asyncio.CancelledError):
                    await backend.send(
                        session_id="sess-1",
                        prompt="hello",
                        working_directory="/tmp",
                        timeout=60,
                    )

        proc.terminate.assert_called_once()
        proc.kill.assert_called()

    async def test_create_session_kills_subprocess_on_cancelled_error(self):
        """CancelledError inside create_session() must send SIGTERM then SIGKILL."""
        backend = self._make_backend()
        proc = self._make_proc()

        async def _wait_for_that_raises(*args, **kwargs):
            raise asyncio.CancelledError()

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with patch("asyncio.wait_for", side_effect=_wait_for_that_raises):
                with self.assertRaises(asyncio.CancelledError):
                    await backend.create_session(working_directory="/tmp")

        proc.terminate.assert_called_once()
        proc.kill.assert_called()

    async def test_send_timeout_still_kills_subprocess(self):
        """TimeoutError path must send SIGTERM then escalate to SIGKILL (regression guard).

        Because ``asyncio.wait_for`` is fully stubbed, the inner grace-period
        ``proc.wait()`` also times out, triggering the SIGKILL escalation path.
        """
        backend = self._make_backend()
        proc = self._make_proc()

        async def _wait_for_that_raises(*args, **kwargs):
            raise asyncio.TimeoutError()

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with patch("asyncio.wait_for", side_effect=_wait_for_that_raises):
                with self.assertRaises(asyncio.TimeoutError):
                    await backend.send(
                        session_id="sess-1",
                        prompt="hello",
                        working_directory="/tmp",
                        timeout=60,
                    )

        proc.terminate.assert_called_once()
        proc.kill.assert_called()


# ── append_system_prompt_file: durable system prompt (issue #52) ────────────


def _cmd_from_call(call) -> list[str]:
    """Extract the argv list from a asyncio.create_subprocess_exec(...) call."""
    return list(call.args)


class TestClaudeBackendAppendSystemPromptFileFlag(unittest.IsolatedAsyncioTestCase):
    """send()/stream() must add --append-system-prompt-file <path> to the CLI
    command when the kwarg is set, and omit it entirely when it is None."""

    async def test_send_includes_flag_when_set(self):
        backend = _make_backend()
        proc = _make_mock_process(stdout_bytes=_stream_json_output("ok"))

        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await backend.send(
                session_id="sess-1",
                prompt="hi",
                working_directory="/tmp",
                timeout=10,
                append_system_prompt_file="/tmp/.acg-system-prompt/w.md",
            )

        cmd = _cmd_from_call(mock_exec.call_args)
        self.assertIn("--append-system-prompt-file", cmd)
        idx = cmd.index("--append-system-prompt-file")
        self.assertEqual(cmd[idx + 1], "/tmp/.acg-system-prompt/w.md")

    async def test_send_omits_flag_when_none(self):
        backend = _make_backend()
        proc = _make_mock_process(stdout_bytes=_stream_json_output("ok"))

        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await backend.send(
                session_id="sess-1",
                prompt="hi",
                working_directory="/tmp",
                timeout=10,
            )

        cmd = _cmd_from_call(mock_exec.call_args)
        self.assertNotIn("--append-system-prompt-file", cmd)

    async def test_stream_includes_flag_when_set(self):
        backend = _make_backend()
        proc = _make_mock_process(stdout_bytes=_stream_json_output("ok"))

        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            events = []
            async for event in backend.stream(
                session_id="sess-1",
                prompt="hi",
                working_directory="/tmp",
                timeout=10,
                append_system_prompt_file="/tmp/.acg-system-prompt/w.md",
            ):
                events.append(event)

        cmd = _cmd_from_call(mock_exec.call_args)
        self.assertIn("--append-system-prompt-file", cmd)
        idx = cmd.index("--append-system-prompt-file")
        self.assertEqual(cmd[idx + 1], "/tmp/.acg-system-prompt/w.md")

    async def test_stream_omits_flag_when_none(self):
        backend = _make_backend()
        proc = _make_mock_process(stdout_bytes=_stream_json_output("ok"))

        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            events = []
            async for event in backend.stream(
                session_id="sess-1",
                prompt="hi",
                working_directory="/tmp",
                timeout=10,
            ):
                events.append(event)

        cmd = _cmd_from_call(mock_exec.call_args)
        self.assertNotIn("--append-system-prompt-file", cmd)


class TestClaudeBackendEnsureDurableInstructions(unittest.IsolatedAsyncioTestCase):
    """ensure_durable_instructions() writes content to a stable per-watcher
    file and always returns its path — regardless of already_delivered — since
    writing has no side effect to avoid repeating (regression test for design
    review blocker #1: a resumed session must still get a non-None path).

    Written under RUNTIME_DIR (AgentCoop's own state directory), NOT under
    working_directory — a real user project directory could be under git
    version control, and a generated file living there risks an accidental
    `git add`. Tests patch the module-local RUNTIME_DIR constant (same
    convention as tests/unit/test_daemon.py, test_control_server.py, etc.)
    rather than relying on the real ~/.agentcoop. `working_directory`
    is passed as an arbitrary unused placeholder since this method no longer
    keys the path off it.
    """

    async def test_writes_file_and_returns_path(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        backend = _make_backend()
        with tempfile.TemporaryDirectory() as tmp:
            with patch("gateway.agents.claude.adapter.RUNTIME_DIR", Path(tmp)):
                path = await backend.ensure_durable_instructions(
                    "sess-1", "/unused", 10, "## Coop Session Identity\nhello",
                    path_key="my-watcher", already_delivered=False,
                )

            self.assertEqual(path, str(Path(tmp) / "system-prompts" / "my-watcher.md"))

    async def test_writes_file_content_correctly(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        backend = _make_backend()
        with tempfile.TemporaryDirectory() as tmp:
            with patch("gateway.agents.claude.adapter.RUNTIME_DIR", Path(tmp)):
                content = "## Coop Session Identity\nhello world"
                path = await backend.ensure_durable_instructions(
                    "sess-1", "/unused", 10, content,
                    path_key="my-watcher", already_delivered=False,
                )
                self.assertEqual(Path(path).read_text(), content)
                self.assertEqual(Path(path), Path(tmp) / "system-prompts" / "my-watcher.md")

    async def test_returns_non_none_path_when_already_delivered_true(self):
        """Resumed session (already_delivered=True) must still get a fresh path."""
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        backend = _make_backend()
        with tempfile.TemporaryDirectory() as tmp:
            with patch("gateway.agents.claude.adapter.RUNTIME_DIR", Path(tmp)):
                path = await backend.ensure_durable_instructions(
                    "sess-1", "/unused", 10, "content",
                    path_key="my-watcher", already_delivered=True,
                )

            self.assertIsNotNone(path)

    async def test_returns_non_none_path_when_already_delivered_false(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        backend = _make_backend()
        with tempfile.TemporaryDirectory() as tmp:
            with patch("gateway.agents.claude.adapter.RUNTIME_DIR", Path(tmp)):
                path = await backend.ensure_durable_instructions(
                    "sess-1", "/unused", 10, "content",
                    path_key="my-watcher", already_delivered=False,
                )

            self.assertIsNotNone(path)

    async def test_overwrites_existing_file_with_fresh_content(self):
        """Writing is idempotent — a second call with different content overwrites."""
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        backend = _make_backend()
        with tempfile.TemporaryDirectory() as tmp:
            with patch("gateway.agents.claude.adapter.RUNTIME_DIR", Path(tmp)):
                await backend.ensure_durable_instructions(
                    "sess-1", "/unused", 10, "old content",
                    path_key="my-watcher", already_delivered=False,
                )
                path = await backend.ensure_durable_instructions(
                    "sess-1", "/unused", 10, "new content",
                    path_key="my-watcher", already_delivered=True,
                )
                self.assertEqual(Path(path).read_text(), "new content")

    async def test_write_is_atomic_no_leftover_temp_files(self):
        """The write goes through a temp-file-then-rename so a concurrently
        running `claude` subprocess never observes a partial write — verified
        here by checking no .tmp artifact survives a successful write."""
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        backend = _make_backend()
        with tempfile.TemporaryDirectory() as tmp:
            with patch("gateway.agents.claude.adapter.RUNTIME_DIR", Path(tmp)):
                path = await backend.ensure_durable_instructions(
                    "sess-1", "/unused", 10, "content",
                    path_key="my-watcher", already_delivered=False,
                )
                acg_dir = Path(tmp) / "system-prompts"
                entries = list(acg_dir.iterdir())

            self.assertEqual(entries, [Path(path)], f"unexpected leftover files: {entries}")

    async def test_written_outside_a_given_working_directory(self):
        """The file must NOT live under working_directory at all — regression
        test for the git-accidental-commit concern: working_directory can be
        a real user project under version control (see docs/user-guide.md's
        `working_directory: ~/my-project` examples), and RUNTIME_DIR must
        never be a subpath of it."""
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        backend = _make_backend()
        with tempfile.TemporaryDirectory() as fake_project, tempfile.TemporaryDirectory() as runtime_dir:
            with patch("gateway.agents.claude.adapter.RUNTIME_DIR", Path(runtime_dir)):
                path = await backend.ensure_durable_instructions(
                    "sess-1", fake_project, 10, "content",
                    path_key="my-watcher", already_delivered=False,
                )

            self.assertFalse(
                Path(path).resolve().is_relative_to(Path(fake_project).resolve()),
                f"durable system-prompt file must not live under working_directory, got {path}",
            )


class TestClaudeBackendTypicalSessionRetentionDays(unittest.TestCase):
    """docs/design/dynamic-watcher-design.md: Claude Code's default cleanupPeriodDays."""

    def test_returns_30(self):
        backend = _make_backend()
        self.assertEqual(backend.typical_session_retention_days(), 30)
