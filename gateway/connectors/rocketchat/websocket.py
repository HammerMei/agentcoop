"""Rocket.Chat Realtime API (DDP over WebSocket) client."""

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import websockets

logger = logging.getLogger("coop.connectors.rocketchat.ws")


@dataclass
class SubscriptionState:
    """Runtime state for one room subscription."""

    room_id: str
    callback: Callable
    sub_id: str | None = None
    status: str = "pending"  # pending | active | reconnecting | degraded | failed
    last_error: str | None = None
    dropped_messages: int = 0


class RCWebSocketClient:
    """WebSocket client for Rocket.Chat Realtime API (DDP protocol)."""

    def __init__(self, server_url: str, username: str, password: str):
        # Convert http(s) to ws(s)
        ws_url = server_url.replace("https://", "wss://").replace("http://", "ws://")
        self.ws_url = f"{ws_url}/websocket"
        self.username = username
        self.password = password

        self._ws: websockets.WebSocketClientProtocol | None = None
        self._subscriptions: dict[str, str] = {}  # room_id -> sub_id
        self._callbacks: dict[str, Callable] = {}  # room_id -> async callback
        self._subscription_states: dict[str, SubscriptionState] = {}
        self._pending_results: dict[str, asyncio.Future] = {}  # method_id -> future
        self._pending_subs: dict[
            str, asyncio.Future
        ] = {}  # sub_id -> future (subscription confirmation)
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 60.0
        self._running = False
        self._listen_task: asyncio.Task | None = None
        self._ping_task: asyncio.Task | None = None
        self._callback_tasks: set[asyncio.Task] = set()
        self._callback_sem = asyncio.Semaphore(20)  # bound concurrent callback tasks
        self._room_queues: dict[
            str, asyncio.Queue
        ] = {}  # per-room bounded inbound queues
        self._room_workers: dict[str, asyncio.Task] = {}  # per-room worker tasks
        # One recovery at a time, in one slot. Two entry points — a socket drop and a
        # stream terminated under a healthy socket — used to run two nearly identical
        # sequences over shared state, and every review round found another write in them
        # that had no owner. There is one sequence now, and the slot is what owns it.
        self._recovery_task: asyncio.Task | None = None
        # Bumped whenever a recovery starts and whenever the client stops. An attempt that
        # was overtaken records nothing: it captures this at the start and compares before
        # publishing, which is the ownership rule for work that outlives its starter and
        # cannot be identified by an id alone.
        self._recovery_generation = 0
        # Optional callback invoked after every successful reconnect + resubscribe.
        # Registered by the connector to replay messages missed during the outage.
        self._on_reconnect_cb: Callable[[], Any] | None = None
        # Fired before a recovery subscribes anything — see `register_outage_callback`.
        self._on_outage_cb: Callable[[], Any] | None = None
        # Set of room IDs currently being unsubscribed.  Checked inside
        # _subscribe_with_confirmation to detect a race where
        # a recovery re-registers a room that unsubscribe_room
        # is concurrently removing.
        self._rooms_unsubscribing: set[str] = set()
        # Consulted for a room with no registered callback. Under per-room subscriptions
        # such a message cannot arrive, so this stays None and nothing changes; under
        # subscribe-all every room the account can see arrives, and this is what decides
        # whether one of them should become a watcher.
        self._default_callback: Callable | None = None
        # The stream this client subscribed to, when it is not a room. Kept separate from
        # `_subscriptions`, which maps rooms to their own subscription ids — the whole
        # point of the key-space split is that a stream is not a room.
        self._stream_sub_id: str | None = None
        # Whether this client should have the stream at all, as distinct from whether it
        # currently does. A failed restore clears the id; only `disconnect` clears intent.
        self._wants_stream = False
        # The stream subscription that has been sent but not yet recorded. Its own field,
        # because between `ready` and the caller resuming there is a window in which the
        # subscription has an id, a confirmation, and no entry in `_stream_sub_id` — and a
        # `nosub` arriving in it belongs to the stream even though nothing yet says so.
        self._pending_stream_sub_id: str | None = None
        # Set when that `nosub` arrives, and read by the caller before it records success:
        # a confirmation is only as good as the transport's last word about it.
        self._revoked_stream_sub_id: str | None = None
        # One queue and one worker for every untracked room, rather than one of each per
        # room. Bounded, because an unbounded routing backlog under subscribe-all is every
        # message in every readable channel.
        self._routing_queue: asyncio.Queue = asyncio.Queue(maxsize=self._ROOM_QUEUE_DEPTH)
        self._routing_workers: list[asyncio.Task] = []
        # The id the DDP login response assigned this account — captured because the
        # membership stream's event name is built from it (`<uid>/subscriptions-changed`),
        # and nothing else on this layer knows it.
        self._user_id: str | None = None
        # The membership-events subscription (§2.7): id, intent, and callback. The same
        # key-space split as the stream above — a notify-user subscription is not a room —
        # but deliberately without the pending/revoked hand-off machinery: recording a
        # dead id here costs missed membership events, which the first-message safety net
        # and the periodic reconciliation both cover, not silent room-wide delivery loss.
        self._membership_sub_id: str | None = None
        self._wants_membership = False
        self._membership_callback: Callable | None = None

    async def connect(self) -> None:
        """Connect, perform DDP handshake, and login.

        If the DDP handshake or login fails after the WebSocket is open,
        the socket is closed before re-raising so no connection is leaked.
        """
        logger.info("Connecting to %s", self.ws_url)
        self._ws = await websockets.connect(self.ws_url)
        self._reconnect_delay = 1.0  # Reset on successful connect

        try:
            # DDP connect
            await self._send({"msg": "connect", "version": "1", "support": ["1"]})
            resp = await self._recv()
            if resp.get("msg") != "connected":
                raise RuntimeError(f"DDP handshake failed: {resp}")
            logger.info("DDP connected (session=%s)", resp.get("session"))

            # Login
            login_id = self._new_id()
            await self._send(
                {
                    "msg": "method",
                    "method": "login",
                    "id": login_id,
                    "params": [
                        {
                            "user": {"username": self.username},
                            "password": {
                                "digest": hashlib.sha256(
                                    self.password.encode()
                                ).hexdigest(),
                                "algorithm": "sha-256",
                            },
                        }
                    ],
                }
            )
            login_resp = await self._recv_until_result(login_id)
            if login_resp.get("msg") == "result" and not login_resp.get("error"):
                # The id this login actually resolved to — the membership
                # stream's event name is `<uid>/subscriptions-changed`, and the
                # configured username is a spelling, not an id (#112).
                self._user_id = (login_resp.get("result") or {}).get("id")
                logger.info("WebSocket login successful for %s", self.username)
            else:
                raise RuntimeError(f"WebSocket login failed: {login_resp}")
        except Exception:
            # Close the open socket so it is not leaked on handshake / login failure.
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
            raise

    async def start(self) -> None:
        """Start the listen and ping loops."""
        self._running = True
        self._listen_task = asyncio.create_task(self._listen_loop())
        self._ping_task = asyncio.create_task(self._ping_loop())

    async def stop(self) -> None:
        """Stop listening and close the connection."""
        self._running = False
        if self._ping_task:
            self._ping_task.cancel()
            try:
                await self._ping_task
            except asyncio.CancelledError:
                pass
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
        if self._recovery_task:
            self._recovery_task.cancel()
            try:
                await self._recovery_task
            except asyncio.CancelledError:
                pass
            self._recovery_task = None
        # Cancelling the recovery is not enough on its own: a `subscribe_all()` started
        # directly by `start_inbound` is not held in that slot, and one whose confirmation
        # has already arrived will resume *after* the fields below are cleared and publish
        # its id back into them. The generation is what a coroutine outliving its starter
        # is checked against; bumping it here retires every attempt in flight.
        self._recovery_generation += 1
        # `→ absent`: the only transition that clears intent. The transition table says so
        # and the transport never implemented its half — a client stopped and connected
        # again went on reporting the closed socket's stream as live, so watcher
        # restoration registered callbacks without subscribing, and a `subscribe_all` that
        # then failed left every restored room with no delivery at all.
        #
        # Not a violation of "intent survives failure": a stop is not a failure.
        self._stream_sub_id = None
        self._wants_stream = False
        self._pending_stream_sub_id = None
        self._revoked_stream_sub_id = None
        # The membership stream follows the same rule: a stop clears intent.
        self._membership_sub_id = None
        self._wants_membership = False
        # Cancel room workers explicitly and collect them for the drain gather.
        # Room workers add themselves to _callback_tasks but also register a
        # done-callback that discards them from that set when they complete.
        # If a worker finishes naturally between worker.cancel() and the gather
        # below, the done-callback fires, removes it from _callback_tasks, and
        # the gather would miss it — leaving an unobserved task exception.
        # Grabbing the workers list before cancellation and including it in the
        # gather set ensures all workers are awaited regardless of timing.
        worker_list = list(self._room_workers.values())
        for worker in worker_list:
            worker.cancel()
        # Cancel and drain all in-flight callback tasks (room workers + others).
        # Union ensures tasks that already left _callback_tasks via done-callback
        # are still awaited through worker_list.
        for task in list(self._callback_tasks):
            task.cancel()
        all_tasks = set(worker_list) | self._callback_tasks
        if all_tasks:
            await asyncio.gather(*all_tasks, return_exceptions=True)
        self._callback_tasks.clear()
        self._room_queues.clear()
        # Its sibling, and missed for the same reason siblings usually are: the per-room
        # queues are cleared here and the shared routing queue was not. Frames left in it
        # belong to a connection that no longer exists — a later reuse would start workers
        # that offer rooms from old activity, carrying an old `roomParticipant: true`
        # snapshot into a membership decision made after the gap.
        while not self._routing_queue.empty():
            try:
                self._routing_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._room_workers.clear()
        if self._ws:
            await self._ws.close()
            self._ws = None
        logger.info("WebSocket client stopped")

    async def subscribe_room(
        self,
        room_id: str,
        callback: Callable,
        timeout: float = 10.0,
    ) -> str:
        """Subscribe to stream-room-messages and wait for server confirmation.

        Sends the ``sub`` DDP frame and blocks until the server responds with
        ``ready`` (success) or ``nosub`` (rejection).  This ensures the caller
        knows the subscription is truly active before registering processors
        in the dispatcher.

        Args:
            room_id:  Room to subscribe to.
            callback: Async callback invoked for each incoming message.
            timeout:  Seconds to wait for server confirmation.

        Returns:
            The DDP subscription ID.

        Raises:
            RuntimeError: If the server rejects the subscription (``nosub``).
            asyncio.TimeoutError: If confirmation is not received within timeout.
        """
        return await self._subscribe_with_confirmation(
            room_id=room_id,
            callback=callback,
            timeout=timeout,
            keep_callback_on_failure=False,
        )

    @property
    def stream_active(self) -> bool:
        """Whether the stream is currently carrying every room.

        Asked rather than remembered by the connector. A restore that fails leaves the
        transport on per-room subscriptions while a connector-side flag would still say
        otherwise — and a watcher added after that would register a callback for a room
        nobody had subscribed to, and receive nothing, silently.
        """
        return self._stream_sub_id is not None

    def register_room_callback(self, room_id: str, callback: Callable) -> None:
        """Route a room to its own callback without subscribing to it.

        For subscribe-all, where the stream already delivers the room and the only thing
        missing is which handler owns it. Kept separate from `subscribe_room` so that
        "this room is tracked" and "ask the server for this room" stay two decisions —
        conflating them is what made `subscribe_room` gate delivery in the first place.
        """
        self._callbacks[room_id] = callback

    async def unsubscribe_rooms_keeping_callbacks(self) -> None:
        """Drop every per-room server subscription, keeping the local routing.

        For the moment the stream takes over. Watcher restoration subscribes each room
        before `start_inbound()` runs, so without this every tracked message arrives twice
        once the stream is confirmed — dedup hides the second handler call, but both copies
        occupy a queue slot and a worker turn, and the drop when a queue fills is a real
        message.

        The callbacks stay: the room is still tracked, and which handler owns it is a
        separate question from who asked the server for it — the same separation
        `register_room_callback` exists for.
        """
        for room_id, sub_id in list(self._subscriptions.items()):
            # Released on the server *first*, and only then dropped from the map. The other
            # order loses the subscription: this migration can be cancelled at the send —
            # a recovery displaces whatever is running, and that is a normal event now — and
            # a mapping already removed leaves the still-live subscription invisible to the
            # replacement, which resubscribes the room and finds no predecessor to release.
            #
            # A mapping left behind by that same cancellation is the harmless direction: it
            # names a subscription that may already be gone, and both the next install and
            # `unsubscribe_room` would re-send an `unsub` the server ignores. An untracked
            # live subscription is the one nothing can ever clean up.
            #
            # The unsub goes out either way, because an unsub for an id the server no
            # longer knows is ignored, and this loop cannot tell the two cases apart.
            #
            # It is *not* true that nothing else would release a replaced id — an earlier
            # version of this comment said so, and believing it is what left the sibling
            # site in `_subscribe_with_confirmation` unguarded. A replacement goes through
            # that function, which releases its predecessor before installing itself. The
            # release is precisely the await during which a race can occur; a model in
            # which the replacement silently overwrites has no such await in it, and
            # therefore no reason to guard.
            try:
                await self._send({"msg": "unsub", "id": sub_id})
            except Exception as e:
                logger.warning(
                    "Could not release the per-room subscription for %s: %s", room_id, e)
            # Compare before removing: a stream lost mid-migration starts the per-room
            # fallback, which can install a *new* subscription for a room this loop has
            # already read, and dropping that one would leave the replacement untracked.
            if self._subscriptions.get(room_id) == sub_id:
                self._subscriptions.pop(room_id, None)
                state = self._subscription_states.get(room_id)
                if state is not None and state.sub_id == sub_id:
                    state.sub_id = None

    def register_default_callback(self, callback: Callable) -> None:
        """Register the handler for rooms with no per-room callback (subscribe-all)."""
        self._default_callback = callback

    async def subscribe_all(self, timeout: float = 10.0) -> bool:
        """Subscribe to every room this account can see, via `__my_messages__`.

        Returns True when the server confirms, False when it refuses (`nosub`) or does not
        answer in time — the caller then falls back to per-room subscriptions. A refusal is
        not an error to raise: it is a capability answer, and an older or differently
        configured server that lacks the stream should still run the gateway.

        The subscription id is kept in `_stream_sub_id`, not in `_subscriptions`, which
        maps *rooms* to their subscription ids. Storing a stream there would make
        `unsubscribe_room` able to tear down every room's delivery by name of one.
        """
        # Intent is recorded before the attempt, not after it succeeds. A timeout, a send
        # failing during a brief disconnect, or any transient error used to return False
        # with the intent never set — so every later reconnect saw no stream to restore and
        # the connector stayed on per-room delivery for the rest of its life, having asked
        # for the stream exactly once.
        self._wants_stream = True

        # Which recovery era this attempt belongs to. An attempt is not identifiable by its
        # subscription id alone — the id is what a *later* attempt would be erasing, not
        # what tells this one it has been overtaken — and `stop()` retires every attempt at
        # once without knowing any of their ids.
        generation = self._recovery_generation

        sub_id = self._new_id()
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_subs[sub_id] = future
        # Published before the send, not after the confirmation: a `nosub` can arrive in the
        # same batch of frames as the `ready` that resolved this future, and until this is
        # set nothing in the receive loop can tell that such a frame is about the stream.
        self._pending_stream_sub_id = sub_id
        try:
            await self._send(
                {
                    "msg": "sub",
                    "id": sub_id,
                    "name": "stream-room-messages",
                    "params": ["__my_messages__", False],
                }
            )
            await asyncio.wait_for(future, timeout=timeout)
        except Exception as e:
            # A timeout is not a refusal: the server may have accepted and simply answered
            # late. Leaving that subscription live while the connector opens per-room ones
            # means every message arrives twice — and the connector would report per-room
            # delivery while untracked rooms kept arriving. Cancel it explicitly; a `nosub`
            # makes this a no-op the server ignores.
            try:
                await self._send({"msg": "unsub", "id": sub_id})
            except Exception as unsub_error:
                logger.warning(
                    "Could not cancel the uncertain __my_messages__ subscription: %s",
                    unsub_error,
                )
            logger.warning(
                "Subscribe-all (__my_messages__) refused or timed out (%s) — "
                "falling back to per-room subscriptions",
                e,
            )
            return False
        finally:
            self._pending_subs.pop(sub_id, None)
            # Only what this attempt published. A socket drop while the confirmation was
            # in flight starts a replacement attempt — `_reconnect` spawns the resubscribe
            # task *before* its `finally` fails the old future — so the newer attempt can
            # have published its own id by the time this runs. Erasing it would leave the
            # replacement unrecognisable to the receive loop, which is exactly the window
            # the previous round closed, reopened from the other side.
            if self._pending_stream_sub_id == sub_id:
                self._pending_stream_sub_id = None
            revoked = self._revoked_stream_sub_id == sub_id
            if revoked:
                self._revoked_stream_sub_id = None

        if revoked:
            # Confirmed and then terminated before this coroutine was scheduled again. The
            # confirmation is stale, and recording it would claim delivery the server has
            # already stopped — after which the connector releases every per-room
            # subscription and no room receives anything at all.
            logger.warning(
                "Subscribe-all (__my_messages__) was confirmed and then terminated before "
                "it took effect — falling back to per-room subscriptions"
            )
            return False

        if generation != self._recovery_generation:
            # Overtaken: a newer recovery started, or the client was stopped, while this
            # attempt was in flight. Recording it now would publish an id nobody is
            # tracking — after `stop()` it also revives `stream_active` on a closed socket,
            # so a reused client registers watcher callbacks and subscribes nothing.
            logger.warning(
                "Subscribe-all (__my_messages__) was confirmed after the recovery that "
                "asked for it was retired — discarding it"
            )
            try:
                await self._send({"msg": "unsub", "id": sub_id})
            except Exception:
                # Best effort: the socket this was confirmed on is usually gone already.
                pass
            return False

        self._stream_sub_id = sub_id
        logger.info("Subscribed to __my_messages__ — delivery is no longer per room")
        return True

    def register_membership_callback(self, callback: Callable) -> None:
        """Register the async callback for subscriptions-changed events (§2.7).

        Called as `callback(action, subscription_doc)` with action `inserted`
        or `removed` — `updated` fires on every unread-count change and is
        filtered in the receive loop, before any task is spawned for it.
        """
        self._membership_callback = callback

    async def subscribe_membership_events(self, timeout: float = 10.0) -> bool:
        """Subscribe to this account's subscriptions-changed notify stream.

        Same intent-before-attempt and generation discipline as
        `subscribe_all`, without its pending/revoked hand-off machinery — a
        confirmation raced by a `nosub` here costs missed membership events,
        which the first-message safety net and the reconciliation both cover
        (§2.7); it cannot cost room delivery. Returns False on refusal or
        timeout, and the caller treats that as degraded, not fatal.
        """
        if not self._user_id:
            logger.warning(
                "No user id from the DDP login — membership events cannot be "
                "subscribed")
            return False
        self._wants_membership = True
        generation = self._recovery_generation

        sub_id = self._new_id()
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_subs[sub_id] = future
        try:
            await self._send(
                {
                    "msg": "sub",
                    "id": sub_id,
                    "name": "stream-notify-user",
                    "params": [f"{self._user_id}/subscriptions-changed", False],
                }
            )
            await asyncio.wait_for(future, timeout=timeout)
        except Exception as e:
            # A timeout is not a refusal — cancel so an accepted-late
            # subscription is not left delivering to a client that recorded
            # nothing (same rule as subscribe_all).
            try:
                await self._send({"msg": "unsub", "id": sub_id})
            except Exception:
                pass
            logger.warning(
                "Membership-events subscription refused or timed out (%s) — "
                "joins and removals will be discovered by messages and the "
                "reconciliation instead", e,
            )
            return False
        finally:
            self._pending_subs.pop(sub_id, None)

        if generation != self._recovery_generation:
            # Overtaken by a newer recovery or a stop — the replacement owns
            # the field now (or the client is stopped and must record nothing).
            try:
                await self._send({"msg": "unsub", "id": sub_id})
            except Exception:
                pass
            return False

        self._membership_sub_id = sub_id
        logger.info("Subscribed to subscriptions-changed — membership events are live")
        return True

    def _still_owns(self, room_id: str, sub_id: str, state) -> bool:
        """Whether this subscription attempt may still speak for `room_id`.

        Three things have to hold, and each was learned the hard way at a different await:

        * **No removal in progress.** `_rooms_unsubscribing` is raised for the duration of
          one `unsubscribe_room`.
        * **The room's state object is still the one this attempt was given.**
          `unsubscribe_room` pops it, so a *completed* removal — which clears the marker on
          its way out — is invisible to the marker and visible here. Identity, not
          membership: a room removed and immediately re-added is also not this attempt's
          room.
        * **The room's subscription is still this attempt's.** The state object is *shared*
          between attempts for the same room — a successor reuses it rather than making a
          new one — so identity alone cannot tell a successor apart from the original. A
          successor that unsubscribes this attempt, installs its own id and then fails
          while keeping the callback leaves the state object exactly where it was, and this
          attempt would then mark it active and report success for a subscription the
          server no longer has. `sub_id` is the only thing here that names *one attempt*.

        The third clause is the one this predicate was created for. The `except` arm below
        deliberately does *not* call this — it tests `_subscriptions.get(room_id) == sub_id`
        alone, because a failing attempt should roll back what it still owns even when the
        room's state object has been replaced under it. Two questions that look alike and
        are not.
        """
        return (
            room_id not in self._rooms_unsubscribing
            and self._subscription_states.get(room_id) is state
            and self._subscriptions.get(room_id) == sub_id
        )

    async def _subscribe_with_confirmation(
        self,
        room_id: str,
        callback: Callable,
        timeout: float,
        keep_callback_on_failure: bool,
    ) -> str:
        """Send a room subscription and wait for explicit server confirmation."""
        sub_id = self._new_id()
        state = self._subscription_states.get(room_id)
        if state is None:
            state = SubscriptionState(room_id=room_id, callback=callback)
            self._subscription_states[room_id] = state
        else:
            state.callback = callback

        # Register confirmation future BEFORE sending the sub frame so the
        # _listen_loop can resolve it as soon as the server replies.
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_subs[sub_id] = future

        # A room has at most one subscription, and installing one releases its
        # predecessor. Overwriting the mapping without releasing it left the old
        # subscription live on the server and untracked — nothing could ever unsubscribe
        # it, so removing the watcher stopped only the replacement while the original kept
        # delivering. That happens whenever a recovery is interrupted partway through
        # releasing them, which is now a normal event rather than an exotic one, since a
        # recovery cancels whatever it displaces.
        #
        # There **is** an await between reading this and installing the replacement — the
        # `unsub` send below — so the read has to be re-checked after it rather than
        # trusted. An earlier version of this comment claimed the two happened in one
        # synchronous block, which was not merely untrue: it named the exact shape this
        # class of defect has, and asserted immunity to it eleven lines above the await.
        superseded = self._subscriptions.get(room_id)
        if superseded is not None:
            # Released **before** the map is updated, not after. The map is the only record
            # of what can still be live on the server, so at every await point it has to
            # name something releasable. Installing first and releasing second inverts
            # that: a cancellation at this send — an ordinary event, since a recovery
            # cancels whatever it displaces — would leave the map naming an id whose `sub`
            # frame has not even gone out yet, while the predecessor is still live and now
            # invisible to everyone, including the watcher removal that should have ended
            # it. Same rule, same reason, as the migration loop in
            # `unsubscribe_rooms_keeping_callbacks`; this is its second site.
            try:
                await self._send({"msg": "unsub", "id": superseded})
            except Exception as e:
                logger.warning(
                    "Could not release the superseded subscription for %s: %s", room_id, e
                )
            # A watcher removal can *complete* inside that await, and completing is
            # precisely what clears `_rooms_unsubscribing` — so the marker cannot be the
            # test here; the later check for it would see a room that has already finished
            # being removed and install a subscription for it anyway, re-registering the
            # callback and opening a server subscription after the last watcher left.
            #
            # The state object can be the test for a *removal*. `unsubscribe_room` pops it,
            # so a different object (or none) means this room is no longer the one this
            # call was asked to subscribe. Identity, not membership: a room removed and
            # immediately re-added is also not this call's room.
            #
            # But identity cannot see a *successor*, because the state object is shared
            # between attempts for one room — the same reason `_still_owns` needs its third
            # clause. A second attempt that installed its own id inside the await above
            # leaves the object exactly where this one left it, and installing below would
            # then overwrite the successor's mapping. The successor's own confirmation
            # rightly declines to roll back a mapping it no longer owns, so nothing is left
            # naming its subscription and no `unsub` can ever reach it: live on the server,
            # invisible to `unsubscribe_room`, for the life of the socket. That is verbatim
            # the defect the release-before-install ordering exists to prevent, reached
            # through the other door.
            #
            # So the map is re-read, not assumed. `superseded` is what this attempt
            # released and therefore what the map must still name for this attempt to be
            # the one entitled to replace it.
            if (
                self._subscription_states.get(room_id) is not state
                or self._subscriptions.get(room_id) != superseded
            ):
                self._pending_subs.pop(sub_id, None)
                raise RuntimeError(
                    f"Room {room_id} was claimed by another subscription attempt while "
                    "its previous subscription was being released"
                )

        # Installed only now, and synchronously: nothing may await between claiming the
        # room and recording the claim.
        state.sub_id = sub_id
        state.status = "pending"
        state.last_error = None
        self._subscriptions[room_id] = sub_id
        self._callbacks[room_id] = callback

        try:
            await self._send(
                {
                    "msg": "sub",
                    "id": sub_id,
                    "name": "stream-room-messages",
                    "params": [room_id, False],
                }
            )
            await asyncio.wait_for(future, timeout=timeout)
            # Check for a concurrent unsubscribe_room call that was in-flight
            # while this confirmation arrived.  unsubscribe_room removes the
            # room from _callbacks/_subscriptions and sends a DDP unsub frame,
            # but if it ran while we were awaiting the confirmation future (a
            # cooperative-multitasking yield point), the server acknowledged
            # our sub before processing the unsub.  We must roll back the
            # local state so the room is not left as an active subscription
            # after the caller intended to remove it.
            # Both tests, because the marker only catches a removal still *in progress*.
            # The reasoning is already written above, at the release await: completing is
            # what clears `_rooms_unsubscribing`, so a removal that finishes entirely
            # inside this confirmation wait leaves nothing for the marker to see — and
            # this wait is far longer than that one, since it spans a server round trip.
            # The rule was stated there and applied there only; this is the site that
            # needed it more. Reported as success, it hands the caller a room whose
            # mapping, callback and state have all been removed, and a processor is
            # installed for a subscription nothing tracks.
            if not self._still_owns(room_id, sub_id, state):
                # Only what this attempt still owns. A recovery starting while a direct
                # `subscribe_room` is mid-confirmation makes two attempts for one room, and
                # the loser's rollback used to take the winner's mapping, callback and
                # state with it — leaving a subscription live on the server that nothing
                # tracks, whose messages then take the unrouted path and are discarded
                # because the connector considers the room tracked.
                if self._subscriptions.get(room_id) == sub_id:
                    self._subscriptions.pop(room_id, None)
                    self._callbacks.pop(room_id, None)
                if state.sub_id == sub_id:
                    state.sub_id = None
                    state.status = "failed"
                    state.last_error = "unsubscribed concurrently during resubscription"
                self._pending_subs.pop(sub_id, None)
                raise RuntimeError(
                    f"Room {room_id} was unsubscribed while resubscription was "
                    "in flight — subscription rolled back"
                )
            state.status = "active"
            state.dropped_messages = 0
        except Exception as e:
            # One arm, not two. There used to be a `(TimeoutError, RuntimeError)` arm and a
            # generic one doing the same thing, and the ownership rule was added to the
            # first only — so a `_send` raising a transport error after a successor had
            # installed itself took the successor's state down through the other door. A
            # rule stated twice is a rule that will be applied once.
            #
            # `CancelledError` is a `BaseException` and still propagates untouched: a
            # cancelled attempt has a caller waiting to decide what its state means.
            #
            # Roll back only the part this attempt still owns. A newer attempt for this
            # room may already have installed its own, and this one's failure is often
            # *caused* by that: the replacement releases its predecessor, and the release
            # is what this future is being rejected for.
            owns = self._subscriptions.get(room_id) == sub_id
            if owns:
                self._subscriptions.pop(room_id, None)
            if state.sub_id == sub_id:
                state.sub_id = None
                state.status = "failed"
                state.last_error = str(e)
            if owns and not keep_callback_on_failure:
                self._callbacks.pop(room_id, None)
                self._subscription_states.pop(room_id, None)
            self._pending_subs.pop(sub_id, None)
            raise
        finally:
            self._pending_subs.pop(sub_id, None)

        logger.info("Subscribed to room %s (sub_id=%s, confirmed)", room_id, sub_id)
        return sub_id

    async def unsubscribe_room(self, room_id: str) -> None:
        """Unsubscribe from a room and cancel its worker task."""
        # Mark this room as being unsubscribed before mutating any state.
        # _subscribe_with_confirmation checks this set after subscription
        # confirmation arrives; if the room is being unsubscribed concurrently
        # (because the recovery captured it in its snapshot before
        # this call), the confirmation will be rolled back rather than
        # re-registering the room as an active subscription.
        self._rooms_unsubscribing.add(room_id)
        try:
            sub_id = self._subscriptions.pop(room_id, None)
            self._callbacks.pop(room_id, None)
            self._subscription_states.pop(room_id, None)
            self._room_queues.pop(room_id, None)
            # Cancel the room's worker task to prevent zombie coroutines.
            worker = self._room_workers.pop(room_id, None)
            if worker and not worker.done():
                worker.cancel()
                try:
                    await worker
                except asyncio.CancelledError:
                    pass
            if sub_id and self._ws:
                await self._send({"msg": "unsub", "id": sub_id})
                logger.info("Unsubscribed from room %s", room_id)
        finally:
            self._rooms_unsubscribing.discard(room_id)

    def register_reconnect_callback(self, cb: Callable[[], Any]) -> None:
        """Register an async callback invoked after every successful reconnect.

        Called once per reconnect cycle, after all room subscriptions have been
        re-confirmed (or attempted).  The connector uses this hook to replay
        messages missed during the outage via the REST history API.

        Only one callback is supported; calling this method again replaces the
        previous registration.
        """
        self._on_reconnect_cb = cb

    @property
    def is_connected(self) -> bool:
        """True if the WebSocket connection is currently open."""
        return self._ws is not None

    @property
    def subscription_statuses(self) -> dict[str, dict[str, str | None]]:
        """Return a snapshot of room subscription health for diagnostics."""
        return {
            room_id: {
                "sub_id": state.sub_id,
                "status": state.status,
                "last_error": state.last_error,
                "dropped_messages": state.dropped_messages,
            }
            for room_id, state in self._subscription_states.items()
        }

    async def call_method(
        self, method: str, params: list, timeout: float = 5.0
    ) -> dict:
        """Call a DDP method and return the server's result.

        Waits up to ``timeout`` seconds for the result message.  Used for
        side-effect calls like typing notifications where we want to surface
        any server-side errors for debugging.

        Fast-fails when the WebSocket is disconnected to avoid stalling
        callers (e.g. typing indicators) for the full timeout duration.

        Returns the raw result dict (may contain an ``error`` key).
        """
        if not self._ws:
            logger.debug("call_method %r skipped — WebSocket not connected", method)
            return {}
        method_id = self._new_id()
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_results[method_id] = future
        logger.debug("call_method → %r params=%s id=%s", method, params, method_id)
        try:
            await self._send(
                {
                    "msg": "method",
                    "method": method,
                    "id": method_id,
                    "params": params,
                }
            )
            result = await asyncio.wait_for(future, timeout=timeout)
            logger.debug("call_method ← %r result=%s", method, result)
            if result.get("error"):
                logger.debug("call_method %r error: %s", method, result["error"])
            return result
        except asyncio.TimeoutError:
            logger.debug(
                "call_method %r timed out (no result in %.1fs)", method, timeout
            )
            return {}
        finally:
            self._pending_results.pop(method_id, None)

    # -- Internal methods --

    async def _send(self, data: dict) -> None:
        if self._ws:
            await self._ws.send(json.dumps(data))

    async def _recv(self) -> dict:
        if not self._ws:
            raise RuntimeError("Not connected")
        raw = await self._ws.recv()
        return json.loads(raw)

    async def _recv_until_result(self, method_id: str, timeout: float = 15.0) -> dict:
        """Receive messages until we get the result for our method call."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(
                    self._ws.recv(),
                    # Clamp to a small positive minimum: if the deadline has
                    # already passed (due to cooperative yielding between the
                    # while-condition check and here), a zero or negative timeout
                    # has implementation-defined behaviour across Python versions.
                    timeout=max(0.001, deadline - asyncio.get_running_loop().time()),
                )
                msg = json.loads(raw)
                if msg.get("msg") == "ping":
                    await self._send({"msg": "pong"})
                    continue
                if msg.get("msg") == "result" and msg.get("id") == method_id:
                    return msg
            except asyncio.TimeoutError:
                break
        raise RuntimeError(f"Timeout waiting for result of method {method_id}")

    async def _listen_loop(self) -> None:
        """Main receive loop. Dispatches room messages to callbacks."""
        while self._running:
            try:
                if not self._ws:
                    await self._reconnect()
                    continue

                raw = await self._ws.recv()
                msg = json.loads(raw)
                msg_type = msg.get("msg")

                if msg_type == "ping":
                    await self._send({"msg": "pong"})
                elif (
                    msg_type == "changed"
                    and msg.get("collection") == "stream-room-messages"
                ):
                    await self._handle_room_message(msg)
                elif (
                    msg_type == "changed"
                    and msg.get("collection") == "stream-notify-user"
                ):
                    self._handle_notify_user(msg)
                elif msg_type == "result":
                    mid = msg.get("id")
                    if mid in self._pending_results:
                        fut = self._pending_results[mid]
                        if not fut.done():
                            fut.set_result(msg)
                elif msg_type == "ready":
                    # Resolve pending subscription futures for confirmed subs.
                    for confirmed_id in msg.get("subs", []):
                        fut = self._pending_subs.get(confirmed_id)
                        if fut and not fut.done():
                            fut.set_result(True)
                elif msg_type == "nosub":
                    # Reject the pending subscription future with an error.
                    nosub_id = msg.get("id", "")
                    nosub_error = msg.get("error", {}).get(
                        "message", "subscription rejected"
                    )
                    fut = self._pending_subs.get(nosub_id)
                    if fut and not fut.done():
                        fut.set_exception(
                            RuntimeError(
                                f"Subscription rejected by server: {nosub_error}"
                            )
                        )
                    elif nosub_id and nosub_id == self._stream_sub_id:
                        self._on_stream_lost(nosub_error)
                    elif nosub_id and nosub_id == self._pending_stream_sub_id:
                        # `ready` then `nosub`, before `subscribe_all` resumed. The future
                        # exists but is already resolved, so the branch above sees nothing
                        # to reject, and `_stream_sub_id` is still unset, so the stream
                        # branch does not recognise its own subscription. Left unhandled the
                        # caller went on to record a dead id as live, and the connector then
                        # released every per-room subscription — all rooms dark, silently,
                        # until an unrelated reconnect.
                        self._revoked_stream_sub_id = nosub_id
                        logger.warning(
                            "The __my_messages__ stream was terminated between its "
                            "confirmation and being recorded (%s)", nosub_error,
                        )
                    elif nosub_id and nosub_id == self._membership_sub_id:
                        # Degraded, not fatal: joins and removals fall back to
                        # the first-message path and the reconciliation until
                        # the next recovery re-subscribes (§2.7).
                        self._membership_sub_id = None
                        logger.warning(
                            "The subscriptions-changed stream was terminated "
                            "by the server (%s) — membership events are "
                            "degraded until the next reconnect", nosub_error,
                        )
                    else:
                        logger.warning(
                            "Subscription rejected (no pending future): %s", msg
                        )
                        for state in self._subscription_states.values():
                            if state.sub_id == nosub_id:
                                state.status = "failed"
                                state.last_error = nosub_error
                                break
                else:
                    logger.debug("Unhandled WS message: %s", msg_type)

            except websockets.ConnectionClosed as e:
                logger.warning(
                    "WebSocket connection closed: code=%s reason=%r",
                    e.code, e.reason,
                )
                self._ws = None
                # Invalidate in-flight work *here*, where the outage is detected, not
                # where recovery starts. `_reconnect` sleeps out a backoff of up to a
                # minute and only bumps after `connect()` succeeds, and the routing
                # workers are independent tasks that nothing pauses meanwhile — so a
                # socket-only outage with REST still healthy drains the whole backlog
                # during the backoff, every frame carrying pre-drop standing. That is
                # the failure this generation exists to prevent, narrowed rather than
                # closed. Retiring an in-flight `subscribe_all` early is correct for
                # the same reason: its socket is already gone.
                self._recovery_generation += 1
                await self._reconnect()
            except json.JSONDecodeError as e:
                # Malformed frame — log and continue without reconnecting.
                # Reconnecting here would be spurious: the connection is still
                # healthy; only this one frame was unparseable.
                logger.warning("Received unparseable WebSocket frame (ignored): %s", e)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Error in listen loop: %s", e)
                self._ws = None
                # The same invalidation as the ConnectionClosed arm (Codex
                # round 10): this arm drops the socket and enters the same
                # backoff, and a frame stamped before the outage must not be
                # consumed with pre-drop standing during it — whatever
                # exception subtype took the socket down.
                self._recovery_generation += 1
                await self._reconnect()

    # Maximum messages buffered per room before drops.  This bounds memory
    # usage under burst load instead of creating unbounded tasks.
    _ROOM_QUEUE_DEPTH = 50

    # Concurrent consumers of the shared routing queue. Small, because routing decides
    # whether a room should exist rather than answering anyone — but more than one, since a
    # single consumer serializes every room behind the slowest classification lookup.
    _ROUTING_WORKERS = 4

    def _handle_notify_user(self, msg: dict) -> None:
        """Dispatch a subscriptions-changed event to the membership callback (§2.7).

        Cheap filters first, on the receive loop: `updated` fires on every
        unread-count change in every room this account is in, so it is
        discarded before any task exists for it. The callback itself runs on
        its own task in `_callback_tasks` (drained by `stop`), because it ends
        in record writes and possibly REST — nothing the receive loop waits on.
        """
        if self._membership_callback is None:
            return
        fields = msg.get("fields") or {}
        event_name = fields.get("eventName") or ""
        if not event_name.endswith("/subscriptions-changed"):
            return
        args = fields.get("args") or []
        if len(args) < 2 or not isinstance(args[1], dict):
            return
        action, doc = args[0], args[1]
        if action not in ("inserted", "removed"):
            return
        task = asyncio.create_task(self._run_membership_callback(action, doc))
        self._callback_tasks.add(task)
        task.add_done_callback(self._callback_tasks.discard)

    async def _run_membership_callback(self, action: str, doc: dict) -> None:
        try:
            await self._membership_callback(action, doc)
        except Exception:
            logger.exception(
                "Unhandled error in membership callback (action=%s)", action)

    async def _handle_room_message(self, msg: dict) -> None:
        """Extract room message and dispatch to per-room worker queue.

        Instead of creating one asyncio task per inbound message (unbounded),
        messages are placed on a bounded per-room queue consumed by a single
        worker task per room.  This provides:
          - Explicit memory bounding under burst load
          - Guaranteed per-room ordering (single worker per room)
          - Bounded concurrency via the existing semaphore
        """
        try:
            fields = msg.get("fields", {})
            args = fields.get("args", [])
            if not args:
                return

            message_doc = args[0]
            if not isinstance(message_doc, dict):
                # Reachable now in a way it was not before: the room used to be read from
                # `eventName` first, which short-circuited past this value entirely when
                # it was present. Guarded rather than left to the outer handler, which
                # would report a malformed frame as "error handling room message".
                logger.debug("Ignoring frame whose first arg is not a message doc")
                return

            # `rid` is authoritative, and `eventName` is only a fallback for a frame that
            # somehow carries no message.
            #
            # The order used to be the other way round, which worked only because a
            # per-room `sub` makes Rocket.Chat set `eventName` to the room id itself. On a
            # stream that spans rooms — `__my_messages__` — `eventName` is the **literal
            # stream name** (§6.1), so every room would resolve to one key, and with it one
            # callback, one queue and one worker: per-room ordering silently becomes global
            # ordering, and a single slow room stalls every other.
            #
            # This is the key-space split. The room key identifies where a message goes;
            # the *stream* key identifies what was subscribed to. They coincide today
            # because we subscribe per room, and they stop coinciding the moment we do not.
            room_id = message_doc.get("rid") or fields.get("eventName")
            if not room_id:
                return

            # The access object Rocket.Chat appends: `roomParticipant`, `roomType`, and —
            # for rooms that have one — `roomName`. A DM's object is present and simply
            # omits the name (`{"roomParticipant": true, "roomType": "d"}`, §6.1); it is
            # the *field* that is missing, not the object.
            #
            # Observed on `__my_messages__` (§6.1). Whether a per-room subscription also
            # carries one has not been checked, which is exactly why this is optional
            # rather than assumed: the code threads through whatever arrives and the
            # connector decides what a missing one means.
            access = args[1] if len(args) > 1 and isinstance(args[1], dict) else None

            callback = self._callbacks.get(room_id)
            if callback is None:
                if self._default_callback is None:
                    return
                # Routing goes on one shared queue, not a per-room one.
                #
                # A per-room queue and worker per *untracked* room means one of each for
                # every room that ever emits a frame — including every readable public
                # channel the membership gate is about to reject — and nothing ever reaps
                # them, because only `unsubscribe_room` removes those objects and an
                # untracked room never had a subscription to remove.
                #
                # Per-room ordering is what the room queues exist for, and routing does not
                # need it: the question is "should this room have a watcher", asked once
                # per room, and the answer does not depend on message order.
                self._queue_for_routing(message_doc, access)
                return

            self.deliver_to_room(room_id, message_doc, access, callback=callback)
        except Exception as e:
            logger.error("Error handling room message: %s", e)

    def deliver_to_room(
        self,
        room_id: str,
        doc: dict,
        access: dict | None = None,
        *,
        callback: Callable | None = None,
    ) -> None:
        """Put one document on this room's worker queue, creating the worker if needed.

        **The only way a document should reach a room's handler.** The creation
        path used to call the connector's dispatch directly for a frame whose
        room became tracked while the frame waited in the routing queue — from
        up to four routing workers at once, around the per-room queue that is
        the thing making delivery ordered. If a newer frame is accepted before
        an older one is handed back, the older hand-back claims a boundary
        already past the message it is trying to preserve.

        So the **serialisation** lives here, in the transport, and the creation
        path asks for delivery instead of performing it.

        Serialisation is not ordering, and the difference matters: the queue
        guarantees that two deliveries for one room never run at once, not that
        they run in timestamp order. An older frame handed over from the routing
        queue still lands *behind* a newer one that arrived live, and the filter
        then rejects it as already processed. It is lost either way — the
        residue the coalescing already accepts — but deterministically rather
        than racily, and without a hand-back claiming a boundary past itself.
        """
        # Lazily create per-room queue and worker on first message.
        # Also re-create if the previous worker died (e.g. after reconnect).
        existing_worker = self._room_workers.get(room_id)
        if room_id not in self._room_queues or (
            existing_worker and existing_worker.done()
        ):
            # Clean up dead worker if present
            if existing_worker and existing_worker.done():
                old_queue = self._room_queues.pop(room_id, None)
                if old_queue and not old_queue.empty():
                    logger.warning(
                        "Room worker for %s died with %d unprocessed message(s) "
                        "in queue — these messages are lost",
                        room_id,
                        old_queue.qsize(),
                    )
                self._room_workers.pop(room_id, None)

            q: asyncio.Queue = asyncio.Queue(maxsize=self._ROOM_QUEUE_DEPTH)
            self._room_queues[room_id] = q
            task = asyncio.create_task(
                self._room_worker(room_id, q),
                name=f"rc-room-worker-{room_id[:8]}",
            )
            self._room_workers[room_id] = task
            self._callback_tasks.add(task)
            task.add_done_callback(self._callback_tasks.discard)

        try:
            # The access object rides with the message: it describes *this delivery*
            # (is the account a participant, what kind of room, what is it called),
            # not the room in general, so storing it per room would be storing a
            # snapshot of the last message rather than a property.
            self._room_queues[room_id].put_nowait((doc, access))
        except asyncio.QueueFull:
            state = self._subscription_states.get(room_id)
            if state is None:
                # Falls back to the registered callback rather than to None: this is
                # the first caller that can arrive without one, and `SubscriptionState`
                # declares the field non-optional. Nothing reads it today, which is
                # exactly why a wrong value here would go unnoticed.
                state = SubscriptionState(
                    room_id=room_id,
                    callback=callback or self._callbacks.get(room_id),
                )
                self._subscription_states[room_id] = state
            state.dropped_messages += 1
            if state.status not in {"failed", "reconnecting"}:
                state.status = "degraded"
            state.last_error = f"inbound room queue overflow: dropped {state.dropped_messages} message(s)"
            logger.warning(
                "Inbound queue full for room %s — dropping message (drop_count=%d)",
                room_id[:8],
                state.dropped_messages,
            )

    def _queue_for_routing(self, doc: dict, access: dict | None) -> None:
        """Hand an untracked room's message to the shared routing worker."""
        self._routing_workers = [t for t in self._routing_workers if not t.done()]
        while len(self._routing_workers) < self._ROUTING_WORKERS:
            # A pool, not one worker. One worker serializes every room behind the slowest
            # classification — Rocket.Chat needs a REST lookup to tell a 1:1 from a group
            # DM — and the shared queue then overflows, dropping rooms that would have been
            # discovered. A pool is not per-room either, so nothing accumulates.
            task = asyncio.create_task(
                self._route_worker(), name=f"rc-routing-worker-{len(self._routing_workers)}")
            self._routing_workers.append(task)
            self._callback_tasks.add(task)
            task.add_done_callback(self._callback_tasks.discard)
        try:
            # Stamped with the generation this frame was *read* under. The queue and
            # its workers survive a reconnect untouched, so without it a frame read
            # before an outage is acted on after one — and its `access` object, which
            # is a snapshot of one delivery rather than a property of the room, still
            # says `roomParticipant: true` for a channel the account may have been
            # removed from while the socket was down. Creating a watcher on that is
            # the one outcome routing must not produce.
            self._routing_queue.put_nowait((doc, access, self._recovery_generation))
        except asyncio.QueueFull:
            # Dropped rather than blocking the listen loop. A lost routing frame costs the
            # first message of a room that has no watcher yet — the next one asks again —
            # whereas blocking here would stall delivery for every tracked room.
            logger.warning("Routing queue full — dropping an untracked-room frame")

    async def _route_worker(self) -> None:
        """Consume routing frames sequentially, off the per-room semaphore.

        Deliberately not holding `_callback_sem`: deciding whether a room should exist can
        involve a REST lookup (Rocket.Chat cannot tell a 1:1 from a group DM without one),
        and twenty slow lookups holding twenty permits would starve every tracked room's
        dispatch and overflow their bounded queues — trading messages that have a watcher
        for rooms that do not.
        """
        while True:
            doc, access, generation = await self._routing_queue.get()
            if generation != self._recovery_generation:
                # A recovery happened between reading this frame and reaching it. Its
                # `access` describes the account's standing at read time, and the very
                # thing a recovery re-establishes is what that standing now is — so
                # acting on it could create a watcher for a room the account left
                # during the outage.
                #
                # Dropped, and **nothing re-offers it**: replay iterates `self._rooms`,
                # the *tracked* rooms, and a routing frame exists precisely because its
                # room is not tracked. So the room waits for its next message. That is
                # the same cost the queue-full drop eleven lines up already accepts, and
                # it is the right trade — a stale `roomParticipant: true` creates a
                # watcher for a room the account may have left, which is worse than a
                # deferred one. Said plainly because an earlier version of this comment
                # claimed replay would cover it, which would tell the next reader the
                # loss is zero.
                logger.warning(
                    "Dropping a routing frame for room %s read before a recovery "
                    "(generation %d, now %d) — the room is offered again on its next "
                    "message, not by the replay",
                    (doc.get("rid") or "?")[:8], generation, self._recovery_generation,
                )
                continue
            # Not re-checked for None: nothing reaches this queue until
            # `register_default_callback` has run — `_queue_for_routing` returns early
            # without one — and nothing ever clears it, `stop()` included. A `continue`
            # here would have been a branch no caller can produce.
            callback = self._default_callback
            try:
                await callback(doc, access)
            except Exception as e:
                logger.error("Routing callback failed: %s", e)

    async def _room_worker(self, room_id: str, queue: asyncio.Queue) -> None:
        """Consume messages for one room sequentially with bounded global concurrency."""
        # Tracks the message currently dequeued but not yet dispatched.  When
        # CancelledError fires between queue.get() and the semaphore acquire,
        # this message would otherwise be silently lost — it is no longer in
        # the queue, and the outer CancelledError drain only counts items still
        # there.  By tracking it explicitly we can include it in the lost count.
        in_flight: object = None
        try:
            while True:
                item = await queue.get()
                doc, access = item
                in_flight = item  # record before semaphore — cancellation safe
                # Looked up late, deliberately: a callback removed while the item waited
                # must not be called. Routing never arrives here — it has its own queue —
                # so this needs no fallback, and an earlier version that had the fallback
                # only in the fan-out discarded every routing frame at exactly this line.
                callback = self._callbacks.get(room_id)
                if callback is None:
                    # Not unreachable any more. `_handle_room_message` only ever
                    # enqueued after finding a callback, so the absence used to be
                    # impossible here; `deliver_to_room` is now also called by the
                    # creation path, which tests `connector._rooms` — a *different*
                    # map, populated before the callback when the per-room subscribe
                    # has to wait for the server to confirm. Losing the frame is the
                    # documented trade (the room has no watcher able to take it yet);
                    # losing it without a word is not.
                    logger.warning(
                        "Room %s: dropping a queued message — no callback is "
                        "registered for this room (it may still be subscribing)",
                        room_id[:8],
                    )
                if callback:
                    async with self._callback_sem:
                        # Semaphore acquired; doc is now being dispatched.
                        # Clear in_flight so the outer CancelledError handler
                        # doesn't double-count it (the mid-callback log below
                        # already accounts for it if cancelled here).
                        in_flight = None
                        try:
                            await callback(doc, access)
                        except asyncio.CancelledError:
                            # Worker was cancelled *while* the callback was in
                            # flight — the current message (doc) is lost.  Log
                            # it so operators can detect message loss during
                            # graceful shutdown.
                            msg_id = doc.get("_id", "<unknown>") if isinstance(doc, dict) else "<unknown>"
                            logger.warning(
                                "Room worker %s cancelled mid-callback — "
                                "message %s is lost (graceful shutdown)",
                                room_id[:8],
                                msg_id,
                            )
                            raise
                        except Exception as e:
                            logger.error(
                                "Callback error for room %s: %s", room_id[:8], e
                            )
                in_flight = None  # fully processed
        except asyncio.CancelledError:
            # Drain remaining queue items before exiting so operators can
            # see exactly how many messages were permanently lost.
            # These messages cannot be re-delivered: the connector already
            # advanced their watermarks (connector.py), and the WebSocket
            # subscription is being torn down.  get_nowait() is used
            # deliberately — await queue.get() would itself be cancelled
            # again because the task is in a cancelled state.
            remaining = 0
            # Count the in-flight message that was dequeued but cancelled
            # before the semaphore could be acquired.
            if in_flight is not None:
                remaining += 1
            while not queue.empty():
                try:
                    queue.get_nowait()
                    remaining += 1
                except asyncio.QueueEmpty:
                    break
            if remaining:
                logger.warning(
                    "Room worker %s cancelled with %d unprocessed message(s) "
                    "still in queue — messages permanently lost (watermarks "
                    "already advanced, replay will not re-deliver)",
                    room_id[:8],
                    remaining,
                )
            raise
        except Exception as e:
            logger.error("Room worker %s died: %s", room_id[:8], e)

    async def _ping_loop(self) -> None:
        """Send periodic pings to keep connection alive."""
        while self._running:
            try:
                await asyncio.sleep(25)
                if self._ws:
                    await self._send({"msg": "ping"})
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

    async def _reconnect(self) -> None:
        """Reconnect with exponential backoff, then re-subscribe all rooms.

        Ordering guarantee: ``connect()`` is awaited to completion before this
        method returns, so the ``_listen_loop`` caller resumes only after the
        full DDP handshake and login are done.  This means ``_recv_until_result``
        (called inside ``connect()``) has exclusive access to the WebSocket
        receive path — the listen loop cannot race with it.  Do not restructure
        this to run ``connect()`` concurrently with the listen loop without
        revisiting that invariant.
        """
        logger.info("Reconnecting in %.1fs...", self._reconnect_delay)
        await asyncio.sleep(self._reconnect_delay)
        self._reconnect_delay = min(
            self._reconnect_delay * 2, self._max_reconnect_delay
        )

        try:
            await self.connect()
            # The state marking moved into `_recover`, after it has waited for whatever it
            # displaced. Marking here wrote shared state while a cancelled-but-not-yet-
            # unwound recovery could still be inside an await — the same shape every
            # ownership finding in this file has had.
            #
            # Not guarded on `_callbacks`. With no tracked rooms that map is empty, and
            # that is exactly the state in which the stream is the *only* subscription —
            # so the guard skipped the restore precisely when it was the only thing left
            # to restore.
            if self._callbacks or self._wants_stream:
                self._start_recovery("Reconnect", try_stream=True)
            else:
                self._retire_recovery()
        except Exception as e:
            logger.error("Reconnect failed: %s", e)
            self._ws = None
        finally:
            # Resolve any futures that are still waiting for subscription
            # confirmation from the old connection.  This must run in a
            # ``finally`` block so it fires even when CancelledError is raised
            # during ``connect()`` (e.g. SIGTERM arriving mid-reconnect).
            #
            # Without this, a caller blocked in _subscribe_with_confirmation()
            # for a room that was mid-subscription when the connection dropped
            # would wait until its asyncio.wait_for timeout (default 30s) before
            # learning the subscription failed — both on normal reconnect AND on
            # task cancellation.  Failing them here with a clear error lets
            # callers surface the problem immediately.
            for sub_id, fut in list(self._pending_subs.items()):
                if not fut.done():
                    fut.set_exception(
                        RuntimeError(
                            "WebSocket connection lost while waiting for "
                            "subscription confirmation — reconnecting"
                        )
                    )
            self._pending_subs.clear()

    def _retire_recovery(self) -> asyncio.Task | None:
        """Stop the recovery in flight and invalidate whatever it started.

        Separate from `_start_recovery` because a reconnect retires the previous recovery
        whether or not it needs a new one: with no tracked rooms and no stream intent there
        is nothing to recover, but a recovery from before the socket dropped is still a
        coroutine holding stale subscription state, and letting it finish would have it
        write that state onto a connection it knows nothing about.
        """
        displaced = self._recovery_task
        if displaced is not None and not displaced.done():
            displaced.cancel()
        self._recovery_generation += 1
        return displaced

    def _start_recovery(self, reason: str, *, try_stream: bool) -> asyncio.Task:
        """Install the one recovery, replacing whatever was running.

        Both entry points come through here — a socket drop and a stream terminated under a
        healthy socket — because they differ only in whether the stream is worth asking for
        again. Everything else they did was the same sequence written twice over the same
        state, and every review round on this file found another write in it with no owner.

        Cancel-and-replace, always: a recovery that is no longer the current one has nothing
        to finish, and letting it run alongside is how two of them reached the replay
        callback together and answered one message twice.
        """
        displaced = self._retire_recovery()
        task = asyncio.create_task(
            self._recover(reason, try_stream=try_stream, displaced=displaced),
            name=f"rc-recovery:{reason}",
        )
        self._recovery_task = task
        self._callback_tasks.add(task)
        task.add_done_callback(self._callback_tasks.discard)
        return task

    async def _recover(
        self,
        reason: str,
        *,
        try_stream: bool,
        displaced: asyncio.Task | None = None,
    ) -> None:
        """Put delivery back, then recover what the gap lost. One sequence, one owner.

        The order is the whole design and every step of it was learned from a defect:

        1. Wait out the recovery this one replaced. Cancelling is a request; until it is
           observed the displaced run can still be inside an await.
        2. Announce the outage boundary, before any `sub` frame goes out — once the first
           room is confirmed, live traffic resumes for it while the rest are still
           subscribing. "Before any `sub`" is the property that matters, not "while nothing
           is running": the per-room workers survive a reconnect and keep draining frames
           queued before the drop.
        3. Ask for the stream, if this entry point is one that should. A stream the server
           has just terminated is not worth re-requesting immediately; a fresh socket is.
        4. Give every room a delivery path — the stream, or one subscription each. Never
           both, so a successful stream releases the per-room subscriptions.
        5. Replay the gap. Restoring delivery and recovering the outage are two obligations
           and a path that discharges only the first loses messages silently.
        """
        task = asyncio.current_task()
        try:
            if displaced is not None:
                # `gather` absorbs its CancelledError rather than re-raising it here, where
                # it would cancel this recovery too.
                await asyncio.gather(displaced, return_exceptions=True)

            # Released, then dropped — not dropped. Every per-room subscription this
            # recovery is about to replace goes through the same release-before-remove
            # loop a stream migration uses, which compares before removing so a
            # replacement installed mid-release stays tracked.
            #
            # The predecessor of this line popped the map instead, on the stated grounds
            # that "a reconnect's ids died with the socket, and a stream fallback has
            # none". The second half is false: a `subscribe_room` can land while a
            # recovery is running — the recovery's own comments say so elsewhere — and a
            # migration cancelled partway leaves ids behind. Dropping those without an
            # `unsub` left them live on the server and untracked, unreachable even by
            # `unsubscribe_room`.
            #
            # On a reconnect the frames go to a socket that never knew the ids and the
            # server ignores them, which is the cost of not having to know which case
            # this is. Clearing the map is still what stops the resubscribe finding a
            # `superseded` id for every room.
            await self.unsubscribe_rooms_keeping_callbacks()

            # Now that nothing else is writing them: every tracked room is between
            # delivery paths until this recovery gives it one.
            for room_id, callback in list(self._callbacks.items()):
                state = self._subscription_states.get(room_id)
                if state is None:
                    state = SubscriptionState(room_id=room_id, callback=callback)
                    self._subscription_states[room_id] = state
                else:
                    state.callback = callback
                # Not unconditionally `None`. The release above awaits once per room, and a
                # `subscribe_room` landing in one of those awaits installs an id this
                # recovery never released — blanking it here would leave that subscription
                # live on the server with nothing naming it, which is the same leak the
                # release-before-install ordering exists to prevent.
                if self._subscriptions.get(room_id) is None:
                    state.sub_id = None
                state.status = "reconnecting"
                state.last_error = None

            await self._fire_outage_callback()

            stream_restored = False
            if try_stream and self._wants_stream:
                # `_stream_sub_id` is the *current* subscription; `_wants_stream` is the
                # intent. Clearing the id used to clear both, so a restore that failed once
                # removed the only marker saying it should be retried. Intent survives
                # failure; only the id is per-attempt.
                self._stream_sub_id = None
                stream_restored = await self.subscribe_all()
                if not stream_restored:
                    logger.error(
                        "Could not restore the __my_messages__ subscription — untracked "
                        "rooms will not arrive, and tracked rooms are being resubscribed "
                        "individually as a fallback"
                    )

            if stream_restored:
                # The stream carries every tracked room, so a room may not also carry
                # itself: the server would send each message twice, and dedup hides the
                # second dispatch but not the queue slot it takes.
                await self.unsubscribe_rooms_keeping_callbacks()
                # The per-room confirmations that would clear `reconnecting` are exactly
                # what the stream makes unnecessary, so they are cleared here instead.
                for state in self._subscription_states.values():
                    state.status = "active"
                    state.last_error = None
                logger.info("Stream restored — per-room resubscription is not needed")
            else:
                await self._subscribe_rooms_individually(
                    list(self._callbacks.items()), context=reason
                )

            if self._wants_membership:
                # Intent survives failure, exactly like the stream's: the id
                # is per-attempt, and a restore that fails degrades to the
                # first-message and reconciliation paths rather than being
                # retried here.
                self._membership_sub_id = None
                await self.subscribe_membership_events()

            await self._fire_reconnect_callback()
        finally:
            if self._recovery_task is task:
                self._recovery_task = None

    async def _subscribe_rooms_individually(
        self, rooms: list[tuple[str, Callable]], context: str
    ) -> None:
        """Subscribe each room in its own right, and account for what failed.

        Shared by the two paths that have to put per-room delivery back: a reconnect whose
        stream restore did not happen, and a stream lost while the socket stayed up. They
        differ in *why* they run, not in what recovery means, and duplicating the accounting
        is how the two would drift into disagreeing about what a failed room looks like.
        """
        results = await asyncio.gather(
            *[
                self._subscribe_with_confirmation(
                    room_id=room_id,
                    callback=callback,
                    timeout=10.0,
                    keep_callback_on_failure=True,
                )
                for room_id, callback in rooms
            ],
            return_exceptions=True,
        )
        success = 0
        failures: list[str] = []
        for (room_id, _callback), result in zip(rooms, results, strict=False):
            if isinstance(result, Exception):
                state = self._subscription_states.get(room_id)
                if state:
                    state.status = "failed"
                    state.last_error = str(result)
                failures.append(f"{room_id}: {result}")
            else:
                success += 1
        if failures:
            logger.warning(
                "%s completed with partial subscription recovery: %d succeeded, %d failed (%s)",
                context,
                success,
                len(failures),
                "; ".join(failures[:5]),
            )
        elif rooms:
            logger.info("%s re-confirmed %d room subscription(s)", context, success)

    def register_outage_callback(self, cb: Callable[[], Any]) -> None:
        """Register the handler told that delivery has stopped, before it is restored.

        Separate from the reconnect callback because the two answer different questions at
        different moments: this one runs while nothing is subscribed, so what it observes is
        the state the outage started from; the other runs once delivery is back. Anything
        that has to be true *of the gap* has to be captured here — by the time the replay
        callback runs, live traffic has already moved on.
        """
        self._on_outage_cb = cb

    async def _fire_outage_callback(self) -> None:
        """Announce the outage boundary. Must precede every `sub` frame of a recovery."""
        if not self._on_outage_cb:
            return
        try:
            await self._on_outage_cb()
        except Exception as cb_err:
            logger.warning(
                "Outage callback raised an unexpected error (replay may fetch from the "
                "wrong point): %s",
                cb_err,
            )

    async def _fire_reconnect_callback(self) -> None:
        """Ask the connector to recover whatever the gap in delivery lost."""
        if not self._on_reconnect_cb:
            return
        try:
            await self._on_reconnect_cb()
        except Exception as cb_err:
            logger.warning(
                "Reconnect callback raised an unexpected error "
                "(history replay may be incomplete): %s",
                cb_err,
            )

    def _on_stream_lost(self, reason: str) -> None:
        """`nosub` for a stream the server had already confirmed — `live → lost` (§6.1).

        The reconnect path cannot cover this one: nothing disconnected, so nothing
        re-subscribes. Left alone the id stays set, `stream_active` goes on answering True,
        and *every tracked room is dark* — their per-room subscriptions were released when
        the stream took over, so at this instant no room has any delivery path at all. That
        is the worst shape a failure can take here, because nothing about it is visible from
        the outside: the socket is healthy and the gateway looks idle.

        Only the id is cleared. `_wants_stream` is the intent, and intent is not a state that
        failure clears — a later reconnect must still try the stream first.
        """
        self._stream_sub_id = None
        logger.error(
            "The __my_messages__ stream was dropped by the server (%s) — restoring per-room "
            "subscriptions; until the next reconnect retries the stream, rooms without a "
            "watcher cannot be discovered",
            reason,
        )
        # One entry point away from a reconnect's recovery, and the same one: the only
        # difference is that a stream the server has just terminated is not worth asking
        # for again this instant, so `try_stream` is False and delivery goes back to
        # per-room until the next reconnect retries it.
        self._start_recovery("Stream fallback", try_stream=False)

    @staticmethod
    def _new_id() -> str:
        return uuid.uuid4().hex[:12]
