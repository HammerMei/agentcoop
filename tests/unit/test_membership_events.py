"""Membership events (§2.7): a removal reclaims the record, a join registers one.

The removal half pins what makes it different from expiry, which shares its
reclamation body: a remove stops a resident processor (expiry bails on
residency), overrides pause with an audit line (expiry never touches paused),
and runs under no TTL. And what makes it the same: everything the record
points at is reclaimed, record popped last, idempotently — a removal
discovered twice reaches the same end state.
"""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from gateway.core.room_pattern import RoomPattern
from gateway.core.session_manager import JOBS_CANCELLED_BOT_REMOVED
from gateway.core.state import StateFilter, lifecycle_state, past_expire_ttl
from gateway.core.watcher_manager import RoomRef, WatcherManager, config_from_record
from gateway.core.watcher_rule import RoomKind, RoomMatcher, WatcherRule
from tests.helpers import (
    MockAgentBackend,
    install_record,
    make_core_config,
    make_lifecycle,
    make_rule_derived_record,
    register_processor,
)


def _harness(records, *, processors=None):
    """A real lifecycle holding real records, its collaborators doubled —
    the idle-sweep suite's shape, without the sweep."""
    connector = MagicMock()
    connector.get_last_processed_ts = MagicMock(return_value=None)
    connector.unsubscribe_room = AsyncMock()
    lifecycle = make_lifecycle(
        connector=connector,
        agents={"default": MockAgentBackend()},
        config=make_core_config(),
    )
    lifecycle._attachment_workspace = MagicMock()
    for r in records:
        install_record(lifecycle, r, as_name=r.watcher_name)
    for name, proc in (processors or {}).items():
        register_processor(lifecycle, name, proc)
    return lifecycle, connector


def _resident_processor():
    processor = MagicMock()
    processor.has_work_in_flight = False
    processor.stop = AsyncMock()
    return processor


