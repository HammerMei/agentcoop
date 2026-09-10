"""OpenCode HTTP agent backend.

Implements AgentBackend using the opencode HTTP server (``opencode serve``):
  - Session creation:  POST /session  {"directory": "..."}
                       then POST /session/{id}/message  (init prompt)
  - Message sending:   POST /session/{id}/message  {"parts": [{"type": "text", ...}]}

Both calls are **synchronous** — the server blocks until the full agent turn
completes (including all tool calls and permission approvals) before returning.
This is the key advantage over the old ``opencode run`` subprocess approach, which
returned early when a tool was blocked by a pending permission request.

File attachments are not natively supported by the HTTP API.  They are injected
into the prompt text via :func:`~gateway.core.adapter_utils.build_attachment_prompt`
so the agent can access them using the Read tool — the same fallback used by the
Claude CLI backend.

The ``env`` parameter of :meth:`send` is a no-op in HTTP mode.  ``COOP_ROLE``
is set on the ``opencode serve`` process at startup via ``sidecar_env``,
hardcoded to ``"owner"`` in ``GatewayService._build_agent_backend()`` because
the sidecar always runs as the gateway's own backend process.  Per-message
guest enforcement (tool allow-lists, permission prompts) is handled by
:class:`~gateway.core.permission.PermissionBroker`, not by environment variables.

Lifecycle
---------
Call :meth:`start` before :meth:`create_session`.  ``start`` spawns
``opencode serve``, allocates a free port, and waits for the health check to
pass.  :meth:`stop` terminates the process.  Both methods are idempotent.

When used via :class:`~gateway.agents.session.AgentSession`, ``start``/``stop``
are called automatically by the context manager — no manual calls needed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import tempfile
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from ...core.adapter_utils import build_attachment_prompt
from ...core.paths import resolve_under
from ...paths import RUNTIME_DIR
from .. import AgentBackend, GatewayBrokerConfig
from ..errors import (
    AgentExecutionError,
    AgentPermissionError,
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

logger = logging.getLogger("coop.agents.opencode")

# The runtime state directory (one definition: gateway/paths.py; bound here so
# tests patch this module's copy). Durable per-watcher instructions files live
# under RUNTIME_DIR/system-prompts/<path_key>.md, where path_key is opaque to
# this adapter — the caller derives it per watcher-in-a-room
# (gateway/core/paths.py's watcher_prompt_key), never from the display name (§2.3)
# for both backends; watcher names are globally unique and forbidden from
# containing "/" (see gateway/config.py), so paths never collide, and each
# watcher only ever uses one backend type, so there's no cross-backend clash.


def _atomic_write_text(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically (temp file + ``os.replace``).

    Without this, a concurrent read of this file (from send()/stream() on
    another in-flight turn) while a write is in progress could see truncated
    or corrupt content instead of either the old or the new version in full.
    The temp file is created in the same directory so ``os.replace`` stays on
    one filesystem (required for atomicity) and is cleaned up on any failure
    before the replace. Mirrors gateway/agents/claude/adapter.py's helper of
    the same name/behavior (see that module for the original rationale).
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


# Sentinel string placed in the SSE queue by _collect_sse() once the HTTP
# streaming connection is established.  Using a module-level constant (rather
# than a bare string literal) makes isinstance checks unnecessary and guards
# against accidental collision with real SSE line content.
_SSE_READY = "__opencode_sse_ready__"
# Maximum seconds queue.get() may block before returning to re-check the
# deadline.  This is an *upper bound* on wait time — not a polling interval.
# Must be kept well below the typical caller timeout so that short deadlines
# are enforced promptly.  Increasing this value degrades deadline granularity.
_SSE_QUEUE_MAX_WAIT = 30.0
# Maximum seconds to wait for the SSE connection to be established before
# giving up with AgentUnavailableError.  Always capped to the caller's deadline.
_SSE_CONNECT_TIMEOUT = 15.0
# Maximum seconds to wait for HTTP 202 from /session/{id}/prompt_async.
# This endpoint returns immediately after queuing the prompt — 30 s is
# generous for a local sidecar; it is NOT the full agent-turn deadline.
_PROMPT_ASYNC_POST_TIMEOUT = 30

_INIT_PROMPT = (
    "Chat session initialized. "
    "You are a chat assistant standing by to respond to incoming messages. "
    "Do not take any proactive action — simply wait for the first user message."
)

_HEALTH_CHECK_PATH = "/session"
_STARTUP_TIMEOUT = 30  # seconds
# Circuit-breaker threshold: after this many consecutive failed auto-restarts,
# _ensure_live_runtime() fast-fails instead of blocking callers for ~30s each.
_MAX_RESTART_FAILURES = 3

# ── OpenCode bash permission injection ───────────────────────────────────────
#
# OpenCode's default permission ruleset uses ``"*": "allow"`` which means ALL
# bash commands run without emitting a ``permission.asked`` SSE event.  This
# completely bypasses AgentCoop's permission broker, so guest and owner tool
# restrictions defined in ``guest_allowed_tools`` / ``owner_allowed_tools``
# have no effect for bash in the default configuration.
#
# Fix: inject ``bash["*"] = "ask"`` via OPENCODE_CONFIG_CONTENT so OpenCode
# emits ``permission.asked`` for every bash command, letting AgentCoop enforce its
# own allow-lists and approval flow.  A short set of read-only patterns is
# pre-approved so common safe operations do not require manual approval.
#
# See ``_build_safe_opencode_config()`` for the full merge logic.

_DEFAULT_BASH_ALLOW_PATTERNS: list[str] = [
    "git log *",
    "git diff *",
    "git status *",
    "git show *",
    "coop send *",
]


def _build_safe_opencode_config(sidecar_env: dict[str, str]) -> str | None:
    """Return a safe ``OPENCODE_CONFIG_CONTENT`` value with bash permission defaults.

    OpenCode's default bash permission is ``"allow"`` (all bash commands run
    without asking), which bypasses AgentCoop's permission broker entirely.  This
    function ensures ``bash["*"] = "ask"`` is always present so that AgentCoop can
    intercept bash tool calls and enforce its own ``owner_allowed_tools`` /
    ``guest_allowed_tools`` rules.

    Merge rules
    -----------
    1. ``OPENCODE_CONFIG_CONTENT`` contains invalid JSON
       → raise ``ValueError`` — never silently drop the user's config.
    2. ``bash["*"]`` is already set (e.g. user explicitly chose ``"allow"`` or
       ``"ask"``) → return ``None`` (respect the user's explicit choice).
    3. ``bash["*"]`` is not set → append ``_DEFAULT_BASH_ALLOW_PATTERNS`` for
       known-safe read-only commands, then add ``"*": "ask"`` as the catch-all.
       Existing user-defined bash patterns are preserved unchanged.

    Args:
        sidecar_env: The environment dict that will be passed to the sidecar
            process.  Only ``OPENCODE_CONFIG_CONTENT`` is read from this dict.

    Returns:
        The new JSON string to assign to ``OPENCODE_CONFIG_CONTENT``, or
        ``None`` if no injection is needed.

    Raises:
        ValueError: If ``OPENCODE_CONFIG_CONTENT`` contains malformed JSON.
    """
    existing_str = sidecar_env.get("OPENCODE_CONFIG_CONTENT", "")

    if existing_str:
        try:
            config: dict = json.loads(existing_str)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"OPENCODE_CONFIG_CONTENT contains invalid JSON: {e}. "
                "Fix the env var or remove it to use AgentCoop's safe defaults."
            ) from e
    else:
        config = {}

    perms: dict = config.setdefault("permission", {})
    bash: dict = perms.setdefault("bash", {})

    # User explicitly set a "*" catch-all — respect their decision, no injection.
    if "*" in bash:
        return None

    # Append default allow patterns only if not already configured by the user.
    for pattern in _DEFAULT_BASH_ALLOW_PATTERNS:
        if pattern not in bash:
            bash[pattern] = "allow"

    # Always add the "*": "ask" catch-all so unlisted commands route through AgentCoop.
    bash["*"] = "ask"

    return json.dumps(config)


def _classify_http_error(status_code: int, message: str) -> AgentExecutionError:
    """Map opencode HTTP failures to structured backend exceptions."""
    if status_code == 429:
        return AgentRateLimitedError(message)
    if status_code in (401, 403):
        return AgentPermissionError(message)
    if status_code in (502, 503, 504):
        return AgentUnavailableError(message)
    return AgentExecutionError(message)


def _find_free_port() -> int:
    """Bind to port 0 to let the OS allocate a free ephemeral port, then release it.

    There is a brief TOCTOU window between release and the sidecar binding, but
    this is acceptable for local loopback use.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class OpenCodeBackend(AgentBackend):
    """Agent backend that communicates with a running ``opencode serve`` process."""

    def __init__(
        self,
        command: str,
        new_session_args: list[str],
        timeout: int,
        sidecar_env: dict[str, str] | None = None,
        sidecar_cwd: str | None = None,
        broker_config: GatewayBrokerConfig | None = None,
    ) -> None:
        """
        Args:
            command: opencode binary name (e.g. ``"opencode"``). Used to build
                the startup command: ``[command, "serve", "--port", "{port}"]
                + new_session_args``.
            new_session_args: Extra flags forwarded to ``opencode serve`` at startup
                (e.g. ``["--model", "anthropic/claude-sonnet-4-5"]``). Unlike the
                Claude backend, these are **server startup flags**, not per-message args.
            timeout: Default HTTP timeout in seconds for all API calls.
            sidecar_env: Environment variables to inject into the sidecar process.
                Hardcoded to ``{"COOP_ROLE": "owner"}`` by GatewayService because
                the sidecar always runs as the gateway's own agent backend.
                Guest enforcement is handled by the PermissionBroker at the
                per-request level, not via process environment.
                ``OPENCODE_CONFIG_CONTENT`` in this dict is merged with AgentCoop's
                safe bash permission defaults (``bash["*"] = "ask"``) unless
                the user has already set a ``"*"`` catch-all.  Raises
                ``ValueError`` if the value is malformed JSON.
            sidecar_cwd: Working directory for the ``opencode serve`` process.
                ``None`` inherits the gateway's cwd. Set this to the project root
                so opencode can find ``.opencode/opencode.json`` and plugins.
            broker_config: Optional permission policy settings for gateway broker
                creation.  ``None`` means permissions are disabled for this agent.
        """
        self._command = command
        self._new_session_args = new_session_args
        self.timeout = timeout
        # Inject safe bash permission defaults before storing sidecar_env.
        # OpenCode's built-in default allows ALL bash commands without asking,
        # which bypasses AgentCoop's permission broker.  _build_safe_opencode_config()
        # ensures "bash": {"*": "ask"} is set via OPENCODE_CONFIG_CONTENT so
        # AgentCoop can enforce owner_allowed_tools / guest_allowed_tools for bash.
        # Raises ValueError on malformed OPENCODE_CONFIG_CONTENT so the caller
        # gets a clear error rather than silently losing their config.
        _env: dict[str, str] = sidecar_env or {}
        _safe_config = _build_safe_opencode_config(_env)
        if _safe_config is not None:
            _env = {**_env, "OPENCODE_CONFIG_CONTENT": _safe_config}
            logger.info("Injected safe bash permission defaults via OPENCODE_CONFIG_CONTENT")
        self._sidecar_env: dict[str, str] = _env
        self._sidecar_cwd: str | None = sidecar_cwd
        self._broker_config = broker_config
        self._base_url: str | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._stdout_drain: asyncio.Task | None = None
        self._stderr_drain: asyncio.Task | None = None
        # Long-lived HTTP client — reused across health checks, session creation,
        # and message sending to avoid repeated connection setup/teardown overhead.
        self._client: httpx.AsyncClient | None = None
        self._orphan_session_ids: set[str] = set()
        # Serializes concurrent restart attempts so two simultaneous send() calls
        # that both detect a dead sidecar cannot race through _ensure_live_runtime()
        # and spawn duplicate processes.
        self._restart_lock: asyncio.Lock = asyncio.Lock()
        # Tracks whether start() has ever succeeded.  Used by _ensure_live_runtime()
        # to distinguish "never started (caller error)" from "started, then died/failed
        # restart (auto-recovery eligible)".  Cleared on explicit stop() so that a
        # manually stopped backend requires a fresh start() call.
        self._ever_started: bool = False
        # Circuit-breaker for repeated failed auto-restarts.
        # After _MAX_RESTART_FAILURES consecutive failures, _ensure_live_runtime()
        # raises immediately (fast-fail) instead of blocking callers for ~30s each
        # time.  Reset to 0 on a successful restart or explicit stop().
        self._consecutive_restart_failures: int = 0

    @property
    def supports_per_message_env(self) -> bool:
        """OpenCode HTTP mode ignores per-message env — role is set at sidecar startup."""
        return False

    def typical_session_retention_days(self) -> int | None:
        """OpenCode has no automatic session expiry of its own — sessions persist
        indefinitely in its SQLite store (confirmed via source, not just docs;
        see docs/design/dynamic-watcher-design.md). Explicit override (rather than
        relying on the AgentBackend default) so this is a visible, deliberate
        statement about OpenCode specifically, not an inherited default that
        happens to be correct.
        """
        return None

    def create_gateway_broker(
        self,
        registry: "PermissionRegistry",
        notifier: "PermissionNotifier",
        session_room_map: dict[str, str],
        session_role_map: dict[str, str],
        session_permission_thread_map: "dict[str, str | None]",
    ) -> "PermissionBroker | None":
        """Return an OpenCodePermissionBroker wired to the shared notification channel.

        Requires ``start()`` to have been called first so ``_base_url`` is set.
        ``AgentRuntimeManager.start_all()`` ensures this ordering internally.
        """
        if self._broker_config is None:
            return None
        if not self._base_url:
            raise RuntimeError(
                "OpenCodeBackend has no base_url — call start() before create_gateway_broker()"
            )
        from .broker import OpenCodePermissionBroker

        return OpenCodePermissionBroker(
            registry=registry,
            notifier=notifier,
            opencode_base_url=self._base_url,
            session_room_map=session_room_map,
            session_role_map=session_role_map,
            session_permission_thread_map=session_permission_thread_map,
            owner_allowed_tools=self._broker_config.owner_allowed_tools,
            guest_allowed_tools=self._broker_config.guest_allowed_tools,
            timeout_seconds=self._broker_config.timeout,
            skip_owner_approval=self._broker_config.skip_owner_approval,
        )

    def create_callable_broker(self, handler, timeout_seconds: int):
        """Return an OpenCodeCallablePermissionBroker for SSE-based permission callbacks.

        Requires the backend to be started (``start()`` must have been called so
        ``_base_url`` is populated) before the broker's SSE listener can connect.
        AgentSession ensures this ordering: ``start()`` is called in ``__aenter__``
        before the broker is created.
        """
        if not self._base_url:
            raise RuntimeError("OpenCodeBackend has no base_url — call start() first")
        from .callable_broker import OpenCodeCallablePermissionBroker

        return OpenCodeCallablePermissionBroker(
            base_url=self._base_url,
            permission_handler=handler,
            timeout_seconds=timeout_seconds,
        )

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def _start_inner(self) -> None:
        """Internal startup logic — **must be called with** ``_restart_lock`` **held**.

        Extracted from :meth:`start` so that :meth:`_ensure_live_runtime` can
        restart the sidecar without re-acquiring the lock, which would deadlock
        because ``asyncio.Lock`` is not re-entrant and ``_ensure_live_runtime``
        already holds ``_restart_lock`` when it calls this method.

        Raises:
            RuntimeError: If the process fails to become healthy within
                ``_STARTUP_TIMEOUT`` seconds.
        """
        if self._base_url:
            return  # another caller already finished start() — double-check guard

        port = _find_free_port()
        base_url = f"http://127.0.0.1:{port}"
        cmd = [self._command, "serve", "--port", str(port)] + self._new_session_args
        env = {**os.environ, **self._sidecar_env}

        logger.info(
            "Starting opencode serve: %s (port=%d, cwd=%s)",
            cmd[0],
            port,
            self._sidecar_cwd or "<inherited>",
        )
        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            env=env,
            cwd=self._sidecar_cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        self._stdout_drain = asyncio.create_task(
            self._drain_pipe(self._process.stdout, logging.DEBUG, "[opencode stdout]"),
            name="opencode-stdout-drain",
        )
        self._stderr_drain = asyncio.create_task(
            self._drain_pipe(
                self._process.stderr, logging.WARNING, "[opencode stderr]"
            ),
            name="opencode-stderr-drain",
        )

        try:
            await self._wait_for_health(base_url)
        except BaseException:
            # Health check failed — clean up the partially started sidecar so
            # we don't leak processes or background tasks.  After cleanup the
            # backend is in a clean retryable state (same as "never started").
            #
            # BaseException (not Exception) is caught here so that
            # asyncio.CancelledError — raised when the gateway is shutting down
            # while the health poll is in progress — also triggers cleanup.
            # Without this, a SIGTERM during startup leaves the opencode serve
            # subprocess running indefinitely as an orphan.
            await self._cleanup_partial_start()
            raise

        self._base_url = base_url
        self._client = httpx.AsyncClient(base_url=base_url, timeout=self.timeout)
        self._ever_started = True
        logger.info("OpenCode server ready at %s", self._base_url)

    async def start(self) -> None:
        """Spawn ``opencode serve``, allocate a port, and wait for the health check.

        Idempotent — returns immediately if the server is already running.

        Raises:
            RuntimeError: If the process fails to become healthy within
                ``_STARTUP_TIMEOUT`` seconds.
        """
        if self._base_url:
            return  # fast path — already running, no lock needed

        # Double-checked locking: a second concurrent caller must not spawn a
        # second ``opencode serve`` process.  The fast-path check above is
        # intentionally outside the lock for performance; the re-check inside
        # ``_start_inner`` is the authoritative guard.  ``_restart_lock`` is also
        # held by ``_ensure_live_runtime()`` and ``stop()``, so all lifecycle
        # operations are mutually exclusive.
        async with self._restart_lock:
            await self._start_inner()

    async def _cleanup_partial_start(self) -> None:
        """Clean up after a failed start() — kill process, cancel drain tasks, reset state."""
        for task in (self._stdout_drain, self._stderr_drain):
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._stdout_drain = None
        self._stderr_drain = None

        if self._process:
            try:
                self._process.kill()
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except Exception:
                pass
            self._process = None

        self._base_url = None
        logger.warning("Cleaned up partially started OpenCode sidecar")

    async def _drain_pipe(
        self, stream: asyncio.StreamReader, level: int, prefix: str
    ) -> None:
        """Read lines from a subprocess pipe and forward them to the logger."""
        try:
            async for line in stream:
                logger.log(
                    level, "%s %s", prefix, line.decode(errors="replace").rstrip()
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("Pipe drain stopped: %s", e)

    async def stop(self) -> None:
        """Terminate the ``opencode serve`` process.

        Idempotent — returns immediately if already stopped.

        Holds ``_restart_lock`` for the entire shutdown so that a concurrent
        ``_ensure_live_runtime()`` cannot race to restart the sidecar while it
        is being torn down.  Without the lock, the following sequence is possible:

          1. ``_ensure_live_runtime()`` checks ``_base_url is not None`` → live
          2. ``stop()`` sets ``_base_url = None`` and closes the client
          3. ``_ensure_live_runtime()`` uses the now-closed client → crash
        """
        if self._process is None:
            return  # fast path — no lock needed for this trivial check
        async with self._restart_lock:
            if self._process is None:
                return  # double-check: another stop() already finished
            logger.info("Stopping opencode serve (pid=%d)", self._process.pid)
            try:
                # Wrap with a short total timeout so a crashed sidecar (where
                # every DELETE request blocks for the full client timeout) cannot
                # stall gateway shutdown for N × self.timeout seconds.  Orphan
                # cleanup is best-effort — it is acceptable to skip it when the
                # sidecar is unreachable during shutdown.
                await asyncio.wait_for(
                    self._cleanup_orphan_sessions_best_effort(),
                    timeout=10.0,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Orphan session cleanup timed out after 10s during shutdown "
                    "(%d session(s) may remain on the opencode server)",
                    len(self._orphan_session_ids),
                )
            except Exception as e:
                logger.warning("Failed orphan session cleanup before shutdown: %s", e)
            for task in (self._stdout_drain, self._stderr_drain):
                if task:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            self._stdout_drain = None
            self._stderr_drain = None
            try:
                self._process.terminate()
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    logger.warning("opencode serve did not exit after 5s — killing")
                    self._process.kill()
                    try:
                        await asyncio.wait_for(self._process.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        logger.error(
                            "opencode serve did not exit after SIGKILL — "
                            "process may be stuck in uninterruptible kernel wait"
                        )
            except ProcessLookupError:
                pass
            except Exception as e:
                logger.error("Error stopping opencode serve: %s", e)
            finally:
                if self._client:
                    await self._client.aclose()
                    self._client = None
                self._base_url = None
                self._process = None
                self._ever_started = False  # explicit stop resets — require new start() call
                self._consecutive_restart_failures = 0  # clear circuit-breaker on explicit stop

    async def _invalidate_dead_runtime(self) -> None:
        """Reset client/runtime state after detecting a dead sidecar process."""
        for task in (self._stdout_drain, self._stderr_drain):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._stdout_drain = None
        self._stderr_drain = None
        if self._client:
            await self._client.aclose()
            self._client = None
        self._base_url = None
        self._process = None

    async def _ensure_live_runtime(self) -> None:
        """Restart the sidecar on demand if a previously started process died.

        Serialized by ``_restart_lock`` to prevent concurrent ``send()`` calls
        from both detecting a dead sidecar and spawning duplicate processes.
        Without the lock, two concurrent coroutines could both pass the
        ``returncode is not None`` check before either completes the restart,
        resulting in two simultaneous ``opencode serve`` processes and two
        conflicting ``_base_url`` assignments.

        NOTE: ``_require_base_url()`` is intentionally called AFTER the restart
        block, not before.  If the previous ``start()`` call failed,
        ``_base_url`` is None.  Calling ``_require_base_url()`` first would
        raise before the restart logic can run, making auto-recovery impossible
        after a failed restart attempt.

        The outer guard covers two cases that both require a restart attempt:
          1. The sidecar process is present but has exited (returncode is not None).
          2. start() was previously called (``_ever_started`` is True) but the
             base_url is gone — this happens when a restart attempt failed and
             ``_cleanup_partial_start()`` cleared both ``_process`` and
             ``_base_url``.  Without this second condition, the backend would be
             permanently stuck after a failed restart.
        """
        needs_restart = (
            (self._process is not None and self._process.returncode is not None)
            or (self._ever_started and self._base_url is None)
        )
        if needs_restart:
            # Circuit-breaker fast-fail: skip the lock and fail immediately if
            # the counter is already at the limit.  This is a *performance hint*
            # only — it avoids blocking for the full _STARTUP_TIMEOUT when the
            # sidecar is known-dead.  A concurrent increment could race past
            # this check; the authoritative guard is the re-check inside the
            # lock below.  Cleared on a successful restart or explicit stop().
            if self._consecutive_restart_failures >= _MAX_RESTART_FAILURES:
                # The failure count is intentionally included: it is operational
                # diagnostic info for administrators/operators, not a secret.
                raise AgentUnavailableError(
                    f"opencode sidecar is unavailable after "
                    f"{self._consecutive_restart_failures} consecutive failed restart "
                    "attempts — call stop() then start() to reset"
                )
            async with self._restart_lock:
                # Re-check inside the lock: a concurrent coroutine may have
                # already completed the restart by the time we acquire it.
                still_needs_restart = (
                    (self._process is not None and self._process.returncode is not None)
                    or (self._ever_started and self._base_url is None)
                )
                if still_needs_restart:
                    # Re-check circuit-breaker inside lock too (another coroutine
                    # may have incremented the counter while we were waiting).
                    if self._consecutive_restart_failures >= _MAX_RESTART_FAILURES:
                        # Authoritative guard (outer check is a performance hint only).
                        # Failure count is intentional diagnostic info for operators.
                        raise AgentUnavailableError(
                            f"opencode sidecar is unavailable after "
                            f"{self._consecutive_restart_failures} consecutive failed restart "
                            "attempts — call stop() then start() to reset"
                        )
                    exit_code = self._process.returncode if self._process else None
                    logger.warning(
                        "Detected dead/unrecovered opencode sidecar (exit=%s) — restarting before request",
                        exit_code,
                    )
                    await self._invalidate_dead_runtime()
                    try:
                        # Call _start_inner() directly — NOT start() — because
                        # _restart_lock is already held here and asyncio.Lock is
                        # not re-entrant.  Calling start() would deadlock waiting
                        # to acquire the lock it already owns.
                        await self._start_inner()
                        self._consecutive_restart_failures = 0
                    except Exception:
                        self._consecutive_restart_failures += 1
                        logger.error(
                            "opencode sidecar restart failed (attempt %d/%d)",
                            self._consecutive_restart_failures,
                            _MAX_RESTART_FAILURES,
                        )
                        raise
        # Shutdown race guard: a concurrent stop() call may have acquired and
        # released _restart_lock between when we evaluated `needs_restart`
        # (outside the lock) and when we entered the lock body.  In that case
        # `still_needs_restart` evaluated to False (because stop() cleared
        # _ever_started), we skipped the restart block, and now _base_url is
        # still None — not because of a programming error, but because the
        # sidecar was explicitly stopped.  Raise AgentUnavailableError (a
        # recoverable operational condition) instead of the confusing
        # RuntimeError("call start() before ...") from _require_base_url().
        if not self._ever_started and self._base_url is None:
            raise AgentUnavailableError(
                "opencode sidecar has been stopped — call start() to restart"
            )
        self._require_base_url()

    async def _wait_for_health(self, base_url: str) -> None:
        """Poll the health-check endpoint until it responds or the deadline passes."""
        health_url = f"{base_url}{_HEALTH_CHECK_PATH}"
        deadline = asyncio.get_running_loop().time() + _STARTUP_TIMEOUT
        last_exc: Exception | None = None

        async with httpx.AsyncClient(timeout=2.0) as client:
            while asyncio.get_running_loop().time() < deadline:
                # Check if process died before becoming healthy
                if self._process and self._process.returncode is not None:
                    raise RuntimeError(
                        f"opencode serve exited with code {self._process.returncode} "
                        "before becoming healthy"
                    )
                try:
                    resp = await client.get(health_url)
                    # Only 200 is accepted as proof the sidecar is fully ready.
                    # 404/401/405 mean the endpoint exists but the service is not
                    # in a known-good state; treat anything other than 200 as not ready.
                    if resp.status_code == 200:
                        return
                except Exception as exc:
                    last_exc = exc
                await asyncio.sleep(0.5)

        # Sanitize: do not interpolate last_exc directly — its __str__ often
        # contains the internal URL (host:port) from the httpx request.
        exc_type = type(last_exc).__name__ if last_exc is not None else "unknown"
        raise RuntimeError(
            f"opencode serve did not become healthy within {_STARTUP_TIMEOUT}s "
            f"(last error type: {exc_type})"
        )

    # ── AgentBackend interface ─────────────────────────────────────────────────

    async def create_session(
        self,
        working_directory: str,
        extra_args: list[str] | None = None,
        session_title: str | None = None,
    ) -> str:
        """Create a new opencode session and return the session_id.

        Args:
            working_directory: The working directory for the agent (passed to
                POST /session as ``directory``).
            extra_args: Ignored in HTTP mode (no per-message subprocess to pass
                args to). Logged at DEBUG if provided.
            session_title: Optional session title. Passed as ``title`` in the
                POST /session body — verify field name against live API.
        """
        if extra_args:
            logger.debug(
                "extra_args ignored by HTTP adapter (set on server at startup): %s",
                extra_args,
            )

        await self._ensure_live_runtime()
        url = f"{self._base_url}/session"
        body: dict = {"directory": working_directory}
        if session_title:
            # ⚠️ Verify field name against live POST /session response — "title" assumed.
            body["title"] = session_title

        logger.info("Creating opencode session via HTTP (cwd=%s)", working_directory)
        try:
            resp = await self._get_client().post(url, json=body)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            message = (
                f"opencode API returned HTTP {exc.response.status_code} "
                f"for POST /session"
            )
            raise _classify_http_error(exc.response.status_code, message) from None
        data = resp.json()

        session_id = data.get("id", "")
        if not session_id:
            # Log raw body at DEBUG only — response may contain internal paths
            # or server details that should not appear in user-facing messages.
            logger.debug("opencode POST /session response missing id: %r", data)
            raise RuntimeError("opencode POST /session returned no session id")

        # Send init prompt to prime the session (same as old CLI adapter).
        try:
            await self._post_message(session_id, _INIT_PROMPT)
        except Exception:
            cleaned = await self._cleanup_session_best_effort(session_id)
            if not cleaned:
                self._orphan_session_ids.add(session_id)
                logger.warning(
                    "OpenCode session %s could not be cleaned up after init failure; marked orphan",
                    session_id[:16],
                )
            raise

        logger.info("Created opencode session: %s", session_id[:16])
        return session_id

    async def reclaim_durable_instructions(self, path_key: str) -> None:
        """Remove the per-watcher file `ensure_durable_instructions` wrote.

        Expiry's half of that contract (§2.5), mirroring `ClaudeBackend`: same
        directory, same `resolve_under` containment check — the key is built
        from connector data and is validated on the way out as on the way in.
        Idempotent: a missing file is success. Without this override the base
        no-op ran, and every expired OpenCode watcher left its prompt file —
        identity and context included — under `system-prompts` for good
        (Codex, PR #140 round 2).
        """
        path = resolve_under(RUNTIME_DIR / "system-prompts", f"{path_key}.md")
        await asyncio.to_thread(path.unlink, missing_ok=True)

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
        """Write ``content`` to a stable per-watcher file for per-request resupply.

        Mirrors ``ClaudeBackend.ensure_durable_instructions()``'s contract
        exactly: writes durable content to ``RUNTIME_DIR/system-prompts/<path_key>.md``
        and returns that path as ``to_repeat``. ``message_processor.py`` already
        resupplies whatever this returns via ``append_system_prompt_file`` on
        every subsequent ``send()``/``stream()`` call for this watcher — see
        that module's ``self._append_system_prompt_file`` plumbing, which is
        backend-agnostic and required no changes for this to work.

        Unlike Claude (which passes the path to the ``claude`` CLI as a flag),
        OpenCode's ``send()``/``stream()`` read this file's *content* and
        forward it as the ``system`` field on ``POST /session/{id}/message``
        (or ``prompt_async``) — a genuine per-request API field (confirmed
        against opencode's own source: ``PromptInput.system`` in
        ``session/prompt.ts``, joined in last in ``session/llm/request.ts``).
        That field travels with each individual API call, not with the
        sidecar process, which matters here: a single OpenCode sidecar/agent
        config can be shared by multiple AgentCoop watchers (``WatcherConfig.agent``
        is many-to-one), so a sidecar-global mechanism like ``config.instructions``
        would leak one watcher's identity/addressing header into another's
        session. The per-request ``system`` field is correctly scoped per
        ``session_id`` instead, avoiding that cross-watcher leak entirely.

        Writing is idempotent (overwrite with identical content) — no side
        effect to avoid repeating, so ``already_delivered`` is irrelevant
        here, same as Claude's implementation; always write fresh so a
        resumed session after a gateway restart still gets a correct, current
        file. The write itself is atomic (see ``_atomic_write_text``).

        Deliberately written under ``RUNTIME_DIR`` (AgentCoop's own state
        directory), NOT under ``working_directory`` — same rationale as
        Claude's implementation (avoids an accidental ``git add`` if
        ``working_directory`` is a real user project under version control).
        """
        acg_dir = RUNTIME_DIR / "system-prompts"
        await asyncio.to_thread(acg_dir.mkdir, parents=True, exist_ok=True)
        # Named by the caller-supplied ``path_key``, which is opaque here: it is scoped
        # to the watcher in a room (watcher_prompt_key), NOT to the room alone, because
        # Scoped to the watcher in a room, not to the room alone. Two watchers on one
        # connector+room are refused now (§4.1), so the collision it was written for
        # cannot occur; the key stays because it names files on disk and re-keying
        # would orphan every existing one.
        # is for the attachment workspace. resolve_under supplies the containment check
        # that comes with treating the key as external data (§2.3).
        path = resolve_under(acg_dir, f"{path_key}.md")
        await asyncio.to_thread(_atomic_write_text, path, content)
        return str(path)

    async def _read_durable_instructions(self, path: str) -> str | None:
        """Read the content of a durable-instructions file written by
        ``ensure_durable_instructions()``, for forwarding as the ``system``
        field on a send()/stream() call.

        Returns ``None`` (rather than raising) if the file is missing or
        unreadable — a turn should still proceed without the durable header
        rather than fail outright; the missing content is logged as a
        warning so it's visible without silently degrading forever.
        """
        try:
            return await asyncio.to_thread(Path(path).read_text, encoding="utf-8")
        except OSError as e:
            logger.warning(
                "Could not read durable instructions file %s (%s) — "
                "proceeding without system-prompt content for this turn.",
                path, e,
            )
            return None

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
        """Send a message to an existing opencode session and return a normalized AgentResponse.

        File attachments are injected into the prompt text via build_attachment_prompt
        (no native HTTP upload equivalent to the CLI's ``-f`` flag).

        The ``env`` kwarg is a no-op: COOP_ROLE and other role vars must be set on the
        opencode server process at startup, not per-message.

        The ``append_system_prompt_file`` kwarg holds a path written by
        ``ensure_durable_instructions()`` — its *content* (not the path
        itself) is read fresh on every call and forwarded as the ``system``
        field on ``POST /session/{id}/message``, so it survives OpenCode's
        own context compaction the same way Claude's ``--append-system-prompt-file``
        does (re-supplied per call, never stored in conversation history).
        """
        if attachments:
            logger.info(
                "Injecting %d attachment(s) into prompt text (no native HTTP upload): %s",
                len(attachments),
                attachments,
            )
        prompt = build_attachment_prompt(prompt, attachments, working_directory)

        if env:
            logger.debug(
                "env kwarg ignored by HTTP adapter (set on server at startup): %s",
                list(env.keys()),
            )
        system_content = None
        if append_system_prompt_file:
            system_content = await self._read_durable_instructions(append_system_prompt_file)

        await self._ensure_live_runtime()
        raw = await self._post_message(
            session_id, prompt, timeout=timeout, system=system_content,
        )
        return self._parse_http_response(raw, session_id)

    async def stream(
        self,
        session_id: str,
        prompt: str,
        working_directory: str,
        timeout: int,
        attachments: list[str] | None = None,
        env: dict[str, str] | None = None,
        append_system_prompt_file: str | None = None,
    ) -> AsyncGenerator[AgentEvent, None]:
        """Stream intermediate agent events for an OpenCode session turn.

        Submits the prompt via ``POST /session/{id}/prompt_async`` (returns
        immediately), then consumes the ``GET /event`` SSE stream, yielding
        :class:`~gateway.agents.response.AgentEvent` objects as content arrives.

        The SSE connection is established **before** the prompt is posted to
        eliminate the race condition where a very fast turn would complete and
        emit ``session.status idle`` before we started listening.

        Yields:
            ``AgentEvent(kind="tool_call")``   — when a tool transitions to
                ``running`` state.
            ``AgentEvent(kind="tool_result")`` — when a tool reaches
                ``completed`` or ``error`` state.
            ``AgentEvent(kind="thinking")``    — on the first non-empty snapshot
                of a ``reasoning`` part.
            ``AgentEvent(kind="final")``       — with the completed
                :class:`~gateway.agents.response.AgentResponse` when the
                session status becomes ``idle``.

        Raises:
            asyncio.TimeoutError: If the turn doesn't complete within ``timeout``
                seconds.
            AgentExecutionError: On ``session.error`` events or SSE parse failures.
            AgentUnavailableError: If the SSE connection cannot be established.
        """
        if attachments:
            logger.info(
                "Injecting %d attachment(s) into prompt text (no native HTTP upload): %s",
                len(attachments),
                attachments,
            )
        prompt = build_attachment_prompt(prompt, attachments, working_directory)

        if env:
            logger.debug(
                "env kwarg ignored by HTTP adapter (set on server at startup): %s",
                list(env.keys()),
            )
        system_content = None
        if append_system_prompt_file:
            system_content = await self._read_durable_instructions(append_system_prompt_file)

        await self._ensure_live_runtime()

        deadline = asyncio.get_running_loop().time() + timeout

        # ── Phase 1: open SSE connection BEFORE posting the prompt ────────────
        # Events are not replayed on reconnect — we must be listening before
        # the prompt is submitted to guarantee we see session.status idle.
        # A background task drains the SSE stream into a queue; the main
        # coroutine reads from the queue so it can also enforce the deadline.
        queue: asyncio.Queue[str | Exception] = asyncio.Queue()

        # Capture base_url before creating the background task so that a
        # concurrent stop() call cannot null out self._base_url mid-flight.
        base_url = self._base_url

        async def _collect_sse() -> None:
            url = f"{base_url}/event"
            try:
                async with httpx.AsyncClient(
                    # TCP-level connect timeout is intentionally shorter than
                    # _SSE_CONNECT_TIMEOUT (15 s): if the TCP handshake itself
                    # hangs for >10 s the socket is broken regardless, so we
                    # fail fast at the httpx layer and let _collect_sse put the
                    # ConnectTimeout in the queue as an AgentUnavailableError.
                    timeout=httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)
                ) as sse_client:
                    async with sse_client.stream("GET", url) as response:
                        response.raise_for_status()
                        await queue.put(_SSE_READY)
                        async for line in response.aiter_lines():
                            await queue.put(line)
                        # Natural stream close before session.status idle —
                        # signal _parse_sse_events so it can raise promptly
                        # rather than waiting for the next _SSE_QUEUE_MAX_WAIT
                        # poll to expire.  Note: if sse_task is cancelled
                        # (e.g. because _post_message_async raised) this put
                        # may itself be interrupted by CancelledError — that
                        # is safe because the EOFError sentinel is then unused.
                        await queue.put(
                            EOFError("OpenCode SSE stream closed before session.status idle")
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await queue.put(exc)

        sse_task = asyncio.create_task(_collect_sse(), name="opencode-stream-sse")
        try:
            # Wait for SSE to connect — capped at _SSE_CONNECT_TIMEOUT but
            # never exceeds the caller's own deadline so short timeouts don't
            # block extra long.
            sse_connect_timeout = min(
                _SSE_CONNECT_TIMEOUT,
                max(0.1, deadline - asyncio.get_running_loop().time()),
            )
            try:
                ready = await asyncio.wait_for(queue.get(), timeout=sse_connect_timeout)
            except asyncio.TimeoutError:
                raise AgentUnavailableError(
                    "OpenCode SSE stream did not connect within "
                    f"{sse_connect_timeout:.2g}s"
                )
            if isinstance(ready, Exception):
                # Sanitize: do not interpolate the raw exception — its __str__
                # often contains the internal URL (host:port) from httpx.
                raise AgentUnavailableError(
                    "OpenCode SSE connection failed for session "
                    f"{session_id[:16]!r} — sidecar may be unreachable"
                ) from None
            if ready != _SSE_READY:
                # Should never happen given the current _collect_sse
                # implementation, but guard defensively so a future change
                # cannot silently pass a raw SSE line to _parse_sse_events.
                raise AgentUnavailableError(
                    f"OpenCode SSE returned unexpected handshake item: {ready!r}"
                )

            # ── Phase 2: post the prompt (SSE is now listening) ───────────────
            await self._post_message_async(session_id, prompt, system=system_content)

            # ── Phase 3: consume SSE events and yield AgentEvents ─────────────
            async for event in self._parse_sse_events(
                session_id, queue, deadline, timeout
            ):
                yield event

        finally:
            sse_task.cancel()
            await asyncio.gather(sse_task, return_exceptions=True)

    async def _post_message_async(
        self, session_id: str, text: str, *, system: str | None = None,
    ) -> None:
        """POST a prompt to the async endpoint — returns immediately (HTTP 202).

        Unlike :meth:`_post_message` (which blocks until the turn completes),
        this method returns as soon as the server acknowledges the request.
        Progress arrives via the ``GET /event`` SSE stream.

        The HTTP timeout is fixed at :data:`_PROMPT_ASYNC_POST_TIMEOUT` seconds.
        This is NOT the agent-turn deadline — the server returns 202 immediately
        after queuing the prompt, so a short timeout is appropriate.

        Args:
            session_id: OpenCode session ID.
            text:       Prompt text to send.
            system:     Optional durable system-prompt content for this call
                        (see ``PromptInput.system`` — a per-request field,
                        joined in last after AGENTS.md/env/agent-prompt on
                        the server side). Not persisted on the session; must
                        be resupplied on every call where it's wanted, same
                        discipline as Claude's ``--append-system-prompt-file``.

        Raises:
            AgentExecutionError:   Server rejected the request (4xx/5xx, including 404).
            AgentUnavailableError: Sidecar unreachable or timed-out at TCP level.

        .. note::
            Endpoint path ``/session/{id}/prompt_async`` — verify against a
            live opencode server if the API version changes.
        """
        url = f"{self._base_url}/session/{session_id}/prompt_async"
        body: dict = {"parts": [{"type": "text", "text": text}]}
        if system:
            body["system"] = system
        try:
            resp = await self._get_client().post(
                url, json=body, timeout=_PROMPT_ASYNC_POST_TIMEOUT
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            message = (
                f"opencode API returned HTTP {exc.response.status_code} "
                f"for session {session_id[:16]!r} (prompt_async)"
            )
            raise _classify_http_error(exc.response.status_code, message) from None
        except (httpx.ConnectError, httpx.TimeoutException):
            raise AgentUnavailableError(
                f"opencode sidecar unreachable (prompt_async) for session {session_id[:16]!r}"
            ) from None

    async def _parse_sse_events(
        self,
        session_id: str,
        queue: asyncio.Queue[str | Exception],
        deadline: float,
        timeout: int,
    ) -> AsyncGenerator[AgentEvent, None]:
        """Consume SSE lines from *queue* and yield :class:`AgentEvent` objects.

        Filters to events whose ``properties.sessionID`` matches *session_id*.
        Tracks accumulated text and usage data across the turn; yields a
        ``final`` event when ``session.status`` becomes ``idle``.

        Args:
            session_id: OpenCode session ID to filter events for.
            queue:      Async queue populated by the SSE collector task.
            deadline:   ``loop.time()`` deadline; raises :exc:`asyncio.TimeoutError`
                        if exceeded.
            timeout:    Original timeout in seconds (used in error messages only).
        """
        # Accumulated state for the final AgentResponse
        part_types: dict[str, str] = {}        # partID → part type
        text_part_order: list[str] = []         # ordered text partIDs
        text_accumulator: dict[str, str] = {}   # partID → accumulated delta text
        emitted_thinking: set[str] = set()      # partIDs whose thinking event was yielded
        # De-duplicate repeated message.part.updated events for the same part:
        # OpenCode may stream incremental state updates (e.g. pending→running→
        # completed) so the same part ID can arrive more than once.
        tool_call_emitted: set[str] = set()     # partIDs for which tool_call was yielded
        tool_result_emitted: set[str] = set()   # partIDs for which tool_result was yielded
        seen_step_finish_ids: set[str] = set()  # partIDs whose tokens were accumulated
        total_cost = 0.0
        total_input = total_output = total_reasoning = 0
        total_cache_read = total_cache_write = 0
        num_turns = 0

        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError(
                    f"OpenCode session {session_id[:16]!r} did not complete "
                    f"within {timeout}s"
                )

            try:
                item = await asyncio.wait_for(
                    queue.get(), timeout=min(remaining, _SSE_QUEUE_MAX_WAIT)
                )
            except asyncio.TimeoutError:
                raise asyncio.TimeoutError(
                    f"OpenCode session {session_id[:16]!r} did not complete "
                    f"within {timeout}s"
                )

            if isinstance(item, Exception):
                if isinstance(item, EOFError):
                    # Clean stream close before session.status idle — the
                    # EOFError message is gateway-controlled text, safe to surface.
                    raise AgentUnavailableError(str(item)) from None
                # Transport / parse failure from httpx or the SSE layer.
                # Do NOT interpolate str(item): transport exceptions carry
                # internal URLs in their string representation.  Only emit
                # the exception type name, consistent with _wait_for_health.
                raise AgentUnavailableError(
                    f"OpenCode SSE stream error ({type(item).__name__}) "
                    f"for session {session_id[:16]!r}"
                ) from None

            line: str = item
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if not raw:
                continue

            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                logger.debug("Malformed OpenCode SSE JSON: %r", raw[:200])
                continue

            # SSE lines may be valid JSON but non-dict (null, array, string…).
            if not isinstance(payload, dict):
                logger.debug("Unexpected non-dict OpenCode SSE payload: %r", raw[:200])
                continue

            event_type = payload.get("type", "")
            # Guard against explicit JSON null ("properties": null).
            props = payload.get("properties") or {}

            # Filter to our session only (the SSE stream is global)
            if props.get("sessionID") != session_id:
                continue

            # ── Text / reasoning streaming ─────────────────────────────────
            if event_type == "message.part.delta":
                part_id = props.get("partID", "")
                field = props.get("field", "")
                delta = props.get("delta", "")
                if not (part_id and field == "text" and isinstance(delta, str) and delta):
                    continue
                # Accumulate only confirmed text parts — skip unregistered
                # parts (part_type unknown) to avoid reasoning deltas bleeding
                # in before the corresponding message.part.updated arrives.
                if part_types.get(part_id) == "text":
                    if part_id not in text_accumulator:
                        text_part_order.append(part_id)
                    text_accumulator[part_id] = (
                        text_accumulator.get(part_id, "") + delta
                    )

            # ── Part creation / state transitions ──────────────────────────
            elif event_type == "message.part.updated":
                # Guard against explicit JSON null for "part" or "state".
                part = props.get("part") or {}
                part_id = part.get("id", "")
                part_type = part.get("type", "")
                if not part_id:
                    continue
                part_types[part_id] = part_type

                if part_type == "tool":
                    state = part.get("state") or {}
                    tool_status = state.get("status", "")
                    tool_name = part.get("tool", "")
                    if not isinstance(tool_name, str):
                        tool_name = ""
                    if tool_status == "running" and tool_name:
                        if part_id not in tool_call_emitted:
                            tool_call_emitted.add(part_id)
                            yield AgentEvent(kind="tool_call", text=f"🔧 {tool_name}")
                    elif tool_status in ("completed", "error") and tool_name:
                        if part_id not in tool_result_emitted:
                            tool_result_emitted.add(part_id)
                            yield AgentEvent(kind="tool_result", text=f"✓ {tool_name}")

                elif part_type == "reasoning":
                    # Yield a single thinking event when the reasoning text
                    # first becomes non-empty (it may be updated incrementally,
                    # but we only need one notification per reasoning block).
                    reasoning_text = part.get("text", "")
                    if reasoning_text and part_id not in emitted_thinking:
                        emitted_thinking.add(part_id)
                        yield AgentEvent(
                            kind="thinking",
                            text=f"💭 {reasoning_text[:80]}",
                        )

                elif part_type == "step-finish":
                    # Guard against double-counting: OpenCode may emit multiple
                    # message.part.updated events for the same step-finish part
                    # (e.g. pending → finalized state transitions).
                    if part_id not in seen_step_finish_ids:
                        seen_step_finish_ids.add(part_id)
                        num_turns += 1
                        # Guard against null and non-numeric values: use
                        # isinstance so string-typed numbers don't cause TypeError.
                        tokens = part.get("tokens") or {}
                        _inp = tokens.get("input", 0)
                        _out = tokens.get("output", 0)
                        _rsn = tokens.get("reasoning", 0)
                        total_input += int(_inp) if isinstance(_inp, (int, float)) else 0
                        total_output += int(_out) if isinstance(_out, (int, float)) else 0
                        total_reasoning += int(_rsn) if isinstance(_rsn, (int, float)) else 0
                        cache = tokens.get("cache") or {}
                        _cr = cache.get("read", 0)
                        _cw = cache.get("write", 0)
                        total_cache_read += int(_cr) if isinstance(_cr, (int, float)) else 0
                        total_cache_write += int(_cw) if isinstance(_cw, (int, float)) else 0
                        _cost = part.get("cost")
                        total_cost += _cost if isinstance(_cost, (int, float)) else 0.0

            # ── Turn completion ────────────────────────────────────────────
            # NOTE: we expect exactly one session.status idle event per turn.
            # If OpenCode ever emits multiple idle transitions (e.g. for
            # parallel sub-sessions), this return would cut the stream early.
            elif event_type == "session.status":
                # Guard against null and non-dict payloads: the server may
                # send "status": "idle" (a string) or "status": null.
                status = props.get("status") or {}
                if isinstance(status, dict) and status.get("type") == "idle":
                    text = "".join(
                        text_accumulator.get(pid, "") for pid in text_part_order
                    ).strip()
                    # Mirror _parse_http_response: empty text + no step-finish
                    # events signals an error turn (e.g. the model refused or
                    # the request was rejected before any content was produced).
                    has_step_finish = num_turns > 0
                    is_error = not text and not has_step_finish
                    if not text:
                        text = "(empty response)"

                    # Include all token buckets so that any non-zero token
                    # count — including reasoning-only or cache-only turns —
                    # produces a TokenUsage object rather than silently dropping
                    # the data.
                    has_usage = (
                        total_input + total_output + total_reasoning
                        + total_cache_read + total_cache_write
                    ) > 0
                    usage = (
                        TokenUsage(
                            input_tokens=total_input,
                            output_tokens=total_output,
                            cache_read_tokens=total_cache_read,
                            cache_write_tokens=total_cache_write,
                            reasoning_tokens=total_reasoning,
                        )
                        if has_usage
                        else None
                    )
                    # duration_ms is intentionally omitted here: the SSE stream
                    # does not carry timing metadata.  The blocking _post_message
                    # path derives duration_ms from info.duration in the JSON
                    # response body, but that field is unavailable over SSE.
                    yield AgentEvent(
                        kind="final",
                        response=AgentResponse(
                            text=text,
                            session_id=session_id,
                            usage=usage,
                            cost_usd=total_cost if total_cost > 0 else None,
                            num_turns=num_turns if num_turns > 0 else None,
                            is_error=is_error,
                        ),
                    )
                    return

            # ── Server-side error ──────────────────────────────────────────
            elif event_type == "session.error":
                error = props.get("error")
                # Extract a readable message: prefer "message" key in dict,
                # fall back to str() of non-empty non-dict values, then
                # fall back to a generic string for null / empty payloads.
                if isinstance(error, dict):
                    error_msg = error.get("message") or (str(error) if error else "unknown error")
                elif error:
                    error_msg = str(error)
                else:
                    error_msg = "unknown error"
                raise AgentExecutionError(f"OpenCode session error: {error_msg}")

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _require_base_url(self) -> None:
        """Raise clearly if the server URL hasn't been set yet."""
        if not self._base_url:
            raise RuntimeError(
                "OpenCodeBackend has no base_url — "
                "call start() before create_session() / send(), "
                "or use AgentSession as a context manager."
            )

    def _get_client(self) -> httpx.AsyncClient:
        """Return the long-lived HTTP client, raising if start() hasn't been called.

        Raises ``AgentUnavailableError`` (not ``RuntimeError``) when the client
        is None so that callers — including ``AgentTurnRunner`` and
        ``InjectedContextBuilder`` — can distinguish a shutdown-race condition from a
        permanent programming error.  The most common cause of a None client
        after ``_ensure_live_runtime()`` returns is a concurrent ``stop()``
        call that cleared ``_client`` between the live-runtime check and the
        actual HTTP call.
        """
        if self._client is None:
            raise AgentUnavailableError(
                "opencode sidecar has been stopped — call start() to restart"
            )
        return self._client

    async def _post_message(
        self,
        session_id: str,
        text: str,
        timeout: int | None = None,
        *,
        system: str | None = None,
    ) -> dict:
        """POST a text message to an existing opencode session.

        Raises :class:`AgentNotFoundError`, :class:`AgentExecutionError`, or
        :class:`AgentUnavailableError` on non-2xx responses — the original
        httpx.HTTPStatusError is NOT propagated because it includes the full
        request URL and response body, which may contain internal server details
        that should not be exposed to end users or leaked into RC chat.

        Callers must not silently catch 404 and create a new session — let the
        error propagate so the watcher fails loudly.

        ``system``: optional durable system-prompt content for this call (see
        ``PromptInput.system`` — a per-request field, not persisted on the
        session; must be resupplied on every call where it's wanted).
        """
        url = f"{self._base_url}/session/{session_id}/message"
        body: dict = {"parts": [{"type": "text", "text": text}]}
        if system:
            body["system"] = system
        effective_timeout = timeout if timeout is not None else self.timeout

        try:
            resp = await self._get_client().post(
                url, json=body, timeout=effective_timeout
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Sanitize: only expose status code and reason, not the full URL or
            # response body (which may contain internal opencode server details).
            message = (
                f"opencode API returned HTTP {exc.response.status_code} "
                f"for session {session_id[:16]!r}"
            )
            raise _classify_http_error(exc.response.status_code, message) from None

        if not resp.content:
            raise AgentUnavailableError(
                f"opencode API returned empty response body (HTTP {resp.status_code}) "
                f"for session {session_id[:16]!r} — "
                "server may have returned a non-JSON response or crashed mid-request"
            )
        try:
            return resp.json()
        except Exception:
            # Log raw body at DEBUG only — it may contain sidecar stack traces
            # or file-system paths that should not appear in user-facing messages.
            logger.debug(
                "opencode non-JSON response body for session %s (HTTP %d): %r",
                session_id[:16],
                resp.status_code,
                resp.text[:200],
            )
            raise AgentUnavailableError(
                f"opencode API returned non-JSON response (HTTP {resp.status_code}) "
                f"for session {session_id[:16]!r}"
            ) from None

    async def _cleanup_session_best_effort(self, session_id: str) -> bool:
        """Try to delete a failed session; return True if cleanup succeeded."""
        if not self._base_url or not session_id or self._client is None:
            return False
        url = f"{self._base_url}/session/{session_id}"
        try:
            resp = await self._get_client().delete(url)
            if resp.status_code in (200, 202, 204, 404):
                logger.info(
                    "Best-effort cleanup for failed opencode session %s returned HTTP %d",
                    session_id[:16],
                    resp.status_code,
                )
                self._orphan_session_ids.discard(session_id)
                return True
            logger.warning(
                "Best-effort cleanup for failed opencode session %s returned unexpected HTTP %d",
                session_id[:16],
                resp.status_code,
            )
        except Exception as e:
            logger.warning(
                "Best-effort cleanup failed for opencode session %s: %s",
                session_id[:16],
                e,
            )
        return False

    async def _cleanup_orphan_sessions_best_effort(self) -> None:
        """Try to clean up any sessions left orphaned by earlier failures."""
        if not self._orphan_session_ids or not self._base_url or self._client is None:
            return
        for session_id in list(self._orphan_session_ids):
            cleaned = await self._cleanup_session_best_effort(session_id)
            if cleaned:
                self._orphan_session_ids.discard(session_id)

    async def delete_session(self, session_id: str) -> bool:
        """Best-effort deletion hook used by watcher startup rollback."""
        return await self._cleanup_session_best_effort(session_id)

    def _parse_http_response(self, data: dict, session_id: str) -> AgentResponse:
        """Parse the synchronous POST /session/{id}/message response body.

        Response shape::

            {
              "info": {"duration": <ms>, ...},
              "parts": [
                {"type": "text", "text": "..."},
                {"type": "step-finish", "tokens": {"input": N, "output": N,
                  "reasoning": N, "cache": {"read": N, "write": N}},
                  "cost": 0.001},
                ...
              ]
            }

        Notes:
          - ``step-finish`` uses a hyphen, not an underscore (unlike the old CLI stream).
          - ``is_error`` is set to True when no text parts are extracted from the
            response (empty or tool-only turns).  This lets InjectedContextBuilder detect
            failed injection attempts without relying on the HTTP status code.
          - ``duration_ms`` is read from ``info.duration`` and coerced to ``int``.
            opencode returns this as a numeric millisecond count; non-numeric or
            null values are treated as unavailable (``None``).
        """
        # Guard against explicit JSON null for top-level fields.
        parts = data.get("parts") or []
        info = data.get("info") or {}

        text_parts: list[str] = []
        total_input = total_output = total_reasoning = 0
        total_cache_read = total_cache_write = 0
        total_cost: float = 0.0
        num_turns: int = 0

        for part in parts:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type", "")
            if ptype == "text":
                t = part.get("text", "")
                if t:
                    text_parts.append(t)
            elif ptype == "step-finish":
                num_turns += 1
                # Guard against null and non-numeric values (same as SSE path).
                tokens = part.get("tokens") or {}
                _inp = tokens.get("input", 0)
                _out = tokens.get("output", 0)
                _rsn = tokens.get("reasoning", 0)
                total_input += int(_inp) if isinstance(_inp, (int, float)) else 0
                total_output += int(_out) if isinstance(_out, (int, float)) else 0
                total_reasoning += int(_rsn) if isinstance(_rsn, (int, float)) else 0
                cache = tokens.get("cache") or {}
                _cr = cache.get("read", 0)
                _cw = cache.get("write", 0)
                total_cache_read += int(_cr) if isinstance(_cr, (int, float)) else 0
                total_cache_write += int(_cw) if isinstance(_cw, (int, float)) else 0
                _cost = part.get("cost")
                total_cost += _cost if isinstance(_cost, (int, float)) else 0.0

        text = "".join(text_parts).strip()
        is_error = False
        if not text:
            # Distinguish tool-only turns (agent ran tools, no text output) from
            # genuine errors (no parts at all, or only unrecognised part types).
            # Tool-only turns are valid — they produce step-finish events but no
            # text blocks.  Marking them as is_error=True would cause InjectedContextBuilder
            # to incorrectly count them as failed injection attempts.
            has_tool_steps = any(
                isinstance(p, dict) and p.get("type") == "step-finish" for p in parts
            )
            if has_tool_steps:
                logger.debug(
                    "No text extracted from opencode response but tool steps completed "
                    "(tool-only turn) — not marking as error."
                )
            else:
                logger.warning(
                    "No text extracted from opencode HTTP response. Parts: %s",
                    str(parts)[:500],
                )
                is_error = True
            text = "(empty response)"

        # Include all token buckets — reasoning-only and cache-only turns must
        # also produce a TokenUsage object (same logic as SSE path).
        has_usage = (
            total_input + total_output + total_reasoning
            + total_cache_read + total_cache_write
        ) > 0
        usage = (
            TokenUsage(
                input_tokens=total_input,
                output_tokens=total_output,
                cache_read_tokens=total_cache_read,
                cache_write_tokens=total_cache_write,
                reasoning_tokens=total_reasoning,
            )
            if has_usage
            else None
        )

        return AgentResponse(
            text=text,
            session_id=session_id,
            usage=usage,
            cost_usd=total_cost if total_cost > 0 else None,
            duration_ms=(
                int(_dur)
                if isinstance(_dur := info.get("duration"), (int, float))
                else None
            ),
            num_turns=num_turns if num_turns > 0 else None,
            is_error=is_error,
        )
