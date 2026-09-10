"""Startup replay over persisted records, and what it stands on (§2.2, §2.4).

Three layers, tested in order:

1. `sync_watchers` must not prune rule-derived records and must hydrate them —
   the prune line `persisted - config_names` *is* the static model, and left
   alone it would delete cross-restart sticky binding, the paused-record drop,
   and the replay's iteration source in one stroke.
2. `_replay_persisted_records` probes each record's gap, recreates only rooms
   with one, and replays through the connector's shared per-room half.
3. The eligibility gates: paused, static, watermarkless and resident records
   are all left alone.
"""

import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from gateway.core.state import WatcherState
from gateway.core.watcher_manager import RoomRef
from gateway.core.watcher_rule import RoomKind
from tests.helpers import make_bare_session_manager, make_lifecycle


def _window(mgr):
    """The down-window as `sync_only` would have frozen it — before anything
    the tests then simulate arriving live."""
    return mgr._snapshot_watermarks()


def _boot_manager(records, *, connector_name="rc", missed=False):
    """A bare `SessionManager` wired for the two boot passes over persisted
    records — the lifecycle evaluation and the startup replay.

    One builder for every class in this file (CLAUDE.md, shared fixtures): the
    connector still serves every room by default (#141), so a test that wants a
    room gone overrides `room_ref_by_id` alone; `missed` is what the probe
    answers for every room. Residency is asked, not remembered — a bare
    MagicMock answers truthily and the residency re-check would then skip
    every record.
    """
    mgr = make_bare_session_manager(_connector_name=connector_name)
    mgr._connector.room_ref_by_id = AsyncMock(
        side_effect=lambda room_id: RoomRef(id=room_id, kind=RoomKind.CHANNEL))
    mgr._connector.probe_missed_since = AsyncMock(return_value=missed)
    mgr._connector.replay_room_since = AsyncMock()
    mgr._lifecycle.states = MagicMock(
        return_value={r.watcher_name: r for r in records})
    mgr._lifecycle.save_state = MagicMock()
    mgr._lifecycle.processor_for_room = MagicMock(return_value=None)
    # A reclamation answers the reclaimed watcher's name and leaves no record
    # behind — what the removal path's tail reads to decide about its jobs.
    names = {r.room_id: r.watcher_name for r in records}
    mgr._lifecycle.reclaim_room = AsyncMock(
        side_effect=lambda room_id, **kw: names.get(room_id))
    mgr._lifecycle.record_for_room = MagicMock(return_value=None)
    mgr._watcher_manager = MagicMock()
    mgr._watcher_manager.get_or_create = AsyncMock(return_value="proc")
    return mgr


def _dynamic_record(name="rc:eng-backend", room_id="r1", **overrides):
    fields = dict(
        watcher_name=name,
        session_id="sess-1",
        room_id=room_id,
        room_type="channel",
        room_name="eng-backend",
        room_kind="channel",
        last_processed_ts="1786874400000",
        connector="rc",
        agent="claude",
        rule_name="eng",
        rule={"name": "eng"},
        config={"name": name, "connector": "rc", "room": "eng-backend",
                "agent": "claude"},
    )
    fields.update(overrides)
    return WatcherState(**fields)