class TestARemovalReclaimsTheRecord(unittest.IsolatedAsyncioTestCase):

    async def test_an_idle_record_is_reclaimed_entirely(self):
        record = make_rule_derived_record(dropped_at="2026-08-01T00:00:00+00:00")
        lifecycle, connector = _harness([record])

        name = await lifecycle.reclaim_room("room-w1", reason="removed from the room")

        self.assertEqual(name, "w1")
        self.assertIsNone(lifecycle.get_watcher_state("w1"), "the record is gone")
        connector.unsubscribe_room.assert_awaited_once_with("room-w1", watcher_id="room-w1")
        lifecycle._maps.remove_session.assert_called_with("sess-1")
        lifecycle._attachment_workspace.reclaim.assert_called_once()
        # `save` MERGES the on-disk records, so the pop alone is not a
        # deletion — without prune the write restores the record from disk
        # and the next boot resurrects it (Codex round 3, P1).
        lifecycle._state_store.save.assert_called_with(
            lifecycle.states(), prune={"w1"})

    async def test_a_resident_watcher_is_stopped_first_then_reclaimed(self):
        """Expiry bails on residency; a remove cannot — the bot can be kicked
        from a live room mid-conversation, and leaving the processor running
        against a popped record is the defect the stop exists to prevent."""
        record = make_rule_derived_record()
        proc = _resident_processor()
        lifecycle, connector = _harness([record], processors={"w1": proc})

        name = await lifecycle.reclaim_room("room-w1", reason="kicked")

        self.assertEqual(name, "w1")
        proc.stop.assert_awaited()
        self.assertIsNone(lifecycle.get_processor("w1"))
        self.assertIsNone(lifecycle.get_watcher_state("w1"))

    async def test_pause_is_overridden_and_audited(self):
        """§2.7: removal is not an inference from inactivity — it is the
        platform stating the room is gone, so it overrides the one setting
        every timer honours. Never silently: the audit line is a requirement."""
        record = make_rule_derived_record(paused=True)
        lifecycle, _ = _harness([record])

        with self.assertLogs(
            "coop.core.watcher_lifecycle", level="WARNING"
        ) as captured:
            name = await lifecycle.reclaim_room("room-w1", reason="removed")

        self.assertEqual(name, "w1")
        self.assertIsNone(lifecycle.get_watcher_state("w1"))
        # Two AUDIT lines are expected now: this override, and the session
        # release every reclamation logs (#143). Count the override alone.
        audit = [line for line in captured.output if "pause is being overridden" in line]
        self.assertEqual(len(audit), 1, "the pause override is audited, once")
        self.assertIn("room-w1", audit[0], "the audit names the room")
        self.assertIn("pause", audit[0].lower())

    async def test_a_removal_with_no_record_still_cancels_the_rooms_jobs(self):
        """`expire` reclaims the record but keeps the jobs. A removal that lands
        before anything recreated the watcher finds no record — and used to skip
        cancellation entirely, leaving room-id jobs firing at a room the bot had
        left, every slot, with no record for reconciliation to revisit (Codex,
        PR #140). A room-id job is cancellable by its room alone."""
        cancelled = []
        mgr = _bare_manager_with_membership(
            _cancel_jobs=lambda room_id, name, reason=None, advice=None: cancelled.append((room_id, name)))
        mgr._lifecycle.reclaim_room = AsyncMock(return_value=None)   # nothing reclaimed…
        mgr._lifecycle.record_for_room = MagicMock(return_value=None)  # …because no record exists

        await mgr._reclaim_removed_room("room-gone", reason="removed", jobs=JOBS_CANCELLED_BOT_REMOVED)

        self.assertEqual(cancelled, [("room-gone", "")],
                         "cancelled by room, with no legacy handle to give")

    async def test_a_refused_reclaim_that_leaves_a_record_cancels_nothing(self):
        """The other meaning of `None`: a record exists but was not reclaimed —
        replaced under the identity pin, or static-era. The room is still served
        and its jobs stay. Telling the two apart is what `record_for_room`
        answers, and why `None` alone was never enough to cancel on."""
        cancelled = []
        mgr = _bare_manager_with_membership(
            _cancel_jobs=lambda room_id, name, reason=None, advice=None: cancelled.append((room_id, name)))
        mgr._lifecycle.reclaim_room = AsyncMock(return_value=None)
        mgr._lifecycle.record_for_room = MagicMock(return_value=object())  # a record remains

        await mgr._reclaim_removed_room("room-w1", reason="removed", jobs=JOBS_CANCELLED_BOT_REMOVED)

        self.assertEqual(cancelled, [])

    async def test_no_record_is_an_idempotent_no_op(self):
        """A missed event discovered twice — the live event and a later REST
        failure, or the reconciliation — must reach the same end state."""
        record = make_rule_derived_record()
        lifecycle, connector = _harness([record])

        self.assertEqual(await lifecycle.reclaim_room("room-w1", reason="removed"), "w1")
        self.assertIsNone(await lifecycle.reclaim_room("room-w1", reason="removed"))
        self.assertIsNone(await lifecycle.reclaim_room("room-unknown", reason="removed"))
        connector.unsubscribe_room.assert_awaited_once()

    async def test_a_static_record_is_config_yamls_not_the_events(self):
        """A record with no materialized config is recreated from config.yaml
        at every boot regardless of records — reclaiming it here would delete
        a watermark while leaving the watcher's owner intent in place."""
        record = make_rule_derived_record(config={})
        lifecycle, connector = _harness([record])

        self.assertIsNone(await lifecycle.reclaim_room("room-w1", reason="removed"))
        self.assertIsNotNone(lifecycle.get_watcher_state("w1"), "the record is kept")
        connector.unsubscribe_room.assert_not_awaited()

    async def test_a_missing_frozen_agent_skips_cleanup_but_still_reclaims(self):
        """Codex round 3: 'resolving' a removed frozen agent to the default
        would run delete_session against the wrong backend and walk the
        default agent's working directory. The agent-bound steps are skipped
        (the leak is logged), the agent-independent ones still run, and the
        record is still reclaimed — keeping it would make it immortal."""
        record = make_rule_derived_record(agent="ghost")
        lifecycle, connector = _harness([record])
        default_backend = lifecycle._agents["default"]
        default_backend.delete_session = AsyncMock()

        with self.assertLogs(
            "coop.core.watcher_lifecycle", level="WARNING"
        ) as captured:
            name = await lifecycle.reclaim_room("room-w1", reason="removed")

        self.assertEqual(name, "w1")
        self.assertIsNone(lifecycle.get_watcher_state("w1"), "still reclaimed")
        default_backend.delete_session.assert_not_awaited()
        lifecycle._attachment_workspace.reclaim.assert_not_called()
        # The agent-independent steps still run.
        connector.unsubscribe_room.assert_awaited_once()
        lifecycle._maps.remove_session.assert_called_with("sess-1")
        lifecycle._state_store.save.assert_called_with(
            lifecycle.states(), prune={"w1"})
        self.assertTrue(any("ghost" in line for line in captured.output),
                        "the accepted leak names the missing agent")

    async def test_a_changed_backend_identity_skips_cleanup_but_still_reclaims(self):
        """Codex round 6, the round-3 gate's twin: the agent survives under
        the same NAME with a changed type or working directory.
        `_provision_session` refuses to REUSE a session across that boundary;
        the reclaim must refuse to DELETE across it — the old id means
        nothing (or someone else's session) in the new store."""
        record = make_rule_derived_record(
            backend_identity="claude:/the/old/workdir")
        lifecycle, connector = _harness([record])
        default_backend = lifecycle._agents["default"]
        default_backend.delete_session = AsyncMock()

        with self.assertLogs(
            "coop.core.watcher_lifecycle", level="WARNING"
        ) as captured:
            name = await lifecycle.reclaim_room("room-w1", reason="removed")

        self.assertEqual(name, "w1")
        self.assertIsNone(lifecycle.get_watcher_state("w1"), "still reclaimed")
        default_backend.delete_session.assert_not_awaited()
        lifecycle._attachment_workspace.reclaim.assert_not_called()
        lifecycle._state_store.save.assert_called_with(
            lifecycle.states(), prune={"w1"})
        self.assertTrue(
            any("claude:/the/old/workdir" in line for line in captured.output),
            "the accepted leak names both identities")

    async def test_a_session_with_no_identity_is_unverifiable_at_reclaim(self):
        """Codex round 19: 'cannot check' must not read as 'checked out' — a
        record with a session but an empty backend_identity would have
        delete_session run against whatever backend the agent name resolves
        to NOW, the same cross-store destruction the mismatch branch refuses.
        Skip agent-bound cleanup; still reclaim."""
        record = make_rule_derived_record(backend_identity="")
        lifecycle, connector = _harness([record])
        default_backend = lifecycle._agents["default"]
        default_backend.delete_session = AsyncMock()

        with self.assertLogs(
            "coop.core.watcher_lifecycle", level="WARNING"
        ) as captured:
            name = await lifecycle.reclaim_room("room-w1", reason="removed")

        self.assertEqual(name, "w1")
        self.assertIsNone(lifecycle.get_watcher_state("w1"), "still reclaimed")
        default_backend.delete_session.assert_not_awaited()
        lifecycle._attachment_workspace.reclaim.assert_not_called()
        self.assertTrue(any("no backend identity" in line
                            for line in captured.output))

    async def test_a_failed_prune_restores_the_record(self):
        """Codex round 18: popped-with-disk-copy-intact is the worst state —
        the reconciliation cannot rediscover the room from memory, and any
        later ordinary save merges the stale disk row back for the next boot
        to resurrect. A failed durable prune puts the record back; the next
        discoverer reclaims again and retries the prune."""
        record = make_rule_derived_record()
        lifecycle, connector = _harness([record])
        lifecycle._state_store.save = MagicMock(
            side_effect=OSError("read-only file system"))

        with self.assertRaises(OSError):
            # The raise propagates — the membership handler's safety net (or
            # the expire verb's operator) is the reporting layer.
            await lifecycle.reclaim_room("room-w1", reason="removed")

        self.assertIs(lifecycle.get_watcher_state("w1"), record,
                      "the record is back in memory for the next discoverer")
        # DORMANT-shaped (round 27): the cleanup already stopped the
        # processor and deleted the session, and the reconciliation only
        # examines dormant records — an active-shaped restore was invisible
        # to every retry path.
        self.assertTrue(record.dropped_at,
                        "the restore stamps dropped_at so the reconciliation "
                        "retries the prune")

    async def test_a_failed_stop_does_not_refuse_the_reclaim(self):
        """The room is gone whatever the teardown hit — a remove that refused
        to reclaim on a stop error would keep a session for a room that can
        never receive another message."""
        record = make_rule_derived_record()
        proc = _resident_processor()
        proc.stop = AsyncMock(side_effect=RuntimeError("network died"))
        lifecycle, _ = _harness([record], processors={"w1": proc})

        name = await lifecycle.reclaim_room("room-w1", reason="removed")

        self.assertEqual(name, "w1")
        self.assertIsNone(lifecycle.get_watcher_state("w1"))


