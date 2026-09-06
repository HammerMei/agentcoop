"""JobScheduler: asyncio background task that fires scheduled jobs.

Architecture
-----------
The scheduler runs as a single asyncio task created in GatewayService.run().
It owns two responsibilities per tick (every 60 s):

  1. _purge_expired_completed_jobs() — remove COMPLETED jobs older than TTL.
  2. _fire_due_jobs()               — inject messages for ACTIVE jobs that are due.

On startup, _catch_up_missed() fires all jobs whose next_run has already passed
(user preference: always fire all missed jobs).

Message delivery uses direct injection (JobScheduler → SessionManager.inject_message())
rather than posting to the chat platform.  This bypasses the connector's self-message
filter, which would silently drop any message sent by the bot's own username.

Dependencies
-----------
  croniter>=2.0.0  — cron expression parsing and next_run calculation.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Callable

try:
    from croniter import croniter  # type: ignore[import-untyped]
except ImportError as _e:
    raise ImportError(
        "croniter is required for scheduling support. "
        "Install it with: pip install 'croniter>=2.0.0'"
    ) from _e

try:
    import zoneinfo
except ImportError:
    from backports import zoneinfo  # type: ignore[no-redef]

from ..schedule_types import FIRE_OWNED_FIELDS, JobStatus, ScheduledJob
from .job_store import JobStore

if TYPE_CHECKING:
    from .session_manager import SessionManager

logger = logging.getLogger("agent-chat-gateway.core.scheduler")

_TICK_INTERVAL = 60  # seconds between scheduler polls


def compute_next_run(cron: str, timezone: str, after: datetime | None = None) -> str:
    """Return the next fire time (ISO 8601 UTC string) for a cron expression.

    Args:
        cron:     5-field POSIX cron expression, e.g. ``"0 9 * * 1-5"``.
        timezone: IANA timezone name, e.g. ``"Asia/Taipei"``.
        after:    Compute next run strictly after this UTC datetime.
                  Defaults to ``datetime.now(UTC)``.

    Returns:
        ISO 8601 UTC string, e.g. ``"2026-04-09T01:00:00+00:00"``.
    """
    try:
        tz = zoneinfo.ZoneInfo(timezone)
    except (zoneinfo.ZoneInfoNotFoundError, KeyError):
        logger.warning("Unknown timezone %r, falling back to UTC", timezone)
        tz = zoneinfo.ZoneInfo("UTC")

    base = after if after is not None else datetime.now(UTC)
    # croniter expects a naive or tz-aware datetime as start; convert to the job's timezone
    base_local = base.astimezone(tz)

    it = croniter(cron, base_local)
    next_local: datetime = it.get_next(datetime)
    # Defensive: croniter may return a naive datetime in some versions.
    # astimezone(UTC) raises ValueError on naive datetimes, so attach the
    # job's timezone explicitly before converting.
    if next_local.tzinfo is None:
        next_local = next_local.replace(tzinfo=tz)
    next_utc = next_local.astimezone(UTC)
    return next_utc.isoformat()


_MAX_MISSED_CATCHUP = 500  # cap to prevent OOM on very long downtimes with frequent crons


def compute_all_missed(
    cron: str,
    timezone: str,
    after_utc: datetime,
    before_utc: datetime,
) -> list[datetime]:
    """Return all cron fire times in the half-open interval (after_utc, before_utc].

    Used for catch-up: determines how many times a job should have fired while
    the daemon was down.

    Capped at ``_MAX_MISSED_CATCHUP`` entries to prevent unbounded memory use
    when a frequent cron (e.g. ``* * * * *``) is combined with a long downtime.
    A warning is logged when the cap is hit.
    """
    try:
        tz = zoneinfo.ZoneInfo(timezone)
    except (zoneinfo.ZoneInfoNotFoundError, KeyError):
        tz = zoneinfo.ZoneInfo("UTC")

    base_local = after_utc.astimezone(tz)
    it = croniter(cron, base_local)
    times = []
    while True:
        t: datetime = it.get_next(datetime)
        # Defensive: croniter may return naive datetimes on some versions.
        if t.tzinfo is None:
            t = t.replace(tzinfo=tz)
        t_utc = t.astimezone(UTC)
        if t_utc > before_utc:
            break
        times.append(t_utc)
        if len(times) >= _MAX_MISSED_CATCHUP:
            logger.warning(
                "compute_all_missed: capped at %d entries for cron %r "
                "(downtime window %s → %s). Remaining missed fires will be skipped.",
                _MAX_MISSED_CATCHUP,
                cron,
                after_utc.isoformat(),
                before_utc.isoformat(),
            )
            break
    return times


class JobScheduler:
    """Asyncio background task that manages job firing and cleanup.

    Usage::

        scheduler = JobScheduler(store, session_managers, completed_job_ttl_days=7)
        task = asyncio.create_task(scheduler.run(), name="job-scheduler")
        # ... on shutdown:
        task.cancel()
    """

    def __init__(
        self,
        store: JobStore,
        session_managers: "dict[str, SessionManager]",  # connector_name → SessionManager
        completed_job_ttl_days: int = 7,
        degraded: "Callable[[str], bool] | None" = None,
    ) -> None:
        self._store = store
        self._session_managers = session_managers
        self._ttl_days = completed_job_ttl_days
        # Whether a connector is in the mapping but DEGRADED (#144): a reload
        # could not bring it back. Its jobs are neither cancelled (it is still
        # configured) nor fired (its manager is not serving); they wait.
        self._degraded = degraded or (lambda name: False)
        # Held around every fire pass (#144). A reload pauses the scheduler by
        # cancelling its task; taking this lock first means the cancel lands in
        # the sleep between passes, never between an injection and the write
        # that records it — which would fire the same slot again on restart.
        self._fire_lock = asyncio.Lock()

    @property
    def fire_lock(self) -> asyncio.Lock:
        """Hold it while cancelling `run()` so no fire is cut in half."""
        return self._fire_lock

    @property
    def completed_job_ttl_days(self) -> int:
        return self._ttl_days

    @completed_job_ttl_days.setter
    def completed_job_ttl_days(self, days: int) -> None:
        """Swapped in place by `config reload` (#144) — read at the next purge."""
        self._ttl_days = days

    async def run(self) -> None:
        """Main scheduler loop.  Runs until cancelled."""
        logger.info("JobScheduler started (tick_interval=%ds, ttl_days=%d)", _TICK_INTERVAL, self._ttl_days)
        try:
            async with self._fire_lock:
                await self._catch_up_missed()
            while True:
                await asyncio.sleep(_TICK_INTERVAL)
                async with self._fire_lock:
                    await self._tick()
        except asyncio.CancelledError:
            logger.info("JobScheduler cancelled")
            raise
        except Exception as e:
            # Log immediately so the scheduler death is visible in runtime logs
            # rather than only surfacing when the task future is awaited at shutdown.
            logger.error("JobScheduler terminated unexpectedly: %s", e, exc_info=True)
            raise

    # ── Startup catch-up ─────────────────────────────────────────────────────

    async def _catch_up_missed(self) -> None:
        """Fire all jobs that were due while the daemon was down."""
        now = datetime.now(UTC)
        jobs = self._store.list_due()
        if not jobs:
            return
        logger.info("Catching up %d missed job(s) on startup", len(jobs))
        for job in jobs:
            try:
                await self._fire_catch_up(job, now)
            except Exception:
                # Per-job isolation, as `_fire_due_jobs` has: a hand-edited
                # cron, a write that fails on a full disk — one job's failure
                # here used to propagate out of `run()` and stop every job on
                # every connector until the next restart, silently (final
                # pre-merge review). Logged with the job, then the next one.
                logger.exception("Catch-up for job %s failed — skipping it this start", job.id)

    async def _fire_catch_up(self, job: ScheduledJob, now: datetime) -> None:
        """Fire a job that was missed during downtime.

        For recurring jobs, counts all missed fire times and fires once per
        missed occurrence (respecting ``times`` limit).  For one-shot jobs,
        fires once and completes.
        """
        def _parse_utc(ts: str) -> datetime | None:
            """Parse an ISO 8601 timestamp and ensure it is UTC-aware."""
            try:
                dt = datetime.fromisoformat(ts)
                if dt.tzinfo is None:
                    # Hand-edited or legacy value without offset — assume UTC.
                    dt = dt.replace(tzinfo=UTC)
                return dt
            except ValueError:
                return None

        # Determine how many times this job should have fired since the last attempt.
        # Use last_attempted_at as the primary anchor: it is set on every _fire_once
        # call (success or failure) so it reflects "what time we last tried", not
        # just "what time we last succeeded".  This prevents replaying fire slots
        # where injection already failed — e.g. if the watcher was down during a
        # catch-up, last_attempted_at already marks that slot as "tried".
        # Fall back to last_run (last successful fire) for jobs upgraded from
        # older gateway versions that predate last_attempted_at.
        last_run_dt: datetime | None = None
        anchor_ts = job.last_attempted_at or job.last_run
        if anchor_ts:
            last_run_dt = _parse_utc(anchor_ts)

        if last_run_dt is None:
            # Never ran or attempted before — try creation time as the start anchor
            if job.created_at:
                last_run_dt = _parse_utc(job.created_at)
            # If created_at is also missing/malformed, fall back to next_run itself
            # (which is already in the past — see list_due() precondition).
            # Using `now` would produce an empty missed-fires list and silently
            # skip the catch-up, so next_run is a better anchor.
            if last_run_dt is None and job.next_run:
                # Anchor one minute before next_run so the job fires exactly once
                nr = _parse_utc(job.next_run)
                if nr is not None:
                    last_run_dt = nr - timedelta(minutes=1)
            if last_run_dt is None:
                # Last resort: fire once unconditionally
                logger.warning(
                    "Job %s has neither last_run nor created_at — firing once unconditionally",
                    job.id,
                )
                await self._fire_once(job, now)
                return

        # Guard: job is exhausted (run_count >= times) but status is still ACTIVE.
        # This can happen if jobs.json was hand-edited or if a persistence race left
        # the status un-updated.  Do NOT fire — mark as COMPLETED and bail out.
        remaining = job.times - job.run_count if job.times > 0 else None
        if remaining is not None and remaining <= 0:
            logger.warning(
                "Job %s has run_count=%d >= times=%d but status=ACTIVE — "
                "marking COMPLETED without firing",
                job.id, job.run_count, job.times,
            )
            job.status = JobStatus.COMPLETED
            job.next_run = None
            # Always reset completed_at to now — a hand-edited future timestamp
            # would otherwise make the job immune to TTL purge.
            job.completed_at = datetime.now(UTC).isoformat()
            # Field-scoped for consistency with every other fire-path write, not
            # because a copy is held: `job` here is the stored object from
            # `list_due()` and nothing awaits between the read and this write,
            # so `update(job)` could not have reverted anything. Verified by
            # planting `update` back — no test can tell the two apart here.
            await asyncio.to_thread(self._store.write_fields, job, FIRE_OWNED_FIELDS)
            return

        # For jobs with exactly one remaining run, fire once regardless of how long
        # they were missed — calling compute_all_missed on a large downtime window
        # could return many entries, but only one more fire is allowed.
        # Use the job's canonical scheduled time (next_run) as the fire timestamp
        # so that last_run reflects the intended schedule, not the catch-up wall clock.
        if remaining == 1:
            fire_time = now
            if job.next_run:
                try:
                    parsed = datetime.fromisoformat(job.next_run)
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=UTC)
                    fire_time = parsed
                except ValueError:
                    pass
            await self._fire_once(job, fire_time)
            return

        # For recurring jobs, enumerate all missed fire times
        missed = compute_all_missed(job.cron, job.timezone, last_run_dt, now)
        if not missed:
            return

        for fire_time in missed:
            if job.status != JobStatus.ACTIVE:
                break  # completed during catch-up
            # _fire_once returns the updated copy; re-assign so the next
            # iteration sees the incremented run_count / updated status.
            job = await self._fire_once(job, fire_time)

    # ── Per-tick ──────────────────────────────────────────────────────────────

    async def _tick(self) -> None:
        """One scheduler tick: purge expired + fire due jobs."""
        # Offload the purge file-write to a thread so the event loop is not
        # blocked while jobs.json is being written.
        try:
            await asyncio.to_thread(self._store.remove_expired_completed, self._ttl_days)
        except Exception:
            # A purge that cannot write must not cost this tick's fires, nor
            # the scheduler task (final pre-merge review).
            logger.exception("Purging expired jobs failed — firing anyway")
        await self._fire_due_jobs()

    async def _fire_due_jobs(self) -> None:
        """Fire all ACTIVE jobs whose next_run has arrived."""
        due = self._store.list_due()
        for job in due:
            # Use the job's nominal scheduled time (next_run) as the fire timestamp
            # so that next_run is computed from the canonical schedule, not from the
            # actual wall-clock time the scheduler polled (which can drift slightly).
            try:
                fire_time = datetime.fromisoformat(job.next_run) if job.next_run else datetime.now(UTC)
                # Guard: fromisoformat returns naive datetimes for legacy / hand-edited
                # values without a UTC offset.  astimezone() on a naive datetime uses
                # the system local timezone, silently producing a wrong next_run.
                if fire_time.tzinfo is None:
                    fire_time = fire_time.replace(tzinfo=UTC)
            except ValueError:
                fire_time = datetime.now(UTC)
            try:
                await self._fire_once(job, fire_time)
            except Exception as e:
                # Per-job isolation: one broken job must not kill the scheduler
                # or prevent other jobs from firing in the same tick.
                logger.error("Unexpected error firing job %s — skipping: %s", job.id, e)

    # ── Job execution ─────────────────────────────────────────────────────────

    async def _fire_once(self, job: ScheduledJob, fire_time: datetime) -> ScheduledJob:
        """Fire a single job: inject message, update state, persist.

        Returns the updated job object (a shallow copy).  Callers that fire the
        same job multiple times (e.g. the catch-up loop) must re-assign their
        local reference to the return value so subsequent iterations see the
        correct ``run_count`` and ``status``.

        ``run_count`` is incremented and ``next_run`` is advanced even when
        injection fails.  This is intentional: silently retrying a failed
        injection on every subsequent tick would flood the queue if the watcher
        stays down for an extended period.  Users can resume the watcher and
        the next scheduled fire will deliver the message normally.

        All field mutations are applied to a shallow copy of the job object so
        that concurrent ``list_jobs()`` / ``list_due()`` callers on the event-
        loop thread never observe a partially mutated state.  ``store.update``
        atomically replaces the reference in the in-memory dict under the lock,
        so readers either see the old state or the fully-updated state.
        """
        # Shallow copy is sufficient: all ScheduledJob fields are immutable
        # scalar types (str, int, enum) so there are no nested mutable objects.
        job = copy.copy(job)

        # Record the attempt time unconditionally — before any early return —
        # so that catch-up on restart uses this as the anchor and does not
        # replay fire slots where injection already failed.
        job.last_attempted_at = fire_time.isoformat()

        logger.info(
            "Firing scheduled job %s (watcher=%s, run=%d/%s)",
            job.id,
            job.watcher,
            job.run_count + 1,
            str(job.times) if job.times > 0 else "∞",
        )

        if job.connector and self._degraded(job.connector):
            logger.warning(
                "Job %s: connector '%s' is degraded (a config reload could not bring it "
                "back) — not fired this slot; it fires once the connector is back. "
                "'agent-chat-gateway status' says what is wrong.", job.id, job.connector,
            )
            return job
        if self._connector_is_gone(job):
            # Owner's rule (PR #140): a job whose connector has left the config
            # is not re-homed and not left to fail at every slot — it is
            # cancelled (marked, kept), with the same audit line a room removal writes. The
            # scheduler owns this because the fire is the moment the job is
            # known to be undeliverable; nothing else touches it until then.
            reason = (f"connector '{job.connector}' is no longer configured, so "
                      f"the job has no account to run under")
            try:
                await asyncio.to_thread(self._store.cancel, job.id, reason=reason)
            except Exception:
                # The store could not be written; the job stays ACTIVE on disk
                # and the next slot tries again. Like every other write on the
                # fire path, a failure is logged, not raised (final review).
                logger.exception("Job %s: could not persist the cancellation", job.id)
            else:
                logger.warning(
                    "AUDIT: cancelled scheduled job %s (watcher '%s', room %s) — %s. "
                    "The record is kept; 'agent-chat-gateway schedule resume %s' restores it.",
                    job.id, job.watcher, job.room_id, reason, job.id,
                )
            # The copy, marked, not None: `_fire_catch_up` re-assigns this
            # return and reads `.status` on its next missed slot (internal
            # review — a bare `return` here raised AttributeError out of the
            # catch-up loop and killed the scheduler task at startup).
            job.status = JobStatus.CANCELLED
            return job

        target = self._resolve_target(job)
        success = await self._inject(job, target)
        if not success:
            sm, room_id = target if target is not None else (None, "")
            watcher_state = sm.record_for_room(room_id) if sm is not None else None
            is_paused = watcher_state is not None and bool(watcher_state.paused)

            if is_paused:
                logger.info(
                    "Job %s: watcher %r is paused — skipping fire (expected). "
                    "Job will retry at next scheduled time.",
                    job.id,
                    job.watcher,
                )
            else:
                logger.warning(
                    "Job %s: injection failed (watcher=%s may not be active). "
                    "Advancing next_run to avoid repeated retry flood.",
                    job.id,
                    job.watcher,
                )
                await self._notify_injection_failure(job, sm, room_id)

            if job.times > 0:
                # Finite job: delivery failed — do NOT consume run_count so the
                # remaining budget is preserved and the job can retry next fire.
                try:
                    job.next_run = compute_next_run(job.cron, job.timezone, after=fire_time)
                except Exception as e:
                    logger.error(
                        "Job %s: failed to compute next_run (cron=%r): %s — pausing job",
                        job.id,
                        job.cron,
                        e,
                    )
                    job.status = JobStatus.PAUSED
                    job.next_run = None
                try:
                    # Field-scoped here too. The success path was converted first
                    # and this branch was missed (Codex, PR #140): a migration
                    # landing while the inject that then FAILED was in flight had
                    # its room id reverted by this whole-object write of the
                    # pre-await copy. Self-healing since `needs_migration` also
                    # looks at the jobs, but a fire must not undo a migration.
                    await asyncio.to_thread(
                        self._store.write_fields, job, FIRE_OWNED_FIELDS)
                except Exception as e:
                    logger.error("Failed to persist job %s after failed fire: %s", job.id, e)
                return job
            # Infinite job (times == 0): run_count is non-binding for completion
            # so we fall through to the normal accounting below.

        now_str = fire_time.isoformat()
        job.run_count += 1
        job.last_run = now_str

        # Check completion
        if job.times > 0 and job.run_count >= job.times:
            job.status = JobStatus.COMPLETED
            job.next_run = None
            job.completed_at = datetime.now(UTC).isoformat()
            logger.info("Job %s completed all %d run(s)", job.id, job.times)
        else:
            # Compute next fire time.  Guard against a corrupted/empty cron field
            # that slipped past validation (e.g. hand-edited jobs.json).  A bad
            # cron would otherwise propagate an exception that kills the scheduler
            # task for ALL jobs, not just this one.
            try:
                job.next_run = compute_next_run(job.cron, job.timezone, after=fire_time)
            except Exception as e:
                logger.error(
                    "Job %s: failed to compute next_run (cron=%r): %s — pausing job",
                    job.id, job.cron, e,
                )
                job.status = JobStatus.PAUSED
                job.next_run = None

        try:
            # Offload the file-write to a thread pool so the event loop is not
            # blocked while jobs.json is being written.
            #
            # `write_fields`, NOT `update`: `job` is the copy taken at the top of
            # this method, before the inject await, so writing it back whole also
            # writes back every field as it was BEFORE the await. A
            # `schedule migrate` completing in that window had its `room_id`
            # silently reverted — and since the migration had already stamped the
            # schema version, the job became permanently unmigratable and routed
            # by handle forever. The fire declares the fields it owns instead.
            await asyncio.to_thread(
                self._store.write_fields, job, FIRE_OWNED_FIELDS)
        except Exception as e:
            logger.error("Failed to persist job %s after fire: %s", job.id, e)

        return job

    def _connector_is_gone(self, job: ScheduledJob) -> bool:
        """The job names a connector, and no configured connector has that name.

        A job that names NO connector is unknown, not gone. Every job the
        scheduler ever wrote names one (`connector` predates schema 2; schema 2
        added `room_id`), so an empty field is a hand-edited or damaged record —
        resolved by its handle, refused if that fails, never cancelled on that
        evidence.
        """
        return bool(job.connector) and job.connector not in self._session_managers

    def _resolve_target(self, job: ScheduledJob) -> "tuple[SessionManager, str] | None":
        """The manager and the ROOM this job fires into, resolved once per fire.

        Every downstream step — inject, pause check, failure notice — takes the
        room id this returns and nothing else. A fire used to resolve the same
        job four times through four different methods, some by room and some by
        handle, and the handle-taking ones were where the defect kept
        reappearing (§2.8). One seam; one answer.

        Manager first, and ONLY `job.connector`. A job whose connector is not
        configured is not resolved by asking who else holds its room: room ids
        are per-server, not per-connector, and the canonical multi-agent setup
        is one account per agent in the same rooms — so when a connector is
        removed, every other account in those rooms is a "holder", and the
        sole survivor is by construction a DIFFERENT agent. Running the job
        there would execute it with another agent's backend, tools and account
        while the fire logged success (Codex, PR #140 round 4; an earlier
        version inferred the owner when exactly one holder remained, which
        refused only the two-holder case). Loud, None. `_fire_once` cancels
        such a job before it gets here (`_connector_is_gone`); this branch is
        the refusal for any other caller.

        Room second. `job.room_id` when the job has one. A job written before
        schema 2 has only its handle, which `resolve_handle` turns into whatever
        room currently answers to that name — the single by-name lookup on this
        path, and the reason `schedule migrate` exists.
        """
        sm = self._session_managers.get(job.connector)
        if sm is None and job.room_id:
            holders = sum(1 for m in self._session_managers.values()
                          if m.record_for_room(job.room_id) is not None)
            logger.error(
                "Job %s: its connector %r is not configured, and %d configured "
                "connector(s) hold a record for its room %s. Refusing to run it "
                "under another account — delete and recreate the job against a "
                "current watcher (or restore the connector under its old name).",
                job.id, job.connector, holders, job.room_id,
            )
            return None
        if sm is None and not job.room_id:
            # A handle embeds its connector's name, so at most one manager can
            # answer for it — this cannot cross accounts the way a room can.
            for manager in self._session_managers.values():
                room_id = manager.resolve_handle(job.watcher)
                if room_id:
                    return manager, room_id
        if sm is None:
            logger.warning(
                "Job %s: no session manager owns watcher %r (connector %r is "
                "not configured and no room or record names one). "
                "'agent-chat-gateway schedule migrate' records the room.",
                job.id, job.watcher, job.connector,
            )
            return None

        room_id = job.room_id or sm.resolve_handle(job.watcher)
        if not room_id:
            logger.warning(
                "Job %s: watcher %r has no resolvable room — a job created "
                "before schema 2 carries no room id and its watcher's record is "
                "gone, so there is nothing to fire into. "
                "'agent-chat-gateway schedule migrate' records one.",
                job.id, job.watcher,
            )
            return None
        return sm, room_id

    async def _notify_injection_failure(
        self,
        job: ScheduledJob,
        sm: "SessionManager | None",
        room_id: str,
    ) -> None:
        """Best-effort notification to the watcher's room when injection fails.

        The message is sent directly via the connector (not through the watcher
        queue) so it reaches the room even while the watcher is stopped or
        draining.  Failures are logged but never re-raised.
        """
        if sm is None or not room_id:
            return
        retry_note = (
            "The run count was **not** consumed — it will retry at the next scheduled time."
            if job.times > 0
            else "It will fire again at the next scheduled time."
        )
        text = (
            f"⚠️ Scheduled task `{job.id}` could not be delivered "
            f"(watcher `{job.watcher}` is not accepting messages). {retry_note}"
        )
        try:
            await sm.notify_watcher_room(room_id, text)
        except Exception as e:
            logger.warning(
                "Job %s: failed to send injection-failure notification: %s", job.id, e
            )

    async def _inject(
        self, job: ScheduledJob, target: "tuple[SessionManager, str] | None",
    ) -> bool:
        """Inject the job message into the room `_resolve_target` chose.

        Takes the resolution rather than redoing it — this method used to
        resolve on its own, and an earlier version resolved by *attempting
        delivery* into every manager with `except Exception: pass`, so a real
        failure in the owning manager was indistinguishable from "no manager
        has it". Resolving once, upstream, and injecting once means a failure
        is reported as a failure, against the room it was actually for.
        """
        if target is None:
            return False  # `_resolve_target` already said why
        sm, room_id = target
        try:
            return await sm.inject_message(room_id, job.message)
        except Exception as e:
            logger.error(
                "Job %s: inject_message failed for room %s (watcher %r): %s",
                job.id, room_id, job.watcher, e,
            )
            return False