class TestRuleDerivedRecordsSurviveBoot(unittest.IsolatedAsyncioTestCase):
    """The prune exemption and the hydration — the replay's prerequisites."""

    async def test_a_dynamic_record_is_not_pruned_and_is_hydrated(self):
        record = _dynamic_record()
        store = MagicMock()
        store.load = MagicMock(return_value={record.watcher_name: record})
        store.save = MagicMock()
        lifecycle = make_lifecycle(state_store=store)

        await lifecycle.sync_watchers()

        prune = store.save.call_args.kwargs.get("prune")
        self.assertNotIn(record.watcher_name, prune or set(),
                         "a rule-derived record is not an orphan of the config")
        # Hydrated: sticky binding answers for it from boot.
        self.assertIs(lifecycle.record_for_room("r1"), record)

    async def test_a_static_orphan_is_still_pruned(self):
        """The exemption must not disable deliberate removal: a record with no
        rule_name whose config entry is gone was deleted by the operator."""
        record = _dynamic_record(rule_name="", rule={}, config={})
        store = MagicMock()
        store.load = MagicMock(return_value={record.watcher_name: record})
        store.save = MagicMock()
        lifecycle = make_lifecycle(state_store=store)

        await lifecycle.sync_watchers()

        prune = store.save.call_args.kwargs.get("prune")
        self.assertIn(record.watcher_name, prune)

    async def test_a_damaged_rule_name_alone_does_not_cost_the_session(self):
        """Codex round 22: the prune test is BOTH fields, not one — the
        static path never wrote a materialized config, so a record whose
        rule_name alone was hand-damaged still carries everything sticky
        recreation needs. Pruning it destroyed a session over one corrupted
        attribution field."""
        record = _dynamic_record(rule_name="")  # config intact
        store = MagicMock()
        store.load = MagicMock(return_value={record.watcher_name: record})
        store.save = MagicMock()
        lifecycle = make_lifecycle(state_store=store)

        await lifecycle.sync_watchers()

        prune = store.save.call_args.kwargs.get("prune")
        self.assertNotIn(record.watcher_name, prune or set(),
                         "a materialized config alone keeps the record")
        self.assertIs(lifecycle.record_for_room("r1"), record,
                      "and it hydrates — the message path can recreate it")

    async def test_a_hydrated_paused_record_still_answers_paused(self):
        record = _dynamic_record(paused=True)
        store = MagicMock()
        store.load = MagicMock(return_value={record.watcher_name: record})
        store.save = MagicMock()
        lifecycle = make_lifecycle(state_store=store)

        await lifecycle.sync_watchers()

        found = lifecycle.record_for_room("r1")
        self.assertTrue(found.paused, "the pause survives the restart")


