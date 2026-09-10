"""Identity, labels and materialization for rule-derived watchers (design §2.3, §2.4).

A watcher is keyed by `(connector, room_id)` and nothing else. Three separate things are
derived from a room, and conflating any two of them has already been the source of real
defects, so they are named here once:

* **the key** — `(connector, room_id)`. Sticky: once a watcher exists for a key it stays
  bound to it until it expires, and between reconciliations editing or deleting the rule
  that created it neither rebinds nor destroys it (§2.4). A reconciliation — every boot,
  every `config reload`; see `reconcile.py` — re-matches the record against the current
  rules and re-materializes or expires it.
* **the label** — `<connector>-<room label>`, cosmetic, for display and CLI. Free to
  change, free to be ugly. It is deliberately *not* a path component and *not* a lookup
  key; filesystem paths key on a digest (`gateway/core/paths.py`) precisely so the label
  can stay cosmetic.
* **the room description** — a human-meaningful answer to "where does this watcher live",
  written into the materialized config's `room` field and re-supplied to the agent in its
  durable identity header on every turn.

Label and description coincide for a channel and **diverge for a group DM**: label
`gdm:a3f9c1b2…`, description `@alice, @bob`. Anything that treats `room` as a lookup key
breaks on that divergence, which is why room resolution goes by `room_id` (§2.3).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, fields, is_dataclass, replace
from typing import TYPE_CHECKING, Any

from .adapter_utils import ts_gt as _ts_gt
from .config import HistoryHandoffConfig, WatcherConfig
from .connector import Room
from .pending_route import STARTING_UP_NOTICE
from .room_pattern import RoomPattern
from .state import CONFIG_SCHEMA_VERSION, WatcherState, carried_fields, now_iso
from .watcher_rule import RoomKind, RuleMatch, WatcherRule

if TYPE_CHECKING:
    from .connector import Connector
    from .message_processor import MessageProcessor
    from .watcher_lifecycle import WatcherLifecycle

logger = logging.getLogger("coop.core.watcher_manager")

WatcherKey = tuple[str, str]  # (connector, room_id)


class StaleRecordError(RuntimeError):
    """A recreation's record was reclaimed or replaced while it waited (§2.5).

    Raised, not answered with None: None is a final decline and the routing
    episode would remember the trigger as one — but the room may now be
    recordless, where a fresh `_create` is the correct outcome and the frame
    is its trigger. An exception is a retryable abort (§2.2), so the episode's
    retry re-enters `get_or_create` and dispatches against current state.
    """

# How many hex characters of the room-id digest a group DM's label carries.
# Sixteen (64 bits), raised from the design's original eight (Codex round 20):
# the "collisions are cosmetic" rationale aged out when the same-name guard
# made the derived name a hard gate — at 32 bits a busy account's group DMs
# had a real birthday-bound chance of two rooms deriving one name, and the
# guard would then permanently refuse the second room's watcher. At 64 bits
# the bound is ~5 billion rooms for the same odds. Still short enough to
# paste from a `list` row, and `resolve()` accepts the room id besides.
_GROUP_DM_LABEL_DIGITS = 16

# Characters a label may carry verbatim. Anything else is percent-encoded (§2.3): the old
# sanitizer collapsed them to `-` and raised on an empty result, which made two different
# rooms label identically and could refuse to name a room at all.
_LABEL_SAFE = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")

# A display bound, not a correctness one — paths key on a digest, so nothing breaks if a
# label is long; a table column that is several hundred characters wide does. Over the cap
# the label is truncated and given a short room-id hash, so two long names that share a
# prefix stay distinguishable.
_LABEL_MAX = 48


@dataclass(frozen=True)
class RoomRef:
    """Everything creating a watcher needs to know about a room, resolved once.

    A struct rather than a room id plus a name, because creation needs the *kind* and,
    for a group DM, the *participants*: the kind selects the label form and decides
    whether `require_mention` applies, and for a group DM the participants are the only
    description of the room that exists (§2.8).

    `name` is the platform's own name and is empty for both DM kinds. `participants` is
    populated for the DM kinds and empty otherwise.
    """

    id: str
    kind: RoomKind
    name: str = ""
    participants: tuple[str, ...] = ()


def room_label(room: RoomRef) -> str:
    """The room half of a watcher's display label, per kind (§2.3).

    | kind | label | stable? |
    |---|---|---|
    | channel / private group | the channel name | until renamed |
    | 1:1 DM | `dm:<counterpart>` | until the counterpart is renamed (§2.3) |
    | group DM | `gdm:<16 hex of the room-id digest>` | yes, by construction |

    The kind prefixes use `:` — the reserved divider, never emitted by
    `_encode` (it is outside `_LABEL_SAFE`, so a literal `:` in a room or
    user name is percent-encoded) — which makes them unforgeable: a channel
    that happens to be NAMED `dm:alice` labels as `dm%3Aalice`, and can never
    collide with the DM for alice (Codex review of #121, round 7).

    **A group DM's members are deliberately not in its label.** The tempting alternative
    is Mattermost's `channel_display_name`, which *is* the member list — but it moves
    whenever membership does, it includes the bot's own name, its ordering is
    undocumented, and Rocket.Chat has no equivalent, so the two platforms would label one
    kind of room by different rules. A digest of the room id is identical on both, stable
    for the room's life, and short enough to type; the members belong in a `list` column,
    which is information about the room rather than its name.
    """
    if room.kind is RoomKind.DM:
        # One counterpart by definition. Falling back to the digest rather than raising:
        # a label is cosmetic, and refusing to name a room would be a worse failure than
        # naming it dully. The counterpart is encoded ALONE and the `dm:`
        # prefix attached outside the encoder, so the prefix stays literal
        # (and unforgeable) while the name part stays safe.
        counterpart = room.participants[0] if room.participants else _digest(room.id)
        return f"dm:{_encode(counterpart, room.id)}"
    if room.kind is RoomKind.GROUP_DM:
        return f"gdm:{_digest(room.id)}"
    return _encode(room.name, room.id) if room.name else _digest(room.id)


def watcher_label(connector: str, room: RoomRef) -> str:
    """`<connector>:<room label>` — the display and CLI handle (§2.3).

    Injective by construction (Codex review of #121, round 7 — the old `-`
    joiner was not: connector `rc` + room `home-general` and connector
    `rc-home` + room `general` both derived `rc-home-general`). Two
    guarantees make `:` a real boundary:

    * a connector name may not contain `:` (refused at config load), so the
      FIRST `:` in a watcher name always ends the connector component;
    * `:` is outside `_LABEL_SAFE`, so `_encode` percent-encodes it out of
      every room and user name — a literal `:` in the label portion can only
      be one this module wrote (the `dm:`/`gdm:` kind prefixes), never one a
      room name smuggled in.

    On Mattermost a channel name is unique only within a team, and one
    connector serves one team (§6.3), so the label portion is unique per
    connector; with the boundary unforgeable, the whole handle is unique
    across the deployment.
    """
    return f"{connector}:{room_label(room)}"


def room_description(room: RoomRef) -> str:
    """What `WatcherConfig.room` holds for a materialized watcher — never a pattern.

    This is the field the durable identity header renders as `- **Room:** …`, appended to
    the system prompt on every turn so it survives compaction. A rule-shaped value would
    permanently tell an agent its room is `eng-*`.

    For a group DM the participants *are* the description; there is nothing else to say.
    """
    if room.kind is RoomKind.GROUP_DM:
        return ", ".join(room.participants) if room.participants else room_label(room)
    if room.kind is RoomKind.DM:
        return room.participants[0] if room.participants else room_label(room)
    return room.name or room.id


def materialize(rule: WatcherRule, room: RoomRef) -> WatcherConfig:
    """Turn a rule plus a resolved room into the concrete config a watcher runs on (§2.4).

    Every field is carried across unchanged except the two that a rule cannot supply:

    * `name` → the derived label. A rule's `name` is the *rule's* identity, reused across
      every room it matches, so it cannot be a watcher's name.
    * `room` → the concrete description. Never the pattern.

    The rule's own lifecycle fields (`session_idle_days`, `session_expire_days`) are
    deliberately *not* copied here: they belong to the rule, and `WatcherConfig` has no
    home for them. The state record keeps the resolved rule alongside the materialized
    config for exactly this reason (§2.4, "two records, not one") — recreation reads the
    config, drift detection reads the rule.
    """
    return WatcherConfig(
        name=watcher_label(rule.connector, room),
        connector=rule.connector,
        room=room_description(room),
        agent=rule.agent,
        context_inject_files=list(rule.context_inject_files),
        history_handoff=replace(rule.history_handoff),
    )


def rule_snapshot(rule: WatcherRule) -> dict:
    """The rule as resolved at creation, in a JSON-safe form — the drift baseline (§2.4).

    **Derived from the dataclass, not from a hand-written field list.** The first version
    listed every field, and review found `history_handoff.max_fetch_count` missing: two
    rules differing only in that cap produced identical snapshots, so drift could not
    report it. A list that has to be updated whenever a field is added is the same shape
    as the hand-maintained type table `state.py` deliberately derives instead.

    `dataclasses.asdict` still cannot do it: a rule's patterns are `RoomPattern` objects (a
    `__slots__` class, compiled at load so an invalid pattern cannot reach the delivery
    path), which `asdict` leaves as objects — the record would fail to serialize on its
    first save. Patterns become the raw strings they were compiled from, which is also the
    only form an operator can compare against their own `config.yaml`.

    **Stored resolved, after `inherits:` has been applied.** Template inheritance is
    flattened at parse time, so the resolved form is what the parser naturally produces —
    and storing it means an edit to a watcher *template* registers as drift for free.
    Storing the raw YAML entry instead would let template changes escape detection.

    The materialized config cannot serve as this baseline: its `name` and `room` are
    overwritten by construction, so diffing it against a rule would report those two
    fields as changed every time.
    """
    return _jsonable(rule)


def _jsonable(value):
    """Recursively convert a rule into JSON-safe data, walking dataclass fields.

    Only one type needs special handling — `RoomPattern`, which is not a dataclass — so
    everything else is reached generically and a field added to any rule dataclass appears
    in the snapshot without this function changing.
    """
    if isinstance(value, RoomPattern):
        return value.raw
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def snapshot_digest(snapshot: dict) -> str:
    """A stable hash of a rule snapshot, for "has anything changed?" without a diff.

    Sorted keys and no whitespace, so the digest depends on content rather than on how
    the dict happened to be built. Cheap equality only — showing an operator *what*
    changed needs the full snapshot, which is why both are stored (§2.4).

    Not a substitute for the ownership check: under first-match precedence a rule inserted
    *above* mine starts winning for my room without any rule's content changing, so that
    is detected by re-running the match against the current ordered list, never by this.
    """
    return hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def first_matching_rule(
    rules: list[WatcherRule], connector: str, room: RoomRef
) -> WatcherRule | None:
    """The rule that owns this room, or None if no rule claims it (§2.2).

    **First match in config order wins, and a decline halts the search.** `except_for`
    produces `DECLINED` rather than falling through, so a room an earlier rule excludes
    does not reach a later one — a deny rule (`include: [X]`, `except_for: [X]`) shadows
    later rules for X completely, which is its purpose. Returning the first *claim* while
    letting a decline fall through would quietly invert that.

    Rules for other connectors are skipped rather than matched-and-discarded: a rule's
    `connector` is part of what it claims, not a filter applied afterwards.

    **A named room with no name matches nothing**, rather than falling back to its id.
    An earlier version substituted `room.id`, so a room whose opaque id happened to look
    like a configured pattern could be claimed — or excluded — on the strength of a string
    the operator never wrote and cannot see. `room_label` still falls back to a digest for
    such a room, because a label is cosmetic; a *match* is not.
    """
    if room.kind not in (RoomKind.DM, RoomKind.GROUP_DM) and not room.name:
        return None

    for rule in rules:
        if rule.connector != connector:
            continue
        # The name is passed as-is, including the empty string a DM carries: `match()`
        # short-circuits on kind for both DM kinds before consulting any pattern, so a
        # pattern cannot claim a DM and a DM opt-in cannot claim a channel. An earlier
        # version passed the *label* for group DMs, to avoid a pattern matching a digest —
        # a branch whose premise the matcher makes impossible, found by injecting the
        # fault and watching nothing fail.
        verdict = rule.match(room.name, room.kind)
        if verdict is RuleMatch.CLAIMED:
            return rule
        if verdict is RuleMatch.DECLINED:
            return None
    return None


def _encode(raw: str, room_id: str) -> str:
    """A label component made safe to print and to type, per §2.3.

    Percent-encoding rather than the old collapse-to-`-`: that mapped every unsafe
    character onto one, so two different rooms could produce identical labels, and it
    raised when nothing survived. This never raises and is reversible by eye.

    Platforms differ in what they allow — Rocket.Chat and Mattermost emit slugs, but the
    voice connector takes its room from a URL path, so `/ask/a/b` yields the room `a/b`,
    and a raw label would carry a character the static config path rejects in a watcher
    name. Encoding here keeps one handle syntax across both shapes.

    Over the length cap the label is truncated and a short room-id hash appended, so two
    long names sharing a prefix remain distinguishable.
    """
    encoded = "".join(
        c if c in _LABEL_SAFE else "".join(f"%{b:02X}" for b in c.encode())
        for c in raw
    )
    if len(encoded) <= _LABEL_MAX:
        return encoded
    keep = _LABEL_MAX - _GROUP_DM_LABEL_DIGITS - 1
    return f"{encoded[:keep]}-{_digest(room_id)}"


def _digest(room_id: str) -> str:
    """Stable short hex for a room id.

    Its own function rather than a call to `gateway/core/paths.py`'s keying: that digest
    is a **filesystem key** and must never change, while this one is cosmetic. Sharing
    them would tie a display decision to a value that names files on disk.
    """
    return hashlib.sha256(room_id.encode()).hexdigest()[:_GROUP_DM_LABEL_DIGITS]


def rule_bound_fields(
    wc: WatcherConfig,
    rule: WatcherRule,
    *,
    connector_name: str,
    agent_name: str,
) -> dict[str, Any]:
    """The frozen fields a record derives from its rule (§2.4).

    The part a reconciliation rewrites when a different rule, or a changed
    one, wins the room. One writer for creation and re-materialization: two
    hand-built dicts drift the day a field is added.
    """
    return {
        "connector": connector_name,
        "agent": agent_name,
        "config": _jsonable(wc),
        "rule_name": rule.name,
        "rule": rule_snapshot(rule),
        "config_schema_version": CONFIG_SCHEMA_VERSION,
    }


def creation_provenance(
    wc: WatcherConfig,
    rule: WatcherRule,
    room: RoomRef,
    *,
    connector_name: str,
    agent_name: str,
    now: str,
    dropped_at: str = "",
) -> dict:
    """The §5.3 fields only the moment of creation knows (§2.4).

    One construction site for both creators — the message-path `_create` and
    the membership-add `register_on_join` — because two hand-built dicts drift
    the day a field is added, and the field they drift on is exactly the one a
    recreation then silently fails to carry. `dropped_at` is the only
    difference between the two: a message-path creation starts active (""),
    a join registers idle (stamped with the join time), and the expiry timer
    runs from that stamp.
    """
    return {
        "room_kind": room.kind.value,
        "participants": list(room.participants),
        "created_at": now,
        "last_activity_at": now,
        "dropped_at": dropped_at,
        **rule_bound_fields(wc, rule, connector_name=connector_name, agent_name=agent_name),
    }


def config_from_record(record: WatcherState) -> WatcherConfig | None:
    """Rebuild the materialized config a record was persisted with (§2.4).

    Recreation reads the record, never the current rule — that is what sticky
    binding means. The record's `config` was written by `_jsonable(materialize(...))`,
    so the shape is ACG's own; still, every read below tolerates absence and wrong
    types by returning None rather than raising, because the caller's correct answer
    to an unreadable record is "decline to recreate and log", not a traceback on the
    routing path. A record with no `config` at all is the static model's (its
    recreation source is `config.yaml`) and returns None for the same reason.
    """
    raw = record.config
    if not isinstance(raw, dict) or not raw:
        return None
    if not isinstance(raw.get("name"), str) or not raw["name"]:
        return None

    def _str(key: str) -> str:
        value = raw.get(key)
        return value if isinstance(value, str) else ""

    hh_raw = raw.get("history_handoff")
    hh_kwargs = {}
    if isinstance(hh_raw, dict):
        for f in fields(HistoryHandoffConfig):
            if f.name in hh_raw and isinstance(hh_raw[f.name], type(f.default)):
                hh_kwargs[f.name] = hh_raw[f.name]
    files = raw.get("context_inject_files")
    # Identity fields rebuild from the record's TOP-LEVEL frozen columns,
    # unconditionally (Codex rounds 14 and 15): the nested copies are inside
    # a dict whose values `_record_from_dict` cannot type-check, so a
    # hand-edited or value-corrupted `config.name`/`config.agent` — absent,
    # wrongly typed, OR a valid-but-different value — must never win.
    # A different nested NAME started and stored the watcher under a second
    # record for the same room, and expiring the original then deleted the
    # backend session the new one was using; a different nested AGENT ran
    # the room under another backend and tool policy, silently. The
    # top-level columns are what every other consumer (reclaim's identity
    # checks, the uniqueness preflight, validate's warnings) already treats
    # as authoritative, and materialize writes both copies from the same
    # rule, so they agree on every honest record. `raw["name"]` above stays
    # as the is-this-config-readable gate only.
    return WatcherConfig(
        name=record.watcher_name,
        # Round 16 extended the same attack to connector and room: a
        # valid-but-conflicting `config.connector` injected the wrong
        # connector's context and keyed the prompt/attachment workspace
        # under a connector reclamation would never clean (reclaim reads
        # the top-level column). Column-first for both, like name/agent —
        # `record.room_name` is the same creation-time description
        # materialize wrote into the nested copy, and the wake's naming
        # already prefers it (round 6).
        connector=(record.connector
                   if isinstance(record.connector, str) and record.connector
                   else _str("connector")),
        room=(record.room_name
              if isinstance(record.room_name, str) and record.room_name
              else _str("room")),
        agent=(record.agent if isinstance(record.agent, str) and record.agent
               else _str("agent")),
        context_inject_files=[p for p in files if isinstance(p, str)]
        if isinstance(files, list) else [],
        history_handoff=HistoryHandoffConfig(**hh_kwargs),
    )


class ReloadInProgressError(RuntimeError):
    """A room offer arrived while `config reload` applies — retryable (#144)."""


class WatcherManager:
    """The runtime half of §2.8: rule-derived creation on the message path.

    One instance per connector, alongside that connector's `WatcherLifecycle` —
    the lifecycle owns the start machinery and the state dicts; this class owns
    the *decision* to create (rule match, sticky binding, single-flight, the
    concurrent-creation cap) and the §5.3 record fields only a creation knows
    (rule snapshot, room kind, participants, creation time).

    Deliberately narrow for now: `get_or_create` is the message path and is the
    only caller that exists. The remaining §2.8 surface (`get` for injection and
    scheduled jobs, the operator verbs, `resolve`) lands with the increments
    that call it — shipping it earlier would be interface with no consumer,
    which this series has been corrected on twice.
    """

    def __init__(
        self,
        connector_name: str,
        connector: "Connector",
        lifecycle: "WatcherLifecycle",
        rules: list[WatcherRule],
        *,
        creation_cap: int = 4,
    ) -> None:
        self._connector_name = connector_name
        self._connector = connector
        self._lifecycle = lifecycle
        self._rules = rules
        # Per-room single-flight (§2.7 step 4). The connectors' own
        # `_rooms_being_routed` sets narrow the window but do not close it: they
        # are released before creation's awaits finish on RC's routing workers,
        # and this lock is what actually covers "existence check + creation" as
        # one critical section. Entries are never removed — the map grows with
        # distinct rooms offered, which is bounded by rooms the account can see.
        # The expiry increment considered shrinking it and ruled it unsafe: an
        # asyncio.Lock briefly reads unlocked during the release-to-waiter
        # handoff, so a delete in that window lets a fresh setdefault mint a
        # second lock and two creations run the "single"-flight section at
        # once. A safe shrink needs refcounting; bounded growth does not earn
        # it. (The lifecycle's `_watcher_locks` map holds by the same
        # reasoning.)
        self._locks: dict[str, asyncio.Lock] = {}
        # A soft cap, checked-then-incremented under the per-room lock. Two
        # rooms' creations can race the check and both proceed; the cap bounds
        # pile-ups, it does not ration exactly (§2.7 step 7).
        self._creation_cap = creation_cap
        self._creations_in_flight = 0
        # Set by SessionManager.shutdown before the teardown begins. The wake
        # arms stay physically reachable until the connector disconnects — an
        # idle room is subscribed with no processor for that whole span, so its
        # messages read UNROUTED and would recreate a watcher *during* the
        # teardown: never drained, absent from stop_all's snapshot, and its
        # save_state rewrites the state file after the final save. Once set,
        # every offer answers None — a final decline, so the declined drain
        # drops and remembers the frames, and the watermark stays put for the
        # next boot's replay to recover them.
        # No flag of its own (structural close): `_shutting_down` reads the
        # lifecycle's single transition flag, so the manager's episodes and
        # the lifecycle's verbs are refused by ONE write at ONE instant —
        # a path checking "a different flag set later" was rounds 4/5/9's
        # recurring hole.

        # In-flight creation/recreation/registration episodes (Codex round 5,
        # P1): the disarm flag stops NEW episodes, but one already inside
        # `start_watcher_in_room` — awaiting session creation, history, or
        # context setup — installs its processor after `stop_all` snapshots
        # `_processors`, and is then never stopped. `drain()` waits these out.
        # The event starts SET: zero in-flight means nothing to wait for.
        self._inflight = 0
        self._drained = asyncio.Event()
        self._drained.set()

    def _enter_episode(self) -> None:
        """MUST be called in the same synchronous segment as the entry disarm
        check — an await between the check and this increment would let an
        episode go invisible to `drain()`."""
        self._inflight += 1
        self._drained.clear()

    def _exit_episode(self) -> None:
        self._inflight -= 1
        if self._inflight == 0:
            self._drained.set()

    def _park_if_reloading(self, room_id: str) -> None:
        """Make an offer PARK, not decline, while a reload applies (#144).

        A disarmed manager answers `None` — a completed decline — and the
        connectors then remember every buffered id, which is right for
        shutdown (the next boot replays from the watermark) and wrong for a
        reload: the kept connector's dedup set survives, so an idle room's
        wake would have its id remembered and the next wake's replay die on
        it. Raising instead makes the offer an abort: the connector retries
        for a few seconds (the apply window is usually shorter) and, if the
        reload is still running, parks the room with its ids unknown so the
        next wake's replay brings the frames back.
        """
        if self._lifecycle.disarmed_for_reload:
            raise ReloadInProgressError(
                f"Not creating a watcher for room {room_id} yet — "
                f"{self._lifecycle.disarm_reason}")

    def replace_rules(self, rules: list[WatcherRule]) -> None:
        """Swap the ordered rule list a `config reload` changed (#144).

        Called on a stilled manager (disarmed and drained): the next room
        offered is matched against the new list, and nothing is mid-match.
        """
        self._rules = list(rules)

    def disarm(self, reason: str | None = None) -> None:
        """Refuse every offer from now on — called by shutdown, before any stop.

        The wake arms stay reachable until the connector disconnects, so
        without this a message landing mid-teardown recreates a watcher the
        teardown will never stop (§2.5). `reason` is what a refused caller
        is told; the default is the lifecycle's shutdown wording.
        """
        if reason is None:
            self._lifecycle.disarm_transitions()
        else:
            self._lifecycle.disarm_transitions(reason)

    async def drain(self, reason: str | None = None) -> None:
        """Disarm, then wait for every in-flight episode to finish (Codex
        round 5). Composes with the under-lock disarm re-checks (round 4):
        an episode parked on a lock wakes, sees the flag, and exits via its
        `finally` instead of completing a full start — so this wait is short
        for parked episodes and bounded by creation latency (10–40s) for one
        already inside `start_watcher_in_room`. That wait is the decision:
        the alternative is a processor `stop_all` never saw. A hung backend
        is the daemon-level grace window's problem, not this method's."""
        self.disarm(reason)
        await self._drained.wait()

    @property
    def _shutting_down(self) -> bool:
        return self._lifecycle.transitions_disarmed

    @property
    def disarmed(self) -> bool:
        """Whether shutdown has begun. The membership handlers read this: an
        event landing mid-teardown must neither register a record after the
        final save nor reclaim one stop_all is dismantling — a skipped event
        is re-discovered (the add by the room's first message, the remove by
        the reconciliation), which is what idempotent handling is for."""
        return self._shutting_down

    async def get_or_create(
        self,
        connector: str,
        room: RoomRef,
        *,
        history_before_ts: str | None = None,
        expected_record: "WatcherState | None" = None,
    ) -> "MessageProcessor | None":
        """A ready watcher for this room, created from the first matching rule
        if the room has never had one (§2.8). None means "no watcher": no rule
        claims the room, its record is paused, or creation is over the cap.

        `history_before_ts` bounds a new session's history handoff strictly
        below the triggering message, so the trigger is not delivered twice.

        **None is a final answer; an exception is a retryable one (§2.2).**
        None means the decision was made and it was "no watcher" — a rule miss
        and a paused record are completed decisions, and re-asking cannot
        change them. A creation or recreation that *raises* is the opposite:
        the decision was never carried out, the message must stay eligible for
        redelivery, and the caller owns the retry. Collapsing the two shapes
        into None made the transaction's abort outcome unreachable, which is
        why this method deliberately does not catch what the start raises.
        The cap refusal answers None too: it already produced a visible
        "starting up" notice, and the next message re-asks.
        """
        if connector != self._connector_name:
            # A wiring error, not a routing outcome — each connector's router
            # closure names its own connector.
            logger.error(
                "get_or_create for connector %r reached the manager for %r",
                connector, self._connector_name,
            )
            return None
        self._park_if_reloading(room.id)
        if self._shutting_down:
            # Disarmed (see __init__): a creation mid-teardown outlives every
            # stop that already ran. Final, not retryable — the daemon is
            # exiting, and the message stays below the watermark for the next
            # boot to replay.
            logger.info(
                "Not creating a watcher for room %s — the gateway is shutting down",
                room.id,
            )
            return None

        # Same synchronous segment as the disarm check above — an await
        # before this increment would let the episode go invisible to drain().
        self._enter_episode()
        try:
            lock = self._locks.setdefault(room.id, asyncio.Lock())
            async with lock:
                self._park_if_reloading(room.id)
                if self._shutting_down:
                    # Re-checked UNDER the lock (TOCTOU sweep after Codex round
                    # 4): an episode that passed the check above and then parked
                    # on this lock — or on the watcher lock inside `_recreate` —
                    # can be released BY the shutdown itself (the sweep's stop
                    # cancels the drop that held it), and a start proceeding then
                    # is a processor `stop_all` never saw, an online notification
                    # posted mid-exit, and a save after the final save.
                    logger.info(
                        "Not creating a watcher for room %s — the gateway began "
                        "shutting down while this episode waited", room.id,
                    )
                    return None
                record = self._lifecycle.record_for_room(room.id)
                if expected_record is not None and record is not expected_record:
                    # The boot loops' identity pin (Codex round 11): they walk
                    # a SNAPSHOT of hydrated records, and a live membership
                    # removal can reclaim one mid-walk — the re-read here then
                    # finds the room recordless and _create would RESURRECT a
                    # watcher for a room the bot just left, active until it
                    # idles into the reconciliation's scope. A message-path
                    # caller passes nothing and follows current state, which
                    # is its correct semantics (same split as reclaim_room's
                    # `expected=`).
                    logger.info(
                        "Room %s: its record changed since the boot snapshot "
                        "— not recreating from stale evidence", room.id,
                    )
                    return None
                if record is not None:
                    resident = self._lifecycle.processor_named(record.watcher_name)
                    if resident is not None:
                        return resident
                    return await self._recreate(record, room, history_before_ts)
                return await self._create(room, history_before_ts)
        finally:
            self._exit_episode()

    async def register_on_join(self, room: RoomRef) -> str | None:
        """A membership-add registers the room's record in `idle` state (§2.7).

        The rule is matched and snapshotted at join time, the config is
        materialized, and **nothing is started** — no session, no history
        handoff, no subscription. Starting would pay the eager cost this
        design exists to avoid, for every room the bot is added to including
        ones never used. The room becomes listable and addressable
        immediately; its first message wakes it through the normal untracked
        path, whose episode finds the record and takes `_recreate`.

        A supplement, never a replacement: an add event that arrives during a
        disconnect is simply gone (Mattermost's socket has no replay), so
        message-triggered creation stays the safety net and this method's
        absence changes nothing but visibility.

        Under the same per-room lock as `get_or_create`, because an add event
        and the room's first message can arrive near-simultaneously and that
        lock is what makes "existence check + create" one critical section.
        A room that already has a record is a no-op — a duplicate add must
        not restamp clocks or re-snapshot the rule (§2.4, sticky binding).

        The registered record inherits full idle semantics deliberately: a
        room nobody ever speaks in expires `session_expire_days` after the
        join, reusing the idle state rather than inventing a fourth one.
        Returns the registered name, or None for every no-op.
        """
        if self._shutting_down:
            return None
        # Same synchronous segment as the check above (see get_or_create):
        # a registration writes a record and saves, so it is an in-flight
        # transition drain() must see.
        self._enter_episode()
        try:
            lock = self._locks.setdefault(room.id, asyncio.Lock())
            async with lock:
                if self._shutting_down:
                    return None
                if self._lifecycle.record_for_room(room.id) is not None:
                    return None
                rule = first_matching_rule(self._rules, self._connector_name, room)
                if rule is None:
                    logger.debug(
                        "Membership add for room %s matches no rule — not registered",
                        room.id,
                    )
                    return None
                wc = materialize(rule, room)
                now = now_iso()
                provenance = creation_provenance(
                    wc, rule, room,
                    connector_name=self._connector_name,
                    agent_name=self._lifecycle.resolve_agent_name(wc.agent),
                    now=now,
                    # Registered idle: the record's expiry clock starts at the join.
                    dropped_at=now,
                )
                platform_room = Room(
                    id=room.id,
                    name=room_description(room),
                    type=room.kind.value,
                )
                async with self._lifecycle.watcher_lock(wc.name):
                    self._lifecycle.register_idle_record(wc, platform_room, provenance)
                logger.info(
                    "Registered watcher '%s' idle for room %s from rule '%s' "
                    "(membership add) — its first message starts it",
                    wc.name, room.id, rule.name,
                )
                return wc.name
        finally:
            self._exit_episode()

    async def _recreate(
        self,
        record: WatcherState,
        room: RoomRef,
        history_before_ts: str | None,
    ) -> "MessageProcessor | None":
        """Sticky binding (§2.4): a room with a record is recreated from its own
        persisted config; the current rules are never consulted."""
        if record.paused:
            # An explicit pause is never overridden by inference (§4.4). The
            # message is deliberately dropped, not deferred.
            logger.debug(
                "Room %s has a paused record ('%s') — not recreating",
                record.room_id, record.watcher_name,
            )
            return None
        wc = config_from_record(record)
        if wc is None:
            # A static-model record (no frozen config). Its recreation source is
            # config.yaml and its owner is sync_watchers' retry-at-every-start
            # rule — recreating it from here would invent a second owner.
            logger.debug(
                "Room %s's record ('%s') carries no materialized config — "
                "leaving it to the static path",
                record.room_id, record.watcher_name,
            )
            return None
        platform_room = Room(
            id=record.room_id,
            # The record's own name first, for the same §2.4 reason as the
            # kind below (Codex round 6): a wake from tracked state offers a
            # ref with no participants, so deriving the name from it degrades
            # a DM's description to the dm-/gdm-digest and overwrites the
            # meaningful room_name the creation wrote. A platform-side rename
            # reaches the record through `WatcherLifecycle.observe_room_name`
            # on the next frame, which also re-derives the handle.
            name=record.room_name or room_description(room),
            # The record's own kind, not the offered room's: the record is what
            # this watcher was created against (§2.4), and a re-classification
            # that disagreed would silently change whether the mention gate
            # applies to a room that already has a watcher.
            type=record.room_kind or room.kind.value,
        )
        # A raise propagates: recreation that failed is an abort, not a decision,
        # and the caller owns the retry (§2.2). The per-room lock releases on the
        # way out, so the retry can re-enter.
        #
        # Under the lifecycle's per-watcher lock (§2.5): a pause and an idle drop
        # both remove the processor first and settle the record last, holding
        # this lock for the span — and the wake this method is means a message
        # can arrive exactly mid-drain. Without the lock the recreation runs
        # against the state object the teardown is still dismantling, and the
        # teardown's last step removes the session binding just made. Waiting
        # is correct in both cases: after a pause the record reads paused and
        # the check below declines; after an idle drop the recreation proceeds
        # against a settled record. Room lock outer, watcher lock inner —
        # nothing takes them reversed.
        async with self._lifecycle.watcher_lock(record.watcher_name):
            self._park_if_reloading(record.room_id)
            if self._shutting_down:
                # The inner half of the get_or_create re-check (TOCTOU sweep
                # after Codex round 4): a wake parked on THIS lock while the
                # sweep's drop held it is released by the shutdown cancelling
                # the sweep — and the drop mutates the record in place, so the
                # staleness check below cannot catch it.
                logger.info(
                    "Not recreating watcher for room %s — the gateway began "
                    "shutting down while its wake waited", record.room_id,
                )
                return None
            if self._lifecycle.record_for_room(record.room_id) is not record:
                # The record this wake was dispatched against is no longer the
                # room's record — an expiry reclaimed it (or a later creation
                # replaced it) while we waited on the lock. One rule, no cases:
                # raise, and the caller's retry re-enters `get_or_create`,
                # which re-reads and dispatches correctly whatever the room's
                # state is now — `_create` for a reclaimed room, the resident
                # processor for a replaced one. Proceeding here instead would
                # resurrect a record the expiry just deleted, session and all.
                raise StaleRecordError(
                    f"room {record.room_id}'s record changed while its "
                    f"recreation waited — retry re-reads"
                )
            if record.paused:
                # Re-read under the lock: the pause that held it just settled.
                logger.debug(
                    "Room %s was paused while its wake waited — not recreating",
                    record.room_id,
                )
                return None
            # Everything a start does not rebuild, carried out of the record being
            # recreated — derived from the field classification in `state.py`, not
            # listed here, so a new §5.3 field survives recreation without this line
            # changing. Read under the lock, and that is load-bearing for the
            # watermark below: a teardown this wake waited on captures the live
            # watermark into the record as one of its steps, and a boundary read
            # before the lock would replay from the stale mark.
            #
            # `last_activity_at` is carried, NOT re-stamped. A recreation is
            # residency, not activity: the boot evaluation routes every
            # was-active record through here at every start, and a stamp at
            # this line is exactly the "boot-time mutation of last_activity_at"
            # §2.5 condemns — it made the record claim activity at a moment
            # there was none, so a deployment restarted more often than its
            # idle TTL never idled anything, silently. When a recreation *is*
            # activity — a wake, a replay with messages waiting — the message
            # that caused it is enqueued moments later, and `enqueue` is the
            # clock's one advancing write site.
            carried = carried_fields(record)
            carried["dropped_at"] = ""
            # The window this recreation owes the room. Read before the start, which
            # restores it into the connector and then advances it as the replayed
            # and live messages commit.
            boundary = record.last_processed_ts
            await self._lifecycle.start_watcher_in_room(
                wc, record, platform_room,
                # The record's own watermark bounds the handoff when the backend has
                # expired the session and a fresh one has to be minted: without it the
                # unbounded fetch pulls in the very interval the replay below is about
                # to deliver, and the agent sees it twice.
                #
                # The **lower** of the two bounds wins, and that is not the same as
                # "the trigger's, if there is one". `before_ts` is an exclusive upper
                # bound, so a *lower* value fetches less; the trigger is by
                # construction a message above the watermark, so preferring it would
                # pick the looser bound and re-admit the whole interval — the exact
                # double delivery this argument exists to prevent, on the common path.
                history_before_ts=_earlier(history_before_ts, boundary),
                provenance=carried,
            )
            self._lifecycle.save_state()

        # Everything above the record's watermark, replayed through the normal
        # pipeline. This is what makes an abort recoverable for a room that has
        # a record (§2.2): the routing episode that parked, or the buffer that
        # overflowed, left its frames below this boundary, and nothing else
        # would ever return to them — the reconnect replay iterates *tracked*
        # rooms and a parked room is untracked, and the next live message's
        # commit would seal the interval by advancing the watermark past it.
        #
        # Best-effort: the room is up either way, and a replay that fails must
        # not undo a successful recreation. Bounded by the record's own mark,
        # so the room's replay boundary is not spent by a window it did not set.
        if boundary:
            try:
                await self._connector.replay_room_since(record.room_id, after_ts=boundary)
            except Exception as e:
                logger.warning(
                    "Watcher '%s' is up, but replaying room %s from %s failed — "
                    "messages from before it may stay undelivered: %s",
                    wc.name, record.room_id, boundary, e,
                )
        return self._lifecycle.processor_named(wc.name)

    async def _create(
        self,
        room: RoomRef,
        history_before_ts: str | None,
    ) -> "MessageProcessor | None":
        """First-ever watcher for this room: match, cap, materialize, start,
        and freeze the §5.3 record fields only this moment knows."""
        rule = first_matching_rule(self._rules, self._connector_name, room)
        if rule is None:
            return None

        if self._creations_in_flight >= self._creation_cap:
            # §2.7 step 7: queue depth bounds messages per room; this bounds
            # rooms being created at once. The honest answer is a visible
            # "starting up", not a silent drop — the sender watched their
            # message arrive.
            logger.warning(
                "Creation cap (%d) reached — deferring watcher creation for "
                "room %s", self._creation_cap, room.id,
            )
            try:
                # send_text, not send_to_room: the latter resolves its argument
                # as a room *name*, and all this layer holds is the opaque id.
                from ..agents.response import AgentResponse

                await self._connector.send_text(
                    room.id, AgentResponse(text=STARTING_UP_NOTICE))
            except Exception:
                logger.debug("Could not post the starting-up notice", exc_info=True)
            return None

        # Someone is answering. A creation — session provisioning, history
        # handoff — is the longest silence a sender ever sees after their
        # message, and it happened with no sign that anything was listening
        # (owner, 2026-09-03). Sent only once a rule has claimed the room, so a
        # room the bot would decline gets no hint that it is here. A hint, never
        # a step: a connector that cannot send one changes nothing. Mattermost
        # clears the indicator on its own after a few seconds; Rocket.Chat's is
        # a toggle, so a failed creation below switches it off — a successful
        # one hands the trigger message to a turn, whose runner owns it.
        try:
            await self._connector.notify_typing(room.id, True)
        except Exception:
            logger.debug("Could not send the typing hint for room %s", room.id, exc_info=True)

        wc = materialize(rule, room)
        platform_room = Room(
            id=room.id,
            name=room_description(room),
            type=room.kind.value,
        )
        # The §5.3 fields only this moment knows (§2.4), handed to the start so
        # they are part of the record from its first instant. Written here rather
        # than onto the record afterwards: an enrichment step leaves a window in
        # which a concurrent creation's save persists this record without its
        # rule, and a crash in that window leaves an orphan for the next boot to
        # prune.
        provenance = creation_provenance(
            wc, rule, room,
            connector_name=self._connector_name,
            agent_name=self._lifecycle.resolve_agent_name(wc.agent),
            now=now_iso(),
        )
        # A handle another room still holds is, on a platform that keeps URL
        # names unique, a room since renamed away and not yet heard from: the
        # handle follows a rename only on the OLD room's next frame. Left
        # alone, the start below refused this room until that frame came or
        # the operator expired the holder — and every message here in the
        # meantime was dropped (final pre-merge review). Ask the platform what
        # the holder is called now; if it has moved, move its handle first.
        held = self._lifecycle.room_holding(wc.name)
        if held is not None and held != room.id:
            try:
                current = await self._connector.room_ref_by_id(held)
            except Exception as exc:
                current = None
                logger.warning("Could not resolve room %s, which still holds handle '%s': %s",
                               held, wc.name, exc)
            if current is not None and current.name and not current.kind.is_direct:
                await self._lifecycle.observe_room_name(held, current.name)

        self._creations_in_flight += 1
        try:
            # A raise propagates (§2.2): a creation that failed is an abort, and
            # catching it here would hand the caller the same None a rule miss
            # produces — a final answer for a non-final condition. The cap slot
            # and the per-room lock both release on the way out.
            #
            # The lifecycle's per-watcher lock, for the same reason `_recreate`
            # takes it: the start and the save are one lifecycle transition, and
            # an operator verb for this name must see it whole or not at all.
            async with self._lifecycle.watcher_lock(wc.name):
                await self._lifecycle.start_watcher_in_room(
                    wc, None, platform_room,
                    history_before_ts=history_before_ts, provenance=provenance,
                )
                self._lifecycle.save_state()
        except BaseException:
            # No turn will follow to switch the toggle-style indicator off.
            try:
                await self._connector.notify_typing(room.id, False)
            except Exception:
                logger.debug("Could not clear the typing hint for room %s", room.id, exc_info=True)
            raise
        finally:
            self._creations_in_flight -= 1
        logger.info(
            "Created watcher '%s' for room %s from rule '%s'",
            wc.name, room.id, rule.name,
        )
        return self._lifecycle.processor_named(wc.name)


def _earlier(a: str | None, b: str | None) -> str | None:
    """The tighter of two exclusive upper bounds, or whichever one exists.

    A function rather than an inline comparison because the direction is easy
    to get backwards and was: an upper bound is tighter when it is *lower*, and
    the first version reached for the trigger's bound as "the tighter of the
    two" when the trigger is by construction above the watermark.
    """
    if not a:
        return b or None
    if not b:
        return a
    return a if _ts_gt(b, a) else b