class TestAnExpectedRecordPinsTheReclaim(unittest.IsolatedAsyncioTestCase):
    """Codex review of #121, rounds 2–3: two pins, deliberately separate.

    `expected` is an identity pin — a replaced record aborts instead of being
    followed the way a live removal event's retry loop follows it. It does
    NOT require dormancy, because the operator's `expire` acts on active
    records too. `require_dormant` is the reconciliation's extra gate: its
    evidence is a stale snapshot, so the same object woken back to life in
    place is also not its to reclaim."""

    async def test_a_matching_dormant_record_is_reclaimed(self):
        record = make_rule_derived_record(dropped_at="2026-08-01T00:00:00+00:00")
        lifecycle, _ = _harness([record])

        name = await lifecycle.reclaim_room(
            "room-w1", reason="reconciliation",
            expected=record, require_dormant=True)

        self.assertEqual(name, "w1")
        self.assertIsNone(lifecycle.get_watcher_state("w1"))

    async def test_a_replaced_record_aborts_the_reclaim(self):
        stale = make_rule_derived_record(dropped_at="2026-08-01T00:00:00+00:00")
        current = make_rule_derived_record(dropped_at="2026-08-01T00:00:00+00:00")
        lifecycle, connector = _harness([current])

        self.assertIsNone(await lifecycle.reclaim_room(
            "room-w1", reason="reconciliation", expected=stale))
        self.assertIsNotNone(lifecycle.get_watcher_state("w1"),
                             "the newer record is not the snapshot's to delete")
        connector.unsubscribe_room.assert_not_awaited()

    async def test_a_record_woken_back_to_active_aborts_under_require_dormant(self):
        """Same object, no longer dormant: an in-place resume cleared the
        flags between the snapshot and the lock, and a stale snapshot has no
        authority over what just happened. A live removal event (no pins)
        still reclaims it."""
        record = make_rule_derived_record()  # active: no dropped_at, not paused
        lifecycle, connector = _harness([record])

        self.assertIsNone(await lifecycle.reclaim_room(
            "room-w1", reason="reconciliation",
            expected=record, require_dormant=True))
        self.assertIsNotNone(lifecycle.get_watcher_state("w1"))
        connector.unsubscribe_room.assert_not_awaited()

    async def test_the_identity_pin_alone_reclaims_an_active_record(self):
        """The operator's `expire` shape (Codex round 3): `expected` without
        `require_dormant` must reclaim a live, active record — the verb's
        documented job — while still refusing a replacement."""
        record = make_rule_derived_record()  # active
        lifecycle, _ = _harness([record])

        name = await lifecycle.reclaim_room(
            "room-w1", reason="operator expire", expected=record)

        self.assertEqual(name, "w1")
        self.assertIsNone(lifecycle.get_watcher_state("w1"))

    async def test_a_paused_record_still_counts_as_dormant(self):
        """Paused is inside the reconciliation's authority — the snapshot said
        the room is gone, and §2.7 has removal override pause."""
        record = make_rule_derived_record(paused=True)
        lifecycle, _ = _harness([record])

        name = await lifecycle.reclaim_room(
            "room-w1", reason="reconciliation",
            expected=record, require_dormant=True)

        self.assertEqual(name, "w1")
        self.assertIsNone(lifecycle.get_watcher_state("w1"))


def _rule(name="eng", include=("eng-*",), **kwargs):
    return WatcherRule(
        name=name,
        connector="rc",
        agent="default",
        rooms=RoomMatcher(
            include=tuple(RoomPattern(p) for p in include),
            except_for=(),
            direct=False,
            group_direct=False,
        ),
        **kwargs,
    )


def _room(id="r1", kind=RoomKind.CHANNEL, name="eng-backend", participants=()):
    return RoomRef(id=id, kind=kind, name=name, participants=participants)


def _add_harness(rules=None):
    """A real manager over a real lifecycle — the add path writes a record
    and starts nothing, so the whole stack can be real except the stores."""
    lifecycle, connector = _harness([])
    manager = WatcherManager(
        "rc", connector, lifecycle, rules if rules is not None else [_rule()])
    return manager, lifecycle, connector


