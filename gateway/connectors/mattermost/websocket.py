"""Mattermost Realtime API (WebSocket) client.

Structurally simpler than Rocket.Chat's DDP client (gateway/connectors/
rocketchat/websocket.py) because Mattermost has no per-channel subscribe
handshake: one authenticated connection streams `posted` events for every
channel the bot is a member of, across every team it belongs to. There is no
sub/unsub/ready/nosub confirmation dance and therefore no per-room refcounted
subscription state to manage — `register_channel`/`unregister_channel` on
this client are local bookkeeping only (which channels does the dispatcher
care about), not wire-protocol calls.

Payload quirks confirmed empirically against a live Mattermost 11.7.0 server
(not just from docs) before writing this:
  - `data.post` on a `posted` event is a JSON-encoded STRING, not a nested
    object — requires its own `json.loads()`.
  - `data.mentions` is ALSO a JSON-encoded STRING (of a user-id array), not a
    native JSON array — requires its own `json.loads()` too. This one is not
    documented anywhere obvious; found by driving one real mention through a
    live server.
  - The server excludes the author from their own `mentions` list — a
    self-mention produces no `mentions` field at all. Only cross-user
    mentions populate it.
  - No custom application-level ping/pong is needed (unlike RC's DDP, which
    requires periodic `{"msg": "ping"}`): the `websockets` library's
    transport-level ping/pong keeps the connection alive and raises
    ConnectionClosed on a dead peer, which the reconnect loop below handles.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import websockets

logger = logging.getLogger("coop.connectors.mattermost.ws")

# Bound concurrent per-channel worker tasks, same rationale as RC's
# _callback_sem: caps total in-flight handler invocations across all channels.
_CALLBACK_CONCURRENCY = 20
# Per-channel bounded queue depth — same backpressure model as RC, just
# without the wire-protocol subscription it's normally paired with.
_CHANNEL_QUEUE_DEPTH = 50

PostedEventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class MattermostWebSocketClient:
    """WebSocket client for the Mattermost Realtime API."""

    def __init__(self, server_url: str, token_provider: Callable[[], str | None]):
        ws_url = server_url.replace("https://", "wss://").replace("http://", "ws://")
        self.ws_url = f"{ws_url}/api/v4/websocket"
        # Called fresh on every (re)connect so a token refreshed by REST-mode
        # re-login (session expiry) is picked up without reconstructing the
        # client.
        self._token_provider = token_provider

        self._ws: websockets.ClientConnection | None = None
        self._handler: PostedEventHandler | None = None
        self._membership_handler: PostedEventHandler | None = None
        self._seq = 1
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 60.0
        self._running = False
        self._listen_task: asyncio.Task | None = None
        self._callback_sem = asyncio.Semaphore(_CALLBACK_CONCURRENCY)
        self._callback_tasks: set[asyncio.Task] = set()
        # The single in-flight reconnect replay — see _reconnect's coalescing.
        self._replay_task: asyncio.Task | None = None
        self._channel_queues: dict[str, asyncio.Queue] = {}
        self._channel_workers: dict[str, asyncio.Task] = {}
        # Local bookkeeping only — no wire-protocol subscribe exists.  Tracks
        # which channels the dispatcher currently cares about, purely so
        # register/unregister has somewhere to record intent (parity with
        # RC's subscribe_room/unsubscribe_room surface for the connector).
        self._registered_channels: set[str] = set()
        # Optional callback invoked after every successful reconnect.
        # Registered by the connector to replay messages missed during the
        # outage via REST history.
        self._on_reconnect_cb: Callable[[], Any] | None = None

    def register_handler(self, handler: PostedEventHandler) -> None:
        """Register the single callback invoked for every decoded `posted` event."""
        self._handler = handler

    def register_membership_handler(self, handler: PostedEventHandler) -> None:
        """Register the callback for raw `user_added`/`user_removed` events (§2.7).

        The transport hands the *whole* event over, undecoded: the two events
        carry their ids in asymmetric places (a removal broadcast to the
        removed user has the channel in `data` and the user in `broadcast`;
        every other variant is the mirror image), and which user matters —
        the bot's own id — is the connector's knowledge, not this layer's.
        The handler must return fast; anything slow belongs on its own task.
        """
        self._membership_handler = handler

    def register_channel(self, channel_id: str) -> None:
        """Record that the dispatcher cares about this channel (no wire call)."""
        self._registered_channels.add(channel_id)

    def unregister_channel(self, channel_id: str) -> None:
        """Release the channel's local delivery machinery (no wire call).

        The RC twin pops the queue and cancels the worker; this method used
        to discard only the bookkeeping set — which is write-only in
        production code — so every unsubscribe/reap over a long-lived process
        leaked an asyncio.Task parked on `queue.get()` plus its queue,
        forever (#115). Cancel-and-forget rather than awaited, because
        `reap_room` is synchronous: a cancelled worker finishes on the loop,
        and `_dispatch` lazily recreates the pair if the channel returns.
        """
        self._registered_channels.discard(channel_id)
        self._channel_queues.pop(channel_id, None)
        worker = self._channel_workers.pop(channel_id, None)
        if worker is not None and not worker.done():
            worker.cancel()

    def set_reconnect_callback(self, cb: Callable[[], Any]) -> None:
        self._on_reconnect_cb = cb

    async def connect(self) -> None:
        """Connect and perform the authentication_challenge handshake.

        If the handshake fails after the socket is open, the socket is
        closed before re-raising so no connection is leaked.
        """
        logger.info("Connecting to %s", self.ws_url)
        self._ws = await websockets.connect(self.ws_url)
        self._reconnect_delay = 1.0  # Reset on successful connect
        try:
            await self._authenticate()
        except Exception:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
            raise

    async def _authenticate(self) -> None:
        token = self._token_provider()
        if not token:
            raise RuntimeError("MattermostWebSocketClient: no token available to authenticate")
        await self._send({
            "seq": self._seq,
            "action": "authentication_challenge",
            "data": {"token": token},
        })
        self._seq += 1
        # Mattermost does not reliably ack authentication_challenge with a
        # dedicated event before streaming `hello`/`posted` etc. — success is
        # implied by subsequent events arriving rather than an explicit
        # confirmation, so there is nothing further to await here (confirmed
        # against a live server: the first frames received were `hello` and a
        # generic seq_reply ack, not a distinguishable "auth OK" event).
        logger.info("Sent authentication_challenge")

    async def start(self) -> None:
        """Start the listen loop."""
        self._running = True
        self._listen_task = asyncio.create_task(self._listen_loop())

    async def stop(self) -> None:
        """Stop listening and close the connection."""
        self._running = False
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
        worker_list = list(self._channel_workers.values())
        for worker in worker_list:
            worker.cancel()
        for task in list(self._callback_tasks):
            task.cancel()
        all_tasks = set(worker_list) | self._callback_tasks
        if all_tasks:
            await asyncio.gather(*all_tasks, return_exceptions=True)
        self._callback_tasks.clear()
        self._channel_queues.clear()
        self._channel_workers.clear()
        if self._ws:
            await self._ws.close()
            self._ws = None
        logger.info("WebSocket client stopped")

    async def _send(self, msg: dict) -> None:
        if self._ws is None:
            raise RuntimeError("WebSocket is not connected")
        await self._ws.send(json.dumps(msg))

    async def send_typing(self, channel_id: str, parent_id: str = "") -> None:
        """Send a best-effort typing indicator action."""
        data: dict[str, Any] = {"channel_id": channel_id}
        if parent_id:
            data["parent_id"] = parent_id
        await self._send({"seq": self._seq, "action": "user_typing", "data": data})
        self._seq += 1

    def _decode_posted_event(self, evt: dict[str, Any]) -> dict[str, Any]:
        """Decode a raw `posted` event into {post, mentions, channel_type, ...}.

        Both `data.post` and `data.mentions` are JSON-encoded strings, not
        native JSON objects/arrays (confirmed against a live server — see
        module docstring). Absence of `mentions` (self-mentions, or no
        mentions at all) decodes to an empty list.
        """
        data = evt.get("data", {})
        post = json.loads(data["post"])
        mentions_raw = data.get("mentions")
        mentions: list[str] = json.loads(mentions_raw) if mentions_raw else []
        return {
            "post": post,
            "mentions": mentions,
            "channel_type": data.get("channel_type"),
            "channel_name": data.get("channel_name"),
            # A DM's `channel_name` is the opaque `<userid>__<userid>` form; its
            # `channel_display_name` is the counterpart's handle, and for a group DM it is
            # the member list (§6.2). Kept because it is the only usable identity a DM
            # event carries — deliberately not used as a *label*, which must not move when
            # membership does.
            "channel_display_name": data.get("channel_display_name"),
            "team_id": data.get("team_id"),
        }

    async def _dispatch(self, decoded: dict[str, Any]) -> None:
        """Route a decoded posted-event to the per-channel ordering queue."""
        self.deliver_to_channel(decoded)

    def deliver_to_channel(self, decoded: dict[str, Any]) -> None:
        """Enqueue a decoded event onto its channel's ordering queue.

        The public form of `_dispatch`, for the creation path (§2.7): the message
        that triggered a creation is handed back through the channel's own queue
        once the channel is tracked, so every gate that applies to a tracked
        channel's message — the mention gate, dedup, the capacity preflight —
        applies to it, and so does the queue's ordering. The Rocket.Chat
        transport's `deliver_to_room` is the same contract; one copy of the
        enqueue rule serves both callers here so the two cannot drift.

        Synchronous on purpose: the enqueue never blocks (`put_nowait`), and a
        full queue drops the event with a warning — the same audible overflow
        answer live delivery gives, because a silent drop here would lose the
        one message the new watcher exists to answer with nothing in the log.
        """
        channel_id = decoded["post"]["channel_id"]
        queue = self._channel_queues.get(channel_id)
        if queue is None:
            queue = asyncio.Queue(maxsize=_CHANNEL_QUEUE_DEPTH)
            self._channel_queues[channel_id] = queue
            worker = asyncio.create_task(self._channel_worker(channel_id, queue))
            self._channel_workers[channel_id] = worker
            self._callback_tasks.add(worker)
            worker.add_done_callback(self._callback_tasks.discard)
        try:
            queue.put_nowait(decoded)
        except asyncio.QueueFull:
            logger.warning(
                "Channel %s inbound queue full (depth=%d) — dropping event",
                channel_id, _CHANNEL_QUEUE_DEPTH,
            )

    async def _channel_worker(self, channel_id: str, queue: asyncio.Queue) -> None:
        """Process one channel's events in order, bounded by the global semaphore."""
        try:
            while True:
                decoded = await queue.get()
                async with self._callback_sem:
                    if self._handler is not None:
                        try:
                            await self._handler(decoded)
                        except Exception:
                            logger.exception(
                                "Unhandled error in posted-event handler for channel %s",
                                channel_id,
                            )
        except asyncio.CancelledError:
            pass

    async def _listen_loop(self) -> None:
        while self._running:
            try:
                if self._ws is None:
                    raise RuntimeError("WebSocket is not connected")
                async for raw in self._ws:
                    try:
                        evt = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.warning("Received non-JSON WS frame, ignoring")
                        continue
                    if evt.get("event") == "posted":
                        try:
                            decoded = self._decode_posted_event(evt)
                        except (KeyError, json.JSONDecodeError):
                            logger.exception("Failed to decode posted event: %r", evt)
                            continue
                        await self._dispatch(decoded)
                    elif evt.get("event") in ("user_added", "user_removed"):
                        # Membership events (§2.7). Deliberately not routed
                        # through the per-channel ordering queues: the handler
                        # filters to the bot's own membership and spawns its
                        # slow half, and ordering against posts is provided by
                        # the core's locks, not the transport. A handler error
                        # must not kill delivery for every channel.
                        if self._membership_handler is not None:
                            try:
                                await self._membership_handler(evt)
                            except Exception:
                                logger.exception(
                                    "Unhandled error in membership handler: %r", evt)
                    # Other events (hello, status_change, typing, seq_reply
                    # acks, etc.) are intentionally not acted on here.
            except asyncio.CancelledError:
                raise
            except websockets.exceptions.ConnectionClosed as e:
                logger.warning("WebSocket connection closed: %s", e)
            except Exception:
                logger.exception("Unexpected error in WS listen loop")

            if not self._running:
                return

            await self._reconnect()

    async def _reconnect(self) -> None:
        """Reconnect with exponential backoff, then fire the reconnect callback."""
        while self._running:
            logger.info("Reconnecting in %.1fs...", self._reconnect_delay)
            await asyncio.sleep(self._reconnect_delay)
            try:
                self._ws = await websockets.connect(self.ws_url)
                await self._authenticate()
                self._reconnect_delay = 1.0
                logger.info("Reconnected to %s", self.ws_url)
                if self._on_reconnect_cb is not None:
                    # OFF the receive loop (Codex round 22): this coroutine
                    # runs ON the listen task, and awaiting the reconnect
                    # replay here stopped event consumption for its whole
                    # duration — including `user_removed`, the ONLY feeder of
                    # the membership generation the replay's own era fence
                    # reads. A removal landing mid-replay was therefore
                    # unreadable until the replay finished, and the stale
                    # batch delivered despite it. As a task, the loop keeps
                    # consuming and the fence's signal stays live; the replay
                    # was never serialized against live traffic anyway (it
                    # calls _on_posted_event directly, not via the workers).
                    def _log_replay_failure(task: asyncio.Task) -> None:
                        if not task.cancelled() and task.exception():
                            logger.error("Error in on_reconnect callback: %r",
                                         task.exception())

                    # COALESCED (Codex round 25): a flaky connection can
                    # complete another reconnect before the previous replay
                    # finishes, and two replays over one room interleave
                    # their dispatches and double the REST work. The prior
                    # replay is cancelled first — its window was claimed
                    # before its first await, so the cancellation loses
                    # nothing: this newer replay reads the same (or older)
                    # boundary and re-covers the tail.
                    prev = self._replay_task
                    if prev is not None and not prev.done():
                        prev.cancel()
                        try:
                            await prev
                        except (asyncio.CancelledError, Exception):
                            pass
                    try:
                        result = self._on_reconnect_cb()
                        if asyncio.iscoroutine(result):
                            task = asyncio.create_task(result)
                            task.add_done_callback(_log_replay_failure)
                            # Tracked like the channel workers (Codex round
                            # 23): stop() must be able to harvest a replay
                            # still fetching when shutdown or another drop
                            # arrives — an untracked task could outlive
                            # disconnect() and use a closed REST client.
                            self._replay_task = task
                            self._callback_tasks.add(task)
                            task.add_done_callback(self._callback_tasks.discard)
                    except Exception:
                        logger.exception("Error in on_reconnect callback")
                return
            except Exception:
                logger.exception("Reconnect attempt failed")
                self._reconnect_delay = min(
                    self._reconnect_delay * 2, self._max_reconnect_delay
                )
