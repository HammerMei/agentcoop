"""Claude CLI agent backend.

Implements AgentBackend using the Claude CLI subprocess:
  - Session creation:  claude -p --output-format json [extra_args]  (prompt via stdin)
  - Message sending:   claude -p --resume <session-id> --output-format stream-json --verbose  (prompt via stdin)

Prompts are passed via stdin to avoid OS ARG_MAX limits for large messages.

Output parsing understands Claude's stream-json format: one JSON object per line,
with type="assistant" content blocks and a type="result" fallback.

Note: claude -p does not support native file attachments.  When attachments are
provided, their paths are injected into the prompt text via
:func:`~gateway.core.adapter_utils.build_attachment_prompt` so the agent can
access them using the Read tool.

Durable system-prompt files (see :meth:`ClaudeBackend.ensure_durable_instructions`)
are written under ``RUNTIME_DIR`` (``~/.agent-chat-gateway``), NOT under a
watcher's ``working_directory`` — that directory can be (and per
``docs/user-guide.md`` examples, often is) a real user project under git
version control, and a generated file living there risks being accidentally
`git add`-ed. ``RUNTIME_DIR`` is ACG's own dedicated state directory (already
used by ``state.json``, ``gateway.pid``, etc. — see ``gateway/runtime_lock.py``),
never inside any user-controlled repo.
"""

import asyncio
import json
import logging
import os
import tempfile
from collections import deque
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING

from ...core.adapter_utils import build_attachment_prompt
from ...core.paths import resolve_under
from ...paths import RUNTIME_DIR
from .. import AgentBackend, GatewayBrokerConfig
from ..errors import (
    AgentExecutionError,
    AgentPermissionError,
    AgentProtocolError,
    AgentRateLimitedError,
    AgentUnavailableError,
)
from ..response import AgentEvent, AgentResponse, TokenUsage

if TYPE_CHECKING:
    from ...core.permission import (
        PermissionBroker,
        PermissionNotifier,
        PermissionRegistry,
    )

logger = logging.getLogger("coop.agents.claude")

# Bound as a module attribute so tests patch THIS module's RUNTIME_DIR (see
# tests/unit/test_claude_adapter.py); the value itself has one home, gateway/paths.py.

_RATE_LIMIT_PATTERNS = (
    "usage limit",
    "rate limit",
    "quota exceeded",
    "quota reached",
    "too many requests",
)
_PERMISSION_PATTERNS = (
    "permission denied",
    "not permitted",
    "approval required",
    "blocked as safe default",
)
_UNAVAILABLE_PATTERNS = (
    "temporarily unavailable",
    "service unavailable",
    "backend unavailable",
)


def _classify_claude_error(
    message: str, subtype: str = ""
) -> type[AgentExecutionError]:
    """Map raw Claude CLI error text to a structured backend exception type."""
    haystack = f"{subtype}\n{message}".lower()
    if any(pattern in haystack for pattern in _RATE_LIMIT_PATTERNS):
        return AgentRateLimitedError
    if any(pattern in haystack for pattern in _PERMISSION_PATTERNS):
        return AgentPermissionError
    if any(pattern in haystack for pattern in _UNAVAILABLE_PATTERNS):
        return AgentUnavailableError
    if "malformed" in haystack or "parse" in haystack:
        return AgentProtocolError
    return AgentExecutionError


# Grace period given to the Claude subprocess after SIGTERM before escalating
# to SIGKILL.  Three seconds is generous enough for claude to flush state yet
# short enough that a stuck process does not block the gateway for long.
_SIGTERM_GRACE_SECONDS: float = 3.0

# Maximum bytes of stderr to accumulate for diagnostics.  Keeps memory
# bounded when the agent writes verbose debug output to stderr.
_MAX_STDERR_BYTES = 65_536

# Maximum chars of raw stdout to keep for diagnostic logging when no text
# is extracted.  Only used for the "(empty response)" warning path.
_MAX_RAW_PREVIEW_CHARS = 500

# Keep the tail of the stream, not just the head.  Claude's final failure
# details often appear in the last ``result`` event rather than the initial
# ``system/init`` event.
_MAX_RAW_TAIL_LINES = 20


