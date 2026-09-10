"""InjectedContextBuilder: builds durable session context and delivers it.

Split responsibilities (see gateway issue #52 — durable system prompt):

  - ``build()`` is pure-ish: it does file I/O (reading configured context
    files) but never calls ``agent.send()`` or touches any AgentBackend. It
    combines the ACG identity/addressing header (``prompt_builder.build_system_header``)
    with the user's configured ``context_inject_files`` content into a single
    string.
  - ``ensure()`` wraps ``agent.ensure_durable_instructions()`` with retry
    bookkeeping (``InjectionStatus``). Each backend decides HOW to make the
    built content durable: Claude writes it to a file and returns a path for
    the caller to re-supply via ``--append-system-prompt-file`` on every turn;
    the default fallback (used by OpenCode today) sends it once as a normal
    message, matching the pre-#52 behavior.

``history_context`` (channel history handoff) is intentionally NOT handled by
this class anymore — it is genuinely one-time/volatile content, not part of
the durability fix, and is now sent directly by the caller
(``WatcherLifecycle._start_watcher``) as a simple best-effort one-time send.

Uses asyncio.to_thread for non-blocking file I/O in ``build()``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from ..agents import AgentBackend
from ..agents.errors import AgentExecutionError
from .config import CoreConfig, WatcherConfig
from .prompt_builder import build_system_header
from .state import WatcherState

logger = logging.getLogger("coop.core.injected_context_builder")

_MAX_FILE_SIZE = 256 * 1024  # 256 KB per file
_MAX_CONTEXT_SIZE = 512 * 1024  # 512 KB total
_MAX_INJECT_ATTEMPTS = 3  # Give up after this many agent-error responses


@dataclass
class InjectionStatus:
    state: str = "not_started"  # not_started | pending | failed_retryable | failed_degraded | injected
    failure_count: int = 0
    last_error: str | None = None


class InjectedContextBuilder:
    """Builds gateway session context and ensures it durably reaches the agent.

    ``build()`` reads the concatenated list of context files from all three
    layers (connector → agent → watcher), combines them with the ACG identity
    header, and returns the combined string. Raises on hard errors (e.g.
    missing file) — caller must handle.

    ``ensure()`` calls ``agent.ensure_durable_instructions()`` with the built
    content, tracking retry state per session so that repeated agent-error
    responses do not retry forever. If the backend returns a non-``None``
    value, the caller must re-supply it on every subsequent turn (Claude's
    ``--append-system-prompt-file``); ``None`` means the backend fully
    handled delivery itself (a one-time side-effecting send).

    Retry behavior:
      - ``ensure()`` is called unconditionally on every watcher start.
      - If ``agent.ensure_durable_instructions()`` raises ``AgentExecutionError``,
        the failure counter increments and the session stays retryable until
        ``_MAX_INJECT_ATTEMPTS`` consecutive failures, at which point the
        session is marked degraded (``ws.context_injected = True``) to avoid
        unbounded retries.
    """

    def __init__(self, config: CoreConfig) -> None:
        self._config = config
        # In-memory injection status keyed by session_id. Not persisted: a gateway
        # restart resets the counters, giving the agent a fresh chance.
        self._inject_status: dict[str, InjectionStatus] = {}
        # Per-session locks serialize concurrent ensure() calls for a shared
        # (pinned) session_id across DIFFERENT watchers — WatcherLifecycle
        # already prevents the SAME watcher name from calling _start_watcher()
        # concurrently with itself (see _get_watcher_lock in watcher_lifecycle.py),
        # so any collision here is always two distinct watchers, each needing
        # its own agent.ensure_durable_instructions() call (different
        # watcher_name/content) — never redundant duplicate work to dedupe.
        # Not persisted/cleaned up: same rationale as _inject_status above;
        # a finished (unlocked) Lock left in this dict is inert.
        self._locks: dict[str, asyncio.Lock] = {}

    def status_for(self, session_id: str) -> InjectionStatus:
        """Return the current injection status for a session."""
        return self._inject_status.get(session_id, InjectionStatus())

    def reset_session(self, session_id: str) -> None:
        """Clear injection state for a session, resetting retry counters.

        Called by watcher reset so that the next injection attempt starts with
        a fresh failure counter.  Without this, a ``failed_degraded`` session
        that is reset would immediately re-enter ``failed_degraded`` on the
        very first retry because the old failure_count is still at or above
        ``_MAX_INJECT_ATTEMPTS``.
        """
        self._inject_status.pop(session_id, None)

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    async def build(
        self,
        agent_name: str,
        connector_name: str,
        wc: WatcherConfig,
        *,
        agent_username: str = "",
    ) -> str:
        """Read configured context files and combine with the identity header.

        Resolves ``context_inject_files`` via the connector → agent → watcher
        layering (``CoreConfig.context_inject_files_for``), reads each file
        (subject to ``_MAX_FILE_SIZE``/``_MAX_CONTEXT_SIZE``), and prepends
        the ACG identity + multi-agent addressing header
        (``prompt_builder.build_system_header``).

        Raises:
            FileNotFoundError: If a configured context file does not exist.

        Returns:
            The combined header + file content string. The header itself is
            never empty, so in practice this only returns whatever survives
            the size gates below plus the identity block.
        """
        files = self._config.context_inject_files_for(
            connector_name, agent_name, wc.context_inject_files
        )

        combined_context: list[str] = []
        for path_str in files:
            p = Path(path_str)
            exists = await asyncio.to_thread(p.exists)
            if not exists:
                raise FileNotFoundError(
                    f"context_inject_files entry not found for watcher '{wc.name}': {p}"
                )
            # Pre-check via stat to skip obviously oversized files early and
            # avoid reading them into memory unnecessarily.  However, the stat
            # result can race with the actual read (TOCTOU), so we re-check the
            # content length after reading — this is the authoritative limit.
            file_stat = await asyncio.to_thread(p.stat)
            if file_stat.st_size > _MAX_FILE_SIZE:
                logger.warning(
                    "Context file %s exceeds %d KB limit (%d KB) — skipping",
                    p.name,
                    _MAX_FILE_SIZE // 1024,
                    file_stat.st_size // 1024,
                )
                continue
            content = await asyncio.to_thread(p.read_text, encoding="utf-8")
            # Re-check after read to close TOCTOU window: a file could grow
            # between the stat() and read_text() calls (e.g., a log file being
            # appended to).  This is the authoritative size gate.
            if len(content.encode()) > _MAX_FILE_SIZE:
                logger.warning(
                    "Context file %s exceeded %d KB limit after read — skipping",
                    p.name,
                    _MAX_FILE_SIZE // 1024,
                )
                continue
            combined_context.append(content)

        # The identity/addressing header is unconditional — it must survive
        # even when there are no context files (or all were skipped for size).
        combined_context.insert(0, build_system_header(wc, agent_username))

        full_context = "\n\n".join(combined_context)
        encoded = full_context.encode()
        if len(encoded) > _MAX_CONTEXT_SIZE:
            logger.warning(
                "Total context size exceeds %d KB limit — truncating",
                _MAX_CONTEXT_SIZE // 1024,
            )
            full_context = encoded[:_MAX_CONTEXT_SIZE].decode(errors="ignore")
            full_context += "\n\n[... context truncated due to size limit ...]"
        return full_context

    async def ensure(
        self,
        ws: WatcherState,
        session_id: str,
        agent: AgentBackend,
        working_directory: str,
        timeout: int,
        watcher_name: str,
        path_key: str,
        content: str,
    ) -> str | None:
        """Call agent.ensure_durable_instructions(), with retry bookkeeping.

        CRITICAL: this method must be called on EVERY watcher start, UNCONDITIONALLY —
        never gated on ``ws.context_injected`` at the top. That gate is about whether
        the ONE-TIME side-effecting delivery (a backend's default send()-based
        fallback) has already happened — it must NOT suppress backends (like Claude)
        whose ensure_durable_instructions() has no side effect and must return a
        fresh value on every call, including for RESUMED sessions after a gateway
        restart.

        Passes ``already_delivered=ws.context_injected`` DOWN into the backend call,
        letting each backend decide what that historical fact means for its own
        mechanism (Claude ignores it; the default fallback uses it to skip
        re-sending into conversation history).

        Concurrency: calls for the SAME session_id are serialized (not
        deduplicated) via a per-session lock — see ``_lock_for()``. An earlier
        version bailed out with ``None`` when a call was already in-flight,
        which silently dropped a second watcher's durable content forever
        (no retry path once the per-message retry loop was removed) whenever
        two watchers shared a pinned session_id. Waiting and then still
        running this call's own attempt fixes that at the cost of a short
        wait in the (rare) contended case — never dropping the work.
        """
        async with self._lock_for(session_id):
            status = self._inject_status.setdefault(session_id, InjectionStatus())
            status.state = "pending"
            try:
                to_repeat = await agent.ensure_durable_instructions(
                    session_id,
                    working_directory,
                    timeout,
                    content,
                    path_key=path_key,
                    already_delivered=ws.context_injected,
                )
            except AgentExecutionError as e:
                status.failure_count += 1
                status.last_error = str(e)[:200]
                if status.failure_count >= _MAX_INJECT_ATTEMPTS:
                    status.state = "failed_degraded"
                    ws.context_injected = True
                    logger.error(
                        "Context injection failed %d times for watcher '%s' (session %s) — "
                        "marking degraded.  Last error: %s",
                        status.failure_count,
                        watcher_name,
                        session_id[:8],
                        status.last_error,
                    )
                else:
                    status.state = "failed_retryable"
                    logger.warning(
                        "Context injection failed for watcher '%s' (session %s) "
                        "[attempt %d/%d]: %s",
                        watcher_name,
                        session_id[:8],
                        status.failure_count,
                        _MAX_INJECT_ATTEMPTS,
                        status.last_error,
                    )
                return None
            except Exception:
                # Unexpected error (e.g. PermissionError, OSError during file I/O).
                # Reset status so the next ensure() call can retry.
                #
                # Note: failure_count is intentionally NOT reset here — it is only
                # incremented in the AgentExecutionError branch above, so an
                # unexpected exception does not count as an inject attempt.
                if status.state == "pending":
                    status.state = "not_started"
                raise
            self._inject_status[session_id] = InjectionStatus(state="injected")
            ws.context_injected = True
            logger.info(
                "Context ensured for watcher '%s' (session %s)",
                watcher_name,
                session_id[:8],
            )
            return to_repeat
