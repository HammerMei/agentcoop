"""MattermostConnector: full Connector implementation for Mattermost.

Encapsulates ALL Mattermost-specific knowledge:
  - REST API calls (auth, posting, file upload/download, room/team/user resolution)
  - WebSocket event stream (no per-channel subscribe — see websocket.py)
  - Inbound message filtering (bot-self, allow-list, @mention, timestamp dedup)
  - Inbound message normalization (field extraction, attachment download)
  - Role resolution from allow-list config (RBAC lives here, not in core)
  - Per-channel state tracking (last processed timestamp, dedup window)

The core library (SessionManager, MessageProcessor) interacts with this
connector only through the Connector ABC defined in gateway.core.connector.

Structural note vs RocketChatConnector: Mattermost's WebSocket streams
`posted` events for every channel the bot is a member of, with no
per-channel subscribe/unsubscribe wire protocol. subscribe_room /
unsubscribe_room here are therefore local bookkeeping only (which channels
does the dispatcher currently care about) — see websocket.py's module
docstring for the confirmed payload-shape details behind this design.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from ...agents.response import AgentEvent, AgentResponse
from ...core.adapter_utils import ts_gt, ts_ms_to_iso_local, weekday_abbrev
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
from ...core.replay_window import ReplayWindow, just_before
from ...core.tz_utils import local_iana_timezone as _server_local_timezone
from ...core.watcher_manager import RoomRef
from ...core.watcher_rule import RoomKind
from .agent_chain import TurnStore
from .config import MattermostConfig
from .mentions import is_room_wide_mention
from .normalize import (
    FilterResult,
    bare_handle,
    filter_mm_message,
    normalize_mm_message,
    sender_allowed,
    text_mentions_bot,
)
from .outbound import send_media as _send_media
from .outbound import send_text as _send_text
from .policy import apply_thread_policy
from .rest import MattermostREST, RoomNotFoundError
from .websocket import MattermostWebSocketClient

logger = logging.getLogger("coop.connectors.mattermost")


# ---------------------------------------------------------------------------
# Per-channel runtime state (internal to the connector)
# ---------------------------------------------------------------------------


# Same rationale as RC's _SEEN_IDS_MAXLEN — bounds the live+replay dedup window.
_SEEN_IDS_MAXLEN = 200


@dataclass
class _ChannelState(ReplayWindow):
    """Connector-level channel state: dedup watermark + local subscriber tracking.

    Unlike RC's _RoomSubscription, there is no wire-protocol subscription to
    track — the WebSocket already streams every channel the bot belongs to.
    This just tracks which channels the dispatcher currently cares about and
    the per-channel dedup watermark/seen-id window.
    """

    room: Room
    last_processed_ts: str | None = None
    # Where to resume from when the watermark alone would skip something. Declared here
    # rather than inherited so it sits beside the watermark it qualifies — see
    # `core.replay_window`, which owns the rule for who may clear it.
    #
    # Mattermost gets the *hand-back* half of that module and not the outage half: there is
    # no per-channel subscribe handshake, so one connection resumes every channel at the
    # same instant and the staggered-resubscribe race Rocket.Chat captures a window for
    # cannot happen here. This mark exists only because ACG itself refuses messages when
    # its queues are full.
    replay_boundary: str | None = None
    boundary_claims: int = 0
    # Set by the membership-removal hook while this object is still the
    # channel's current state. RC carries a membership_epoch for the same
    # job; Mattermost has no epoch machinery, and the commit-redirection
    # fence needs exactly one bit: did a membership loss happen while a
    # delivery holding this object was in flight.
    membership_lost: bool = False
    seen_ids: collections.deque = field(default_factory=lambda: collections.deque())
    seen_ids_set: set = field(default_factory=set)
    watcher_ids: set = field(default_factory=set)
    # Ids of the frames a creation episode handed back through the queue while a
    # live message may already have advanced the watermark past them. If such a
    # frame is then rejected as "already processed", it needs a replay and gets
    # a boundary claimed for it AT THAT MOMENT — not pre-emptively at creation.
    # The pre-emptive claim was never discharged when the frames were accepted
    # (the ordinary case), so shutdown persisted a boundary at the room's
    # creation and every restart replayed, and re-answered, everything since.
    promised_ids: set = field(default_factory=set)


# Mattermost channel type → the gateway's room kind. `room_type_for` in rest.py maps the
# same four letters to the *string* room type an `IncomingMessage` carries; this maps them
# to the enum routing uses. Two mappings because they answer to two different consumers,
# and both are stated once.
_ROOM_KINDS = {
    "O": RoomKind.CHANNEL,
    "P": RoomKind.GROUP,
    "D": RoomKind.DM,
    "G": RoomKind.GROUP_DM,
}


class MattermostConnector(Connector):
    """Connector for Mattermost (REST v4 + WebSocket).

    Usage::

        config = MattermostConfig.from_connector_config(cc)
        connector = MattermostConnector(config)

        connector.register_handler(my_handler)
        await connector.connect()

        room = await connector.resolve_room("town-square")
        await connector.subscribe_room(room, watcher_id="abc123")

        # Required, and last: the socket is open from connect() but unread until
        # here, so events arriving during setup are buffered rather than dropped for
        # a channel this connector does not know yet. Subscribe first, then this.
        await connector.start_inbound()

        # ... messages arrive, handler is called ...

        await connector.disconnect()
    """

    @property
    def delivery_mode(self):
        """Persistent WebSocket-driven delivery, same transport model as RC."""
        return "gateway"

    # Mattermost's default per-post character limit (ServiceSettings.MaxPostSize)
    # is 16383 — leave a safety margin below that.
    _TEXT_CHUNK_LIMIT = 16_000

    # One routing episode's buffer — matches the transport's per-channel queue
    # depth (websocket._CHANNEL_QUEUE_DEPTH), same reasoning as RC's.
    _PENDING_BUFFER_DEPTH = 50
    # Bounded backoff for a creation that raised (§2.2 outcome 4). MM episodes
    # run on their own task, so the backoff holds no shared worker — the bound
    # exists so a dead backend does not retry forever.
    _ROUTE_RETRY_DELAYS: tuple[float, ...] = (0.5, 1.0, 2.0)

    def __init__(self, config: MattermostConfig) -> None:
        self._config = config
        self._rest = MattermostREST(
            config.server_url,
            token=config.token,
            username=config.username,
            password=config.password,
        )
        self._ws = MattermostWebSocketClient(
            config.server_url, token_provider=lambda: self._rest._token
        )
        self._handler: MessageHandler | None = None
        self._capacity_check: CapacityCheck | None = None
        self._router = None
        self._membership_hook = None
        # Room-level membership-loss generation — RC's twin (round 2 of the
        # #121 review): the membership_lost bit on _ChannelState dies with
        # the object, so a loss that marked a mid-flight replacement was
        # invisible to a delivery holding the original. Every loss bumps
        # this; a delivery captures it at entry; the commit fence refuses to
        # redirect across a bump.
        self._membership_gen: dict[str, int] = {}
        # Per-channel serialization of membership HOOK calls — see
        # `_run_membership`. Keyed like the generation above; never cleaned,
        # like the generation: a lock is a few hundred bytes and a channel
        # id space is bounded by rooms the bot is ever told about.
        self._membership_serial: dict[str, asyncio.Lock] = {}
        self._channels: dict[str, _ChannelState] = {}  # channel_id -> state
        # Channels with an open routing episode — the same single-episode rule as
        # RC's `_pending_routes`, and needed here for a sharper reason: the offer
        # runs off the handler path (see `_offer_to_router`), so the per-channel
        # worker's serialization no longer covers the creation, and two back-to-back
        # messages for one new channel could otherwise both trigger it. Frames that
        # arrive during the episode wait in its bounded buffer (§2.7 step 3) and
        # drain in arrival order when it ends.
        self._pending_routes: dict[str, PendingRoute] = {}
        # The off-handler routing tasks, tracked so disconnect() can cancel them
        # rather than leaving offers running against a closed transport.
        self._routing_tasks: set[asyncio.Task] = set()
        self._attachments_cache_base = (
            Path(config.attachments.cache_dir_global).expanduser() / config.name
        )
        self._turn_store: TurnStore | None = (
            TurnStore(ttl_seconds=config.agent_chain.ttl_seconds)
            if config.agent_chain.agent_usernames
            else None
        )

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Authenticate, resolve identity + team, and open the WebSocket."""
        await self._rest.authenticate()
        # Mandatory regardless of auth mode: PAT mode has no login response to
        # pull an identity from, and the own-message filter needs bot_user_id.
        await self._rest.get_me()
        await self._rest.resolve_team(self._config.team)

        self._ws.register_handler(self._on_posted_event)
        self._ws.register_membership_handler(self._on_membership_event)
        self._ws.set_reconnect_callback(self._on_ws_reconnect)
        # The socket opens here; the listen loop starts in `start_inbound()`, after the
        # watchers exist. Events arriving in between are buffered by the client rather
        # than read and discarded for a channel this connector does not know yet.
        await self._ws.connect()
        logger.info(
            "MattermostConnector connected to %s as %s (team=%s)",
            self._config.server_url,
            self._rest.bot_username,
            self._config.team,
        )

    async def start_inbound(self) -> None:
        """Start the listen loop, once `_channels` holds the rooms being watched.

        `_on_posted_event` returns early for a channel with no state, and nothing
        replays it afterwards — the initial watermark restore only covers channels that
        already exist. Reading before the restore therefore turns "not yet subscribed"
        into "permanently lost", which is why this is the last step of startup rather
        than part of connecting.
        """
        await self._ws.start()

    async def disconnect(self) -> None:
        """Close the WebSocket and release HTTP client resources.

        The transport stops **first** — the channel workers are what spawn
        routing and wake episodes (§2.5), so cancelling `_routing_tasks`
        before they stop lets a worker spawn a newcomer during the gather:
        never cancelled, and `clear()` then drops its only strong reference
        while it runs against a dead transport. Stop the spawner, then
        harvest; the offers themselves run off the handler path, which is why
        they still need the explicit cancel afterwards.
        """
        await self._ws.stop()
        for task in list(self._routing_tasks):
            task.cancel()
        if self._routing_tasks:
            await asyncio.gather(*self._routing_tasks, return_exceptions=True)
        self._routing_tasks.clear()
        await self._rest.close()
        logger.info("MattermostConnector disconnected")

    # Maximum messages fetched per channel during a reconnect history replay —
    # same rationale and value as RC's _REPLAY_HISTORY_COUNT.
    _REPLAY_HISTORY_COUNT = 200

    async def _on_ws_reconnect(self) -> None:
        """Replay messages missed during a WebSocket outage.

        Mirrors RocketChatConnector._on_ws_reconnect: for each tracked
        channel with a known watermark, fetch missed messages via REST
        history and re-inject them through the normal filter/normalize/
        dispatch pipeline (_on_posted_event, with is_replay=True).

        Mention detection differs from live dispatch: Mattermost's REST
        history API returns bare Post objects with no mention data at all
        (unlike the WS event's sibling `mentions` field) — see
        normalize.text_mentions_bot for the text-based fallback this uses
        instead, and its documented limitation (only detects mentions of
        the bot itself, not other agents in the same message).
        """
        logger.info(
            "WebSocket reconnected — replaying missed messages for %d channel(s)",
            len(self._channels),
        )
        for channel_id in list(self._channels):
            await self.replay_room_since(channel_id)

    async def replay_room_since(
        self, room_id: str, after_ts: str | None = None
    ) -> None:
        """Replay one tracked channel's outage window from its watermark.

        The per-channel half of the reconnect replay, shared with the startup
        replay for the same reason as RC's: what "cannot copy the reconnect
        path" forbids is the iteration source (live channels vs persisted
        records), not this fetch-and-inject. The channel must already be
        tracked; startup recreates the watcher first, which restores the
        watermark this reads.
        """
        state = self._channels.get(room_id)
        if state is None:
            return
        channel_id = room_id
        # The membership era, captured in the FIRST synchronous segment —
        # before the history fetch awaits (Codex rounds 19 and 21): a live
        # remove-then-re-add can land during the fetch itself, and a capture
        # taken after it would snapshot the NEW era's generation and happily
        # dispatch the OLD era's fetched posts into the re-added room. The
        # loop below re-checks this value before every dispatch.
        entry_gen = self._membership_gen.get(channel_id, 0)
        # Membership, revalidated before a single replayed post is dispatched.
        # A removal that happened while the WebSocket was down produced no
        # `user_removed` event, so `_membership_gen` never moved — and the
        # bot's token can still READ a public channel it has left (probed).
        # Without this, an old tracked watcher processed a kicked channel's
        # backlog on reconnect. `None` is the connector's own final answer
        # ("not ours any more"); a transport failure raises out of
        # `_resolved_channel` and is treated as UNKNOWN — the replay proceeds
        # as before, rather than inventing a removal from a network blip
        # (Codex, PR #140 round 2; the gap `core/replay_window.py` records).
        try:
            still_member = await self._resolved_channel(channel_id) is not None
        except Exception as exc:
            logger.warning(
                "Channel %s: could not confirm membership before replay (%s) — "
                "proceeding; only a confirmed removal reclaims the room",
                channel_id, exc,
            )
            still_member = True
        if not still_member:
            logger.warning(
                "Channel %s: this account is no longer a member — skipping the "
                "replay and reclaiming the room as a removal",
                channel_id,
            )
            self._note_removed(channel_id)
            return
        # An explicitly named window (startup, post-park) is not this channel's
        # boundary to spend — same rule, same reason, as Rocket.Chat's.
        external_window = after_ts is not None
        # Snapshot the watermark NOW, before any await in this iteration —
        # same race rationale as RC: a concurrent live message must not be
        # allowed to advance the watermark mid-replay and cause the rest
        # of this channel's replay window to be skipped as "already
        # processed".
        # Captured before the fetch so the close below can tell whether anyone has
        # claimed this window since. A hand-back landing while this batch is dispatching
        # claims the very window it is reading and writes back the same timestamp, so a
        # value comparison would report "unchanged" in exactly the case it exists to
        # catch. Replay is not serialized against live traffic here — it calls
        # `_on_posted_event` directly rather than through the per-channel worker.
        watermark = after_ts or state.replay_boundary or state.last_processed_ts
        if not watermark:
            logger.debug("Channel '%s': no watermark yet — skipping replay", state.room.name)
            return
        if not external_window:
            # Claim the window BEFORE the first await (Codex round 25): this
            # replay runs detached since round 22, and a cancellation — a
            # shutdown, another drop's harvest — used to leave nothing
            # pointing at the unprocessed tail while live traffic advanced
            # last_processed_ts past it; the next boot then started above
            # the miss. Claimed, the boundary survives the cancellation and
            # the for/else below discharges it only after the whole batch
            # dispatched. (An external window is its caller's to keep — the
            # failure arms below already claim it on that caller's behalf.)
            state.claim_boundary(watermark)
        claims_at_entry = state.boundary_claims
        try:
            page = await self._rest.get_room_history_page(
                channel_id, count=self._REPLAY_HISTORY_COUNT, after_ts=watermark
            )
            raw_msgs = page.messages
        except Exception as e:
            # An EXTERNAL window that was not read is claimed (Codex review of
            # #121, RC's twin has the same rule): the caller's mark lives in
            # the record, this channel state is fresh, and the triggering
            # post's commit would otherwise seal the unread interval away
            # permanently. Whoever fails to replay owns keeping it reachable.
            if external_window:
                state.claim_boundary(after_ts)
            logger.warning("Channel '%s': failed to fetch history for replay: %s", state.room.name, e)
            return

        if not raw_msgs:
            if page.was_full:
                # Not an empty window — a page the server filled entirely with system
                # posts, because `per_page` is applied before ACG filters them out.
                # Every user post older than this page is still waiting behind it, and
                # reporting the outage as read would skip them silently. Same rule, and
                # same reason, as the Rocket.Chat replay: the count the server applied
                # is not the count that survived filtering.
                logger.warning(
                    "Channel '%s': the newest %d history entries are all system posts "
                    "— any user posts older than them cannot be reached in one page "
                    "and may be permanently missed",
                    state.room.name, self._REPLAY_HISTORY_COUNT,
                )
            else:
                logger.debug(
                    "Channel '%s': no missed messages since %s",
                    state.room.name, watermark)
                # A read that found nothing is still a read — of the window this replay
                # came in for, not one claimed during the fetch above. An externally
                # named window is not this channel's mark to spend either way.
                if not external_window:
                    state.discharge_boundary(claims_at_entry)
            return

        if page.was_full:
            logger.warning(
                "Channel '%s': replay fetched the maximum %d message(s) — "
                "the outage window may have produced more; some messages "
                "could be permanently lost",
                state.room.name, self._REPLAY_HISTORY_COUNT,
            )
        else:
            logger.info(
                "Channel '%s': replaying %d missed message(s) since %s",
                state.room.name, len(raw_msgs), watermark,
            )

        for idx, post in enumerate(raw_msgs):
            if (channel_id not in self._channels
                    or self._membership_gen.get(channel_id, 0) != entry_gen):
                logger.debug(
                    "Channel '%s' was unsubscribed or its membership changed "
                    "during replay — skipping %d remaining message(s)",
                    state.room.name, len(raw_msgs) - idx,
                )
                break
            decoded = self._synthesize_decoded_for_replay(post)
            await self._on_posted_event(decoded, is_replay=True, replay_after_ts=watermark)
        else:
            # `for`/`else`, so a cancellation or the `break` above does not reach it: a
            # window is spent once its batch has been *dispatched*, not once it was
            # fetched. Any hand-back during the batch — this replay's own or a live one
            # — left a newer claim, and the window stays open for the next recovery.
            if not external_window and not state.discharge_boundary(claims_at_entry):
                logger.info(
                    "Channel '%s': the replay window was claimed again while this batch "
                    "was being dispatched — leaving it open", state.room.name,
                )

    def _synthesize_decoded_for_replay(self, post: dict) -> dict:
        """Build a decoded-event dict for a REST-history post (replay path only).

        REST history posts have no `mentions` sibling field (see
        text_mentions_bot's docstring) — approximated here as [bot_user_id]
        when the bot's username appears in the text, so the existing
        bot_user_id-in-mentions check in filter_mm_message works unchanged
        for both live and replayed messages.
        """
        mentions: list[str] = []
        bot_username = self._rest.bot_username or ""
        if text_mentions_bot(post.get("message", ""), bot_username) and self._rest.bot_user_id:
            mentions = [self._rest.bot_user_id]
        return {"post": post, "mentions": mentions, "channel_type": None, "channel_name": None, "team_id": None}

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
        """Post an agent response to the channel.

        ``thread_id`` is forwarded as Mattermost's ``root_id`` so the reply
        lands in the correct thread.
        """
        await _send_text(
            self._rest,
            room_id,
            response.text,
            chunk_limit=self.text_chunk_limit,
            root_id=thread_id,
        )

    async def notify_agent_event(
        self,
        room_id: str,
        event: AgentEvent,
        thread_id: str | None = None,
    ) -> None:
        """Refresh the typing indicator on each intermediate agent event.

        Same rationale as RC: keeps a live indicator visible for long-running
        turns (tool calls, permission approvals) instead of it silently
        expiring mid-turn.  Errors are swallowed — a failed typing refresh
        must never abort an agent turn.
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
        """Upload a local file to the channel."""
        await _send_media(self._rest, room_id, file_path, caption)

    async def send_to_room(
        self,
        room: str,
        text: str,
        attachment_path: str | None = None,
    ) -> None:
        """Send a message (and optional attachment) to a room by name or ID.

        Overrides the base Connector implementation for efficient direct
        REST resolution + delivery, same rationale as RC's override.
        """
        try:
            room_info = await self._rest.resolve_room(room)
            room_id = room_info["id"]
        except RoomNotFoundError:
            # Input is likely already a channel ID — use it directly.
            room_id = room

        if attachment_path:
            file_ids = await self._rest.upload_file(room_id, attachment_path)
            await self._rest.post_message(room_id, text, file_ids=file_ids)
        elif text:
            await self._rest.post_message(room_id, text)

    # ── Room resolution ───────────────────────────────────────────────────────

    async def resolve_room(self, room_name: str) -> Room:
        """Resolve a human-readable channel name to a Room object via REST."""
        info = await self._rest.resolve_room(room_name)
        return Room(
            id=info["id"],
            name=info.get("name", room_name),
            type=info.get("type", "channel"),
        )

    async def resolve_room_by_id(self, room_id: str) -> Room:
        """A `Room` for a channel id, for callers that never had a name.

        Boot and recreation resolve by id, never by a persisted name: a name freed by a
        rename can be reused by a different room, and resolving by name would bind an
        existing session to the wrong one (§2.3).

        The name this returns is the **server's**, where `resolve_room` returns the
        configured string for a DM (`@alice`). That divergence is deliberate and bounded:
        the value reaches the prompt prefix and the history header, and both want a
        human-meaningful room — but a DM's server-side name is the opaque
        `<userid>__<userid>` form, so the display name is used for the DM kinds and the
        channel name otherwise.
        """
        resolved = await self._resolved_channel(room_id)
        if resolved is None:
            raise RoomNotFoundError(
                f"Channel {room_id} is not one this connector can serve"
            )
        kind, name, _participants = resolved
        return Room(id=room_id, name=name, type=kind)

    async def _resolved_channel(
        self, room_id: str
    ) -> "tuple[str, str, tuple[str, ...]] | None":
        """`(type, display name, participants)` for a channel id, or `None`.

        The one place that fetches a channel by id, checks it is one this
        connector may serve, and — for the DM kinds — reads its membership.
        Shared by `resolve_room_by_id` and `room_ref_by_id` so the member lookup
        happens ONCE per resolution: an earlier version had the second method
        call the first and then re-read the members, which was two identical
        round trips and a window in which the two answers could disagree.

        `None` for every permanent absence, so `room_ref_by_id` can honour its
        contract: no such channel, one in a team this connector does not serve,
        or one this account is no longer a member of. A transport failure still
        raises out of `self._rest`.
        """
        try:
            info = await self._rest.get_channel(room_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (400, 403, 404):
                # Gone, or never visible to this account. Final either way — a
                # retry cannot make a deleted channel exist.
                logger.info(
                    "Channel %s is not available to this connector (%s)",
                    room_id, exc.response.status_code,
                )
                return None
            raise
        kind = info["type"]

        # A channel id is globally unique, so this can reach a channel in a team
        # the connector no longer serves — the bot account may still belong to
        # both. DMs are exempt: they belong to no team (§6.3), so there is
        # nothing to compare.
        if kind not in ("dm", "group_dm"):
            team_id = info.get("team_id", "")
            if team_id and self._rest.team_id and team_id != self._rest.team_id:
                logger.info(
                    "Channel %s belongs to team %s, but this connector serves %s",
                    room_id, team_id, self._rest.team_id,
                )
                return None

        # Membership, which Rocket.Chat gets for free (its subscription record IS
        # membership) and Mattermost does not: a channel the bot was removed from
        # still resolves by id. Without this a resurrection could put the agent
        # back into a room it was kicked out of (§2.7). `get_member_channel_ids`
        # is the probe rather than the per-channel one, because that answers 403
        # for a non-member indistinguishably from a permissions problem (§6.2).
        if room_id not in await self._rest.get_member_channel_ids():
            logger.info(
                "This account is not a member of channel %s — not resolving it",
                room_id,
            )
            return None

        if kind in ("dm", "group_dm"):
            # The REST channel object's `display_name` is empty for a direct
            # channel: the counterpart handle on a WebSocket event is
            # viewer-specific and is not part of the channel. The members supply
            # it instead.
            members = tuple(await self._rest.channel_member_usernames(
                room_id, exclude=self._rest.bot_user_id or ""))
            name = ", ".join(members) or info["display_name"] or room_id
            return kind, name, members
        return kind, info["name"] or room_id, ()

    async def room_ref_by_id(self, room_id: str) -> "RoomRef | None":
        """See `Connector.room_ref_by_id`.

        Keeps the participants `resolve_room_by_id` flattens into a display name
        — a `Room` has nowhere else to put them, and they are what a 1:1 DM's
        watcher handle is built from.
        """
        resolved = await self._resolved_channel(room_id)
        if resolved is None:
            return None
        kind, name, participants = resolved
        # `RoomKind`'s values ARE these strings, and `_resolved_channel` has
        # already normalised the wire letter into one.
        room_kind = RoomKind(kind)
        if room_kind in (RoomKind.DM, RoomKind.GROUP_DM):
            return RoomRef(id=room_id, kind=room_kind, participants=participants)
        return RoomRef(id=room_id, kind=room_kind, name=name)

    # ── Per-channel local bookkeeping (no wire protocol — see websocket.py) ────

    async def subscribe_room(
        self,
        room: Room,
        watcher_id: str = "",
        working_directory: str = "",
    ) -> None:
        """Start caring about inbound events for this channel.

        No wire-protocol call: the WebSocket already streams every channel
        the bot is a member of. This just registers local dispatch state —
        events for channels with no _ChannelState entry are ignored by
        _on_posted_event.
        """
        wid = watcher_id or room.id
        state = self._channels.get(room.id)
        if state is None:
            state = _ChannelState(room=room)
            self._channels[room.id] = state
            self._ws.register_channel(room.id)
        state.watcher_ids.add(wid)
        logger.info(
            "Now tracking channel '%s' (id=%s, type=%s) for watcher '%s'",
            room.name, room.id, room.type, wid,
        )

    async def unsubscribe_room(self, room_id: str, watcher_id: str = "") -> None:
        """Stop caring about this channel once its last watcher leaves."""
        state = self._channels.get(room_id)
        if state is None:
            return
        if watcher_id:
            state.watcher_ids.discard(watcher_id)
        if state.watcher_ids:
            logger.debug(
                "Channel %s still has %d active watcher(s) — keeping local state",
                room_id, len(state.watcher_ids),
            )
            return
        self._channels.pop(room_id, None)
        self._ws.unregister_channel(room_id)
        logger.info("Stopped tracking channel %s", room_id)

    def update_last_processed_ts(self, room_id: str, ts: str) -> None:
        state = self._channels.get(room_id)
        if state:
            state.last_processed_ts = ts

    def get_last_processed_ts(self, room_id: str) -> str | None:
        state = self._channels.get(room_id)
        if state is None:
            return None
        # The oldest OWED mark, not merely the newest processed one (Codex
        # round 26): a claimed replay boundary is a window someone still owes
        # a read of, and shutdown's watermark capture persists this getter's
        # answer — returning only last_processed_ts let a cancelled replay's
        # claimed-but-undischarged tail vanish from the durable record, and
        # the next boot started above it. Older is the safe direction: a low
        # watermark costs a re-fetch dedup absorbs; a high one loses messages.
        if state.boundary_claims and state.replay_boundary:
            from gateway.core.replay_window import ts_to_float

            lp = ts_to_float(state.last_processed_ts or "")
            rb = ts_to_float(state.replay_boundary)
            if lp is None or (rb is not None and rb < lp):
                return state.replay_boundary
        return state.last_processed_ts

    # ── Attachment cache ────────────────────────────────────────────────────────

    def _cache_dir_for(self, channel_id: str) -> Path:
        """The channel's cache directory, contained under the cache base.

        The character-class sanitize alone lets `..` through — dots are legal
        filename characters — and expiry `rmtree`s this directory, so
        `resolve_under` is the actual fence. See Rocket.Chat's
        `attachment_cache_dir` twin; a refused component raises to the caller
        below, which answers None (no caching for this channel).
        """
        safe_channel_id = re.sub(r"[^\w.\-]", "_", channel_id)
        return resolve_under(self._attachments_cache_base, safe_channel_id)

    @property
    def text_chunk_limit(self) -> int | None:
        return self._TEXT_CHUNK_LIMIT

    # ── Security: server-injected prompt prefix ───────────────────────────────

    # Same rationale as RC's _PREFIX_UNSAFE_RE: a channel/user display name
    # containing these characters could inject fake delimiter fields into the
    # trusted header and bypass RBAC enforcement in CLAUDE.md.
    _PREFIX_UNSAFE_RE = re.compile(r"[\|\[\]\r\n]")

    def bot_identity(self) -> BotIdentity:
        """Account id from `users/me`, scoped by the **resolved** team id.

        Both halves are server-resolved for the same reason: token-only auth leaves
        `username` empty, and `team:` accepts either a team name or a team id, so two
        connectors on one team can spell it two ways. Comparing what the operator wrote
        would call them different and let the duplicate through — the one case this
        exception is supposed to be safe for is exactly the case it would break.

        A missing team is fatal rather than an empty scope: an empty scope means "no
        sub-scope keeps me apart from another connector on this account", which for
        Mattermost would silently convert a supported two-team setup into a rejected
        one — or, worse, admit a connector whose team gate cannot work.
        """
        user_id = self._rest.bot_user_id
        team_id = self._rest.team_id
        if not user_id:
            raise ConnectorIdentityError(
                f"Mattermost connector for {self._config.server_url} cannot report its "
                f"own user id — `users/me` returned none, so this connector is not "
                f"authenticated and cannot be checked against the others for a shared "
                f"bot account."
            )
        if not team_id:
            raise ConnectorIdentityError(
                f"Mattermost connector for {self._config.server_url} has no resolved "
                f"team id for team '{self._config.team}'. Two connectors may share one "
                f"bot account only when each is scoped to a different team, and that "
                f"cannot be established here."
            )
        return BotIdentity(
            platform="mattermost",
            origin=canonical_origin(self._config.server_url),
            user_id=user_id,
            scope=team_id,
        )

    @property
    def agent_username(self) -> str:
        """The bot's own Mattermost username.

        Resolved via get_me() during connect() — falls back to the
        configured username (login mode) if called before connect(), which
        is empty in token mode until connect() completes.
        """
        return self._rest.bot_username or self._config.username

    @property
    def timezone(self) -> str:
        return self._config.timezone or _server_local_timezone()

    def _compute_to_field(self, msg: IncomingMessage) -> str:
        """Compute the compact ``to:`` routing field — same semantics as RC's.

        See RocketChatConnector._compute_to_field for the full field
        vocabulary (to: me / @agent / me+@agent / @all / *).
        """
        # A scheduled job's message is addressed to the agent it was created
        # against — `to: me`, before any mention arithmetic. Derived from
        # mentions it rendered as `to: *`, and the routing rules for a broadcast
        # in a channel say "be conservative; if you decide not to respond, output
        # ONLY the termination token" — which is exactly what the agent did, every
        # minute, so a working job produced nothing in the room and looked broken.
        if is_scheduled_message(msg):
            return "to: me"
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

    def format_prompt_prefix(self, msg: IncomingMessage) -> str:
        """Return the trusted Mattermost identity header for the agent prompt.

        Matches RC's actual (richer-than-CLAUDE.md-documented) format, for
        feature parity with RC's day/ts/to fields:
            [Mattermost #<channel> | from: <user> | role: <role> |
             day: <Mon-Sun> | ts: <ISO8601> | to: <addressing>]
        """
        safe_room = self._PREFIX_UNSAFE_RE.sub("_", msg.room.name)
        safe_user = self._PREFIX_UNSAFE_RE.sub("_", msg.sender.username)
        ts = ts_ms_to_iso_local(msg.timestamp, self.timezone)
        day = weekday_abbrev(ts)
        day_part = f" | day: {day}" if day else ""
        ts_part = f" | ts: {ts}" if ts else ""
        to_part = f" | {self._compute_to_field(msg)}"
        return (
            f"[Mattermost #{safe_room} | "
            f"from: {safe_user} | "
            f"role: {msg.role.value}{day_part}{ts_part}{to_part}]"
        )

    # ── Status notifications ──────────────────────────────────────────────────

    async def notify_typing(self, room_id: str, is_typing: bool) -> None:
        """Send a typing indicator via the WebSocket user_typing action.

        Mattermost auto-clears typing indicators client-side after a few
        seconds, so is_typing=False is a no-op (nothing to explicitly clear).
        """
        if is_typing:
            try:
                await self._ws.send_typing(room_id)
            except Exception as e:
                logger.debug("Failed to send typing indicator: %s", e)

    def on_agent_chain_drop(self, room_id: str, thread_id: str | None, sender: str) -> None:
        """Reset the sender's turn counter after an agent-chain termination drop."""
        if self._turn_store is not None:
            self._turn_store.reset_sender(room_id, thread_id, sender)

    # ── Attachment support ────────────────────────────────────────────────────

    def supports_attachments(self) -> bool:
        return True

    async def download_attachment(self, ref: dict, dest_path: str) -> None:
        """Download a Mattermost file attachment (identified by file_id) to dest_path."""
        file_id = ref.get("file_id", "")
        await self._rest.download_file(file_id, dest_path)

    def attachment_cache_dir(self, room_id: str) -> str | None:
        """Return the global cache directory for a channel's attachments."""
        try:
            return str(self._cache_dir_for(room_id))
        except ValueError:
            logger.warning(
                "Refusing an attachment cache path for channel id %r — it does "
                "not name a directory under the cache base", room_id,
            )
            return None

    # ── History ──────────────────────────────────────────────────────────────

    def supports_room_lookup(self) -> bool:
        """See `Connector.supports_room_lookup` — `room_ref_by_id` is a real lookup here."""
        return True

    def supports_unsolicited_inbound(self) -> bool:
        """Yes — one socket carries every channel the bot is a member of, and only
        those (design §2.6, verified in §6.2)."""
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

        Same contract and security boundary as RocketChatConnector's
        fetch_room_history: excludes messages from senders not in the
        owner/guest allowlist or agent chain (anonymous users are excluded
        to prevent prompt injection). The bot's own prior messages are
        included with role="agent"/username="me"; peer agents are included
        with role="agent" and their real (sanitized) username.

        `before_ts`/`after_ts` are epoch milliseconds, the internal
        representation (§5.2), which is what `MattermostREST.get_room_history`
        wants natively — so they pass straight through. They used to be
        converted from ISO here, and the converter *raised* on an epoch-ms
        value: the same bound that worked on Rocket.Chat, whose normalizer
        tolerates both, silently cost every Mattermost recreation its history
        handoff. Applied as a best-effort client-side filter, not exact
        server-side pagination.
        """
        raw_msgs = await self._rest.get_room_history(
            room.id,
            count,
            before_ts=before_ts or None,
            after_ts=after_ts or None,
        )
        bot_username = self.agent_username
        owners = set(self._config.owners)
        guests = set(self._config.guests)
        peer_agents = set(self._config.agent_chain.agent_usernames)
        safe_room = self._PREFIX_UNSAFE_RE.sub("_", room.name)
        tz = self.timezone

        result: list[dict] = []
        for m in raw_msgs:
            sender_id = m.get("user_id", "")
            if not sender_id:
                continue
            try:
                sender = await self._rest.resolve_username(sender_id)
            except Exception as e:
                logger.warning("Failed to resolve sender for history message: %s", e)
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
                role = "agent"
                display_username = self._PREFIX_UNSAFE_RE.sub("_", sender)
            else:
                # Anonymous / unlisted sender — exclude for prompt injection safety.
                continue

            ts_str = ts_ms_to_iso_local(str(m.get("create_at", "")), tz)
            result.append({
                "ts": ts_str,
                "username": display_username,
                "role": role,
                "room_name": safe_room,
                "text": m.get("message", ""),
            })
        return result

    # ── Internal: posted-event dispatch ──────────────────────────────────────

    def register_router(self, router) -> None:
        """Register the callback consulted for a channel no watcher is tracking.

        Replaces "unknown channel → discard" with "unknown channel → ask". The connector
        supplies a `RoomRef`; deciding whether a watcher should exist for it is the core's
        business (a rule has to match, §2.2).

        Called as `router(room, trigger)`, where `trigger` is the decoded event that
        prompted the offer — the same contract as Rocket.Chat's, and for the same
        non-hypothetical reason: a creation whose `history_handoff` fetches with no upper
        bound picks up the trigger, which this connector then hands back as the live
        prompt, and the agent sees the same message twice. The trigger is passed rather
        than a timestamp because the creation path decides what it needs from it
        (`fetch_room_history` takes a `before_ts`; excluding by id is also open to it).

        Optional on purpose: with no router registered the connector behaves exactly as
        before, which is what keeps this branch runnable while creation is still driven by
        static config.
        """
        self._router = router

    def register_membership_hook(self, hook) -> None:
        """Register the callbacks for the bot's own membership events (§2.7).

        Registration alone changes nothing on the wire — the websocket already
        receives `user_added`/`user_removed`; with no hook they stay ignored,
        which is what keeps a static deployment's behaviour byte-identical.
        """
        self._membership_hook = hook

    async def _on_membership_event(self, evt: dict) -> None:
        """Filter a raw membership event to the bot's own, and act off-path.

        The two events carry their ids in asymmetric places (verified against
        the server's `NewWebSocketEvent` calls in `channel.go`):

        * `user_added` — `data.user_id` is the added user; the channel is in
          `broadcast.channel_id` on both the channel-scoped and user-scoped
          variants.
        * `user_removed` broadcast to the channel — `data.user_id` is the
          removed user, channel in `broadcast.channel_id`. The removed user
          no longer belongs to the channel, so the bot sees this only for
          *other* people's removals.
        * `user_removed` broadcast to the removed user — the one the bot gets
          for itself: the user is `broadcast.user_id`, the channel is
          `data.channel_id`, and `data.user_id` is absent.

        Reading user-then-broadcast for the id and broadcast-then-data for the
        channel covers all three without knowing which arrived. Everything
        past the own-id filter runs on its own task (`_routing_tasks`, so
        disconnect cancels it): the add needs a REST call, and this method
        runs on the listen loop, where an await stalls every channel.
        """
        if self._membership_hook is None:
            return
        own_id = self._rest.bot_user_id
        if not own_id:
            return
        data = evt.get("data") or {}
        broadcast = evt.get("broadcast") or {}
        user_id = data.get("user_id") or broadcast.get("user_id") or ""
        if user_id != own_id:
            return
        channel_id = broadcast.get("channel_id") or data.get("channel_id") or ""
        if not channel_id:
            return
        if evt.get("event") == "user_removed":
            self._note_removed(channel_id)   # stamps synchronously, then the hook
            return
        else:
            # The generation, captured SYNCHRONOUSLY at dispatch (Codex round
            # 4): the add's REST classification awaits, and a removal that
            # lands in that window must not be outrun — the recheck before
            # `hook.added` compares against this. A genuine re-add captures
            # the already-bumped generation and still passes.
            entry_gen = self._membership_gen.get(channel_id, 0)

            def make_coro(cid=channel_id, gen=entry_gen):
                return self._handle_membership_add(cid, gen)
        # Guarded like RC's `_run_membership_callback`, and for the same
        # reason: a hook that raises inside a spawned task is otherwise an
        # unobserved task exception — a GC-time warning, not a log line.
        task = asyncio.create_task(self._run_membership(make_coro, channel_id))
        self._routing_tasks.add(task)
        task.add_done_callback(self._routing_tasks.discard)

    def _note_removed(self, channel_id: str) -> None:
        """This account is out of `channel_id`: stamp, fence, and run the hook.

        The one path for a removal however it was learned — the live
        `user_removed` event, or a replay's membership check finding the
        channel gone after an offline interval. The second used to set the
        flag only and leave the reclaim to "reconciliation", which examines
        paused and dropped records and never this active one: the record,
        processor and jobs persisted indefinitely, and a re-add kept dropping
        every post because nothing cleared the flag (Codex, PR #140 round 4).

        Stamps SYNCHRONOUSLY, before the task below exists, so a delivery in
        flight that holds this state can tell a membership replacement from a
        benign watcher restart (the commit fence in `_on_posted_event`).
        """
        state = self._channels.get(channel_id)
        if state is not None:
            state.membership_lost = True
        self._membership_gen[channel_id] = self._membership_gen.get(channel_id, 0) + 1
        if self._membership_hook is None:
            return  # static deployment: nothing registered to reclaim

        # A FACTORY, not a coroutine (internal review of the serialization
        # close): the task's first await is the serial lock, and a task
        # cancelled while parked there would leave an eagerly-created
        # coroutine never awaited — a RuntimeWarning per disconnect and a
        # silently skipped hook. Created inside the lock instead, so a
        # cancelled park abandons only a factory.
        def make_coro(cid=channel_id):
            return self._membership_hook.removed(cid)

        task = asyncio.create_task(self._run_membership(make_coro, channel_id))
        self._routing_tasks.add(task)
        task.add_done_callback(self._routing_tasks.discard)

    async def _run_membership(self, make_coro, channel_id: str) -> None:
        # Serialized PER CHANNEL, in arrival order (structural close after
        # Codex rounds 4/5/9 each found one interleaving of the unordered
        # version): each event's task is created in arrival order and its
        # first await is this acquisition, so the lock's FIFO wakeup
        # processes a channel's add/remove hooks in the order the platform
        # sent them — a removal can no longer complete around an add that is
        # still classifying, and a parked removal no longer swallows the
        # re-add behind it. The delivery fences stamp SYNCHRONOUSLY at
        # dispatch, before this task exists, so serialization never delays
        # them. Cross-channel events still run concurrently.
        lock = self._membership_serial.setdefault(channel_id, asyncio.Lock())
        async with lock:
            try:
                await make_coro()
            except Exception:
                logger.exception(
                    "Membership handling failed for channel %s — the safety nets "
                    "cover it", channel_id,
                )

    async def _handle_membership_add(
        self, channel_id: str, entry_gen: int = 0,
    ) -> None:
        """Classify a joined channel via REST and hand it to the hook.

        The event itself is sparse — `data` carries only `user_id` and
        `team_id` — so unlike a post there is no metadata to classify from,
        and one REST call per join is the price of the registration. Failure
        is logged and dropped: an add is a supplement (§2.7), and the room's
        first message still creates its watcher.
        """
        try:
            chan = await self._rest.get_channel(channel_id)
        except Exception as e:
            logger.warning(
                "Could not classify joined channel %s — it stays unregistered "
                "until its first message: %s", channel_id, e,
            )
            return
        decoded = {
            "channel_type": chan.get("type"),
            "channel_name": chan.get("name"),
            "channel_display_name": chan.get("display_name"),
            "team_id": chan.get("team_id"),
        }
        if not self._in_scope(decoded):
            logger.debug(
                "Joined channel %s belongs to another team — not registered",
                channel_id,
            )
            return
        room = self._room_ref_from_event(channel_id, decoded)
        if room is None:
            return
        if self._membership_gen.get(channel_id, 0) != entry_gen:
            # A removal landed while the classification awaited (Codex round
            # 4): registering now would create an idle record for a room the
            # bot has already left. The last recheck before the hook, so both
            # interleavings are correct — a removal after this add's dispatch
            # fails here, and a genuine re-add captured the bumped generation
            # at dispatch and passes.
            logger.info(
                "Channel %s: membership was lost while the join was being "
                "classified — not registering", channel_id,
            )
            return
        await self._membership_hook.added(room)

    async def membership_snapshot(self) -> set[str] | None:
        """See `Connector.membership_snapshot`. The full-membership listing is
        the one probe that is unambiguous with the bot's own token (§6.2) —
        the per-channel member lookup 403s for a non-member, which a
        permissions problem also does."""
        try:
            return await self._rest.get_member_channel_ids()
        except Exception as e:
            logger.warning(
                "Could not read the channel-membership set — membership is "
                "unknown this pass: %s", e,
            )
            return None

    def reap_room(self, room_id: str) -> None:
        """Forget a channel's local state without touching watcher bookkeeping.

        Unsubscribing is a *watcher* releasing a room; reaping is the room going away — a
        channel the bot was removed from, or one whose watcher expired. Keeping them
        separate matters because `unsubscribe_room` returns early while any other watcher
        holds the room, which is the wrong answer when the room itself is gone.

        Idempotent: reaping an unknown channel is not an error, since the caller is
        reacting to an event that may arrive more than once.
        """
        if self._channels.pop(room_id, None) is not None:
            self._ws.unregister_channel(room_id)
            logger.info("Reaped channel state for %s", room_id)

    async def probe_missed_since(self, room: Room, after_ts: str) -> bool:
        """See `Connector.probe_missed_since`. Raw posts, so `user_id` is still
        on them — `fetch_room_history` maps the bot's own posts to `"me"`."""
        page = await self._rest.get_room_history_page(
            room.id, count=self._REPLAY_HISTORY_COUNT, after_ts=after_ts
        )
        if page.was_full:
            # Same trap, same reason as Rocket.Chat's: `count` is applied before
            # system posts are filtered out, so an empty filtered list can mean
            # "nothing here" or "a full page of joins hiding everything older".
            return True
        own_id = self._rest.bot_user_id
        for post in page.messages:
            if own_id and post.get("user_id") == own_id:
                continue
            # Strictly after: `after_ts` is inclusive, so the message that set
            # this watermark is in the page and is not a gap.
            if ts_gt(str(post.get("create_at", "")), after_ts):
                return True
        return False

    def trigger_history_bound(self, trigger) -> str | None:
        """The trigger's `post.create_at` — already epoch milliseconds, which is
        the internal representation (§5.2), so this reads it rather than
        converting it."""
        if not isinstance(trigger, dict):
            return None
        post = trigger.get("post")
        create_at = post.get("create_at", "") if isinstance(post, dict) else ""
        return str(create_at) if create_at else None

    def _room_ref_from_event(self, channel_id: str, decoded: dict) -> "RoomRef | None":
        """Build a `RoomRef` from the event, or None when the event cannot describe a room.

        Everything needed is on the wire for a channel post — name, type and team — so this
        costs no REST call, which matters because Mattermost holds a connector-wide permit
        for the whole handler (§6.2).

        **Returns None when the metadata is absent, and that is not a defensive
        formality.** The reconnect-replay path synthesizes its decoded events from REST
        history, which carries bare posts with no channel metadata at all — every field
        here is `None` on every replayed message. A router asked to judge one of those
        would be judging `channel_type=None`, and a team gate reading the same fields would
        silently swallow the entire replay window.
        """
        channel_type = decoded.get("channel_type")
        if not channel_type:
            return None

        kind = _ROOM_KINDS.get(channel_type)
        if kind is None:
            # BOTH spellings are real inputs (Codex round 13): the wire event
            # carries the platform letters, but the membership-add path feeds
            # this helper `get_channel()`'s output, which `room_type_for` has
            # already normalized — and the letters-only lookup made
            # `hook.added` unreachable for every real REST response, so a
            # joined room was never registered until its first message. Same
            # dual lookup `_room_ref_from_state` already uses.
            try:
                kind = RoomKind(channel_type)
            except ValueError:
                logger.debug(
                    "Channel %s has unknown type %r — not routable",
                    channel_id, channel_type)
                return None

        display = decoded.get("channel_display_name") or ""
        if kind is RoomKind.DM:
            # `channel_name` is `<userid>__<userid>`; the display name is the counterpart,
            # and on the wire it is `@`-PREFIXED (`@alice`). `bare_handle` drops the prefix
            # so one person has one handle on both platforms — Rocket.Chat supplies the name
            # bare, and `@` is outside `_LABEL_SAFE`, so keeping it produced `dm:%40alice`.
            #
            # Stripped BEFORE the emptiness test, not after: a display of `"@"` alone would
            # otherwise pass the guard and yield `("",)`, and an empty participant makes
            # `room_label` produce a nameless `dm:` instead of falling back to the room-id
            # digest.
            counterpart = bare_handle(display)
            participants = (counterpart,) if counterpart else ()
        elif kind is RoomKind.GROUP_DM:
            # The display name is the member list, including the bot itself. Split for the
            # participants column; never used as a label, which must not move when membership
            # does (§2.3).
            #
            # **Group DMs are NOT prefixed** — Mattermost is inconsistent between the two
            # kinds, verified on 11.7.0 (§6.4): a DM event carries `'@mmadmin'` while a group
            # DM event carries `'acg_bot, mmadmin, probe-extra'`. `bare_handle` is applied
            # anyway, and is a no-op here: the alternative is two branches that treat the
            # same field by different rules, which is how the prefix went unnoticed on the
            # DM side for as long as it did.
            participants = tuple(
                h for p in display.split(",") if (h := bare_handle(p.strip()))
            )
        else:
            participants = ()

        return RoomRef(
            id=channel_id,
            kind=kind,
            name="" if kind in (RoomKind.DM, RoomKind.GROUP_DM)
            else (decoded.get("channel_name") or ""),
            participants=participants,
        )

    def _in_scope(self, decoded: dict) -> bool:
        """Whether this event belongs to the team this connector serves (§6.3).

        The socket spans every team the account belongs to, so a connector scoped to one
        team must discard another team's channels itself.

        **Two things pass the gate that a naive equality check would reject**, and both
        would be silent losses:

        * **A DM has no team.** `data.team_id` is empty for DMs and `broadcast.team_id` is
          empty always (§6.2), so gating on equality would reject every direct message —
          disabling DM support by way of a team filter.
        * **A replayed message has no metadata at all.** The reconnect path synthesizes
          events from REST history with `team_id=None`, so an equality check would drop
          the entire replay window after every disconnect. Those posts were fetched for
          channels this connector already tracks, so their scope is established by the
          subscription that fetched them.
        """
        team_id = decoded.get("team_id")
        if not team_id:
            return True
        return team_id == self._rest.team_id

    async def _offer_to_router(self, channel_id: str, decoded: dict) -> None:
        """Hand an untracked channel to the router, if there is one and it is in scope.

        The router itself runs **off the handler path**. This method executes on the
        channel's worker, which holds the connector-wide `_callback_sem` for the whole
        call (§6.2) — so awaiting a creation here would stall delivery for *every*
        channel, which is exactly the stall §2.7 step 3 moves creation off the handler
        to avoid. The gates below are cheap (one cached REST call at most); everything
        after them is spawned as a task and this method returns, releasing the permit.
        """
        if self._router is None:
            return
        if not self._in_scope(decoded):
            logger.debug(
                "Channel %s belongs to another team — not offering to the router",
                channel_id,
            )
            return
        room = self._room_ref_from_event(channel_id, decoded)
        if room is None:
            return

        # §2.7 step 1 puts the sender allow-list among the cheap rejects, above the
        # room-state lookup, and this is why: a sender who cannot start a turn must not be
        # able to cause a watcher and a backend session to be created. The username costs
        # one REST call per distinct user (`resolve_username` caches), and only for
        # channels no watcher tracks.
        #
        # The *mention* gate deliberately stays out of here. It is kind-dependent —
        # `require_mention` does not apply to a 1:1 DM but does to a group DM — so §2.7
        # runs it after classification, on the tracked path the trigger is handed back
        # through below.
        sender_id = decoded["post"].get("user_id", "")
        try:
            sender_username = await self._rest.resolve_username(sender_id)
        except Exception as e:
            logger.warning(
                "Could not resolve sender %s for untracked channel %s: %s",
                sender_id, channel_id, e,
            )
            return
        if not sender_allowed(self._config, sender_username):
            logger.debug(
                "Sender %r is not allowed to start a watcher in channel %s",
                sender_username, channel_id,
            )
            return

        self._route_or_buffer(channel_id, room, decoded)

    def _channel_is_served(self, channel_id: str) -> bool:
        """A processor answers for this channel now. Tracked is necessary, not sufficient.

        The idle drop keeps a channel's local state (§2.2), so `channel_id in
        self._channels` goes on answering True for a channel whose next message has
        nowhere to go. Every deliver-or-route decision keys on this predicate rather
        than on tracked-ness, and the drain's branch is the load-bearing one:
        delivering an unserved channel's frame puts it back on the tracked path, whose
        UNROUTED arm routes it back here — a hot loop with no retry delay anywhere in
        it, entered by every message to a channel whose offer was declined.
        """
        if channel_id not in self._channels:
            return False
        if self._capacity_check is None:
            # No dispatcher wired: nothing can answer UNROUTED, so tracked is served.
            return True
        return self._capacity_check(channel_id) is not RoomCapacity.UNROUTED

    def _room_ref_from_state(self, state: "_ChannelState") -> "RoomRef":
        """A RoomRef for a channel this connector already tracks — the wake's classification.

        No event metadata needed, which is what lets a *replayed* post wake a channel:
        the reconnect path synthesizes its events from REST history with no channel
        metadata at all, so `_room_ref_from_event` answers None for every one of them.
        The tracked state holds what the original classification decided. For a channel
        with a record none of this is load-bearing anyway — `_recreate` reads the kind
        and participants from the record itself (§2.4); the fallback matters only on
        the recordless edge, where `_create` rule-matches this ref.
        """
        kind = _ROOM_KINDS.get(state.room.type)
        if kind is None:
            try:
                kind = RoomKind(state.room.type)
            except ValueError:
                kind = RoomKind.CHANNEL
        return RoomRef(
            id=state.room.id,
            kind=kind,
            # A direct room's tracked name is its *description* (the counterpart, the
            # member list — §2.3); `RoomRef.name` is the platform's own name, empty
            # for both DM kinds by contract.
            name="" if kind.is_direct else (state.room.name or ""),
            # Empty where Rocket.Chat's twin reads its permanent DM cache: Mattermost
            # gets participants from event metadata, and the tracked state keeps
            # none. Rule matching keys on the kind, and a room with a record never
            # consults this ref's participants — the only consequence lives on the
            # recordless-DM wake edge, where `_create` labels the watcher by digest
            # rather than counterpart. Display-only, accepted.
            participants=(),
        )

    def _route_or_buffer(self, channel_id: str, room: "RoomRef", decoded: dict) -> None:
        """One entrance to the routing episode — untracked offers and wakes alike.

        `_offer_to_router` arrives from the untracked path with a gate-checked,
        event-classified room; the tracked handler's UNROUTED arm arrives with the room
        resolved from the tracked state — the wake (§2.5). Both share the pending
        buffer, the single open episode and `_route_channel`'s drain, because a second
        creation entrance is how a wake would skip exactly the guarantees the episode
        exists to make.
        """
        pending = self._pending_routes.get(channel_id)
        if pending is not None:
            # An episode for this channel is open. Checked *before* the served
            # check: the channel may have become served an instant ago with its
            # buffer not yet drained, and delivering this frame directly would put
            # it ahead of every frame that arrived before it.
            verdict = pending.add(decoded["post"].get("id", ""), decoded)
            if verdict == "duplicate":
                # §2.2 outcome 6: the reservation is not disturbed, the copy goes.
                logger.debug(
                    "Channel %s: discarding a duplicate of a reserved message",
                    channel_id)
            elif verdict == "full":
                # §2.2 outcome 5: audible in the room, once per episode.
                logger.warning(
                    "Channel %s: pending buffer full — dropping a frame", channel_id)
                # Spawned, not awaited: this runs on the channel worker, which
                # holds the connector-wide permit for the whole handler call
                # (§6.2), so a slow REST post here would stall delivery for
                # every channel. The notice is owed, not urgent.
                notice = asyncio.create_task(
                    self._post_starting_up_notice(pending, channel_id))
                self._routing_tasks.add(notice)
                notice.add_done_callback(self._routing_tasks.discard)
            return
        if self._channel_is_served(channel_id):
            # Served now — a creation finished while this frame was on its way here.
            # Deliver rather than offer: the per-channel callback that would have
            # taken this frame was registered after it was routed here, so nobody else
            # is going to deliver it. Served, not tracked: an idle channel is tracked
            # and its frame still has nowhere to go — delivering it would bounce it
            # off the tracked path's UNROUTED arm straight back here.
            self._ws.deliver_to_channel(decoded)
            return
        pending = PendingRoute(self._PENDING_BUFFER_DEPTH)
        pending.add(decoded["post"].get("id", ""), decoded)
        self._pending_routes[channel_id] = pending
        task = asyncio.create_task(self._route_channel(channel_id, room, decoded))
        self._routing_tasks.add(task)
        task.add_done_callback(self._routing_tasks.discard)

    async def _route_channel(self, channel_id: str, room: "RoomRef", decoded: dict) -> None:
        """Run one routing episode to completion, off the handler path.

        Owns the `_pending_routes` entry it was spawned under: popped in `finally`,
        whatever happened, so a channel whose offer failed is offerable again on
        its next message — holding the reservation would make one transient
        failure permanent for that channel.

        The router raising means a creation was started and not carried out
        (§2.2 outcome 4) — retryable with bounded backoff, because the manager
        deliberately lets those propagate. A None-shaped outcome (rule miss,
        pause, cap) does not raise and is final. Unlike RC there is no
        classification stage: the kind arrived free on the event.
        """
        # Whether the routing decision was *completed* — see Rocket.Chat's twin:
        # a decline is an answer and its frames are remembered; a park or a
        # cancellation is the absence of one, and remembering those ids would
        # have the park's promised recovery — the next wake's replay from the
        # record watermark — die at the dedup check, silently.
        declined = False
        try:
            async def offer() -> None:
                try:
                    await self._router(room, decoded)
                except RoomAlreadyRoutedError:
                    # Final, not retryable — see Rocket.Chat's copy of this arm.
                    logger.warning(
                        "Channel %s is already served by another watcher — not "
                        "creating a second one", channel_id,
                    )
                    return

            declined = await route_attempts(
                offer, retry_on=Exception,
                delays=self._ROUTE_RETRY_DELAYS, logger=logger,
                label=f"Creating a watcher for channel {channel_id}",
            )
        finally:
            # The episode ends, and the buffer has one of two fates — same rule,
            # same honesty, as RC's `_on_unrouted_message`: drained trigger-first
            # onto the channel's own queue when the channel became tracked, or
            # dropped audibly with the episode when it did not.
            ended = self._pending_routes.pop(channel_id, None)
            frames = ended.drain() if ended is not None else []
            state = self._channels.get(channel_id)
            if state is not None and not self._channel_is_served(channel_id):
                # Tracked and still unserved. Served, not tracked, decides delivery
                # here: these frames' only tracked-path outcome is the UNROUTED arm,
                # which routes them straight back into a new episode — a hot loop
                # with no delay in it, entered by every message to a declined
                # channel. What happens to the ids depends on WHICH way the offer
                # ended — see Rocket.Chat's twin of this branch.
                if declined:
                    # A completed decline: re-remembered (the wake arm forgot them
                    # so a served redelivery could pass the dedup check). The
                    # decline is a configuration state and can persist
                    # indefinitely, so an id left unknown would have every
                    # reconnect re-fetch and re-offer a batch that can never be
                    # spent. The watermark is left where it is, so a user who
                    # resends is served normally once a watcher exists (§2.7).
                    for frame in frames:
                        fid = frame["post"].get("id", "")
                        if fid:
                            self._remember_seen(state, fid)
                    if frames:
                        logger.warning(
                            "Channel %s: dropping %d buffered frame(s) — no watcher "
                            "took the channel. A declined offer: no rule claims it, "
                            "or its record is paused.", channel_id, len(frames),
                        )
                elif frames:
                    # Parked or cancelled: the decision was never made, and this
                    # channel HAS a record — the park's promised recovery is the
                    # next wake's replay from that record's watermark (§2.2), and
                    # a remembered id would have it die at the dedup check. The
                    # wake arm already forgot the trigger and claimed a boundary
                    # below it (`_keep_replayable`), so the ids stay unknown and
                    # the window stays open.
                    logger.warning(
                        "Channel %s: %d buffered frame(s) not delivered — the offer "
                        "parked or was cancelled. Their ids stay unknown so the "
                        "next wake's replay recovers them.", channel_id, len(frames),
                    )
            elif state is not None:
                # Same rule, same mechanism, same reason as Rocket.Chat's: the
                # watermark is a scalar, so a live message accepted during this
                # episode has already advanced it past these frames and the
                # filter would reject them as already processed. The claim is a
                # promise that a recovery comes back for them (§2.2), which is
                # what makes a filtered delivery a deferral and not a loss.
                # PROMISED, not claimed. A boundary claimed here for frames that
                # were then delivered and accepted — the ordinary outcome — was
                # never discharged: only a replay discharges, and none ran. So
                # shutdown persisted a boundary at the channel's creation and
                # the next boot replayed everything since, re-answering messages
                # the agent had already answered (measured on three rooms). The
                # frames' ids are remembered instead, and the boundary is claimed
                # only if the filter rejects one of them as already processed —
                # the exact case the promise exists for (`_on_posted_event`).
                state.promised_ids.update(
                    f["post"].get("id", "") for f in frames if f["post"].get("id"))
                for frame in frames:
                    self._ws.deliver_to_channel(frame)
            elif frames:
                logger.info(
                    "Channel %s: dropping %d buffered frame(s) — no watcher was created",
                    channel_id, len(frames),
                )

    async def _post_starting_up_notice(self, pending: PendingRoute, channel_id: str) -> None:
        """Tell the room its messages are outrunning its setup — once per episode."""
        if pending.notice_posted:
            return
        pending.notice_posted = True
        try:
            await self.send_text(channel_id, AgentResponse(text=STARTING_UP_NOTICE))
        except Exception:
            logger.debug("Could not post the starting-up notice", exc_info=True)

    async def _on_posted_event(
        self,
        decoded: dict,
        *,
        is_replay: bool = False,
        replay_after_ts: str | None = None,
    ) -> None:
        """Filter, normalize, and dispatch one decoded WS posted-event.

        Mirrors RocketChatConnector._on_raw_ddp_message's pipeline, adapted
        for Mattermost's ID-based identity and lack of a wire subscription:
        events for channels with no local _ChannelState (i.e. no watcher has
        called subscribe_room for them) are ignored even though the socket
        delivers them, since the bot may belong to channels ACG isn't
        watching.

        Args:
            is_replay      : True when called from _on_ws_reconnect's history
                              replay path. Suppresses the "server busy" REST
                              notification to avoid spamming the user with one
                              per missed message.
            replay_after_ts: Watermark snapshotted at the start of
                              _on_ws_reconnect's replay loop for this channel.
                              When set, used for the dedup comparison instead
                              of the live state.last_processed_ts, so a
                              concurrent live message can't advance the
                              watermark mid-replay and cause the rest of the
                              replay window to be skipped as already-processed.

        seen_ids registration timing (code-review fix): registered
        immediately after the own-message/system-message checks, BEFORE the
        resolve_username() await below — not after filter_mm_message, as RC
        does. RC can register after filtering because its DDP doc already
        carries the sender's username inline, so nothing awaits between the
        dedup check and registration. Mattermost identifies senders by ID, so
        resolving a username is an unavoidable await sitting between the two
        — leaving it unregistered until after filtering re-opened the exact
        live-vs-replay duplicate-dispatch race the seen_ids window exists to
        close (confirmed via code review: two concurrent calls for the same
        message both passed the dedup check and both dispatched to the
        handler). The tradeoff versus RC's placement: a message that gets
        filtered out (e.g. sender not in the allow-list) is now marked seen
        and won't be re-evaluated on a later replay — acceptable since
        filtering is deterministic given the same message and config, unlike
        RC's rationale for delaying registration (avoiding permanent
        suppression of a message that might become eligible later).
        """
        if not self._handler:
            return

        post = decoded["post"]
        channel_id = post.get("channel_id", "")

        # System and own messages are rejected before the channel is looked up. Both are
        # delivered by Mattermost (§6.2), and both are rejected for reasons that have
        # nothing to do with which channel they arrived in — so checking the channel first
        # only decided *where* they died. It matters now because an unknown channel is no
        # longer the end of the road: it is a candidate for routing, and a join
        # notification is not something to create a watcher for.
        #
        # The dedup below cannot move with them: `seen_ids` lives on the channel's state,
        # so there is nothing to check against until the channel is known.
        if post.get("type"):
            return  # A system message (join/leave/…), never a turn.

        sender_id = post.get("user_id", "")
        if sender_id == self._rest.bot_user_id:
            return  # Own message — also skipped before spending a resolve_username call.

        state = self._channels.get(channel_id)
        entry_mgen = self._membership_gen.get(channel_id, 0)
        if not state:
            await self._offer_to_router(channel_id, decoded)
            return
        if state.membership_lost:
            # The removal hook stamped this state, but the reclamation runs in
            # its own task and can wait on the watcher lock — a post handled
            # in that window used to be normalized and DELIVERED to the agent
            # of a room the bot had already been removed from, with the flag
            # consulted only later at the commit fence (Codex round 9). The
            # bot cannot answer in a room it left; drop at entry.
            logger.debug(
                "Channel %s: membership lost, reclamation pending — "
                "dropping post", channel_id,
            )
            return
        # The frame names the channel as it is NOW. A rename shows up here first
        # (there is no `channel_updated` handling), and the message built below
        # carries it to the lifecycle, which re-derives the watcher's handle.
        # Named kinds only: a DM's `channel_name` is the opaque id pair.
        current_name = decoded.get("channel_name")
        if (current_name and state.room.type in ("channel", "group")
                and state.room.name != current_name):
            logger.info("Channel %s was renamed '%s' → '%s'",
                        channel_id, state.room.name, current_name)
            state.room = Room(id=state.room.id, name=current_name, type=state.room.type)

        msg_id = post.get("id", "")
        if msg_id and msg_id in state.seen_ids_set:
            state.promised_ids.discard(msg_id)   # its live copy was processed
            logger.debug("Skipping already-seen message id=%s in channel %s", msg_id, channel_id)
            return

        # System messages carry no useful sender identity to resolve — and
        # filter_mm_message rejects them anyway — so skip the async username
        # resolution entirely for them.
        # Register BEFORE the first await (resolve_username) — see the
        # seen_ids registration timing note in the docstring above.
        if msg_id:
            self._remember_seen(state, msg_id)

        try:
            sender_username = await self._rest.resolve_username(sender_id)
        except Exception as e:
            logger.error("Failed to resolve sender username for id=%s: %s", sender_id, e)
            # The id was registered above, before this await, so leaving it recorded would
            # have the next replay skip this post at the dedup check — permanently, for a
            # REST call that failed once. And this call fires up to `_REPLAY_HISTORY_COUNT`
            # times immediately after a reconnect, which is exactly when REST is least
            # reliable.
            #
            # Unlike a normalization failure — a property of the message, which will fail
            # again — a lookup failure is a property of the network and will not.
            self._keep_replayable(state, msg_id, str(post.get("create_at", "")))
            return

        filter_ts = (
            replay_after_ts
            if (is_replay and replay_after_ts is not None)
            else state.last_processed_ts
        )
        result: FilterResult = filter_mm_message(
            post=post,
            mentions=decoded["mentions"],
            sender_username=sender_username,
            config=self._config,
            room_type=state.room.type,
            last_processed_ts=filter_ts,
            bot_user_id=self._rest.bot_user_id or "",
            turn_store=self._turn_store,
        )
        # Captured with no await between the filter's increment and this read, so it names
        # the count that increment belonged to — a reset restarts the numbering, and a
        # token from the previous count must not take a turn from the new one.
        turn_generation = (
            self._turn_store.generation(
                post.get("channel_id", ""), post.get("root_id") or None, result.sender)
            if (result.agent_chain_token and self._turn_store is not None)
            else 0
        )
        if not result.accepted:
            if msg_id in state.promised_ids:
                state.promised_ids.discard(msg_id)
                if result.reason.startswith("already processed"):
                    # The race the promise exists for: a live message accepted
                    # during the creation episode advanced the watermark past
                    # this handed-back frame, and the filter now reads it as
                    # old. It was never processed. Keep it reachable.
                    self._keep_replayable(state, msg_id, str(post.get("create_at", "")))
                    logger.info(
                        "Channel %s: handed-back frame %s fell below the watermark — "
                        "boundary claimed so the next replay recovers it",
                        channel_id, msg_id,
                    )
            logger.debug("Message filtered: %s (sender=%s)", result.reason, result.sender)
            return
        state.promised_ids.discard(msg_id)   # accepted: the promise is kept

        logger.info(
            "Filter passed for message from %s in channel '%s' — dispatching: %s",
            result.sender, state.room.name, post.get("message", "")[:80],
        )

        capacity = self._capacity_check(channel_id) if self._capacity_check else None
        if capacity is RoomCapacity.UNROUTED:
            # No processor serves this *tracked* channel. The idle drop keeps the
            # channel's local state on purpose (§2.2), so an idle channel's next
            # message arrives here, and this arm is the wake (§2.5): the channel is
            # offered back through the same episode funnel an untracked one goes
            # through — see the Rocket.Chat connector's twin of this branch.
            #
            # The id was registered optimistically above, before the first await —
            # undone here, with the window kept open below the post, or the episode's
            # redelivery dies at the dedup check. The declined episode's drain is what
            # re-remembers it, so a channel nothing claims still converges. The turn
            # is released because the redelivery runs the filter — and its charge —
            # again.
            if self._router is None:
                # No router registered — a static-only deployment. The old arm's
                # behaviour, verbatim: drop audibly, id left registered so reconnect
                # replays do not re-offer it, watermark untouched.
                logger.warning(
                    "Message for channel '%s' has no watcher — dropping without a "
                    "reply.", state.room.name,
                )
                self._release_turn_for(post, result, turn_generation, "no watcher")
                return
            logger.info(
                "Message for channel '%s' has no processor — offering the channel "
                "back to the router (wake).", state.room.name,
            )
            self._keep_replayable(state, msg_id, str(post.get("create_at", "")))
            self._release_turn_for(post, result, turn_generation, "waking the channel")
            self._route_or_buffer(
                channel_id, self._room_ref_from_state(state), decoded)
            return
        if capacity is RoomCapacity.FULL:
            logger.warning(
                "Preflight rejected for message from %s in channel '%s' — "
                "all processor queues full, skipping normalize + download",
                result.sender, state.room.name,
            )
            if is_replay:
                # A replayed post rejected for capacity has to stay replayable, and the id
                # registered before the `resolve_username` await above is what stops that:
                # the next recovery skips it at the dedup check and reports the window
                # read. Nothing tells the sender either — the busy notice below is
                # suppressed for replays — so the message is gone silently and for good.
                #
                # Rocket.Chat has two hand-back sites, not one: the handler-queue-full
                # branch and this preflight. The round that swept for Mattermost parity
                # carried the first and declared the rule applied; this is the second.
                logger.info(
                    "Channel '%s': a replayed post could not be accepted (queues full) — "
                    "keeping it replayable rather than recording it as handled",
                    state.room.name,
                )
                self._keep_replayable(state, msg_id, result.msg_ts)
                self._release_turn_for(post, result, turn_generation, "replay preflight")
                return

            try:
                await self._rest.post_message(
                    channel_id,
                    "⚠️ Server busy — your message was dropped. Please retry.",
                    root_id=post.get("root_id") or None,
                )
            except Exception as exc:
                logger.debug("Best-effort busy notification failed: %s", exc)
            # A live post keeps its id: the sender was told, and can resend. The turn it
            # spent is still given back — the filter charged it before anything knew the
            # post could not be delivered.
            self._release_turn_for(post, result, turn_generation, "live preflight")
            return

        try:
            msg: IncomingMessage = await normalize_mm_message(
                post=post,
                mentions=decoded["mentions"],
                room=state.room,
                sender_username=result.sender,
                sender_id=sender_id,
                msg_ts=result.msg_ts,
                config=self._config,
                rest=self._rest,
                cache_dir=self._cache_dir_for(channel_id),
                is_agent_chain=result.is_agent_chain,
                agent_chain_turn=result.agent_chain_turn,
                agent_chain_max_turns=result.agent_chain_max_turns,
            )
        except Exception as e:
            logger.error("Failed to normalize message: %s", e)
            self._release_turn_for(post, result, turn_generation, "normalize failed")
            return

        apply_thread_policy(msg, self._config)

        try:
            accepted = await self._handler(msg)
        except Exception as e:
            logger.error("Handler error for message from %s: %s", result.sender, e)
            self._release_turn_for(post, result, turn_generation, "handler raised")
            return

        # The commit target may no longer be `state` (#115, RC's sibling has the
        # same fence): a watcher stop→start while the handler ran popped it from
        # `self._channels` and installed a fresh `_ChannelState`, so a watermark,
        # dedup id or hand-back boundary written to `state` vanishes with it —
        # the next reconnect replay re-delivers an accepted post, and a
        # handed-back one is never recovered. The commit follows the channel.
        live = self._channels.get(channel_id)
        if live is not state and live is not None:
            if (state.membership_lost
                    or self._membership_gen.get(channel_id, 0) != entry_mgen):
                # A membership loss happened while this delivery ran: the live
                # state belongs to a re-add's fresh membership, and a
                # pre-removal post has no claim on its watermark or window —
                # committing one would point the next replay below the
                # removal, delivering the whole non-member interval.
                logger.warning(
                    "Channel %s: discarding the marks of a delivery that "
                    "crossed a membership removal", channel_id,
                )
                if not accepted:
                    self._release_turn_for(post, result, turn_generation,
                                           "handler queue full")
                return
            logger.warning(
                "Channel %s: a delivery outlived its state (watcher restarted "
                "mid-delivery) — committing to the live one", channel_id,
            )

        if not accepted:
            logger.warning("Message from %s was dropped (queue full)", result.sender)
            if live is state:
                self._keep_replayable(state, msg_id, result.msg_ts)
            elif live is not None:
                self._keep_replayable(live, msg_id, result.msg_ts)
            # else: the channel is gone; there is nowhere for a replay to
            # recover into.
            # Forgetting the id is not enough: the filter already spent a turn of this
            # sender's agent-chain budget, before anything knew whether the post could be
            # delivered. Every retry spends another, and once the budget is gone the
            # filter rejects the post as complete — so the message the retry exists for is
            # the one it can never deliver. Same rule as the Rocket.Chat hand-back; it was
            # written for one connector and a comment here asserted the other had no
            # hand-back path, which this branch is.
            self._release_turn_for(post, result, turn_generation, "handler queue full")
            return

        if live is not state:
            if live is None:
                logger.warning(
                    "Channel %s: discarding the watermark of a post whose channel "
                    "was reclaimed mid-delivery", channel_id,
                )
                return
            self._remember_seen(live, msg_id)
            state = live

        # Never backwards, for the reason Rocket.Chat's sibling site gives: replay calls
        # `_on_posted_event` directly rather than through the per-channel worker, so a
        # replayed post awaiting `normalize_mm_message` — an attachment download — can be
        # overtaken by live traffic that commits a newer cursor, and an unconditional
        # assignment then rewinds it. The replayed post is not rejected on the way in
        # because its filter timestamp is pinned to `replay_after_ts`; the pin that makes
        # replay work is what makes the regression reachable. In memory `seen_ids` hides
        # the rewind; across a save and a restart it does not.
        if ts_gt(result.msg_ts, state.last_processed_ts or ""):
            state.last_processed_ts = result.msg_ts

    @staticmethod
    def _keep_replayable(state: "_ChannelState", msg_id: str, msg_ts: str) -> None:
        """Forget a post's id, and leave a mark below it so a replay can still find it.

        The two go together, and separating them is the bug: forgetting the id alone makes
        the post retryable only for as long as nothing else succeeds, because the *next*
        accepted post moves the watermark past it and the next reconnect fetches from
        there. Whoever drops a message owns keeping it reachable.

        `claim_boundary` never narrows an open window, and falls through to a point just
        below this post because both marks are empty for the first delivery into a channel.
        """
        if msg_id:
            state.seen_ids_set.discard(msg_id)
            try:
                state.seen_ids.remove(msg_id)
            except ValueError:
                pass
        state.claim_boundary(state.last_processed_ts, just_before(msg_ts))

    def _release_turn_for(self, post: dict, result, generation: int, reason: str) -> None:
        """Give back the turn a post took, for a post that was not delivered.

        One place, because Mattermost has **seven** ways to decline a post after the
        filter has already charged it — the wake (the post is re-charged when the
        episode redelivers it), the no-router drop, the preflight for a replay, the
        preflight for live traffic, normalization raising, the handler raising, and
        the handler queue — and they were added one at a time. The count is in the
        comment because it was wrong here twice: it said three when three of six
        released, and six after the wake made it seven.
        """
        if not result.agent_chain_token or self._turn_store is None:
            return
        remaining = self._turn_store.release_turn(
            post.get("channel_id", ""), post.get("root_id") or None,
            result.sender, result.agent_chain_token, generation,
        )
        logger.debug(
            "Released an agent-chain turn for %s (%s) — now at %d",
            result.sender, reason, remaining,
        )

    @staticmethod
    def _remember_seen(state: _ChannelState, msg_id: str) -> None:
        state.seen_ids_set.add(msg_id)
        state.seen_ids.append(msg_id)
        if len(state.seen_ids) > _SEEN_IDS_MAXLEN:
            evicted = state.seen_ids.popleft()
            state.seen_ids_set.discard(evicted)