class TestAJoinRegistersAnIdleRecord(unittest.IsolatedAsyncioTestCase):

    async def test_the_record_is_idle_addressable_and_recreatable(self):
        manager, lifecycle, connector = _add_harness()

        name = await manager.register_on_join(_room())

        self.assertEqual(name, "rc:eng-backend")
        record = lifecycle.get_watcher_state(name)
        self.assertIsNotNone(record, "the record is persisted")
        # Idle, not active and not failed: dropped_at is the join stamp.
        self.assertTrue(record.dropped_at)
        self.assertEqual(
            lifecycle_state(record, resident=False), StateFilter.IDLE)
        # The rule was snapshotted at join (§2.4) and the config materialized —
        # without the frozen config the first-message wake declines the record
        # as static and the room is permanently deaf.
        self.assertIsNotNone(config_from_record(record))
        self.assertEqual(record.rule_name, "eng")
        self.assertTrue(record.rule)
        self.assertEqual(record.room_kind, "channel")
        lifecycle._state_store.save.assert_called()

    async def test_a_join_for_a_recreated_room_does_not_clobber_the_old_record(self):
        """Codex round 7: the same-name/different-room refusal at the SECOND
        install site. `register_on_join` establishes no record exists for the
        ROOM — but a room deleted and recreated under the same platform name
        derives the same watcher NAME, and installing the join's record
        silently replaced the old room's record, pause and session included."""
        manager, lifecycle, connector = _add_harness()
        old = make_rule_derived_record(
            name="rc:eng-backend", room_id="old-room-id", paused=True)
        install_record(lifecycle, old, as_name="rc:eng-backend")

        with self.assertRaises(RuntimeError) as ctx:
            await manager.register_on_join(
                _room(id="recreated-room-id", name="eng-backend"))

        # The raise is contained one layer up: the SessionManager's membership
        # handler logs and swallows (pinned by
        # test_handler_failures_never_reach_the_connector), and the room's
        # first message reports the same exit loudly via the start guard.
        self.assertIn("expire rc:eng-backend", str(ctx.exception))
        self.assertIs(lifecycle.get_watcher_state("rc:eng-backend"), old,
                      "the old room's record — an operator's pause included "
                      "— was left untouched")

    async def test_nothing_is_started(self):
        manager, lifecycle, connector = _add_harness()

        name = await manager.register_on_join(_room())

        record = lifecycle.get_watcher_state(name)
        self.assertEqual(record.session_id, "", "no session is provisioned")
        self.assertIsNone(lifecycle.get_processor(name), "no processor runs")
        connector.subscribe_room.assert_not_called()
        lifecycle._dispatcher.add_processor.assert_not_called()

    async def test_a_duplicate_add_never_restamps_the_clocks(self):
        """A duplicate add event — or an add for a room whose record already
        exists — must not reset dropped_at (pushing expiry out) or re-snapshot
        the rule (breaking sticky binding)."""
        manager, lifecycle, _ = _add_harness()

        name = await manager.register_on_join(_room())
        record = lifecycle.get_watcher_state(name)
        stamped = record.dropped_at

        self.assertIsNone(await manager.register_on_join(_room()))
        self.assertIs(lifecycle.get_watcher_state(name), record)
        self.assertEqual(record.dropped_at, stamped)

    async def test_no_matching_rule_registers_nothing(self):
        manager, lifecycle, _ = _add_harness(rules=[_rule(include=("ops-*",))])

        self.assertIsNone(await manager.register_on_join(_room()))
        self.assertIsNone(lifecycle.get_watcher_state("rc:eng-backend"))

    async def test_a_disarmed_manager_registers_nothing(self):
        manager, lifecycle, _ = _add_harness()
        manager.disarm()

        self.assertIsNone(await manager.register_on_join(_room()))
        self.assertIsNone(lifecycle.get_watcher_state("rc:eng-backend"))

    async def test_a_never_spoken_room_expires_from_the_join_stamp(self):
        """Deliberate (§2.7): the registered record reuses the idle state
        rather than inventing a fourth one, so it inherits idle's full
        semantics — including expiry `session_expire_days` after the join."""
        manager, lifecycle, _ = _add_harness()

        name = await manager.register_on_join(_room())
        record = lifecycle.get_watcher_state(name)

        joined = datetime.fromisoformat(record.dropped_at)
        if joined.tzinfo is None:
            joined = joined.astimezone(timezone.utc)
        self.assertFalse(past_expire_ttl(record, joined + timedelta(days=14)))
        self.assertTrue(past_expire_ttl(record, joined + timedelta(days=16)))

    async def test_the_sweep_reclaims_a_never_woken_registration(self):
        """The reclaim itself, not just the arithmetic: a join-registered
        record is sessionless and was never subscribed, so the expiry leg
        walks the reclaim body with an empty session, no maps entry and no
        live room state — every step best-effort, which is exactly why
        nothing else ever executes this combination."""
        from gateway.core.lifecycle_sweep import LifecycleSweep

        manager, lifecycle, connector = _add_harness()

        name = await manager.register_on_join(_room())
        record = lifecycle.get_watcher_state(name)
        joined = datetime.fromisoformat(record.dropped_at)

        sweep = LifecycleSweep(lifecycle, now=lambda: joined + timedelta(days=16))
        transitioned = await sweep.run_once()

        self.assertEqual(transitioned, [name])
        self.assertIsNone(lifecycle.get_watcher_state(name), "the record is gone")
        # The room was registered, never subscribed — the unsubscribe is the
        # reclaim body's first step and must be a harmless no-op here.
        connector.unsubscribe_room.assert_awaited_once()
        lifecycle._state_store.save.assert_called()


def _bare_manager_with_membership(**attrs):
    from tests.helpers import make_bare_session_manager

    mgr = make_bare_session_manager(**attrs)
    if mgr._watcher_manager is None:
        mgr._watcher_manager = MagicMock()
        mgr._watcher_manager.disarmed = False
        mgr._watcher_manager.register_on_join = AsyncMock(return_value="w1")
    mgr._lifecycle.reclaim_room = AsyncMock(return_value="w1")
    return mgr


class TestTheMembershipHandlers(unittest.IsolatedAsyncioTestCase):
    """The SessionManager half: events reach the manager and the lifecycle,
    jobs are cancelled for a reclaimed name, and nothing ever raises toward
    the connector — a dropped add is re-discovered by the room's first
    message, a dropped remove by the reconciliation."""

    async def test_an_add_registers_through_the_manager(self):
        mgr = _bare_manager_with_membership()
        room = _room()

        await mgr._on_membership_added(room)

        mgr._watcher_manager.register_on_join.assert_awaited_once_with(room)

    async def test_a_remove_reclaims_and_cancels_the_jobs(self):
        cancelled = []
        mgr = _bare_manager_with_membership(_cancel_jobs=lambda room_id, name, reason=None, advice=None: cancelled.append(name))

        await mgr._on_membership_removed("room-w1")

        mgr._lifecycle.reclaim_room.assert_awaited_once()
        self.assertEqual(
            mgr._lifecycle.reclaim_room.call_args.args[0], "room-w1")
        self.assertEqual(cancelled, ["w1"])

    async def test_a_no_op_reclaim_cancels_nothing(self):
        """reclaim_room answering None means no record was reclaimed — a
        static record, or no record at all — and the jobs of a watcher that
        still exists must not be cancelled."""
        cancelled = []
        mgr = _bare_manager_with_membership(_cancel_jobs=lambda room_id, name, reason=None, advice=None: cancelled.append(name))
        mgr._lifecycle.reclaim_room = AsyncMock(return_value=None)

        await mgr._on_membership_removed("room-w1")

        self.assertEqual(cancelled, [])

    async def test_a_disarmed_manager_ignores_both(self):
        """Mid-shutdown, an add must not register after the final save and a
        remove must not race stop_all — both are re-discovered later."""
        mgr = _bare_manager_with_membership()
        mgr._watcher_manager.disarmed = True

        await mgr._on_membership_added(_room())
        await mgr._on_membership_removed("room-w1")

        mgr._watcher_manager.register_on_join.assert_not_awaited()
        mgr._lifecycle.reclaim_room.assert_not_awaited()

    async def test_no_manager_means_no_membership_handling(self):
        """Static-only deployments keep their exact behaviour (§2.7)."""
        mgr = _bare_manager_with_membership()
        mgr._watcher_manager = None

        await mgr._on_membership_added(_room())
        await mgr._on_membership_removed("room-w1")

        mgr._lifecycle.reclaim_room.assert_not_awaited()

    async def test_handler_failures_never_reach_the_connector(self):
        mgr = _bare_manager_with_membership()
        mgr._watcher_manager.register_on_join = AsyncMock(
            side_effect=RuntimeError("boom"))
        mgr._lifecycle.reclaim_room = AsyncMock(
            side_effect=RuntimeError("boom"))

        await mgr._on_membership_added(_room())      # must not raise
        await mgr._on_membership_removed("room-w1")  # must not raise

    async def test_a_failed_job_cancel_does_not_undo_the_reclaim(self):
        def explode(name):
            raise RuntimeError("store is corrupt")

        mgr = _bare_manager_with_membership(_cancel_jobs=explode)

        await mgr._on_membership_removed("room-w1")  # must not raise

        mgr._lifecycle.reclaim_room.assert_awaited_once()

    async def test_connect_only_registers_the_hook_with_the_router(self):
        """The hook is gated exactly like the router: rules exist, or neither
        is registered and a static-only deployment keeps its behaviour."""
        mgr = _bare_manager_with_membership()
        await mgr.connect_only()
        mgr._connector.register_membership_hook.assert_called_once()
        hook = mgr._connector.register_membership_hook.call_args.args[0]
        self.assertEqual(hook.added, mgr._on_membership_added)
        self.assertEqual(hook.removed, mgr._on_membership_removed)

        static = _bare_manager_with_membership()
        static._watcher_manager = None
        await static.connect_only()
        static._connector.register_membership_hook.assert_not_called()


