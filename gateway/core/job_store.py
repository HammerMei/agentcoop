"""JobStore: atomic read/write of scheduled jobs to jobs.json.

The daemon process is the sole writer — all mutations come through the control
socket, ensuring serial access without file-level locking. The CLI sends commands
via the control socket rather than writing directly.

Storage format
--------------
  ~/.agent-chat-gateway/data/jobs.json

  {
    "version": 1,
    "jobs": [ { ...ScheduledJob fields... }, ... ]
  }

The ``data/`` subdirectory is designed to be bind-mounted as a Docker volume
so that persistent runtime state (jobs, and any future files) survives
container recreates. Directory mounts allow atomic rename(2) inside them,
unlike single-file bind-mounts which pin the inode and cause EBUSY.

Atomic writes use the same PID-unique temp-file + rename(2) pattern as
``gateway.core.state`` to guarantee no partial writes on crash.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import get_ident as _thread_ident

from ..paths import RUNTIME_DIR
from ..schedule_types import JobStatus, ScheduledJob

logger = logging.getLogger("coop.core.job_store")


DATA_DIR = RUNTIME_DIR / "data"
JOBS_FILE = DATA_DIR / "jobs.json"
# Bump when a change to jobs.json needs an operator step. Written on every
# save, and — since version 2 — actually READ, so a file older than this code
# announces itself instead of being silently misinterpreted.
#
#   1 → 2  each job records the room it targets (`room_id`), so it survives a
#          room rename and an `expire`. Filled in by
#          `agent-chat-gateway schedule migrate`.
_SCHEMA_VERSION = 2


def _coerce_version(raw: object) -> int:
    """A file's declared version, defaulting to 1 for anything unreadable.

    1 rather than 0 or a raise: the field was written from the first release, so
    a missing or corrupt value means "old", and treating it as old only ever
    costs a migration run that finds nothing to do. Guessing NEW would skip a
    migration silently.

    `bool` is excluded explicitly because it is a subclass of `int`: without
    that, `{"version": true}` returned `True`, which then flowed through
    `min(...)` in `save` and wrote a JSON *boolean* back into the version field.
    Harmless in effect — `True < _SCHEMA_VERSION`, so it lands on the fail-safe
    side — but it contradicted this function's one promise, which is that what
    comes out is an int.
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        return 1
    return raw if raw > 0 else 1


# The statuses a job does not leave on its own; hidden from `list` by default
# and purged by `remove_expired_completed` after the TTL.
_TERMINAL = frozenset({JobStatus.COMPLETED, JobStatus.CANCELLED})


