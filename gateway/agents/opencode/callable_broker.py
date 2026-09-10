"""Callable-based permission broker for OpenCode AgentSession scripting use.

Mirrors CallablePermissionBroker but uses OpenCode's SSE + reply API instead
of Claude's PreToolUse HTTP hook mechanism.

OpenCode blocks tool execution until POST /permission/{id}/reply is called.
This broker listens to the SSE event stream for ``permission.asked`` events,
calls the user-provided async handler to approve or deny, then replies.

Usage with AgentSession::

    async def my_handler(tool_name: str, tool_input: dict) -> bool:
        return tool_name.lower() in ("read", "glob")   # approve only safe tools

    async with AgentSession(
        OpenCodeBackend("opencode", [], 120),
        "/my/project",
        permission_handler=my_handler,
    ) as session:
        reply = await session.send("List the Python files here")

The broker is created and managed automatically by AgentSession via
``OpenCodeBackend.create_callable_broker()``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable

import httpx

logger = logging.getLogger("coop.permissions.opencode_callable")

PermissionHandler = Callable[[str, dict], Awaitable[bool]]


class OpenCodeCallablePermissionBroker:
    """Permission broker for OpenCode that delegates decisions to an async callable.

    Listens to OpenCode's SSE event stream for ``permission.asked`` events,
    calls ``permission_handler(tool_name, tool_input)`` for each one, and
    replies to OpenCode's reply API with the result.

    Unlike :class:`~gateway.agents.claude.callable_broker.CallablePermissionBroker`
    (which works via Claude's PreToolUse HTTP hook), this broker is SSE-based and
    requires no backend patching — it communicates directly with the opencode server.

    Lifecycle::

        broker = OpenCodeCallablePermissionBroker(base_url, my_handler)
        await broker.start()   # begins SSE listening
        ...
        await broker.stop()    # cancels SSE task

    Typically managed automatically by AgentSession.
    """

    def __init__(
        self,
        base_url: str,
        permission_handler: PermissionHandler,
        timeout_seconds: int = 300,
    ) -> None:
        """
        Args:
            base_url: Base URL of the running opencode server
                (e.g. ``"http://127.0.0.1:54321"``).
            permission_handler: Async callable invoked for each tool call.
                Signature: ``async (tool_name: str, tool_input: dict) -> bool``.
                Return ``True`` to allow, ``False`` to deny.
            timeout_seconds: Seconds to wait for the handler before auto-denying.
        """
        self._base_url = base_url.rstrip("/")
        self._handler = permission_handler
        self._timeout_seconds = timeout_seconds
        self._sse_task: asyncio.Task | None = None
        self._pending_tasks: set[asyncio.Task] = set()
        self._pending_request_ids: dict[asyncio.Task, tuple[str, bool]] = {}
        # Bound concurrent permission handling to avoid unbounded task creation
        # during bursts of permission events from the SSE stream.
        self._permission_sem = asyncio.Semaphore(10)
        # Long-lived HTTP client for reply API calls — reused across all permission
        # replies to avoid per-request connection churn.
        self._reply_client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        """Start the SSE listener background task."""
        self._reply_client = httpx.AsyncClient(timeout=10.0)
        self._sse_task = asyncio.create_task(
            self._listen_sse(), name="opencode-callable-permission-sse"
        )
        logger.info(
            "OpenCode callable permission broker started (url=%s)", self._base_url
        )

    async def stop(self) -> None:
        """Cancel the SSE listener task and drain pending permission tasks."""
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
            # Gather all deny-on-cancel replies concurrently under a total timeout so
            # shutdown cannot hang indefinitely if the opencode endpoint is slow.
            deny_coros = [
                self._reply(req_id, False)
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
                logger.warning(
                    "OpenCode callable broker: SSE connection lost: %s — reconnecting in 3s",
                    e,
                )
                await asyncio.sleep(3)

    async def _handle_sse_line(self, line: str) -> None:
        if not line.startswith("data:"):
            return
        raw = line[5:].strip()
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            logger.warning("Malformed SSE JSON: %r", raw[:200])
            return

        if payload.get("type") != "permission.asked":
            return

        props = payload.get("properties", {})
        req_id = props.get("id", "")
        tool_name = props.get("permission", "")
        patterns = props.get("patterns", [])
        metadata = props.get("metadata", {})
        tool_input: dict = (
            metadata if metadata else ({"commands": patterns} if patterns else {})
        )

        # Handle in a background task so the SSE loop isn't blocked while the
        # handler waits for user input (e.g. tui_permission_handler)
        task = asyncio.create_task(
            self._dispatch(req_id, tool_name, tool_input),
            name=f"opencode-callable-perm-{req_id[:8] if req_id else 'unknown'}",
        )
        self._track_pending_task(task, req_id, reject_on_cancel=True)

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

    async def _dispatch(self, req_id: str, tool_name: str, tool_input: dict) -> None:
        """Call the handler and reply to opencode."""
        async with self._permission_sem:
            await self._dispatch_inner(req_id, tool_name, tool_input)

    async def _dispatch_inner(
        self, req_id: str, tool_name: str, tool_input: dict
    ) -> None:
        """Inner dispatch logic — called under the semaphore."""
        try:
            approved = await asyncio.wait_for(
                self._handler(tool_name, tool_input),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Permission handler timed out for tool %r — denying", tool_name
            )
            approved = False
        except Exception as e:
            logger.error(
                "Permission handler raised for tool %r: %s — denying", tool_name, e
            )
            approved = False

        await self._reply(req_id, approved)

    async def _reply(self, req_id: str, approved: bool) -> None:
        """Call opencode's reply API to unblock the tool."""
        if not req_id:
            return
        reply = "once" if approved else "reject"
        url = f"{self._base_url}/permission/{req_id}/reply"
        try:
            if not self._reply_client:
                logger.error("Reply client not initialized — broker not started?")
                return
            resp = await self._reply_client.post(url, json={"reply": reply})
            resp.raise_for_status()
            logger.info("Replied %r to opencode permission %s", reply, req_id[:16])
        except Exception as e:
            logger.error(
                "Failed to reply to opencode permission %s: %s", req_id[:16], e
            )