class TestTheMembershipReconciliation(unittest.IsolatedAsyncioTestCase):
    """The backstop (§2.7): dormant records — paused or idle — receive no
    inbound and no timer reclamation, so a missed removal event leaves them
    alive forever. A slow tick probes the platform and reclaims the ones
    whose membership is unambiguously gone. Fail means keep."""

    def _mgr(self, records, *, snapshot, cancelled=None):
        mgr = _bare_manager_with_membership(
            _cancel_jobs=lambda room_id, name, reason=None, advice=None: cancelled.append(name) if cancelled is not None else None)
        mgr._lifecycle.states = MagicMock(
            return_value={r.watcher_name: r for r in records})
        # The pre-reclaim re-read (stale-snapshot guard) resolves by room —
        # modelled against the same records, so "unchanged" answers itself.
        by_room = {r.room_id: r for r in records}
        mgr._lifecycle.record_for_room = MagicMock(side_effect=by_room.get)
        # A method, not a property (core/connector.py) — modelled as one, so
        # the gate's call form is what the test exercises.
        mgr._connector.supports_unsolicited_inbound = MagicMock(return_value=True)
        mgr._connector.membership_snapshot = AsyncMock(return_value=snapshot)
        reclaimed = []

        async def reclaim(room_id, *, reason, expected=None, require_dormant=False, **kw):
            reclaimed.append((room_id, reason))
            return f"w-{room_id}"

        mgr._lifecycle.reclaim_room = AsyncMock(side_effect=reclaim)
        mgr.reclaimed = reclaimed
        return mgr

    async def test_a_dormant_record_missing_from_the_snapshot_is_reclaimed(self):
        idle = make_rule_derived_record(name="idle", room_id="gone",
                                        dropped_at="2026-08-01T00:00:00+00:00")
        paused = make_rule_derived_record(name="paused", room_id="also-gone",
                                          paused=True)
        kept = make_rule_derived_record(name="kept", room_id="still-here",
                                        dropped_at="2026-08-01T00:00:00+00:00")
        cancelled = []
        mgr = self._mgr([idle, paused, kept], snapshot={"still-here"},
                        cancelled=cancelled)

        await mgr._reconcile_membership()

        self.assertEqual(sorted(r for r, _ in mgr.reclaimed),
                         ["also-gone", "gone"])
        self.assertEqual(sorted(cancelled), ["w-also-gone", "w-gone"])
        for _, reason in mgr.reclaimed:
            self.assertIn("reconciliation", reason)
        by_room = {idle.room_id: idle, paused.room_id: paused}
        for call in mgr._lifecycle.reclaim_room.call_args_list:
            self.assertIs(
                call.kwargs.get("expected"), by_room[call.args[0]],
                "the reconciliation pins each reclaim to the exact record "
                "its snapshot judged — a replacement aborts, never follows")
            self.assertTrue(
                call.kwargs.get("require_dormant"),
                "the reconciliation also passes require_dormant — an "
                "in-place resume is not its to reclaim (round 3)")

    async def test_a_room_that_woke_after_the_snapshot_is_left_alone(self):
        """Codex review of #121: the snapshot ages while the loop awaits
        earlier reclamations — a record that became active again (or was
        replaced by a re-add) is not this snapshot's to reclaim."""
        woken = make_rule_derived_record(name="woken", room_id="gone",
                                         dropped_at="2026-08-01T00:00:00+00:00")
        mgr = self._mgr([woken], snapshot=set())

        # The wake, mid-loop: dormancy re-read happens per record, and this
        # record is no longer dormant by the time the loop reaches it.
        woken.dropped_at = ""

        await mgr._reconcile_membership()

        self.assertEqual(mgr.reclaimed, [])

    async def test_an_unanswered_snapshot_keeps_everything(self):
        """None is 'could not answer', and an unanswered probe must never
        reclaim: an empty set is a claim, None is the absence of one."""
        idle = make_rule_derived_record(name="idle", room_id="gone",
                                        dropped_at="2026-08-01T00:00:00+00:00")
        mgr = self._mgr([idle], snapshot=None)

        await mgr._reconcile_membership()

        self.assertEqual(mgr.reclaimed, [])

    async def test_active_and_static_records_are_not_probed(self):
        active = make_rule_derived_record(name="active", room_id="a1")
        static = make_rule_derived_record(name="static", room_id="s1",
                                          dropped_at="2026-08-01T00:00:00+00:00",
                                          config={})
        mgr = self._mgr([active, static], snapshot=set())

        await mgr._reconcile_membership()

        self.assertEqual(mgr.reclaimed, [])
        mgr._connector.membership_snapshot.assert_not_awaited()

    async def test_a_connector_without_unsolicited_inbound_is_skipped(self):
        idle = make_rule_derived_record(name="idle", room_id="gone",
                                        dropped_at="2026-08-01T00:00:00+00:00")
        mgr = self._mgr([idle], snapshot=set())
        mgr._connector.supports_unsolicited_inbound = MagicMock(return_value=False)

        await mgr._reconcile_membership()

        mgr._connector.membership_snapshot.assert_not_awaited()
        self.assertEqual(mgr.reclaimed, [])

    async def test_the_sweep_runs_it_on_its_own_slower_cadence(self):
        from gateway.core.lifecycle_sweep import LifecycleSweep

        reconcile = AsyncMock()
        sweep = LifecycleSweep(MagicMock(), reconcile=reconcile,
                               reconcile_every=3)

        for _ in range(7):
            await sweep._after_pass()

        self.assertEqual(reconcile.await_count, 2, "passes 3 and 6")

    async def test_a_failed_reconciliation_does_not_kill_the_cadence(self):
        from gateway.core.lifecycle_sweep import LifecycleSweep

        reconcile = AsyncMock(side_effect=RuntimeError("snapshot exploded"))
        sweep = LifecycleSweep(MagicMock(), reconcile=reconcile,
                               reconcile_every=1)

        await sweep._after_pass()  # must not raise
        await sweep._after_pass()

        self.assertEqual(reconcile.await_count, 2)