class TestStartupReplay(unittest.IsolatedAsyncioTestCase):
    """The replay itself, against a mocked lifecycle and connector."""

    def _manager(self, records):
        return _boot_manager(records)

    async def test_an_empty_gap_leaves_the_room_idle(self):
        """The lazy model working: no messages missed, nothing recreated.

        The probe is the connector's judgment, not a raw history read: the
        agent's own last reply always sits above the watermark (which only
        advances on accepted *inbound*), so a probe that counted it would
        report a gap for nearly every room at every boot."""
        mgr = self._manager([_dynamic_record()])

        await mgr._replay_persisted_records(_window(mgr))

        mgr._watcher_manager.get_or_create.assert_not_awaited()
        mgr._connector.replay_room_since.assert_not_awaited()
        # The probe asked from the stored watermark.
        self.assertEqual(
            mgr._connector.probe_missed_since.await_args.args[1], "1786874400000")

    async def test_a_gap_recreates_from_the_record_and_replays(self):
        record = _dynamic_record(room_kind="group_dm",
                                 participants=["alice", "bob"], room_name="")
        mgr = self._manager([record])
        mgr._connector.probe_missed_since = AsyncMock(return_value=True)

        await mgr._replay_persisted_records(_window(mgr))

        # The probe was asked about the room typed by its *kind*, so a group DM
        # reaches the direct-room history endpoint rather than a channel one.
        self.assertEqual(
            mgr._connector.probe_missed_since.await_args.args[0].type, "group_dm")
        args = mgr._watcher_manager.get_or_create.await_args
        self.assertEqual(args.args[0], "rc")
        room = args.args[1]
        self.assertEqual(room.id, "r1")
        self.assertEqual(room.kind.value, "group_dm",
                         "the RoomRef is rebuilt from the record, not re-resolved")
        self.assertEqual(room.participants, ("alice", "bob"))
        # The recreation replays the record's own window itself, so this loop
        # does not replay again — two passes over one interval are not free:
        # replay hands the filter the same boundary both times, so only the id
        # window stands between them.
        mgr._connector.replay_room_since.assert_not_awaited()

    async def test_ineligible_records_are_left_alone(self):
        records = [
            _dynamic_record(name="w-paused", room_id="r2", paused=True),
            _dynamic_record(name="w-static", room_id="r3", rule_name="", config={}),
            _dynamic_record(name="w-fresh", room_id="r4", last_processed_ts=""),
        ]
        mgr = self._manager(records)
        mgr._connector.probe_missed_since = AsyncMock(return_value=True)

        await mgr._replay_persisted_records(_window(mgr))

        mgr._connector.probe_missed_since.assert_not_awaited()
        mgr._watcher_manager.get_or_create.assert_not_awaited()

    async def test_a_room_a_live_message_already_recreated_is_left_alone(self):
        """Inverted again — the fourth deliberate inversion in this step, and
        the first one a *model* overturned rather than a review finding.

        Writing down replay ownership (§2.2) settled it: a recreation owns the
        replay for the room it recreates, and this loop owns no interval at
        all. A resident room with a record must have come through a recreation
        — its record rules out `_create`, and a rule-derived record is absent
        from `watchers:` so the static path never starts one — so its window is
        already replayed, and replaying here would be a second pass over it.

        The probe still reads the frozen snapshot rather than the record: a
        live message may have advanced the watermark past the whole
        down-window, and reading it would report no gap at all.
        """
        record = _dynamic_record()
        mgr = self._manager([record])
        window = _window(mgr)
        mgr._connector.probe_missed_since = AsyncMock(return_value=True)
        # What the live path did between start_inbound and this loop.
        mgr._lifecycle.processor_for_room = MagicMock(return_value="already-running")
        record.last_processed_ts = "9999999999999"

        await mgr._replay_persisted_records(window)

        self.assertEqual(
            mgr._connector.probe_missed_since.await_args.args[1], "1786874400000",
            "the probe asks about the frozen down-window, not the advanced watermark",
        )
        mgr._watcher_manager.get_or_create.assert_not_awaited()
        mgr._connector.replay_room_since.assert_not_awaited()

    async def test_one_bad_room_does_not_kill_boot(self):
        """Best-effort per record: the probe failing, or the recreation
        raising (which the manager now does on a failed start), logs and moves
        to the next record — the room recovers on its next live message."""
        bad_probe = _dynamic_record(name="w-bad", room_id="r-bad")
        bad_create = _dynamic_record(name="w-worse", room_id="r-worse")
        good = _dynamic_record(name="w-good", room_id="r-good")
        mgr = self._manager([bad_probe, bad_create, good])

        async def probe(room, after_ts):
            if room.id == "r-bad":
                raise RuntimeError("rest down")
            return True

        mgr._connector.probe_missed_since = AsyncMock(side_effect=probe)
        mgr._watcher_manager.get_or_create = AsyncMock(
            side_effect=[RuntimeError("backend down"), "proc"])

        await mgr._replay_persisted_records(_window(mgr))

        # Both failures were survived and the good room was still recovered —
        # by its recreation, which owns the replay.
        self.assertEqual(
            [c.args[1].id for c in mgr._watcher_manager.get_or_create.await_args_list],
            ["r-worse", "r-good"],
        )

    async def test_without_a_manager_the_replay_is_a_no_op(self):
        mgr = self._manager([_dynamic_record()])
        mgr._watcher_manager = None
        await mgr._replay_persisted_records(_window(mgr))
        mgr._connector.probe_missed_since.assert_not_awaited()


class TestTheDownWindowSnapshot(unittest.IsolatedAsyncioTestCase):
    """The snapshot's *placement* is the defence: taken after hydration (which
    is what puts the records in memory) and before the stream opens."""

    def test_only_rule_derived_records_with_a_watermark_are_snapshotted(self):
        from tests.helpers import make_bare_session_manager

        mgr = make_bare_session_manager()
        mgr._lifecycle.states = MagicMock(return_value={
            "keep": _dynamic_record(name="keep"),
            # Static = BOTH provenance fields empty (round 22): a damaged
            # rule_name with a surviving config still replays.
            "static": _dynamic_record(name="static", rule_name="", config={}),
            "fresh": _dynamic_record(name="fresh", last_processed_ts=""),
        })

        window = mgr._snapshot_watermarks()

        self.assertEqual(window, {"keep": "1786874400000"})

    async def test_the_snapshot_is_taken_before_the_stream_opens(self):
        """After start_inbound a live message can advance a watermark past the
        whole down-window, so a snapshot taken later describes nothing."""
        from tests.helpers import make_bare_session_manager

        mgr = make_bare_session_manager()
        order = []
        mgr._connector.start_inbound = AsyncMock(
            side_effect=lambda: order.append("inbound"))
        mgr._lifecycle.sync_watchers = AsyncMock(
            side_effect=lambda **kw: order.append("sync") or [])
        mgr._snapshot_watermarks = MagicMock(
            side_effect=lambda: order.append("snapshot") or {})
        mgr._replay_persisted_records = AsyncMock(
            side_effect=lambda w: order.append("replay"))

        await mgr.sync_only()

        self.assertEqual(order, ["sync", "snapshot", "inbound", "replay"])