def _parse_intermediate_events(line: str) -> list[AgentEvent]:
    """Extract zero or more intermediate :class:`AgentEvent` objects from one stream-json line.

    Recognises tool-use invocations and extended-thinking blocks emitted by
    ``type="assistant"`` events.  All other event types (text blocks, result,
    user/tool-result, system, etc.) return an empty list — they are handled
    separately by :class:`_StreamParser`.

    Args:
        line: A single raw line from Claude's ``stream-json`` stdout.

    Returns:
        A (possibly empty) list of intermediate :class:`AgentEvent` objects.
    """
    # Fast-path: only assistant events carry tool_use / thinking blocks.
    # Skipping JSON parsing for result / user / system lines avoids the
    # allocation cost on large tool-result payloads (file contents, command
    # output) which can be many kilobytes per line.
    if '"assistant"' not in line:
        return []

    try:
        event = json.loads(line.strip())
    except (json.JSONDecodeError, ValueError):
        return []

    if event.get("type") != "assistant":
        return []

    events: list[AgentEvent] = []
    for block in event.get("message", {}).get("content", []):
        block_type = block.get("type")
        if block_type == "tool_use":
            tool_name = block.get("name", "tool")
            events.append(AgentEvent(kind="tool_call", text=f"🔧 {tool_name}"))
        elif block_type == "thinking":
            thinking = block.get("thinking", "").strip()
            if thinking:
                preview = thinking[:80]
                if len(thinking) > 80:
                    preview += "..."
                events.append(AgentEvent(kind="thinking", text=f"💭 {preview}"))
    return events


async def _terminate_gracefully(proc: asyncio.subprocess.Process) -> None:
    """Signal *proc* with SIGTERM, then escalate to SIGKILL after the grace period.

    Design goals
    ------------
    * ``proc.terminate()`` is a synchronous OS call — it runs even if the
      surrounding coroutine is about to be cancelled, giving the subprocess a
      chance to flush buffers and exit cleanly.
    * We then *await* ``proc.wait()`` for at most :data:`_SIGTERM_GRACE_SECONDS`.
      If cancelled or the timer fires we escalate to SIGKILL.
    * All exceptions are swallowed — cleanup must never propagate to the caller.
    """
    try:
        proc.terminate()
    except ProcessLookupError:
        return  # process already exited — nothing to do
    except Exception:
        pass  # e.g. permission error on an unusual platform

    try:
        await asyncio.wait_for(proc.wait(), timeout=_SIGTERM_GRACE_SECONDS)
        return  # exited cleanly after SIGTERM
    except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
        pass  # grace period elapsed or outer task cancelled — escalate

    try:
        proc.kill()
    except Exception:
        pass  # already dead or unrecoverable


