"""Callable-based permission broker for AgentSession scripting use.

Instead of posting to RC and waiting for human approval, this broker
calls a user-provided async callable to approve or deny tool calls.

Designed for use with AgentSession when no Connector/RC room is available::

    async def my_handler(tool_name: str, tool_input: dict) -> bool:
        # approve bash reads, deny everything else
        return tool_name.lower() == "read"

    async with AgentSession(
        ClaudeBackend(...),
        "/my/project",
        permission_handler=my_handler,
    ) as session:
        reply = await session.send("List the files here")
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Awaitable, Callable

from ._http_utils import build_error_response, build_http_response, read_http_body

logger = logging.getLogger("coop.permissions.callable")

_HOST = "127.0.0.1"

# Handler signature: receives tool_name and tool_input, returns True to approve.
PermissionHandler = Callable[[str, dict], Awaitable[bool]]


class CallablePermissionBroker:
    """Permission broker that delegates approval decisions to an async callable.

    Runs a minimal localhost HTTP server to receive Claude's PreToolUse hook
    callbacks.  On each tool call, invokes ``permission_handler(tool_name,
    tool_input)`` and returns allow/deny to Claude based on the result.

    Lifecycle::

        broker = CallablePermissionBroker(my_handler)
        await broker.start()
        # broker.settings_path is now set — pass to ClaudeBackend
        ...
        await broker.stop()

    Typically managed automatically by AgentSession when a
    ``permission_handler`` is provided.
    """

    def __init__(
        self,
        permission_handler: PermissionHandler,
        timeout_seconds: int = 300,
    ) -> None:
        """
        Args:
            permission_handler: Async callable invoked for each tool call.
                Signature: ``async (tool_name: str, tool_input: dict) -> bool``.
                Return ``True`` to allow, ``False`` to deny.
            timeout_seconds: Seconds to wait for the handler before auto-denying.
        """
        self._handler = permission_handler
        self._timeout_seconds = timeout_seconds
        self._server: asyncio.Server | None = None
        self._port: int = 0
        self._settings_path: str = ""

    @property
    def settings_path(self) -> str:
        """Path to the generated Claude settings JSON (for ``--settings`` flag).

        Empty string until ``start()`` has been called.
        """
        return self._settings_path

    async def start(self) -> None:
        """Start the local HTTP hook server and write the settings file."""
        self._server = await asyncio.start_server(
            self._handle_connection, _HOST, 0
        )
        self._port = self._server.sockets[0].getsockname()[1]
        logger.info("Callable permission broker HTTP server on port %d", self._port)
        self._settings_path = self._write_settings_file()

    async def stop(self) -> None:
        """Stop the HTTP server and clean up the settings file."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if self._settings_path:
            # Use missing_ok=True instead of exists()+unlink() to avoid the
            # TOCTOU race where the file could be deleted between the check
            # and the unlink call.
            Path(self._settings_path).unlink(missing_ok=True)
            logger.debug("Removed settings file: %s", self._settings_path)

    # ── HTTP server ───────────────────────────────────────────────────────────

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            body = await read_http_body(reader)
            response_body = await self._handle_hook(body)
            writer.write(build_http_response(response_body))
            await writer.drain()
        except Exception as e:
            logger.error("Error handling hook connection: %s — blocking tool as safe default", e)
            writer.write(build_error_response(
                "Permission handler error — tool blocked as safe default."
            ))
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def _handle_hook(self, raw_body: str) -> str:
        """Parse the PreToolUse payload, call the handler, return JSON decision."""
        try:
            body = json.loads(raw_body) if raw_body else {}
        except json.JSONDecodeError:
            logger.warning("Malformed hook body: %r", raw_body[:200])
            return json.dumps({"decision": "block", "reason": "Malformed hook body."})

        tool_name: str = body.get("tool_name", "")
        tool_input: dict = body.get("tool_input", {})
        logger.info("Hook received: tool=%r", tool_name)

        try:
            approved = await asyncio.wait_for(
                self._handler(tool_name, tool_input),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning("Permission handler timed out for tool %r — denying", tool_name)
            return json.dumps({
                "decision": "block",
                "reason": f"Permission handler timed out for tool '{tool_name}'.",
            })
        except Exception as e:
            logger.error("Permission handler raised: %s — denying tool %r", e, tool_name)
            return json.dumps({
                "decision": "block",
                "reason": f"Permission handler error: {e}",
            })

        if approved:
            logger.debug("Handler approved tool %r", tool_name)
            return json.dumps({"decision": "allow"})

        logger.debug("Handler denied tool %r", tool_name)
        return json.dumps({
            "decision": "block",
            "reason": f"Tool '{tool_name}' denied by permission handler.",
        })

    # ── Settings file ─────────────────────────────────────────────────────────

    def _write_settings_file(self) -> str:
        settings = {
            "hooks": {
                "PreToolUse": [{
                    "matcher": ".*",
                    "hooks": [{
                        "type": "http",
                        "url": f"http://{_HOST}:{self._port}/hook",
                        "timeout": self._timeout_seconds + 10,
                    }],
                }]
            }
        }
        fd, path = tempfile.mkstemp(suffix=".json", prefix="acg-callable-settings-")
        os.chmod(path, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(settings, f)
        logger.info("Settings file written: %s", path)
        return path