# ── The connector halves, twins side by side (§2.7) ─────────────────────────
#
# Cross-connector parity is this repo's recurring defect shape, so the RC and
# MM suites below pin the same contract each: the bot's own add reaches
# `hook.added` with a classified RoomRef, its own remove reaches
# `hook.removed` with the room id, other users' events and failures reach
# neither, and nothing ever raises toward the transport.


def _hook():
    from gateway.core.connector import MembershipHook

    added = AsyncMock()
    removed = AsyncMock()
    return MembershipHook(added=added, removed=removed), added, removed


class TestRocketChatMembershipEvents(unittest.IsolatedAsyncioTestCase):

    def _connector(self):
        from gateway.connectors.rocketchat.connector import RocketChatConnector
        from tests.helpers import make_rc_config

        connector = RocketChatConnector.__new__(RocketChatConnector)
        connector.__init__(make_rc_config())
        connector._rest = MagicMock()
        # Pre-login: agent_username falls back to the configured spelling.
        connector._rest.bot_username = None
        connector._ws = MagicMock()
        return connector

    async def test_an_inserted_channel_subscription_registers_the_room(self):
        connector = self._connector()
        hook, added, removed = _hook()
        connector.register_membership_hook(hook)

        await connector._on_membership_event(
            "inserted", {"rid": "r1", "t": "c", "name": "eng-backend"})

        added.assert_awaited_once()
        room = added.call_args.args[0]
        self.assertEqual(room.id, "r1")
        self.assertEqual(room.kind, RoomKind.CHANNEL)
        self.assertEqual(room.name, "eng-backend")
        removed.assert_not_awaited()

    async def test_a_private_group_classifies_as_group(self):
        connector = self._connector()
        hook, added, _ = _hook()
        connector.register_membership_hook(hook)

        await connector._on_membership_event(
            "inserted", {"rid": "r2", "t": "p", "name": "secret"})

        self.assertEqual(added.call_args.args[0].kind, RoomKind.GROUP)

    async def test_a_direct_room_takes_the_member_lookup(self):
        """`t: "d"` covers both DM kinds, and the difference decides whether
        the mention gate applies (§6.4) — so the add pays the same lookup the
        routing path pays, and never guesses."""
        connector = self._connector()
        connector._rest.dm_members = AsyncMock(return_value=["alice", "bob"])
        hook, added, _ = _hook()
        connector.register_membership_hook(hook)

        await connector._on_membership_event("inserted", {"rid": "d1", "t": "d"})

        room = added.call_args.args[0]
        self.assertEqual(room.kind, RoomKind.GROUP_DM)
        self.assertEqual(room.participants, ("alice", "bob"))

    async def test_an_add_outrun_by_its_own_removal_does_not_register(self):
        """Codex round 4, RC twin: the direct-room add pays a member lookup,
        and a removal landing in that await must not be outrun — the
        generation captured before the classification is rechecked as the
        last statement before the hook."""
        connector = self._connector()
        hook, added, _ = _hook()
        connector.register_membership_hook(hook)

        async def members_then_removal_lands(rid):
            # The removal, mid-await: what the removed arm stamps.
            connector._note_membership_loss("d1")
            return ["alice", "bob"]

        connector._rest.dm_members = AsyncMock(
            side_effect=members_then_removal_lands)

        await connector._on_membership_event("inserted", {"rid": "d1", "t": "d"})

        added.assert_not_awaited()

    async def test_a_rooms_events_process_in_arrival_order(self):
        """Structural close: membership hooks are serialized per room, so a
        removal whose reclaim is slow cannot complete AROUND the add that
        arrived after it — the interleavings rounds 4/5/9 each found one of."""
        connector = self._connector()
        connector._rest.dm_members = AsyncMock(return_value=["alice"])
        order: list[str] = []
        removal_gate = asyncio.Event()

        async def slow_removed(rid):
            order.append("removed-start")
            await removal_gate.wait()
            order.append("removed-end")

        async def added(room):
            order.append("added")

        from gateway.core.connector import MembershipHook
        connector.register_membership_hook(
            MembershipHook(added=added, removed=slow_removed))

        t1 = asyncio.create_task(
            connector._on_membership_event("removed", {"rid": "r1", "t": "c"}))
        t2 = asyncio.create_task(
            connector._on_membership_event(
                "inserted", {"rid": "r1", "t": "c", "name": "eng"}))
        for _ in range(10):
            await asyncio.sleep(0)
        self.assertEqual(order, ["removed-start"],
                         "the add waits behind the parked removal")
        removal_gate.set()
        await asyncio.gather(t1, t2)

        self.assertEqual(order, ["removed-start", "removed-end", "added"])

    async def test_a_removed_subscription_reclaims_by_room_id(self):
        connector = self._connector()
        hook, added, removed = _hook()
        connector.register_membership_hook(hook)

        await connector._on_membership_event(
            "removed", {"rid": "r1", "t": "c", "name": "eng-backend"})

        removed.assert_awaited_once_with("r1")
        added.assert_not_awaited()

    async def test_a_failed_classification_is_dropped_not_raised(self):
        """The safety net (§2.7): an add that cannot classify stays
        unregistered, and the room's first message creates its watcher."""
        connector = self._connector()
        connector._rest.dm_members = AsyncMock(side_effect=RuntimeError("down"))
        hook, added, _ = _hook()
        connector.register_membership_hook(hook)

        await connector._on_membership_event("inserted", {"rid": "d1", "t": "d"})

        added.assert_not_awaited()

    async def test_no_hook_means_no_action(self):
        connector = self._connector()
        await connector._on_membership_event(
            "inserted", {"rid": "r1", "t": "c", "name": "x"})  # must not raise

    async def test_the_receive_loop_discards_updated_before_any_task(self):
        """`updated` fires on every unread-count change in every room — it
        must die on the receive loop, not as a spawned task."""
        from gateway.connectors.rocketchat.websocket import RCWebSocketClient

        ws = RCWebSocketClient("http://x", "bot", "pw")
        calls = []

        async def cb(action, doc):
            calls.append(action)

        ws.register_membership_callback(cb)

        def frame(action, doc=None, event="uid1/subscriptions-changed"):
            return {"msg": "changed", "collection": "stream-notify-user",
                    "fields": {"eventName": event,
                               "args": [action, doc if doc is not None
                                        else {"rid": "r1"}]}}

        ws._handle_notify_user(frame("updated"))
        ws._handle_notify_user(frame("inserted", event="uid1/rooms-changed"))
        ws._handle_notify_user({"msg": "changed",
                                "collection": "stream-notify-user",
                                "fields": {"eventName": "uid1/subscriptions-changed",
                                           "args": ["inserted"]}})
        self.assertEqual(ws._callback_tasks, set(),
                         "nothing spawned for filtered frames")

        ws._handle_notify_user(frame("inserted"))
        ws._handle_notify_user(frame("removed"))
        await asyncio.gather(*ws._callback_tasks)
        self.assertEqual(calls, ["inserted", "removed"])