if __name__ == "__main__":
    unittest.main()


class TestBootRunsTheSweepsEvaluation(unittest.IsolatedAsyncioTestCase):
    """Boot runs the same evaluation the sweep runs (§2.5) — `past_idle_ttl`,
    one function, two callers — over the records that were active at shutdown.

    Inside its TTL: recreated through `get_or_create` (replay, sticky binding
    and the paused refusal apply for free). Past it: marked idle with a
    *fresh* `dropped_at` — never backdated, which is what keeps
    `active → expired` impossible through an outage of any length. Either
    way, nothing is left in the no-processor-no-dropped_at limbo that `list`
    reports as `failed` after every restart.
    """

    def _manager(self, records):
        mgr = _boot_manager(records)
        self._old = (datetime.now().astimezone()
                     - timedelta(days=16)).isoformat(timespec="seconds")
        self._recent = (datetime.now().astimezone()
                        - timedelta(days=1)).isoformat(timespec="seconds")
        return mgr

    async def test_a_was_active_record_inside_its_ttl_is_recreated(self):
        mgr = self._manager([])
        record = _dynamic_record(
            rule={"name": "eng", "session_idle_days": 15})
        mgr._lifecycle.states = MagicMock(
            return_value={record.watcher_name: record})
        record.last_activity_at = self._recent

        await mgr._evaluate_lifecycle_at_boot()

        mgr._watcher_manager.get_or_create.assert_awaited_once()
        room = mgr._watcher_manager.get_or_create.await_args.args[1]
        self.assertEqual(room.id, "r1")
        self.assertEqual(record.dropped_at, "")

    async def test_a_resident_record_is_not_stamped_idle_by_boot(self):
        """Codex round 18: inbound opens before this pass, so a live message
        can finish recreating a past-TTL record before the loop reaches it —
        the fresh record carries the OLD clock, and stamping it would mark a
        RUNNING watcher idle with nothing ever clearing dropped_at: every
        sweep then takes the expiry leg, whose residency guard blocks it —
        wedged until a restart."""
        mgr = self._manager([])
        record = _dynamic_record(rule={"name": "eng", "session_idle_days": 15})
        record.last_activity_at = self._old  # past TTL on its face
        mgr._lifecycle.states = MagicMock(
            return_value={record.watcher_name: record})
        # …but a wake already made it resident.
        mgr._lifecycle.processor_for_room = MagicMock(return_value="the-proc")

        await mgr._evaluate_lifecycle_at_boot()

        self.assertEqual(record.dropped_at, "",
                         "a resident record is never stamped idle")
        mgr._watcher_manager.get_or_create.assert_not_awaited()

    async def test_a_garbled_room_kind_degrades_instead_of_aborting_boot(self):
        """Codex round 8: `load_state` promises a corrupted record degrades
        rather than taking the service down, and a raising RoomKind(...)
        conversion OUTSIDE the per-record try defeated that one field later —
        one garbled room_kind aborted the whole connector's boot. Unknown
        kinds fall back to CHANNEL, loudly."""
        mgr = self._manager([])
        bad = _dynamic_record(rule={"name": "eng", "session_idle_days": 15})
        bad.room_kind = "channel_typo"
        bad.last_activity_at = self._recent
        good = _dynamic_record(rule={"name": "eng", "session_idle_days": 15})
        good.watcher_name = "w2"
        good.room_id = "r2"
        good.last_activity_at = self._recent
        mgr._lifecycle.states = MagicMock(return_value={
            bad.watcher_name: bad, good.watcher_name: good})

        with self.assertLogs(
            "coop.state", level="WARNING"
        ) as captured:
            await mgr._evaluate_lifecycle_at_boot()

        rooms = [c.args[1] for c in
                 mgr._watcher_manager.get_or_create.await_args_list]
        self.assertEqual(sorted(r.id for r in rooms), ["r1", "r2"],
                         "BOTH records were recreated — the garbled one "
                         "degraded to channel instead of aborting the boot")
        self.assertTrue(any("channel_typo" in line for line in captured.output))

    async def test_a_was_active_record_past_its_ttl_is_marked_idle_fresh(self):
        """Marked idle rather than resumed — and the stamp is *this* boot's
        moment, so the outage is not counted against expiry."""
        from datetime import datetime, timedelta

        mgr = self._manager([])
        record = _dynamic_record(
            rule={"name": "eng", "session_idle_days": 15})
        mgr._lifecycle.states = MagicMock(
            return_value={record.watcher_name: record})
        record.last_activity_at = self._old

        await mgr._evaluate_lifecycle_at_boot()

        mgr._watcher_manager.get_or_create.assert_not_awaited()
        self.assertTrue(record.dropped_at, "stamped idle")
        stamped = datetime.fromisoformat(record.dropped_at)
        self.assertLess(
            datetime.now().astimezone() - stamped, timedelta(minutes=1),
            "fresh — the expiry clock starts now, not in the outage",
        )
        mgr._lifecycle.save_state.assert_called_once()

    async def test_what_boot_must_not_touch(self):
        """Paused (§4.4), already idle (its own leg), static (config.yaml's),
        and roomless records are all left exactly as found."""
        mgr = self._manager([])
        paused = _dynamic_record(name="p", room_id="r-p")
        paused.paused = True
        paused.last_activity_at = self._old
        idle = _dynamic_record(name="i", room_id="r-i")
        idle.dropped_at = "2026-07-01T00:00:00-07:00"
        idle.last_activity_at = self._old
        # Static = BOTH provenance fields empty (round 22): a config-only
        # record is now boot-eligible, so the untouchable case is truly bare.
        static = _dynamic_record(name="s", room_id="r-s", rule_name="", rule={},
                                 config={})
        static.last_activity_at = self._old
        roomless = _dynamic_record(name="r", room_id="")
        roomless.last_activity_at = self._old
        mgr._lifecycle.states = MagicMock(return_value={
            "p": paused, "i": idle, "s": static, "r": roomless})

        await mgr._evaluate_lifecycle_at_boot()

        mgr._watcher_manager.get_or_create.assert_not_awaited()
        self.assertEqual(paused.dropped_at, "")
        self.assertEqual(idle.dropped_at, "2026-07-01T00:00:00-07:00")
        self.assertEqual(static.dropped_at, "")
        self.assertEqual(roomless.dropped_at, "")

    async def test_a_failed_recreation_leaves_the_record_failed_and_continues(self):
        from unittest.mock import AsyncMock

        mgr = self._manager([])
        first = _dynamic_record(
            name="a", room_id="r-a", rule={"name": "eng", "session_idle_days": 15})
        second = _dynamic_record(
            name="b", room_id="r-b", rule={"name": "eng", "session_idle_days": 15})
        first.last_activity_at = self._recent
        second.last_activity_at = self._recent
        mgr._lifecycle.states = MagicMock(return_value={"a": first, "b": second})
        mgr._watcher_manager.get_or_create = AsyncMock(
            side_effect=[RuntimeError("backend down"), "proc"])

        await mgr._evaluate_lifecycle_at_boot()

        self.assertEqual(mgr._watcher_manager.get_or_create.await_count, 2,
                         "one abort does not stop the pass")
        self.assertEqual(first.dropped_at, "", "an abort leaves it failed, honestly")

    async def test_the_evaluation_runs_after_inbound_and_before_the_replay(self):
        """A record the evaluation recreates owns its own replay, so the loop
        must find it resident; a record it idles must still be probed, because
        messages waiting in a room outrank its idleness."""
        from unittest.mock import AsyncMock, MagicMock

        from tests.helpers import make_bare_session_manager

        mgr = make_bare_session_manager()
        order = []
        mgr._connector.start_inbound = AsyncMock(
            side_effect=lambda: order.append("inbound"))
        mgr._lifecycle.sync_watchers = AsyncMock(
            side_effect=lambda **kw: order.append("sync") or [])
        mgr._snapshot_watermarks = MagicMock(
            side_effect=lambda: order.append("snapshot") or {})
        mgr._evaluate_lifecycle_at_boot = AsyncMock(
            side_effect=lambda: order.append("evaluate"))
        mgr._replay_persisted_records = AsyncMock(
            side_effect=lambda w: order.append("replay"))

        await mgr.sync_only()

        self.assertEqual(
            order, ["sync", "snapshot", "inbound", "evaluate", "replay"])


