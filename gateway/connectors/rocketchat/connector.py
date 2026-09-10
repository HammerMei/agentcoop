"""RocketChatConnector: full Connector implementation for Rocket.Chat.

Encapsulates ALL Rocket.Chat-specific knowledge:
  - DDP WebSocket subscription per room (subscribe_room / unsubscribe_room)
  - REST API calls for posting text and uploading files
  - Inbound message filtering (bot-self, allow-list, @mention, timestamp dedup)
  - Inbound message normalization (field extraction, attachment download)
  - Role resolution from allow-list config (RBAC lives here, not in core)
  - Per-room state tracking (room type, last processed timestamp, cache path)

The core library (SessionManager, MessageProcessor) interacts with this
connector only through the Connector ABC defined in gateway.core.connector.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from ...agents.response import AgentEvent, AgentResponse
from ...core.adapter_utils import ts_gt as _ts_gt
from ...core.adapter_utils import ts_ms_to_iso_local, weekday_abbrev
from ...core.adapter_utils import ts_to_float as _ts_to_float
from ...core.bot_identity import (
    BotIdentity,
    ConnectorIdentityError,
    canonical_origin,
)
from ...core.connector import (
    CapacityCheck,
    Connector,
    IncomingMessage,
    MessageHandler,
    Room,
    RoomCapacity,
    is_scheduled_message,
)
from ...core.dispatch import RoomAlreadyRoutedError
from ...core.paths import resolve_under
from ...core.pending_route import (
    STARTING_UP_NOTICE,
    PendingRoute,
    route_attempts,
)
from ...core.replay_window import ReplayWindow
from ...core.replay_window import just_before as _just_before
from ...core.sender_policy import sender_allowed
from ...core.tz_utils import local_iana_timezone as _server_local_timezone
from ...core.watcher_manager import RoomRef
from ...core.watcher_rule import RoomKind
from .agent_chain import TurnStore
from .config import RocketChatConfig
from .mentions import is_room_wide_mention
from .normalize import (
    FilterResult,
    extract_ts,
    filter_rc_message,
    normalize_rc_message,
)
from .outbound import send_media as _send_media
from .outbound import send_text as _send_text
from .policy import apply_thread_policy
from .rest import RocketChatREST, RoomNotFoundError, room_type_for
from .websocket import RCWebSocketClient

logger = logging.getLogger("coop.connectors.rocketchat")


class ClassificationUnavailable(Exception):
    """A room's kind could not be determined *this time* — retryable (§2.2).

    Raised instead of returning None because the routing transaction needs the
    two apart: None from `_room_ref_from_access` is a **final** decline (a room
    with no name, a direct room the server says has no counterpart — conditions
    a retry cannot change), while this is an **abort** — the classification was
    never made, so the routing decision was never made, and the message must
    stay eligible for redelivery rather than being committed as decided.
    """


# ---------------------------------------------------------------------------
# Per-room runtime state (internal to the connector)
# ---------------------------------------------------------------------------


# Sustained size of the per-room seen-id dedup window.  The eviction check
# uses strict-greater-than (len > MAXLEN) so the deque reaches MAXLEN + 1
# momentarily (one synchronous Python statement) before popleft brings it
# back to exactly MAXLEN.  The post-eviction cap is MAXLEN; the peak is
# MAXLEN + 1 but is never visible to other coroutines because no await
# occurs between append and popleft.
_SEEN_IDS_MAXLEN = 200


@dataclass
class _RoomSubscription(ReplayWindow):
    """Connector-level room state: platform subscription + shared dedup watermark.

    Owned by the connector, not by any individual watcher.
    """

    room: Room
    last_processed_ts: str | None = None
    # Where the outage started, captured before delivery is restored — see
    # `_snapshot_replay_boundaries`. `last_processed_ts` cannot answer that question by
    # the time replay runs, because the first live message through the new subscription
    # has already moved it past the gap.
    replay_boundary: str | None = None
    # How many claims have been made on `replay_boundary`. Bumped by `claim_boundary`
    # even when the value it writes is the one already there — which is the whole point.
    # A claim is a promise that someone still needs this window read; two claimants can
    # want the same timestamp, and the timestamp cannot tell them apart.
    boundary_claims: int = 0
    # Bumped whenever this account is confirmed to have left the room. A replay reads it
    # before its history fetch and again as it dispatches: the fetch and the dispatch loop
    # are long, and a live rejection arriving inside them is exactly the news that the
    # batch must not be delivered.
    membership_epoch: int = 0
    # Bounded FIFO set of recently-seen message _id values.  Used to deduplicate
    # messages that arrive on both the live DDP stream and the reconnect history
    # replay path.  deque provides O(1) append and fast len() checks while the
    # set provides O(1) membership tests.  Both are updated together so they
    # stay in sync; the deque is used only for eviction ordering.
    seen_ids: collections.deque = field(default_factory=lambda: collections.deque())
    seen_ids_set: set = field(default_factory=set)
    # Ids of the frames a creation episode handed back through the queue while a
    # live message may already have advanced the watermark past them. If such a
    # frame is then rejected as "already processed", it needs a replay and gets
    # a boundary claimed for it AT THAT MOMENT — not pre-emptively at creation.
    # The pre-emptive claim was never discharged when the frames were accepted
    # (the ordinary case), so shutdown persisted a boundary at the room's
    # creation and every restart replayed, and re-answered, everything since.
    promised_ids: set = field(default_factory=set)

    def left_the_room(self) -> None:
        """Record that this account is no longer a member. Three things, one call.

        A method for the same reason `remember` is one: the parts have to move together
        and one of them is easy to leave out. They were written separately at the two
        sites that learn this fact — the live `roomParticipant` gate and the REST
        membership check — and the epoch was added to one of them only, so a message in
        flight past the other one restored the watermark it had just cleared.

        * `replay_boundary` — a retained window is a promise to read it later, and after a
          removal nobody is entitled to.
        * `last_processed_ts` — the *fallback* boundary, frozen at the removal because the
          live gate remembers a rejected id without advancing it. Left set, a reconnect
          arriving before the first post-re-add message replays the whole time away.
          Empty means for a re-added room what it means for one seen for the first time.
        * `membership_epoch` — how work already in flight finds out. Clearing the marks
          cannot reach a replay or a dispatch holding its own copy of them.
        """
        self.discard_boundary()
        # `""`, not `None`, and the difference is what survives a restart. Both are falsy
        # everywhere this value is read, but the lifecycle's save step only copies the
        # connector's watermark when it has an opinion — `None` means "this room had no
        # activity in this run, keep what is on disk", which is right for a quiet room and
        # exactly wrong here. Empty says "cleared on purpose", so the stored record is
        # overwritten and a restart cannot hand the pre-removal mark back.
        self.last_processed_ts = ""
        self.membership_epoch += 1

    def forget(self, msg_id: str) -> None:
        """Undo `remember`, for a message that turned out not to be handled.

        The mirror of `remember`, and a method for the same reason: the deque and the set
        have to move together. This existed inline at the hand-back site and nowhere else,
        so every later "actually, nobody handled that one" had to rediscover it.
        """
        if not msg_id:
            return
        self.seen_ids_set.discard(msg_id)
        try:
            self.seen_ids.remove(msg_id)
        except ValueError:
            pass

    def remember(self, msg_id: str) -> None:
        """Record a message id as handled, evicting the oldest past the bound.

        A method because the deque and the set have to move together and the eviction
        is easy to leave out: this existed as two copies, and a third — added for the
        unrouted path — kept the two appends and dropped the eviction, so a busy room
        with no watcher grew both containers without limit. One place to state it.
        """
        if not msg_id:
            return
        self.seen_ids_set.add(msg_id)
        self.seen_ids.append(msg_id)
        if len(self.seen_ids) > _SEEN_IDS_MAXLEN:
            self.seen_ids_set.discard(self.seen_ids.popleft())


@dataclass
class _WatcherRoomContext:
    """Per-watcher subscription membership for a shared room.

    The connector tracks watcher IDs for refcounting — when the last watcher
    for a room is removed, the DDP subscription is torn down.  All per-watcher
    filesystem concerns (working directory, attachment workspace) live in the
    core layer (``WatcherLifecycle`` / ``AttachmentWorkspace``), not here.
    """

    watcher_id: str


# ---------------------------------------------------------------------------
# RocketChatConnector
# ---------------------------------------------------------------------------


class RocketChatConnector(Connector):
    """Connector for Rocket.Chat (REST + DDP/WebSocket).

    Usage::

        config = RocketChatConfig.from_gateway_config(gateway_cfg)
        connector = RocketChatConnector(config)

        connector.register_handler(my_handler)
        await connector.connect()

        room = await connector.resolve_room("general")
        await connector.subscribe_room(
            room,
            watcher_id="abc123",
            working_directory="/path/to/cwd",
        )

        # Required, and last. Subscribing to every room the account can see happens here
        # rather than in connect(), because a message arriving before the watchers exist
        # would be treated as a room nothing tracks — offered for creation while its real
        # watcher was still being built. With no router registered this is a no-op and
        # delivery stays per-room.
        await connector.start_inbound()

        # ... messages arrive, handler is called ...

        await connector.disconnect()
    """

    @property
    def delivery_mode(self):
        """Delivery goes through the RC DDP gateway broker."""
        return "gateway"

    _TEXT_CHUNK_LIMIT = 40_000

    # One routing episode's buffer — matches the transport's per-room queue
    # depth, since a room that cannot hold this many live frames has no better
    # claim to hold more while it is being created.
    _PENDING_BUFFER_DEPTH = 50
    # Bounded backoff for the two retryable resolution stages (§2.2): a
    # classification the network ate, and a creation that raised. Three retries,
    # ~3.5s worst case, holding one of the four routing workers — bounded on
    # purpose, because an unbounded retry would turn one dead REST endpoint
    # into a parked worker pool.
    _ROUTE_RETRY_DELAYS: tuple[float, ...] = (0.5, 1.0, 2.0)

    def __init__(self, config: RocketChatConfig) -> None:
        self._config = config
        self._rest = RocketChatREST(config.server_url)
        self._ws = RCWebSocketClient(
            config.server_url, config.username, config.password
        )
        self._handler: MessageHandler | None = None
        self._capacity_check: CapacityCheck | None = None
        self._rooms: dict[str, _RoomSubscription] = {}  # room_id -> subscription
        self._watcher_contexts: dict[
            str, list[_WatcherRoomContext]
        ] = {}  # room_id -> [watcher...]
        self._room_refcount: dict[str, int] = {}  # room_id -> subscriber count
        self._router = None
        self._membership_hook = None
        # Room-level membership-loss generation (Codex review of #121, round
        # 2). `membership_epoch` lives on the subscription OBJECT and dies
        # with it, so a delivery holding subscription A could not see a loss
        # that marked its replacement B before re-add installed C — three
        # transitions inside one flight, and the commit fence would have
        # redirected a pre-removal watermark into the new membership. This
        # counter belongs to the ROOM: every membership-loss site bumps it,
        # a delivery captures it at entry, and the fence refuses to redirect
        # across a bump, whichever object the loss happened to mark.
        self._room_membership_gen: dict[str, int] = {}
        # Per-room serialization of membership HOOK calls — see
        # `_on_membership_event`. Keyed and retained like the generation.
        self._membership_serial: dict[str, asyncio.Lock] = {}
        # Rooms with an open routing episode. The routing workers are a pool, so
        # several frames from one untracked room can be in flight at once — and offering a
        # room is slow (a DM needs `im.members` before it can even be classified), which
        # makes the overlap the normal case for a room that has just started talking rather
        # than a rare one. Two offers for one room are two watchers and two sessions for
        # it, which is what the single open episode prevents; the frames that arrive
        # during it wait in the episode's bounded buffer instead of being dropped (§2.7
        # step 3) and are drained in arrival order when it ends.
        self._pending_routes: dict[str, PendingRoute] = {}
        # Episodes opened from the *tracked* path (§2.5, the wake): the untracked path
        # runs its episode inline on a routing worker, but the tracked handler runs on
        # the room's own worker, and awaiting a creation there would stall the very
        # queue the drain is about to deliver into. Strong references, because a task
        # nothing holds is collected mid-flight; discarded on completion, cancelled and
        # gathered on disconnect — the same shape Mattermost's `_routing_tasks` has.
        self._routing_tasks: set[asyncio.Task] = set()
        # What `start_inbound` decided, once — **not** whether the stream is live now, and
        # nothing may read it as that. The stream can die under a healthy socket, and a copy
        # of its liveness would go on saying otherwise while a watcher added in that window
        # registered a callback for a room nobody had subscribed to (§6.1, invariant 2).
        # Ask `self._ws.stream_active` for the current fact; this only records that delivery
        # started out reaching every room the account can see, which is why `subscribe_room`
        # became local bookkeeping rather than what makes a room deliver (§5.2).
        self._subscribe_all = False
        # room_id → RoomKind for direct rooms. Never invalidated, and that is a verified
        # property rather than an optimisation: on 8.5.1 every route for adding a member to
        # a type-`d` room is refused on the room's *type*, and `im.create` returns a
        # **different room id** for a different member set — so a group DM is a separate
        # room, never a mutated 1:1, and this cache cannot go stale (§6.4).
        self._dm_kinds: dict[str, tuple[RoomKind, tuple[str, ...]]] = {}
        # Global attachment cache base: {cache_dir_global}/{connector_name}/{room_id}/
        # Namespaced by connector name to avoid collisions across multi-connector deployments.
        self._attachments_cache_base = (
            Path(config.attachments.cache_dir_global).expanduser() / config.name
        )
        # TurnStore for agent-to-agent loop protection; only allocated when agents are configured.
        self._turn_store: TurnStore | None = (
            TurnStore(ttl_seconds=config.agent_chain.ttl_seconds)
            if config.agent_chain.agent_usernames
            else None
        )

    # ── Lifecycle ────────────────────────────────────────────────────────────

    # Maximum messages fetched per room during a reconnect history replay.
    # Chosen to cover most realistic outage windows (e.g. 200 messages at
    # ~1 msg/s ≈ 3 minutes of traffic).  A warning is emitted when the
    # response fills the limit so operators know replay may be incomplete.
    _REPLAY_HISTORY_COUNT = 200

    async def connect(self) -> None:
        """Login via REST and establish the DDP WebSocket connection."""
        await self._rest.login(self._config.username, self._config.password)
        await self._ws.connect()
        self._ws.register_outage_callback(self._snapshot_replay_boundaries)
        self._ws.register_reconnect_callback(self._on_ws_reconnect)
        await self._ws.start()

        logger.info(
            "RocketChatConnector connected to %s as %s",
            self._config.server_url,
            self._config.username,
        )

    async def start_inbound(self) -> None:
        """Ask for every room the account can see — after the watchers exist.

        Deliberately not in `connect()`. Startup restores watchers between connecting and
        this call, and a message arriving in that window would take the *untracked* path:
        offered to the router, and either dropped or turned into a second attempt to create
        a watcher for a room whose real one was still being built. That is the same
        ordering `start_inbound` exists for on Mattermost — this connector had it backwards
        until review caught it.

        A server that refuses answers `nosub`, and the connector keeps its per-room
        subscriptions: a capability answer, not an error, so an older or differently
        configured server still runs the gateway. Only attempted when a router is
        registered — without one there is nothing to do with a message for an untracked
        room, and asking for them all would pay delivery cost for messages the connector
        immediately drops.
        """
        if self._router is None:
            return
        self._subscribe_all = await self._ws.subscribe_all()
        if self._subscribe_all:
            # Watchers are restored before this call, so every tracked room already has a
            # per-room subscription. Left in place they deliver every message a second
            # time — dedup hides the duplicate handler call, not the queue slot it takes.
            await self._ws.unsubscribe_rooms_keeping_callbacks()
        logger.info(
            "Rocket.Chat delivery: %s",
            "all rooms" if self._subscribe_all else "per room",
        )
        if not self._subscribe_all:
            # Post-cutover, rules are the only watcher shape and they DEPEND
            # on unsolicited delivery for discovery (Codex round 7). In
            # per-room fallback the blast radius is worse than "degraded":
            # rooms whose watchers started this boot keep working; an idle
            # record wakes only if the boot replay found messages already
            # waiting (the REST probe still works) — a message arriving
            # LATER reaches no subscription, so the room is deaf until the
            # next restart; and a genuinely new matching room or a
            # membership add is never discovered at all. Loud, not fatal:
            # the running rooms are real service worth keeping.
            logger.error(
                "Rocket.Chat: the server refused the all-rooms subscription, "
                "so delivery is per-room only. Watchers running now keep "
                "working, but idle rooms cannot wake on new messages until "
                "the next restart, and NEW rooms matching your rules will "
                "not be discovered. Rule-based discovery needs a server that "
                "allows streaming '__my_messages__'."
            )
        if self._membership_hook is not None:
            # Gated like the router that gates this method: no hook, no wire
            # cost. A refusal is degraded rather than fatal — joins are
            # discovered by first messages, removals by the reconciliation.
            await self._ws.subscribe_membership_events()

    async def disconnect(self) -> None:
        """Close the WebSocket and release HTTP client resources.

        The transport stops **first**: the room workers are what spawn wake
        episodes (§2.5), so cancelling `_routing_tasks` before they stop lets
        a worker spawn a newcomer during the gather — never cancelled, and
        `clear()` then drops its only strong reference while it runs against a
        dead transport. Stop the spawner, then harvest.
        """
        await self._ws.stop()
        for task in list(self._routing_tasks):
            task.cancel()
        if self._routing_tasks:
            await asyncio.gather(*self._routing_tasks, return_exceptions=True)
        self._routing_tasks.clear()
        await self._rest.close()
        logger.info("RocketChatConnector disconnected")

    async def _snapshot_replay_boundaries(self) -> None:
        """Record where each room's outage starts, before delivery is restored.

        Called by the transport at the point delivery is known lost and nothing is
        subscribed on the server yet. That is not the same as nothing being dispatched
        here: a reconnect does not clear `_room_queues` or `_room_workers` — only `stop()`
        does — so the workers keep draining frames that were already queued when the socket
        dropped, concurrently with this. The snapshot is safe anyway, because the loop
        below contains no await and a queued frame that advances the watermark really was
        delivered. **Do not add an await to it** on the strength of "nothing else is
        running"; an earlier version of this line said that, and it was not true.

        Replay used to read `last_processed_ts` when it ran, which is *after* every room
        has been resubscribed — and rooms are confirmed one by one, so a message arriving
        in the first room while the last is still subscribing was dispatched immediately
        and moved that room's watermark past the whole gap. History was then requested
        from the new position and the outage's messages were skipped for good. The window
        is small and the loss is silent, which is the combination that makes it worth a
        second field rather than a comment.

        Snapshotting the *boundary* rather than freezing `last_processed_ts` keeps live
        dedup working normally during the recovery: the watermark stays free to advance,
        and only replay reads the older mark.
        """
        for sub in self._rooms.values():
            # An unconsumed boundary is an outage nobody has read yet, and it is older than
            # this one — overwriting it would advance past a gap while claiming to record
            # where a gap starts, which is the bug this field exists to prevent. The older
            # mark covers both windows; dedup and `_REPLAY_HISTORY_COUNT` bound what that
            # costs.
            sub.claim_boundary(sub.last_processed_ts)

    async def _on_ws_reconnect(self) -> None:
        """Replay messages missed during a WebSocket outage.

        Called by ``RCWebSocketClient`` after every successful reconnect + room
        resubscription.  For each subscribed room that has a known watermark,
        we fetch up to ``_REPLAY_HISTORY_COUNT`` messages via the REST history
        API and re-inject them through the normal filter/normalize/dispatch
        pipeline.

        The ``_id``-based dedup window on ``_RoomSubscription`` ensures that any
        messages delivered on both the live DDP stream and this replay path are
        processed exactly once.  The ts-watermark handles older messages that
        fall below the last-processed timestamp.
        """
        logger.info("WebSocket reconnected — replaying missed messages for %d room(s)", len(self._rooms))
        for room_id in list(self._rooms):
            await self.replay_room_since(room_id)

    async def replay_room_since(
        self, room_id: str, after_ts: str | None = None
    ) -> None:
        """Replay one tracked room's outage window.

        The per-room half of the reconnect replay, and deliberately shared with
        the startup replay (§2.2): "cannot copy the reconnect path" forbids the
        *iteration source* — reconnect walks live subscriptions, startup walks
        persisted records — not the fetch-and-inject this method owns. The room
        must already be tracked; startup recreates the watcher first.

        Everything is re-injected through the normal filter/normalize/dispatch
        pipeline with the replay flags set, so the id window and the watermark
        dedup the frames that also arrived live.

        ``after_ts`` names the window explicitly. Without it the room's own
        marks are read, which is the reconnect case. With it — the startup and
        post-park cases — the caller is asking about a window it froze earlier,
        and **the boundary is not discharged**: that mark belongs to the room's
        own hand-back accounting, and a replay that read a different window has
        no claim to spend it. Leaving it costs one deduped re-read at the next
        reconnect; spending it wrongly costs a window nobody ever reads.
        """
        sub = self._rooms.get(room_id)
        if sub is None:
            return
        # An explicitly named window is not this room's boundary to spend.
        external_window = after_ts is not None
        # Snapshot the watermark NOW, before any await in this iteration.
        # The live DDP listen loop runs concurrently: awaiting get_room_history
        # for an earlier room yields the event loop and allows live messages for
        # subsequent rooms to advance their last_processed_ts.  If we read the
        # watermark inside the await we would use a newer ts that skips the
        # entire outage window for those rooms.
        # Captured so the completion below can tell whether anyone has claimed this
        # window since. A live message rejected for capacity while this batch is
        # dispatching claims the boundary, and closing it on this batch's success
        # loses that message: the next accepted message advances the watermark past it
        # and nothing points below it any more.
        #
        # The *claim count*, not the value. A hand-back inside a replay claims the
        # window the replay is already reading, so it writes back the same timestamp —
        # comparing values reports "unchanged" for precisely the case this guard is
        # for. That was the bug, and it read as correct for four review rounds.
        claims_at_entry = sub.boundary_claims

        # The outage boundary if one was captured, and the live watermark only as a
        # fallback for a replay that no outage callback preceded. Cleared where the
        # history is actually read, not here — a replay that declines below (membership
        # unknown, or the fetch failing) has not read the window, and dropping the mark
        # would close a gap nobody looked at. Those two failures are correlated with the
        # outage itself, so this is the likely path, not the exotic one.
        watermark = after_ts or sub.replay_boundary or sub.last_processed_ts
        if not watermark:
            logger.debug(
                "Room '%s': no watermark yet — skipping replay", sub.room.name
            )
            return

        # Membership is re-established before the outage is replayed, because the
        # outage is exactly when it can have changed and nobody was listening. The
        # live path is gated on `roomParticipant` (see `_on_raw_ddp_message`); replay
        # has no access object to read it from, so it asks. Without this, an account
        # removed from a public channel mid-outage still replays that channel — REST
        # history for a public channel does not require membership, so the fetch
        # succeeds and the agent answers in a room it was thrown out of.
        # Read before the lookup, and compared again around every await below. The
        # fetch is a REST round trip and the dispatch loop is up to 200 handler calls;
        # a live rejection arriving anywhere inside that is news this batch has to act
        # on, and it cannot see the cleared marks because it is holding a snapshot.
        epoch = sub.membership_epoch
        member = await self._rest.is_room_member(sub.room.id)
        if sub.membership_epoch != epoch:
            logger.warning(
                "Room '%s': this account was removed while its membership was being "
                "checked — abandoning the replay",
                sub.room.name,
            )
            return
        if member is False:
            # Removed, confirmed. Both marks are dropped, not just the boundary: an
            # account that is later re-added would otherwise replay from before its
            # removal, delivering everything said while it was not in the room. A
            # retained boundary is a promise to read that window later, and after a
            # removal nobody is entitled to read it.
            #
            # The watermark has to go with it, because the watermark *is* the fallback
            # boundary and it is frozen at the moment of removal — the live gate
            # remembers a rejected id without advancing it. Left in place, a reconnect
            # that arrives before the first post-re-add message would snapshot that
            # frozen value and replay the whole time away. Empty means what it means
            # for a room seen for the first time: no window, and ts-dedup off until
            # live traffic establishes one (`normalize.py`, step 4).
            sub.left_the_room()
            self._note_membership_loss(room_id)
            logger.warning(
                "Room '%s': this account is no longer a member — skipping replay and "
                "closing the outage window; a later re-add starts from that point, "
                "not from before the removal",
                sub.room.name,
            )
            # And RECLAIMED, like the live removal and the participant-false
            # paths: a removal while the socket was down raised no
            # subscription event, and the reconciliation examines only paused
            # and dropped records — so an active record, its processor,
            # session and jobs stayed indefinitely (Codex, PR #140; the same
            # gap Mattermost's replay had). Serialized per room like every
            # membership hook, so it cannot complete around a re-add.
            if self._membership_hook is not None:
                async def _removed(rid=room_id):
                    lock = self._membership_serial.setdefault(rid, asyncio.Lock())
                    async with lock:
                        try:
                            await self._membership_hook.removed(rid)
                        except Exception:
                            logger.exception(
                                "Membership removal (discovered on replay) for room "
                                "%s failed — the safety nets cover it", rid,
                            )
                task = asyncio.create_task(_removed())
                self._routing_tasks.add(task)
                task.add_done_callback(self._routing_tasks.discard)
            return
        if member is None:
            # Unknown is not removal. The lookup failing is correlated with the outage
            # itself, so this is the likely path, and the window stays open for the
            # next attempt to read.
            #
            # For an EXTERNAL window (the wake, the startup replay) "stays open"
            # is not automatic (Codex review of #121): the caller's mark lives in
            # the record, this subscription is fresh, and the triggering message
            # commits a newer watermark moments after this return — past the
            # whole unread interval, permanently. Whoever fails to replay owns
            # keeping the window reachable, so the failure claims it here.
            if external_window:
                sub.claim_boundary(after_ts)
            logger.warning(
                "Room '%s': membership could not be established — skipping replay; "
                "live delivery is unaffected and the next reconnect will ask again",
                sub.room.name,
            )
            return

        try:
            page = await self._rest.get_room_history_page(
                sub.room.id,
                sub.room.type,
                count=self._REPLAY_HISTORY_COUNT,
                after_ts=watermark,
            )
            raw_msgs = page.messages
        except Exception as e:
            # Same rule as the membership-unknown arm above: an external
            # window that was not read is claimed, so the next reconnect
            # recovers what the triggering message's commit would otherwise
            # seal away.
            if external_window:
                sub.claim_boundary(after_ts)
            logger.warning(
                "Room '%s': failed to fetch history for replay: %s",
                sub.room.name, e,
            )
            return

        if sub.membership_epoch != epoch:
            logger.warning(
                "Room '%s': this account was removed while its history was being "
                "fetched — discarding %d message(s) rather than dispatching them",
                sub.room.name, len(raw_msgs),
            )
            return

        if not raw_msgs:
            if page.was_full:
                # Not an empty window — a page the server filled entirely with system
                # events, because the count is applied before they are filtered out.
                # Every user message older than this page is still waiting behind it,
                # and reporting the outage as read would skip them silently. This is
                # the same bound the warning below describes, reached from the one
                # direction that produced no evidence at all.
                logger.warning(
                    "Room '%s': the newest %d history entries are all system events "
                    "— any user messages older than them cannot be reached in one "
                    "page and may be permanently missed",
                    sub.room.name, self._REPLAY_HISTORY_COUNT,
                )
            elif external_window:
                pass  # not this replay's mark to spend — see the docstring
            elif not sub.discharge_boundary(claims_at_entry):
                # A read that found nothing is still a read — but only of the window
                # this replay came in for. The membership check and the history fetch
                # above are both awaits, and a live hand-back inside either of them
                # claims a window this fetch has not looked below.
                logger.info(
                    "Room '%s': the outage window was claimed again while its history "
                    "was being fetched — leaving it open",
                    sub.room.name,
                )
            logger.debug(
                "Room '%s': no missed messages since %s",
                sub.room.name, watermark,
            )
            return

        if page.was_full:
            logger.warning(
                "Room '%s': replay fetched the maximum %d message(s) — "
                "the outage window may have produced more; some messages "
                "could be permanently lost",
                sub.room.name, self._REPLAY_HISTORY_COUNT,
            )
        else:
            logger.info(
                "Room '%s': replaying %d missed message(s) since %s",
                sub.room.name, len(raw_msgs), watermark,
            )

        # Warn when the live DDP subscription for this room is not healthy.
        # History replay still proceeds — the user gets missed messages —
        # but future live messages will be lost until the sub recovers.
        ws_status = self._ws.subscription_statuses.get(room_id, {})
        if ws_status.get("status") not in ("active", None, ""):
            logger.warning(
                "Room '%s': DDP subscription is in '%s' state — "
                "replaying history but future live messages will be lost "
                "until the subscription recovers",
                sub.room.name, ws_status.get("status"),
            )

        all_accepted = True
        for idx, doc in enumerate(raw_msgs):
            # Guard against concurrent unsubscribe_room: if the room was
            # removed while we were awaiting get_room_history, skip the
            # remaining docs rather than logging spurious "unknown room_id"
            # warnings for each one.
            if sub.membership_epoch != epoch:
                logger.warning(
                    "Room '%s': this account was removed mid-replay — dropping the "
                    "remaining %d message(s)",
                    sub.room.name, len(raw_msgs) - idx,
                )
                break
            if room_id not in self._rooms:
                logger.debug(
                    "Room '%s' was unsubscribed during replay — "
                    "skipping %d remaining message(s)",
                    sub.room.name,
                    len(raw_msgs) - idx,
                )
                break
            accepted = await self._on_raw_ddp_message(
                room_id, doc, is_replay=True, replay_after_ts=watermark
            )
            if sub.membership_epoch != epoch:
                # The removal landed *inside* that handler. The check at the top of the
                # loop cannot reach this: the last document has no next iteration, so
                # the `for`/`else` below would bless a revoked batch as complete.
                #
                # The marks are not re-cleared here. `left_the_room()` cleared them and
                # `_on_raw_ddp_message` refuses to commit a watermark once the epoch has
                # moved under it, so there is nothing left to repair — and repairing it
                # here as well would be the same rule in two places, which is how the
                # rule ends up applied in one.
                logger.warning(
                    "Room '%s': this account was removed while message %d of %d was "
                    "being handled — dropping the rest and re-closing the window",
                    sub.room.name, idx + 1, len(raw_msgs),
                )
                break
            if not accepted:
                all_accepted = False
        else:
            # Only once the batch has actually been *dispatched*. Fetching it is not
            # reading it: a shutdown or another disconnect cancelling this loop midway
            # leaves the tail unprocessed, and by then the restored live traffic has
            # moved `last_processed_ts` past it — so a boundary cleared at fetch time
            # would have the next recovery snapshot the newer mark and skip the tail
            # for good.
            #
            # `for`/`else`, so neither a cancellation nor the `break` above reaches it.
            # The cancellation case is the one this exists for; the `break` means the
            # room stopped being tracked mid-replay, and leaving a boundary on a
            # subscription nobody holds any more costs nothing.
            #
            # Completing the call is not the handler accepting: a full processor queue
            # hands the message back and forgets its id so a later replay can bring it
            # back. Spending the boundary on a batch that contains one of those removes
            # the only mark that could — the live watermark has moved past it by then.
            #
            # Two independent questions, and folding them into one condition
            # made the `else` speak for both: an externally-named window took
            # the hand-back arm and logged that a queue was full, on the most
            # ordinary path there is. Whose window this was decides whether the
            # mark may be spent; whether the batch was accepted decides whether
            # it *should* be.
            if not all_accepted:
                logger.warning(
                    "Room '%s': part of the replayed batch was handed back (queue "
                    "full) — keeping the outage window open for the next recovery",
                    sub.room.name,
                )
            elif not external_window and not sub.discharge_boundary(claims_at_entry):
                logger.info(
                    "Room '%s': the outage window was claimed again while this "
                    "batch was being dispatched — leaving it open",
                    sub.room.name,
                )

    # ── Inbound ──────────────────────────────────────────────────────────────

    def register_handler(self, handler: MessageHandler) -> None:
        self._handler = handler

    def register_capacity_check(self, check) -> None:
        self._capacity_check = check

    # ── Outbound ─────────────────────────────────────────────────────────────

    async def send_text(
        self,
        room_id: str,
        response: AgentResponse,
        thread_id: str | None = None,
    ) -> None:
        """Post an agent response to the room.

        Uses ``response.text`` as the message body.  When ``response.is_error``
        is True the text is delivered as-is (already contains an error prefix).
        ``thread_id`` is forwarded as RC's ``tmid`` so the reply lands in the
        correct thread.
        """
        await _send_text(
            self._rest,
            room_id,
            response.text,
            chunk_limit=self.text_chunk_limit,
            tmid=thread_id,
        )

    async def notify_agent_event(
        self,
        room_id: str,
        event: AgentEvent,
        thread_id: str | None = None,
    ) -> None:
        """Refresh the typing indicator on each intermediate agent event.

        RC's typing indicator auto-expires after ~10 seconds.  For long-running
        turns (tool calls, permission approvals, extended thinking) this means
        the indicator vanishes mid-turn, leaving the user with no feedback.

        Re-triggering it on every non-final AgentEvent keeps it alive for the
        full duration without posting any messages (no delete permissions needed,
        no placeholder race conditions).

        All errors are silently swallowed — a failed typing refresh must never
        abort an agent turn.
        """
        if event.kind == "final":
            return
        try:
            await self.notify_typing(room_id, True)
        except Exception as exc:
            logger.debug(
                "Failed to refresh typing indicator for room %s: %s", room_id, exc
            )

    async def send_media(self, room_id: str, file_path: str, caption: str = "") -> None:
        """Upload a local file to the room."""
        await _send_media(self._rest, room_id, file_path, caption)

    async def send_to_room(
        self,
        room: str,
        text: str,
        attachment_path: str | None = None,
    ) -> None:
        """Send a message (and optional attachment) to a room by name or ID.

        Overrides the base Connector implementation to use the RC REST client
        directly for efficient room resolution and delivery.
        """
        # Resolve room name to ID.
        # Only fall back to treating the input as a raw room ID when the room
        # was genuinely not found.  Broader failures (auth, network, API errors)
        # are re-raised so callers receive an accurate error.
        try:
            room_info = await self._rest.resolve_room(room)
            room_id = room_info["_id"]
        except RoomNotFoundError:
            # Input is likely already a room ID — use it directly.
            room_id = room

        if attachment_path:
            await self._rest.upload_file(room_id, attachment_path, caption=text)
        elif text:
            await self._rest.post_message(room_id, text)

    def supports_attachments(self) -> bool:
        return True

    async def download_attachment(self, ref: dict, dest_path: str) -> None:
        """Download a RC file attachment (identified by title_link) to dest_path."""
        title_link = ref.get("title_link", "")
        await self._rest.download_file(title_link, dest_path)

    # ── Room resolution ───────────────────────────────────────────────────────

    async def resolve_room(self, room_name: str) -> Room:
        """Resolve a human-readable room name to a Room object via REST."""
        info = await self._rest.resolve_room(room_name)
        return Room(
            id=info["_id"],
            name=info.get("name", room_name),
            type=info.get("type", "channel"),
        )

    async def room_ref_by_id(self, room_id: str) -> "RoomRef | None":
        """See `Connector.room_ref_by_id`.

        Rocket.Chat serves the account's own subscription document from the room
        id alone, and that document carries exactly what classification needs —
        the type letter and the name — so this is `_room_ref_from_sub_doc` over a
        fetched document rather than a pushed one. Reusing that classifier is the
        point: the DM branch (which asks `im.members`, because the letter `d`
        covers both DM kinds and the difference decides whether the mention gate
        applies, §6.4) stays one implementation.

        Membership comes free: Rocket.Chat drops the subscription when this
        account leaves or is removed, so a room the bot is no longer in has no
        document and answers `None`.
        """
        doc = await self._rest.get_subscription(room_id)
        if doc is None:
            return None
        return await self._room_ref_from_sub_doc(room_id, doc)

    # ── Per-room subscription ─────────────────────────────────────────────────

    def register_router(self, router) -> None:
        """Register the callback consulted for a room no watcher is tracking.

        Called as `router(room, trigger)`, where `trigger` is the document that prompted
        the offer. The room alone is not enough, and the reason is not hypothetical: a
        creation that starts a session with `history_handoff` fetches the room's recent
        messages with no upper bound, so it picks up the trigger — which this connector
        then dispatches as the live prompt, and the agent sees the same user message twice,
        once as history and once as the turn it is answering.

        The trigger is passed rather than a timestamp because the creation path decides
        what it needs from it: `fetch_room_history` takes a `before_ts`, and excluding by
        id is also open to it. A contract that made the correct behaviour impossible would
        be a defect in the contract, not in whoever implements it.

        Only ever consulted under subscribe-all: with per-room subscriptions a message for
        an unwatched room cannot arrive at all. Optional, so a deployment whose server
        refuses `__my_messages__` behaves exactly as before.
        """
        self._router = router
        self._ws.register_default_callback(self._on_unrouted_message)

    def register_membership_hook(self, hook) -> None:
        """Register the callbacks for the bot's own membership events (§2.7).

        The wire subscription is opened in `start_inbound`, gated on this hook
        existing — registration alone changes nothing, which is what keeps a
        static deployment's behaviour byte-identical.
        """
        self._membership_hook = hook
        self._ws.register_membership_callback(self._on_membership_event)

    async def _on_membership_event(self, action: str, doc: dict) -> None:
        """A subscriptions-changed event for this account (§2.7).

        The stream is already scoped to the bot's own user id (the event name
        is `<uid>/subscriptions-changed`), so unlike Mattermost there is no
        own-id filter to apply — every event here is about the bot's own
        membership. The doc is the server's subscription record (verified
        against `notifyOnSubscriptionChanged`'s callers: both `inserted` and
        `removed` carry the full document), so `rid`, `t` and `name` are on it.

        Failures are logged and dropped, never raised: an add is a supplement
        (the room's first message still creates its watcher), and a remove is
        re-discovered by the reconciliation.
        """
        if self._membership_hook is None:
            return
        rid = doc.get("rid") or ""
        if not rid:
            return
        if action == "removed":
            # Mark the room's CURRENT state before ANY await — this event's
            # task runs its first synchronous segment in arrival order, so
            # the delivery fences see the loss immediately, and the per-room
            # serialization below never delays the stamp (Codex review of
            # #121): a delivery in flight holds this object, and the
            # commit-redirection fence reads its epoch to tell a benign
            # watcher restart from a membership replacement (a pre-removal
            # frame must not commit into the re-added room's fresh state —
            # that watermark would point the next replay below the removal,
            # delivering the whole non-member interval).
            sub = self._rooms.get(rid)
            if sub is not None:
                sub.left_the_room()
            self._note_membership_loss(rid)
        # Serialized PER ROOM, in arrival order — the RC twin of Mattermost's
        # `_run_membership` lock (structural close): a room's add/remove
        # hooks run in the order the platform sent them, so a removal cannot
        # complete around an add still classifying, and a parked removal no
        # longer swallows the re-add behind it. Cross-room events still run
        # concurrently.
        lock = self._membership_serial.setdefault(rid, asyncio.Lock())
        async with lock:
            await self._run_membership_hooks(action, rid, doc)

    async def _run_membership_hooks(self, action: str, rid: str, doc: dict) -> None:
        try:
            if action == "removed":
                await self._membership_hook.removed(rid)
                return
            # The generation, captured before the classification awaits (Codex
            # round 4): a removal landing in that window bumps it, and the
            # recheck below is the last statement before the hook — so an add
            # outrun by its own removal never registers a record for a room
            # the bot has already left, while a genuine re-add captures the
            # bumped generation here and still passes.
            entry_gen = self._room_membership_gen.get(rid, 0)
            room = await self._room_ref_from_sub_doc(rid, doc)
            if room is not None:
                if self._room_membership_gen.get(rid, 0) != entry_gen:
                    logger.info(
                        "Room %s: membership was lost while the join was "
                        "being classified — not registering", rid,
                    )
                    return
                await self._membership_hook.added(room)
        except Exception:
            logger.exception(
                "Membership event (%s) for room %s failed — the safety nets "
                "cover it", action, rid,
            )

    async def _room_ref_from_sub_doc(
        self, rid: str, doc: dict
    ) -> "RoomRef | None":
        """Classify a joined room from its subscription document.

        The same shape as `_room_ref_from_access`, from the other source: the
        subscription's `t` is the room type letter and `name` the room name,
        and a direct room takes the member lookup because the letter `d`
        covers both DM kinds and the difference decides whether the mention
        gate applies (§6.4). `ClassificationUnavailable` propagates to the
        caller's catch — an add that cannot classify is dropped, not guessed.
        """
        t = doc.get("t") or ""
        if t == "d":
            identity = await self._direct_room_identity(rid)
            if identity is None:
                logger.warning(
                    "Joined direct room %s has no counterpart to classify by — "
                    "not registered", rid,
                )
                return None
            kind, participants = identity
            return RoomRef(id=rid, kind=kind, participants=participants)
        if t not in ("c", "p"):
            logger.debug(
                "Joined room %s has unsupported type %r — not registered", rid, t)
            return None
        name = doc.get("name") or ""
        if not name:
            logger.debug(
                "Joined room %s has no name and is not direct — not registered", rid)
            return None
        kind = RoomKind.GROUP if t == "p" else RoomKind.CHANNEL
        return RoomRef(id=rid, kind=kind, name=name)

    def _note_membership_loss(self, room_id: str) -> None:
        """Record a membership loss at ROOM level — see `_room_membership_gen`."""
        self._room_membership_gen[room_id] = (
            self._room_membership_gen.get(room_id, 0) + 1
        )

    async def membership_snapshot(self) -> set[str] | None:
        """See `Connector.membership_snapshot`. Read from the subscription
        records, same source of truth as `is_room_member` — hidden rooms are
        included, because hidden is a display choice, not a departure."""
        try:
            return await self._rest.get_subscription_room_ids()
        except Exception as e:
            logger.warning(
                "Could not read the subscription set — membership is unknown "
                "this pass: %s", e,
            )
            return None

    async def _on_unrouted_message(self, doc: dict, access: dict | None = None) -> None:
        """A message for a room this connector has no watcher for (§2.2).

        Three gates before the room is offered, in this order and for different reasons:

        1. **System messages.** A join notification is not a reason to create a watcher,
           and `t` is on the doc, so this costs nothing.
        2. **Own messages.** The bot's own posts arrive too.
        3. **Membership.** `roomParticipant` is server-computed per message, and under
           subscribe-all it is load-bearing in a way it never was before: the stream
           delivers **public channels the account can merely read**, not only the ones it
           belongs to. Without this gate the gateway would offer a watcher for every
           public channel on the server.

        A missing access object is treated as *not* a routing candidate rather than as a
        participant. That is the one place absence must not be read generously: the object
        is what carries the membership answer, so no object means no answer, and creating a
        watcher on no answer is how the agent ends up in a room nobody invited it to.
        """
        # `_router` is still tested for None: `register_router` is the only writer and
        # this method is installed as the transport's default callback by that same call,
        # so in principle it cannot run without one. It stays because the two are wired in
        # separate objects and a future caller could install the callback directly — an
        # early return is a cheaper insurance than the alternative, which is offering a
        # room to nothing.
        if self._router is None or doc.get("t"):
            return
        sender = doc.get("u", {}).get("username", "")
        if doc.get("u", {}).get("_id") == self._rest.user_id or (
            sender == self.agent_username
        ):
            # By id first, and by name only as a fallback for frames that carry no id.
            # A login whose canonical username differs in casing, or which is an alias,
            # made the name test miss the account's own posts — and with sender filtering
            # open, its own message in an untracked room then read as user activity and
            # created a watcher for it. In a DM the replies that followed would skip the
            # mention gate too, because a room typed `dm` does not have one.
            #
            # The same rule as `dm_members`: who someone is, not how their name is spelled.
            return
        if not access or not access.get("roomParticipant"):
            # An explicit `roomParticipant: False` for a room this connector
            # still tracks is the news the account was removed (#115): the
            # tracked path records it (`left_the_room()` clears the watermark
            # and bumps the epoch), and this path used to drop it — so a
            # later re-add replayed the whole non-member interval from the
            # stale watermark. Absence stays a plain return: "nobody said"
            # is not "not a participant".
            if access is not None and access.get("roomParticipant") is False:
                rid = doc.get("rid", "")
                sub = self._rooms.get(rid)
                if sub is not None:
                    sub.left_the_room()
                if rid:
                    self._note_membership_loss(rid)
                    # The untracked twin of the tracked branch's hook fire
                    # (Codex round 20, mirroring round 18): reachable when a
                    # FAILED record's room — never subscribed this boot —
                    # gets a subscribe-all frame with the server's own
                    # participant-false answer. A failed record is neither
                    # paused nor idle, so the reconciliation never sees it;
                    # without the hook, a later boot recreates the session
                    # across the membership boundary. Same per-room
                    # serialization as every membership event.
                    if self._membership_hook is not None:
                        async def _removed(room_id=rid):
                            lock = self._membership_serial.setdefault(
                                room_id, asyncio.Lock())
                            async with lock:
                                try:
                                    await self._membership_hook.removed(room_id)
                                except Exception:
                                    logger.exception(
                                        "Membership removal (untracked "
                                        "participant-false) for room %s failed "
                                        "— the safety nets cover it", room_id,
                                    )
                        task = asyncio.create_task(_removed())
                        self._routing_tasks.add(task)
                        task.add_done_callback(self._routing_tasks.discard)
            return
        if not sender_allowed(self._config, sender):
            return

        room_id = doc.get("rid", "")
        if not room_id:
            return
        await self._route_room(room_id, doc, access)

    def _room_is_served(self, room_id: str) -> bool:
        """A processor answers for this room now. Tracked is necessary, not sufficient.

        The idle drop keeps a room subscribed (§2.2), so `room_id in self._rooms` goes
        on answering True for a room whose next message has nowhere to go. Every
        deliver-or-route decision keys on this predicate rather than on tracked-ness,
        and the drain's branch is the load-bearing one: delivering an unserved room's
        frame puts it back on the tracked path, whose UNROUTED arm routes it back here
        — a hot loop with no retry delay anywhere in it, entered by every message to a
        room whose offer was declined.
        """
        if room_id not in self._rooms:
            return False
        if self._capacity_check is None:
            # No dispatcher wired: nothing can answer UNROUTED, so tracked is served.
            return True
        return self._capacity_check(room_id) is not RoomCapacity.UNROUTED

    async def _route_room(
        self,
        room_id: str,
        doc: dict,
        access: dict | None,
        *,
        resolved_room: "RoomRef | None" = None,
    ) -> None:
        """One routing episode for one room — the single entrance to creation (§2.7).

        Two callers, one funnel. `_on_unrouted_message` arrives from the routing
        workers with an untracked room and classifies it here; the tracked handler's
        UNROUTED arm arrives with `resolved_room` already in hand — the wake (§2.5),
        where the room was classified when it was first routed — and skips straight
        to the offer. Both share the pending buffer, the single open episode and the
        drain, because a second creation entrance is how a wake would skip exactly
        the guarantees the episode exists to make.
        """
        pending = self._pending_routes.get(room_id)
        if pending is not None:
            # An episode for this room is open. Checked *before* the served
            # check on purpose: the room may have become served an instant ago
            # with its buffer not yet drained, and delivering this frame
            # directly would put it ahead of every frame that arrived before
            # it. While an episode is open, the buffer is the room's order.
            #
            # **This check only covers the routing path, and that is a real
            # limit rather than a formality.** Once the room is tracked, later
            # frames go straight to its worker and never consult the buffer —
            # so a frame can overtake the trigger in the window between the
            # tracked-write (inside the router call, `start_watcher_in_room`
            # step 7) and the drain below.
            #
            # That window is **not** empty — a recreation replays the interval
            # its room owes before returning — so a live frame can land in it
            # and advance the watermark past the buffered trigger. The
            # watermark is a scalar, so that commit implicitly claims
            # everything below it and the trigger is then filtered as already
            # processed (§2.2, "commits within a room must be ordered").
            #
            # **Which is why the drain claims the window before handing the
            # frames over** — see the `finally` below. A filtered frame is then
            # a deferral rather than a loss: the claim is a promise that a
            # recovery comes back for it, the same promise the queue-full
            # hand-back makes with the same mechanism.
            verdict = pending.add(doc.get("_id", ""), (doc, access))
            if verdict == "duplicate":
                # §2.2 outcome 6: the reservation is not disturbed, the copy goes.
                logger.debug(
                    "Room %s: discarding a duplicate of a reserved message", room_id[:8])
            elif verdict == "full":
                # §2.2 outcome 5: the drop is audible in the room, once per
                # episode — the sender watched this message arrive.
                logger.warning(
                    "Room %s: pending buffer full — dropping a frame", room_id[:8])
                await self._post_starting_up_notice(pending, room_id)
            return
        if self._room_is_served(room_id):
            # Served now — so deliver it rather than offer it again. The frame reached
            # this path because the room was unserved when it was routed here, and the
            # watcher was created while it waited: the per-room callback that would have
            # taken it was registered after the routing decision was made, so nobody else
            # is going to deliver it. Returning here is how the message that arrived
            # during a creation used to be lost. Served, not tracked: an idle room is
            # tracked and its frame still has nowhere to go — delivering it would bounce
            # it off the tracked path's UNROUTED arm straight back here.
            #
            # **Onto the room's worker, not around it.** This runs on one of several
            # routing workers, so dispatching here directly puts concurrent deliveries
            # into a room whose whole guarantee is that one queue serialises them.
            # Serialising does not reorder: an older frame still lands behind a newer
            # one that arrived live, and the filter rejects it as already processed.
            # What it prevents is the two running at once, and a hand-back from the
            # older one claiming a boundary already past itself.
            self._ws.deliver_to_room(room_id, doc, access)
            return

        # First frame for this room: open the episode with the trigger buffered
        # as its first frame, so success drains trigger-first in arrival order.
        pending = PendingRoute(self._PENDING_BUFFER_DEPTH)
        pending.add(doc.get("_id", ""), (doc, access))
        self._pending_routes[room_id] = pending
        # Whether the routing decision was *completed* — a decline is an answer
        # ("no watcher": rule miss, pause, cap), a park or a cancellation is the
        # absence of one, and the drain below must treat them oppositely: a
        # declined frame is remembered so it cannot re-offer forever, a parked
        # frame's id must stay unknown or the recovery the park is promised —
        # the next wake's replay from the record watermark — dies at the dedup
        # check, silently.
        declined = False
        try:
            # Stage 1 — classify, unless the caller already holds the answer (the
            # wake, whose room was classified when it was first routed). Only
            # ClassificationUnavailable is retryable (§2.2 outcome 3: the routing
            # decision was never made); None is a final decline and anything else
            # is a bug that should surface.
            room = resolved_room
            if room is None:
                resolved: dict = {}

                async def classify() -> None:
                    resolved["room"] = await self._room_ref_from_access(room_id, access)

                if not await route_attempts(
                    classify, retry_on=ClassificationUnavailable,
                    delays=self._ROUTE_RETRY_DELAYS, logger=logger,
                    label=f"Classifying room {room_id[:8]}",
                ):
                    return  # parked; the finally drops the buffer
                room = resolved["room"]
            if room is None:
                declined = True  # final: no name to match, or no counterpart
                return

            # Stage 2 — offer. The router raising means a creation was started
            # and not carried out (§2.2 outcome 4) — retryable, because the
            # manager deliberately lets those propagate. A None-shaped outcome
            # (rule miss, pause, cap) does not raise and is final.
            async def offer() -> None:
                try:
                    await self._router(room, doc)
                except RoomAlreadyRoutedError:
                    # Final, not retryable: another watcher already serves this
                    # room, and three backoffs cannot change that — they would
                    # only hold a routing worker for ~3.5s per message to a room
                    # that will never be claimed.
                    logger.warning(
                        "Room %s is already served by another watcher — not "
                        "creating a second one", room_id[:8],
                    )
                    return

            # True when the offer ran to completion (its answer may still be
            # "no watcher" — that is the decline); False when every attempt
            # raised and the room parked. A cancellation propagates past this
            # line, leaving `declined` False, which is the same honest answer.
            declined = await route_attempts(
                offer, retry_on=Exception,
                delays=self._ROUTE_RETRY_DELAYS, logger=logger,
                label=f"Creating a watcher for room {room_id[:8]}",
            )
        finally:
            # The episode ends here, whatever happened, and the buffer has one
            # of two fates. Tracked: every frame — trigger first, then the ones
            # that arrived during the episode — goes onto the room's worker, so
            # every gate a tracked message passes applies to each of them, and
            # so does the queue's ordering. Not tracked: the decision was "no
            # watcher" or the room parked, and the frames go with it — stated
            # audibly, because a brand-new room has no watermark for any replay
            # to recover them from.
            ended = self._pending_routes.pop(room_id, None)
            frames = ended.drain() if ended is not None else []
            sub = self._rooms.get(room_id)
            if sub is not None and not self._room_is_served(room_id):
                # Tracked and still unserved. Served, not tracked, decides delivery
                # here for the same reason it does above: these frames' only
                # tracked-path outcome is the UNROUTED arm, which routes them
                # straight back into a new episode — a hot loop with no delay in
                # it, entered by every message to a declined room.
                #
                # What happens to the ids depends on WHICH way the offer ended,
                # because the two promises point in opposite directions:
                if declined:
                    # A completed decline — no rule claims the room, its record is
                    # paused. A configuration state that can persist indefinitely,
                    # so an id left unknown would have every reconnect re-fetch and
                    # re-offer a batch that can never be spent. Remembered, exactly
                    # as the old arm remembered the frames it dropped. The watermark
                    # is left where it is, so a user who resends is served normally
                    # once a watcher exists (§2.7).
                    for pending_doc, _ in frames:
                        sub.remember(pending_doc.get("_id", ""))
                    if frames:
                        logger.warning(
                            "Room %s: dropping %d buffered frame(s) — no watcher took "
                            "the room. A declined offer: no rule claims it, or its "
                            "record is paused.", room_id[:8], len(frames),
                        )
                elif frames:
                    # Parked (every attempt raised) or cancelled: the decision was
                    # never made, and this room HAS a record — the park's promised
                    # recovery is the next wake's replay from that record's
                    # watermark (§2.2). A remembered id would have that replay die
                    # at the dedup check, silently and permanently; unknown ids are
                    # exactly what lets it bring these frames back.
                    logger.warning(
                        "Room %s: %d buffered frame(s) not delivered — the offer "
                        "parked or was cancelled. Their ids stay unknown so the "
                        "next wake's replay recovers them.", room_id[:8], len(frames),
                    )
            elif sub is not None:
                # **Claim the window before handing the frames over.** The
                # watermark is a scalar high-water mark, so a live message
                # accepted while this episode was resolving has already
                # advanced it past these buffered frames — one timestamp
                # cannot say "committed the later one but not the earlier"
                # (§2.2, "commits within a room must be ordered"). The filter
                # would then reject each of them as already processed, and
                # nothing would point below the mark any more.
                #
                # This is the same promise the queue-full hand-back makes with
                # the same mechanism: a message below here was not read, so a
                # recovery must come back for it. Delivery below is still
                # attempted first — the claim is what makes the case where it
                # is filtered a *deferral* rather than a loss.
                # PROMISED, not claimed — same reasoning and the same measured
                # defect as Mattermost's creation path: a boundary claimed here
                # was never discharged when the frames were accepted, so shutdown
                # persisted a boundary at the room's creation and every restart
                # replayed, and re-answered, everything since. The ids are
                # remembered; the boundary is claimed only if the filter later
                # rejects one of them as already processed (`_on_raw_ddp_message`).
                sub.promised_ids.update(
                    d.get("_id", "") for d, _ in frames if d.get("_id"))
                for pending_doc, pending_access in frames:
                    self._ws.deliver_to_room(room_id, pending_doc, pending_access)
            elif frames:
                logger.info(
                    "Room %s: dropping %d buffered frame(s) — no watcher was created",
                    room_id[:8], len(frames),
                )

    async def _post_starting_up_notice(self, pending: PendingRoute, room_id: str) -> None:
        """Tell the room its messages are outrunning its setup — once per episode.

        Best-effort: the notice is owed, but a REST failure posting it must not
        take the routing worker down with it.
        """
        if pending.notice_posted:
            return
        pending.notice_posted = True
        try:
            await self.send_text(room_id, AgentResponse(text=STARTING_UP_NOTICE))
        except Exception:
            logger.debug("Could not post the starting-up notice", exc_info=True)

    async def probe_missed_since(self, room: Room, after_ts: str) -> bool:
        """See `Connector.probe_missed_since`. Raw docs, so the sender id is
        still on them — `fetch_room_history` has already reduced the bot's own
        posts to `username: "me"` by the time it returns."""
        page = await self._rest.get_room_history_page(
            room.id, room.type, self._REPLAY_HISTORY_COUNT, after_ts=after_ts
        )
        if page.was_full:
            # The page, and the *filter*: `count` is applied by the server and
            # system events are dropped afterwards, so a window whose newest
            # entries are all joins and topic changes filters down to nothing
            # while every user message in it waits behind that page. An empty
            # filtered list therefore has two meanings, and only `raw_count`
            # tells them apart — the reconnect replay reads it for the same
            # reason. Answering "gap" when they cannot be told apart costs one
            # recreation; answering "no gap" costs someone their reply.
            return True
        own_id = self._rest.user_id
        for doc in page.messages:
            if own_id and doc.get("u", {}).get("_id") == own_id:
                continue
            # Strictly after: `after_ts` is inclusive, so the boundary message —
            # the one that set this watermark — is in the page and is not a gap.
            if _ts_gt(extract_ts(doc), after_ts):
                return True
        return False

    def trigger_history_bound(self, trigger) -> str | None:
        """The trigger doc's `ts` as epoch milliseconds (§5.2).

        DDP carries it as `{"$date": ms}` or a bare numeric — both already the
        internal representation, so this extracts rather than converts. It used
        to convert to ISO, which then met an epoch-ms watermark in a numeric
        comparison that could not parse it.
        """
        if not isinstance(trigger, dict):
            return None
        ts = extract_ts(trigger)
        return ts if ts and _ts_to_float(ts) is not None else None

    def _room_ref_from_sub(self, sub: "_RoomSubscription") -> "RoomRef":
        """A RoomRef for a room this connector already tracks — the wake's classification.

        No REST call and no access object: the room was classified when it was first
        routed, and what that classification decided is in the tracked state — the
        room's type (a `RoomKind` value for every room the dynamic path subscribed) and,
        for a direct room, the permanently-cached kind and participants (§6.4). For a
        room with a record none of this is load-bearing anyway: `_recreate` reads the
        kind and participants from the record itself (§2.4). The fallback matters only
        on the recordless edge, where `_create` rule-matches this ref — and a room the
        dynamic path never touched carries a platform type no `RoomKind` names, which
        the channel fallback covers.
        """
        kind = RoomKind.CHANNEL
        try:
            kind = RoomKind(sub.room.type)
        except ValueError:
            pass
        participants: tuple[str, ...] = ()
        cached = self._dm_kinds.get(sub.room.id)
        if cached is not None:
            kind, participants = cached
        return RoomRef(
            id=sub.room.id,
            kind=kind,
            # A direct room's tracked name is its *description* (the counterpart, the
            # member list — §2.3), and `RoomRef.name` is the platform's own name,
            # empty for both DM kinds by contract.
            name="" if kind.is_direct else (sub.room.name or ""),
            participants=participants,
        )

    async def _room_ref_from_access(
        self, room_id: str, access: dict
    ) -> "RoomRef | None":
        """Build a `RoomRef` from the per-delivery access object.

        `roomName` is absent for direct rooms, which is why the kind is resolved before the
        name is used: a channel without a name is a frame we cannot describe, while a DM
        without one is normal.
        """
        room_type = room_type_for(access.get("roomType"))
        if room_type == "dm":
            # May raise ClassificationUnavailable — deliberately not caught here:
            # None from this method means *final* (a retry cannot change the
            # answer), and a network failure is the opposite of that. The caller
            # owns the retry (§2.2 outcome 3).
            identity = await self._direct_room_identity(room_id)
            if identity is None:
                # The server answered, and the answer names nobody. An unknown
                # classification is not a kind: answering `dm` here would create
                # a 1:1 watcher for what may be a group DM, and a room typed `dm`
                # skips the mention gate entirely (§6.4) — so the agent would
                # answer every message from everyone in that group.
                logger.warning(
                    "Direct room %s has no counterpart to classify by — not routing it",
                    room_id,
                )
                return None
            kind, participants = identity
            # The participants are not decoration: a direct room has no name, so they are
            # the only thing that identifies it to a human. Without them a 1:1 watcher is
            # labelled by a room-id digest and a group DM's *description* — what the agent
            # is told about where it lives — becomes its own opaque label (§2.3, §2.4).
            return RoomRef(id=room_id, kind=kind, participants=participants)

        name = access.get("roomName") or ""
        if not name:
            logger.debug("Room %s has no name and is not direct — not routable", room_id)
            return None
        kind = RoomKind.GROUP if room_type == "group" else RoomKind.CHANNEL
        return RoomRef(id=room_id, kind=kind, name=name)

    async def _direct_room_identity(
        self, room_id: str
    ) -> tuple[RoomKind, tuple[str, ...]] | None:
        """1:1 or group DM — the distinction Rocket.Chat does not make on the wire.

        Cached permanently, and that is verified rather than assumed: on 8.5.1 every route
        for adding a member to a type-`d` room is refused on the room's *type*, and
        `im.create` returns a **different room id** for a different member set. A group DM
        is therefore a separate room and never a mutated 1:1, so there is nothing for an
        invalidation path to catch (§6.4).

        The *kind* is what that immutability justifies caching. The participant names are a
        snapshot, and a renamed counterpart keeps the old name here for the life of the
        process. Left that way on purpose: the cache is what stops a DM that no rule claims
        from calling `im.members` on every message it ever receives, and nothing binds to
        the name — watchers are keyed `(connector, room_id)` and recreation reads the
        persisted config rather than re-deriving the label, so a stale name cannot split a
        watcher's identity. Recorded in §2.3.

        Neither outcome caches, and the two are deliberately different shapes
        (§2.2): a *failed* lookup raises `ClassificationUnavailable`, because the
        classification was never made and the message must stay redeliverable,
        while a lookup that succeeds and names nobody returns `None` — final,
        since a retry cannot invent a counterpart.

        Neither falls back to 1:1. This answer decides whether the mention gate
        applies at all, so a wrong one is not a slightly-off label but a watcher
        that replies to everyone in a group.
        """
        cached = self._dm_kinds.get(room_id)
        if cached is not None:
            return cached

        try:
            members = await self._rest.dm_members(room_id)
        except Exception as e:
            # Retryable, and typed so the routing path can tell it from a final
            # decline (§2.2 outcome 3): the classification was never made, so the
            # routing decision was never made either, and the message must stay
            # redeliverable. Not cached, for the same reason.
            raise ClassificationUnavailable(
                f"could not read members of direct room {room_id}: {e}"
            ) from e
        if not members:
            # Final, not retryable: the server answered and the answer names
            # nobody. A retry cannot invent a counterpart, and there is still no
            # safe kind to guess — so the room is declined, uncached, and the
            # next message asks again with fresh data.
            return None

        # `dm_members` has already excluded this account, by id rather than by the
        # spelling of its configured username — so everything here is a counterpart, and
        # one counterpart means a 1:1.
        others = tuple(members)
        kind = RoomKind.GROUP_DM if len(others) > 1 else RoomKind.DM
        identity = (kind, others)
        self._dm_kinds[room_id] = identity
        return identity

    async def subscribe_room(
        self,
        room: Room,
        watcher_id: str = "",
        working_directory: str = "",
    ) -> None:
        """Subscribe to DDP stream-room-messages for this room.

        Each call registers a new per-watcher context even if the DDP
        subscription already exists.  The DDP subscription is opened only once
        (on the first subscriber); subsequent callers increment the refcount and
        append their watcher context.

        Args:
            room              : Resolved Room to subscribe to.
            watcher_id        : Unique ID for the watcher; used as the
                                attachment cache subdirectory name.
            working_directory : Base path for attachment cache storage.
        """
        ctx = _WatcherRoomContext(
            watcher_id=watcher_id or room.id,
        )

        if room.id in self._rooms:
            contexts = self._watcher_contexts.setdefault(room.id, [])
            for i, existing in enumerate(contexts):
                if existing.watcher_id == ctx.watcher_id:
                    # The same watcher re-subscribing to a room it already holds — a
                    # wake after an idle drop, which keeps the room tracked (§2.2) and
                    # then runs the same start path a fresh creation does. Idempotent,
                    # like the dispatcher's claim ("replaces its own; refuses
                    # another's"): appending a second context and bumping the refcount
                    # here would leak one of each per idle/wake cycle, and the leaked
                    # refcount means the room's real unsubscribe never reaches zero.
                    contexts[i] = ctx
                    logger.debug(
                        "Room '%s' (id=%s) already subscribed by watcher '%s' — "
                        "replaced its context, refcount stays %d",
                        room.name, room.id, ctx.watcher_id,
                        self._room_refcount[room.id],
                    )
                    return
            self._room_refcount[room.id] += 1
            contexts.append(ctx)
            logger.debug(
                "Room '%s' (id=%s) already subscribed — added watcher '%s', refcount=%d",
                room.name,
                room.id,
                ctx.watcher_id,
                self._room_refcount[room.id],
            )
            return

        self._rooms[room.id] = _RoomSubscription(room=room)
        self._watcher_contexts[room.id] = [ctx]
        self._room_refcount[room.id] = 1

        try:
            if self._ws.stream_active:
                # Asked, not remembered: a restore that failed leaves the transport on
                # per-room subscriptions, and a connector-side flag saying otherwise would
                # register this room's callback without anyone having subscribed to it —
                # a watcher that receives nothing, silently.
                #
                # Local bookkeeping only (§5.2). The stream already delivers this room, so
                # a per-room `sub` would ask the server to send it twice — and the frames
                # are indistinguishable, so the second copy would be deduped by message id
                # rather than refused, wasting the round trip and the queue slot.
                #
                # The callback still has to be registered: the fan-out prefers a per-room
                # callback and falls back to the default one, and this room *is* tracked,
                # so it belongs on the tracked path rather than the routing path.
                self._ws.register_room_callback(room.id, self._make_ddp_callback(room.id))
            else:
                await self._ws.subscribe_room(room.id, self._make_ddp_callback(room.id))
        except Exception:
            # DDP subscription failed — roll back the connector-level state so there is no
            # dangling entry with a refcount of 1 and no live subscription. Only this call
            # can be here: the already-subscribed branch above returns before the transport
            # is touched, so a failure always belongs to the watcher that created the room.
            self._rooms.pop(room.id, None)
            self._watcher_contexts.pop(room.id, None)
            self._room_refcount.pop(room.id, None)
            # And the transport's state with it. A concurrent recovery can install its own
            # subscription for this room and release this one — which is what made the
            # await raise — and the transport's own rollback deliberately leaves a
            # successor it does not own alone. Without this the successor goes on
            # delivering into a room the connector has just forgotten: dropped as unknown,
            # and never offered to the router either, because a registered callback keeps
            # those frames off the routing path.
            try:
                await self._ws.unsubscribe_room(room.id)
            except Exception as cleanup_error:
                logger.warning(
                    "Room '%s': could not release the transport state of a failed "
                    "subscription: %s", room.name, cleanup_error,
                )
            raise

        logger.info(
            "Subscribed to room '%s' (id=%s, type=%s)",
            room.name,
            room.id,
            room.type,
        )

    async def unsubscribe_room(self, room_id: str, watcher_id: str = "") -> None:
        """Remove a watcher from a room; cancel the DDP subscription when the last watcher leaves.

        Args:
            room_id   : Platform room ID.
            watcher_id: ID of the departing watcher.  Its ``_WatcherRoomContext``
                        is removed regardless of the refcount; the DDP subscription
                        is cancelled only when the refcount reaches zero.
        """
        # Remove the specific watcher context and track whether it was found.
        # The refcount must only be decremented when an actual watcher is removed;
        # calling unsubscribe_room with a stale/unknown watcher_id must be a no-op.
        removed = False
        if room_id in self._watcher_contexts and watcher_id:
            before = self._watcher_contexts[room_id]
            after = [ctx for ctx in before if ctx.watcher_id != watcher_id]
            removed = len(after) < len(before)
            self._watcher_contexts[room_id] = after

        if room_id in self._room_refcount:
            if removed:
                self._room_refcount[room_id] -= 1
            if self._room_refcount[room_id] > 0:
                logger.debug(
                    "Room %s still has %d active watcher(s) — skipping DDP unsubscribe",
                    room_id,
                    self._room_refcount[room_id],
                )
                return
            del self._room_refcount[room_id]

        self._rooms.pop(room_id, None)
        self._watcher_contexts.pop(room_id, None)
        await self._ws.unsubscribe_room(room_id)
        logger.info("Unsubscribed from room %s", room_id)

    def update_last_processed_ts(self, room_id: str, ts: str) -> None:
        """Update the deduplication timestamp for a room after processing."""
        if room_id in self._rooms:
            self._rooms[room_id].last_processed_ts = ts

    def get_last_processed_ts(self, room_id: str) -> str | None:
        """The oldest OWED mark for the room — the claimed replay boundary
        when one is open and older, else the processed watermark (Codex
        round 26, the MM twin): shutdown persists this getter's answer, and
        a claimed-but-undischarged window must survive into the durable
        record or the next boot starts above the unprocessed tail."""
        sub = self._rooms.get(room_id)
        if sub is None:
            return None
        if sub.boundary_claims and sub.replay_boundary:
            from gateway.core.replay_window import ts_to_float

            lp = ts_to_float(sub.last_processed_ts or "")
            rb = ts_to_float(sub.replay_boundary)
            if lp is None or (rb is not None and rb < lp):
                return sub.replay_boundary
        return sub.last_processed_ts

    # ── Attachment cache ────────────────────────────────────────────────────────

    def attachment_cache_dir(self, room_id: str) -> str | None:
        """Return the global cache directory for a room's attachments.

        Contained via `resolve_under`, not raw joining: the id arrives from
        the server and is a path component here, and expiry `rmtree`s this
        directory — so an id spelling `..` (which survives a character-class
        sanitize, because dots are legal) must not be able to name a path
        outside the cache base. Sanitized first for the characters a filename
        cannot carry, exactly as Mattermost's `_cache_dir_for` does; a
        component `resolve_under` still refuses answers None, which the
        pipeline already reads as "no attachment caching for this room" — the
        fail-closed direction. Rocket.Chat ids are alphanumeric in practice,
        so real rooms resolve to the same directory they always did.
        """
        safe_room_id = re.sub(r"[^\w.\-]", "_", room_id)
        try:
            return str(resolve_under(self._attachments_cache_base, safe_room_id))
        except ValueError:
            logger.warning(
                "Refusing an attachment cache path for room id %r — it does "
                "not name a directory under the cache base", room_id,
            )
            return None

    @property
    def text_chunk_limit(self) -> int | None:
        """Maximum outbound text size before RC responses are split into chunks."""
        return self._TEXT_CHUNK_LIMIT

    # ── Security: server-injected prompt prefix ───────────────────────────────

    # Characters that are illegal in this protocol's delimiter grammar.
    # The prefix format uses '|' as a field separator and ']' as the closing
    # bracket.  A room name or username containing these could be crafted by a
    # malicious RC admin to inject fake role fields (e.g. "| role: owner") and
    # bypass RBAC enforcement in CLAUDE.md.  Stripping them here closes the gap.
    _PREFIX_UNSAFE_RE = re.compile(r"[\|\[\]\r\n]")

    def bot_identity(self) -> BotIdentity:
        """The id Rocket.Chat's own login response assigned, never the configured name.

        `username` is what an operator typed; the id is what the server says this
        connection is. Two connectors can spell one account two ways (case, an alias),
        and only the id makes them compare equal.

        No scope: Rocket.Chat has no team concept to keep two connectors on one account
        apart, so they duplicate every room, not merely DMs.

        The id arrives on the REST login response (`POST /api/v1/login`). The DDP login
        returns it too and discards it, so this deliberately reads the REST client
        rather than adding a second source that could disagree.
        """
        user_id = self._rest.user_id
        if not user_id:
            raise ConnectorIdentityError(
                f"Rocket.Chat connector for {self._config.server_url} cannot report its "
                f"own user id — the login response carried none, so this connector is "
                f"not authenticated. It cannot be checked against the other connectors "
                f"for a shared bot account, and starting it unchecked is what that "
                f"check exists to prevent."
            )
        return BotIdentity(
            platform="rocketchat",
            origin=canonical_origin(self._config.server_url),
            user_id=user_id,
        )

    @property
    def agent_username(self) -> str:
        """The bot's own RC username — the CANONICAL spelling once logged in.

        Never the configured one when the server has answered (#112): login
        is not spelling-exact, message frames carry the canonical form, and
        every identity comparison this property feeds (the mention gate, the
        history handoff's own-turn labels, the own-message fallback) fails
        silently under a lowercase or email login otherwise. Falls back to
        the config before login, exactly like Mattermost's."""
        return self._rest.bot_username or self._config.username

    @property
    def timezone(self) -> str:
        """IANA timezone from connector config, falling back to server local."""
        return self._config.timezone or _server_local_timezone()

    def _compute_to_field(self, msg: IncomingMessage) -> str:
        """Compute the compact ``to:`` routing field for the agent prompt prefix.

        Summarises who the message is addressed to among agents:

        - ``to: me``          — only this bot is @mentioned
        - ``to: @wavebro``    — one or more other agents mentioned, not this bot
        - ``to: me+@wavebro`` — this bot and other agents mentioned
        - ``to: @all``        — room-wide explicit mention such as ``@all``
        - ``to: me+@all+@wavebro`` — room-wide mention plus priority agents
        - ``to: *``           — no explicit agent mention in a channel (broadcast)

        DMs are treated as ``to: me`` because the user is speaking to the bot
        directly without needing an @mention.  All other broadcast messages
        (no agent mention in a channel) are ``to: *``.

        Usernames from ``msg.mentions`` are sanitized with ``_PREFIX_UNSAFE_RE``
        before use in the trusted header — the same treatment as room and sender.
        """
        # A scheduled job's message is addressed to the agent it was created
        # against — `to: me`, before any mention arithmetic. Derived from
        # mentions it rendered as `to: *`, and the routing rules for a broadcast
        # in a channel say "be conservative; if you decide not to respond, output
        # ONLY the termination token" — which is exactly what the agent did, every
        # minute, so a working job produced nothing in the room and looked broken.
        if is_scheduled_message(msg):
            return "to: me"
        # DMs are always addressed to this bot, regardless of mentions metadata.
        if msg.room.type == "dm":
            return "to: me"

        own = self.agent_username
        agent_names = set(self._config.agent_chain.agent_usernames)
        mentioned = set(msg.mentions)

        own_mentioned = own in mentioned
        all_mentioned = any(is_room_wide_mention(u) for u in mentioned)
        other_agents = [
            self._PREFIX_UNSAFE_RE.sub("_", u)
            for u in msg.mentions
            if u != own and not is_room_wide_mention(u) and u in agent_names
        ]

        if not own_mentioned and not all_mentioned and not other_agents:
            return "to: *"

        parts = []
        if own_mentioned:
            parts.append("me")
        if all_mentioned:
            parts.append("@all")
        parts.extend(f"@{u}" for u in other_agents)
        return "to: " + "+".join(parts)

    def supports_room_lookup(self) -> bool:
        """See `Connector.supports_room_lookup` — `room_ref_by_id` is a real lookup here."""
        return True

    def supports_unsolicited_inbound(self) -> bool:
        """Yes — `stream-room-messages` with the reserved id `__my_messages__`
        delivers messages for rooms this connector never per-room-subscribed to
        (design §2.6, verified in §6.1)."""
        return True

    def supports_history(self) -> bool:
        return True

    async def fetch_room_history(
        self,
        room: Room,
        count: int,
        before_ts: str | None = None,
        after_ts: str | None = None,
    ) -> list[dict]:
        """Fetch recent channel history as normalized, filtered message dicts.

        Calls the RC REST history endpoint, filters out messages from users
        not in the owner/guest allowlist or agent chain (same security boundary
        as live processing — anonymous users are excluded to prevent prompt
        injection), and returns a chronological list of normalized dicts.

        The bot's own prior messages are included with ``role: "agent"`` and
        ``username: "me"`` so the agent knows what it said before the session
        was reset.  Peer agents (``agent_chain.agent_usernames``) are also
        included as ``role: "agent"`` with their sanitized username, giving the
        agent full conversation context in multi-agent rooms.

        Args:
            room     : Resolved Room (provides id and type for the API call).
            count    : Maximum number of messages to retrieve.
            before_ts: ISO 8601 exclusive upper-bound timestamp for backward
                       pagination (maps to RC ``latest`` parameter).  Only
                       messages older than this timestamp are returned.
            after_ts : ISO 8601 inclusive lower-bound timestamp for forward
                       navigation (maps to RC ``oldest`` parameter).  Only
                       messages newer than or equal to this timestamp are
                       returned.
        """
        raw_msgs = await self._rest.get_room_history(
            room.id, room.type, count, before_ts=before_ts, after_ts=after_ts
        )
        bot_username = self.agent_username
        owners = set(self._config.owners)
        guests = set(self._config.guests)
        peer_agents = set(self._config.agent_chain.agent_usernames)
        safe_room = self._PREFIX_UNSAFE_RE.sub("_", room.name)
        tz = self.timezone

        result: list[dict] = []
        for m in raw_msgs:
            sender = m.get("u", {}).get("username", "")
            if not sender:
                continue

            if sender == bot_username:
                role = "agent"
                display_username = "me"
            elif sender in owners:
                role = "owner"
                display_username = self._PREFIX_UNSAFE_RE.sub("_", sender)
            elif sender in guests:
                role = "guest"
                display_username = self._PREFIX_UNSAFE_RE.sub("_", sender)
            elif sender in peer_agents:
                # Peer agents in the agent chain — include as role="agent" with
                # their actual username so the bot can distinguish peer turns
                # from its own prior turns (which use username="me").
                role = "agent"
                display_username = self._PREFIX_UNSAFE_RE.sub("_", sender)
            else:
                # Anonymous / unlisted sender — exclude for prompt injection safety.
                continue

            ts_raw = m.get("ts", {})
            ts_epoch_ms = ts_raw.get("$date") if isinstance(ts_raw, dict) else None
            ts_str = ts_ms_to_iso_local(str(ts_epoch_ms), tz) if ts_epoch_ms else None

            result.append({
                "ts": ts_str,
                "username": display_username,
                "role": role,
                "room_name": safe_room,
                "text": m.get("msg", ""),
            })
        return result

    def format_prompt_prefix(self, msg: IncomingMessage) -> str:
        """Return the trusted RC identity header for the agent prompt.

        This is server-controlled and parsed by CLAUDE.md as the security
        boundary for RBAC enforcement.  It must never be derived from
        user-controlled content.

        room.name and sender.username are sanitized to remove characters that
        could be used to inject fake delimiter fields (``|``, ``[``, ``]``,
        newlines).  role.value is an enum — not user-controlled.

        The ``ts`` field is the original RC message timestamp formatted in the
        connector's configured timezone (ISO 8601 with UTC offset) so agents
        can reason about local time without needing to know the offset.  It
        is kept machine-parseable — agents echo it back into
        ``fetch-history --before/--after`` — so the day of week is surfaced
        separately as a ``day:`` field instead of being embedded in ``ts``.

        The ``to:`` field summarises addressing among agents: ``me``, ``@other``,
        ``me+@other``, or ``*`` (broadcast).  See ``_compute_to_field``.
        """
        safe_room = self._PREFIX_UNSAFE_RE.sub("_", msg.room.name)
        safe_user = self._PREFIX_UNSAFE_RE.sub("_", msg.sender.username)
        ts = ts_ms_to_iso_local(msg.timestamp, self.timezone)
        day = weekday_abbrev(ts)
        day_part = f" | day: {day}" if day else ""
        ts_part = f" | ts: {ts}" if ts else ""
        to_part = f" | {self._compute_to_field(msg)}"
        return (
            f"[Rocket.Chat #{safe_room} | "
            f"from: {safe_user} | "
            f"role: {msg.role.value}{day_part}{ts_part}{to_part}]"
        )

    # ── Status notifications ──────────────────────────────────────────────────

    async def notify_typing(self, room_id: str, is_typing: bool) -> None:
        """Send a typing indicator via DDP WebSocket.

        RC 7.x replaced the old stream-notify-room/typing event with
        stream-notify-room/user-activity.  The event args are:
          typing=True:  [username, ["user-typing"], {}]
          typing=False: [username, [],              {}]
        """
        activity = ["user-typing"] if is_typing else []
        await self._ws.call_method(
            "stream-notify-room",
            [f"{room_id}/user-activity", self.agent_username, activity],
        )

    def on_agent_chain_drop(self, room_id: str, thread_id: str | None, sender: str) -> None:
        """Called when an agent chain LLM response was dropped (termination token detected).

        Resets the sender's turn counter so future messages from the same agent
        are not penalised by an artificially inflated count.
        """
        if self._turn_store is not None:
            self._turn_store.reset_sender(room_id, thread_id, sender)

    # ── Internal: DDP callback factory ───────────────────────────────────────

    def _make_ddp_callback(self, room_id: str):
        """Return the async callback that the WebSocket client calls for each DDP message."""

        async def on_raw_ddp_message(doc: dict, access: dict | None = None) -> None:
            await self._enqueue_room_doc(room_id, doc, access)

        return on_raw_ddp_message

    async def _enqueue_room_doc(
        self, room_id: str, doc: dict, access: dict | None = None
    ) -> None:
        """Forward one raw DDP doc into connector normalization/dispatch.

        Per-room buffering and ordering already live in ``RCWebSocketClient``.
        Keeping a second connector-owned room queue here duplicated backpressure
        and blurred the transport-vs-connector boundary.  The callback now
        relies on the transport layer's queue and proceeds directly to the
        connector-specific normalize/filter step.
        """
        await self._on_raw_ddp_message(room_id, doc, access=access)

    def _hand_back(self, doc: dict, result, reason: str, generation: int) -> bool:
        """Report a message as still owed, and give back what taking it consumed.

        Every path that answers False has already run the filter, and the filter is not
        read-only: it spends a turn of the sender's agent-chain budget before anything
        knows whether the message can be delivered. A retry re-enters it and spends
        another, so a message handed back often enough exhausts a budget it never used —
        after which the filter rejects it as complete, the replay reports success, and the
        window closes over a message nobody saw.

        One place, because there are two ways to hand a message back and they are the two
        that kept being fixed one at a time.
        """
        self._release_unused_turn(doc, result, generation, reason)
        return False

    def _release_unused_turn(self, doc: dict, result, generation: int, reason: str) -> None:
        """Give back the turn a message took, for a message that was never delivered.

        **Every** way of not delivering has to come through here, not only the two that
        hand a message back for retry. The filter charges a turn before anything knows the
        message can be delivered, so a path that drops it and keeps the charge spends the
        sender's budget on a message nobody saw — and once the budget is gone the filter
        rejects the *next* ones as complete.

        Codex found one such path (the live capacity preflight). Sweeping the rest of the
        function found three more: no watcher for the room, normalization failing, and the
        handler raising. A rule applied at two sites of six is the shape this PR has
        produced nine times, so the answer is one named call rather than a fifth patch.

        Releasing after the handler raised is deliberate. The handler may have enqueued
        before it threw, in which case the chain gets one extra turn; not releasing loses
        every later message from that sender instead. `release_turn`'s own docstring
        settles which way to be wrong: "marginally stricter" is not the safe direction.
        """
        if not result.agent_chain_token or self._turn_store is None:
            return
        remaining = self._turn_store.release_turn(
            doc.get("rid", ""), doc.get("tmid") or None, result.sender,
            result.agent_chain_token, generation,
        )
        logger.debug(
            "Released an agent-chain turn for %s (%s) — now at %d",
            result.sender, reason, remaining,
        )

    async def _on_raw_ddp_message(
        self,
        room_id: str,
        doc: dict,
        *,
        access: dict | None = None,
        is_replay: bool = False,
        replay_after_ts: str | None = None,
    ) -> bool:
        """Parse a raw RC DDP message doc, filter it, normalize it, fire handler.

        Returns **False only when this message is still owed a retry**. Two paths do that,
        and both are about a full processor: the handler handing the message back, which
        forgets its id so a later replay can bring it back, and a *replayed* message
        rejected by the capacity preflight, which never records the id at all. Every other
        outcome returns True, including messages filtered out on purpose: they are
        finished with, not pending. The distinction exists because the
        replay loop cannot otherwise tell "dispatched" from "handed back", and a boundary
        spent on a batch that was handed back loses it (§6.1, invariant 6).

        A handler *exception* also returns True, and deliberately: the id is left
        registered in that path, so a redo could not re-deliver the message anyway, and
        holding the window open for something no redo can produce would keep it open for
        good.

        `access` is the per-delivery object Rocket.Chat appends to the frame —
        `roomParticipant`, `roomType`, and `roomName` for rooms that have one. It is
        `None` on a per-room subscription and on the replay path, which reconstructs its
        docs from REST history, so nothing here may treat its absence as a negative
        answer: "not a participant" and "nobody said" are different, and only the first is
        a reason to drop a message.

        Filtering and deduplication are room-level (done once).
        Normalization and dispatch are per-watcher so each watcher gets its own
        attachment cache path and its own IncomingMessage instance.

        This is the boundary where all RC-specific field names disappear.
        After this method, only IncomingMessage objects exist in the codebase.

        Args:
            room_id        : Platform room ID.
            doc            : Raw RC DDP message document.
            is_replay      : True when called from the reconnect history replay
                             path.  Suppresses busy-notification REST posts to
                             avoid spamming the user with one per missed message.
            replay_after_ts: Snapshotted watermark passed by ``_on_ws_reconnect``
                             at the start of a replay loop.  When set, the
                             timestamp dedup filter uses this value instead of
                             the live ``sub.last_processed_ts``, preventing a
                             concurrent live message from advancing the watermark
                             past the replay window and silently dropping every
                             remaining replay message as "already processed".
        """
        if not self._handler:
            return True

        sub = self._rooms.get(room_id)
        if not sub:
            logger.warning("Received message for unknown room_id=%s", room_id)
            return True
        # `roomName` is the room's name as of THIS delivery; a rename shows up
        # here first. Named kinds only — a direct room has no `roomName`.
        current_name = (access or {}).get("roomName") or ""
        if (current_name and sub.room.type in ("channel", "group")
                and sub.room.name != current_name):
            logger.info("Room %s was renamed '%s' → '%s'", room_id, sub.room.name, current_name)
            sub.room = Room(id=sub.room.id, name=current_name, type=sub.room.type)

        # Which membership era this delivery belongs to, read before the first await. Every
        # path from here to the watermark commit is long — normalization, attachment
        # downloads, the handler itself — and a message that was delivered with
        # `roomParticipant: true` moments before a removal is entitled to nothing on the
        # other side of it. The commit at the end is the write that matters: it is what a
        # later re-add would replay from.
        entry_epoch = sub.membership_epoch
        # The ROOM-level loss generation, captured beside the object's epoch:
        # the epoch cannot survive the object, and a benign restart replacing
        # the object mid-flight would otherwise hide a loss that marked the
        # replacement (round 2). The commit fence compares this, not the
        # replacement's own epoch.
        entry_mgen = self._room_membership_gen.get(room_id, 0)

        # --- _id dedup (live + replay race guard) ---
        # A message can arrive on both the live DDP stream and the reconnect
        # history replay path within the same short window.  The ts-based
        # watermark alone cannot catch this because the watermark is advanced
        # *after* the handler returns, leaving a gap.  The seen_ids set provides
        # a fast O(1) check that eliminates exact duplicates regardless of
        # ordering.  The deque bounds memory to _SEEN_IDS_MAXLEN entries.
        # Membership, on the tracked path. `_on_unrouted_message` gates rooms the
        # connector does not know; this gates the ones it does, and they are not the same
        # question. Under subscribe-all the stream keeps delivering a channel after the
        # bot is removed from it — the account can still *read* it — so a watcher would go
        # on answering in a room it no longer belongs to, which is exactly the state
        # someone removing the bot was trying to produce.
        #
        # Only an explicit `False` counts. Absence means nobody said: a per-room
        # subscription carries no access object and neither does the replay path, and
        # treating those as "not a member" would drop every message on both.
        if access is not None and access.get("roomParticipant") is False:
            logger.info(
                "No longer a participant in room %s — dropping message", room_id)
            # Recorded, or the rejection lasts only until the next reconnect. History is
            # fetched from an unchanged watermark and re-injected with no access object,
            # and absence is deliberately not a negative — so the same post would come
            # back and be accepted, and the removed bot would answer in the room again.
            # The one thing the live path knows and the replay path cannot is this
            # rejection; remembering the id is how it is carried across.
            sub.remember(doc.get("_id", ""))
            # And the window closes here, exactly as it does on the REST membership check
            # (§6.1, invariant 6): this rejection is the same fact, learned earlier. Left
            # open, an account re-added before any reconnect would have the next one see
            # `True` and replay from the frozen pre-removal mark — the whole interval it
            # was not a member, none of which is in the 200-id window because none of it
            # was ever delivered.
            sub.left_the_room()
            self._note_membership_loss(room_id)
            # And the CORE learns it too (Codex round 18): this is the
            # server's own per-message answer — an authoritative removal
            # signal, not the offline inference #123 defers — and stopping at
            # the connector-local marks left the processor, record, session
            # and jobs alive until the idle TTL aged the room into the
            # dormant-only reconciliation. Scheduled through the same
            # per-room serialization every membership hook takes, so it
            # cannot complete around a concurrent re-add's registration.
            if self._membership_hook is not None:
                async def _removed(rid=room_id):
                    lock = self._membership_serial.setdefault(
                        rid, asyncio.Lock())
                    async with lock:
                        try:
                            await self._membership_hook.removed(rid)
                        except Exception:
                            logger.exception(
                                "Membership removal (participant-false) for "
                                "room %s failed — the safety nets cover it",
                                rid,
                            )
                task = asyncio.create_task(_removed())
                self._routing_tasks.add(task)
                task.add_done_callback(self._routing_tasks.discard)
            return True

        msg_id = doc.get("_id", "")
        if msg_id and msg_id in sub.seen_ids_set:
            sub.promised_ids.discard(msg_id)   # its live copy was processed
            logger.debug("Skipping already-seen message _id=%s in room %s", msg_id, room_id)
            return True

        # --- Filter (room-level, evaluated once) ---
        # During replay, use the watermark that was snapshotted at the START of
        # _on_ws_reconnect (passed as replay_after_ts) rather than the live
        # sub.last_processed_ts.  Without this, a concurrent live message that
        # arrives and advances sub.last_processed_ts mid-replay would cause every
        # remaining replay message (whose ts falls inside the outage window but
        # below the new live watermark) to be rejected as "already processed",
        # silently dropping messages the user sent during the outage.
        filter_ts = (
            replay_after_ts
            if (is_replay and replay_after_ts is not None)
            else sub.last_processed_ts
        )
        result: FilterResult = filter_rc_message(
            doc=doc,
            config=self._config,
            room_type=sub.room.type,
            last_processed_ts=filter_ts,
            turn_store=self._turn_store,
            bot_user_id=self._rest.user_id or "",
            bot_username=self.agent_username,
        )
        # Captured here, with no await between the filter's increment and this read, so
        # it names the count that increment belonged to. `_hand_back` compares it before
        # giving the turn back.
        turn_generation = (
            self._turn_store.generation(room_id, doc.get("tmid") or None, result.sender)
            if (result.agent_chain_token and self._turn_store is not None)
            else 0
        )
        if not result.accepted:
            if msg_id in sub.promised_ids:
                sub.promised_ids.discard(msg_id)
                if result.reason.startswith("already processed"):
                    # A handed-back frame that a live message overtook during
                    # the creation episode: never processed, now read as old.
                    # Forget its id and claim a boundary below it, as a
                    # hand-back does, so the next replay recovers it.
                    sub.forget(msg_id)
                    sub.claim_boundary(sub.last_processed_ts, _just_before(extract_ts(doc)))
                    logger.info(
                        "Room %s: handed-back frame %s fell below the watermark — "
                        "boundary claimed so the next replay recovers it",
                        room_id[:8], msg_id,
                    )
            logger.debug(
                "Message filtered: %s (sender=%s)", result.reason, result.sender
            )
            return True
        sub.promised_ids.discard(msg_id)   # accepted: the promise is kept

        logger.info(
            "Filter passed for message from %s in room '%s' — dispatching: %s",
            result.sender,
            sub.room.name,
            doc.get("msg", "")[:80],
        )

        # --- Preflight capacity check (two-phase inbound acceptance) ---
        # Short-circuit BEFORE expensive normalization + attachment download
        # when the core pipeline cannot accept the message anyway.  This avoids
        # wasted network, disk, and CPU under overload.
        #
        # Note: there is a TOCTOU gap — capacity may change between this check
        # and the later enqueue().  This is handled correctly: enqueue() returns
        # False and the watermark is not advanced.  The preflight is a best-effort
        # optimization, not a hard guarantee.
        capacity = self._capacity_check(room_id) if self._capacity_check else None
        if capacity is RoomCapacity.UNROUTED:
            # No processor serves this *tracked* room. The idle drop keeps a room
            # subscribed on purpose (§2.2) — the watermark and seen-id window are what
            # make recreation cheap — so an idle room's next message arrives here, on
            # the tracked path, and this arm is the wake (§2.5): the room is offered
            # back through the same episode funnel an untracked room goes through, so
            # the pending buffer, the single open episode and the recreation's replay
            # all apply. A direct recreate-on-UNROUTED shortcut would skip exactly
            # those, and they are the defects the routing transaction closed.
            #
            # NOT remembered, deliberately — the inverse of what this arm did when it
            # only dropped. The episode ends by delivering this frame back through
            # this handler, and a remembered id would be rejected at the dedup check
            # above; the declined episode's drain is what remembers it instead, so a
            # room nothing claims still converges. The watermark is left where it is
            # either way.
            #
            # The turn is released because the redelivery runs the filter — and its
            # charge — again. Spawned, not awaited: this runs on the room's own
            # worker, and the episode ends by delivering into that worker's queue.
            # Tasks run in creation order, and the episode reserves `_pending_routes`
            # before its first await, so a burst of frames buffers behind its first.
            if self._router is None:
                # No router registered — a static-only deployment. The old arm's
                # behaviour, verbatim: drop audibly, remember the id so reconnect
                # replays do not re-fetch a batch nothing can spend, watermark
                # untouched so a resend is served once a watcher exists.
                logger.warning(
                    "Message for room '%s' has no watcher — dropping without a "
                    "reply. A watcher that failed to start, or a room subscribed "
                    "with none configured.", sub.room.name,
                )
                sub.remember(msg_id)
                self._release_unused_turn(doc, result, turn_generation, "no watcher")
                return True
            logger.info(
                "Message for room '%s' has no processor — offering the room back "
                "to the router (wake).", sub.room.name,
            )
            # Claim a boundary below this frame, exactly as Mattermost's
            # `_keep_replayable` does on its wake arm. Without it a *replayed*
            # frame that lands here returns True, the batch reports itself
            # all-accepted, and `discharge_boundary` spends the outage window
            # on a frame that is only sitting in an episode buffer — so if the
            # episode then parks, nothing points below the watermark any more
            # and the frame is unrecoverable. The claim makes that discharge
            # refuse. Only a replay ever discharges: the served drain *claims*
            # too (the opposite operation), so on a happy wake this boundary
            # lingers until the next reconnect replay reads its own window and
            # spends it — dedup absorbs the refetch, the same lifecycle every
            # successful episode's drain claim already has. A claim never
            # narrows an open window.
            ts = extract_ts(doc)
            if ts:
                sub.claim_boundary(sub.last_processed_ts, _just_before(ts))
            self._release_unused_turn(doc, result, turn_generation, "waking the room")
            task = asyncio.create_task(
                self._route_room(
                    room_id, doc, access,
                    resolved_room=self._room_ref_from_sub(sub),
                )
            )
            self._routing_tasks.add(task)
            task.add_done_callback(self._routing_tasks.discard)
            return True
        if capacity is RoomCapacity.FULL:
            logger.warning(
                "Preflight rejected for message from %s in room '%s' — "
                "all processor queues full, skipping normalize + download",
                result.sender,
                sub.room.name,
            )
            if is_replay:
                # Nothing is remembered and nothing is announced, so the only way this
                # message can come back is a later replay — which means the id must stay
                # unknown and the batch must not report itself complete.
                #
                # The live path below remembers the id on purpose, because the sender is
                # told and can resend. That reasoning does not survive being copied here:
                # the busy notification is suppressed during replay (200 missed messages
                # against full queues would fire 200 REST posts), so nobody is told, and a
                # remembered id makes the next recovery skip it at the dedup check and
                # then clear the window as though the batch had been delivered. Silent,
                # and permanent.
                logger.info(
                    "Room '%s': a replayed message could not be accepted (queues full) — "
                    "keeping it replayable rather than recording it as handled",
                    sub.room.name,
                )
                return self._hand_back(
                    doc, result, "replay preflight, queues full", turn_generation)

            # Record _id BEFORE the first await so a concurrent delivery of the
            # same msg_id (e.g. live DDP racing a replay) hits the dedup check
            # at the top of this function rather than both calls appending to the
            # deque and creating a phantom duplicate entry.
            # Watermark is intentionally left unchanged so the user can retry by
            # resending — we only suppress automated replay re-delivery.
            sub.remember(msg_id)
            # Best-effort notification so the user knows their message was dropped.
            try:
                await self._handler_send_busy(room_id, doc)
            except Exception as exc:
                logger.debug(
                    "Best-effort busy notification failed for room '%s': %s",
                    room_id, exc
                )
            # Watermark NOT advanced — the sender has been told and can resend. The turn
            # is still given back: the message was not delivered, and a resend re-enters
            # the filter and is charged again.
            self._release_unused_turn(doc, result, turn_generation, "live preflight")
            return True

        # --- Optimistic seen_ids registration (TOCTOU guard) ---
        # Mark this message as "in-flight" before the first await so that any
        # concurrent delivery of the same _id (live DDP racing a replay, or two
        # replay calls overlapping) will hit the dedup check at the top and
        # return immediately.  We do this AFTER the filter / preflight checks so
        # that messages rejected by those paths are NOT permanently suppressed
        # from future deliveries (a filtered message may become eligible once
        # room state changes; a preflight-rejected message was already recorded
        # above so the duplicate add here is a no-op for that branch).
        #
        # Consequence: if normalize or handler *raises*, msg_id stays in seen_ids and the
        # message will NOT be replayed. That is intentional — a message that fails
        # normalization would likely fail again on replay, causing a poison-pill storm on
        # every reconnect.
        #
        # Only half of that reasoning survives inspection, and it is recorded here rather
        # than quietly acted on: `normalize_rc_message`'s first await is an attachment
        # *download*, so a failure there can be the network rather than the message. The
        # trade is a poison pill on every reconnect against losing a message whose
        # attachment fetch failed once. Left as it stands, deliberately, pending a decision
        # — not because the current answer is obviously right.
        #
        # **Cancellation is not covered by that reasoning at all** and is handled below:
        # being interrupted says nothing about the message, so the registration is undone.
        sub.remember(msg_id)

        # --- Normalize (once per message) ---
        # Attachment files are downloaded to a connector-global cache directory
        # namespaced by connector name and room ID.  All processors that subscribe
        # to this room reference the same local file paths — no per-watcher copies.
        # Routing to the room's processor is the core's responsibility;
        # the connector always calls the handler exactly once per accepted message.
        # Sanitize room_id before using it as a path component — room IDs are
        # server-controlled values and may contain path-traversal characters.
        # The downstream path-traversal check in normalize.py provides a second
        # layer of defense, but early sanitization is cleaner.
        safe_room_id = re.sub(r"[^\w.\-]", "_", room_id)
        cache_dir = self._attachments_cache_base / safe_room_id
        try:
            msg: IncomingMessage = await normalize_rc_message(
                doc=doc,
                room=sub.room,
                sender_username=result.sender,
                msg_ts=result.msg_ts,
                config=self._config,
                rest=self._rest,
                cache_dir=cache_dir,
                is_agent_chain=result.is_agent_chain,
                agent_chain_turn=result.agent_chain_turn,
                agent_chain_max_turns=result.agent_chain_max_turns,
            )
        except asyncio.CancelledError:
            # Not a verdict on the message — this delivery was interrupted. The
            # optimistic registration above is undone so a replay can bring it back;
            # left in place, the next replay skips it at the dedup check, counts the skip
            # as handled, and closes the outage window over a message nobody ever saw.
            #
            # `CancelledError` is a `BaseException`, so the arm below never covered this,
            # and a recovery cancelling the one it displaces is an ordinary event here.
            sub.forget(msg_id)
            self._release_unused_turn(doc, result, turn_generation, "cancelled")
            raise
        except Exception as e:
            logger.error("Failed to normalize message: %s", e)
            self._release_unused_turn(doc, result, turn_generation, "normalize failed")
            return True

        # --- Apply thread + permission-thread policy (extracted to policy.py) ---
        apply_thread_policy(msg, self._config)

        # --- Hand off to core (the dispatcher routes to the room's processor) ---
        try:
            accepted = await self._handler(msg)
        except asyncio.CancelledError:
            # Same reason as the normalize arm above: an interruption is not a decision
            # about the message.
            sub.forget(msg_id)
            self._release_unused_turn(doc, result, turn_generation, "cancelled")
            raise
        except Exception as e:
            logger.error("Handler error for message from %s: %s", result.sender, e)
            self._release_unused_turn(doc, result, turn_generation, "handler raised")
            return True

        if not accepted:
            logger.warning(
                "Message from %s was dropped (queue full)",
                result.sender,
            )
            # Remove msg_id from seen_ids so the reconnect replay path can
            # re-deliver this message.  The optimistic registration above added
            # it before the handler call to guard against concurrent live+replay
            # races, but a queue-full drop should be retryable: the queue may
            # have drained by the time the next reconnect fires.
            # We discard from the set and remove the single deque entry to keep
            # them in sync; the O(N) deque.remove is acceptable at N ≤ 200.
            sub.forget(msg_id)
            # Pin the outage window at "before this message", because forgetting the id is
            # not enough on its own to bring it back. The watermark has not advanced past
            # it — that only happens on acceptance — but the *next* accepted message moves
            # it past for good, and a replay copy of this same message may already have
            # reported the batch complete on the strength of the id this branch has just
            # removed. Whoever drops a message owns keeping it reachable.
            #
            # `claim_boundary` takes the *oldest* candidate, so an older window already
            # open is not narrowed to this one — and neither is this message overtaken by a
            # watermark that a concurrent worker has already pushed above it. This message's
            # own timestamp is offered last because both other marks are empty for the first
            # delivery into a new room and for the first after a membership reset. Leaving no boundary there loses the message outright:
            # replay skips a room whose window is falsy, and the next accepted message
            # advances the cursor past this one for good.
            #
            # One millisecond below this message, not its own timestamp. The fetch would
            # include it either way — `oldest` is inclusive — but the replay hands the
            # same boundary to the filter as `last_processed_ts`, and the filter rejects
            # `msg_ts <= last_ts` as already processed. A boundary equal to the message
            # therefore fetches it and then throws it away, which is a fix that changes
            # nothing: the previous version of this line did exactly that.
            #
            # Rocket.Chat timestamps are epoch milliseconds, so "just below" is a real
            # value rather than an approximation.
            # Not if the account left the room while this handler was running. The
            # watermark commit below already refuses that, and this is the same rule for
            # the other mark a delivery leaves behind: `left_the_room()` closed the window
            # deliberately, and reopening it here points a later replay below a removal.
            # If the account is re-added before that replay, membership answers True and
            # the whole non-member interval is delivered — which is the one thing the
            # rejected-id window cannot prevent, because those messages were never seen
            # live at all.
            #
            # Nothing is owed by not claiming: this message belongs to a membership the
            # account no longer has, so leaving it unreachable is the outcome, not a loss.
            #
            # And not to a DETACHED object (#115): a watcher stop→start while this
            # delivery was in flight popped `sub` and installed a fresh one, so a
            # boundary written to `sub` is a note left in an object nothing reads
            # again — the hand-back is never recovered. The claim goes to the room's
            # LIVE subscription when one exists; when none does, the room is gone
            # and there is nowhere for a replay to recover into anyway.
            live = self._rooms.get(room_id)
            if live is sub:
                if sub.membership_epoch == entry_epoch:
                    sub.claim_boundary(sub.last_processed_ts, _just_before(result.msg_ts))
                else:
                    logger.warning(
                        "Room %s: not reopening the outage window for a message that was in "
                        "flight when this account was removed", room_id,
                    )
            elif (live is not None
                  and self._room_membership_gen.get(room_id, 0) == entry_mgen):
                logger.warning(
                    "Room %s: a hand-back outlived its subscription (watcher "
                    "restarted mid-delivery) — claiming the outage window on the "
                    "live one instead", room_id,
                )
                live.claim_boundary(live.last_processed_ts, _just_before(result.msg_ts))
            elif live is not None:
                # A membership loss happened somewhere in this delivery's
                # flight — whichever object it marked (the room generation
                # survives replacements, round 2): the live object belongs to
                # a NEW membership, and a pre-removal frame has no claim on
                # its window.
                logger.warning(
                    "Room %s: not claiming a window for a hand-back that "
                    "crossed a membership removal", room_id,
                )
            # The one outcome that leaves this message pending: its id was just forgotten
            # precisely so a later replay can bring it back, and a boundary spent on a
            # batch containing it would remove the only thing that could.
            return self._hand_back(doc, result, "handler queue full", turn_generation)

        # --- Advance dedup watermark AFTER confirmed acceptance ---
        # Update the watermark only once the handler has confirmed the message
        # was accepted (enqueued).  Advancing it before the handler call would
        # silently lose messages that are dropped due to queue-full conditions:
        # the RC replay mechanism skips messages whose ts <= last_processed_ts,
        # so a message dropped before it reaches a processor would never be
        # re-delivered on reconnect.
        #
        # Reconnect-duplicate risk: the window between handler returning True
        # and this assignment is a single Python statement — effectively zero.
        # This is a much smaller race than waiting for the entire handler
        # duration, so the previous "advance before handler" behaviour did not
        # meaningfully reduce reconnect duplication in practice.
        # The commit target may no longer be `sub` (#115): a watcher stop→start
        # while the handler ran popped it and installed a fresh subscription, so
        # a watermark and dedup id written to `sub` vanish with it — the next
        # reconnect replay re-delivers this very message to the new processor.
        # The commit follows the room, not the object.
        live = self._rooms.get(room_id)
        if live is sub:
            if sub.membership_epoch != entry_epoch:
                # The account left this room while this message was in flight.
                # Committing now would restore the very watermark the removal
                # cleared, and a later re-add would replay from before the
                # removal — delivering the interval the account was not a
                # member for. The message itself is already handled; only the
                # mark it would leave behind is refused.
                logger.warning(
                    "Room %s: discarding the watermark of a message that was in flight when "
                    "this account left", room_id,
                )
                return True
            target = sub
        elif (live is not None
              and self._room_membership_gen.get(room_id, 0) == entry_mgen):
            logger.warning(
                "Room %s: a delivery outlived its subscription (watcher restarted "
                "mid-delivery) — committing its watermark and dedup id to the live "
                "one", room_id,
            )
            live.remember(msg_id)
            target = live
        elif live is not None:
            # A membership loss happened somewhere in this delivery's flight —
            # whichever object it marked, because the room generation survives
            # replacements (round 2): the live state is a re-add's fresh
            # membership, and committing a pre-removal watermark into it would
            # point the next replay below the removal — delivering the whole
            # non-member interval, which the epoch machinery exists to prevent.
            logger.warning(
                "Room %s: discarding the watermark of a delivery that crossed "
                "a membership removal", room_id,
            )
            return True
        else:
            # The room is gone entirely; there is nothing to commit into.
            logger.warning(
                "Room %s: discarding the watermark of a message whose room was "
                "reclaimed mid-delivery", room_id,
            )
            return True

        # Never backwards. Restored live delivery can accept a newer message while this
        # replay is still awaiting normalization or a handler for an older fetched one,
        # and an unconditional assignment then rewinds the cursor. In memory the seen-id
        # window hides that; across a save and a restart it does not, and history after
        # the regressed cursor is dispatched a second time.
        if _ts_gt(result.msg_ts, target.last_processed_ts or ""):
            target.last_processed_ts = result.msg_ts
        # msg_id was already added to seen_ids_set by the optimistic registration
        # block above (before the first await; re-added to the live subscription
        # above when the original was replaced mid-delivery).
        return True

    async def _handler_send_busy(self, room_id: str, doc: dict) -> None:
        """Best-effort 'server busy' notification to the user when preflight rejects."""
        thread_id = doc.get("tmid") or None
        await self._rest.post_message(
            room_id,
            "⚠️ Server busy — your message was dropped. Please retry.",
            tmid=thread_id,
        )