class TestMattermostMembershipEvents(unittest.IsolatedAsyncioTestCase):

    def _connector(self):
        from tests.unit.test_mattermost_connector import _make_connector

        connector = _make_connector()
        connector._rest.team_id = "team-1"
        # FAITHFUL to the real REST layer (Codex round 13): get_channel
        # normalizes the platform letter to "channel"/"group"/"dm"/"group_dm"
        # via room_type_for. The old mock returned the raw letter "O", which
        # is why no test caught that the letters-only kind lookup made
        # hook.added unreachable for every real response — the mock-drift
        # lesson (get_watcher_config) again.
        connector._rest.get_channel = AsyncMock(return_value={
            "id": "chan-1", "type": "channel", "name": "eng-backend",
            "display_name": "Eng Backend", "team_id": "team-1",
        })
        return connector

    async def _settle(self, connector):
        if connector._routing_tasks:
            await asyncio.gather(*connector._routing_tasks)

    async def test_the_bots_own_add_registers_the_room(self):
        connector = self._connector()
        hook, added, removed = _hook()
        connector.register_membership_hook(hook)

        await connector._on_membership_event({
            "event": "user_added",
            "data": {"user_id": "bot-id-1", "team_id": "team-1"},
            "broadcast": {"channel_id": "chan-1"},
        })
        await self._settle(connector)

        added.assert_awaited_once()
        room = added.call_args.args[0]
        self.assertEqual(room.id, "chan-1")
        self.assertEqual(room.kind, RoomKind.CHANNEL)
        self.assertEqual(room.name, "eng-backend")
        removed.assert_not_awaited()

    async def test_a_channels_events_process_in_arrival_order(self):
        """The MM twin of the RC ordering pin (structural close): hooks
        serialize per channel via `_run_membership`'s lock."""
        connector = self._connector()
        order: list[str] = []
        removal_gate = asyncio.Event()

        async def slow_removed(channel_id):
            order.append("removed-start")
            await removal_gate.wait()
            order.append("removed-end")

        async def added(room):
            order.append("added")

        from gateway.core.connector import MembershipHook
        connector.register_membership_hook(
            MembershipHook(added=added, removed=slow_removed))

        await connector._on_membership_event({
            "event": "user_removed",
            "data": {"channel_id": "chan-1", "remover_id": "admin"},
            "broadcast": {"user_id": "bot-id-1"},
        })
        await connector._on_membership_event({
            "event": "user_added",
            "data": {"user_id": "bot-id-1", "team_id": "team-1"},
            "broadcast": {"channel_id": "chan-1"},
        })
        for _ in range(10):
            await asyncio.sleep(0)
        self.assertEqual(order, ["removed-start"],
                         "the add waits behind the parked removal")
        removal_gate.set()
        await self._settle(connector)

        self.assertEqual(order, ["removed-start", "removed-end", "added"])

    async def test_an_add_outrun_by_its_own_removal_does_not_register(self):
        """Codex round 4: the add's REST classification awaits, and a removal
        landing in that window must win — registering afterwards would create
        an idle record for a room the bot has already left, operable until
        the daily reconciliation. The generation captured at dispatch is what
        the recheck before the hook compares."""
        connector = self._connector()
        hook, added, _ = _hook()
        connector.register_membership_hook(hook)

        async def classify_then_removal_lands(channel_id):
            # The removal, mid-await: what the user_removed arm stamps.
            connector._membership_gen[channel_id] = (
                connector._membership_gen.get(channel_id, 0) + 1)
            return {"id": "chan-1", "type": "O", "name": "eng-backend",
                    "display_name": "Eng Backend", "team_id": "team-1"}

        connector._rest.get_channel = AsyncMock(
            side_effect=classify_then_removal_lands)

        await connector._on_membership_event({
            "event": "user_added",
            "data": {"user_id": "bot-id-1", "team_id": "team-1"},
            "broadcast": {"channel_id": "chan-1"},
        })
        await self._settle(connector)

        added.assert_not_awaited()

    async def test_someone_elses_add_is_ignored(self):
        connector = self._connector()
        hook, added, _ = _hook()
        connector.register_membership_hook(hook)

        await connector._on_membership_event({
            "event": "user_added",
            "data": {"user_id": "someone-else", "team_id": "team-1"},
            "broadcast": {"channel_id": "chan-1"},
        })
        await self._settle(connector)

        added.assert_not_awaited()
        connector._rest.get_channel.assert_not_awaited()

    async def test_the_bots_own_remove_arrives_user_scoped(self):
        """The variant the bot actually receives for itself (verified against
        `channel.go`): the removed user no longer belongs to the channel, so
        the server sends a user-scoped event with the channel in `data` and
        the user in `broadcast` — the mirror image of every other variant."""
        connector = self._connector()
        hook, added, removed = _hook()
        connector.register_membership_hook(hook)

        await connector._on_membership_event({
            "event": "user_removed",
            "data": {"channel_id": "chan-1", "remover_id": "admin"},
            "broadcast": {"user_id": "bot-id-1"},
        })
        await self._settle(connector)

        removed.assert_awaited_once_with("chan-1")
        added.assert_not_awaited()

    async def test_someone_elses_remove_is_ignored(self):
        """The channel-scoped variant, which the bot sees for OTHER people's
        removals from channels it is still in."""
        connector = self._connector()
        hook, _, removed = _hook()
        connector.register_membership_hook(hook)

        await connector._on_membership_event({
            "event": "user_removed",
            "data": {"user_id": "someone-else", "remover_id": "admin"},
            "broadcast": {"channel_id": "chan-1"},
        })
        await self._settle(connector)

        removed.assert_not_awaited()

    async def test_another_teams_channel_is_not_registered(self):
        connector = self._connector()
        connector._rest.get_channel = AsyncMock(return_value={
            "id": "chan-9", "type": "O", "name": "other",
            "display_name": "Other", "team_id": "team-9",
        })
        hook, added, _ = _hook()
        connector.register_membership_hook(hook)

        await connector._on_membership_event({
            "event": "user_added",
            "data": {"user_id": "bot-id-1", "team_id": "team-9"},
            "broadcast": {"channel_id": "chan-9"},
        })
        await self._settle(connector)

        added.assert_not_awaited()

    async def test_a_failed_classification_is_dropped_not_raised(self):
        connector = self._connector()
        connector._rest.get_channel = AsyncMock(side_effect=RuntimeError("down"))
        hook, added, _ = _hook()
        connector.register_membership_hook(hook)

        await connector._on_membership_event({
            "event": "user_added",
            "data": {"user_id": "bot-id-1", "team_id": "team-1"},
            "broadcast": {"channel_id": "chan-1"},
        })
        await self._settle(connector)

        added.assert_not_awaited()

    async def test_no_hook_means_no_action(self):
        connector = self._connector()
        await connector._on_membership_event({
            "event": "user_added",
            "data": {"user_id": "bot-id-1"},
            "broadcast": {"channel_id": "chan-1"},
        })  # must not raise, must not spawn
        self.assertEqual(connector._routing_tasks, set())


