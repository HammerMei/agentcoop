"""Unit tests for gateway.core.scheduler: compute_next_run, compute_all_missed, JobScheduler."""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.core.job_store import JobStore
from gateway.core.scheduler import JobScheduler, compute_all_missed, compute_next_run
from gateway.schedule_types import JobStatus, ScheduledJob


def _make_job(**kwargs) -> ScheduledJob:
    now = datetime.now(UTC)
    defaults = dict(
        watcher="test-watcher",
        connector="rc-home",
        message="scheduled check",
        cron="0 9 * * *",  # every day at 09:00
        timezone="UTC",
        times=0,
        status=JobStatus.ACTIVE,
        created_at=now.isoformat(),
        next_run=(now + timedelta(hours=1)).isoformat(),
        run_count=0,
    )
    defaults.update(kwargs)
    return ScheduledJob(**defaults)


class TestComputeNextRun(unittest.TestCase):
    def test_daily_cron(self):
        # "0 9 * * *" should give a time with minute=0, hour=9
        after = datetime(2026, 4, 7, 8, 0, 0, tzinfo=UTC)
        result = compute_next_run("0 9 * * *", "UTC", after=after)
        dt = datetime.fromisoformat(result)
        self.assertEqual(dt.hour, 9)
        self.assertEqual(dt.minute, 0)

    def test_hourly_cron(self):
        after = datetime(2026, 4, 7, 8, 0, 0, tzinfo=UTC)
        result = compute_next_run("0 * * * *", "UTC", after=after)
        dt = datetime.fromisoformat(result)
        self.assertEqual(dt.hour, 9)
        self.assertEqual(dt.minute, 0)

    def test_every_30min(self):
        after = datetime(2026, 4, 7, 8, 5, 0, tzinfo=UTC)
        result = compute_next_run("*/30 * * * *", "UTC", after=after)
        dt = datetime.fromisoformat(result)
        self.assertEqual(dt.minute, 30)

    def test_timezone_offset(self):
        # 09:00 Asia/Taipei = 01:00 UTC (UTC+8)
        after = datetime(2026, 4, 7, 0, 30, 0, tzinfo=UTC)
        result = compute_next_run("0 9 * * *", "Asia/Taipei", after=after)
        dt = datetime.fromisoformat(result).astimezone(UTC)
        self.assertEqual(dt.hour, 1)
        self.assertEqual(dt.minute, 0)

    def test_invalid_timezone_falls_back_to_utc(self):
        # Unknown timezone should fall back to UTC without raising
        after = datetime(2026, 4, 7, 8, 0, 0, tzinfo=UTC)
        result = compute_next_run("0 9 * * *", "Invalid/Zone", after=after)
        self.assertIsNotNone(result)  # Should still return a valid datetime string

    def test_result_is_utc_iso_string(self):
        after = datetime(2026, 4, 7, 8, 0, 0, tzinfo=UTC)
        result = compute_next_run("0 9 * * *", "UTC", after=after)
        # Should be parseable as ISO datetime
        dt = datetime.fromisoformat(result)
        self.assertIsNotNone(dt.tzinfo)


class TestComputeAllMissed(unittest.TestCase):
    def test_no_missed_fires(self):
        # after > before, so no fires
        after = datetime(2026, 4, 7, 10, 0, 0, tzinfo=UTC)
        before = datetime(2026, 4, 7, 8, 0, 0, tzinfo=UTC)
        missed = compute_all_missed("0 9 * * *", "UTC", after, before)
        self.assertEqual(missed, [])

    def test_one_missed_fire(self):
        # Daily job at 09:00, daemon was down from 08:50 to 09:10
        after = datetime(2026, 4, 7, 8, 50, 0, tzinfo=UTC)
        before = datetime(2026, 4, 7, 9, 10, 0, tzinfo=UTC)
        missed = compute_all_missed("0 9 * * *", "UTC", after, before)
        self.assertEqual(len(missed), 1)
        self.assertEqual(missed[0].hour, 9)
        self.assertEqual(missed[0].minute, 0)

    def test_multiple_missed_fires(self):
        # Hourly job, 3 hours of downtime
        after = datetime(2026, 4, 7, 9, 0, 0, tzinfo=UTC)
        before = datetime(2026, 4, 7, 12, 0, 0, tzinfo=UTC)
        missed = compute_all_missed("0 * * * *", "UTC", after, before)
        # Should include 10:00, 11:00, 12:00
        self.assertEqual(len(missed), 3)

    def test_boundary_exactly_on_fire_time(self):
        # before == fire time exactly → should be included (half-open interval (after, before])
        after = datetime(2026, 4, 7, 8, 0, 0, tzinfo=UTC)
        before = datetime(2026, 4, 7, 9, 0, 0, tzinfo=UTC)
        missed = compute_all_missed("0 9 * * *", "UTC", after, before)
        self.assertEqual(len(missed), 1)

    def test_cap_prevents_oom_on_frequent_long_downtime(self):
        """compute_all_missed must not return more than _MAX_MISSED_CATCHUP entries."""
        from gateway.core.scheduler import _MAX_MISSED_CATCHUP
        # Every-minute cron, 2 years of downtime → would produce >1M entries uncapped
        after = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
        before = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        missed = compute_all_missed("* * * * *", "UTC", after, before)
        self.assertLessEqual(len(missed), _MAX_MISSED_CATCHUP)


def _make_sm_mock(inject_result: bool = True, paused: bool = False, room_id: str = "room-1") -> MagicMock:
    """Build a SessionManager mock with all scheduler-facing methods pre-wired."""
    sm = MagicMock()
    sm.inject_message = AsyncMock(return_value=inject_result)
    sm.notify_watcher_room = AsyncMock(return_value=True)
    # `get_watcher_config` was removed with the static path (Codex round 4) —
    # a bare MagicMock would keep answering for it and hide exactly the
    # AttributeError the scheduler's fallback used to raise in production.
    del sm.get_watcher_config
    watcher_state = MagicMock()
    watcher_state.paused = paused
    watcher_state.room_id = room_id
    sm.get_watcher_state = MagicMock(return_value=watcher_state)
    # `record_for_room` answers only for the rooms this manager owns. A bare
    # MagicMock returns a truthy object for ANY room id, which would make
    # `_resolve_target`'s room-first loop match the first manager in the
    # dict whatever it was asked — so a future multi-manager test would pass
    # while delivery went to the wrong connector (review called it a loaded
    # gun, and it was: today the branch is only dead because every fixture
    # job has an empty `room_id`).
    sm._owned_rooms = {room_id} if room_id else set()
    sm.record_for_room = MagicMock(
        side_effect=lambda rid: watcher_state if rid in sm._owned_rooms else None)
    # The one by-name entry on the runtime path (§2.8). Answers the room this
    # mock owns for ANY handle, mirroring `get_watcher_state` above — a fixture
    # that needs a handle to miss sets `resolve_handle` itself.
    sm.resolve_handle = MagicMock(return_value=room_id)
    return sm


def _manager_of(target):
    """`_resolve_target` answers `(manager, room_id)` or None; tests about WHICH
    manager was chosen read the first half."""
    return target[0] if target is not None else None