def _atomic_write_text(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically (temp file + ``os.replace``).

    Without this, a ``claude`` subprocess started concurrently with a write
    (e.g. a message arriving mid-update) could read a partially-written
    ``--append-system-prompt-file`` and get truncated/corrupt content instead
    of either the old or the new version. The temp file is created in the
    same directory so ``os.replace`` stays on one filesystem (required for
    atomicity) and is cleaned up on any failure before the replace.
    """
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class _StreamParser:
    """Incremental parser for Claude's ``stream-json`` output.

    Consumes one JSON line at a time via :meth:`feed_line`, accumulating only
    the parsed fields (text parts, usage metadata, etc.) — *not* the raw bytes.
    This keeps peak memory proportional to the agent's response text rather than
    the full stdout payload (which can be much larger due to verbose JSON framing).
    """

    __slots__ = (
        "text_parts",
        "session_id",
        "cost_usd",
        "duration_ms",
        "num_turns",
        "is_error",
        "usage",
        "_raw_preview",
        "_raw_tail_lines",
        "result_subtype",
        "result_text",
    )

    def __init__(self) -> None:
        self.text_parts: list[str] = []
        self.session_id: str | None = None
        self.cost_usd: float | None = None
        self.duration_ms: int | None = None
        self.num_turns: int | None = None
        self.is_error: bool = False
        self.usage: TokenUsage | None = None
        # Bounded preview of raw lines — only used for diagnostic logging when
        # no text is extracted.
        self._raw_preview: str = ""
        # Tail of raw lines for failure diagnostics.  Keeping the end of the
        # stream is often more useful than the beginning because the terminal
        # ``result`` event frequently carries the actual error.
        self._raw_tail_lines: deque[str] = deque(maxlen=_MAX_RAW_TAIL_LINES)
        self.result_subtype: str = ""
        self.result_text: str = ""

    def feed_line(self, line: str) -> None:
        """Parse a single JSON line and accumulate into internal state."""
        line = line.strip()
        if not line:
            return
        if len(self._raw_preview) < _MAX_RAW_PREVIEW_CHARS:
            self._raw_preview += line + "\n"
            if len(self._raw_preview) > _MAX_RAW_PREVIEW_CHARS:
                self._raw_preview = self._raw_preview[:_MAX_RAW_PREVIEW_CHARS]
        self._raw_tail_lines.append(line)
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return

        if event.get("type") == "assistant":
            message = event.get("message", {})
            for block in message.get("content", []):
                if block.get("type") == "text":
                    self.text_parts.append(block["text"])

        elif event.get("type") == "result":
            subtype = event.get("subtype", "")
            self.is_error = bool(event.get("is_error", False))
            self.session_id = event.get("session_id") or None
            self.cost_usd = event.get("total_cost_usd") or None
            self.duration_ms = event.get("duration_ms") or None
            self.num_turns = event.get("num_turns") or None
            self.result_subtype = subtype
            self.result_text = event.get("result", "") or ""

            raw_usage = event.get("usage") or {}
            if raw_usage:
                self.usage = TokenUsage(
                    input_tokens=raw_usage.get("input_tokens", 0),
                    output_tokens=raw_usage.get("output_tokens", 0),
                    cache_read_tokens=raw_usage.get("cache_read_input_tokens", 0),
                    cache_write_tokens=raw_usage.get("cache_creation_input_tokens", 0),
                )

            if self.is_error or subtype not in ("success", ""):
                logger.warning(
                    "Agent result non-success: subtype=%r is_error=%s — "
                    "if tool access was denied, consider adding it to allowedTools in settings.json. "
                    "Result preview: %s",
                    subtype,
                    self.is_error,
                    event.get("result", "")[:300],
                )

            # Fallback text from result event if no assistant blocks found
            result_text = event.get("result", "")
            if result_text and not self.text_parts:
                self.text_parts.append(result_text)

    @property
    def raw_preview(self) -> str:
        """Bounded preview of the first raw lines for diagnostic logging."""
        return self._raw_preview

    @property
    def raw_tail_preview(self) -> str:
        return "\n".join(self._raw_tail_lines)

    def build_response(self) -> AgentResponse:
        """Build the final :class:`AgentResponse` from accumulated state."""
        text = "\n".join(self.text_parts).strip()
        if not text:
            logger.warning(
                "No text extracted from stream. Raw preview: %s",
                self._raw_preview[:_MAX_RAW_PREVIEW_CHARS],
            )
            text = "(empty response)"
        return AgentResponse(
            text=text,
            session_id=self.session_id,
            usage=self.usage,
            cost_usd=self.cost_usd,
            duration_ms=self.duration_ms,
            num_turns=self.num_turns,
            is_error=self.is_error,
        )


class ClaudeBackend(AgentBackend):
    """Agent backend that drives the Claude CLI subprocess."""

    def __init__(
        self,
        command: str,
        new_session_args: list[str],
        timeout: int,
        broker_config: GatewayBrokerConfig | None = None,
    ):
        self.command = command
        self.new_session_args = new_session_args
        self.timeout = timeout
        self.settings_path: str = (
            ""  # patched by ClaudePermissionBroker.start() when active
        )
        self._broker_config = broker_config

    def typical_session_retention_days(self) -> int | None:
        """Claude Code's default ``cleanupPeriodDays`` (settings.json) is 30 —
        JSONL session transcripts older than this are deleted at CLI startup.
        This is a documented default, not a guaranteed constant: a user can
        change it per-machine, and ACG has no way to read the actual configured
        value from here (see docs/design/dynamic-watcher-design.md's open items).
        """
        return 30

    def create_gateway_broker(
        self,
        registry: "PermissionRegistry",
        notifier: "PermissionNotifier",
        session_room_map: dict[str, str],
        session_role_map: dict[str, str],
        session_permission_thread_map: "dict[str, str | None]",
    ) -> "PermissionBroker | None":
        """Return a ClaudePermissionBroker wired to the shared RC connector maps.

        Passes ``self`` as ``backend`` (Option C) so the broker patches
        ``self.settings_path`` during ``start()`` — GatewayService never needs
        to know about this Claude-internal detail.
        """
        if self._broker_config is None:
            return None
        from .broker import ClaudePermissionBroker

        return ClaudePermissionBroker(
            registry=registry,
            notifier=notifier,
            session_room_map=session_room_map,
            session_role_map=session_role_map,
            session_permission_thread_map=session_permission_thread_map,
            owner_allowed_tools=self._broker_config.owner_allowed_tools,
            guest_allowed_tools=self._broker_config.guest_allowed_tools,
            timeout_seconds=self._broker_config.timeout,
            skip_owner_approval=self._broker_config.skip_owner_approval,
            backend=self,
        )

    def create_callable_broker(self, handler, timeout_seconds: int):
        """Return a CallablePermissionBroker that uses Claude's PreToolUse HTTP hook."""
        from .callable_broker import CallablePermissionBroker

        return CallablePermissionBroker(handler, timeout_seconds=timeout_seconds)

    def attach_callable_broker(self, broker: object) -> None:
        """Patch ``settings_path`` so ``create_session()`` uses the broker's hook URL.

        Claude needs the ``--settings`` flag to route tool calls through the
        broker's HTTP server.  This is a Claude-internal detail that does not
        belong in AgentSession — the generic wrapper only calls this hook.
        """
        sp = getattr(broker, "settings_path", "")
        if sp:
            self._pre_broker_settings_path = self.settings_path
            self.settings_path = sp

    def detach_callable_broker(self) -> None:
        """Restore ``settings_path`` to its pre-broker value."""
        if hasattr(self, "_pre_broker_settings_path"):
            self.settings_path = self._pre_broker_settings_path
            del self._pre_broker_settings_path

    async def ensure_durable_instructions(
        self,
        session_id: str,
        working_directory: str,
        timeout: int,
        content: str,
        *,
        path_key: str,
        already_delivered: bool,
    ) -> str | None:
        """Write `content` to a stable per-watcher file for --append-system-prompt-file.

        Writing is idempotent (overwrite with identical content) — no side
        effect to avoid repeating, so already_delivered is irrelevant here;
        always write fresh so a resumed session after a gateway restart still
        gets a correct, current file.

        The write itself is atomic (see ``_atomic_write_text``) — a ``claude``
        subprocess reading this file concurrently with an update always sees
        either the old or the new content in full, never a partial write.

        Deliberately written under ``RUNTIME_DIR`` (ACG's own state directory),
        NOT under ``working_directory`` — Claude reads this file via a plain
        CLI flag at startup, before any tool-permission sandboxing applies, so
        it is not restricted to the project directory (verified empirically:
        --append-system-prompt-file works with an absolute path outside any
        project). Putting it under working_directory would risk an
        accidental `git add` if that directory is a real user project under
        version control (a documented, real configuration — see
        docs/user-guide.md's ``working_directory: ~/my-project`` examples).
        The file is named by ``path_key``, which the caller derives — treat it as opaque.
        It is scoped to the *watcher in a room*, not to the room alone. Two watchers on one
        connector+room are refused now (§4.1), so the collision it was written for cannot
        occur; the key stays because it names files on disk and re-keying would orphan
        every existing one. The display name used to be the
        path component outright, which made it load-bearing — a rename orphaned the file
        and two rooms could collide (§2.3). ``resolve_under`` re-checks containment,
        because the key is built from external connector data and a path from such data
        is validated rather than trusted.
        """
        acg_dir = RUNTIME_DIR / "system-prompts"
        await asyncio.to_thread(acg_dir.mkdir, parents=True, exist_ok=True)
        path = resolve_under(acg_dir, f"{path_key}.md")
        await asyncio.to_thread(_atomic_write_text, path, content)
        return str(path)

    async def reclaim_durable_instructions(self, path_key: str) -> None:
        """Remove the per-watcher prompt file `ensure_durable_instructions` wrote.

        Expiry's half of that contract (§2.5): same directory, same
        `resolve_under` containment check — the key is built from external
        connector data and a path from such data is validated on the way out
        exactly as it was on the way in. Idempotent: a missing file is
        success.
        """
        path = resolve_under(RUNTIME_DIR / "system-prompts", f"{path_key}.md")
        await asyncio.to_thread(path.unlink, missing_ok=True)

    async def create_session(
        self,
        working_directory: str,
        extra_args: list[str] | None = None,
        session_title: str | None = None,
    ) -> str:
        """Create a new Claude session and return the session_id."""
        args_to_use = extra_args if extra_args is not None else self.new_session_args
        cmd = [
            self.command,
            "-p",
            "--output-format",
            "json",
            *args_to_use,
        ]
        if session_title:
            cmd += ["--name", session_title]
        if self.settings_path:
            # --dangerously-skip-permissions bypasses Claude's native permission
            # system entirely, making the HTTP hook the sole security gate.
            # This is required for the permission broker to work correctly —
            # without it, Claude's native check blocks tool calls before the
            # hook fires, causing spurious "approval required" responses.
            cmd += ["--dangerously-skip-permissions", "--settings", self.settings_path]

        init_prompt = (
            "Chat session initialized. "
            "You are a chat assistant standing by to respond to incoming messages. "
            "Do not take any proactive action — simply wait for the first user message."
        )

        # Strip CLAUDECODE so the subprocess does not inherit the parent session context
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

        logger.info("Creating new agent session (cwd=%s)", working_directory)
        logger.debug("Command: %s", " ".join(cmd))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=working_directory,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=init_prompt.encode()), timeout=self.timeout
            )
        except asyncio.TimeoutError:
            # Graceful SIGTERM first; escalates to SIGKILL after the grace period.
            await _terminate_gracefully(proc)
            # Drain and reap with a bounded timeout so we do not block forever.
            try:
                await asyncio.wait_for(proc.communicate(), timeout=5)
            except Exception:
                pass
            raise
        except asyncio.CancelledError:
            # External task cancellation — give subprocess a chance to flush
            # before escalating to SIGKILL.  _terminate_gracefully() swallows
            # any secondary CancelledError so cleanup does not re-raise.
            await _terminate_gracefully(proc)
            try:
                await asyncio.wait_for(proc.communicate(), timeout=5)
            except Exception:
                pass
            raise

        if proc.returncode != 0:
            err_text = stderr.decode().strip() if stderr else ""
            out_text = stdout.decode().strip() if stdout else ""
            logger.error(
                "Session creation failed (exit %d), stderr: %s",
                proc.returncode,
                err_text[:500] or "(empty)",
            )
            logger.error("Session creation stdout: %s", out_text[:500] or "(empty)")
            raise RuntimeError(
                f"Failed to create session: {err_text or out_text or 'unknown error'}"
            )

        try:
            data = json.loads(stdout.decode())
        except json.JSONDecodeError as exc:
            raw_out = stdout.decode().strip()
            raise RuntimeError(
                f"Failed to parse session creation output as JSON: {exc}. "
                f"Raw output (first 500 chars): {raw_out[:500]}"
            ) from exc
        session_id = data.get("session_id")
        if not session_id:
            raise RuntimeError(f"No session_id in agent output: {data}")

        logger.info("Created session: %s", session_id[:8])
        return session_id

    async def send(
        self,
        session_id: str,
        prompt: str,
        working_directory: str,
        timeout: int,
        attachments: list[str] | None = None,
        env: dict[str, str] | None = None,
        append_system_prompt_file: str | None = None,
    ) -> AgentResponse:
        """Send a message to an existing Claude session and return a normalized AgentResponse."""
        if attachments:
            logger.info(
                "Injecting %d attachment path(s) into prompt text: %s",
                len(attachments),
                attachments,
            )
            prompt = build_attachment_prompt(prompt, attachments, working_directory)

        cmd = [
            self.command,
            "-p",
            "--resume",
            session_id,
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        if self.settings_path:
            cmd += ["--dangerously-skip-permissions", "--settings", self.settings_path]
        if append_system_prompt_file:
            cmd += ["--append-system-prompt-file", append_system_prompt_file]

        # Strip CLAUDECODE so the subprocess does not inherit the parent session context,
        # then merge any role env vars (e.g. COOP_ROLE) for hook enforcement.
        process_env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        if env:
            process_env.update(env)

        logger.debug(
            "Running: %s (cwd=%s)", " ".join(cmd) + " <stdin>", working_directory
        )

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=working_directory,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=process_env,
            limit=16 * 1024 * 1024,  # 16MB — handles large tool results (e.g. base64 images)
        )

        async def _write_stdin(writer: asyncio.StreamWriter, data: bytes) -> None:
            """Write prompt to stdin and close it so the subprocess sees EOF."""
            writer.write(data)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        parser = _StreamParser()

        async def _read_and_parse_stdout(stream: asyncio.StreamReader) -> None:
            """Read stdout line-by-line and parse each line incrementally.

            Only the parsed fields (text parts, usage metadata) are kept in
            memory — the raw bytes are discarded after each line, reducing peak
            memory from O(full stdout) to O(response text + metadata).
            """
            while True:
                line = await stream.readline()
                if not line:
                    break
                parser.feed_line(line.decode(errors="replace"))

        async def _read_stderr_bounded(stream: asyncio.StreamReader) -> bytes:
            """Read stderr with a bounded buffer for diagnostics."""
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    break
                if total < _MAX_STDERR_BYTES:
                    sliced = chunk[: _MAX_STDERR_BYTES - total]
                    chunks.append(sliced)
                    total += len(sliced)
            return b"".join(chunks)

        try:
            # stdin write, stdout parse, and stderr read run concurrently.
            # Stdout is parsed incrementally — raw bytes are not accumulated.
            _, _, stderr = await asyncio.wait_for(
                asyncio.gather(
                    _write_stdin(proc.stdin, prompt.encode()),
                    _read_and_parse_stdout(proc.stdout),
                    _read_stderr_bounded(proc.stderr),
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            # Graceful SIGTERM first; escalates to SIGKILL after the grace period.
            await _terminate_gracefully(proc)
            # Close stdin so the subprocess is not waiting for more input.
            try:
                proc.stdin.close()
            except Exception:
                pass
            # Drain streams and reap the process to avoid ResourceWarning /
            # zombie processes lingering in the process table.
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        proc.stdout.read(),
                        proc.stderr.read(),
                    ),
                    timeout=5,
                )
            except Exception:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                pass
            raise
        except asyncio.CancelledError:
            # Task was cancelled externally (e.g. gateway shutdown).  Give the
            # subprocess a chance to flush before escalating to SIGKILL.
            # _terminate_gracefully() is synchronous up to proc.terminate() and
            # swallows any secondary CancelledError so cleanup does not re-raise.
            await _terminate_gracefully(proc)
            try:
                proc.stdin.close()
            except Exception:
                pass
            # Best-effort drain and reap.  These awaits may themselves be
            # interrupted by a subsequent cancellation; that is acceptable —
            # the process has already been signalled above.
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        proc.stdout.read(),
                        proc.stderr.read(),
                    ),
                    timeout=5,
                )
            except Exception:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                pass
            raise
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            logger.warning("claude process did not exit after pipe drain — killing")
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                logger.error("claude process stuck after SIGKILL — leaking process")

        if proc.returncode != 0:
            err_text = stderr.decode(errors="replace").strip() if stderr else ""
            out_preview = parser.raw_preview.strip()
            out_tail = parser.raw_tail_preview.strip()
            parsed_subtype = parser.result_subtype.strip()
            parsed_error = parser.result_text.strip()
            logger.error(
                "agent exit code %d, stderr: %s",
                proc.returncode,
                err_text[:500] or "(empty)",
            )
            logger.error("agent stdout preview: %s", out_preview[:500] or "(empty)")
            logger.error("agent stdout tail: %s", out_tail[:2000] or "(empty)")
            if parsed_subtype or parsed_error:
                logger.error(
                    "agent parsed result: subtype=%s text=%s",
                    parsed_subtype or "(empty)",
                    parsed_error[:1000] or "(empty)",
                )
            diag = (
                err_text or parsed_error or out_tail or out_preview or "unknown error"
            )
            exc_type = _classify_claude_error(diag, parsed_subtype)
            raise exc_type(
                f"agent exited with code {proc.returncode}"
                f"{f' ({parsed_subtype})' if parsed_subtype else ''}: {diag}"
            )

        return parser.build_response()

    async def stream(
        self,
        session_id: str,
        prompt: str,
        working_directory: str,
        timeout: int,
        attachments: list[str] | None = None,
        env: dict[str, str] | None = None,
        append_system_prompt_file: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Stream Claude events, yielding intermediate tool/thinking events then a final one.

        Reads Claude's ``stream-json`` stdout line-by-line.  For each line,
        :func:`_parse_intermediate_events` extracts any tool-call or thinking
        blocks and yields them immediately so the caller can forward them to the
        connector for live status updates.  Lines are also fed to
        :class:`_StreamParser` to accumulate the final :class:`AgentResponse`.

        The generator guarantees exactly one ``final`` :class:`AgentEvent` as
        its last item.  ``asyncio.TimeoutError`` and ``asyncio.CancelledError``
        are re-raised after gracefully terminating the subprocess.
        """
        if attachments:
            logger.info(
                "Injecting %d attachment path(s) into prompt text: %s",
                len(attachments),
                attachments,
            )
            prompt = build_attachment_prompt(prompt, attachments, working_directory)

        cmd = [
            self.command,
            "-p",
            "--resume",
            session_id,
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        if self.settings_path:
            cmd += ["--dangerously-skip-permissions", "--settings", self.settings_path]
        if append_system_prompt_file:
            cmd += ["--append-system-prompt-file", append_system_prompt_file]

        process_env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        if env:
            process_env.update(env)

        logger.debug(
            "Running (stream): %s (cwd=%s)", " ".join(cmd) + " <stdin>", working_directory
        )

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=working_directory,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=process_env,
            limit=16 * 1024 * 1024,  # 16MB — handles large tool results (e.g. base64 images)
        )

        # Write stdin in a background task so it does not block stdout reading.
        async def _write_stdin() -> None:
            proc.stdin.write(prompt.encode())
            await proc.stdin.drain()
            proc.stdin.close()
            await proc.stdin.wait_closed()

        # Read stderr in a background task with a bounded buffer.
        async def _read_stderr() -> bytes:
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = await proc.stderr.read(4096)
                if not chunk:
                    break
                if total < _MAX_STDERR_BYTES:
                    sliced = chunk[: _MAX_STDERR_BYTES - total]
                    chunks.append(sliced)
                    total += len(sliced)
            return b"".join(chunks)

        stdin_task: asyncio.Task[None] = asyncio.create_task(_write_stdin())
        stderr_task: asyncio.Task[bytes] = asyncio.create_task(_read_stderr())
        parser = _StreamParser()
        deadline = asyncio.get_running_loop().time() + timeout

        try:
            # Read stdout line-by-line, yielding intermediate events as they arrive.
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise asyncio.TimeoutError()
                line_bytes = await asyncio.wait_for(
                    proc.stdout.readline(), timeout=remaining
                )
                if not line_bytes:
                    break  # EOF — subprocess closed stdout
                line = line_bytes.decode(errors="replace")
                for intermediate in _parse_intermediate_events(line):
                    yield intermediate
                parser.feed_line(line)

        except (asyncio.TimeoutError, asyncio.CancelledError):
            stdin_task.cancel()
            stderr_task.cancel()
            await _terminate_gracefully(proc)
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                await asyncio.wait_for(
                    asyncio.gather(proc.stdout.read(), proc.stderr.read()),
                    timeout=5,
                )
            except Exception:
                pass
            # Reap cancelled tasks so their CancelledErrors are observed and
            # Python does not emit "Task exception was never retrieved" warnings.
            try:
                await asyncio.wait_for(
                    asyncio.gather(stdin_task, stderr_task, return_exceptions=True),
                    timeout=2,
                )
            except Exception:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                pass
            raise

        # Stdout drained normally — collect stderr and reap the process.
        stdin_task.cancel()
        # Await the cancelled task so its CancelledError is observed — avoids
        # "Task exception was never retrieved" warnings in Python 3.11+.
        try:
            await asyncio.gather(stdin_task, return_exceptions=True)
        except Exception:
            pass
        stderr_bytes: bytes = b""
        try:
            stderr_bytes = await asyncio.wait_for(stderr_task, timeout=5)
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            logger.warning("claude stream process did not exit after stdout drain — killing")
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                pass

        if proc.returncode and proc.returncode != 0:
            err_text = stderr_bytes.decode(errors="replace").strip()
            out_preview = parser.raw_preview.strip()
            out_tail = parser.raw_tail_preview.strip()
            parsed_subtype = parser.result_subtype.strip()
            parsed_error = parser.result_text.strip()
            logger.error(
                "agent stream exit code %d, stderr: %s",
                proc.returncode,
                err_text[:500] or "(empty)",
            )
            logger.error("agent stream stdout preview: %s", out_preview[:500] or "(empty)")
            logger.error("agent stream stdout tail: %s", out_tail[:2000] or "(empty)")
            diag = err_text or parsed_error or out_tail or out_preview or "unknown error"
            exc_type = _classify_claude_error(diag, parsed_subtype)
            raise exc_type(
                f"agent exited with code {proc.returncode}"
                f"{f' ({parsed_subtype})' if parsed_subtype else ''}: {diag}"
            )

        yield AgentEvent(kind="final", response=parser.build_response())