class TestAJoinRegisteredRoomWakesOnFirstMessage(unittest.IsolatedAsyncioTestCase):
    """The seam money test (§2.7): register_on_join → the room's first message
    arrives untracked → the real routing episode finds the record →
    `_recreate` accepts a record with no session and no watermark → a session
    is minted, `dropped_at` clears, and the frozen snapshots survive.

    The decision layers are pinned above; this is the A1 lesson — a mocked
    seam validates the halves while the seam's output corrupts state — so the
    connector, manager, lifecycle and dispatcher are all real here, the same
    harness as the wake suite's."""

    from tests.unit.test_wake_path import (
        TestTheWakeResumesTheSameSession as _WakeSuite,
    )

    _harness = _WakeSuite._harness
    _settle = _WakeSuite._settle

    async def test_join_register_then_first_message_wakes(self):
        from unittest.mock import patch

        from tests.unit.test_wake_path import _ACCESS, _doc

        connector, lifecycle, dispatcher = await self._harness()

        # 1. The join: an idle, sessionless record — nothing started.
        name = await self.manager.register_on_join(
            _room(id="wake-1", name="eng-backend"))
        self.assertEqual(name, "rc:eng-backend")
        record = lifecycle.get_watcher_state(name)
        self.assertEqual(record.session_id, "")
        self.assertEqual(record.last_processed_ts, "")
        self.assertTrue(record.dropped_at)
        created_at = record.created_at
        self.assertNotIn("wake-1", connector._rooms,
                         "a join registers, it does not subscribe")

        # 2. The first message: untracked room → the real routing episode.
        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            await connector._on_unrouted_message(_doc("m1", 1500), _ACCESS)
            await self._settle(connector)

        woken = lifecycle.get_watcher_state(name)
        self.assertTrue(woken.session_id, "a session was minted on the wake")
        self.assertEqual(woken.dropped_at, "", "the wake cleared the idle stamp")
        self.assertEqual(woken.rule_name, "eng",
                         "the join-frozen rule survived the wake")
        self.assertTrue(dict(woken.config),
                        "the join-frozen config survived the wake")
        self.assertEqual(woken.created_at, created_at,
                         "creation time is the join's, not the wake's")
        self.assertIsNotNone(lifecycle.processor_named(name))
        self.assertEqual(self.delivered, ["m1"], "the trigger was delivered")
        # No watermark at the join, so the recreation owes no replay — and the
        # empty boundary must not trip an after_ts="" fetch.
        connector.replay_room_since.assert_not_awaited()

    async def test_a_join_registered_record_is_untouched_by_boot(self):
        """Across a restart the record persists with no watermark and a
        `dropped_at`: the watermark snapshot excludes it (nothing to replay
        from), and the boot evaluation skips it (it is idle, not was-active) —
        verified here so neither loop ever probes with an empty bound."""
        record = make_rule_derived_record(
            name="joined", room_id="r-j", session_id="",
            dropped_at="2026-08-16T00:00:00+00:00", last_processed_ts="")
        mgr = _bare_manager_with_membership()
        mgr._lifecycle.states = MagicMock(return_value={"joined": record})
        mgr._watcher_manager.get_or_create = AsyncMock()

        self.assertEqual(mgr._snapshot_watermarks(), {},
                         "no watermark, nothing to replay from")
        await mgr._evaluate_lifecycle_at_boot()
        mgr._watcher_manager.get_or_create.assert_not_awaited()
        self.assertEqual(record.dropped_at, "2026-08-16T00:00:00+00:00",
                         "boot restamps nothing on an idle record")


class TestTheWiring(unittest.IsolatedAsyncioTestCase):
    """The one layer everything above mocks past: registration reaches the
    transport, and the wire subscription is gated on the hook."""

    def _rc(self):
        from gateway.connectors.rocketchat.connector import RocketChatConnector
        from tests.helpers import make_rc_config

        connector = RocketChatConnector(make_rc_config())
        connector._rest = MagicMock()
        # Pre-login: agent_username falls back to the configured spelling.
        connector._rest.bot_username = None
        connector._ws = MagicMock()
        connector._ws.subscribe_all = AsyncMock(return_value=True)
        connector._ws.unsubscribe_rooms_keeping_callbacks = AsyncMock()
        connector._ws.subscribe_membership_events = AsyncMock(return_value=True)
        connector._router = lambda room, trigger: None
        return connector

    async def test_rc_subscribes_membership_events_iff_the_hook_exists(self):
        connector = self._rc()
        await connector.start_inbound()
        connector._ws.subscribe_membership_events.assert_not_awaited()

        hook, _, _ = _hook()
        connector.register_membership_hook(hook)
        await connector.start_inbound()
        connector._ws.subscribe_membership_events.assert_awaited_once()
        connector._ws.register_membership_callback.assert_called_once_with(
            connector._on_membership_event)

    async def test_mm_connect_registers_the_membership_handler(self):
        from tests.unit.test_mattermost_connector import _make_connector

        connector = _make_connector()
        connector._rest.authenticate = AsyncMock()
        connector._rest.get_me = AsyncMock()
        connector._rest.resolve_team = AsyncMock()
        connector._ws = MagicMock()
        connector._ws.connect = AsyncMock()

        await connector.connect()

        connector._ws.register_membership_handler.assert_called_once_with(
            connector._on_membership_event)


if __name__ == "__main__":
    unittest.main()
