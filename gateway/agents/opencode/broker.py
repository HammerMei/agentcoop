"""OpenCode permission broker — SSE listener + HTTP reply API.

opencode has a built-in permission system that:
  1. Emits a `permission.updated` Server-Sent Event when a tool needs approval.
  2. Blocks tool execution until POST /permission/{requestID}/reply is called.

This broker listens to the SSE stream, enforces guest tool restrictions,
posts RC notifications for owner-approval requests, and calls the reply API
when the owner approves or denies via RC chat slash command.

SSE event format (confirmed from live traffic)::

    {
      "type": "permission.asked",
      "properties": {
        "id": "per_...",
        "sessionID": "ses_...",
        "permission": "bash",
        "patterns": ["ls", "printf 'hello world 123\\n'"],
        "metadata": {},
        "always": ["ls *", "printf *"],
        "tool": { "messageID": "msg_...", "callID": "call_..." }
      }
    }

All permission fields are nested under ``properties``, not at the top level.
The tool name is in ``properties.permission`` (not ``properties.type``).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

import httpx

from ...core.permission import (
    PermissionBroker,
    PermissionNotificationError,
    PermissionNotifier,
    PermissionRegistry,
)
from ...core.tool_match import all_params_match_any, get_param_strings_for_opencode

if TYPE_CHECKING:
    from ...config import ToolRule

logger = logging.getLogger("coop.permissions.opencode")


class OpenCodePermissionBroker(PermissionBroker):
    """Broker that integrates with opencode's native permission system.

    opencode blocks tool execution internally until we call its reply API.
    This broker listens to the SSE event stream for ``permission.updated`` events,
    enforces guest tool restrictions (auto-deny disallowed tools without bothering
    the owner), posts RC notifications for owner sessions, and calls
    POST /permission/{requestID}/reply when the owner responds.

    Unlike ClaudePermissionBroker, there is no HTTP connection to hold open —
    opencode handles the blocking internally, and we unblock it via a separate
    API call.

    Guest enforcement mirrors ClaudePermissionBroker: tools matching any pattern
    in ``guest_allowed_tools`` are auto-approved; all others are auto-denied without
    posting a 🔐 notification to RC.
    """

    def __init__(
        self,
        registry: PermissionRegistry,
        notifier: PermissionNotifier,
        opencode_base_url: str,
        session_room_map: dict[str, str],
        session_role_map: dict[str, str] | None = None,
        session_permission_thread_map: "dict[str, str | None] | None" = None,
        owner_allowed_tools: "list[ToolRule] | None" = None,
        guest_allowed_tools: "list[ToolRule] | None" = None,
        timeout_seconds: int = 300,
        skip_owner_approval: bool = False,
    ) -> None:
        super().__init__(registry, notifier, timeout_seconds)
        self._base_url = opencode_base_url.rstrip("/")
        self._session_room_map = session_room_map
        self._session_role_map: dict[str, str] = (
            session_role_map if session_role_map is not None else {}
        )
        self._session_permission_thread_map: dict[str, str | None] = (
            session_permission_thread_map
            if session_permission_thread_map is not None
            else {}
        )
        self._owner_allowed_tools: list[ToolRule] = (
            owner_allowed_tools if owner_allowed_tools is not None else []
        )
        self._guest_allowed_tools: list[ToolRule] = (
            guest_allowed_tools if guest_allowed_tools is not None else []
        )
        self._skip_owner_approval: bool = skip_owner_approval
        self._sse_task: asyncio.Task | None = None
        self._pending_tasks: set[asyncio.Task] = set()
        self._pending_request_ids: dict[asyncio.Task, tuple[str, bool]] = {}
        # Bound concurrent permission request handling to avoid unbounded task creation
        # during bursts of permission events from the SSE stream.
        self._permission_sem = asyncio.Semaphore(10)
        # Long-lived HTTP client for reply API calls — reused across all permission
        # replies to avoid per-request connection churn.
        self._reply_client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        self._reply_client = httpx.AsyncClient(timeout=10.0)
        self._sse_task = asyncio.create_task(
            self._listen_sse(), name="opencode-permission-sse"
        )
        logger.info("OpenCode permission SSE listener started (url=%s)", self._base_url)

    async def stop(self) -> None:
        if self._sse_task:
            self._sse_task.cancel()
            try:
                await self._sse_task
            except asyncio.CancelledError:
                pass
            finally:
                self._sse_task = None
        pending_tasks = list(self._pending_tasks)
        pending_request_ids = {
            task: self._pending_request_ids.get(task, ("", False))
            for task in pending_tasks
        }
        for task in pending_tasks:
            task.cancel()
        if pending_tasks:
            results = await asyncio.gather(*pending_tasks, return_exceptions=True)
            # For tasks flagged reject_on_cancel, send a deny reply to opencode so
            # that blocked tool calls are not left pending indefinitely.  Gather all
            # deny calls concurrently and apply a total timeout to keep shutdown fast
            # even if the opencode HTTP endpoint is slow or unreachable.
            deny_coros = [
                self._reply_to_opencode(req_id, approved=False)
                for task, result in zip(pending_tasks, results, strict=False)
                for req_id, reject_on_cancel in [pending_request_ids.get(task, ("", False))]
                if (
                    reject_on_cancel
                    and req_id
                    and (task.cancelled() or isinstance(result, asyncio.CancelledError))
                )
            ]
            if deny_coros:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*deny_coros, return_exceptions=True),
                        timeout=15.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "Timed out sending deny-on-cancel replies to opencode during shutdown"
                    )
        self._pending_tasks.clear()
        self._pending_request_ids.clear()
        if self._reply_client:
            await self._reply_client.aclose()
            self._reply_client = None

    # ── SSE listener ──────────────────────────────────────────────────────────

    def _queue_auto_reply(self, opencode_req_id: str, approved: bool) -> None:
        """Fire-and-forget an auto-approve/deny reply as a tracked background task.

        Using create_task here avoids blocking the SSE reader loop on an HTTP reply
        call (which can take 100ms+ under load).  The task is added to
        ``_pending_tasks`` so stop() can cancel and await it on shutdown.
        """
        task = asyncio.create_task(
            self._reply_to_opencode(opencode_req_id, approved=approved),
            name=f"opencode-auto-reply-{opencode_req_id[:8] if opencode_req_id else 'unknown'}",
        )
        self._track_pending_task(task, opencode_req_id, reject_on_cancel=False)

    async def _listen_sse(self) -> None:
        """Long-running SSE consumer. Reconnects automatically on disconnect."""
        url = f"{self._base_url}/event"
        while True:
            try:
                # connect timeout prevents hanging forever if opencode is slow to accept.
                # read timeout is None so the long-lived SSE stream is not cut off.
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(
                        connect=10.0, read=None, write=10.0, pool=10.0
                    )
                ) as client:
                    async with client.stream("GET", url) as response:
                        response.raise_for_status()
                        logger.debug("Connected to opencode SSE stream")
                        async for line in response.aiter_lines():
                            await self._handle_sse_line(line)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("SSE connection lost: %s — reconnecting in 3s", e)
                await asyncio.sleep(3)

    async def _handle_sse_line(self, line: str) -> None:
        if not line.startswith("data:"):
            if line:  # suppress blank separator lines from debug output
                logger.debug("SSE non-data line: %r", line)
            return
        raw = line[5:].strip()
        logger.debug("SSE data: %s", raw[:300])
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            logger.warning("SSE malformed JSON: %r", raw[:200])
            return

        event_type = payload.get("type", "")
        if event_type != "permission.asked":
            logger.debug("SSE ignoring event type %r", event_type)
            return

        props = payload.get("properties", {})
        opencode_req_id = props.get("id", "")
        tool_name = props.get("permission", "")
        session_id = props.get("sessionID", "")
        # Use metadata if populated; fall back to patterns (the actual commands/paths)
        metadata = props.get("metadata", {})
        patterns = props.get("patterns", [])
        tool_input: dict = (
            metadata if metadata else ({"commands": patterns} if patterns else {})
        )

        room_id = self._session_room_map.get(session_id, "")
        if not room_id:
            # Without a room mapping we cannot post RC notifications.
            # Exception: owners with skip_owner_approval enabled never need RC
            # notifications, so they can be auto-approved without routing context.
            role_for_guard = self._session_role_map.get(session_id, "guest")
            if role_for_guard == "owner" and self._skip_owner_approval:
                logger.debug(
                    "Owner: auto-approving %r for session %s "
                    "(skip_owner_approval; no room mapping needed)",
                    tool_name,
                    session_id[:8] if session_id else "?",
                )
                self._queue_auto_reply(opencode_req_id, approved=True)
            else:
                logger.warning(
                    "No room_id for session %s — auto-DENYING tool %s (no routing context)",
                    session_id[:8] if session_id else "?",
                    tool_name,
                )
                self._queue_auto_reply(opencode_req_id, approved=False)
            return

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
        # Require ALL patterns to match — OpenCode provides one pattern per AST
        # sub-command for compound bash expressions (e.g. "echo hi && rm -rf /").
        # Checking only patterns[0] would allow dangerous sub-commands to slip through.
        param_strings = get_param_strings_for_opencode(patterns)

        if role == "guest":
            approved = all_params_match_any(
                self._guest_allowed_tools, tool_name, param_strings
            )
            if approved:
                logger.debug(
                    "Guest: auto-approving %r for session %s (matched guest_allowed_tools)",
                    tool_name,
                    session_id[:8],
                )
            else:
                logger.info(
                    "Guest: auto-denying %r for session %s (not in guest_allowed_tools)",
                    tool_name,
                    session_id[:8],
                )
            # Dispatch as background task — do not block the SSE reader loop on an
            # HTTP reply call (which could take 100ms+ under load).
            self._queue_auto_reply(opencode_req_id, approved=approved)
            return

        # Owner: skip_owner_approval bypasses all checks — auto-approve without RC notification
        if self._skip_owner_approval:
            logger.debug(
                "Owner: auto-approving %r for session %s (skip_owner_approval enabled)",
                tool_name,
                session_id[:8],
            )
            self._queue_auto_reply(opencode_req_id, approved=True)
            return

        # Owner: check owner_allowed_tools — auto-approve without RC notification
        if all_params_match_any(self._owner_allowed_tools, tool_name, param_strings):
            logger.debug(
                "Owner: auto-approving %r for session %s (matched owner_allowed_tools)",
                tool_name,
                session_id[:8],
            )
            self._queue_auto_reply(opencode_req_id, approved=True)
            return

        thread_id = self._session_permission_thread_map.get(session_id)

        # Handle in background so the SSE loop isn't blocked
        task = asyncio.create_task(
            self._handle_permission_request(
                opencode_req_id=opencode_req_id,
                tool_name=tool_name,
                tool_input=tool_input,
                session_id=session_id,
                room_id=room_id,
                thread_id=thread_id,
            ),
            name=f"opencode-perm-{opencode_req_id[:8] if opencode_req_id else 'unknown'}",
        )
        self._track_pending_task(task, opencode_req_id, reject_on_cancel=True)

    def _track_pending_task(
        self, task: asyncio.Task, request_id: str = "", reject_on_cancel: bool = False
    ) -> None:
        """Track a background task and optionally associate it with a permission id."""
        self._pending_tasks.add(task)
        if request_id:
            self._pending_request_ids[task] = (request_id, reject_on_cancel)

        def _cleanup(done_task: asyncio.Task) -> None:
            self._pending_tasks.discard(done_task)
            self._pending_request_ids.pop(done_task, None)

        task.add_done_callback(_cleanup)

    async def _handle_permission_request(
        self,
        opencode_req_id: str,
        tool_name: str,
        tool_input: dict,
        session_id: str,
        room_id: str,
        thread_id: str | None = None,
    ) -> None:
        async with self._permission_sem:
            try:
                approved = await self.request_permission(
                    tool_name=tool_name,
                    tool_input=tool_input,
                    session_id=session_id,
                    room_id=room_id,
                    thread_id=thread_id,
                )
            except PermissionNotificationError as e:
                logger.warning("Permission notification failed: %s — auto-denying", e)
                approved = False
            await self._reply_to_opencode(opencode_req_id, approved)

    async def _reply_to_opencode(self, opencode_req_id: str, approved: bool) -> None:
        """Call opencode's reply API to unblock the tool."""
        if not opencode_req_id:
            return
        reply = "once" if approved else "reject"
        url = f"{self._base_url}/permission/{opencode_req_id}/reply"
        try:
            if not self._reply_client:
                logger.error("Reply client not initialized — broker not started?")
                return
            resp = await self._reply_client.post(url, json={"reply": reply})
            resp.raise_for_status()
            logger.info("Replied %r to opencode permission %s", reply, opencode_req_id)
        except Exception as e:
            logger.error(
                "Failed to reply to opencode permission %s: %s", opencode_req_id, e
            )
