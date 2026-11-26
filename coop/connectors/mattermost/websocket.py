"""One bot account's websocket to Mattermost: the ``authentication_challenge``
handshake, the listen loop with reconnect backoff, and the typing action. Lifted
from the frozen gateway's client without its per-channel queues: ordering is the
service's job (one queue per room), and this layer only hands every event over.

Mattermost facts the frozen client established and this one keeps: a ``posted``
event's ``data.post`` and ``data.mentions`` are JSON-encoded strings; the server
does not ack the handshake, success shows as events arriving; the library's
transport ping keeps the connection alive and raises ``ConnectionClosed`` on a
dead peer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from collections.abc import Awaitable, Callable
from typing import Any

import websockets

logger = logging.getLogger(__name__)

OnFrame = Callable[[dict], Awaitable[None]]
OnReconnect = Callable[[], Awaitable[None]]


class MattermostWebSocket:
    def __init__(self, url: str, token: Callable[[], str], on_frame: OnFrame, on_reconnect: OnReconnect | None = None,
                 max_backoff: float = 60.0, tls: ssl.SSLContext | None = None) -> None:
        """``token`` is read at every (re)connect, so a session token the REST client
        replaced after a login is the one the next connection authenticates with.
        ``tls`` is the context a ``wss`` connection uses (``None``: the library's default,
        which verifies); it is passed only then, as the library refuses one for ``ws``."""
        scheme = "wss" if url.startswith("https") else "ws"
        self.ws_url = scheme + "://" + url.split("://", 1)[-1].rstrip("/") + "/api/v4/websocket"
        self._token = token
        self._tls = tls if scheme == "wss" else None
        self._on_frame = on_frame
        self._on_reconnect = on_reconnect
        self._max_backoff = max_backoff
        self._ws: Any = None
        self._seq = 1
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Connect and authenticate now (so a bad token fails the bot's ``add_bot``),
        then listen on a task of its own."""
        await self._connect()
        self._running = True
        self._task = asyncio.create_task(self._listen(), name=f"mattermost ws {self.ws_url}")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._close()

    async def typing(self, channel_id: str, parent_id: str | None = None) -> None:
        data: dict[str, Any] = {"channel_id": channel_id}
        if parent_id:
            data["parent_id"] = parent_id
        await self._send({"action": "user_typing", "data": data})

    # ── the connection ──────────────────────────────────────────────────────

    async def _connect(self) -> None:
        self._ws = await websockets.connect(self.ws_url, **({} if self._tls is None else {"ssl": self._tls}))
        try:
            await self._send({"action": "authentication_challenge", "data": {"token": self._token()}})
        except Exception:
            await self._close()
            raise

    async def _close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001 - closing a dead socket
                pass
            self._ws = None

    async def _send(self, message: dict) -> None:
        if self._ws is None:
            raise ConnectionError("websocket is not connected")
        message = {"seq": self._seq, **message}
        self._seq += 1
        await self._ws.send(json.dumps(message))

    async def _listen(self) -> None:
        while self._running:
            try:
                async for raw in self._ws:
                    try:
                        frame = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.warning("non-JSON websocket frame ignored")
                        continue
                    if not isinstance(frame, dict) or "event" not in frame:
                        continue  # seq_reply acks and the like
                    try:
                        await self._on_frame(frame)
                    except Exception:  # noqa: BLE001 - one bad frame must not end the connection
                        logger.exception("websocket frame not handled: %r", frame.get("event"))
            except asyncio.CancelledError:
                raise
            except websockets.exceptions.ConnectionClosed as exc:
                logger.warning("websocket closed: %s", exc)
            except Exception:  # noqa: BLE001
                logger.exception("websocket listen failed")
            if self._running:
                await self._reconnect()

    async def _reconnect(self) -> None:
        delay = 1.0
        while self._running:
            logger.info("reconnecting to %s in %.0fs", self.ws_url, delay)
            await asyncio.sleep(delay)
            try:
                await self._connect()
            except Exception as exc:  # noqa: BLE001
                logger.warning("reconnect failed: %s", exc)
                delay = min(delay * 2, self._max_backoff)
                continue
            logger.info("reconnected to %s", self.ws_url)
            if self._on_reconnect is not None:
                # not awaited on this task: a long resync must not stall the frames
                asyncio.create_task(self._on_reconnect(), name="mattermost ws resync")
            return