class TestBootValidatesRoomScope(unittest.IsolatedAsyncioTestCase):
    """#141: before boot recreates a watcher from a persisted record — in the
    lifecycle evaluation or in the startup replay — the room is resolved
    through the connector. A connector reconfigured away from the room (another
    Mattermost team, a room the bot left, a deleted room) answers None, and the
    record is reclaimed the way a membership removal reclaims it, jobs
    included, instead of being rebuilt from its stored fields."""

    def _active(self, **overrides):
        record = _dynamic_record(connector="mm", **overrides)
        record.last_activity_at = (datetime.now().astimezone()
                                   - timedelta(days=1)).isoformat(timespec="seconds")
        return record

    async def test_the_evaluation_reclaims_a_room_the_connector_no_longer_serves(self):
        """The issue's own site: a was-active record with NO watermark is
        never reached by the replay loop, so the evaluation is where an
        old-team room would otherwise come back to life."""
        record = self._active(name="mm:old-team-general", room_id="r-old",
                              session_id="sess-old-team-1234", last_processed_ts="")
        mgr = _boot_manager([record], connector_name="mm")
        mgr._connector.room_ref_by_id = AsyncMock(return_value=None)
        mgr._cancel_jobs = MagicMock()

        with self.assertLogs("coop.core.session_manager", level="WARNING") as logs:
            await mgr._evaluate_lifecycle_at_boot()

        mgr._watcher_manager.get_or_create.assert_not_awaited()
        call = mgr._lifecycle.reclaim_room.await_args
        self.assertEqual(call.args[0], "r-old")
        self.assertIs(call.kwargs.get("expected"), record,
                      "pinned to the snapshot the pass walked, like the recreation")
        mgr._cancel_jobs.assert_called_once()
        self.assertEqual(mgr._cancel_jobs.call_args.args[0], "r-old",
                         "the removal path's tail: a room nobody serves keeps no jobs")
        # The full session id is on the AUDIT line the (here mocked) lifecycle
        # emits when it reclaims — asserted with a real lifecycle in
        # test_boot_reconciliation; this seam only sees that the reclaim was asked.
        self.assertTrue(any("not available to this connector" in line for line in logs.output),
                        logs.output)

    async def test_the_replay_reclaims_a_dormant_room_before_probing_it(self):
        """The replay's own recreation site. The room is resolved BEFORE the
        history probe (Codex on #147): a room the bot was removed from, or that
        was deleted, makes the probe itself raise — and a probe failure is
        skipped as best-effort, so a check placed after it would never run and
        the stale record (and its jobs) would survive the boot."""
        record = self._active(name="mm:old-team-idle", room_id="r-idle",
                              session_id="sess-idle-5678")
        record.dropped_at = record.last_activity_at  # idle: skipped by the evaluation
        mgr = _boot_manager([record], connector_name="mm")
        mgr._connector.room_ref_by_id = AsyncMock(return_value=None)
        mgr._connector.probe_missed_since = AsyncMock(
            side_effect=RuntimeError("403: not a channel member"))

        await mgr._replay_persisted_records(_window(mgr))

        mgr._watcher_manager.get_or_create.assert_not_awaited()
        self.assertEqual(mgr._lifecycle.reclaim_room.await_args.args[0], "r-idle")
        # An unserved room is not worth a history read.
        mgr._connector.probe_missed_since.assert_not_awaited()

    async def test_a_resident_room_is_not_resolved_again_by_the_replay(self):
        """The evaluation already resolved and recreated it; the replay's
        check is for the dormant records the evaluation skipped."""
        record = self._active(name="mm:kept", room_id="r-kept")
        mgr = _boot_manager([record], connector_name="mm", missed=True)
        mgr._lifecycle.processor_for_room = MagicMock(return_value="proc")

        await mgr._replay_persisted_records(_window(mgr))

        mgr._connector.room_ref_by_id.assert_not_awaited()

    async def test_boot_reclamation_is_counted_in_the_shutdown_barrier(self):
        """Like the membership-removal path (Codex on #147): a shutdown that
        lands mid-boot must wait for a destructive reclamation to settle, or
        the final save can persist an active-looking record whose session was
        just deleted."""
        record = self._active(name="mm:gone", room_id="r-gone")
        mgr = _boot_manager([record], connector_name="mm")
        mgr._connector.room_ref_by_id = AsyncMock(return_value=None)
        order = []
        mgr._lifecycle._enter_verb = MagicMock(side_effect=lambda *a: order.append("enter"))
        mgr._lifecycle.reclaim_room = AsyncMock(
            side_effect=lambda *a, **k: order.append("reclaim") or "mm:gone")
        mgr._lifecycle._exit_verb = MagicMock(side_effect=lambda: order.append("exit"))

        await mgr._evaluate_lifecycle_at_boot()

        self.assertEqual(order, ["enter", "reclaim", "exit"])
        self.assertEqual(mgr._lifecycle._enter_verb.call_args.args[0], "reclaim")

    async def test_a_reclamation_refused_by_a_shutdown_in_progress_is_dropped(self):
        record = self._active(name="mm:gone", room_id="r-gone")
        mgr = _boot_manager([record], connector_name="mm")
        mgr._connector.room_ref_by_id = AsyncMock(return_value=None)
        mgr._lifecycle._enter_verb = MagicMock(
            side_effect=RuntimeError("the gateway is shutting down"))

        await mgr._evaluate_lifecycle_at_boot()

        mgr._lifecycle.reclaim_room.assert_not_awaited()
        mgr._watcher_manager.get_or_create.assert_not_awaited()

    async def test_a_room_still_served_is_recreated_as_before(self):
        record = self._active(name="mm:kept", room_id="r-kept")
        mgr = _boot_manager([record], connector_name="mm")

        await mgr._evaluate_lifecycle_at_boot()

        mgr._lifecycle.reclaim_room.assert_not_awaited()
        self.assertEqual(mgr._watcher_manager.get_or_create.await_args.args[1].id, "r-kept")

    async def test_a_connector_that_cannot_look_rooms_up_recreates_as_before(self):
        """The base `room_ref_by_id` answers None for a connector with no id
        lookup — "cannot resurrect, callers degrade" — which must not read as
        answered-and-absent here, or such a connector would reclaim every record
        at boot (Codex on #147). The capability is asked first; without it the
        record is recreated from its stored fields as before this change."""
        record = self._active(name="x:kept", room_id="r-kept")
        mgr = _boot_manager([record], connector_name="x")
        mgr._connector.supports_room_lookup = MagicMock(return_value=False)
        mgr._connector.room_ref_by_id = AsyncMock(return_value=None)

        await mgr._evaluate_lifecycle_at_boot()

        mgr._lifecycle.reclaim_room.assert_not_awaited()
        mgr._connector.room_ref_by_id.assert_not_awaited()
        self.assertEqual(mgr._watcher_manager.get_or_create.await_args.args[1].id, "r-kept")

    async def test_a_transient_lookup_failure_leaves_the_record_for_this_boot(self):
        """A raise means "could not ask", not "not ours": nothing is reclaimed
        or recreated, and the room's next live message resolves it again."""
        record = self._active(name="mm:flaky", room_id="r-flaky")
        mgr = _boot_manager([record], connector_name="mm")
        mgr._connector.room_ref_by_id = AsyncMock(side_effect=RuntimeError("rest down"))

        await mgr._evaluate_lifecycle_at_boot()

        mgr._lifecycle.reclaim_room.assert_not_awaited()
        mgr._watcher_manager.get_or_create.assert_not_awaited()
        self.assertEqual(record.dropped_at, "", "left as it was, not stamped idle")

    async def test_an_unserved_room_does_not_stop_the_pass(self):
        """Best-effort per record, like the recreation itself: one room the
        connector no longer serves — even one whose reclamation raises — must
        not keep the rooms after it from coming back."""
        gone = self._active(name="mm:gone", room_id="r-gone")
        kept = self._active(name="mm:kept", room_id="r-kept")
        mgr = _boot_manager([gone, kept], connector_name="mm")
        mgr._connector.room_ref_by_id = AsyncMock(
            side_effect=lambda room_id: None if room_id == "r-gone"
            else RoomRef(id=room_id, kind=RoomKind.CHANNEL))
        mgr._lifecycle.reclaim_room = AsyncMock(side_effect=RuntimeError("lock hiccup"))

        await mgr._evaluate_lifecycle_at_boot()

        self.assertEqual(
            [c.args[1].id for c in mgr._watcher_manager.get_or_create.await_args_list],
            ["r-kept"],
        )
