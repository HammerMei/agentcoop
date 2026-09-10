"""Claude Code permission broker — HTTP PreToolUse hook server.

Claude Code's HTTP hook type POSTs to a local endpoint when PreToolUse fires.
This broker runs a minimal asyncio HTTP server, holds the connection open while
waiting for owner approval via RC chat, then returns allow/deny JSON.

The generated settings file is passed to claude via --settings <path>.
Option C: when a ``backend`` reference is provided (gateway service flow),
the broker patches ``backend.settings_path`` in ``start()`` automatically,
keeping GatewayService free of any Claude-internal knowledge.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from ...core.permission import (
    PermissionBroker,
    PermissionNotificationError,
    PermissionNotifier,
    PermissionRegistry,
)
from ...core.tool_match import all_params_match_any, get_param_strings_for_claude
from ._http_utils import build_error_response, build_http_response, read_http_body
from .settings_adapter import ClaudeSettingsAdapter

if TYPE_CHECKING:
    from ...config import ToolRule
    from .adapter import ClaudeBackend

logger = logging.getLogger("coop.permissions.claude")

_HOST = "127.0.0.1"

# Claude Code meta-tools that load deferred tool schemas — no side effects,
# always safe to auto-allow regardless of role or allow-list configuration.
# Real security is enforced when the *actual* tool (e.g. WebSearch) is invoked.
_CLAUDE_META_TOOLS: frozenset[str] = frozenset({"ToolSearch"})


class ClaudePermissionBroker(PermissionBroker):
    """Broker that intercepts Claude Code tool calls via HTTP PreToolUse hook.

    Lifecycle:
      1. start() → bind asyncio HTTP server to a random localhost port,
         write a settings JSON file with the hook URL.
         If ``backend`` was provided (Option C), also patches
         ``backend.settings_path`` so every ``claude -p`` invocation picks
         up the hook URL automatically via ``--settings``.
      2. ClaudeBackend passes --settings <path> to every claude -p invocation.
      3. Claude POSTs to http://127.0.0.1:<port>/hook on each PreToolUse event.
      4. Hook handler calls request_permission(), which posts to RC and awaits
         the asyncio.Future — the HTTP connection stays open (Claude is paused).
      5. Owner replies /approve <id> or /deny <id> in RC chat.
      6. Future resolves → hook handler returns {"decision": "allow/deny"}.
      7. Claude resumes or skips the tool call.
    """

    def __init__(
        self,
        registry: PermissionRegistry,
        notifier: PermissionNotifier,
        session_room_map: dict[str, str],
        session_role_map: dict[str, str] | None = None,
        session_permission_thread_map: "dict[str, str | None] | None" = None,
        owner_allowed_tools: "list[ToolRule] | None" = None,
        guest_allowed_tools: "list[ToolRule] | None" = None,
        timeout_seconds: int = 300,
        skip_owner_approval: bool = False,
        backend: "ClaudeBackend | None" = None,
    ) -> None:
        super().__init__(registry, notifier, timeout_seconds)
        self._session_room_map = session_room_map
        self._session_role_map = session_role_map if session_role_map is not None else {}
        self._session_permission_thread_map: dict[str, str | None] = (
            session_permission_thread_map if session_permission_thread_map is not None else {}
        )
        self._owner_allowed_tools: list[ToolRule] = owner_allowed_tools if owner_allowed_tools is not None else []
        self._guest_allowed_tools: list[ToolRule] = guest_allowed_tools if guest_allowed_tools is not None else []
        self._skip_owner_approval: bool = skip_owner_approval
        self._server: asyncio.Server | None = None
        self._port: int = 0
        # Tracks every in-flight _handle_connection coroutine so that stop()
        # can cancel and await them — without this, a connection blocked at
        # request_permission() (up to 300 s) would continue running and access
        # resources that have already been torn down.
        self._connection_tasks: set[asyncio.Task] = set()
        # Settings file lifecycle and backend wiring — extracted to its own class
        # so the broker stays focused on approval transport and policy.
        self._settings = ClaudeSettingsAdapter(backend=backend)

    @property
    def settings_path(self) -> str:
        """Absolute path to the generated Claude settings JSON file.

        Pass as --settings <path> to every claude -p invocation.
        Empty string if start() has not been called yet.
        """
        return self._settings.settings_path

    async def start(self) -> None:
        # Bind to port 0 → OS assigns a free port
        self._server = await asyncio.start_server(
            self._handle_connection, _HOST, 0
        )
        self._port = self._server.sockets[0].getsockname()[1]
        logger.info("Claude permission HTTP hook server on port %d", self._port)

        # Delegate settings file creation and backend patching to the adapter
        self._settings.write_hook_config(
            port=self._port, timeout_seconds=self._timeout_seconds,
        )
        self._settings.patch_backend()

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        # Cancel all in-flight connections so they do not linger after teardown.
        # Connections stuck at request_permission() can block for up to
        # ``timeout_seconds`` (default 300 s) and would access the connector,
        # registry, and notifier after they have been cleaned up.
        if self._connection_tasks:
            tasks = list(self._connection_tasks)
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        self._settings.cleanup()

    # ── Minimal asyncio HTTP server ───────────────────────────────────────────

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle one HTTP connection from Claude Code."""
        task = asyncio.current_task()
        if task is not None:
            self._connection_tasks.add(task)
        try:
            body = await read_http_body(reader)
            response_body = await self._handle_hook(body)
            writer.write(build_http_response(response_body))
            await writer.drain()
        except asyncio.CancelledError:
            # Broker is shutting down — close the connection silently.
            raise
        except Exception as e:
            logger.error("Error handling hook connection: %s — blocking tool as safe default", e)
            writer.write(build_error_response(
                "Permission broker internal error — tool blocked as safe default. Please retry."
            ))
            await writer.drain()
        finally:
            if task is not None:
                self._connection_tasks.discard(task)
            writer.close()
            await writer.wait_closed()

    # ── Pure policy decision (synchronous, independently testable) ───────────

    def _decide(
        self,
        tool_name: str,
        param_strings: list[str],
        role: str,
        room_id: str,
    ) -> tuple[str, str]:
        """Return a ``(action, reason)`` policy decision for a tool call.

        This method is **pure** — it reads only ``self._guest_allowed_tools``,
        ``self._owner_allowed_tools``, and the arguments.  It performs no async
        I/O and holds no locks, making it independently unit-testable.

        ``action`` is one of:
          - ``"allow"``  — auto-approve without notifying the owner
          - ``"block"``  — reject immediately; ``reason`` explains why
          - ``"ask"``    — route to ``request_permission()`` for owner approval

        ``reason`` is a human-readable string (meaningful only when
        ``action == "block"``; empty otherwise).
        """
        # Meta-tools (e.g. ToolSearch) only load deferred schemas — no side
        # effects.  Always auto-allow before any role check so they never surface
        # as a spurious approval request.
        if tool_name in _CLAUDE_META_TOOLS:
            return "allow", ""

        if role == "guest":
            if all_params_match_any(self._guest_allowed_tools, tool_name, param_strings):
                return "allow", ""
            return "block", f"Guest: tool '{tool_name}' is not permitted"

        # Owner path — skip_owner_approval bypasses all checks and auto-approves
        if self._skip_owner_approval:
            return "allow", ""

        # Owner path — check auto-allow list first
        if all_params_match_any(self._owner_allowed_tools, tool_name, param_strings):
            return "allow", ""

        # Owner + unlisted tool: need a room to post the approval request
        if not room_id:
            return "block", (
                "Permission broker has no room mapping for this session "
                "— tool blocked as safe default. Please retry your request."
            )

        return "ask", ""

    # ── Hook handler (orchestrator: I/O + policy via _decide) ────────────────

    async def _handle_hook(self, raw_body: str) -> str:
        """Process a PreToolUse POST body and return a JSON decision string.

        Orchestrates the full flow:
          1. JSON parse
          2. Extract fields and log
          3. Compute param_strings (offloaded to thread — tree-sitter is sync C)
          4. Resolve role with fail-closed default
          5. Delegate policy decision to ``_decide()``
          6. Execute the decision: auto-allow/block or request owner approval
        """
        try:
            body = json.loads(raw_body) if raw_body else {}
        except json.JSONDecodeError:
            logger.warning("Failed to parse hook body: %r", raw_body[:200])
            return json.dumps({"decision": "block", "reason": "Malformed hook body — tool blocked as safe default."})

        tool_name: str = body.get("tool_name", "")
        tool_input: dict = body.get("tool_input", {})
        # cwd is passed in the hook body by Claude Code; used for path normalization.
        cwd: str = body.get("cwd", "")
        session_id: str = body.get("session_id", "")
        logger.info(
            "Hook received: tool=%r session=%s input_keys=%s",
            tool_name,
            session_id[:8] or "?",
            list(tool_input.keys()),
        )

        # get_param_strings_for_claude calls tree-sitter's parser.parse() which
        # is a synchronous C extension — offload to a thread so it cannot stall
        # the event loop while parsing large bash scripts.
        param_strings = await asyncio.to_thread(
            get_param_strings_for_claude, tool_name, tool_input, cwd
        )

        # Fail-closed: unknown session defaults to "guest" (least privilege).
        # "owner" would be a fail-open default — if the session-role mapping is
        # absent due to startup ordering or an unexpected state gap, tool calls
        # must not silently gain elevated permissions.
        role = self._session_role_map.get(session_id, "guest")
        if role == "guest" and session_id not in self._session_role_map:
            logger.warning(
                "No role mapping for session %s — defaulting to 'guest' (fail-closed)",
                session_id[:8] if session_id else "?",
            )

        room_id = self._session_room_map.get(session_id, "")
        action, reason = self._decide(tool_name, param_strings, role, room_id)

        if action == "allow":
            logger.debug(
                "%s: auto-approving %r (matched allowed_tools)",
                role.capitalize(), tool_name,
            )
            return json.dumps({"decision": "allow"})

        if action == "block":
            logger.warning(
                "Blocking tool %r for %s session %s: %s",
                tool_name, role, session_id[:8] if session_id else "?", reason,
            )
            return json.dumps({"decision": "block", "reason": reason})

        # action == "ask" — post notification and wait for owner approval
        thread_id = self._session_permission_thread_map.get(session_id)
        try:
            approved = await self.request_permission(
                tool_name=tool_name,
                tool_input=tool_input,
                session_id=session_id,
                room_id=room_id,
                thread_id=thread_id,
            )
        except PermissionNotificationError as e:
            logger.warning("Permission notification failed: %s", e)
            return json.dumps({
                "decision": "block",
                "reason": (
                    "Permission request could not be delivered to chat "
                    "(connection error — the owner was never asked). "
                    "Please retry your request."
                ),
            })

        if approved:
            return json.dumps({"decision": "allow"})
        return json.dumps({
            "decision": "block",
            "reason": "Denied by owner via RC chat (owner replied 'deny <id>')",
        })

    # Settings file creation and backend wiring have been extracted to
    # ClaudeSettingsAdapter (gateway.agents.claude.settings_adapter).