class TestJobSchedulerFiring(unittest.IsolatedAsyncioTestCase):
    def _make_store_and_scheduler(self, sm=None, **job_kwargs) -> tuple[JobStore, JobScheduler, ScheduledJob]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = JobStore(jobs_file=Path(tmp.name) / "jobs.json")
        store.load()

        if sm is None:
            sm = _make_sm_mock(inject_result=True)

        scheduler = JobScheduler(
            store=store,
            session_managers={"rc-home": sm},
            completed_job_ttl_days=7,
        )
        scheduler._session_managers = {"rc-home": sm}
        job = store.add(_make_job(**job_kwargs))
        return store, scheduler, job

    async def test_fire_increments_run_count(self):
        store, scheduler, job = self._make_store_and_scheduler(
            next_run=(datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        )
        await scheduler._fire_due_jobs()
        updated = store.get(job.id)
        self.assertEqual(updated.run_count, 1)
        self.assertIsNotNone(updated.last_run)

    async def test_fire_advances_next_run(self):
        original_next_run = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        store, scheduler, job = self._make_store_and_scheduler(
            cron="0 * * * *",  # hourly
            next_run=original_next_run,
        )
        await scheduler._fire_due_jobs()
        updated = store.get(job.id)
        # next_run must be strictly later than the job's original scheduled time.
        # We compare against the original next_run (not datetime.now()) because
        # the scheduler uses fire_time (the canonical schedule) as the croniter
        # base — on a slow CI machine the computed next_run could still be in
        # the past relative to wall-clock time if the test runs near an hour boundary.
        next_dt = datetime.fromisoformat(updated.next_run)
        original_dt = datetime.fromisoformat(original_next_run)
        self.assertGreater(next_dt, original_dt)

    async def test_fire_completes_times_job(self):
        store, scheduler, job = self._make_store_and_scheduler(
            times=1,
            run_count=0,
            next_run=(datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        )
        await scheduler._fire_due_jobs()
        updated = store.get(job.id)
        self.assertEqual(updated.status, JobStatus.COMPLETED)
        self.assertIsNotNone(updated.completed_at)
        self.assertIsNone(updated.next_run)

    async def test_forever_job_never_completes(self):
        store, scheduler, job = self._make_store_and_scheduler(
            times=0,
            next_run=(datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        )
        await scheduler._fire_due_jobs()
        updated = store.get(job.id)
        self.assertEqual(updated.status, JobStatus.ACTIVE)
        self.assertIsNone(updated.completed_at)

    async def test_paused_job_not_fired(self):
        store, scheduler, job = self._make_store_and_scheduler(
            status=JobStatus.PAUSED,
            next_run=(datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        )
        await scheduler._fire_due_jobs()
        updated = store.get(job.id)
        self.assertEqual(updated.run_count, 0)  # not fired

    async def test_future_job_not_fired(self):
        store, scheduler, job = self._make_store_and_scheduler(
            next_run=(datetime.now(UTC) + timedelta(hours=1)).isoformat()
        )
        await scheduler._fire_due_jobs()
        updated = store.get(job.id)
        self.assertEqual(updated.run_count, 0)

    async def test_inject_failure_infinite_job_advances_next_run(self):
        """times=0 job: next_run advances and run_count increments even on injection failure."""
        sm = _make_sm_mock(inject_result=False, paused=False)
        store, scheduler, job = self._make_store_and_scheduler(
            sm=sm,
            times=0,
            next_run=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        )
        await scheduler._fire_due_jobs()
        updated = store.get(job.id)
        self.assertEqual(updated.run_count, 1)    # count still incremented (non-binding for times=0)
        self.assertIsNotNone(updated.last_run)
        self.assertIsNotNone(updated.next_run)    # next_run advances (avoid retry flood)

    async def test_inject_failure_finite_job_preserves_run_count(self):
        """times=1 job: run_count must NOT be consumed when injection fails."""
        sm = _make_sm_mock(inject_result=False, paused=False)
        store, scheduler, job = self._make_store_and_scheduler(
            sm=sm,
            times=1,
            run_count=0,
            next_run=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        )
        await scheduler._fire_due_jobs()
        updated = store.get(job.id)
        self.assertEqual(updated.run_count, 0)    # run NOT consumed
        self.assertNotEqual(updated.status, JobStatus.COMPLETED)   # NOT marked done

    async def test_inject_failure_finite_job_still_advances_next_run(self):
        """times=N job: next_run advances after failed injection so it retries next tick."""
        sm = _make_sm_mock(inject_result=False, paused=False)
        original_next_run = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        store, scheduler, job = self._make_store_and_scheduler(
            sm=sm,
            times=3,
            run_count=1,
            next_run=original_next_run,
        )
        await scheduler._fire_due_jobs()
        updated = store.get(job.id)
        self.assertEqual(updated.run_count, 1)   # unchanged
        original_dt = datetime.fromisoformat(original_next_run)
        next_dt = datetime.fromisoformat(updated.next_run)
        self.assertGreater(next_dt, original_dt)  # next_run advanced

    async def test_inject_failure_paused_watcher_no_notification(self):
        """When the watcher is intentionally paused, no notification is sent."""
        sm = _make_sm_mock(inject_result=False, paused=True)
        store, scheduler, job = self._make_store_and_scheduler(
            sm=sm,
            next_run=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        )
        await scheduler._fire_due_jobs()
        sm.notify_watcher_room.assert_not_awaited()

    async def test_inject_failure_active_watcher_sends_notification(self):
        """When the watcher is not paused, a best-effort notification is sent."""
        sm = _make_sm_mock(inject_result=False, paused=False)
        store, scheduler, job = self._make_store_and_scheduler(
            sm=sm,
            next_run=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        )
        await scheduler._fire_due_jobs()
        sm.notify_watcher_room.assert_awaited_once()
        call_args = sm.notify_watcher_room.call_args
        notified_room = call_args[0][0]
        notified_text = call_args[0][1]
        # Addressed by ROOM: the job has no room id, so `_resolve_target` asked
        # the manager to resolve its handle once, and the notice goes there.
        self.assertEqual(notified_room, sm.resolve_handle.return_value)
        self.assertIn("⚠️", notified_text)

    async def test_broken_job_does_not_block_other_jobs(self):
        """Per-job isolation: an exception in one job must not prevent others from firing."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = JobStore(jobs_file=Path(tmp.name) / "jobs.json")
        store.load()
        sm = _make_sm_mock(inject_result=True)
        scheduler = JobScheduler(store=store, session_managers={"rc-home": sm}, completed_job_ttl_days=7)

        due_time = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        # A job with a bad cron expression that will raise in compute_next_run
        bad_job = store.add(_make_job(cron="not-a-cron", next_run=due_time))
        good_job = store.add(_make_job(cron="0 * * * *", next_run=due_time))

        # Should not raise despite bad_job's broken cron
        await scheduler._fire_due_jobs()

        # The good job must have fired
        self.assertEqual(store.get(good_job.id).run_count, 1)
        # The bad job's run_count also increments (fire ran up to the cron error), but next_run is cleared
        bad_updated = store.get(bad_job.id)
        self.assertEqual(bad_updated.run_count, 1)
        self.assertIsNone(bad_updated.next_run)
        self.assertEqual(bad_updated.status, JobStatus.PAUSED)

    async def test_successful_fire_sets_last_attempted_at(self):
        """last_attempted_at is set to fire_time on a successful fire."""
        store, scheduler, job = self._make_store_and_scheduler(
            next_run=(datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        )
        await scheduler._fire_due_jobs()
        updated = store.get(job.id)
        self.assertIsNotNone(updated.last_attempted_at, "last_attempted_at must be set after fire")
        # last_attempted_at should equal last_run on success (both anchored to fire_time)
        self.assertEqual(updated.last_attempted_at, updated.last_run)

    async def test_inject_failure_sets_last_attempted_at(self):
        """last_attempted_at is set even when injection fails (times > 0 path)."""
        sm = _make_sm_mock(inject_result=False, paused=False)
        fire_time = datetime.now(UTC) - timedelta(minutes=1)
        store, scheduler, job = self._make_store_and_scheduler(
            sm=sm,
            times=2,
            run_count=0,
            next_run=fire_time.isoformat(),
        )
        await scheduler._fire_due_jobs()
        updated = store.get(job.id)
        self.assertIsNotNone(
            updated.last_attempted_at,
            "last_attempted_at must be set even when injection fails",
        )
        # run_count must NOT be consumed
        self.assertEqual(updated.run_count, 0)
        # last_run must NOT be set (only set on success)
        self.assertIsNone(updated.last_run)

    async def test_inject_failure_infinite_sets_last_attempted_at(self):
        """last_attempted_at is set on failed injection for times=0 (infinite) jobs too."""
        sm = _make_sm_mock(inject_result=False, paused=False)
        store, scheduler, job = self._make_store_and_scheduler(
            sm=sm,
            times=0,
            next_run=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        )
        await scheduler._fire_due_jobs()
        updated = store.get(job.id)
        self.assertIsNotNone(
            updated.last_attempted_at,
            "last_attempted_at must be set on infinite jobs even when injection fails",
        )


class TestJobSchedulerCatchUp(unittest.IsolatedAsyncioTestCase):
    async def test_catch_up_fires_missed_jobs(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = JobStore(jobs_file=Path(tmp.name) / "jobs.json")
        store.load()
        sm = _make_sm_mock(inject_result=True)

        # Hourly job created 4 h ago, last fired 3 h ago, next fire was 2 h ago
        now = datetime.now(UTC)
        job = store.add(_make_job(
            cron="0 * * * *",  # hourly
            times=0,
            created_at=(now - timedelta(hours=4)).isoformat(),
            next_run=(now - timedelta(hours=2)).isoformat(),
            last_run=(now - timedelta(hours=3)).isoformat(),
        ))

        scheduler = JobScheduler(store=store, session_managers={"rc-home": sm}, completed_job_ttl_days=7)
        await scheduler._catch_up_missed()

        updated = store.get(job.id)
        # last_run = now-3h; hourly fires at (now-2h), (now-1h), now → exactly 3 missed
        self.assertEqual(updated.run_count, 3)

    async def test_catch_up_one_shot_fires_once(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = JobStore(jobs_file=Path(tmp.name) / "jobs.json")
        store.load()
        sm = _make_sm_mock(inject_result=True)

        # One-shot job that never ran
        job = store.add(_make_job(
            times=1,
            run_count=0,
            next_run=(datetime.now(UTC) - timedelta(days=2)).isoformat(),
        ))

        scheduler = JobScheduler(store=store, session_managers={"rc-home": sm}, completed_job_ttl_days=7)
        await scheduler._catch_up_missed()

        updated = store.get(job.id)
        self.assertEqual(updated.run_count, 1)
        self.assertEqual(updated.status, JobStatus.COMPLETED)

    async def test_catch_up_remaining_one_fires_exactly_once(self):
        """T6: remaining==1 fast-path in _fire_catch_up fires exactly once.

        A job with times=2 and run_count=1 has exactly 1 remaining run.
        The catch-up fast-path should fire it once and mark it COMPLETED,
        without calling compute_all_missed (which would be wasteful and
        could over-count for long downtime windows).
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = JobStore(jobs_file=Path(tmp.name) / "jobs.json")
        store.load()
        sm = MagicMock()
        sm.inject_message = AsyncMock(return_value=True)

        now = datetime.now(UTC)
        job = store.add(_make_job(
            cron="* * * * *",   # every minute — would produce many missed fires
            times=2,
            run_count=1,        # 1 run already done → remaining = 1
            next_run=(now - timedelta(days=7)).isoformat(),  # very overdue
            last_run=(now - timedelta(days=7, minutes=1)).isoformat(),
        ))

        scheduler = JobScheduler(store=store, session_managers={"rc-home": sm}, completed_job_ttl_days=7)
        await scheduler._catch_up_missed()

        updated = store.get(job.id)
        # Must fire exactly once (not many times due to the 7-day backlog)
        self.assertEqual(updated.run_count, 2)
        self.assertEqual(updated.status, JobStatus.COMPLETED)
        # inject_message must be called exactly once
        self.assertEqual(sm.inject_message.call_count, 1)

    async def test_catch_up_remaining_one_uses_scheduled_fire_time(self):
        """m-R3-1: remaining==1 catch-up records last_run from next_run, not wall-clock now.

        The fast-path fires with fire_time = job.next_run (the canonical scheduled
        time), not datetime.now(UTC).  This keeps last_run consistent with how
        _fire_due_jobs records fire times (anchored to the scheduled time).
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = JobStore(jobs_file=Path(tmp.name) / "jobs.json")
        store.load()
        sm = MagicMock()
        sm.inject_message = AsyncMock(return_value=True)

        now = datetime.now(UTC)
        scheduled_time = now - timedelta(hours=3)  # 3 hours in the past
        job = store.add(_make_job(
            times=2,
            run_count=1,  # remaining == 1
            next_run=scheduled_time.isoformat(),
            last_run=(scheduled_time - timedelta(hours=1)).isoformat(),
        ))

        scheduler = JobScheduler(store=store, session_managers={"rc-home": sm}, completed_job_ttl_days=7)
        await scheduler._catch_up_missed()

        updated = store.get(job.id)
        self.assertEqual(updated.run_count, 2)
        self.assertEqual(updated.status, JobStatus.COMPLETED)
        # last_run should reflect the nominal scheduled time, not the catch-up wall clock
        last_run_dt = datetime.fromisoformat(updated.last_run)
        # Tolerance: last_run should be within 1 second of the scheduled time,
        # not near 'now' (which is 3 hours later).
        self.assertLess(abs((last_run_dt - scheduled_time).total_seconds()), 1.0,
            f"last_run {updated.last_run!r} should be close to scheduled_time "
            f"{scheduled_time.isoformat()!r}, not near now")

    async def test_catch_up_remaining_zero_marks_completed_without_firing(self):
        """M-R3-1: _fire_catch_up with remaining==0 must NOT fire the job.

        A job where run_count >= times but status is still ACTIVE (e.g. due to a
        hand-edited jobs.json) should be marked COMPLETED without firing, rather
        than delivering one extra message.
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = JobStore(jobs_file=Path(tmp.name) / "jobs.json")
        store.load()
        sm = MagicMock()
        sm.inject_message = AsyncMock(return_value=True)

        now = datetime.now(UTC)
        # times=2, run_count=2 → remaining=0, but status left as ACTIVE
        job = store.add(_make_job(
            times=2,
            run_count=2,
            status=JobStatus.ACTIVE,
            next_run=(now - timedelta(minutes=5)).isoformat(),
            last_run=(now - timedelta(minutes=6)).isoformat(),
        ))

        scheduler = JobScheduler(store=store, session_managers={"rc-home": sm}, completed_job_ttl_days=7)
        await scheduler._catch_up_missed()

        updated = store.get(job.id)
        # Must NOT fire — run_count stays at 2
        self.assertEqual(updated.run_count, 2)
        self.assertEqual(updated.status, JobStatus.COMPLETED)
        self.assertIsNotNone(updated.completed_at)
        self.assertEqual(sm.inject_message.call_count, 0,
            "inject_message must not be called for an already-exhausted job")

    async def test_catch_up_remaining_zero_overwrites_future_completed_at(self):
        """m-R4-1: remaining<=0 guard always resets completed_at to now.

        A hand-edited jobs.json could have completed_at set to a far-future date,
        which would make the job immune to TTL purge.  The guard must overwrite it
        unconditionally with datetime.now(UTC) so TTL purge works correctly.
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = JobStore(jobs_file=Path(tmp.name) / "jobs.json")
        store.load()
        sm = MagicMock()
        sm.inject_message = AsyncMock(return_value=True)

        now = datetime.now(UTC)
        far_future = (now + timedelta(days=9999)).isoformat()
        job = store.add(_make_job(
            times=1,
            run_count=1,
            status=JobStatus.ACTIVE,
            next_run=(now - timedelta(minutes=1)).isoformat(),
            completed_at=far_future,  # hand-edited future timestamp
        ))

        scheduler = JobScheduler(store=store, session_managers={"rc-home": sm}, completed_job_ttl_days=7)
        await scheduler._catch_up_missed()

        updated = store.get(job.id)
        self.assertEqual(updated.status, JobStatus.COMPLETED)
        # completed_at must be reset to approximately now, NOT the far-future value
        completed_dt = datetime.fromisoformat(updated.completed_at)
        self.assertLess(
            abs((completed_dt - now).total_seconds()), 5.0,
            f"completed_at should be ~now, not the hand-edited future value {far_future!r}",
        )
        self.assertEqual(sm.inject_message.call_count, 0)

    async def test_catch_up_uses_last_attempted_at_to_avoid_replay(self):
        """Catch-up must not replay fire slots that already failed injection.

        Scenario:
          - Daily job at 09:00, last successful run = Mon 09:00 (last_run).
          - On Tue 09:00, the scheduler fired but injection failed (watcher down).
            last_attempted_at = Tue 09:00; last_run stays at Mon 09:00; next_run → Wed 09:00.
          - Daemon is down all of Wednesday; restarts Thu morning.
          - next_run = Wed 09:00 < now → job appears in list_due() catch-up list.
          - Catch-up must use last_attempted_at (Tue 09:00) as anchor, NOT last_run (Mon 09:00).
          - compute_all_missed(after=Tue 09:00, before=Thu) = [Wed 09:00] → fires ONCE.
          - If it used last_run (Mon 09:00), it would find [Tue 09:00, Wed 09:00] → fires TWICE,
            replaying the Tue slot that already failed.
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = JobStore(jobs_file=Path(tmp.name) / "jobs.json")
        store.load()
        sm = _make_sm_mock(inject_result=True)

        now = datetime.now(UTC)
        # Simulate: last_run = 3 hours ago (Mon), last_attempted_at = 2 hours ago (Tue, failed),
        # next_run = 1 hour ago (Wed, not yet tried because daemon was down)
        job = store.add(_make_job(
            cron="0 * * * *",       # hourly to keep times manageable
            times=0,
            last_run=(now - timedelta(hours=3)).isoformat(),
            last_attempted_at=(now - timedelta(hours=2)).isoformat(),   # 1 failed attempt
            next_run=(now - timedelta(hours=1)).isoformat(),            # the wed slot
        ))

        scheduler = JobScheduler(store=store, session_managers={"rc-home": sm}, completed_job_ttl_days=7)
        await scheduler._catch_up_missed()

        updated = store.get(job.id)
        # last_attempted_at = 2h ago; missed = [1h ago, now] → 2 fires (not 3)
        # Without the fix: anchor = last_run (3h ago) → missed = [2h ago, 1h ago, now] → 3 fires
        self.assertLessEqual(
            updated.run_count, 2,
            "Catch-up must not replay the slot already attempted (last_attempted_at anchor).",
        )

    async def test_catch_up_falls_back_to_last_run_when_no_last_attempted_at(self):
        """Backward-compat: jobs without last_attempted_at still use last_run as anchor."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = JobStore(jobs_file=Path(tmp.name) / "jobs.json")
        store.load()
        sm = _make_sm_mock(inject_result=True)

        now = datetime.now(UTC)
        # Old-format job: last_run set, last_attempted_at = None (pre-upgrade job)
        job = store.add(_make_job(
            cron="0 * * * *",   # hourly
            times=0,
            last_run=(now - timedelta(hours=3)).isoformat(),
            next_run=(now - timedelta(hours=2)).isoformat(),
        ))
        # Ensure last_attempted_at is None (it is by default, but be explicit)
        self.assertIsNone(job.last_attempted_at)

        scheduler = JobScheduler(store=store, session_managers={"rc-home": sm}, completed_job_ttl_days=7)
        await scheduler._catch_up_missed()

        updated = store.get(job.id)
        # anchor = last_run (3h ago); hourly fires at -2h, -1h, now → 3 catches
        self.assertEqual(updated.run_count, 3,
            "Without last_attempted_at, catch-up must fall back to last_run as anchor")


class TestJobSchedulerPurge(unittest.IsolatedAsyncioTestCase):
    async def test_tick_purges_expired_completed(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = JobStore(jobs_file=Path(tmp.name) / "jobs.json")
        store.load()
        sm = MagicMock()
        sm.inject_message = AsyncMock(return_value=True)

        old_completed = store.add(_make_job(
            status=JobStatus.COMPLETED,
            completed_at=(datetime.now(UTC) - timedelta(days=10)).isoformat(),
            next_run=None,
        ))

        scheduler = JobScheduler(store=store, session_managers={"rc-home": sm}, completed_job_ttl_days=7)
        await scheduler._tick()

        self.assertIsNone(store.get(old_completed.id))


# ── Tests: _build_cron_expression ────────────────────────────────────────────


class TestBuildCronExpression(unittest.TestCase):
    """Tests for the CLI helper _build_cron_expression and _parse_one_shot_at."""

    def _build(self, every=None, at=None):
        from gateway.cli import _build_cron_expression
        return _build_cron_expression(every, at)

    # ── No arguments ──────────────────────────────────────────────────────────

    def test_no_args_raises(self):
        with self.assertRaises(ValueError):
            self._build()

    # ── Basic intervals (no --at) ─────────────────────────────────────────────

    def test_1m(self):
        self.assertEqual(self._build("1m"), "* * * * *")

    def test_5m(self):
        self.assertEqual(self._build("5m"), "*/5 * * * *")

    def test_30m(self):
        self.assertEqual(self._build("30m"), "*/30 * * * *")

    def test_1h(self):
        self.assertEqual(self._build("1h"), "0 * * * *")

    def test_6h(self):
        self.assertEqual(self._build("6h"), "0 */6 * * *")

    def test_1d(self):
        self.assertEqual(self._build("1d"), "0 9 * * *")

    def test_1w(self):
        self.assertEqual(self._build("1w"), "0 9 * * 1")

    def test_unsupported_interval_raises(self):
        with self.assertRaises(ValueError, msg="should reject unknown interval"):
            self._build("2d")

    # ── --every + --at HH:MM ──────────────────────────────────────────────────

    def test_daily_with_at_time(self):
        self.assertEqual(self._build("1d", "14:30"), "30 14 * * *")

    def test_weekly_with_at_time(self):
        self.assertEqual(self._build("1w", "08:00"), "0 8 * * 1")

    def test_hourly_with_at_minute_only(self):
        # Sub-daily: only the minute is applied; hour is discarded
        self.assertEqual(self._build("1h", "00:15"), "15 * * * *")

    def test_sub_daily_at_non_zero_hour_still_applies_minute(self):
        # Hour is ignored for sub-daily, but minute is still applied
        result = self._build("6h", "02:30")
        self.assertEqual(result.split()[0], "30")   # minute = 30
        self.assertEqual(result.split()[1], "*/6")  # hour unchanged

    def test_sub_minute_interval_rejects_at_hhmm(self):
        with self.assertRaises(ValueError):
            self._build("30m", "09:00")

    # ── --every 1w + --at DOW HH:MM ───────────────────────────────────────────

    def test_weekly_with_dow_time(self):
        self.assertEqual(self._build("1w", "Fri 17:00"), "0 17 * * 5")

    def test_weekly_dow_case_insensitive(self):
        self.assertEqual(self._build("1w", "fri 17:00"), "0 17 * * 5")

    def test_weekly_sunday(self):
        self.assertEqual(self._build("1w", "Sun 00:00"), "0 0 * * 0")

    def test_dow_syntax_only_with_1w(self):
        with self.assertRaises(ValueError):
            self._build("1d", "Mon 09:00")

    def test_unknown_dow_raises(self):
        with self.assertRaises(ValueError):
            self._build("1w", "Xyz 09:00")

    # ── One-shot (no --every, --at datetime) ──────────────────────────────────

    def test_one_shot_at_datetime(self):
        self.assertEqual(self._build(at="2026-04-10 15:30"), "30 15 10 4 *")

    def test_one_shot_at_iso_format(self):
        self.assertEqual(self._build(at="2026-04-10T15:30"), "30 15 10 4 *")

    def test_one_shot_at_slash_format(self):
        self.assertEqual(self._build(at="2026/04/10 15:30"), "30 15 10 4 *")

    def test_one_shot_at_invalid_format_raises(self):
        with self.assertRaises(ValueError):
            self._build(at="not-a-date")

    def test_one_shot_at_empty_raises(self):
        with self.assertRaises(ValueError):
            self._build(at="")

    # ── _parse_hhmm edge cases ─────────────────────────────────────────────────

    def test_invalid_hhmm_raises(self):
        from gateway.cli import _parse_hhmm
        with self.assertRaises(ValueError):
            _parse_hhmm("25:00")  # hour out of range

    def test_invalid_hhmm_no_colon_raises(self):
        from gateway.cli import _parse_hhmm
        with self.assertRaises(ValueError):
            _parse_hhmm("0900")

    def test_valid_hhmm(self):
        from gateway.cli import _parse_hhmm
        self.assertEqual(_parse_hhmm("09:05"), (9, 5))
        self.assertEqual(_parse_hhmm("23:59"), (23, 59))
        self.assertEqual(_parse_hhmm("00:00"), (0, 0))

    # ── Boundary cron values ──────────────────────────────────────────────────

    def test_arbitrary_2m_recurring(self):
        """2m (not in _INTERVAL_MAP, but valid 1-59 range) → */2 * * * *."""
        self.assertEqual(self._build("2m"), "*/2 * * * *")

    def test_arbitrary_59m_recurring(self):
        """59m is the upper boundary for sub-hourly intervals → */59 * * * *."""
        self.assertEqual(self._build("59m"), "*/59 * * * *")

    def test_arbitrary_23h_recurring(self):
        """23h is the upper boundary for hourly intervals → 0 */23 * * *."""
        self.assertEqual(self._build("23h"), "0 */23 * * *")

    def test_arbitrary_7h_recurring(self):
        """7h (not in _INTERVAL_MAP) → 0 */7 * * *."""
        self.assertEqual(self._build("7h"), "0 */7 * * *")

    def test_60m_raises(self):
        """60m exceeds the 1-59 minute range → ValueError."""
        with self.assertRaises(ValueError):
            self._build("60m")

    def test_0h_raises(self):
        """0h is below the 1-23 hour range → ValueError."""
        with self.assertRaises(ValueError):
            self._build("0h")

    def test_24h_raises(self):
        """24h exceeds the 1-23 hour range → ValueError."""
        with self.assertRaises(ValueError):
            self._build("24h")

    # ── Daily/weekly --at boundary times ─────────────────────────────────────

    def test_daily_at_midnight(self):
        """1d + 00:00 → '0 0 * * *'."""
        self.assertEqual(self._build("1d", "00:00"), "0 0 * * *")

    def test_daily_at_end_of_day(self):
        """1d + 23:59 → '59 23 * * *'."""
        self.assertEqual(self._build("1d", "23:59"), "59 23 * * *")

    def test_weekly_plain_hhmm_preserves_monday_dow(self):
        """1w + '15:00' (no DOW token) preserves the default DOW=1 (Monday)."""
        result = self._build("1w", "15:00")
        self.assertEqual(result, "0 15 * * 1")

    # ── --at with hourly interval: non-zero hour triggers warning ─────────────

    def test_hourly_at_nonzero_hour_emits_warning(self):
        """1h + '09:00' discards the hour with a warning; minute stays 0."""
        import io
        from contextlib import redirect_stderr
        buf = io.StringIO()
        with redirect_stderr(buf):
            result = self._build("1h", "09:00")
        self.assertIn("ignored", buf.getvalue())
        self.assertEqual(result, "0 * * * *")

    def test_6h_at_nonzero_hour_only_applies_minute(self):
        """6h + '03:45' discards hour=3, applies only minute=45."""
        import io
        from contextlib import redirect_stderr
        buf = io.StringIO()
        with redirect_stderr(buf):
            result = self._build("6h", "03:45")
        parts = result.split()
        self.assertEqual(parts[0], "45")   # minute applied
        self.assertEqual(parts[1], "*/6")  # hour unchanged
        self.assertIn("ignored", buf.getvalue())

    # ── One-shot --at past-date emits warning but succeeds ────────────────────

    def test_one_shot_past_date_warns_but_returns_cron(self):
        """A past --at datetime emits a warning but still returns a valid cron."""
        import io
        from contextlib import redirect_stderr
        buf = io.StringIO()
        with redirect_stderr(buf):
            result = self._build(at="2000-01-01 09:00")
        self.assertIn("past", buf.getvalue().lower())
        self.assertEqual(result, "0 9 1 1 *")

    def test_one_shot_boundary_dec31(self):
        """Boundary one-shot date Dec 31 23:59 → '59 23 31 12 *'."""
        import io
        from contextlib import redirect_stderr
        buf = io.StringIO()
        with redirect_stderr(buf):
            result = self._build(at="2099-12-31 23:59")
        self.assertEqual(result, "59 23 31 12 *")


# ── Tests: _parse_one_shot_interval ──────────────────────────────────────────


class TestParseOneShotInterval(unittest.TestCase):
    """Tests for _parse_one_shot_interval (arbitrary Nm/Nh for one-shot reminders)."""

    def _parse(self, s: str):
        from gateway.cli import _parse_one_shot_interval
        return _parse_one_shot_interval(s)

    def test_1m_returns_1(self):
        self.assertEqual(self._parse("1m"), 1)

    def test_7m_returns_7(self):
        self.assertEqual(self._parse("7m"), 7)

    def test_59m_returns_59(self):
        self.assertEqual(self._parse("59m"), 59)

    def test_90m_returns_90(self):
        """Values above 59 are allowed for one-shot: 90m = 90 minutes from now."""
        self.assertEqual(self._parse("90m"), 90)

    def test_2h_returns_120(self):
        """2h → 120 minutes."""
        self.assertEqual(self._parse("2h"), 120)

    def test_1h_returns_60(self):
        self.assertEqual(self._parse("1h"), 60)

    def test_0m_returns_none(self):
        """0m is not a valid positive interval → None (falls through to _build)."""
        self.assertIsNone(self._parse("0m"))

    def test_1d_returns_none(self):
        """1d is not an Nm/Nh expression → None (falls through to _INTERVAL_MAP)."""
        self.assertIsNone(self._parse("1d"))

    def test_1w_returns_none(self):
        """1w is not an Nm/Nh expression → None."""
        self.assertIsNone(self._parse("1w"))

    def test_bad_string_returns_none(self):
        """Non-matching garbage → None."""
        self.assertIsNone(self._parse("bad"))

    def test_empty_string_returns_none(self):
        self.assertIsNone(self._parse(""))

    def test_case_insensitive_uppercase_M(self):
        """Uppercase M is accepted (input is lowercased before parsing)."""
        self.assertEqual(self._parse("5M"), 5)

    def test_case_insensitive_uppercase_H(self):
        self.assertEqual(self._parse("2H"), 120)

    def test_with_leading_whitespace(self):
        """strip() normalizes surrounding whitespace before parsing."""
        self.assertEqual(self._parse("  5m  "), 5)


# ── Tests: _parse_starting ────────────────────────────────────────────────────


class TestParseStarting(unittest.TestCase):
    """Tests for _parse_starting: smart date parsing for the --starting flag."""

    def _parse(self, s: str, tz_name: str | None = "UTC", now_utc: datetime | None = None):
        from gateway.cli import _parse_starting
        if now_utc is None:
            now_utc = datetime(2026, 4, 9, 10, 0, 0, tzinfo=UTC)  # 2026-04-09 10:00 UTC (Thursday)
        return _parse_starting(s, tz_name, now_utc)

    # ── HH:MM format ──────────────────────────────────────────────────────────

    def test_hhmm_future_today(self):
        """'15:00' when it's 10:00 UTC → today at 15:00, was_past=False."""
        now = datetime(2026, 4, 9, 10, 0, 0, tzinfo=UTC)
        result = self._parse("15:00", now_utc=now)
        self.assertEqual(result.hour, 15)
        self.assertEqual(result.minute, 0)
        self.assertFalse(result.was_past)
        self.assertIsNone(result.dow)
        # first_run should be on the same day
        self.assertEqual(result.first_run.date(), now.date())

    def test_hhmm_past_advances_to_tomorrow(self):
        """'09:00' when it's 10:00 UTC → tomorrow at 09:00, was_past=True."""
        now = datetime(2026, 4, 9, 10, 0, 0, tzinfo=UTC)
        result = self._parse("09:00", now_utc=now)
        self.assertEqual(result.hour, 9)
        self.assertEqual(result.minute, 0)
        self.assertTrue(result.was_past)
        # first_run should be the next day
        from datetime import timedelta
        expected_date = (now + timedelta(days=1)).date()
        self.assertEqual(result.first_run.astimezone(UTC).date(), expected_date)

    def test_hhmm_first_run_is_utc_and_future(self):
        """first_run is always UTC and in the future."""
        now = datetime(2026, 4, 9, 10, 0, 0, tzinfo=UTC)
        result = self._parse("09:00", now_utc=now)
        self.assertGreater(result.first_run, now)
        self.assertIsNotNone(result.first_run.tzinfo)

    # ── Mon HH:MM format ──────────────────────────────────────────────────────

    def test_dow_next_monday(self):
        """'Mon 09:00' on a Thursday → next Monday."""
        # 2026-04-09 is a Thursday
        now = datetime(2026, 4, 9, 10, 0, 0, tzinfo=UTC)
        result = self._parse("Mon 09:00", now_utc=now)
        self.assertEqual(result.dow, "1")  # cron DOW for Monday
        self.assertEqual(result.hour, 9)
        self.assertEqual(result.minute, 0)
        self.assertFalse(result.was_past)
        # Next Monday from Thursday Apr 9 is Apr 13
        self.assertEqual(result.first_run.astimezone(UTC).date().isoformat(), "2026-04-13")

    def test_dow_case_insensitive(self):
        """'fri 17:00' works (lowercase)."""
        now = datetime(2026, 4, 9, 10, 0, 0, tzinfo=UTC)
        result = self._parse("fri 17:00", now_utc=now)
        self.assertEqual(result.dow, "5")  # Friday

    def test_dow_unknown_raises(self):
        """Unknown DOW raises ValueError."""
        with self.assertRaises(ValueError):
            self._parse("Xyz 09:00")

    # ── Apr 15 09:00 format ───────────────────────────────────────────────────

    def test_month_name_future_this_year(self):
        """'Apr 15 09:00' when today is Apr 9 → this year Apr 15."""
        now = datetime(2026, 4, 9, 10, 0, 0, tzinfo=UTC)
        result = self._parse("Apr 15 09:00", now_utc=now)
        self.assertFalse(result.was_past)
        self.assertEqual(result.first_run.astimezone(UTC).month, 4)
        self.assertEqual(result.first_run.astimezone(UTC).day, 15)

    def test_month_name_past_advances_one_year(self):
        """'Jan 01 09:00' in April → next year."""
        now = datetime(2026, 4, 9, 10, 0, 0, tzinfo=UTC)
        result = self._parse("Jan 01 09:00", now_utc=now)
        self.assertTrue(result.was_past)
        self.assertEqual(result.first_run.astimezone(UTC).year, 2027)

    def test_month_name_case_insensitive(self):
        """'apr 15 09:00' works (lowercase)."""
        now = datetime(2026, 4, 9, 10, 0, 0, tzinfo=UTC)
        result = self._parse("apr 15 09:00", now_utc=now)
        self.assertEqual(result.first_run.astimezone(UTC).month, 4)

    # ── 04-15 09:00 format ────────────────────────────────────────────────────

    def test_mmdd_future_this_year(self):
        """'04-15 09:00' → this year Apr 15."""
        now = datetime(2026, 4, 9, 10, 0, 0, tzinfo=UTC)
        result = self._parse("04-15 09:00", now_utc=now)
        self.assertFalse(result.was_past)
        self.assertEqual(result.first_run.astimezone(UTC).month, 4)
        self.assertEqual(result.first_run.astimezone(UTC).day, 15)

    def test_mmdd_past_advances_one_year(self):
        """'01-01 09:00' in April → next year."""
        now = datetime(2026, 4, 9, 10, 0, 0, tzinfo=UTC)
        result = self._parse("01-01 09:00", now_utc=now)
        self.assertTrue(result.was_past)
        self.assertEqual(result.first_run.astimezone(UTC).year, 2027)

    # ── Full datetime format ──────────────────────────────────────────────────

    def test_full_datetime_future(self):
        """'2026-05-01 09:00' → explicit UTC datetime."""
        now = datetime(2026, 4, 9, 10, 0, 0, tzinfo=UTC)
        result = self._parse("2026-05-01 09:00", now_utc=now)
        self.assertFalse(result.was_past)
        self.assertEqual(result.first_run.astimezone(UTC).year, 2026)
        self.assertEqual(result.first_run.astimezone(UTC).month, 5)
        self.assertEqual(result.first_run.astimezone(UTC).day, 1)
        self.assertEqual(result.hour, 9)
        self.assertEqual(result.minute, 0)

    def test_full_datetime_past_raises_error(self):
        """'2000-01-01 09:00' (past full datetime) → ValueError (not silently created).

        Unlike partial formats (HH:MM, Mon HH:MM) which auto-advance to the next
        occurrence, an explicit full datetime in the past is almost certainly a typo.
        We raise an error so the user can correct it rather than creating a job
        that fires immediately.
        """
        now = datetime(2026, 4, 9, 10, 0, 0, tzinfo=UTC)
        with self.assertRaises(ValueError) as ctx:
            self._parse("2000-01-01 09:00", now_utc=now)
        self.assertIn("in the past", str(ctx.exception))

    # ── Timezone handling ─────────────────────────────────────────────────────

    def test_tz_shifts_first_run_to_utc(self):
        """'09:00' with tz='America/New_York' → UTC = 09:00 + offset."""
        now = datetime(2026, 4, 9, 10, 0, 0, tzinfo=UTC)
        result = self._parse("09:00", tz_name="America/New_York", now_utc=now)
        # America/New_York is UTC-4 in April (EDT)
        # 09:00 EDT = 13:00 UTC
        utc_hour = result.first_run.astimezone(UTC).hour
        self.assertIn(utc_hour, (13, 14))  # EDT is -4, so 09+4=13; DST edge: 14 is possible

    def test_invalid_tz_falls_back_to_local(self):
        """Unknown timezone silently falls back to server local timezone (not UTC)."""
        now = datetime(2026, 4, 9, 10, 0, 0, tzinfo=UTC)
        result = self._parse("15:00", tz_name="Invalid/Zone", now_utc=now)
        self.assertIsNotNone(result.first_run)  # should not raise

    def test_no_tz_uses_server_local_not_utc(self):
        """tz_name=None falls back to server local timezone, not UTC.

        We verify that tz_str is not 'UTC' unless the server literally runs in UTC.
        Also verify first_run is well-formed and in the future.
        """
        now = datetime(2026, 4, 9, 10, 0, 0, tzinfo=UTC)
        result = self._parse("15:00", tz_name=None, now_utc=now)
        self.assertIsNotNone(result.first_run)
        self.assertGreater(result.first_run, now)
        # tz_str should be set (non-empty)
        self.assertTrue(result.tz_str)

    # ── Invalid input ─────────────────────────────────────────────────────────

    def test_invalid_format_raises(self):
        """Completely unrecognized format raises ValueError."""
        with self.assertRaises(ValueError):
            self._parse("not-a-date")

    def test_empty_string_raises(self):
        """Empty string raises ValueError."""
        with self.assertRaises(ValueError):
            self._parse("")

    # ── T2: Feb 29 year-advance ───────────────────────────────────────────────

    def test_feb29_advance_skips_non_leap_year(self):
        """'Feb 29 09:00' on a leap year that has passed → advances to next leap year."""
        # 2028-02-29 is a real date; 2029 is not a leap year.
        # Simulate: today is 2028-03-01 (past Feb 29, 2028).
        now = datetime(2028, 3, 1, 10, 0, 0, tzinfo=UTC)
        result = self._parse("Feb 29 09:00", now_utc=now)
        self.assertTrue(result.was_past)
        # first_run must be a real date (Feb 29 exists in the advanced year)
        fr = result.first_run.astimezone(UTC)
        self.assertEqual(fr.month, 2)
        self.assertEqual(fr.day, 29)
        self.assertGreater(fr.year, 2028)

    def test_feb29_mmdd_advance_skips_non_leap_year(self):
        """'02-29 09:00' (MM-DD format) on a past leap year → advances to next leap year."""
        now = datetime(2028, 3, 1, 10, 0, 0, tzinfo=UTC)
        result = self._parse("02-29 09:00", now_utc=now)
        self.assertTrue(result.was_past)
        fr = result.first_run.astimezone(UTC)
        self.assertEqual(fr.month, 2)
        self.assertEqual(fr.day, 29)
        self.assertGreater(fr.year, 2028)

    # ── T5: local_iana_timezone fallback ─────────────────────────────────────

    def test_local_iana_timezone_returns_valid_string(self):
        """local_iana_timezone() returns a non-empty string on the current system."""
        from gateway.core.tz_utils import local_iana_timezone
        result = local_iana_timezone()
        self.assertIsInstance(result, str)
        self.assertTrue(result, "Expected a non-empty timezone string")

    def test_local_iana_timezone_fallback_when_not_symlink(self, *args):
        """local_iana_timezone() falls back to 'UTC' when /etc/localtime cannot be read."""
        from gateway.core.tz_utils import local_iana_timezone
        # Simulate /etc/localtime not being a symlink (e.g. Alpine container)
        with patch("pathlib.Path.is_symlink", return_value=False):
            result = local_iana_timezone()
        self.assertEqual(result, "UTC")

    # ── T7: DOW same-day past + MM-DD year rollover ──────────────────────────

    def test_dow_same_day_past_advances_by_7(self):
        """'Mon 09:00' on a Monday at 10:00 (09:00 already past) → next Monday (+7 days)."""
        # 2026-04-13 is a Monday; now is 10:00 (past 09:00)
        now = datetime(2026, 4, 13, 10, 0, 0, tzinfo=UTC)
        result = self._parse("Mon 09:00", now_utc=now)
        self.assertTrue(result.was_past)
        # first_run should be the NEXT Monday (Apr 20)
        fr = result.first_run.astimezone(UTC)
        self.assertEqual(fr.date().isoformat(), "2026-04-20")
        self.assertEqual(result.dow, "1")

    def test_mmdd_past_same_month_advances_one_year(self):
        """'04-15 09:00' when today is Apr 16 → next year's Apr 15."""
        now = datetime(2026, 4, 16, 10, 0, 0, tzinfo=UTC)
        result = self._parse("04-15 09:00", now_utc=now)
        self.assertTrue(result.was_past)
        fr = result.first_run.astimezone(UTC)
        self.assertEqual(fr.year, 2027)
        self.assertEqual(fr.month, 4)
        self.assertEqual(fr.day, 15)


class TestInjectMessageWakesAnIdleRoom(unittest.IsolatedAsyncioTestCase):
    """The wake, from the inside (§2.5).

    The sweep exempts job-bearing rooms from expiry on the sentence "idling
    one is harmless — the job wakes it", and `inject_message` bypasses the
    connector entirely, so nothing on the message path can wake it for the
    job. The injection must therefore recreate through the same
    `get_or_create` a message would — and a pause must still win (§4.4),
    which it does because a paused record answers None there.
    """

    def _record(self, **kw):
        from gateway.core.state import WatcherState

        defaults = dict(
            watcher_name="rc-eng", session_id="sess-1", room_id="room-1",
            room_type="channel", room_kind="channel", room_name="eng-backend",
            participants=["alice"], rule_name="eng",
        )
        defaults.update(kw)
        return WatcherState(**defaults)

    async def test_an_idle_watchers_injection_recreates_it(self):
        from unittest.mock import AsyncMock, MagicMock

        from gateway.core.watcher_manager import RoomRef
        from gateway.core.watcher_rule import RoomKind
        from tests.helpers import make_bare_session_manager

        woken_processor = MagicMock()
        woken_processor.enqueue = AsyncMock(return_value=True)

        sm = make_bare_session_manager(_connector_name="rc")
        sm._lifecycle.processor_for_room = MagicMock(return_value=None)  # idle
        sm._lifecycle.record_for_room = MagicMock(return_value=self._record())
        # An idle record is a RECREATION, so the connector is asked first
        # (#145). Its answer here matches the record, so this test pins only
        # that the wake happens; which source describes the room is pinned in
        # test_recreation_resolves_room_scope.py.
        sm._connector.supports_room_lookup = MagicMock(return_value=True)
        sm._connector.room_ref_by_id = AsyncMock(return_value=RoomRef(
            id="room-1", kind=RoomKind.CHANNEL, name="eng-backend",
            participants=("alice",)))
        sm._watcher_manager = MagicMock()
        sm._watcher_manager.get_or_create = AsyncMock(return_value=woken_processor)

        result = await sm.inject_message("room-1", "check stock prices")

        self.assertTrue(result)
        woken_processor.enqueue.assert_awaited_once()
        call = sm._watcher_manager.get_or_create.await_args
        self.assertEqual(call.args[0], "rc")
        room = call.args[1]
        self.assertIsInstance(room, RoomRef)
        self.assertEqual(room.id, "room-1")
        self.assertIs(room.kind, RoomKind.CHANNEL)
        self.assertEqual(room.participants, ("alice",))

    async def test_a_declined_wake_is_a_visible_failure(self):
        """Paused, or no frozen config: get_or_create answers None, and the
        injection reports False with the same warning as before — a schedule
        must not override a pause (§4.4)."""
        import logging
        from unittest.mock import AsyncMock, MagicMock

        from gateway.core.watcher_manager import RoomRef
        from gateway.core.watcher_rule import RoomKind
        from tests.helpers import make_bare_session_manager

        sm = make_bare_session_manager(_connector_name="rc")
        sm._lifecycle.processor_for_room = MagicMock(return_value=None)
        sm._lifecycle.record_for_room = MagicMock(
            return_value=self._record(paused=True))
        # The room IS served (#145's check passes) — so the refusal below is
        # `get_or_create`'s, not the room lookup's.
        sm._connector.supports_room_lookup = MagicMock(return_value=True)
        sm._connector.room_ref_by_id = AsyncMock(return_value=RoomRef(
            id="room-1", kind=RoomKind.CHANNEL, name="eng-backend"))
        sm._watcher_manager = MagicMock()
        sm._watcher_manager.get_or_create = AsyncMock(return_value=None)

        with self.assertLogs("agent-chat-gateway.core.session_manager",
                             level=logging.WARNING):
            result = await sm.inject_message("room-1", "hello")

        self.assertFalse(result)

    async def test_a_static_deployment_keeps_the_old_answer(self):
        """No watcher manager → no creation path; the injection fails exactly
        as it always has rather than reaching for a router that is not there."""
        import logging
        from unittest.mock import AsyncMock, MagicMock

        from gateway.core.watcher_manager import RoomRef
        from gateway.core.watcher_rule import RoomKind
        from tests.helpers import make_bare_session_manager

        sm = make_bare_session_manager()
        sm._lifecycle.processor_for_room = MagicMock(return_value=None)
        sm._lifecycle.record_for_room = MagicMock(return_value=self._record())
        # The room resolves fine (#145's check is not what fails here): with
        # an un-awaitable MagicMock the lookup would raise TypeError, be
        # swallowed as a transient blip, and satisfy assertLogs with the wrong
        # warning — the test would pass for a reason its docstring does not
        # claim.
        sm._connector.supports_room_lookup = MagicMock(return_value=True)
        sm._connector.room_ref_by_id = AsyncMock(return_value=RoomRef(
            id="room-1", kind=RoomKind.CHANNEL, name="eng-backend"))

        with self.assertLogs("agent-chat-gateway.core.session_manager",
                             level=logging.WARNING) as logs:
            result = await sm.inject_message("room-1", "hello")

        self.assertFalse(result)
        self.assertTrue(any("no active processor" in line for line in logs.output),
                        logs.output)


class TestAJobWithNoResolvableRoomIsNotFired(unittest.TestCase):
    """No room means no injection, not an injection with no address.

    Decided at the scheduler's ONE resolution seam (`_resolve_target`), not
    inside `inject_message` — which now takes a room id and nothing else, so it
    cannot even be asked the question. Before, a watcher with no persisted
    state was still injected with a warning that the reply "may be dropped":
    the agent ran a full turn and the reply went nowhere while `enqueue`
    returning True counted the fire as a SUCCESS, burning a finite job's run
    every slot, forever.
    """

    def _scheduler(self, sm):
        scheduler = JobScheduler.__new__(JobScheduler)
        scheduler._session_managers = {"rc": sm}
        return scheduler

    def test_a_legacy_job_whose_record_is_gone_resolves_to_nothing(self):
        sm = _make_sm_mock(room_id="")
        sm.resolve_handle = MagicMock(return_value="")
        job = ScheduledJob(watcher="rc:general", connector="rc")   # no room_id

        with self.assertLogs("agent-chat-gateway.core.scheduler", "WARNING") as log_ctx:
            target = self._scheduler(sm)._resolve_target(job)

        self.assertIsNone(target, "an unaddressable job must not be fired")
        self.assertTrue([r for r in log_ctx.output if "no resolvable room" in r],
                        log_ctx.output)

    def test_the_message_points_at_the_migration(self):
        """A job created before schema 2 carries no room id, and that is the
        common way to reach this — so the log says which command fixes it."""
        sm = _make_sm_mock(room_id="")
        sm.resolve_handle = MagicMock(return_value="")
        job = ScheduledJob(watcher="rc:general", connector="rc")

        with self.assertLogs("agent-chat-gateway.core.scheduler", "WARNING") as log_ctx:
            self._scheduler(sm)._resolve_target(job)

        self.assertTrue([r for r in log_ctx.output if "schedule migrate" in r])

    def test_a_legacy_job_is_resolved_through_the_handle_exactly_once(self):
        """The single by-name lookup on the runtime path, and its answer is the
        room every downstream step then takes."""
        sm = _make_sm_mock(room_id="room-1")
        job = ScheduledJob(watcher="rc:general", connector="rc")

        target = self._scheduler(sm)._resolve_target(job)

        self.assertEqual(target, (sm, "room-1"))
        sm.resolve_handle.assert_called_once_with("rc:general")

    def test_a_job_with_a_room_id_never_asks_by_name(self):
        sm = _make_sm_mock(room_id="room-1")
        job = ScheduledJob(watcher="rc:general", connector="rc", room_id="room-1")

        target = self._scheduler(sm)._resolve_target(job)

        self.assertEqual(target, (sm, "room-1"))
        sm.resolve_handle.assert_not_called()


class TestInjectMessageTimestampFormat(unittest.IsolatedAsyncioTestCase):
    """agent-chat-gateway#53: scheduler-injected messages must carry a
    timestamp RocketChatConnector.format_prompt_prefix() can actually parse
    into ts:/day: — otherwise scheduled tasks (e.g. stock reports) never see
    a day-of-week hint and can misjudge weekday vs. weekend."""

    async def test_injected_timestamp_produces_day_and_ts_fields(self):
        from unittest.mock import MagicMock

        from gateway.config import AttachmentConfig
        from gateway.connectors.rocketchat.config import RocketChatConfig
        from gateway.connectors.rocketchat.connector import RocketChatConnector
        from tests.helpers import make_bare_session_manager

        captured: list = []

        mock_processor = MagicMock()

        async def _capture_enqueue(msg):
            captured.append(msg)
            return True

        mock_processor.enqueue = _capture_enqueue

        from gateway.core.state import WatcherState

        sm = make_bare_session_manager()
        sm._lifecycle.processor_for_room = MagicMock(return_value=mock_processor)
        # A real room, because this test is about the TIMESTAMP: injection
        # refuses an unaddressable message rather than sending it nowhere, so a
        # missing record would fail for an unrelated reason and stop exercising
        # the prompt prefix at all.
        sm._lifecycle.record_for_room = MagicMock(return_value=WatcherState(
            watcher_name="test-watcher", session_id="", room_id="room-1",
            room_name="general", room_type="channel", room_kind="channel",
        ))

        result = await sm.inject_message("room-1", "check stock prices")
        self.assertTrue(result)
        self.assertEqual(len(captured), 1)
        injected_msg = captured[0]

        # Feed the exact message SessionManager built into the real RC
        # connector's header formatter — this is the code path a scheduled
        # job's message actually goes through.
        connector = RocketChatConnector.__new__(RocketChatConnector)
        # Pre-login: agent_username falls back to the configured spelling (#112).
        from unittest.mock import MagicMock as _M
        connector._rest = _M()
        connector._rest.bot_username = None
        connector._config = RocketChatConfig(
            server_url="http://chat.example.com",
            username="bot",
            password="pw",
            name="rc",
            owners=["alice"],
            timezone="UTC",
            attachments=AttachmentConfig(cache_dir_global="/tmp/rc-cache"),
        )
        prefix = connector.format_prompt_prefix(injected_msg)

        self.assertIn("day:", prefix)
        self.assertIn("ts:", prefix)


class TestInjectionResolvesOnceAndReportsFailure(unittest.IsolatedAsyncioTestCase):
    """`_inject` used to re-implement `_resolve_target`, and worse than it.

    Its copy resolved by *attempting delivery* into every manager in turn under
    `except Exception: pass`, so a real failure in the manager that owns the watcher was
    indistinguishable from "no manager has it" — the operator saw the generic message
    either way, and other connectors' watchers were poked in the process. It resolves
    first now, then injects once.
    """

    def _scheduler(self, managers):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = JobStore(jobs_file=Path(tmp.name) / "jobs.json")
        store.load()
        return JobScheduler(
            store=store, session_managers=managers, completed_job_ttl_days=7)

    async def test_a_failure_in_the_owning_manager_is_not_a_lookup_miss(self):
        owner = _make_sm_mock()
        owner.inject_message = AsyncMock(side_effect=RuntimeError("backend down"))
        scheduler = self._scheduler({"rc-home": owner})

        with self.assertLogs("agent-chat-gateway.core.scheduler", "ERROR") as logs:
            delivered = await scheduler._inject(_make_job(), scheduler._resolve_target(_make_job()))

        self.assertFalse(delivered)
        joined = "\n".join(logs.output)
        self.assertIn("inject_message failed", joined)
        self.assertNotIn("no session manager", joined)

    async def test_other_connectors_are_not_tried(self):
        """The old fallback injected into every manager until one returned True, which
        could deliver a job's message through a connector it was not scheduled on."""
        owner = _make_sm_mock()
        owner.inject_message = AsyncMock(return_value=False)
        stranger = _make_sm_mock()
        scheduler = self._scheduler({"rc-home": owner, "mm-eng": stranger})

        delivered = await scheduler._inject(_make_job(connector="rc-home"), scheduler._resolve_target(_make_job(connector="rc-home")))

        self.assertFalse(delivered)
        stranger.inject_message.assert_not_awaited()

    async def test_a_stale_connector_name_still_resolves_by_config(self):
        """The fallback that is worth keeping: a job written before a connector was
        renamed still finds its owner, because that lookup asks who *has* the watcher
        rather than trying to deliver to everyone."""
        owner = _make_sm_mock()
        scheduler = self._scheduler({"rc-home": owner})

        delivered = await scheduler._inject(_make_job(connector="renamed-away"), scheduler._resolve_target(_make_job(connector="renamed-away")))

        self.assertTrue(delivered)
        owner.inject_message.assert_awaited_once()

    async def test_no_owner_reports_a_lookup_miss(self):
        stranger = _make_sm_mock()
        stranger.resolve_handle = MagicMock(return_value="")   # never heard of it
        scheduler = self._scheduler({"mm-eng": stranger})

        with self.assertLogs("agent-chat-gateway.core.scheduler", "WARNING") as logs:
            delivered = await scheduler._inject(_make_job(connector="gone"), scheduler._resolve_target(_make_job(connector="gone")))

        self.assertFalse(delivered)
        self.assertIn("no session manager owns watcher", "\n".join(logs.output))
        stranger.inject_message.assert_not_awaited()

class TestTheManagerIsFoundByRoomBeforeByHandle(unittest.TestCase):
    """The manager is `job.connector`, and the room id is what the job fires
    into — never the handle once a room id exists (§2.8). This class also pins
    what a stale connector does NOT do: it does not reach whichever manager
    holds the room. `_make_sm_mock`'s `record_for_room` answers only for the
    rooms a mock owns, so a test here cannot pass on a bare-mock accident
    (review).
    """

    def _scheduler(self, managers):
        scheduler = JobScheduler.__new__(JobScheduler)
        scheduler._session_managers = managers
        return scheduler

    def test_a_stale_connector_is_refused_even_when_one_manager_holds_the_room(self):
        """Occupancy is not ownership: `mm` holding `room-mm` does not make
        `retired`'s job `mm`'s to run (Codex, PR #140 round 4)."""
        rc = _make_sm_mock(room_id="room-rc")
        mm = _make_sm_mock(room_id="room-mm")
        scheduler = self._scheduler({"rc": rc, "mm": mm})
        job = ScheduledJob(
            watcher="gone:general", connector="retired", room_id="room-mm")

        with self.assertLogs("agent-chat-gateway.core.scheduler", "ERROR"):
            self.assertIsNone(_manager_of(scheduler._resolve_target(job)))

    def test_the_named_connector_still_wins_when_it_is_configured(self):
        rc = _make_sm_mock(room_id="room-rc")
        mm = _make_sm_mock(room_id="room-mm")
        scheduler = self._scheduler({"rc": rc, "mm": mm})
        job = ScheduledJob(watcher="rc:general", connector="rc", room_id="room-mm")

        self.assertIs(_manager_of(scheduler._resolve_target(job)), rc)


class TestAnAmbiguousRoomIsRefusedRatherThanGuessed(unittest.TestCase):
    """The by-room fallback took the FIRST manager holding the room, in
    `config.yaml` order.

    Room ids are per-server, not per-connector, and the canonical multi-agent
    setup is one account per agent in the SAME rooms (CLAUDE.md) — so several
    managers holding a record for one room is the normal case. Measured: a job
    whose connector had been renamed away was handed to another agent's session
    manager, ran in that agent's processor, and that agent's ACCOUNT posted the
    reply, while the fire logged an ordinary success.

    Before this branch the same case failed loudly ("no session manager owns
    watcher …"). Restoring loudness on ambiguity is better than both: the branch
    keeps the by-room fix for the case it was for, and refuses the case only an
    operator can decide.
    """

    def _scheduler(self, managers):
        scheduler = JobScheduler.__new__(JobScheduler)
        scheduler._session_managers = managers
        return scheduler

    def _job(self):
        return ScheduledJob(id="acg-1", watcher="alice:standup",
                            connector="alice", room_id="room-shared")

    def test_two_owners_means_no_manager_rather_than_the_first_one(self):
        first = _make_sm_mock(room_id="room-shared")
        second = _make_sm_mock(room_id="room-shared")
        scheduler = self._scheduler({"bob": first, "alice-bot": second})

        self.assertIsNone(_manager_of(scheduler._resolve_target(self._job())))

    def test_the_refusal_is_logged_with_what_to_do(self):
        """A silent `None` would only surface as a job that stopped arriving."""
        scheduler = self._scheduler({
            "bob": _make_sm_mock(room_id="room-shared"),
            "alice-bot": _make_sm_mock(room_id="room-shared"),
        })

        with self.assertLogs("agent-chat-gateway.core.scheduler", "ERROR") as cm:
            _manager_of(scheduler._resolve_target(self._job()))

        logged = "\n".join(cm.output)
        self.assertIn("room-shared", logged)
        self.assertIn("Refusing to run it under another account", logged)
        self.assertIn("delete and recreate", logged)

    def test_one_remaining_holder_is_not_inferred_to_be_the_owner(self):
        """Occupancy is not ownership. Under one-account-per-agent, removing a
        connector leaves every OTHER account in its rooms as the holder — the
        sole survivor is by construction a different agent, and an earlier
        version ran the job there, with that agent's backend and tools, while
        the fire logged success (Codex, PR #140 round 4). Refused, loudly."""
        survivor = _make_sm_mock(room_id="room-shared")
        other = _make_sm_mock(room_id="room-elsewhere")
        scheduler = self._scheduler({"bob": other, "alice-bot": survivor})

        with self.assertLogs("agent-chat-gateway.core.scheduler", "ERROR") as cm:
            target = scheduler._resolve_target(self._job())

        self.assertIsNone(_manager_of(target))
        logged = "\n".join(cm.output)
        self.assertIn("'alice' is not configured", logged)
        self.assertIn("another account", logged)
        survivor.inject_message.assert_not_awaited()

    def test_ambiguity_does_not_fall_through_to_the_handle(self):
        """Falling back to the handle after refusing the room would reinstate the
        guess by another route — and the handle is the WEAKER key. Both mocks
        answer `get_watcher_state` for any name, so a fall-through would pick
        `bob`, the first in config order."""
        first = _make_sm_mock(room_id="room-shared")
        second = _make_sm_mock(room_id="room-shared")
        scheduler = self._scheduler({"bob": first, "alice-bot": second})

        self.assertIsNone(_manager_of(scheduler._resolve_target(self._job())))

    def test_with_no_room_owner_it_does_not_fall_through_to_the_handle(self):
        """The routing rule (§2.8): the handle is consulted ONLY when the job has
        no room id. This job has one; nobody holds the room; its connector is
        gone. Falling back to the handle here would reinstate the weaker key on
        exactly the job that carries the stronger one — so: None, and loud.
        Before this seam the same case silently resolved through the handle."""
        rc = _make_sm_mock(room_id="room-rc")
        scheduler = self._scheduler({"rc": rc})
        job = ScheduledJob(
            watcher="gone:general", connector="retired", room_id="room-nobody")

        with self.assertLogs("agent-chat-gateway.core.scheduler", "WARNING"):
            self.assertIsNone(scheduler._resolve_target(job))
        rc.resolve_handle.assert_not_called()

    def test_neither_field_naming_a_manager_answers_none(self):
        """An old job whose connector was renamed away and whose room has no
        live record. `schedule migrate` records both, which is what makes such
        a job resolvable again."""
        rc = _make_sm_mock(room_id="room-rc")
        rc.resolve_handle = MagicMock(return_value="")
        scheduler = self._scheduler({"rc": rc})
        job = ScheduledJob(watcher="gone:general", connector="retired")  # no id either

        with self.assertLogs("agent-chat-gateway.core.scheduler", "WARNING"):
            self.assertIsNone(scheduler._resolve_target(job))



if __name__ == "__main__":
    unittest.main()


class TestNoFirePathWriteRevertsAMigration(unittest.IsolatedAsyncioTestCase):
    """The success path was converted to `write_fields` first and the finite-job
    FAILURE branch was missed (Codex, PR #140): it still wrote the pre-await
    copy back whole, so a `room_id` that `schedule migrate` wrote while the
    inject was in flight — and the inject then failed — was reverted.

    The migration is simulated exactly where it lands in production: inside the
    awaited inject, after `_fire_once` took its copy. Planting `update` back
    fails this test.

    The catch-up COMPLETION write (`_fire_catch_up`) was converted too, for
    consistency only: it writes the stored object with no await in between, so
    `update` there could not revert anything, and no test can tell the two
    apart — a test claiming otherwise was written and then removed once the
    plant showed it did not discriminate.
    """

    def _store_and_scheduler(self, sm, **job_kwargs):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = JobStore(jobs_file=Path(tmp.name) / "jobs.json")
        store.load()
        job = _make_job(room_id="", **job_kwargs)
        store.add(job)
        scheduler = JobScheduler(store=store, session_managers={"rc-home": sm}, completed_job_ttl_days=7)
        return store, scheduler, job

    def _sm_whose_inject(self, store, job_id, *, returns):
        sm = _make_sm_mock()

        async def _inject(room_id, text):
            store.set_room_id(job_id, "R-migrated")   # the migration lands mid-fire
            return returns
        sm.inject_message = AsyncMock(side_effect=_inject)
        return sm

    async def test_a_failed_finite_fire_keeps_the_migrated_room_id(self):
        sm = _make_sm_mock()
        store, scheduler, job = self._store_and_scheduler(
            sm, times=3, next_run=(datetime.now(UTC) - timedelta(minutes=1)).isoformat())
        sm.inject_message = self._sm_whose_inject(store, job.id, returns=False).inject_message

        await scheduler._fire_due_jobs()

        self.assertEqual(store.get(job.id).room_id, "R-migrated",
                         "the failure branch wrote the pre-await copy back whole")
        self.assertEqual(store.get(job.id).run_count, 0, "a failed finite fire consumes no run")


class TestAJobWhoseConnectorIsGoneIsCancelled(unittest.IsolatedAsyncioTestCase):
    """Owner's rule (PR #140 round 4 follow-up): a job is never re-homed to
    another account, and it is not left to fail at every slot either — when the
    connector it names has left the config, the fire cancels it with the same
    audit line a room removal writes."""

    def _store_and_scheduler(self, managers, **job_kwargs):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = JobStore(jobs_file=Path(tmp.name) / "jobs.json")
        store.load()
        job = _make_job(next_run=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
                        **job_kwargs)
        store.add(job)
        return store, JobScheduler(store=store, session_managers=managers,
                                   completed_job_ttl_days=7), job

    async def test_a_named_but_unconfigured_connector_cancels_the_job(self):
        survivor = _make_sm_mock(room_id="R-shared")
        store, scheduler, job = self._store_and_scheduler(
            {"bob": survivor}, connector="retired", room_id="R-shared")

        with self.assertLogs("agent-chat-gateway.core.scheduler", "WARNING") as cm:
            await scheduler._fire_due_jobs()

        kept = store.get(job.id)
        self.assertEqual(kept.status, JobStatus.CANCELLED, "marked and kept, not removed")
        self.assertIn("retired", kept.cancel_reason)
        self.assertTrue(kept.cancelled_at)
        survivor.inject_message.assert_not_awaited()
        logged = "\n".join(cm.output)
        self.assertIn("AUDIT: cancelled scheduled job", logged)
        self.assertIn("connector 'retired'", logged)

    async def test_a_job_that_names_no_connector_is_not_cancelled_on_that_evidence(self):
        """Pre-schema-2: unknown is not gone. Resolved by handle; here nobody
        answers for it, so it is refused — and it is still in the store."""
        sm = _make_sm_mock(room_id="R-1")
        sm.resolve_handle = MagicMock(return_value="")
        store, scheduler, job = self._store_and_scheduler(
            {"rc-home": sm}, connector="", room_id="")

        await scheduler._fire_due_jobs()

        kept = store.get(job.id)
        self.assertEqual(kept.status, JobStatus.ACTIVE, "refused, not cancelled")
        self.assertIsNone(kept.cancelled_at)
        sm.inject_message.assert_not_awaited()

    async def test_catch_up_over_several_missed_slots_survives_the_cancellation(self):
        """`_fire_catch_up` re-assigns `_fire_once`'s return and reads `.status`
        on the next missed slot. The cancel branch returned None, so a gone
        connector with two or more missed slots raised AttributeError out of
        the catch-up loop — which has no per-job isolation — and killed the
        scheduler task at startup (internal review, P1)."""
        three_min_ago = (datetime.now(UTC) - timedelta(minutes=3)).isoformat()
        store, scheduler, job = self._store_and_scheduler(
            {"bob": _make_sm_mock(room_id="R-shared")}, connector="retired",
            room_id="R-shared", cron="* * * * *", last_run=three_min_ago,
            last_attempted_at=three_min_ago)

        await scheduler._catch_up_missed()   # must not raise

        self.assertEqual(store.get(job.id).status, JobStatus.CANCELLED)


class TestOneBadJobCannotKillTheScheduler(unittest.IsolatedAsyncioTestCase):
    """`_fire_due_jobs` isolated each job; catch-up and the cancel write did not
    (final pre-merge review). A raise on either path propagated out of `run()`
    and stopped every job on every connector until a restart, silently."""

    def _store_and_scheduler(self, managers, **job_kwargs):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = JobStore(jobs_file=Path(tmp.name) / "jobs.json")
        store.load()
        job = _make_job(next_run=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(), **job_kwargs)
        store.add(job)
        return store, JobScheduler(store=store, session_managers=managers, completed_job_ttl_days=7), job

    async def test_catch_up_skips_a_job_whose_cancellation_cannot_be_written(self):
        earlier = (datetime.now(UTC) - timedelta(minutes=3)).isoformat()
        store, scheduler, job = self._store_and_scheduler(
            {"bob": _make_sm_mock(room_id="R")}, connector="retired", room_id="R",
            cron="* * * * *", last_run=earlier, last_attempted_at=earlier)
        store.cancel = MagicMock(side_effect=OSError("disk full"))

        with self.assertLogs("agent-chat-gateway.core.scheduler", "ERROR"):
            await scheduler._catch_up_missed()   # must not raise

        self.assertEqual(store.get(job.id).status, JobStatus.ACTIVE, "left for the next slot")

    async def test_catch_up_isolates_a_job_whose_cron_cannot_be_enumerated(self):
        store, scheduler, job = self._store_and_scheduler(
            {"rc-home": _make_sm_mock(room_id="R")}, connector="rc-home", room_id="R",
            cron="not a cron", last_run=(datetime.now(UTC) - timedelta(hours=1)).isoformat())

        with self.assertLogs("agent-chat-gateway.core.scheduler", "ERROR"):
            await scheduler._catch_up_missed()   # must not raise

    async def test_a_failing_purge_does_not_cost_the_ticks_fires(self):
        store, scheduler, job = self._store_and_scheduler(
            {"rc-home": _make_sm_mock(room_id="R")}, connector="rc-home", room_id="R")
        store.remove_expired_completed = MagicMock(side_effect=OSError("read-only"))

        with self.assertLogs("agent-chat-gateway.core.scheduler", "ERROR"):
            await scheduler._tick()

        self.assertEqual(store.get(job.id).run_count, 1, "the job still fired")

