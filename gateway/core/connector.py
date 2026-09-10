"""Connector abstraction layer: abstract base and normalized message models.

This module defines the platform-agnostic interface that all messaging platform
integrations must implement.  The core library (SessionManager, MessageProcessor)
only ever deals with the types defined here — it never imports anything
platform-specific.

Design influences:
  - OpenClaw (github.com/openclaw/openclaw): decomposed adapter pattern,
    send_text/send_media split, delivery_mode, text_chunk_limit.
  - matterbridge: minimal 4-method Bridger interface.
  - OpenClaw security note: "trusted sender id from inbound context —
    server-injected, must never be sourced from tool/model-controlled params."
    This is the principle behind format_prompt_prefix().
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal

from ..agents.response import AgentEvent, AgentResponse
from .bot_identity import BotIdentity  # noqa: F401 — used in an annotation

if TYPE_CHECKING:  # `watcher_manager` imports this module, so a runtime
    from .watcher_manager import RoomRef  # import here would cycle.

# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

class UserRole(str, Enum):
    """Access level assigned to a sender by the Connector (never by the core)."""

    OWNER = "owner"
    GUEST = "guest"
    ANONYMOUS = "anonymous"


# ---------------------------------------------------------------------------
# Normalized data models
# ---------------------------------------------------------------------------

@dataclass
class Attachment:
    """A platform file attachment already resolved to a local path on disk.

    The Connector is responsible for downloading the file before handing the
    IncomingMessage to the core.  The core passes local_path directly to
    AgentBackend.send() — it never fetches from the platform itself.
    """

    original_name: str
    local_path: str      # Absolute path, ready for AgentBackend.send(attachments=[...])
    mime_type: str = ""
    size_bytes: int = 0


@dataclass(frozen=True)
class HistoryPage:
    """One page of history, and whether the server had more to give.

    `raw_count` counts what the server returned *before* system and empty-body events were
    dropped, because the limit is applied before that filtering. A page of two hundred
    joins comes back as an empty `messages` list with `raw_count == limit`, and a caller
    that cannot tell that from a genuinely empty window will report an outage as read when
    every user message in it is still waiting behind that page.

    In `core` rather than beside either REST client because the distinction is AgentCoop's, not a
    platform's: every connector filters something out of a page it did not size, so every
    connector's replay can be handed an empty list that is not an empty window. *What* gets
    filtered stays per-platform — Rocket.Chat drops `t`-typed events, Mattermost drops
    `type`-tagged posts — and each client counts before its own filter.
    """

    messages: list[dict]
    raw_count: int
    limit: int

    @property
    def was_full(self) -> bool:
        return self.raw_count >= self.limit



@dataclass
class Room:
    """Platform-agnostic channel / conversation descriptor.

    id   — opaque platform identifier used when sending replies (RC room _id,
            Slack channel ID, Discord channel snowflake, etc.)
    name — human-readable label (#channel, @username, "script", …)
    type — "channel" | "group" | "dm" | "group_dm" | "thread" | "script".
            "group_dm" only ever comes from the creation path, which types the
            room from its classified kind (§2.2) — platform resolvers that
            cannot tell the two DM kinds apart keep answering "dm", and the
            mention gate treats only "dm" as exempt (§6.4).
    """

    id: str
    name: str
    type: str = "channel"


# The sender id `SessionManager.inject_message` stamps on a scheduled job's
# message. One name, so the connectors' prompt-prefix `to:` field and the turn
# runner can recognise a scheduled turn without three copies of a string.
SCHEDULER_SENDER_ID = "scheduler"


def is_scheduled_message(msg: "IncomingMessage") -> bool:
    """Did this message come from the scheduler rather than a person or an agent?

    Load-bearing for two decisions. The prompt prefix renders it `to: me` — a
    scheduled job is addressed to the agent it was created against, and a
    mention-derived `to: *` told the agent it was an unaddressed broadcast in a
    channel, which the routing rules say to answer with silence. And the turn
    runner warns when the reply to one is empty after stripping the termination
    token, because that silence was the whole symptom of a job "not working".
    """
    return msg.sender.id == SCHEDULER_SENDER_ID


@dataclass
class User:
    """Platform-agnostic sender descriptor."""

    id: str
    username: str
    display_name: str = ""


@dataclass
class IncomingMessage:
    """Normalized inbound message — the only form the core library ever sees.

    All platform-specific parsing (DDP field extraction, @mention stripping,
    attachment downloads, deduplication) happens INSIDE the Connector before
    this object is created.  The Connector also resolves sender identity to a
    UserRole so the core never touches raw platform user data.

    The raw field preserves the original platform payload for debugging or
    platform-specific downstream handling (e.g. Slack blocks, Discord embeds).
    """

    id: str                                        # Platform message ID (dedup key)
    timestamp: str                                 # ISO 8601 or sortable string
    room: Room
    sender: User
    role: UserRole                                 # Resolved by Connector — NOT by core
    text: str                                      # Cleaned body (mention prefix stripped)
    attachments: list[Attachment] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Human-readable warnings from the Connector (e.g. attachment download failures).
    # The core injects these into the agent prompt so the agent can inform the user.
    thread_id: str | None = None                   # Platform thread ID (RC tmid, etc.); None = top-level
    mentions: list[str] = field(default_factory=list)
    # Usernames explicitly @-mentioned in this message (sanitized, server-controlled).
    # Connectors populate this from the platform's mention metadata (e.g. RC's
    # ``mentions[]`` array).  Used by ``format_prompt_prefix`` to build the ``to:``
    # routing field so agents know whether a message is addressed to them or others.
    extra_context: dict[str, Any] = field(default_factory=dict)
    # Connector-computed behavioral hints (e.g. RC's "permission_thread_id").
    # Distinct from raw: raw holds the unmodified platform payload for debugging;
    # extra_context holds derived values the core/broker layer may act on.
    raw: dict[str, Any] = field(default_factory=dict)  # Original platform payload


# Handler type: the callback the core registers to receive inbound messages.
# Returns True if the message was accepted for processing, False if dropped
# (e.g. queue full).  Connectors use the return value to gate watermark advancement
# so that a dropped message is not silently marked as processed.
MessageHandler = Callable[[IncomingMessage], Awaitable[bool]]

class RoomCapacity(Enum):
    """Why a room can or cannot take a message right now.

    Lives here rather than beside the dispatcher because it is part of the *connector*
    contract — `dispatch.py` already imports this module, so the reverse would be a
    cycle, and the enum is what a connector is handed.
    """

    AVAILABLE = "available"   # a processor is running with queue space
    FULL = "full"             # a processor exists; its queue is full or it is draining
    UNROUTED = "unrouted"     # no processor serves this room


# Capacity check: a quick preflight, called BEFORE expensive work (normalize,
# attachment download) to short-circuit when a message cannot be accepted.
# (room_id) -> RoomCapacity. Three-valued rather than a bool because a room with no
# processor and a room whose queue is full call for different behaviour: the first is a
# routing miss, the second is backpressure, and reporting the first as the second made
# an idle gateway announce that it was busy (§2.7).
CapacityCheck = Callable[[str], RoomCapacity]


@dataclass(frozen=True)
class MembershipHook:
    """The callbacks a connector fires for the bot's own membership events (§2.7).

    `added` takes the classified room (a `RoomRef` — annotated loosely because
    `watcher_manager` imports this module); `removed` takes only the room id,
    since a room the bot was removed from may no longer be resolvable. A pair
    of named callables rather than two registration methods so a connector
    cannot end up holding one half of the contract.
    """

    added: Callable[[Any], Awaitable[None]]
    removed: Callable[[str], Awaitable[None]]


# Connector *types* whose transport delivers unsolicited inbound — the load-time
# twin of `Connector.supports_unsolicited_inbound()` below, which is the
# declaration; this is what the config loader can actually read.
#
# It has to be a set of type strings rather than a lookup through the connector
# classes: enforcement happens in `gateway/config.py`, which only ever sees a
# `ConnectorConfig` (a type string, no instance), and `gateway/connectors/` imports
# `gateway.config`, so reading the classes from there would invert the dependency
# and pull the whole websocket stack into `coop config validate`.
#
# Two declarations of one fact is the shape that has bitten this loader repeatedly,
# so they are bound by a test that walks every type the connector factory knows and
# asserts the set agrees with the class — rather than a comment asking the next
# person to remember. Membership (not absence) is the test, so an unrecognised type
# is restricted, matching the method's fail-closed default.
# Every connector type `connector_factory` knows how to build. Lives here rather
# than beside the factory because `gateway.config` needs it and
# `gateway.connectors` imports `gateway.config` — the other direction would be a
# cycle.
#
# The factory's error message is built from this tuple, and a test binds the two
# so a fifth connector type cannot be added to one and forgotten in the other.
# Order is the order a human should read them in, not alphabetical.
SUPPORTED_CONNECTOR_TYPES: tuple[str, ...] = (
    "rocketchat",
    "mattermost",
    "voice",
    "script",
)

TYPES_WITH_UNSOLICITED_INBOUND: frozenset[str] = frozenset({
    "rocketchat",
    "mattermost",
})


# ---------------------------------------------------------------------------
# Connector ABC
# ---------------------------------------------------------------------------

class Connector(ABC):
    """Abstract base for all messaging platform integrations.

    A Connector is responsible for:
      1. Authenticating and establishing the platform connection.
      2. Receiving inbound messages, normalizing them to IncomingMessage, and
         firing the registered handler.
      3. Delivering outbound text and media back to the platform.
      4. Resolving sender identity to a UserRole (RBAC lives here, not in core).
      5. Optionally downloading platform file attachments to local disk.

    Transport model
    ---------------
    Pull-based (WebSocket / polling):
        Connector drives the event loop internally and fires handler() when a
        message arrives.  Callers use connect() then the Connector self-runs.

    Push-based (webhook):
        Connector exposes handle_webhook(); an external HTTP server calls it for
        each inbound POST.  Both models share the same register_handler() API.

    Design notes (from OpenClaw study)
    ------------------------------------
    * send_text / send_media are separate methods — platforms differ
      significantly in how they deliver text vs files (e.g. Slack text API vs
      file upload API are completely different endpoints).
    * delivery_mode makes the transport model explicit for SessionManager.
    * text_chunk_limit carries the per-platform message size constraint so the
      core can split long responses without knowing the platform.
    * format_prompt_prefix() injects a server-controlled trusted header into the
      agent prompt.  Per OpenClaw: "server-injected, must never be sourced from
      tool/model-controlled params."  This is the RBAC security boundary.
    * Optional capabilities (attachments, media, webhooks) use non-abstract
      methods with sensible defaults rather than forcing every connector to stub
      out methods it cannot support.
    """

    # ── Lifecycle ────────────────────────────────────────────────────────────

    @abstractmethod
    async def connect(self) -> None:
        """Authenticate and establish the platform connection.

        WebSocket platforms : login via REST/auth API, open WebSocket.
        Webhook platforms   : start HTTP server, or no-op if server is external.
        Script connector    : no-op.

        Must be called once before the Connector can send messages or subscribe.

        **It does not, by itself, mean messages will arrive.** Startup is two phases —
        `connect()`, then `subscribe_room()` for each room, then `start_inbound()` — and
        a connector whose transport delivers every room the account can see defers
        reading until that last call, because events for a room it has no state for are
        discarded and never replayed. Callers embedding a connector directly must make
        that third call; `SessionManager` makes it at the end of its sync phase.
        """
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Graceful shutdown: close connections, cancel tasks, stop HTTP servers."""
        ...

    # ── Inbound — Observer pattern ───────────────────────────────────────────

    @abstractmethod
    def register_handler(self, handler: MessageHandler) -> None:
        """Register the callback the core uses to receive normalized messages.

        The Connector stores this and fires it for each valid inbound message,
        AFTER platform-level filtering (bot-self-filter, allowlist, @mention
        check, timestamp deduplication).

        Args:
            handler: Async callable that accepts an IncomingMessage.
        """
        ...

    def register_capacity_check(self, check: "CapacityCheck") -> None:
        """Register a preflight capacity check for two-phase inbound acceptance.

        Connectors that perform expensive work before dispatch (normalize,
        attachment download) should call this before the heavy phase to avoid
        wasting resources when the queue is already full.

        The default implementation is a no-op — connectors that don't perform
        expensive pre-dispatch work (e.g. ScriptConnector) need not override.

        The check returns a `RoomCapacity`, not a bool: `FULL` is backpressure and
        deserves the "server busy" reply, while `UNROUTED` means no watcher serves this
        room, which is not something to tell the room's members about.
        """

    async def membership_snapshot(self) -> set[str] | None:
        """Room ids this account is currently a member of, or None for unknown.

        The periodic membership reconciliation's probe (§2.7): a dormant
        record whose room id is absent from an answered snapshot has
        unambiguously lost its membership and is reclaimed. **None means the
        question could not be answered** — unsupported on this connector, or
        the lookup failed — and the caller must keep everything: an empty set
        is a claim ("member of nothing"), and a connector that cannot answer
        must never make it. The base returns None, so a connector without a
        membership stream needs no carve-out here either.
        """
        return None

    def register_membership_hook(self, hook: "MembershipHook") -> None:
        """Register the callbacks for the bot's own membership events (§2.7).

        Implemented only where a membership stream exists (Rocket.Chat's
        subscriptions-changed notification, Mattermost's user_added/user_removed
        events). The base is a no-op, so a connector without one needs no
        carve-out — the caller registers unconditionally alongside the router.

        The hook's `added` receives the room the bot was added to, classified
        exactly as a router offer would be; `removed` receives only the room id,
        because a room the bot was removed from may no longer be resolvable at
        all. Both fire for the *bot's own* membership only, never other users'.
        """

    # ── Outbound ─────────────────────────────────────────────────────────────
    # Inspired by OpenClaw's ChannelOutboundAdapter split of sendText / sendMedia.

    @abstractmethod
    async def send_text(
        self,
        room_id: str,
        response: AgentResponse,
        thread_id: str | None = None,
    ) -> None:
        """Deliver an agent response to the platform room.

        The ``response.text`` field carries the primary reply.  Implementations
        may also inspect other fields (``response.is_error``, ``response.usage``,
        etc.) to adjust formatting, add metadata footers, or post error notices.

        ``thread_id`` — when set, the reply is posted inside the given thread
        (e.g. RC's ``tmid`` field).  Connectors that do not support threading
        should accept and silently ignore this parameter.

        Implementations should respect ``text_chunk_limit`` and split long text
        if needed.  The ``room_id`` is the opaque platform ID from ``Room.id``.
        """
        ...

    async def send_to_room(
        self,
        room: str,
        text: str,
        attachment_path: str | None = None,
    ) -> None:
        """Send a message (and optional file attachment) to a room by name or ID.

        This is a high-level convenience method used by the CLI ``send`` command
        via the control socket.  It resolves room names, sends text via
        ``send_text``, and uploads attachments via ``send_media``.

        Subclasses may override for platform-specific optimizations; the default
        implementation delegates to resolve_room / send_text / send_media.

        Args:
            room           : Room name or opaque platform room ID.
            text           : Message body to send (may be empty when only an
                             attachment is provided).
            attachment_path: Optional absolute path to a local file to upload.
        """
        from ..agents.response import AgentResponse

        resolved = await self.resolve_room(room)
        room_id = resolved.id

        if attachment_path:
            await self.send_media(room_id, attachment_path, caption=text)
        elif text:
            response = AgentResponse(text=text, session_id="")
            await self.send_text(room_id, response)

    async def send_media(
        self,
        room_id: str,
        file_path: str,
        caption: str = "",
    ) -> None:
        """Upload a local file to the platform room with an optional caption.

        Default: raises NotImplementedError.
        Override in connectors that support file upload (RC, Slack, Discord, …).

        Args:
            room_id  : Opaque platform room ID.
            file_path: Absolute local path to the file to upload.
            caption  : Optional description / caption for the uploaded file.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support file upload"
        )

    # ── Room resolution ───────────────────────────────────────────────────────

    @abstractmethod
    async def resolve_room(self, room_name: str) -> Room:
        """Resolve a human-readable name to a platform Room object.

        RC     : channels.info / groups.info / im.create
        Slack  : conversations.info
        Script : in-memory Room(id=name, name=name, type="script")

        Args:
            room_name: Human-readable identifier e.g. "general", "@alice", "#dev".

        Returns:
            Populated Room dataclass with platform id, name, and type.
        """
        ...

    async def room_ref_by_id(self, room_id: str) -> "RoomRef | None":
        """The room this id names, described well enough to create a watcher for.

        The inverse of `resolve_room`, and the direction that survives a rename:
        a name freed by a rename can be reused by a different room, so anything
        rebuilding a watcher for a room it has seen before must ask by id. The
        record layer already works this way — `WatcherState.room_name` is
        documented "display only: resolution goes by `room_id`".

        Returns a `RoomRef`, not a `Room`, because creating a watcher needs the
        **kind** (it selects the label form and decides whether `require_mention`
        applies) and, for the DM kinds, the **participants** — a direct room has
        no name, so they are the only thing that identifies it to a human.

        **`None` means answered-and-absent, and an implementation must not raise
        instead.** No such room, one this connector does not serve, or one this
        account is no longer a member of: all three are final answers a caller
        acts on by giving up on this room. A TRANSPORT failure raises, because
        that is the one case where retrying later can change the answer (§2.2).
        Collapsing the two makes a deleted room look like a network blip
        forever — the same distinction `RocketChatRest.is_room_member` builds
        three answers for.

        The default is `None`: a connector that cannot look a room up by id
        cannot resurrect one, and its callers degrade rather than break.
        """
        return None

    # ── Per-room subscription (pull-based platforms) ─────────────────────────
    # Rocket.Chat DDP requires explicit per-room WebSocket subscriptions.
    # Slack / Discord / WhatsApp / webhook connectors: default no-op.

    async def subscribe_room(self, room: Room, **kwargs: object) -> None:
        """Subscribe to inbound messages for this room.

        RC: opens a DDP stream-room-messages subscription for room.id.
        Other platforms: no-op (their transport already covers all rooms).

        Extra keyword arguments (e.g. watcher_id, working_directory) are
        accepted and ignored by the default implementation.  RC's override
        uses them to set up per-room watcher state.
        """
        pass

    async def unsubscribe_room(self, room_id: str, watcher_id: str = "") -> None:
        """Unsubscribe from this room's message stream.

        RC: cancels the DDP subscription when the last watcher leaves the room.
        Other platforms: no-op.

        Args:
            room_id   : Opaque platform room ID.
            watcher_id: ID of the watcher that is unsubscribing.  Used by
                        connectors that track per-watcher state (e.g. RC) to
                        remove the correct watcher context while keeping the
                        DDP subscription alive for any remaining watchers.
        """
        pass

    def get_last_processed_ts(self, room_id: str) -> str | None:
        """Return the last processed message timestamp for a room, or None.

        Override in connectors that track per-room deduplication timestamps.
        Default: no-op (returns None).
        """
        return None

    def update_last_processed_ts(self, room_id: str, ts: str) -> None:
        """Update the deduplication timestamp for a room after processing.

        Override in connectors that track per-room deduplication timestamps.
        Default: no-op.
        """
        pass

    # ── Transport capability ──────────────────────────────────────────────────

    def supports_room_lookup(self) -> bool:
        """Return True if `room_ref_by_id` is a real lookup on this connector.

        The base `room_ref_by_id` answers `None` for a connector with no id
        lookup — "cannot resurrect, callers degrade" — and that `None` is
        indistinguishable from a real lookup's answered-and-absent. Callers that
        would act *destructively* on absence (boot reclaiming a record whose
        room the connector no longer serves, #141) ask this first and, without
        the capability, keep the pre-lookup behaviour instead of treating
        "cannot answer" as "gone". Non-destructive callers (a wake that simply
        does not recreate) need not ask.

        Default: False. Every shipped connector overrides `room_ref_by_id` and
        declares True; a test over `SUPPORTED_CONNECTOR_TYPES` holds that.
        """
        return False

    def supports_unsolicited_inbound(self) -> bool:
        """Return True if this transport delivers messages for rooms not asked for.

        The single property design §2.6 derives idle eligibility, eager-versus-lazy
        watcher creation and black-hole behaviour from, instead of branching per
        connector. Mattermost receives every channel the bot belongs to on one
        socket; Rocket.Chat can subscribe-all via ``__my_messages__``. Script's
        messages arrive by direct injection that bypasses the connector, and Voice's
        rooms arrive as HTTP path segments — neither has a stream to discover rooms
        from.

        Default: False. A connector that cannot discover rooms may only be given
        rules naming **literal** rooms, enforced at config load (see
        ``TYPES_WITH_UNSOLICITED_INBOUND``), so defaulting to False means a new
        connector type is restricted until it declares otherwise. That direction is
        deliberate: a pattern rule on a connector that cannot discover rooms fails
        *silently* — the rule simply never materializes — while the restriction
        applied wrongly fails *loudly*, at load, naming the field.
        """
        return False

    # ── Attachment support ────────────────────────────────────────────────────

    def supports_history(self) -> bool:
        """Return True if this connector can fetch channel message history.

        Used by ``_handle_fetch_history`` in the control socket to distinguish
        "connector doesn't support history" (hard error) from "empty channel"
        (both return ``[]`` from ``fetch_room_history``).

        Default: False.  Override in connectors that implement history fetch
        (e.g. RocketChatConnector).
        """
        return False

    async def probe_missed_since(self, room: Room, after_ts: str) -> bool:
        """Whether this room holds a message the gateway has not processed.

        The startup replay's cheap question, asked before a watcher is
        recreated (§2.2): recreating every recorded room at every boot would
        pay a session resume per room for nothing, which is precisely the eager
        cost the lazy model exists to avoid.

        Two exclusions decide the answer, and both need platform knowledge,
        which is why this lives on the connector rather than in the replay loop:

        * **The bot's own messages.** History includes them by design (the
          agent is shown what it said), but the watermark only advances on
          *accepted inbound*, so the agent's own last reply always sits above
          it — and a naive probe therefore reports a gap for every room that
          ended with the agent speaking, which is nearly all of them. Compared
          **by id, not by username**: an account whose canonical spelling
          differs from the configured one is a real and documented case here.
        * **The boundary message itself.** ``after_ts`` is an inclusive lower
          bound, so the very message that set the watermark comes back — and it
          is a user message, so the own-message rule does not remove it.

        ``after_ts`` is epoch milliseconds, like every timestamp inside coop
        (§5.2).

        Default: ``False`` — a connector with no history API has nothing to
        probe, and a startup replay over its records must be harmless.
        """
        return False

    async def replay_room_since(
        self, room_id: str, after_ts: str | None = None
    ) -> None:
        """Replay one tracked room's missed messages.

        The per-room half of the reconnect replay, exposed so other recoveries
        can drive it room by room (§2.2, "abort is only retryable if something
        replays"): reconnect iterates live subscriptions, startup iterates
        persisted records, and a recreation replays the interval its own room
        parked. This is the fetch-and-inject all three share. The room must
        already be tracked — recreation restores the watermark it reads.

        ``after_ts`` names the window explicitly; without it the room's own
        marks are used. A caller that names a window is asking about an
        interval it froze earlier, so the room's replay boundary is left
        undischarged — that mark belongs to the room's own accounting.

        Default: no-op. Connectors with no history API have nothing to replay,
        and a startup replay over their records must be harmless.
        """
        return None

    def trigger_history_bound(self, trigger: Any) -> str | None:
        """A router trigger frame's timestamp, for bounding history handoff.

        Epoch milliseconds as a string, like every timestamp inside AgentCoop (§5.2):
        its consumer compares it against a room's watermark and forwards it as
        a `fetch_room_history` bound, and both of those are epoch-ms.

        `register_router` passes the platform-native frame that prompted an offer;
        the creation path needs one thing from it — an exclusive upper bound for
        `fetch_room_history`, so the trigger itself is not fetched as history and
        then delivered a second time as the live prompt (§2.7). Each connector
        knows its own frame shape, which is why this lives here and not in the
        routing layer.

        Default: ``None`` (no bound) — connectors that never offer rooms to a
        router need not override it, and an unparseable frame answers None rather
        than raising, because the cost of no bound is one duplicated message
        while the cost of raising is a failed creation.
        """
        return None

    async def fetch_room_history(
        self,
        room: Room,
        count: int,
        before_ts: str | None = None,
        after_ts: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch recent channel history as normalized message dicts.

        Returns messages in chronological order (oldest first), already
        filtered to exclude anonymous/unlisted senders (same security
        boundary as live message processing).

        Each returned dict has the following keys:
            ts        : str | None  — ISO 8601 timestamp with UTC offset,
                        or ``None`` when the timestamp cannot be parsed.
            username  : str         — sanitized sender username.
            role      : str         — ``"owner"`` | ``"guest"`` | ``"agent"``
                        (``"agent"`` marks the bot's own prior responses).
            room_name : str         — sanitized room name.
            text      : str         — message body.

        Default: returns ``[]`` — connectors that do not support history
        (e.g. ScriptConnector) need not override this method.

        Args:
            room     : Resolved ``Room`` object (provides ``id`` and ``type``).
            count    : Maximum number of messages to retrieve.
            before_ts: Exclusive upper bound, **epoch milliseconds as a string**
                       — the internal representation for every timestamp
                       crossing an AgentCoop interface (§5.2). Only messages older
                       than it are returned. Maps to the platform's own
                       upper-bound parameter, which each connector converts to
                       if its API wants something else.
            after_ts : Inclusive lower bound, epoch milliseconds as a string.
                       Connectors that do not support it may silently ignore it.

        Note the asymmetry, which is deliberate: the *bounds* are epoch-ms
        because AgentCoop compares them, while the ``ts`` field of each returned dict
        is ISO because an agent reads it.
        """
        return []

    def supports_attachments(self) -> bool:
        """Return True if this connector can download platform file attachments.

        The Connector must download attachments to local disk before calling the
        handler, so the agent receives file paths it can read directly.
        """
        return False

    async def download_attachment(self, ref: dict[str, Any], dest_path: str) -> None:
        """Download a platform file attachment to a local absolute path.

        Args:
            ref      : Platform-specific reference dict (e.g. RC's files[] entry
                       with title_link, or Slack's file object with url_private).
            dest_path: Absolute local path to write the downloaded file.

        Default: raises NotImplementedError.
        Override in connectors that carry file attachments.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support attachment download"
        )

    def attachment_cache_dir(self, room_id: str) -> str | None:
        """Return the absolute path to the attachment cache directory for a room.

        Used by SessionManager to create per-watcher symlinks inside the agent's
        working directory, so the agent can read attachments without triggering
        out-of-project permission prompts.

        Default: None (no attachment caching).
        Override in connectors that download attachments to a global cache.
        """
        return None

    # ── Webhook entry point (push-based platforms) ────────────────────────────
    # The HTTP server lives OUTSIDE the connector (FastAPI, aiohttp, Flask, …).
    # The connector provides this single entry point; the server calls it.
    #
    # Platform signature algorithms:
    #   Slack     : HMAC-SHA256 of "v0:{timestamp}:{body}"  → X-Slack-Signature
    #   WhatsApp  : HMAC-SHA256 of raw body                 → X-Hub-Signature-256
    #   Discord   : Ed25519 of timestamp + body             → X-Signature-Ed25519
    #   Telegram  : token in URL path (no header signature)

    async def handle_webhook(
        self,
        headers: dict[str, str],
        body: bytes,
    ) -> dict[str, Any]:
        """Entry point for inbound webhook POST requests.

        The Connector must:
          1. Verify the platform's HMAC / Ed25519 signature.
          2. Handle one-time challenge handshakes (Slack URL verify,
             WhatsApp hub.verify_token, Discord PING → {"type": 1}).
          3. Parse the payload and emit normalized IncomingMessage(s) to handler.
          4. Return {"status": 200, "body": "OK"} or platform-required response.

        Raises:
            ValueError : If the signature verification fails.

        Default: raises NotImplementedError — WebSocket connectors don't use this.
        """
        raise NotImplementedError(
            f"{type(self).__name__} is not a webhook-based connector"
        )

    # ── Platform capability hints ─────────────────────────────────────────────
    # Inspired by OpenClaw's ChannelOutboundAdapter properties.

    @property
    def delivery_mode(self) -> Literal["direct", "gateway"]:
        """How outbound messages reach the platform.

        "direct"  — Connector sends directly via REST / API call.
        "gateway" — Connector proxies through an intermediary broker
                    (e.g. RC's DDP WebSocket gateway).

        Used by SessionManager to select timeout and retry strategies.
        """
        return "direct"

    async def start_inbound(self) -> None:
        """Begin consuming inbound events. Called after watchers are restored.

        Separate from `connect()` because authenticating and *receiving* are different
        moments, and a connector that starts both at once drops everything arriving
        before its rooms are known. Mattermost's socket delivers every channel the
        account can see and its handler discards events for channels with no state yet,
        so each such message is lost with no watermark to recover it from.

        The gap is not new — it already spanned each connector's own watcher restore,
        which creates sessions and fetches history — but the identity barrier widens it
        by every other connector's login, and this closes both: the socket is open
        during the wait, so the client library buffers what arrives, and the listen loop
        starts once the channels those events belong to exist.

        A no-op by default. A connector whose delivery is gated per room (Rocket.Chat
        subscribes room by room) has nothing to defer.
        """
        return None

    def bot_identity(self) -> "BotIdentity | None":
        """Who this connector is authenticated as, or ``None`` if it has no account.

        Called once after ``connect()`` and before any subscription, so that two
        connectors on one bot account are refused before either can start answering
        (§4.5). Override in every connector that authenticates as an account on a
        server other connectors could also reach.

        ``None`` is a claim, not a default to fall through: it says this connector has
        no shared account to collide over — a local stdin/stdout or script connector.
        `tests/unit/test_bot_identity_coverage.py` enumerates the connectors in this
        package and fails when a new one neither overrides this nor is listed there, so
        a platform connector cannot inherit the accountless answer by omission.

        Raise `ConnectorIdentityError` when the connector *does* have an account but
        cannot establish it — a whoami that failed, an id the login response omitted.
        Fail-closed: an unanswerable identity cannot be compared, and starting anyway is
        the situation this check exists to prevent.
        """
        return None

    @property
    def agent_username(self) -> str:
        """The bot's own username on this platform, or ``""`` if not applicable.

        Used to inject the agent's own identity into the session context so it
        can reason about messages addressed to it vs. other agents.  Override
        in connectors that authenticate with a platform username (e.g. RC, Slack).
        """
        return ""

    @property
    def timezone(self) -> str:
        """IANA timezone name for this connector, or ``""`` to use server local.

        Used to format per-message timestamps in the agent prompt prefix so
        agents see local time rather than UTC.  Override in connectors that
        expose a user-configurable ``timezone`` setting.
        """
        return ""

    @property
    def text_chunk_limit(self) -> int | None:
        """Maximum characters per outbound message, or None for no limit.

        Inspired by OpenClaw's ChannelOutboundAdapter.textChunkLimit.
        send_text() implementations should split responses that exceed this.

        Platform defaults:
            Rocket.Chat : ~40 000   Discord  : 2 000
            Slack       :  4 000   Telegram : 4 096
        """
        return None

    # ── Security: server-injected prompt prefix ───────────────────────────────

    def format_prompt_prefix(self, msg: IncomingMessage) -> str:
        """Return a trusted platform header to prepend to the agent prompt.

        This is the security boundary for RBAC enforcement.  The header is
        injected by the Connector (server-controlled) and parsed by CLAUDE.md
        instructions.  It must NEVER be derived from user-controlled content.

        Per OpenClaw: "trusted sender id from inbound context — server-injected,
        must never be sourced from tool/model-controlled params."

        RC returns : "[Rocket.Chat #general | from: alice | role: owner]"
        Others     : return "" (no prefix) or define their own convention.
                     Any new connector that uses RBAC MUST document its prefix
                     format in CLAUDE.md.
        """
        return ""

    # ── Optional status notifications ─────────────────────────────────────────

    async def notify_agent_event(
        self,
        room_id: str,
        event: AgentEvent,
        thread_id: str | None = None,
    ) -> None:
        """Handle an intermediate agent event during a turn.

        Called by :class:`~gateway.core.agent_turn_runner.AgentTurnRunner` for
        each non-``final`` :class:`~gateway.agents.response.AgentEvent` yielded
        by :meth:`~gateway.agents.AgentBackend.stream`.

        Default implementation: **no-op**.  Connectors that do not support live
        status updates (e.g. :class:`~gateway.connectors.script.connector.ScriptConnector`)
        need not override this method.

        Connectors that override (e.g. RC) can post a placeholder status message
        on the first call and update it on subsequent calls, giving users real-time
        visibility into what the agent is doing.

        Errors raised by this method are silently swallowed by the turn runner —
        a failed status update must never abort an agent turn.

        Args:
            room_id  : Opaque platform room ID.
            event    : Intermediate AgentEvent (``kind != "final"``).
            thread_id: Thread ID to post into, if applicable.
        """

    def on_agent_chain_drop(self, room_id: str, thread_id: str | None, sender: str) -> None:
        """Called when an agent chain LLM response is dropped (termination token detected).

        Connectors that support agent-to-agent communication override this to
        reset the sender's turn counter so future messages are not penalised.
        Default: no-op.
        """
        pass

    async def notify_typing(self, room_id: str, is_typing: bool) -> None:
        """Signal that the agent is (or has stopped) typing in a room.

        Called by MessageProcessor immediately before and after agent.send().
        Platforms that auto-clear the indicator (e.g. Telegram, 5 s TTL) can
        ignore the is_typing=False call; platforms that require an explicit
        clear (e.g. Rocket.Chat) should send it.

        Connectors that need to keep a long-running indicator alive (Telegram)
        should start/cancel an internal refresh loop here rather than expecting
        the caller to repeat the call.

        Default: no-op.  Override in connectors that support typing indicators.
        """
        pass
