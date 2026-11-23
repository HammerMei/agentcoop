"""The operator's channel into the running daemon: a Unix socket, one JSON request
line in, one JSON response line out, owner-only (design §3.1 operator commands).
The agent commands never come this way; they use the doorbell.

Request: ``{"cmd": "status" | "list" | "pause" | "resume" | "reset" | "reload" | "stop",
...}``. Response: ``{"ok": true, "data": ...}`` or ``{"ok": false, "error": "..."}``.
The socket-file handling is the frozen gateway's: a live socket refuses the start,
a stale one is replaced.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from .config import ConfigError, load
from .service import Service

logger = logging.getLogger(__name__)

RESPONSE_LIMIT = 16 * 1024 * 1024


class ControlServer:
    def __init__(self, service: Service, socket_path: Path, config_path: Path,
                 on_stop: Callable[[], Awaitable[None] | None], locked_pid: Callable[[], int | None] = lambda: None) -> None:
        self.service = service
        self.socket_path = socket_path
        self.config_path = config_path
        self._on_stop = on_stop
        self._locked_pid = locked_pid
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            await self._replace_stale()
        self._server = await asyncio.start_unix_server(self._handle_client, path=str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        logger.info("control socket listening at %s", self.socket_path)

    async def _replace_stale(self) -> None:
        """A socket file from a daemon that is gone is replaced; one that answers, or
        that times out while a pid holds the lock, refuses this start."""
        try:
            _, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(self.socket_path)), timeout=2.0)
        except asyncio.TimeoutError:
            pid = self._locked_pid()
            if pid is not None:
                raise RuntimeError(f"another coop daemon may be running (pid={pid}, socket {self.socket_path} "
                                   "timed out); stop it first") from None
            self.socket_path.unlink()
        except (ConnectionRefusedError, OSError):
            self.socket_path.unlink()
        else:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            raise RuntimeError(f"another coop daemon is running (socket {self.socket_path} is live); stop it first")

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self.socket_path.unlink(missing_ok=True)

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            data = await asyncio.wait_for(reader.readline(), timeout=30.0)
            if not data:
                return
            try:
                response = await self.dispatch(json.loads(data.decode()))
            except Exception as exc:  # noqa: BLE001 - the operator gets the error, the daemon goes on
                logger.exception("control: command failed")
                response = {"ok": False, "error": str(exc)}
            writer.write(json.dumps(response).encode() + b"\n")
            await asyncio.wait_for(writer.drain(), timeout=10.0)
        except (asyncio.TimeoutError, OSError, ValueError) as exc:
            logger.warning("control: client dropped: %s", exc)
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=5.0)
            except (asyncio.TimeoutError, OSError):
                pass

    async def dispatch(self, request: dict) -> dict:
        cmd = request.get("cmd")
        service = self.service
        if cmd == "status":
            return _ok(service.status())
        if cmd == "list":
            # ``hosted`` names agents, as ``pause``/``resume``/``reset`` do; ``bots`` are the
            # room's bot usernames, the names in the room's text (design §2)
            return _ok({handle: {"hosted": service.hosted_agents(run), "bots": list(run.core.agents),
                                 "room_id": run.info.id, "dm": run.info.is_dm}
                        for handle, run in sorted(service.rooms.items())})
        if cmd in ("pause", "resume", "reset"):
            agent, room = str(request.get("agent", "")), str(request.get("room", ""))
            try:
                getattr(service, cmd)(agent, room)
            except KeyError as exc:
                return _error(str(exc.args[0]))
            return _ok({"agent": agent, "room": room})
        if cmd == "reload":
            try:
                notes = await service.reload(load(self.config_path))
            except (ConfigError, OSError) as exc:
                return _error(f"configuration not applied: {exc}")
            return _ok({"notes": notes})
        if cmd == "stop":
            result = self._on_stop()
            if result is not None:
                await result
            return _ok({"stopping": True})
        return _error(f"unknown command {cmd!r}")


def _ok(data) -> dict:
    return {"ok": True, "data": data}


def _error(message: str) -> dict:
    return {"ok": False, "error": message}


async def send_command(socket_path: Path, request: dict, timeout: float = 60.0) -> dict:
    """The client side: one request, one response."""
    reader, writer = await asyncio.open_unix_connection(str(socket_path), limit=RESPONSE_LIMIT)
    try:
        writer.write(json.dumps(request).encode() + b"\n")
        await writer.drain()
        data = await asyncio.wait_for(reader.readline(), timeout=timeout)
        if not data:
            raise ConnectionError("the daemon closed the connection without answering")
        return json.loads(data.decode())
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