class JobStore:
    """CRUD store for ScheduledJob objects, persisted to jobs.json.

    All write operations are atomic (PID-unique temp file + rename).
    The in-memory list is the single source of truth; the file is written
    after every mutating operation.
    """

    def __init__(self, jobs_file: Path = JOBS_FILE) -> None:
        self._file = jobs_file
        self._jobs: dict[str, ScheduledJob] = {}  # keyed by job.id
        self._loaded = False
        # Protects _jobs dict against concurrent access from asyncio.to_thread()
        # workers (scheduler tick/fire) and the event loop thread (control socket
        # handlers).  The lock is held only during dict operations, never during
        # disk I/O, to avoid blocking the event loop longer than necessary.
        self._lock = threading.Lock()
        # Serialises whole saves — snapshot, version, write — against each
        # other. `_lock` alone made the snapshot consistent but not the save: a
        # fire's `to_thread` save could snapshot the jobs, lose the CPU while
        # the migration wrote every `room_id` and stamped the version, then
        # read the NEW version and replace the file with the OLD snapshot. The
        # file then claimed a migration it did not contain (Codex, PR #140
        # round 3; loud — the startup warning and `needs_migration` both read
        # the jobs, not the stamp — but a "success" that has to be re-run).
        self._save_lock = threading.Lock()
        # Assume current until a file says otherwise: no file means
        # nothing to migrate.
        self._file_version = _SCHEMA_VERSION

    # ── Schema version ────────────────────────────────────────────────────────

    @property
    def file_version(self) -> int:
        """The schema version the loaded file declared.

        `_SCHEMA_VERSION` for a file this code wrote or for no file at all
        (nothing to migrate), lower for one an older ACG wrote, and HIGHER for
        one a newer ACG wrote — that direction is not an error here; it is what
        `_announce_version` warns about and what `migrate` refuses.

        It is what the file said when it was LOADED. A save does not move it (see
        `save`, which writes `min(...)` and leaves this alone), so after this code
        has written a newer file down to its own version, this still reports the
        version that file arrived with. Deliberate: that is the version whose
        fields were dropped, which is what the operator needs to be told.
        """
        return self._file_version

    def needs_migration(self) -> bool:
        """Is there migration work outstanding?

        Two signals, because the declared version alone is a claim about the
        file and the second one is the observable fact.

        The version can say "current" over a job that is not: any interleaving
        that writes a job back from a copy taken before the migration's write
        drops the `room_id` after the version has been stamped. `write_fields`
        closed the one such path this system has, but the failure it produced was
        SILENT AND PERMANENT — `migrate` early-returns on a current version, so
        the startup warning never came back and the operator was dead-ended by
        the system's own advice. A job with no room id is the thing migration
        exists to fix, so ask about it directly rather than trusting the stamp.

        The cost of the second signal is a re-run that finds nothing, which is
        what idempotence is for. A COMPLETED job is excluded: it has nothing
        left to fire, so it has nothing left to migrate — `list_jobs()` already
        drops those.
        """
        return self._file_version < _SCHEMA_VERSION or self.jobs_missing_room_id()

    def jobs_missing_room_id(self) -> bool:
        """Is any live job still without a room id?

        Defined here, once, because it is a fact about the store's contents —
        `needs_migration`, the startup warning and `job_migrate`'s step selection
        all ask it, and three copies of "any job with an empty room_id" is how
        one of them comes to disagree with the others.

        Reads `_jobs` directly rather than through `list_jobs()`, which asserts
        the store is loaded: the startup warning asks this from inside `load()`,
        before `_loaded` is set. Going through `list_jobs` raised there, and
        `load`'s broad `except` reported it as "Failed to load jobs file —
        starting with empty list" and skipped the announcement entirely. The
        COMPLETED exclusion is duplicated instead, deliberately — a completed job
        has nothing left to fire, so nothing left to migrate.
        """
        with self._lock:
            return any(not job.room_id for job in self._jobs.values()
                       if job.status != JobStatus.COMPLETED)

    def stamp_version(self, version: int) -> None:
        """Record that the file is now at `version`, and write it out.

        Separate from `save()` because saving happens constantly and must not
        silently claim a migration that did not run. `save` therefore writes the
        version the file already had, and this is the ONE place that moves it —
        called only by the migration, only after every step finished with nothing
        left needing attention.

        An earlier version of `save` wrote `_SCHEMA_VERSION` unconditionally,
        which is what makes the separation load-bearing: one ordinary fire
        stamped an unmigrated file current, silencing both the startup warning
        and `schedule migrate` while no job had a room id.
        """
        self._file_version = version
        self.save()

    def _announce_version(self) -> None:
        """Say something at startup, because a migration nobody knows about is
        a migration nobody runs.

        A file NEWER than this code is the more dangerous direction and gets a
        warning of its own: an older ACG reading it will drop whatever fields it
        does not know on the next save.
        """
        if self._file_version > _SCHEMA_VERSION:
            logger.warning(
                "%s declares schema version %d but this ACG understands %d — it "
                "was written by a newer version. Saving from here will DROP any "
                "field this version does not know. Upgrade, or move the file "
                "aside.", self._file, self._file_version, _SCHEMA_VERSION,
            )
        elif self._file_version < _SCHEMA_VERSION:
            logger.warning(
                "%s is at schema version %d; this ACG uses %d. Scheduled jobs "
                "keep working, with the behaviour of the older version. Run "
                "'agent-chat-gateway schedule migrate' to bring them up to date "
                "— do it before renaming any rooms, since the migration reads "
                "each job's watcher name to find its room.",
                self._file, self._file_version, _SCHEMA_VERSION,
            )
        elif self.jobs_missing_room_id():
            # The version says current and a job says otherwise. Reported as its
            # own case rather than folded into the message above, which would
            # name a version number that is not the problem — and reported at
            # all because the alternative was a job that quietly stopped
            # arriving with the warning switched off.
            logger.warning(
                "%s declares schema version %d, but at least one scheduled job "
                "has no recorded room. Such a job cannot bring its watcher back "
                "once the room's record is reclaimed — it fails at every slot. "
                "Run 'agent-chat-gateway schedule migrate' to record the rooms; "
                "it is safe to re-run.",
                self._file, self._file_version,
            )

    # ── Load / save ───────────────────────────────────────────────────────────

    def load(self) -> None:
        """Load jobs from disk. Call once at daemon startup."""
        self._file.parent.mkdir(parents=True, exist_ok=True)
        if not self._file.exists():
            logger.info("No jobs file found at %s — starting with empty job list", self._file)
            self._loaded = True
            return
        try:
            data = json.loads(self._file.read_text())
            self._file_version = _coerce_version(data.get("version"))
            raw_jobs = data.get("jobs", [])
            self._jobs = {}
            for raw in raw_jobs:
                try:
                    job = ScheduledJob.from_dict(raw)
                    self._jobs[job.id] = job
                except Exception as e:
                    logger.warning("Skipping malformed job entry: %s — %s", raw, e)
            logger.info("Loaded %d scheduled job(s) from %s", len(self._jobs), self._file)
            self._announce_version()
        except Exception as e:
            logger.warning("Failed to load jobs file %s — starting with empty list: %s", self._file, e)
        self._loaded = True

    def save(self) -> None:
        """Atomically write current job list to disk.

        A snapshot of the in-memory dict is taken under ``_lock`` before any
        I/O begins, so the lock is held only for the duration of the dict
        iteration — not during the file write itself.  This prevents a
        ``RuntimeError: dictionary changed size during iteration`` if a control-
        socket handler mutates ``_jobs`` concurrently on the event loop thread
        while the scheduler calls ``save()`` via ``asyncio.to_thread()``.
        """
        with self._save_lock:
            with self._lock:
                snapshot = [j.to_dict() for j in self._jobs.values()]
            self._file.parent.mkdir(parents=True, exist_ok=True)
            # `self._file_version`, NOT `_SCHEMA_VERSION`. An ordinary save — one per
            # fire, one per `schedule pause` — would otherwise stamp an unmigrated
            # file as current, and the operator would lose both the startup warning
            # and the migration: `schedule migrate` would answer "nothing to do"
            # while every job still had an empty `room_id`. `stamp_version()` is the
            # only thing that moves this, and only after a migration has run.
            # `min(...)`, not either one alone — both directions are wrong on their
            # own, and both were verified:
            #
            # * `_SCHEMA_VERSION` stamps an UNMIGRATED file as current. One ordinary
            #   fire then silences the startup warning and makes `schedule migrate`
            #   answer "nothing to do" while every job has an empty `room_id`.
            # * `self._file_version` stamps a NEWER file with its own version while
            #   writing content this code shaped — `to_dict` has already dropped the
            #   fields it does not know, so the file would claim a version whose
            #   fields it no longer contains, and a future ACG would skip the
            #   migrations that restore them.
            #
            # The floor is honest in both: never claim more than this code wrote, and
            # never claim more than the file already earned.
            data = {"version": min(self._file_version, _SCHEMA_VERSION), "jobs": snapshot}
            # Include thread ident so concurrent save() calls from different
            # asyncio.to_thread() workers don't clobber each other's temp file.
            tmp = self._file.with_name(f"{self._file.name}.{os.getpid()}.{_thread_ident()}.tmp")
            try:
                tmp.write_text(json.dumps(data, indent=2))
                tmp.replace(self._file)
            except Exception:
                tmp.unlink(missing_ok=True)
                raise
            logger.debug("Saved %d scheduled job(s) to %s", len(snapshot), self._file)

    # ── CRUD ──────────────────────────────────────────────────────────────────

    def _assert_loaded(self) -> None:
        """Raise RuntimeError if load() has not been called yet."""
        if not self._loaded:
            raise RuntimeError(
                "JobStore.load() must be called before any CRUD operation. "
                "Did you forget to call load() at daemon startup?"
            )

    def add(self, job: ScheduledJob) -> ScheduledJob:
        """Add a new job and persist. Returns the saved job."""
        self._assert_loaded()
        with self._lock:
            self._jobs[job.id] = job
        self.save()
        logger.info("Scheduled job created: %s (watcher=%s, cron=%r)", job.id, job.watcher, job.cron)
        return job

    def update(self, job: ScheduledJob) -> None:
        """Update an existing job in place and persist."""
        self._assert_loaded()
        with self._lock:
            if job.id not in self._jobs:
                raise KeyError(f"Job {job.id!r} not found")
            self._jobs[job.id] = job
        self.save()

    def write_fields(self, job: ScheduledJob, fields: frozenset[str]) -> bool:
        """Copy `fields` from `job` onto the STORED job of the same id. `False`
        if it is gone.

        The safe form of `update` for any writer that holds a job across an
        `await`. `update` replaces the stored object, so it also writes back
        every field the holder read BEFORE the await — reverting whatever
        another writer changed in the meantime. That is not hypothetical:
        `_fire_once` copies the job on entry, and a `schedule migrate` landing
        inside its inject window had its `room_id` silently discarded, after
        reporting success and stamping the schema version.

        `fields` is the caller's declaration of what it owns — see
        `schedule_types.FIRE_OWNED_FIELDS` and friends, whose union is checked
        against the dataclass so a new field must pick an owner.
        """
        self._assert_loaded()
        with self._lock:
            stored = self._jobs.get(job.id)
            if stored is None:
                return False
            if stored.status == JobStatus.CANCELLED:
                # A cancellation landed while this fire was in flight. Its copy
                # says ACTIVE; writing that back would resurrect the job under
                # the cancellation's own timestamp and reason (see `cancel`).
                return False
            for name in fields:
                setattr(stored, name, getattr(job, name))
        self.save()
        return True

    def set_room_id(self, job_id: str, room_id: str, *, connector: str = "") -> bool:
        """Record a job's room (and connector, if it had none). `False` if gone.

        The narrow case of `write_fields` — see there for why a targeted write is
        the general rule — plus one thing of its own: `False` rather than a raise
        for a job deleted mid-run. `update` raises `KeyError`, which aborts the
        whole migration and loses the report for every job already done, and the
        report IS `schedule migrate`'s product.

        This docstring has been wrong twice, in opposite directions, and both
        times for the same reason: it reasoned about the store's ACCESSORS and
        drew a conclusion about its WRITERS. First it claimed a lost-update
        hazard that `get`-returns-the-live-object rules out. Then it concluded
        there was therefore nothing to protect — which `core/scheduler.py`'s
        `copy.copy(job)` refutes, and that copy was discarding `room_id` on a
        path the release documents. **A claim about concurrent writes is a claim
        about writers**: enumerate them (fire, `schedule migrate`, `schedule
        pause`/`resume`) and what each holds across an await, not the getters.
        """
        self._assert_loaded()
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            job.room_id = room_id
            if connector and not job.connector:
                job.connector = connector
        self.save()
        return True

    def cancel(self, job_id: str, *, reason: str) -> bool:
        """Mark a job CANCELLED and keep it. Returns False if it is gone.

        The gateway's own cancellations — the bot removed from the room, the
        job's connector gone from the config — used to `remove`, leaving one
        AUDIT log line as the only trace. A job cancelled by mistake was then
        unrecoverable and, once the log rotated, unexplainable. The record now
        stays, with when and why, for `completed_job_ttl_days` like a completed
        job; `schedule resume` restores it. Only the operator's `schedule
        delete` removes it early (owner, 2026-09-02).

        Written on the live object under the lock, like `set_room_id`, so a fire
        holding its own copy across an await cannot revert it — `write_fields`
        refuses a write-back on a CANCELLED job.
        """
        self._assert_loaded()
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job.status == JobStatus.CANCELLED:
                return True   # the first cancellation's when-and-why is the evidence
            job.status = JobStatus.CANCELLED
            job.cancelled_at = datetime.now(UTC).isoformat()
            job.cancel_reason = reason
        self.save()
        return True

    def remove(self, job_id: str) -> bool:
        """Remove a job by ID. Returns True if found and removed."""
        self._assert_loaded()
        with self._lock:
            if job_id not in self._jobs:
                return False
            del self._jobs[job_id]
        self.save()
        logger.info("Scheduled job deleted: %s", job_id)
        return True

    def remove_expired_completed(self, ttl_days: int) -> int:
        """Remove terminal jobs — COMPLETED or CANCELLED — older than ttl_days.

        A completed job ages from `completed_at`, a cancelled one from
        `cancelled_at`. If ttl_days == 0, removes all of them immediately.
        Returns the number of jobs removed.
        """
        self._assert_loaded()
        if ttl_days < 0:
            return 0
        cutoff = datetime.now(UTC) - timedelta(days=ttl_days)
        to_remove = []
        # Hold the lock for both the iteration and deletion passes so that a
        # concurrent add() / remove() from the event loop thread cannot change
        # the dict size mid-iteration (which would raise RuntimeError in CPython).
        with self._lock:
            for job in self._jobs.values():
                if job.status not in _TERMINAL:
                    continue
                ended_at = (job.cancelled_at if job.status == JobStatus.CANCELLED
                            else job.completed_at)
                if ttl_days == 0:
                    to_remove.append(job.id)
                elif ended_at:
                    try:
                        ended = datetime.fromisoformat(ended_at)
                        if ended < cutoff:
                            to_remove.append(job.id)
                    except ValueError:
                        # Malformed timestamp — remove it
                        to_remove.append(job.id)
            for jid in to_remove:
                del self._jobs[jid]
        if to_remove:
            self.save()
            logger.info("Purged %d expired completed/cancelled job(s)", len(to_remove))
        return len(to_remove)

    # ── Queries ───────────────────────────────────────────────────────────────

    def get(self, job_id: str) -> ScheduledJob | None:
        """Return a job by ID, or None if not found."""
        self._assert_loaded()
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(
        self,
        *,
        connector: str | None = None,
        include_completed: bool = False,
    ) -> list[ScheduledJob]:
        """Return jobs, optionally filtered by connector.

        By default only ACTIVE and PAUSED jobs are returned. Pass
        ``include_completed=True`` to also include the terminal ones — COMPLETED
        and CANCELLED — which stay in the file for the TTL.
        """
        self._assert_loaded()
        with self._lock:
            jobs = list(self._jobs.values())
        if not include_completed:
            jobs = [j for j in jobs if j.status not in _TERMINAL]
        if connector:
            jobs = [j for j in jobs if j.connector == connector]
        return jobs

    def list_due(self) -> list[ScheduledJob]:
        """Return ACTIVE jobs whose next_run is at or before now (UTC)."""
        self._assert_loaded()
        now = datetime.now(UTC)
        with self._lock:
            snapshot = list(self._jobs.values())
        due = []
        for job in snapshot:
            if job.status != JobStatus.ACTIVE:
                continue
            if job.next_run is None:
                continue
            try:
                fire_at = datetime.fromisoformat(job.next_run)
                if fire_at <= now:
                    due.append(job)
            except ValueError:
                logger.warning("Job %s has malformed next_run %r — skipping", job.id, job.next_run)
        return due
