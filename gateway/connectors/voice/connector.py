"""VoiceConnector: HTTP voice gateway for Siri / iOS Shortcuts.

Exposes a minimal HTTP server that accepts plain-text POST requests and returns
plain-text agent replies.  Designed to slot directly into an iOS Shortcut:

    Dictate Text  →  POST /ask/<room>  →  Agent  →  Speak Text

The ``<room>`` path segment maps directly to the watcher's ``room:`` field in
config.yaml, consistent with how all other AgentCoop connectors use the room concept.

    POST /ask/laomei   →  room "laomei"  →  watcher → laomei agent
    POST /ask/xiaomei  →  room "xiaomei" →  watcher → xiaomei agent

Architecture
------------
- Path-derived rooms: URL segment ``/ask/<room>`` is the room name.
- Per-room Lock + Queue — requests to different rooms run concurrently;
  same-room requests are serialized (Siri is sequential within one agent).
- Stale-reply drain on each dispatch — prevents queue desync after timeouts.
- No extra dependencies: uses asyncio.start_server (stdlib only).

Security
--------
- Optional bearer token (``secret`` config key). Empty = no auth (dev only).
- Voice messages are sent as UserRole.OWNER so the agent can use its full
  tool set. Gate this at the network level (VPN / firewall) for production.
- Bind address defaults to 0.0.0.0 so the iPhone can reach the server over LAN.

Siri UX note
------------
Responses MUST be plain text — no markdown, no emoji — or Siri will read
asterisks and emoji names aloud. Inject ``contexts/voice-context.md`` in the
watcher config to enforce this automatically.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from ...agents.response import AgentResponse
from ...core.connector import (
    Connector,
    IncomingMessage,
    MessageHandler,
    Room,
    User,
    UserRole,
)

if TYPE_CHECKING:
    from ...core.watcher_manager import RoomRef
from .config import VoiceConfig

logger = logging.getLogger("coop.connectors.voice")

_VOICE_USER = User(id="siri-user", username="siri", display_name="Siri")

# Hard cap on request body to avoid memory issues.
_MAX_BODY_BYTES = 4096

_TIMEOUT_REPLY = "Sorry, the request timed out. Please try again in a moment."
_BUSY_REPLY = "The gateway is busy right now. Please try again in a moment."


@dataclass
class _RoomState:
    """Per-room synchronization state.

    Each room gets its own lock and reply queue so requests to different rooms
    run concurrently while same-room requests are serialized.

    ``dispatch_active`` gates send_text() so that only the agent reply for the
    current in-flight dispatch is enqueued.  Non-reply send_text() calls
    (permission notifications, queue-full error messages, scheduler notices)
    are silently dropped when no dispatch is waiting — preventing them from
    being returned to the caller as a spurious voice reply.
    """

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    queue: asyncio.Queue[str] = field(default_factory=asyncio.Queue)
    dispatch_active: bool = False


class VoiceConnector(Connector):
    """HTTP voice gateway connector.

    One instance per ``type: voice`` connector entry in config.yaml.

    Lifecycle, and the middle step is not optional::

        await connector.connect()        # binds the port; does NOT accept yet
        # ... watchers start, processors are installed ...
        await connector.start_inbound()  # begins accepting requests
        await connector.disconnect()     # closes the server

    Binding and accepting are separate on purpose. Binding early keeps a port conflict
    a startup failure where it is easy to attribute; accepting late means a request
    never arrives before a watcher can answer it — one that did would reach a
    dispatcher with no processor and be told the gateway is busy, which is a wrong
    answer from a daemon that is merely still starting. An embedding that skips
    ``start_inbound()`` gets a bound port that refuses every connection.

    Endpoint: ``POST /ask/<room>``
        The ``<room>`` segment is the AgentCoop room name — matches the ``room:``
        field in the watcher config exactly.
    """

    delivery_mode: Literal["direct"] = "direct"

    def __init__(self, config: VoiceConfig) -> None:
        self._config = config
        self._handler: MessageHandler | None = None
        self._server: asyncio.Server | None = None
        # Lazily created per room — populated on first request to each room.
        self._rooms: dict[str, _RoomState] = {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Bind the listen address without accepting connections yet.

        `start_inbound()` is what opens the door; see the class docstring for why the
        two halves are separate.
        """
        # Bound but not accepting: `start_serving=False` keeps a port conflict a
        # startup failure at the moment it is easiest to attribute, while leaving the
        # listener closed until `start_inbound()`. Serving from here would answer
        # requests that arrive before the watcher's processor is installed, and the
        # dispatcher has nothing to route them to — the caller hears "the gateway is
        # busy" from a gateway that is merely still starting.
        self._server = await asyncio.start_server(
            self._handle_connection,
            host=self._config.host,
            port=self._config.port,
            start_serving=False,
        )
        addrs = ", ".join(
            str(s.getsockname()) for s in self._server.sockets or []
        )
        auth_status = "token-auth" if self._config.secret else "NO AUTH (dev mode)"
        logger.info(
            "VoiceConnector listening on %s [%s, timeout=%ds]",
            addrs,
            auth_status,
            self._config.timeout,
        )
        if not self._config.secret:
            logger.warning(
                "VoiceConnector has no 'secret' set — any device on the network "
                "can send voice commands to the agent. Set a bearer token in config."
            )

    async def start_inbound(self) -> None:
        """Begin accepting requests, once the watchers that answer them exist.

        The socket is already bound (see `connect()`), so this only starts accepting.
        Callers arriving before it are refused by the OS rather than told the gateway is
        busy — an honest "not up yet" instead of a wrong answer from a healthy daemon.
        """
        if self._server is not None:
            await self._server.start_serving()

    async def disconnect(self) -> None:
        """Stop the HTTP server."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            logger.info("VoiceConnector stopped")

    # ── Inbound ───────────────────────────────────────────────────────────────

    def register_handler(self, handler: MessageHandler) -> None:
        self._handler = handler

    # ── Outbound ──────────────────────────────────────────────────────────────

    async def send_text(
        self,
        room_id: str,
        response: AgentResponse,
        thread_id: str | None = None,  # noqa: ARG002
    ) -> None:
        """Bridge the agent reply back to the waiting HTTP handler for this room.

        Only enqueues when a dispatch is actively waiting for a reply
        (``dispatch_active == True``).  Calls that arrive outside a dispatch
        window — permission notifications, queue-full error messages, scheduler
        notices — are dropped so they cannot be returned to the caller as a
        spurious voice reply.
        """
        state = self._get_room(room_id)
        if not state.dispatch_active:
            # Receiving a send_text with no dispatch waiting is unexpected on a
            # voice connector. Likely causes: permission notifications fired
            # because permissions.enabled=true without skip_owner_approval=true,
            # or a system/scheduler message routed to this room. Either way it
            # would have been returned as a spurious voice reply — log a warning
            # so the operator can fix the config.
            logger.warning(
                "VoiceConnector [%s]: send_text received outside active dispatch "
                "— dropped (%d chars). If this recurs, check that "
                "skip_owner_approval=true (or permissions.enabled=false) is set "
                "for the voice agent.",
                room_id,
                len(response.text),
            )
            return
        await state.queue.put(response.text)
        logger.debug(
            "VoiceConnector [%s]: reply queued (%d chars)", room_id, len(response.text)
        )

    # ── Room resolution ───────────────────────────────────────────────────────

    async def resolve_room(self, room_name: str) -> Room:
        return Room(id=room_name, name=room_name, type="channel")

    def supports_room_lookup(self) -> bool:
        """See `Connector.supports_room_lookup` — the identity lookup below is an answer, not a shrug."""
        return True

    async def room_ref_by_id(self, room_id: str) -> "RoomRef | None":
        """See `Connector.room_ref_by_id`.

        Voice has no room directory to consult: `resolve_room` returns a
        `Room` whose id IS its name, so the inverse is the same identity read the
        other way. Any id names a valid room here — a room is the `/ask/<room>` path a caller chooses, created on first use — so this never
        answers `None`. Kind is `CHANNEL`, which is what the inbound path assigns
        (`kind_for.get('channel')`).

        Without this, a scheduled job could not bring one of these watchers back
        after an `expire`: the base class answers `None` for a connector that
        cannot look a room up by id, and the fire then failed at every slot while
        logging that the room was "gone, in another team, or this account is no
        longer in it" — none of which can be true of a voice room.
        """
        from ...core.watcher_manager import RoomRef
        from ...core.watcher_rule import RoomKind

        return RoomRef(id=room_id, kind=RoomKind.CHANNEL, name=room_id)

    # ── Security: prompt prefix ───────────────────────────────────────────────

    def format_prompt_prefix(self, msg: IncomingMessage) -> str:
        return f"[Voice | from: {msg.sender.username} | role: {msg.role.value}]"

    # ── Per-room state ────────────────────────────────────────────────────────

    def _get_room(self, room_name: str) -> _RoomState:
        """Return (creating if needed) the per-room Lock+Queue state."""
        if room_name not in self._rooms:
            self._rooms[room_name] = _RoomState()
        return self._rooms[room_name]

    # ── Internal HTTP server ──────────────────────────────────────────────────

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Top-level connection handler — enforces an outer timeout."""
        peer = writer.get_extra_info("peername", "<unknown>")
        logger.debug("VoiceConnector: connection from %s", peer)
        try:
            await asyncio.wait_for(
                self._handle_http(reader, writer),
                timeout=self._config.timeout + 10,
            )
        except asyncio.TimeoutError:
            logger.warning("VoiceConnector: connection from %s timed out", peer)
            _write_response(writer, 504, "Gateway Timeout", b"")
            await writer.drain()
        except Exception as exc:
            logger.error(
                "VoiceConnector: error handling connection from %s: %s", peer, exc
            )
        finally:
            # Drain before close so buffered error responses (4xx, 504) are
            # flushed to the client.  Without this, error paths that write a
            # response and return without awaiting drain() produce a connection
            # reset instead of a well-formed HTTP response.
            try:
                await writer.drain()
            except Exception:
                pass
            writer.close()

    async def _handle_http(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Minimal HTTP/1.1 handler for POST /ask/<room>."""
        # ── Request line ──────────────────────────────────────────────────────
        try:
            raw_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
        except asyncio.TimeoutError:
            _write_response(writer, 408, "Request Timeout", b"")
            return

        parts = raw_line.decode("utf-8", errors="replace").strip().split(maxsplit=2)
        if len(parts) < 2:
            _write_response(writer, 400, "Bad Request", b"malformed request line")
            return
        method, path = parts[0].upper(), parts[1]

        # ── Headers ───────────────────────────────────────────────────────────
        headers: dict[str, str] = {}
        while True:
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            except asyncio.TimeoutError:
                _write_response(writer, 408, "Request Timeout", b"")
                return
            if line in (b"\r\n", b"\n", b""):
                break
            decoded = line.decode("utf-8", errors="replace").strip()
            if ":" in decoded:
                name, _, value = decoded.partition(":")
                headers[name.strip().lower()] = value.strip()

        # ── Auth ──────────────────────────────────────────────────────────────
        if self._config.secret:
            auth = headers.get("authorization", "")
            expected = f"Bearer {self._config.secret}"
            # Use constant-time comparison to prevent timing-based token brute-force.
            if not hmac.compare_digest(auth.encode(), expected.encode()):
                logger.warning("VoiceConnector: unauthorized request rejected")
                _write_response(
                    writer, 401, "Unauthorized", b"invalid or missing Bearer token"
                )
                return

        # ── Route: POST /ask/<room> ────────────────────────────────────────────
        if method != "POST":
            _write_response(writer, 404, "Not Found", b"POST /ask/<room> only")
            return

        # Distinguish wrong prefix (404) from missing room name (400).
        # Use "/ask/" (with trailing slash) to avoid false-matching "/askfoo/...".
        bare_path = path.split("?", 1)[0]
        if not (bare_path.startswith("/ask/") or bare_path == "/ask"):
            _write_response(writer, 404, "Not Found", b"POST /ask/<room> only")
            return

        room_name = _parse_room(path)
        if room_name is None:
            _write_response(
                writer,
                400,
                "Bad Request",
                b"room name required: POST /ask/<room>",
            )
            return

        # ── Body ──────────────────────────────────────────────────────────────
        try:
            content_length = int(headers.get("content-length", "0"))
        except ValueError:
            _write_response(writer, 400, "Bad Request", b"invalid Content-Length")
            return
        if content_length < 0:
            _write_response(writer, 400, "Bad Request", b"invalid Content-Length")
            return
        if content_length > _MAX_BODY_BYTES:
            _write_response(writer, 413, "Payload Too Large", b"")
            return

        try:
            raw_body = await asyncio.wait_for(
                reader.readexactly(content_length), timeout=5.0
            )
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            _write_response(writer, 400, "Bad Request", b"incomplete body")
            return

        # ── Body parsing — plain text or JSON {"text": "..."} ─────────────────
        # iOS Shortcuts "Get Contents of URL" only offers JSON/Form/File body
        # types (no plain-text option), so we accept both:
        #   Content-Type: application/json  →  extract the "text" key
        #   anything else                   →  treat raw body as plain text
        raw_str = raw_body.decode("utf-8", errors="replace").strip()
        content_type = headers.get("content-type", "")
        if "application/json" in content_type:
            import json as _json
            try:
                payload = _json.loads(raw_str)
                raw_text = payload.get("text")
                if not isinstance(raw_text, str):
                    # Reject null, numbers, booleans — e.g. {"text": null} from
                    # an iOS Shortcut that failed to populate the variable.
                    _write_response(writer, 400, "Bad Request", b"'text' must be a string")
                    return
                text = raw_text.strip()
            except (_json.JSONDecodeError, AttributeError):
                _write_response(writer, 400, "Bad Request", b"invalid JSON")
                return
        else:
            text = raw_str

        if not text:
            _write_response(writer, 400, "Bad Request", b"empty message")
            return

        logger.info("VoiceConnector [%s]: query (%d chars)", room_name, len(text))

        # ── Dispatch ──────────────────────────────────────────────────────────
        reply = await self._dispatch(text, room_name)

        # ── Response ──────────────────────────────────────────────────────────
        encoded = reply.encode("utf-8")
        _write_response(
            writer, 200, "OK", encoded, content_type="text/plain; charset=utf-8"
        )
        await writer.drain()
        logger.info("VoiceConnector [%s]: reply sent (%d chars)", room_name, len(reply))

    async def _dispatch(self, text: str, room_name: str) -> str:
        """Inject a voice message into room_name, wait for reply, return it.

        Serialized per room — concurrent requests to different rooms proceed
        independently; same-room requests queue behind the lock.

        The reply queue is drained before each dispatch (inside the lock) to
        discard any stale reply left by a prior timed-out request.

        Known limitation — cross-request reply mixup after timeout:
            If Request A times out and its agent turn is still running in the
            background, Request B can acquire the lock, drain an empty queue
            (A's reply hasn't arrived yet), set dispatch_active=True, and then
            receive A's late reply as if it were B's.  B's own reply arrives
            after dispatch_active resets to False and is dropped with a warning.

            Root cause: a per-room boolean cannot associate a reply with the
            specific dispatch that requested it.  The robust fix requires a
            per-dispatch correlation token — each dispatch creates its own
            Queue and discards it after use; send_text() routes by token.
            Deferred: Siri is sequential and the window (timeout < agent turn
            duration) is narrow in practice.  If this becomes a problem,
            track it as a follow-up and implement per-dispatch queues.
"""
        state = self._get_room(room_name)
        async with state.lock:
            # Drain stale replies from any prior timed-out request.
            while not state.queue.empty():
                stale = state.queue.get_nowait()
                logger.debug(
                    "VoiceConnector [%s]: discarding stale reply (%d chars)",
                    room_name,
                    len(stale),
                )

            if not self._handler:
                logger.warning(
                    "VoiceConnector [%s]: handler not registered yet", room_name
                )
                return _BUSY_REPLY

            msg = IncomingMessage(
                id=f"voice-{uuid.uuid4().hex[:8]}",
                timestamp="",
                room=Room(id=room_name, name=room_name, type="channel"),
                sender=_VOICE_USER,
                role=UserRole.OWNER,
                text=text,
            )

            accepted = await self._handler(msg)
            if not accepted:
                logger.warning(
                    "VoiceConnector [%s]: message dropped (queue full)", room_name
                )
                return _BUSY_REPLY

            # Gate send_text() so only the real agent reply is enqueued —
            # permission notifications and other system send_text() calls
            # arrive while this flag is True and would otherwise be dequeued
            # as the voice reply.
            state.dispatch_active = True
            try:
                return await asyncio.wait_for(
                    state.queue.get(), timeout=float(self._config.timeout)
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "VoiceConnector [%s]: agent reply timed out after %ds",
                    room_name,
                    self._config.timeout,
                )
                return _TIMEOUT_REPLY
            finally:
                state.dispatch_active = False


# ── URL helpers ───────────────────────────────────────────────────────────────


def _parse_room(path: str) -> str | None:
    """Extract the room name from a ``/ask/<room>`` path.

    Returns the room name string, or None if the path is invalid / missing a room.

    Valid:
        /ask/laomei       → "laomei"
        /ask/voice-room   → "voice-room"
    Invalid (returns None):
        /ask              → None  (no room)
        /ask/             → None  (empty room)
        /other/laomei     → None  (wrong prefix)
    """
    # Strip query string
    path = path.split("?", 1)[0]
    # Must start with /ask/
    if not path.startswith("/ask/"):
        return None
    room = path[len("/ask/"):].strip("/")
    return room if room else None


# ── HTTP helpers ──────────────────────────────────────────────────────────────


def _write_response(
    writer: asyncio.StreamWriter,
    status: int,
    reason: str,
    body: bytes,
    content_type: str = "text/plain; charset=utf-8",
) -> None:
    """Write a minimal HTTP/1.1 response."""
    writer.write(f"HTTP/1.1 {status} {reason}\r\n".encode())
    writer.write(f"Content-Type: {content_type}\r\n".encode())
    writer.write(f"Content-Length: {len(body)}\r\n".encode())
    writer.write(b"Connection: close\r\n")
    writer.write(b"\r\n")
    if body:
        writer.write(body)
