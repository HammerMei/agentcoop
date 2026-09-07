"""SessionManager: thin orchestrator that wires collaborators together.

Delegates all real work to focused collaborators:
  - WatcherLifecycle: start/stop/pause/resume/reset watchers
  - MessageDispatcher: inbound message routing + permission interception
  - InjectedContextBuilder: context file reading + durable agent session delivery
  - StateStore: WatcherState persistence + watermark management
  - SessionMaps: shared session→room/role/connector routing state
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import UTC, datetime
from typing import Collection

from ..agents import AgentBackend
from .config import CoreConfig
from .connector import (
    SCHEDULER_SENDER_ID,
    Connector,
    IncomingMessage,
    MembershipHook,
    Room,
    User,
    UserRole,
)
from .dispatch import MessageDispatcher
from .injected_context_builder import InjectedContextBuilder
from .lifecycle_sweep import LifecycleSweep
from .permission import PermissionRegistry
from .reconcile import RecordAction, reconcile_records, rematerialized_fields
from .retry_stop import stop_with_retries
from .session_maps import SessionMaps
from .state import (
    StateFilter,
    WatcherState,
    parse_state_filter,
    past_idle_ttl,
    room_kind_or_channel,
)
from .state_store import StateStore
from .watcher_lifecycle import WatcherLifecycle
from .watcher_manager import (
    RoomRef,
    StaleRecordError,
    WatcherManager,
    first_matching_rule,
    room_label,
)
from .watcher_rule import RoomKind, WatcherRule

logger = logging.getLogger("agent-chat-gateway.core.session_manager")

# Why a reclaimed room's scheduled jobs are cancelled, and what the operator can
# do about it — `(reason, advice)`, one pair per cause, so the AUDIT line names
# the cause that applied instead of assuming one. `{job_id}` is filled per job.
JOBS_CANCELLED_BOT_REMOVED = (
    "the bot was removed from the room, so the job could never deliver",
    "'agent-chat-gateway schedule resume {job_id}' restores it once the bot is back.",
)
JOBS_CANCELLED_ROOM_UNSERVED = (
    "the room is no longer available to this connector",
    "recreate the job if the room becomes available to this connector again.",
)
JOBS_CANCELLED_CONNECTOR_REMOVED = (
    "its connector was removed from config.yaml",
    "recreate the job against a current watcher if the connector returns.",
)
JOBS_CANCELLED_BY_RECONCILIATION = (
    "its watcher was expired by reconciliation — no rule in config.yaml covers the room any more",
    "no rule recreates this room's watcher, so recreate the job once a rule covers the room again.",
)


# The shared degrade-don't-raise conversion lives in state.py since Codex
# round 9 found a THIRD raising site — one helper, every consumer.
_room_kind_or_channel = room_kind_or_channel


class SessionManager:
    """Thin orchestrator: wires collaborators and manages top-level lifecycle.

    Accepts any Connector implementation — RocketChatConnector, ScriptConnector,
    or future Slack/Discord connectors — without knowing their platform details.

    Usage::

        manager = SessionManager(connector, agents, "assistance", core_config,
                                 watcher_rules=rules)
        await manager.run()   # blocks until cancelled
    """

    def __init__(
        self,
        connector: Connector,
        agents: dict[str, AgentBackend],
        config: CoreConfig,
        state_name: str = "default",
        permission_registry: PermissionRegistry | None = None,
        session_maps: SessionMaps | None = None,
        watcher_rules: list | None = None,
        cancel_jobs=None,
    ) -> None:
        self._connector = connector
        # `state_name` is the connector's config name in production (service.py
        # passes `cc.name`), which is also the name rules bind to — the manager
        # and the router closure below both key on it.
        self._connector_name = state_name
        maps = session_maps or SessionMaps()

        # Collaborators
        self._dispatcher = MessageDispatcher(connector, permission_registry)
        self._injector = InjectedContextBuilder(config)
        self._state_store = StateStore(state_name, connector)
        self._lifecycle = WatcherLifecycle(
            connector=connector,
            agents=agents,
            config=config,
            state_store=self._state_store,
            dispatcher=self._dispatcher,
            injector=self._injector,
            permission_registry=permission_registry,
            maps=maps,
        )
        # The manager is constructed UNCONDITIONALLY, empty rule list and all
        # (Codex round 5, P1). It was once gated on rules existing — a
        # pre-cutover rule protecting static-only deployments' delivery
        # behaviour — but that deployment shape no longer loads, and the gate
        # had acquired a new victim: a connector whose operator removed its
        # last rule still hydrates its self-sufficient rule-derived records
        # (§2.4 keeps them until expiry), and without a manager those records
        # got no router, no boot recreation, no replay and no sweep — every
        # existing session unreachable, silently. With an empty rule list the
        # manager recreates persisted records and merely declines genuinely
        # new rooms. Consequence, named: the router is now always registered,
        # so Rocket.Chat runs subscribe-all even with zero rules — every
        # offer declined, which is the uniform §2.8 behaviour post-cutover.
        self._watcher_manager = WatcherManager(
            state_name, connector, self._lifecycle,
            # Normalized like `_watcher_rules` below (Codex round 10): a
            # standalone caller using the declared default None would hand
            # the always-on manager an un-iterable rule list, and the first
            # new room's first_matching_rule would raise instead of declining.
            list(watcher_rules or []))
        # The idle sweep exists only where the transport can wake what it
        # drops: §2.6 rules eager connectors (Script, Voice) "never"
        # idle-eligible — no message can ever arrive to wake an idled room
        # there, so a timer that dropped one would be muting it permanently.
        self._sweep = (
            LifecycleSweep(self._lifecycle,
                           reconcile=self._reconcile_membership)
            if connector.supports_unsolicited_inbound()
            else None
        )
        # Fired by the membership-remove handler for the reclaimed watcher's
        # name: its pending jobs are cancelled with a stated reason rather
        # than left pointing at nothing (§2.7). Injected as a closure,
        # because the job store lives above this layer.
        self._cancel_jobs = cancel_jobs
        # Kept for the eager-start loop (§2.6): a connector with no
        # unsolicited inbound never has a room offered to it, so its rules'
        # literal rooms are walked at boot instead.
        self._watcher_rules = list(watcher_rules or [])
        # Hydration + reconciliation happen once per boot, before connectors
        # connect (`settle_records`); `sync_only` runs them itself when nothing
        # did — `run_once` and the tests boot a manager on its own.
        self._records_settled = False
        # Bot-membership events that arrived while a reload had this manager
        # quiesced (#144): replayed on `rearm`, in order, because the sweep's
        # membership reconciliation covers paused and idle records only — an
        # ACTIVE watcher removed from its room mid-reload would otherwise keep
        # its session and its jobs indefinitely.
        self._deferred_membership: list[tuple[str, object]] = []  # ("added"|"removed", ref)
        self._quiesced = False

    # ── Main entry point ──────────────────────────────────────────────────────

    async def run(self) -> None:
        """Connect, sync watchers, block until cancelled.

        Note: control socket ownership belongs to GatewayService.
        Use run_once() when GatewayService is the orchestrator (normal production use).
        This method is kept for standalone/test use cases that don't use GatewayService.
        """
        await self.run_once()
        logger.info("SessionManager running")
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()

    async def run_once(self, unavailable_agents: set[str] | None = None) -> list[str]:
        """Connect and sync watchers WITHOUT blocking forever.

        Args:
            unavailable_agents: Optional set of agent names whose permission
                broker failed to start.  Watchers that use these agents will
                be skipped with an error rather than started without permission
                enforcement.

        Returns:
            List of human-readable error strings for any watchers that failed.
        """
        await self.connect_only()
        errors = await self.sync_only(unavailable_agents=unavailable_agents)
        logger.info("SessionManager ready (run_once)")
        return errors

    async def connect_only(self) -> None:
        """Phase one: authenticate. No subscription, no watcher, no room.

        Split from `sync_only` so an orchestrator holding several connectors can put a
        barrier between the phases — two connectors on one bot account have to be
        refused *before* either subscribes, and each SessionManager owns exactly one
        connector, so nothing at this layer can see the collision (§4.5).

        A single-connector caller has no such barrier to run and should keep using
        `run_once()`.
        """
        self._connector.register_handler(self._on_inbound)
        self._connector.register_capacity_check(self._dispatcher.capacity)
        if (self._watcher_manager is not None
                and self._connector.supports_unsolicited_inbound()):
            # The discovery hooks belong to discovering connectors only:
            # Script and Voice have no stream to offer a room from — their
            # rooms start eagerly (§2.6) — and neither implements
            # register_router. Before start_inbound(), necessarily:
            # Rocket.Chat's start_inbound attempts subscribe-all only when a
            # router is already registered, so a router registered later
            # would never receive an offer.
            self._connector.register_router(self._route_unclaimed_room)
            # Alongside the router, and gated the same way: membership events
            # exist to serve the rule-derived lifecycle, and a static-only
            # deployment keeps its exact behaviour until its operator writes a
            # rule. The base method is a no-op, so this is safe on a connector
            # with no membership stream.
            self._connector.register_membership_hook(MembershipHook(
                added=self._on_membership_added,
                removed=self._on_membership_removed,
            ))
        await self._connector.connect()

    async def sync_only(self, unavailable_agents: set[str] | None = None) -> list[str]:
        """Phase two: restore watchers, subscribe, then open the inbound stream.

        `start_inbound()` is last on purpose. A connector that delivers everything its
        account can see discards events for rooms it has no state for, so reading before
        the restore turns "not subscribed yet" into "lost" — and the errors returned
        here are per-watcher failures, not a reason to leave the connector deaf, so the
        stream starts regardless of them.
        """
        errors = await self.settle_records(unavailable_agents=unavailable_agents)
        # Eager creation for connectors with no unsolicited inbound (§2.6):
        # nothing ever offers them a room, so their rules' literal rooms are
        # started here, before the inbound surface opens.
        await self._eager_start_rule_rooms(errors)
        # Snapshotted before the stream opens, and that placement is the whole
        # defence against the race below: after `start_inbound` a live message
        # can recreate a room and advance its watermark past the entire
        # down-window before the replay loop reaches that record. Reading the
        # watermarks here freezes what "missed" means; the replay itself still
        # runs after the stream, so a room's live traffic is never held up by
        # a REST probe.
        down_window = self._snapshot_watermarks()
        await self._connector.start_inbound()
        # Before the replay, deliberately: a record this evaluation recreates
        # owns its own replay (§2.2, replay ownership), so the loop below finds
        # it resident and skips it — and a record it marks idle is still probed
        # below, because messages waiting in a room outrank its idleness.
        await self._evaluate_lifecycle_at_boot()
        await self._replay_persisted_records(down_window)
        if self._sweep is not None:
            # After the replay completes, structurally — §2.5 makes this
            # ordering non-optional once the expiry leg exists (expiry deletes
            # the record recreation reads from, and the replay's whole job is
            # reviving rooms with messages waiting), and the idle leg honors it
            # from day one so step 5 does not have to move this line.
            self._sweep.start()
        return errors

    async def _eager_start_rule_rooms(self, errors: list[str]) -> None:
        """Start every literal rule room on a connector that cannot discover (§2.6).

        Script's messages arrive by direct injection and Voice's rooms as HTTP
        path segments — no stream ever offers a room, so lazy creation can
        never fire. Their rules name **literal** rooms (enforced at config
        load, `_enforce_literal_rooms`), and each named room starts at boot
        through `get_or_create`: sticky binding, the paused refusal and the
        §5.3 record fields all apply exactly as they do on the message path.
        `resolve_room(name)` here is the one legitimately name-based
        resolution left (§2.8) — an eager rule genuinely starts from a name.

        Per-room failures append to the startup error list, as the static
        loop's did; boot proceeds, and the daemon reports the list at startup.
        **The catch below deliberately narrows `get_or_create`'s contract**
        ("an exception is retryable — the caller owns the retry"): on an
        eager connector no message can ever re-trigger the room, so the only
        retries that exist are the next daemon restart and the operator's
        `resume`. A room whose eager start fails therefore stays down, loudly,
        until one of those — a weaker recovery than RC/MM's redelivery, and
        the price of a transport with no inbound stream. Connectors with
        unsolicited inbound return immediately — their rooms arrive, they are
        never walked.
        """
        if self._watcher_manager is None:
            return
        if self._connector.supports_unsolicited_inbound():
            return
        kind_for = {k.value: k for k in RoomKind}
        # Rooms the rule loop already attempted — the record walk below must
        # not retry them: a start that failed loudly there would otherwise be
        # attempted (and reported) twice in one boot.
        attempted: set[str] = set()
        for rule in self._watcher_rules:
            for pattern in rule.rooms.include:
                if not pattern.is_literal:
                    # Load enforcement makes this unreachable; skipping is
                    # belt-and-braces, not a policy.
                    continue
                name = pattern.raw
                # The ORDERED first-match decision, not just this rule's own
                # except_for (Codex rounds 6 and 12): an earlier rule that
                # declines or claims this literal shadows this one — shadowed
                # rules are valid config, only warned about — and
                # get_or_create would correctly answer None, which the branch
                # below then misreported as a startup failure on every boot.
                # An intentional exclusion (or shadowing) is a no-op here.
                probe = RoomRef(id="", kind=RoomKind.CHANNEL, name=name)
                winner = first_matching_rule(
                    self._watcher_rules, self._connector_name, probe)
                if winner is not rule:
                    logger.info(
                        "Rule '%s': room '%s' is %s — not started here",
                        rule.name, name,
                        "excluded by an earlier (or this) rule's except_for"
                        if winner is None
                        else f"claimed by earlier rule '{winner.name}'",
                    )
                    continue
                try:
                    room = await self._connector.resolve_room(name)
                    ref = RoomRef(
                        id=room.id,
                        kind=kind_for.get(room.type, RoomKind.CHANNEL),
                        name=room.name,
                    )
                    attempted.add(ref.id)
                    proc = await self._watcher_manager.get_or_create(
                        self._connector_name, ref)
                except Exception as e:
                    msg = (f"Rule '{rule.name}' (room '{name}'): eager start "
                           f"failed: {e}")
                    logger.error(msg)
                    errors.append(msg)
                    continue
                if proc is None:
                    record = self._lifecycle.record_for_room(ref.id)
                    if record is not None and record.paused:
                        logger.info(
                            "Rule '%s': room '%s' is paused — not started",
                            rule.name, name,
                        )
                    else:
                        msg = (f"Rule '{rule.name}' (room '{name}'): eager "
                               f"start produced no watcher — check that the "
                               f"room matches the rule's include patterns")
                        logger.error(msg)
                        errors.append(msg)
        # The persisted records, independently of the current rules (Codex
        # round 6, completing round 5's unconditional manager): sticky binding
        # (§2.4) keeps a record alive after its rule is deleted or renamed,
        # and on an eager connector no message can ever arrive to wake it —
        # the rule loop above is the only starter there is. Rooms the rule
        # loop just started answer with their resident processor; paused
        # records are skipped silently (an operator's mute, not a failure);
        # anything else failing to start is loud, per the eager contract.
        for record in list(self._lifecycle.states().values()):
            if not record.config or not record.room_id:
                continue
            if record.room_id in attempted:
                # The rule loop already attempted this room this boot — a
                # failure there was reported once, loudly, and a second
                # attempt here would double it.
                continue
            if self._lifecycle.processor_for_room(record.room_id) is not None:
                continue
            if record.paused:
                logger.info(
                    "Record '%s': room %s is paused — not started",
                    record.watcher_name, record.room_id,
                )
                continue
            ref = RoomRef(
                id=record.room_id,
                kind=kind_for.get(record.room_kind, RoomKind.CHANNEL),
                name=record.room_name,
                participants=tuple(record.participants),
            )
            try:
                proc = await self._watcher_manager.get_or_create(
                    self._connector_name, ref)
            except Exception as e:
                msg = (f"Record '{record.watcher_name}' (room "
                       f"'{record.room_id}'): eager recreation failed: {e}")
                logger.error(msg)
                errors.append(msg)
                continue
            if proc is None:
                msg = (f"Record '{record.watcher_name}' (room "
                       f"'{record.room_id}'): eager recreation produced no "
                       f"watcher")
                logger.error(msg)
                errors.append(msg)

    async def _evaluate_lifecycle_at_boot(self) -> None:
        """Boot runs the same evaluation the sweep runs (§2.5), over the
        records that were *active* at shutdown.

        One function, two callers: the TTL arithmetic is `past_idle_ttl`, the
        same call the sweep makes — a boot rule and a running rule would drift
        on exactly the restart-only path nothing exercises.

        A was-active record (rule-derived, not paused, no `dropped_at`) is
        either **recreated** through `get_or_create` — so replay, sticky
        binding and the paused refusal all apply for free — or, when it is
        already past its idle TTL, **marked idle with a fresh `dropped_at`**
        rather than paying a resume for a room the first sweep would drop
        minutes later. Fresh, never backdated: expiry measures from this
        moment, which is what keeps `active → expired` impossible through an
        outage of any length (§2.5).

        This is also what keeps `list` honest after a restart: a was-active
        record has no `dropped_at` and no processor, which reads as `failed` —
        the default view reporting a healthy fleet as broken. After this pass
        every such record is resident again or honestly idle.
        """
        if self._watcher_manager is None:
            return
        if not self._connector.supports_unsolicited_inbound():
            # §2.6: eager connectors are never idle-eligible — a record this
            # evaluation stamped idle could never be woken (no message can
            # arrive), so it would be muted permanently. Their records were
            # just restarted by the eager loop instead.
            return
        now = datetime.now().astimezone()
        stamped = 0
        for record in list(self._lifecycle.states().values()):
            if (
                # Both-fields eligibility, same as the prune (round 22/23): a
                # damaged rule_name with a surviving config still recreates.
                not (record.rule_name or record.config)
                or record.paused
                or record.dropped_at
                or not record.room_id
            ):
                continue
            if self._lifecycle.processor_for_room(record.room_id) is not None:
                # Resident already (Codex round 18): inbound opened before
                # this pass, and a live message can finish recreating a
                # past-TTL record before the loop reaches it — the fresh
                # record carries the OLD clock, so the stamp below would mark
                # a RUNNING watcher idle, and nothing ever clears dropped_at
                # (enqueue advances only last_activity_at): every sweep then
                # takes the expiry leg, whose residency guard blocks it —
                # wedged until a restart. The timed drop re-checks residency
                # under the lock; boot's stamp must too.
                continue
            if past_idle_ttl(record, now):
                record.dropped_at = now.isoformat(timespec="seconds")
                stamped += 1
                logger.info(
                    "Watcher '%s' was idle past its TTL across the restart — "
                    "marked idle; its next message wakes it",
                    record.watcher_name,
                )
                continue
            # Tolerant, not raising (Codex round 8): `load_state` promises to
            # degrade a corrupted record rather than let one take the service
            # down, and a raising enum conversion OUTSIDE the per-record try
            # defeated that contract — one garbled room_kind aborted the
            # whole connector's boot. Unknown falls back to CHANNEL, loudly:
            # the mention gate applies there, which is the safe default.
            if not await self._room_still_served(record):
                continue
            kind = _room_kind_or_channel(record)
            try:
                await self._watcher_manager.get_or_create(
                    self._connector_name,
                    RoomRef(
                        id=record.room_id,
                        kind=kind,
                        name=record.room_name,
                        participants=tuple(record.participants),
                    ),
                    # The snapshot pin (Codex round 11): a live removal can
                    # reclaim this record mid-walk, and an unpinned call
                    # would _create a fresh watcher for the just-left room.
                    expected_record=record,
                )
            except Exception as e:
                # An abort, not a decision (§2.2): the record keeps reading
                # `failed`, which is the honest state, and the next boot — or
                # the room's next message — retries.
                logger.warning(
                    "Boot recreation failed for watcher '%s' (room %s) — it "
                    "stays failed until its next message or the next start: %s",
                    record.watcher_name, record.room_id, e,
                )
        if stamped:
            self._lifecycle.save_state()

    def _snapshot_watermarks(self) -> dict[str, str]:
        """Each rule-derived record's watermark as of boot, by watcher name.

        Taken after `sync_watchers` — hydration is what puts the records in
        memory — and before the inbound stream opens, so every value is the
        boundary of the interval the daemon was down for, uncontaminated by
        anything that arrives after.
        """
        return {
            ws.watcher_name: ws.last_processed_ts
            for ws in self._lifecycle.states().values()
            if (ws.rule_name or ws.config) and ws.last_processed_ts
        }

    async def settle_records(
        self, unavailable_agents: set[str] | None = None, *,
        report_expiry_failures: bool = False,
    ) -> list[str]:
        """Hydrate and reconcile this connector's records — boot's "reload" (§2.4).

        Runs before the connector connects and before the identity barrier, so
        everything that consumes records afterwards — the DM-claim check, the
        eager loop, the startup evaluation and replay — sees the fleet as the
        current config describes it, not as the last run left it. Nothing here
        needs the network: the plan is pure, re-materialization is an in-memory
        rewrite plus a save, and an expiry's connector step (unsubscribe) is a
        no-op for a room nothing has subscribed yet. Idempotent per boot.

        Returns the hydration's startup errors — or, with
        `report_expiry_failures` (a reload settling a new connector), the
        watchers the reconciliation could not expire: boot logs those and
        moves on, a reload must not report a clean apply over them.
        """
        if self._records_settled:
            return []
        errors = await self._lifecycle.sync_watchers(unavailable_agents=unavailable_agents)
        _rewritten, not_expired = await self._reconcile_records()
        self._records_settled = True
        return not_expired if report_expiry_failures else errors

    async def _reconcile_records(self) -> tuple[list[str], list[str]]:
        """Run the current rules over every hydrated record (§2.4, #143).

        Applies the plan `reconcile_records` returns: re-materialized records
        keep their object, session and clocks; expired ones go through the
        removal path's shared tail, jobs included, with the session id on the
        AUDIT line. A record the engine could not re-match honestly is kept
        and said so. The plan is logged either way, as what was actually
        applied, so a restart's effect on the fleet can be read back.

        Boot-independent: boot runs it over a still fleet from `settle_records`;
        `config reload` runs it on a live manager through `reconcile_live`,
        which then restarts the resident processors of the records this
        rewrote — returned here by room id for exactly that, together with the
        watchers an expiry could NOT remove (their records are still
        installed; the log says why), so a reload does not report a clean
        apply over a record its rules no longer cover.
        """
        plan = reconcile_records(
            self._lifecycle.states().values(),
            self._watcher_rules,
            connector=self._connector_name,
        )
        for action in plan.of("keep"):
            if action.reason:
                logger.warning(
                    "Reconciliation (%s): watcher '%s' (room %s) kept — %s",
                    self._connector_name, action.watcher_name, action.room_id,
                    action.reason,
                )
        if not plan.changes:
            logger.info("Reconciliation (%s): %s — nothing to change",
                        self._connector_name, plan.summary())
            return [], []
        rules_by_name = {r.name: r for r in self._watcher_rules}
        rewritten: list[str] = []
        not_expired: list[str] = []
        expired = 0
        for action in plan.changes:
            record = self._lifecycle.record_for_room(action.room_id)
            if record is None:
                continue
            if action.action == "rematerialize":
                self._apply_rematerialize(record, action, rules_by_name[action.to_rule])
                rewritten.append(record.room_id)
                continue
            try:
                self._lifecycle._enter_verb("reclaim", action.room_id)
            except RuntimeError:
                break  # shutting down — leave the rest as it is, save what was done
            try:
                went = await self._apply_expire(record, action)
            finally:
                self._lifecycle._exit_verb()
            expired += went
            if not went:
                not_expired.append(action.watcher_name)
        if rewritten:
            self._lifecycle.save_state()
        logger.info("Reconciliation (%s): %s", self._connector_name,
                    plan.format_counts(len(plan.of("keep")), len(rewritten), expired))
        return rewritten, not_expired

    # ── Config reload (#144) ─────────────────────────────────────────────────

    def records(self) -> list[WatcherState]:
        """This connector's in-memory records — what a reload plans over."""
        return list(self._lifecycle.states().values())

    def resident_rooms(self) -> set[str]:
        """The rooms with a running processor — the ones a restart reaches."""
        return {ws.room_id for ws in self._lifecycle.states().values()
                if self._lifecycle.processor_for_room(ws.room_id) is not None}

    def set_unavailable_agents(self, names: set[str]) -> None:
        """Push the agents a reload could not start (or brought back) to the
        lifecycle's fail-closed gate — boot wrote it once; a reload rewrites it."""
        self._lifecycle.set_blocked_agents(names)

    def replace_rules(self, rules: list[WatcherRule]) -> None:
        """Take the candidate config's rules for this connector.

        On a quiesced manager: both the router's list and the eager loop's
        are replaced, so the next offered room and the next reconciliation
        both read the new list.
        """
        self._watcher_rules = list(rules)
        self._watcher_manager.replace_rules(self._watcher_rules)

    async def quiesce(self, reason: str) -> None:
        """Still this manager for a reload's apply window.

        Refuses new transitions everywhere with `reason` — operator verbs are
        refused outright, a room offer parks (retryable, see
        `WatcherManager._park_if_reloading`) — waits out the ones in flight,
        and stops the sweep so no pass overlaps the apply. Processors keep
        running: a room whose agent is not being rebuilt keeps answering, and
        the processors of an agent that IS are drained by `stop_watchers_on_agents`
        before that backend stops. `rearm` is the inverse.
        """
        self._quiesced = True
        await self._watcher_manager.drain(reason)
        await self._lifecycle.drain_verbs()
        if self._sweep is not None:
            await self._sweep.stop()

    async def rearm(self) -> None:
        """Open the manager again after a reload's apply window, then handle
        the membership removals that arrived while it was quiesced."""
        self._lifecycle.rearm_transitions()
        self._quiesced = False
        if self._sweep is not None:
            self._sweep.start()
        # Both sides of a membership transition, in arrival order: a room the
        # bot left and rejoined during the window ends up registered, not
        # reclaimed — replaying removals alone would reclaim a valid room.
        deferred, self._deferred_membership = self._deferred_membership, []
        for kind, ref in deferred:
            if kind == "added":
                await self._on_membership_added(ref)
            else:
                await self._on_membership_removed(ref)

    async def reconcile_live(self) -> tuple[list[str], list[str]]:
        """Reconcile a RUNNING fleet against the rules just installed (#144).

        Boot's reconciliation plus what a live fleet adds: a re-materialized
        record with a resident processor is restarted, because that processor
        was built from the config the record no longer carries; and on an
        eager connector the new rules' literal rooms are started, since no
        message will ever offer them. Called re-armed: the expiries go
        through the verb barrier like every other reclamation, and a wake
        racing a restart is serialized by the watcher lock. Returns the rooms
        it restarted — so a caller restarting processors for another reason
        (an agent change) does not restart them twice — and the rooms that
        did NOT come up, one message each: a re-materialized record whose
        processor would not restart (already stopped by then), and an eager
        room a new rule names that would not start. Either reads as failed in
        `list` until `resume` or its next message, and the caller must not
        report a clean apply over it.
        """
        rewritten, not_expired = await self._reconcile_records()
        restarted: list[str] = []
        failures: list[str] = [
            f"Connector '{self._connector_name}': watcher '{name}' could not be expired — its "
            f"record is still installed and no rule covers its room; 'expire {name}' by hand"
            for name in not_expired
        ]
        for room_id in rewritten:
            try:
                if await self._lifecycle.restart_resident(room_id):
                    restarted.append(room_id)
            except Exception as e:
                failures.append(
                    f"Connector '{self._connector_name}': the re-materialized watcher for "
                    f"room {room_id} could not be restarted: {e}")
        eager: list[str] = []
        await self._eager_start_rule_rooms(eager)
        failures.extend(eager)
        return restarted, failures

    async def stop_watchers_on_agents(self, agents: set[str]) -> tuple[list[str], list[str]]:
        """Drain and stop every resident processor bound to one of `agents` (#144).

        The first half of a reload's agent restart, run BEFORE the backend
        stops: a processor's stop drains its queue by processing it, so the
        backend must still be alive for the drain to be a drain and not a
        string of failures posted to the room. The drains run concurrently,
        as `stop_all`'s do — each may take the full queue timeout, and one
        busy agent can have many rooms — and a stop that raises is retried
        (`stop_with_retries`). Returns the room ids stopped, so the reload can
        start them again wherever their records point afterwards, and one
        message per room that would not stop.
        """
        targets = [r for r in self.records() if r.agent in agents]

        def gone(record) -> bool:
            return self._lifecycle.processor_for_room(record.room_id) is None

        async def _stop(record) -> tuple[bool, BaseException | None]:
            """(stopped, error). A stop that raised AFTER removing the processor
            — at the unsubscribe — is a stop, noisily: not retried (a retry would
            find nothing resident and "succeed", hiding the error), but reported,
            and the room is started again like the others."""
            try:
                return await self._lifecycle.stop_resident(record.room_id), None
            except Exception as first:
                if gone(record):
                    return True, first
                err = await stop_with_retries(
                    f"connector '{self._connector_name}' watcher '{record.watcher_name}'",
                    lambda: self._lifecycle.stop_resident(record.room_id))
                return gone(record), err

        results = await asyncio.gather(*[_stop(r) for r in targets])
        rooms: list[str] = []
        failures: list[str] = []
        for record, (stopped, err) in zip(targets, results, strict=True):
            if err is not None:
                failures.append(
                    f"Connector '{self._connector_name}': watcher '{record.watcher_name}' "
                    f"{'stopped with errors' if stopped else 'could not be stopped'} before "
                    f"agent '{record.agent}' restarts ({err})"
                    + ("" if stopped else "; its processor may still hold the old backend"))
            if stopped:
                rooms.append(record.room_id)
        return rooms, failures

    async def reclaim_all(self, *, reason: str, jobs: tuple[str, str]) -> list[str]:
        """Reclaim every record — the removal path's shared tail, per room (#144).

        For a connector `config reload` removes: with the manager quiesced and
        the backends still alive, each record goes through `reclaim_room`
        (prompt file and attachment workspace removed, one AUDIT line) and its
        jobs are cancelled — what boot's orphan sweep cannot do for a file
        whose connector is gone. The backend session is KEPT, as boot keeps it:
        a state file copied under the connector's new name still names it.
        Returns the watchers whose record is STILL installed afterwards — a
        reclaim that failed (the shared tail swallows and logs it): their file
        must not be swept as if every record had gone.
        """
        records = self.records()
        for record in records:
            await self._reclaim_removed_room(
                record.room_id, reason=reason, expected=record, jobs=jobs,
                keep_backend_session=True)
        return [r.watcher_name for r in records
                if self._lifecycle.record_for_room(r.room_id) is r]

    async def reclaim_rooms(self, room_ids: list[str], *, reason: str,
                            jobs: tuple[str, str]) -> None:
        """Reclaim these records now, through the shared tail — for a reload
        whose new rules expire them while their agent is about to stop: the
        reconciliation would run after the backend is gone and have to skip
        the session, prompt-file and attachment cleanup (#144)."""
        for room_id in room_ids:
            record = self._lifecycle.record_for_room(room_id)
            if record is not None:
                await self._reclaim_removed_room(
                    room_id, reason=reason, expected=record, jobs=jobs)

    async def start_watchers_on_agents(
        self, agents: set[str], *, rooms: Collection[str] = (),
        exclude: set[str] = frozenset(),
    ) -> list[str]:
        """Start every WAS-ACTIVE record on one of `agents` once its backend is
        back (#144) — the second half of the agent restart.

        Was-active is boot's own criterion (`_evaluate_lifecycle_at_boot`):
        rule-derived, not paused, no `dropped_at`. That covers the rooms an
        earlier reload left down because the agent did not come up then — a
        fixed agent brings its rooms back without a restart. `rooms` are the
        ones `stop_watchers_on_agents` stopped this reload, started whatever
        agent their record names NOW: a reconciliation in between may have
        moved one to an agent that did not change. An idle room stays idle:
        its next message wakes it. `exclude` names rooms the reconciliation
        already restarted. Sessions are kept; a changed backend identity is
        settled by the start's own provisioning check, which logs the
        abandoned id. Returns error strings, one per room that could not
        come back.
        """
        errors: list[str] = []
        rooms = set(rooms)
        blocked = self._lifecycle.blocked_agents
        for record in self.records():
            if ((record.agent not in agents and record.room_id not in rooms)
                    or record.room_id in exclude
                    or not (record.rule_name or record.config)
                    or record.paused or record.dropped_at or not record.room_id):
                continue
            if record.agent in blocked:
                # Would be refused by `_ensure_agent_available`; the agent's own
                # degraded finding already says why every room on it is down.
                continue
            try:
                await self._lifecycle.start_from_record(record.room_id)
            except Exception as e:
                msg = (f"Connector '{self._connector_name}': watcher "
                       f"'{record.watcher_name}' could not be started after agent "
                       f"'{record.agent}' changed: {e}")
                logger.error(msg)
                errors.append(msg)
        return errors

    def _apply_rematerialize(
        self, record: WatcherState, action: RecordAction, rule: WatcherRule
    ) -> None:
        """Rewrite one record's rule-derived fields from the rule that now wins it."""
        self._lifecycle.rematerialize(record, rematerialized_fields(record, rule))
        logger.info(
            "Reconciliation (%s): watcher '%s' (room %s) re-materialized "
            "from rule '%s' to rule '%s'",
            self._connector_name, action.watcher_name, action.room_id,
            action.from_rule, action.to_rule,
        )

    async def _apply_expire(self, record: WatcherState, action: RecordAction) -> int:
        """Expire one record through the removal path's shared tail.

        Returns 1 if the record went, 0 if it is still installed afterwards.
        """
        logger.warning(
            "Reconciliation (%s): watcher '%s' (room %s) is expired — %s",
            self._connector_name, action.watcher_name, action.room_id, action.reason,
        )
        await self._reclaim_removed_room(
            action.room_id,
            reason=f"reconciliation: {action.reason}",
            expected=record,
            jobs=JOBS_CANCELLED_BY_RECONCILIATION,
        )
        if self._lifecycle.record_for_room(action.room_id) is record:
            # The shared tail swallows a failed reclamation (a transient save
            # error re-installs the record dormant) because its other callers
            # are re-discovered by the membership reconciliation. Nothing
            # re-discovers this one before the next boot, so say what is
            # still running and why.
            logger.error(
                "Reconciliation (%s): watcher '%s' (room %s) could NOT be "
                "expired — its record is still installed and may be "
                "recreated from a rule config.yaml no longer has, until the "
                "next start reconciles it again",
                self._connector_name, action.watcher_name, action.room_id,
            )
            return 0
        return 1

    async def _room_still_served(self, record: WatcherState) -> bool:
        """Whether this connector still serves the record's room — asked before
        boot recreates a watcher from that record (#141).

        A record's room fields say what the room was when the record was
        written, not whether this connector still serves it: a Mattermost
        connector whose `server.team` changed under an unchanged name, or an
        account removed from a room, leaves them intact, and a watcher rebuilt
        from them keeps serving a room the connector was configured away from.
        `room_ref_by_id` is the connector's own scope check and the wake path
        already goes through it; the two boot recreation sites (the lifecycle
        evaluation and the startup replay) go through this.

        Cost: the connector's room lookup per record the boot would recreate.
        On Rocket.Chat that is one subscription read; on Mattermost it is the
        channel read plus the account-wide membership list (see the connector's
        `_resolved_channel`), so a large Mattermost install pays roughly two
        serialized requests per record on top of the existing history probe.

        The connector contract's two failure shapes are kept apart, as in
        `_resolve_room_for_wake`: `None` is permanent (gone, another team, no
        longer a member) and reclaims the record through the removal path's
        shared tail — the same end state as the bot being removed from the
        room, jobs included; a raise is transient, and the record is left as it
        is for this boot — its next live message resolves the room again.
        """
        if not self._connector.supports_room_lookup():
            # The base `room_ref_by_id` answers None for "cannot look rooms
            # up", which here would read as "gone" and reclaim every record
            # at boot. Without the capability, recreate from the record as
            # before this check existed.
            return True
        try:
            current = await self._connector.room_ref_by_id(record.room_id)
        except Exception as exc:
            logger.warning(
                "Boot: could not resolve room %s for watcher '%s' — not "
                "recreated this boot; its next live message retries: %s",
                record.room_id, record.watcher_name, exc,
            )
            return False
        if current is not None:
            return True
        # The session id is on the AUDIT line the reclamation emits
        # (`release_session`), the one place every discarded session is logged.
        logger.warning(
            "Boot: room %s is not available to this connector (gone, in "
            "another team, or this account is no longer in it) — its record "
            "(watcher '%s') is reclaimed, unless a live event already "
            "replaced it",
            record.room_id, record.watcher_name,
        )
        # Counted in the shutdown barrier, like the membership-removal path:
        # a shutdown landing mid-boot must wait for this destructive
        # reclamation to settle, or the final save can persist an
        # active-looking record whose session was just deleted.
        try:
            self._lifecycle._enter_verb("reclaim", record.room_id)
        except RuntimeError:
            return False  # shutting down — the record is left as it is
        try:
            await self._reclaim_removed_room(
                record.room_id,
                reason="the room is no longer available to this connector",
                expected=record,
                jobs=JOBS_CANCELLED_ROOM_UNSERVED,
            )
        finally:
            self._lifecycle._exit_verb()
        return False

    async def _replay_persisted_records(self, down_window: dict[str, str]) -> None:
        """Recover messages that arrived while the daemon was down (§2.2).

        The abort guarantee — "watermark unchanged, so redelivery can retry" —
        is only worth something if something actually redelivers, and at
        startup nothing did: both connectors replay from their *reconnect*
        callback, which a process restart never fires. This walks the
        persisted rule-derived records instead, and for each one probes the
        gap between its boot-time watermark and now. An empty gap leaves the
        room idle — that is the lazy model working. A non-empty gap recreates
        the watcher from its own record (sticky, §2.4 — rules are not
        consulted) and replays the window through the connector's normal
        pipeline, where the restored watermark and the id window dedup as
        usual.

        ``down_window`` holds the watermarks as of *before the stream opened*
        (`_snapshot_watermarks`), and every decision here reads it rather than
        the record. A live message arriving between `start_inbound` and this
        loop recreates its room and advances that record's watermark past the
        whole down-window; reading the record then would report no gap and skip
        the room, losing every message from the outage with no log line. The
        snapshot also means a room that is *already resident* by the time the
        loop reaches it is still replayed — being resident is not evidence that
        anyone looked below the boundary.

        Like the boot evaluation, a room is resolved through the connector
        (`_room_still_served`, #141) before a watcher is recreated from its
        record.

        Best-effort per record: a room whose probe, recreation or replay fails
        stays idle, and its next live message triggers a recreation — which
        replays from the record's watermark, so the interval is recovered
        rather than merely the room. Boot must not die on one bad room.

        The accepted residual, restated: a room that never produced a record —
        or produced one with no watermark — has nothing to replay from, and a
        message that arrived for it while the daemon was down is gone until
        someone speaks again.
        """
        if self._watcher_manager is None:
            return
        if not self._connector.supports_unsolicited_inbound():
            # §2.6: an eager connector has no history surface to probe and no
            # down-window to recover — its messages arrive by injection or
            # HTTP request, which nothing buffers while the daemon is down.
            return
        for ws in list(self._lifecycle.states().values()):
            boundary = down_window.get(ws.watcher_name)
            if not boundary:
                # No record at boot, or no watermark to replay from — including
                # every room created live since the stream opened, which has
                # nothing behind it to recover.
                continue
            if ws.paused or not ws.config or not ws.room_id:
                continue
            # Before the probe, not after it: a room the bot was removed from,
            # or that was deleted, makes the history read itself raise, and a
            # failed probe is skipped as best-effort — a check placed after it
            # would never run for exactly the rooms it exists for. Resident
            # rooms were resolved by the evaluation that recreated them.
            if (self._lifecycle.processor_for_room(ws.room_id) is None
                    and not await self._room_still_served(ws)):
                continue
            room = Room(
                id=ws.room_id,
                name=ws.room_name or ws.watcher_name,
                # The record's kind, so a group DM reaches the direct-room
                # history endpoint rather than a channel one.
                type=ws.room_kind or ws.room_type or "channel",
            )
            try:
                missed = await self._connector.probe_missed_since(room, boundary)
            except Exception as e:
                logger.warning(
                    "Startup replay: could not probe room %s for watcher '%s': %s",
                    ws.room_id, ws.watcher_name, e,
                )
                continue
            if not missed:
                continue
            try:
                if self._lifecycle.processor_for_room(ws.room_id) is not None:
                    # Already resident, so a recreation already ran for it — and
                    # a recreation owns its room's replay (§2.2, "replay
                    # ownership"). Nothing to do: replaying here would be a
                    # second pass over the interval the recreation just covered,
                    # and a second pass is not free, because replay hands the
                    # filter its own boundary rather than the live watermark —
                    # so the ts filter suppresses nothing and only the bounded
                    # id window separates the two.
                    #
                    # That a resident room with a record must have come through
                    # a recreation is not an assumption: its record rules out
                    # `_create`, and a rule-derived record is absent from
                    # `watchers:`, so the static path never starts one either.
                    continue
                # Tolerant like the boot and injection paths (Codex round 9 —
                # the third raising site): a garbled kind must not strand the
                # outage messages behind an idle record no live traffic wakes.
                kind = room_kind_or_channel(ws)
                # Triggering the recreation is this loop's whole job. It owns no
                # interval of its own; the recreation replays what the room owes.
                await self._watcher_manager.get_or_create(
                    self._connector_name,
                    RoomRef(
                        id=ws.room_id,
                        kind=kind,
                        name=ws.room_name,
                        participants=tuple(ws.participants),
                    ),
                    # The snapshot pin (Codex round 11) — same as the boot
                    # evaluation's: this loop walks records hydrated before
                    # the inbound stream opened, and a live removal can
                    # reclaim one mid-walk.
                    expected_record=ws,
                )
            except Exception as e:
                logger.warning(
                    "Startup replay failed for watcher '%s' (room %s) — the room "
                    "stays idle; its next message recreates it and replays from "
                    "the record's watermark, so this interval is deferred rather "
                    "than lost: %s",
                    ws.watcher_name, ws.room_id, e,
                )

    async def _on_inbound(self, msg) -> bool:
        """The connector's handler: note the room's current name, then dispatch.

        One seam every claimed message passes, so a rename is followed on the
        first frame after it (`WatcherLifecycle.observe_room_name`). Only for
        connectors that discover rooms — an eager connector's room name is the
        configured literal, not a platform fact a frame could correct.
        """
        if self._connector.supports_unsolicited_inbound():
            await self._lifecycle.observe_room_name(msg.room.id, msg.room.name)
        return await self._dispatcher.dispatch(msg)

    async def _route_unclaimed_room(self, room: RoomRef, trigger) -> None:
        """The router the connectors call for a room no watcher tracks (§2.2).

        The manager answers the whole question — sticky record, rule match,
        create or drop — and the connector delivers the trigger afterwards if
        the room became tracked. Nothing here inspects the trigger beyond
        asking the connector what history bound it implies: the frame is
        platform-shaped, and this layer deliberately is not.
        """
        await self._watcher_manager.get_or_create(
            self._connector_name,
            room,
            history_before_ts=self._connector.trigger_history_bound(trigger),
        )

    async def _on_membership_added(self, room: RoomRef) -> None:
        """The bot was added to a room: register its record idle (§2.7).

        Never raises toward the connector — an add the handler drops is
        re-discovered by the room's first message, which is the safety net
        membership registration supplements and never replaces.
        """
        if self._watcher_manager is None:
            return
        if self._watcher_manager.disarmed:
            if self._quiesced:
                self._deferred_membership.append(("added", room))  # a reload; replayed on rearm
            return
        try:
            await self._watcher_manager.register_on_join(room)
        except Exception:
            logger.exception(
                "Membership-add handling failed for room %s — the room's "
                "first message still creates its watcher", room.id,
            )

    async def _on_membership_removed(self, room_id: str) -> None:
        """The bot was removed from a room: reclaim its record, cancel its jobs.

        Never raises toward the connector — a remove the handler drops is
        re-discovered by the periodic membership reconciliation, and
        `reclaim_room` is idempotent so the late discovery reaches the same
        end state. Gated on disarm like the add: a reclaim racing stop_all
        would dismantle state the shutdown is flushing.
        """
        if self._watcher_manager is None:
            return
        if self._watcher_manager.disarmed:
            if self._quiesced:
                self._deferred_membership.append(("removed", room_id))  # replayed on rearm
            return
        # Counted in the shutdown barrier (Codex round 10): a removal past
        # the gate above but parked on the watcher lock or mid-reclaim was
        # invisible to both drains — the final save could run mid-prune, and
        # disconnect's task cleanup then cancelled the reclamation half-done.
        # The traced residue was bounded (record popped last, reconciliation
        # re-reclaims), but the barrier machinery makes counting it three
        # lines. The reconciliation's calls stay uncounted on purpose: they
        # ride the sweep task, which shutdown cancels and awaits first.
        try:
            self._lifecycle._enter_verb("reclaim", room_id)
        except RuntimeError:
            return  # disarm flipped since the gate above — same answer.
        try:
            await self._reclaim_removed_room(
                room_id,
                reason="the platform reported the bot removed from the room",
                jobs=JOBS_CANCELLED_BOT_REMOVED,
            )
        finally:
            self._lifecycle._exit_verb()

    async def _reclaim_removed_room(
        self, room_id: str, *, reason: str, jobs: tuple[str, str], expected=None,
        require_dormant: bool = False, keep_backend_session: bool = False,
    ) -> None:
        """The removal path's shared tail: reclaim the record, cancel its jobs.

        Two discoverers, one end state (§2.7): the live membership event and
        the periodic reconciliation both land here, so a removal discovered
        late cannot reach a different outcome than one seen live.
        """
        try:
            name = await self._lifecycle.reclaim_room(
                room_id, reason=reason, expected=expected,
                require_dormant=require_dormant, keep_backend_session=keep_backend_session)
        except Exception:
            logger.exception(
                "Membership-removal reclaim failed for room %s — the "
                "reconciliation re-discovers it", room_id,
            )
            return
        # `None` from reclaim_room covers two cases that must be told apart
        # here: no record for the room at all, or a record that was not
        # reclaimed (a replacement pinned by `expected`, or a static-era one).
        # In the second the room is still served and its jobs must stay. In the
        # first — `expire` reclaimed the record but kept the jobs, and the bot
        # was removed before anything recreated the watcher (Codex, PR #140) —
        # skipping cancellation left room-id jobs firing at a room the bot had
        # left, every slot, with no record for reconciliation to revisit. The
        # distinguishing fact is whether a record remains for the room.
        room_still_served = (
            name is None and self._lifecycle.record_for_room(room_id) is not None
        )
        if self._cancel_jobs is not None and not room_still_served:
            try:
                # The ROOM, not just the handle. Matching jobs by handle was
                # wrong in both directions once another room could take a
                # handle over: it cancelled a live room's jobs under the audit
                # line "the bot was removed from the room" — false for that
                # room — and left this room's own jobs firing at a room the bot
                # had left. Found by the sweep. A room-id job is cancellable by
                # its room alone; only a pre-schema-2 job needs the handle, and
                # with no record there is none to give.
                self._cancel_jobs(room_id, name or "", reason=jobs[0], advice=jobs[1])
            except Exception:
                logger.exception(
                    "Could not cancel scheduled jobs for reclaimed watcher "
                    "'%s' — they will fail audibly when they fire", name,
                )

    async def _reconcile_membership(self) -> None:
        """The periodic membership reconciliation (§2.7): the backstop for a
        missed removal event, for exactly the records nothing else can reach.

        "Discovered later as a REST failure" needs a future operation to touch
        the room, and a **dormant** record — paused or idle — has no timer
        reclamation and receives no inbound, so nothing ever touches it: a
        missed remove leaves its session, prompt file, attachment directory
        and jobs alive indefinitely, and `resume` keeps offering a room the
        bot cannot access (R3 by another route). Active records are excluded
        because the live paths do reach them; static records because their
        owner is config.yaml.

        Keyed on the connector declaring unsolicited inbound, not on a
        platform: Rocket.Chat's reconnect replay covers message history, not
        membership, so it has the same hole for a paused room. **Fail means
        keep**: a snapshot that could not be read (None) reclaims nothing —
        only an answered snapshot that unambiguously omits the room does.
        """
        if self._watcher_manager is None or self._watcher_manager.disarmed:
            return
        # A method, not a property — the unparenthesised form is a bound
        # method and always truthy, which silently killed this gate once.
        if not self._connector.supports_unsolicited_inbound():
            return
        dormant = [
            r for r in self._lifecycle.states().values()
            if (r.paused or r.dropped_at) and r.config and r.room_id
        ]
        if not dormant:
            return
        snapshot = await self._connector.membership_snapshot()
        if snapshot is None:
            logger.info(
                "Membership snapshot unavailable — reconciliation kept all "
                "%d dormant record(s) this round", len(dormant),
            )
            return
        for record in dormant:
            if record.room_id in snapshot:
                continue
            # Re-read before acting (Codex review of #121): the snapshot ages
            # while this loop awaits earlier reclamations, and both halves of
            # staleness are real — a wake can make the record active again
            # (dropped_at cleared), and a re-add can replace it with a fresh
            # record entirely. A record that is no longer THIS dormant record
            # is not this snapshot's to reclaim; the next daily round decides
            # it against a snapshot taken after whatever just happened.
            current = self._lifecycle.record_for_room(record.room_id)
            if current is not record or not (current.paused or current.dropped_at):
                logger.info(
                    "Reconciliation: room %s changed since the snapshot was "
                    "taken — leaving it for the next round", record.room_id,
                )
                continue
            await self._reclaim_removed_room(
                record.room_id,
                reason="the membership reconciliation found the bot is no "
                       "longer in the room (a removal event was missed)",
                jobs=JOBS_CANCELLED_BOT_REMOVED,
                # Pinned to this snapshot's record: the reclaim aborts under
                # the lock if a wake or re-add got there first (round 2) —
                # including an in-place resume, which clears `paused` without
                # replacing the object (round 3, hence require_dormant).
                expected=record,
                require_dormant=True,
            )

    async def shutdown(self) -> None:
        """Stop all processors, save state, disconnect connector.

        Ordering is critical: processors must be stopped FIRST so their final
        live watermarks are flushed back into WatcherState before save_state()
        reads them.  Saving before stop_all() would persist stale watermarks
        and cause duplicate message delivery on the next restart.
        """
        logger.info("SessionManager shutting down")
        if self._watcher_manager is not None:
            # First, before anything stops: the wake arms stay reachable until
            # the connector disconnects, and an idle room's message landing
            # mid-teardown would otherwise recreate a watcher nothing below
            # will stop — absent from stop_all's snapshot, its save rewriting
            # the state file after the final save (§2.5). `drain` disarms AND
            # waits out episodes already in flight (Codex round 5): one
            # already inside `start_watcher_in_room` installs its processor
            # after stop_all's snapshot, so it must finish — or bail at a
            # disarm re-check — before the snapshot is taken.
            await self._watcher_manager.drain()
        # The verbs' half of the same barrier (Codex round 9): resume and
        # reset start watchers off control-socket handlers the ControlServer
        # does not await, so they need their own drain — new transitions
        # refuse, in-flight ones finish before stop_all's snapshot.
        await self._lifecycle.drain_verbs()
        if self._sweep is not None:
            # Before stop_all, so a pass cannot overlap the shutdown's own
            # teardown of the processors it is judging.
            await self._sweep.stop()
        await self._lifecycle.stop_all()
        self._lifecycle.save_state()
        await self._connector.disconnect()
        logger.info("SessionManager shut down")

    # ── Public query API ──────────────────────────────────────────────────────

    def list_watchers(
        self, state_filter: StateFilter = StateFilter.OPERABLE
    ) -> list[dict]:
        return self._lifecycle.list_watchers(state_filter)

    def get_watcher_state(self, name: str):
        """Return the WatcherState for a watcher, or None if not found."""
        return self._lifecycle.get_watcher_state(name)

    def record_for_room(self, room_id: str):
        """The record bound to a room, or None (§2.4 sticky binding).

        The by-room counterpart to `get_watcher_state`, exposed for the same
        reason the scheduler needs it: a room id identifies a watcher even after
        the room has been renamed and its handle no longer matches.
        """
        return self._lifecycle.record_for_room(room_id)

    def get_processor(self, name: str):
        """Return the running MessageProcessor for a watcher, or None.

        "Is a processor running" is a different question from "what state is
        this watcher's record in", and `list` answers only the second (§2.8).
        The old row answered both — `active` from the processor, `paused` from
        the record — which is why callers could reach the first one through
        `list`. They cannot now, and should not: under rule-derived watchers a
        row exists for rooms with no processor by design.
        """
        return self._lifecycle.get_processor(name)

    def get_all_watcher_names(self) -> list[str]:
        """Every persisted watcher name for this connector — records are the
        only watcher identity left (§2.8)."""
        return sorted(self._lifecycle.states().keys())

    async def pause_watcher(self, name: str) -> None:
        await self._lifecycle.pause_watcher(name)

    async def resume_watcher(self, name: str) -> None:
        # A resume with no resident processor RECREATES the watcher from the
        # record's stored room fields (`_resume_record`), so it asks the
        # connector first, like boot and the wake path (#145). A resume on a
        # running watcher only clears the paused flag — nothing to resolve.
        # Residency is read here, outside the watcher lock the lifecycle takes:
        # a processor dropped by the idle sweep in that window is recreated
        # without the check, which is the pre-#145 behaviour and nothing worse.
        record = self._lifecycle.get_watcher_state(name)
        if (record is not None and record.room_id
                and self._lifecycle.processor_for_room(record.room_id) is None):
            await self._require_room_served(record, verb="resume")
            if self._lifecycle.get_watcher_state(name) is not record:
                # The lookup yielded, and in that window the record was
                # reclaimed and another room took the handle over (Codex on
                # #150). The lifecycle reads the name afresh, so it would
                # resume the replacement — whose room this check never saw.
                # Same identity pin as `_resume_locked`'s, one await earlier.
                raise RuntimeError(
                    f"Watcher '{name}' was replaced while the resume waited — "
                    f"re-check 'list' and retry."
                )
        await self._lifecycle.resume_watcher(name)

    async def _require_room_served(self, record: WatcherState, *, verb: str) -> None:
        """Refuse an operator-driven recreation of a room this connector no
        longer serves (#145) — the check `_room_still_served` makes at boot,
        with the other answer to "and then what".

        Boot RECLAIMS such a record, because nobody is there to act and the
        record would otherwise come back to life. Here an operator is present,
        asked for the watcher back, and is told why that cannot happen; a verb
        that meant "bring it back" must not quietly do something destructive
        instead. The record is left as it is — paused, so no timer touches it,
        or idle, and the expiry TTL reclaims it in time — and the refusal names
        `expire`, the verb that reclaims it now. The wake path refuses the same
        way and reclaims nothing.

        The connector contract's two failure shapes stay apart, as everywhere:
        `None` is permanent (gone, another team, no longer a member) and the
        refusal says so; a raise is transient and the refusal says to retry.
        Without `supports_room_lookup` the base `room_ref_by_id`'s `None` would
        read as "gone" and refuse every resume, so the check is skipped, as at
        boot.
        """
        if not self._connector.supports_room_lookup():
            return
        try:
            current = await self._connector.room_ref_by_id(record.room_id)
        except Exception as exc:
            logger.warning(
                "Could not reach the connector to resolve room %s for watcher "
                "'%s' — the %s is refused and the record left as it is: %s",
                record.room_id, record.watcher_name, verb, exc,
            )
            raise RuntimeError(
                f"Could not reach the connector to resolve room "
                f"{record.room_id} for watcher '{record.watcher_name}' — the "
                f"{verb} is refused and the record left as it is. Retry: {exc}"
            ) from exc
        if current is not None:
            return
        logger.warning(
            "Room %s is not available to this connector (gone, in another "
            "team, or this account is no longer in it) — the %s of watcher "
            "'%s' is refused and its record left as it is (session %s); "
            "'expire' reclaims it",
            record.room_id, verb, record.watcher_name, record.session_id or "-",
        )
        raise RuntimeError(
            f"Room {record.room_id} is not available to this connector (gone, "
            f"in another team, or this account is no longer in it) — the {verb} "
            f"is refused and watcher '{record.watcher_name}' is left as it is. "
            f"This is final, not a retry: 'expire {record.watcher_name}' "
            f"reclaims its record."
        )

    async def reset_watcher(self, name: str) -> None:
        await self._lifecycle.reset_watcher(name)

    async def expire_watcher(self, name: str) -> None:
        """The §2.8 `expire` verb: operator-initiated reclamation, now.

        Runs the same forced-reclamation path a membership removal runs — pause
        overridden with an audit line — but raises where the event handlers
        swallow: an operator watching the command must see the failure the
        connector must not. Only a rule-derived record is expirable; a static
        watcher's owner is config.yaml, and there is nothing durable to reclaim
        for it.

        **Scheduled jobs are deliberately NOT cancelled** (owner, 2026-08-31).
        This shared the membership-removal path's cancellation, whose reason was
        "a job left in the store would fire forever at a room that cannot
        answer". That holds when the bot has been removed from the room. It does
        not hold here: the room is still there, the bot is still in it, and a
        watcher handle is a pure function of `(connector, room)` — so the room's
        next message recreates a watcher under the SAME name and the job starts
        working again. Cancelling destroyed something that recovers on its own,
        and it destroyed it silently, for an operator who asked about a watcher
        and said nothing about their schedules.

        A job whose room has not spoken yet fails audibly instead: the scheduler
        logs the failed injection, advances `next_run`, and — deliberately —
        does not consume a finite job's `run_count`.

        **Refused on a connector without unsolicited inbound** (owner, PR #140):
        the recreation this verb promises needs a message that can arrive on
        its own, and voice/script have none — see the check below.
        """
        state = self._lifecycle.get_watcher_state(name)
        if state is None or not state.config or not state.room_id:
            raise RuntimeError(
                f"No expirable record for watcher '{name}' — expire acts on a "
                f"rule-derived record, and this name has none."
            )
        if not self._connector.supports_unsolicited_inbound():
            # Owner's decision (PR #140, Codex round 1): expire's contract is
            # "reclaimed now, recreated by the room's next message". An eager
            # connector (§2.6 — voice, script) has no next message: nothing is
            # pushed to it, so nothing routes an unclaimed room into watcher
            # creation, and its injections/requests would meet no processor
            # until the daemon restarts or a scheduled wake happens to fire.
            # That is a silent outage, not an expiry. `reset` is the verb with
            # the effect expire can honestly have here — a fresh session,
            # record and processor kept.
            raise RuntimeError(
                f"Watcher '{name}' is on connector '{self._connector.name}', "
                f"which receives no unsolicited messages — nothing would bring "
                f"an expired watcher back until the daemon restarts or a "
                f"scheduled job happens to wake it. Use "
                f"'agent-chat-gateway reset {name}' to clear its session instead."
            )
        # The destructive verbs join the shutdown barrier (internal review of
        # the barrier close): expire was outside flag+counter, protected only
        # by the ControlServer's stop ordering — incidental and
        # Python-version-dependent. reclaim_room itself stays unbarriered on
        # purpose: its other two callers are the live removal event (entry-
        # gated on disarm) and the reconciliation (rides the sweep task,
        # which shutdown cancels and awaits).
        self._lifecycle._enter_verb("expire", name)
        try:
            reclaimed = await self._lifecycle.reclaim_room(
                state.room_id, reason=f"operator 'expire' on watcher '{name}'",
                # The identity pin, without require_dormant (Codex round 3):
                # the operator selected THIS record — following a replacement
                # would delete a newly created watcher and contradict the
                # error below — but expire acts on active and dormant records
                # alike.
                expected=state,
            )
        finally:
            self._lifecycle._exit_verb()
        if reclaimed is None:
            raise RuntimeError(
                f"Watcher '{name}' was not reclaimed — its record changed "
                f"while the expire ran. Re-check 'list' and retry."
            )

    async def _resolve_room_for_wake(self, room_id: str) -> "RoomRef | None":
        """Describe a room well enough to recreate a watcher for it, from its id.

        Separated from the wake so the two failure shapes stay legible, because
        the connector contract distinguishes them and the caller acts on them
        differently: `None` is permanent (no such room, not ours, not a member),
        a raise is transient (could not ask). Both end as "no processor" here,
        but only after saying which — a permanent absence logged as a retryable
        blip is how a deleted room looks like a network problem forever.
        """
        try:
            room = await self._connector.room_ref_by_id(room_id)
        except Exception as exc:
            logger.warning(
                "Could not reach the connector to resolve room %s — the wake "
                "is skipped and the caller retries at its next slot: %s",
                room_id, exc,
            )
            return None
        if room is None:
            logger.info(
                "Room %s is not available to this connector — no watcher will "
                "be recreated for it. This is final, not a retry: the room is "
                "gone, in another team, or this account is no longer in it.",
                room_id,
            )
        return room

    async def inject_message(self, room_id: str, text: str) -> bool:
        """Inject a synthetic OWNER-role message into the watcher for `room_id`.

        Bypasses the connector layer entirely, avoiding the self-message filter
        that drops messages sent by the bot's own username. The injected message
        is treated as if it came from a trusted owner, so it is processed without
        permission approval prompts.

        **Addressed by room id, and only by room id.** This used to take a
        watcher HANDLE first and a room id as an optional keyword, and that shape
        produced the same defect four times: with an id in hand, the ergonomic
        argument to reach for was the name, and a name is a pure function of
        (connector, room) that another room can take over once the original's
        record is reclaimed. A caller holding only a handle — a job written
        before schema 2 — resolves it ONCE through `resolve_handle` first; that
        is the single by-name entry on the runtime path (§2.8).

        Returns True if the message was accepted into the queue, False otherwise
        (watcher paused, claimed by no rule, room unresolvable, queue full).
        """
        if not room_id:
            raise ValueError("inject_message needs a room id; resolve a handle "
                             "through resolve_handle first")

        # ── The room, described once ──────────────────────────────────────
        # ONE resolution feeds both the wake and the reply address. Re-reading
        # the record after the wake is what once produced an empty room id for
        # a resurrected-under-a-new-name watcher: the agent ran a full turn and
        # the reply went nowhere, while `enqueue` returned True.
        record = self._lifecycle.record_for_room(room_id)
        processor = self._lifecycle.processor_for_room(room_id)
        room: "RoomRef | None"
        if record is not None and (
                processor is not None or not self._connector.supports_room_lookup()):
            # A running watcher: the record describes the room it is serving,
            # and nothing is recreated. (Or a connector that cannot look rooms
            # up, whose base `room_ref_by_id` would read as "gone" — recreate
            # from the record, as before the check existed.)
            room = RoomRef(
                id=record.room_id,
                kind=_room_kind_or_channel(record),
                name=record.room_name,
                participants=tuple(record.participants),
            )
        else:
            # No resident processor: this fire RECREATES a watcher, from the
            # record or from nothing, and either way the connector is asked
            # first (#145, #141). A record's room fields say what the room was
            # when the record was written, not whether this connector still
            # serves it; and with no record — `expire` reclaimed it, or it
            # never existed — there is nothing else to ask. A name is
            # display-only and a rename frees it for another room (§2.3),
            # which is the whole reason this path takes an id. The lookup
            # precedes the pause decision `get_or_create` makes under its
            # lock, so a job aimed at a paused room pays it at every slot.
            room = await self._resolve_room_for_wake(room_id)
            if room is None and record is not None:
                # `_resolve_room_for_wake` said which failure it was, by room
                # id; this line adds what the record knows, so the session id
                # is in the log for this site too (#145). Nothing is
                # reclaimed — `expire` is the operator's, as for `resume`.
                logger.warning(
                    "inject_message: watcher '%s' for room %s is not recreated "
                    "(see the line above) — its record is left as it is "
                    "(session %s); if the room is gone for good, 'expire %s' "
                    "reclaims it",
                    record.watcher_name, room_id, record.session_id or "-",
                    record.watcher_name,
                )

        # ── The processor ─────────────────────────────────────────────────
        # Resident for THIS room, else `get_or_create`, which is also where
        # pause, the creation cap and the rule match are decided — what makes
        # "a job cannot reach a room a message could not" true, not asserted.
        if processor is None and self._watcher_manager is not None and room is not None:
            try:
                processor = await self._watcher_manager.get_or_create(
                    self._connector_name, room,
                )
            except StaleRecordError:
                # The record this wake read was reclaimed while `get_or_create`
                # waited on the watcher lock (an expiry, or `expire`). The
                # contract is "raise, and the caller retries" — connector
                # routing does; this path took the exception as a failed
                # delivery and advanced `next_run`, which for a date-anchored
                # one-shot meant next year (Codex, PR #140). One re-read: the
                # room now has no record, so the retry takes `_create`.
                processor = await self._watcher_manager.get_or_create(
                    self._connector_name, room,
                )
        label = record.watcher_name if record is not None else room_id
        if processor is None:
            logger.warning(
                "inject_message: no active processor for room %s (%s) — the "
                "watcher may be paused, claimed by no rule, or the room "
                "unresolvable", room_id, label,
            )
            return False
        if room is None:
            logger.warning(
                "inject_message: room %s could not be described — the reply "
                "would have nowhere to go, so the message is not injected.",
                room_id,
            )
            return False
        room_name = room.name or room_label(room)

        msg = IncomingMessage(
            id=f"sched-{secrets.token_hex(8)}",
            # Epoch milliseconds (as a string), matching the format RC's own
            # messages carry — RocketChatConnector.format_prompt_prefix() feeds
            # this straight into ts_ms_to_iso_local(), which only parses epoch-ms.
            # A plain ISO string here silently drops ts:/day: from the header,
            # which is exactly the "scheduled stock report" scenario in #53.
            timestamp=str(int(datetime.now(UTC).timestamp() * 1000)),
            room=Room(id=room.id, name=room_name, type=room.kind.value),
            sender=User(id=SCHEDULER_SENDER_ID, username=SCHEDULER_SENDER_ID,
                        display_name="Scheduler"),
            role=UserRole.OWNER,
            text=text,
            attachments=[],
            warnings=[],
            thread_id=None,
            raw={},
        )
        accepted = await processor.enqueue(msg)
        if not accepted:
            logger.warning(
                "inject_message: message for room %s (%s) was dropped (queue "
                "full or processor stopped)", room_id, label,
            )
        return accepted

    def resolve_handle(self, handle: str) -> str:
        """The room id a watcher HANDLE currently names, or `""`.

        **The one by-name lookup on the runtime path.** Everything downstream —
        `inject_message`, `notify_watcher_room`, the scheduler's pause check and
        failure notice, job cancellation — takes a room id and nothing else, so a
        caller holding only a handle (a job written before schema 2, and nothing
        else) comes through here exactly once. Fenced by
        `tests/unit/test_by_name_lookups_are_fenced.py`, which walks the runtime
        modules and fails on any other by-name call.

        A handle is the weaker key: a pure function of (connector, room) that
        another room can take over once the original's record is reclaimed. The
        answer here is therefore "whatever room currently answers to this name",
        which is the best a handle can do — and why `schedule migrate` records
        the id so no job has to ask.
        """
        record = self._lifecycle.get_watcher_state(handle)
        return record.room_id if record is not None else ""

    async def notify_watcher_room(self, room_id: str, text: str) -> bool:
        """Send a notification directly to `room_id` via the connector.

        Bypasses the watcher queue entirely — used for system notifications
        (scheduler injection failures) that should reach the room even when the
        watcher is paused or its queue is full. Room id only, for the reason
        `inject_message` gives: addressed by handle, a job's failure notice once
        went to whichever room had taken the handle over.

        Returns True if sent, False on error.
        """
        from ..agents.response import AgentResponse  # local import avoids circular dependency

        if not room_id:
            raise ValueError("notify_watcher_room needs a room id")
        try:
            await self._connector.send_text(room_id, AgentResponse(text=text))
            return True
        except Exception as e:
            logger.warning(
                "notify_watcher_room: failed to send notification to room %s: %s",
                room_id, e,
            )
            return False

    # ── Control command dispatch (called by GatewayService) ───────────────────

    async def dispatch_command(self, request: dict) -> dict:
        cmd = request.get("cmd")

        if cmd == "list":
            # An unparseable filter is an error, not a silent fallback to the
            # default: the caller asked a specific question and cannot tell from
            # the rows that it was answered with a different one.
            try:
                state_filter = parse_state_filter(request.get("states"))
            except (ValueError, TypeError) as e:
                # TypeError too: `parse_state_filter` iterates what it is given,
                # and a hand-written socket client can send `"states": 5`. The
                # CLI always sends a list, so this arm is unreachable from it —
                # but escaping as a TypeError turns a bad request into a
                # per-connector "failed to list watchers", which reads as the
                # daemon being broken rather than the request being wrong.
                return {"ok": False, "error": f"invalid 'states' filter: {e}"}
            return {"ok": True, "data": self.list_watchers(state_filter)}

        elif cmd == "pause":
            name = request.get("watcher_name", "")
            if not name:
                return {"ok": False, "error": "Missing 'watcher_name' for 'pause' command"}
            try:
                await self.pause_watcher(name)
                return {"ok": True}
            except Exception as e:
                return {"ok": False, "error": str(e)}

        elif cmd == "resume":
            name = request.get("watcher_name", "")
            if not name:
                return {"ok": False, "error": "Missing 'watcher_name' for 'resume' command"}
            try:
                await self.resume_watcher(name)
                return {"ok": True}
            except Exception as e:
                return {"ok": False, "error": str(e)}

        elif cmd == "reset":
            name = request.get("watcher_name", "")
            if not name:
                return {"ok": False, "error": "Missing 'watcher_name' for 'reset' command"}
            try:
                await self.reset_watcher(name)
                return {"ok": True}
            except Exception as e:
                return {"ok": False, "error": str(e)}

        elif cmd == "expire":
            name = request.get("watcher_name", "")
            if not name:
                return {"ok": False, "error": "Missing 'watcher_name' for 'expire' command"}
            try:
                await self.expire_watcher(name)
                return {"ok": True}
            except Exception as e:
                return {"ok": False, "error": str(e)}

        else:
            return {"ok": False, "error": f"Unknown command: {cmd}"}
