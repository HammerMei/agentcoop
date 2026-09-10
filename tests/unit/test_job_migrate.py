"""`schedule migrate` — version-aware, re-runnable, and it never guesses.

The owner's design (2026-09-01), replacing a lazy fire-time backfill. The
difference is not speed: a backfill runs at a moment nobody chose, and the 1→2
step reads each job's watcher HANDLE to find its room — which only names the
right room while nobody has renamed it. An operator can pick a moment when that
holds; a job firing once a year cannot.

Two properties, tested separately because they answer different questions:

* **Version awareness** decides WHICH steps run, so a deployment jumping 1 → 3
  gets both and one jumping 2 → 3 gets only the second.
* **Idempotence** makes each step safe to run again, so a wrong version guess
  cannot corrupt anything.

Run with:
    uv run python -m pytest tests/unit/test_job_migrate.py -v
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from gateway.core.job_migrate import (
    migrate,
    room_name_for_label,
    split_handle,
)
from gateway.core.job_store import _SCHEMA_VERSION, JobStore
from gateway.core.state import WatcherState
from gateway.schedule_types import FIRE_OWNED_FIELDS, JobStatus, ScheduledJob


def _entry(name="rc", *, records=None, resolves=None):
    """A `ConnectorEntry` stand-in: a record lookup and a name resolver."""
    entry = MagicMock()
    entry.name = name
    entry.session_manager.get_watcher_state = MagicMock(
        side_effect=(records or {}).get)
    entry.connector.resolve_room = AsyncMock(
        side_effect=lambda n: (resolves or {}).get(n))
    return entry


def _record(handle, room_id):
    return WatcherState(
        watcher_name=handle, session_id="", room_id=room_id,
        room_name="general", room_type="channel", room_kind="channel",
    )


def _room(room_id):
    room = MagicMock()
    room.id = room_id
    return room


class TestHandleParsing(unittest.TestCase):
    def test_the_first_colon_is_the_boundary(self):
        """A connector name may not contain `:` and the label encoder escapes it
        out of room names, so a room called `dm:alice` cannot fool this."""
        self.assertEqual(split_handle("rc:general"), ("rc", "general"))
        self.assertEqual(split_handle("rc:dm:alice"), ("rc", "dm:alice"))

    def test_a_handle_with_no_colon_has_no_connector(self):
        self.assertEqual(split_handle("legacy-name"), ("", "legacy-name"))

    def test_a_channel_label_is_the_room_name(self):
        self.assertEqual(room_name_for_label("general"), "general")

    def test_a_label_is_percent_decoded_first(self):
        """`watcher_label` escapes everything outside `[A-Za-z0-9._-]`, so a
        voice room `a/b` labels as `a%2Fb`. The voice connector echoes whatever
        name it is given, so asking it to resolve `a%2Fb` would have recorded
        that as the room id — matching nothing — and reported a success."""
        self.assertEqual(room_name_for_label("a%2Fb"), "a/b")
        self.assertEqual(room_name_for_label("dm:al%20ice"), "@al ice")

    def test_a_dm_label_becomes_the_at_spelling(self):
        """`@alice` is the spelling `resolve_room` documents for a DM."""
        self.assertEqual(room_name_for_label("dm:alice"), "@alice")

    def test_a_group_dm_label_cannot_be_resolved(self):
        """It is a digest of the room id, not a name. Reported, never guessed."""
        self.assertIsNone(room_name_for_label("gdm:8f3a2b1c"))


class _MigrateCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "jobs.json"

    def _write_file(self, version, jobs):
        self.path.write_text(json.dumps({"version": version, "jobs": jobs}))

    def _store(self) -> JobStore:
        store = JobStore(self.path)
        store.load()
        return store

    def _job(self, job_id="acg-1", watcher="rc:general", **kw):
        return ScheduledJob(
            id=job_id, watcher=watcher, connector="rc", message="poke",
            cron="0 9 * * *", status=JobStatus.ACTIVE, **kw,
        ).to_dict()


class TestVersionAwareness(_MigrateCase):
    async def test_a_current_file_reports_nothing_to_do(self):
        self._write_file(_SCHEMA_VERSION, [])
        store = self._store()

        report = await migrate(store, [_entry()])

        self.assertEqual(report.steps, [])
        self.assertEqual(report.from_version, report.to_version)

    async def test_an_old_file_runs_the_step_and_is_stamped(self):
        self._write_file(1, [self._job()])
        store = self._store()
        entry = _entry(records={"rc:general": _record("rc:general", "room-1")})

        report = await migrate(store, [entry])

        self.assertEqual(len(report.steps), 1)
        self.assertEqual(store.file_version, _SCHEMA_VERSION)
        self.assertEqual(
            json.loads(self.path.read_text())["version"], _SCHEMA_VERSION)

    async def test_a_newer_file_is_refused_rather_than_migrated_down(self):
        """Saving would drop the fields this version does not know."""
        self._write_file(_SCHEMA_VERSION + 5, [])
        store = self._store()

        with self.assertRaises(ValueError) as cm:
            await migrate(store, [_entry()])
        self.assertIn("newer version", str(cm.exception))

    async def test_a_missing_version_reads_as_old(self):
        """The field has been written since the first release, so an absent one
        means old. Guessing NEW would skip a migration silently."""
        self.path.write_text(json.dumps({"jobs": []}))
        store = self._store()

        self.assertEqual(store.file_version, 1)
        self.assertTrue(store.needs_migration())

    async def test_every_unreadable_version_lands_on_the_fail_safe_side(self):
        """Enumerated rather than sampled: this value decides whether migrations
        run at all, so a value that read as NEW would skip one in silence. Every
        case must come out an `int` >= 1 that still needs migrating."""
        hostile = [None, "2", "garbage", 2.0, True, False, -1, 0, [], {}]
        for raw in hostile:
            with self.subTest(version=raw):
                self.path.write_text(json.dumps({"version": raw, "jobs": []}))
                store = self._store()

                self.assertIs(type(store.file_version), int,
                              "the version is an int or nothing downstream holds")
                self.assertEqual(store.file_version, 1)
                self.assertTrue(store.needs_migration())

    async def test_a_boolean_version_does_not_survive_a_save(self):
        """`bool` is a subclass of `int`, so `{"version": true}` passed the type
        check and `min(True, 2)` wrote a JSON *boolean* straight back into the
        schema field."""
        self.path.write_text(json.dumps({"version": True, "jobs": []}))
        store = self._store()

        store.save()

        written = json.loads(self.path.read_text())["version"]
        self.assertIs(type(written), int, f"persisted {written!r}")
        self.assertEqual(written, 1)


class TestTheOneToTwoStep(_MigrateCase):
    async def test_a_record_supplies_the_room_id(self):
        self._write_file(1, [self._job()])
        store = self._store()
        entry = _entry(records={"rc:general": _record("rc:general", "room-1")})

        report = await migrate(store, [entry])

        self.assertEqual(store.get("acg-1").room_id, "room-1")
        self.assertEqual(report.changed, 1)
        entry.connector.resolve_room.assert_not_awaited()

    async def test_a_cancelled_job_is_migrated_too(self):
        """A cancelled job is kept so `schedule resume` can restore it, and a
        restored job needs its room. `jobs_missing_room_id` counted it while
        the step skipped it, so the startup warning became permanent and
        `migrate` reported success with nothing done (internal review)."""
        job = self._job()
        job.update(status="cancelled", cancelled_at="2026-09-02T10:00:00+00:00",
                   cancel_reason="the bot was removed")
        self._write_file(2, [job])
        store = self._store()
        self.assertTrue(store.needs_migration())

        report = await migrate(store, [_entry(records={"rc:general": _record("rc:general", "room-1")})])

        self.assertEqual(store.get("acg-1").room_id, "room-1")
        self.assertEqual(report.changed, 1)
        self.assertFalse(store.needs_migration())

    async def test_without_a_record_the_connector_resolves_the_name(self):
        """The case right after an upgrade, where static-era records have been
        pruned and rule-derived ones do not exist until a room speaks."""
        self._write_file(1, [self._job()])
        store = self._store()
        entry = _entry(resolves={"general": _room("room-1")})

        report = await migrate(store, [entry])

        self.assertEqual(store.get("acg-1").room_id, "room-1")
        entry.connector.resolve_room.assert_awaited_once_with("general")
        # Said out loud in the report: a room found by NAME from the handle is a
        # guess the operator should be able to spot — a static-era watcher whose
        # name was shaped like a handle could have served another room.
        detail = report.outcomes[0].detail
        self.assertIn("by NAME", detail)
        self.assertIn("verify", detail)

    async def test_a_room_taken_from_the_record_is_not_flagged_as_by_name(self):
        self._write_file(1, [self._job()])
        store = self._store()
        entry = _entry(records={"rc:general": _record("rc:general", "room-1")})

        report = await migrate(store, [entry])

        self.assertNotIn("by NAME", report.outcomes[0].detail)

    async def test_a_dm_job_resolves_through_the_at_spelling(self):
        self._write_file(1, [self._job(watcher="rc:dm:alice")])
        store = self._store()
        entry = _entry(resolves={"@alice": _room("room-dm")})

        await migrate(store, [entry])

        self.assertEqual(store.get("acg-1").room_id, "room-dm")

    async def test_an_empty_connector_field_is_filled_in_too(self):
        """`_resolve_target` needs it, and a job with neither field cannot be
        routed to a manager at all."""
        job = self._job()
        job["connector"] = ""
        self._write_file(1, [job])
        store = self._store()
        entry = _entry(records={"rc:general": _record("rc:general", "room-1")})

        await migrate(store, [entry])

        self.assertEqual(store.get("acg-1").connector, "rc")

    async def test_a_completed_job_is_left_alone(self):
        job = self._job()
        job["status"] = JobStatus.COMPLETED.value
        self._write_file(1, [job])
        store = self._store()

        report = await migrate(store, [_entry()])

        self.assertEqual(report.outcomes, [])


class TestAStaticEraNameIsNotARoomName(_MigrateCase):
    """The worst guess this module could make, and it was making it.

    A derived handle always contains a `:` — `watcher_label` builds it as
    `f"{connector}:{label}"` and config refuses a colon in a connector name. So
    a colon-less watcher is a STATIC-era name, which is not a room name and
    never was. The migration was resolving it as one.

    Measured before the fix: a static watcher `stock-bot` that watched #trading
    bound its job to a channel that merely SHARED the name, reported it as
    `✓ resolved 'stock-bot'`, and stamped the schema version — so the startup
    warning went quiet and every later fire delivered into the wrong room, with
    nothing anywhere saying so.
    """

    STATIC = "stock-bot"

    def _static_job(self):
        return self._job(watcher=self.STATIC)

    async def test_it_is_never_resolved_as_a_room_name(self):
        self._write_file(1, [self._static_job()])
        store = self._store()
        # A channel on the server happens to share the static watcher's name.
        entry = _entry(resolves={self.STATIC: _room("room-someone-elses")})

        report = await migrate(store, [entry])

        self.assertEqual(store.get("acg-1").room_id, "",
                         "bound to a room that merely shared the name")
        # Never even asked about: the name was not a room name to begin with.
        entry.connector.resolve_room.assert_not_awaited()
        self.assertEqual(len(report.unresolved), 1)
        self.assertFalse(report.outcomes[0].changed)

    async def test_the_operator_is_told_what_to_do_about_it(self):
        """"Delete and recreate" — the same instruction step 7 of the migration
        guide gives. Repeating it here is what makes it hold for an operator who
        skipped that step."""
        self._write_file(1, [self._static_job()])
        entry = _entry(resolves={self.STATIC: _room("room-someone-elses")})

        report = await migrate(self._store(), [entry])
        detail = report.outcomes[0].detail

        self.assertIn("static-era", detail)
        self.assertIn("delete this job", detail)
        self.assertIn(self.STATIC, detail, "name the job's watcher, not a class")

    async def test_it_holds_the_schema_version_back(self):
        """Silence was the real damage: stamping made the startup warning go
        quiet, so nothing was left pointing at the job."""
        self._write_file(1, [self._static_job()])
        store = self._store()

        report = await migrate(store, [_entry(resolves={self.STATIC: _room("x")})])

        self.assertFalse(report.stamped)
        self.assertEqual(json.loads(self.path.read_text())["version"], 1)

    async def test_a_live_record_is_still_authoritative(self):
        """The fence is on GUESSING, not on the static-era job. If a record
        exists it holds the real room id — no name lookup, no guess — and the
        job is migrated properly."""
        self._write_file(1, [self._static_job()])
        store = self._store()
        entry = _entry(records={self.STATIC: _record(self.STATIC, "room-trading")})

        report = await migrate(store, [entry])

        self.assertEqual(store.get("acg-1").room_id, "room-trading")
        self.assertEqual(report.unresolved, [])
        entry.connector.resolve_room.assert_not_awaited()

    async def test_a_derived_handle_is_unaffected(self):
        """The fence must not catch the population the migration exists for."""
        self._write_file(1, [self._job()])  # 'rc:general'
        store = self._store()

        await migrate(store, [_entry(resolves={"general": _room("room-1")})])

        self.assertEqual(store.get("acg-1").room_id, "room-1")


class TestNothingIsGuessed(_MigrateCase):
    async def test_an_unresolvable_room_leaves_the_job_untouched(self):
        self._write_file(1, [self._job()])
        store = self._store()
        entry = _entry()  # no record, and the connector knows no such room

        report = await migrate(store, [entry])

        self.assertEqual(store.get("acg-1").room_id, "")
        self.assertEqual(report.changed, 0)
        self.assertEqual(len(report.unresolved), 1)
        self.assertIn("knows no room named", report.unresolved[0].detail)

    async def test_a_group_dm_says_what_to_do_instead(self):
        self._write_file(1, [self._job(watcher="rc:gdm:8f3a2b1c")])
        store = self._store()

        report = await migrate(store, [_entry()])

        self.assertEqual(store.get("acg-1").room_id, "")
        self.assertIn("recreate it", report.unresolved[0].detail)

    async def test_a_connector_that_raises_does_not_stop_the_run(self):
        """One job's resolution failure must not deny the others their migration."""
        self._write_file(1, [self._job("acg-1"), self._job("acg-2", "rc:dev")])
        store = self._store()
        entry = _entry(resolves={"dev": _room("room-dev")})
        entry.connector.resolve_room = AsyncMock(
            side_effect=lambda n: (_ for _ in ()).throw(OSError("boom"))
            if n == "general" else _room("room-dev"))

        report = await migrate(store, [entry])

        self.assertEqual(store.get("acg-1").room_id, "")
        self.assertEqual(store.get("acg-2").room_id, "room-dev")
        self.assertEqual(report.changed, 1)

    async def test_an_unknown_connector_is_reported_not_assumed(self):
        job = self._job()
        job["connector"] = "gone-away"
        job["watcher"] = "gone-away:general"
        self._write_file(1, [job])
        store = self._store()

        report = await migrate(store, [_entry("rc")])

        self.assertEqual(store.get("acg-1").room_id, "")
        self.assertIn("no configured connector", report.unresolved[0].detail)


class TestItIsSafeToRunAgain(_MigrateCase):
    async def test_a_second_run_changes_nothing(self):
        self._write_file(1, [self._job()])
        store = self._store()
        entry = _entry(records={"rc:general": _record("rc:general", "room-1")})

        await migrate(store, [entry])
        first = json.loads(self.path.read_text())

        # Reload, as a fresh daemon would, and run it again.
        again = self._store()
        report = await migrate(again, [entry])

        self.assertEqual(report.steps, [], "the version stopped it")
        self.assertEqual(json.loads(self.path.read_text())["jobs"], first["jobs"])

    async def test_the_step_itself_is_idempotent_even_if_the_version_is_wrong(self):
        """The property the version cannot provide: if a file claims to be old
        when it is not, re-running the step must still be harmless."""
        self._write_file(1, [self._job(room_id="room-already")])
        store = self._store()
        entry = _entry(records={"rc:general": _record("rc:general", "room-other")})

        report = await migrate(store, [entry])

        self.assertEqual(store.get("acg-1").room_id, "room-already",
                         "an existing id must not be overwritten")
        self.assertEqual(report.changed, 0)
        self.assertIn("already has", report.outcomes[0].detail)


if __name__ == "__main__":
    unittest.main()


class TestTheVersionOnlyMovesWhenAMigrationRuns(_MigrateCase):
    """Found by review: `save()` stamped the code's version unconditionally, so
    one ordinary fire marked an unmigrated file as current — the startup warning
    vanished and `schedule migrate` answered "nothing to do" while every job
    still had an empty `room_id`.

    `stamp_version`'s docstring described that hazard and only half-prevented it:
    it was the one place that moved the IN-MEMORY version, while every save
    rewrote the file's.
    """

    async def test_an_ordinary_save_leaves_the_file_version_alone(self):
        self._write_file(1, [self._job()])
        store = self._store()

        job = store.get("acg-1")
        job.run_count = 1
        store.update(job)  # what a fire does

        self.assertEqual(json.loads(self.path.read_text())["version"], 1)
        self.assertEqual(self._store().file_version, 1,
                         "a restart would see the file as migrated")
        self.assertTrue(self._store().needs_migration())

    async def test_a_save_does_not_stamp_a_newer_file_with_its_own_version(self):
        """The other direction, and the one the first fix introduced: writing
        `self._file_version` would claim version N while `to_dict` had already
        dropped the fields version N carries — a future AgentCoop would then skip the
        migrations that restore them."""
        self._write_file(_SCHEMA_VERSION + 5, [self._job()])
        store = self._store()

        store.update(ScheduledJob.from_dict(self._job()))

        self.assertEqual(
            json.loads(self.path.read_text())["version"], _SCHEMA_VERSION,
            "never claim more than this code wrote",
        )

    async def test_a_removal_leaves_it_alone_too(self):
        """`add`/`update`/`remove`/`remove_expired_completed` all save."""
        self._write_file(1, [self._job()])
        store = self._store()

        store.remove("acg-1")

        self.assertEqual(json.loads(self.path.read_text())["version"], 1)

    async def test_only_the_migration_moves_it(self):
        self._write_file(1, [self._job()])
        store = self._store()
        entry = _entry(records={"rc:general": _record("rc:general", "room-1")})

        await migrate(store, [entry])

        self.assertEqual(json.loads(self.path.read_text())["version"],
                         _SCHEMA_VERSION)


class TestAnUnfinishedMigrationStaysRunnable(_MigrateCase):
    """Also from review: stamping over an unresolved job would make the re-run
    the operator is TOLD to make answer "nothing to do" — and that job could
    then never be migrated at all."""

    async def test_the_version_does_not_move_while_a_job_needs_attention(self):
        self._write_file(1, [self._job("acg-1"), self._job("acg-2", "rc:dev")])
        store = self._store()
        # 'general' resolves; 'dev' does not.
        entry = _entry(resolves={"general": _room("room-1")})

        report = await migrate(store, [entry])

        self.assertEqual(report.changed, 1)
        self.assertEqual(len(report.unresolved), 1)
        self.assertEqual(json.loads(self.path.read_text())["version"], 1,
                         "an unfinished migration must stay runnable")

    async def test_the_re_run_after_a_fix_completes_it(self):
        """The whole point of not stamping early."""
        self._write_file(1, [self._job("acg-1"), self._job("acg-2", "rc:dev")])
        store = self._store()
        await migrate(store, [_entry(resolves={"general": _room("room-1")})])

        # The operator brings the second room back, and re-runs.
        again = self._store()
        report = await migrate(
            again, [_entry(resolves={"general": _room("room-1"),
                                     "dev": _room("room-dev")})])

        self.assertEqual(again.get("acg-2").room_id, "room-dev")
        self.assertEqual(report.unresolved, [])
        self.assertEqual(json.loads(self.path.read_text())["version"],
                         _SCHEMA_VERSION)

    async def test_a_clean_re_run_is_not_mistaken_for_unfinished_work(self):
        """A job that already has an id did not change and needs nothing. The
        two used to be one state, which would have left the version stuck."""
        self._write_file(1, [self._job(room_id="room-1")])
        store = self._store()

        report = await migrate(store, [_entry()])

        self.assertEqual(report.changed, 0)
        self.assertEqual(report.unresolved, [], "'already done' is not 'stuck'")
        self.assertEqual(json.loads(self.path.read_text())["version"],
                         _SCHEMA_VERSION)


class TestTheReportSaysWhetherTheVersionActuallyMoved(_MigrateCase):
    """`to_version` is the version the run AIMED at, and it is set before any
    decision — so the CLI, reading it alone, told the operator "jobs.json
    migrated 1 → 2" about a file the very same run had deliberately left on 1.
    The next startup then warned that a migration was still owed.

    `stamped` is the answer to the question the operator is actually asking.
    """

    async def test_it_is_false_when_a_job_held_the_version_back(self):
        self._write_file(1, [self._job("acg-1"), self._job("acg-2", "rc:dev")])
        store = self._store()

        report = await migrate(store, [_entry(resolves={"general": _room("room-1")})])

        self.assertFalse(report.stamped)
        self.assertEqual(report.to_version, _SCHEMA_VERSION, "still the target")
        self.assertEqual(json.loads(self.path.read_text())["version"], 1,
                         "and the flag agrees with the file")

    async def test_it_is_true_when_the_run_finished_clean(self):
        self._write_file(1, [self._job("acg-1")])
        store = self._store()

        report = await migrate(store, [_entry(resolves={"general": _room("room-1")})])

        self.assertTrue(report.stamped)
        self.assertEqual(json.loads(self.path.read_text())["version"],
                         _SCHEMA_VERSION)

    async def test_an_already_current_file_counts_as_stamped(self):
        """Nothing was written, but the file IS at `to_version` — which is what
        the flag reports. Saying otherwise would have the CLI announce a problem
        on the happiest path there is."""
        self._write_file(_SCHEMA_VERSION, [self._job(room_id="room-1")])
        store = self._store()

        report = await migrate(store, [_entry()])

        self.assertTrue(report.stamped)
        self.assertEqual(report.from_version, report.to_version)

    async def test_the_flag_crosses_the_control_socket(self):
        """It is only useful if the CLI can see it — `to_dict` is the boundary,
        and the handler spreads it into the response."""
        self._write_file(1, [self._job("acg-1"), self._job("acg-2", "rc:dev")])
        report = await migrate(
            self._store(), [_entry(resolves={"general": _room("room-1")})])

        self.assertIn("stamped", report.to_dict())
        self.assertFalse(report.to_dict()["stamped"])


class TestAConcurrentFireCannotUndoTheMigration(_MigrateCase):
    """`_migrate_1_to_2` reads a job, awaits `resolve_room` (a network round
    trip), then writes. These pin what may land in that window.

    **Only the third test discriminates.** Reverting the production call to
    `store.update(job)` fails `test_a_deletion_mid_run_costs_only_that_job` and
    nothing else — measured, after an earlier version of this docstring claimed
    the other two caught lost updates. They cannot: `JobStore.get` hands out the
    stored object itself, so a fire and the migration mutate one instance and
    neither holds a stale copy.

    The first two are therefore invariant tests, not regression tests, and they
    are kept as such: they pin that a fire's progress and the migration's write
    coexist, which is the property a future change to copy-on-read would break.
    Anything claiming to have prevented a lost update here has to name the read
    that produced the stale copy.
    """

    async def _migrate_with_a_fire_in_the_middle(self, store, entry, mutate):
        """Run the migration, letting `mutate(store)` land during resolution.

        The fire's copy is taken BEFORE the migration starts, which is the point:
        `_fire_once` copies on entry and writes back after its inject await, so
        its copy predates whatever the migration wrote.
        """
        self._fire_copy_taken_before = copy.copy(store.get("acg-1"))
        resolving = entry.connector.resolve_room

        async def _resolve_then_interleave(name):
            room = await resolving(name)
            mutate(store)
            return room

        entry.connector.resolve_room = _resolve_then_interleave
        return await migrate(store, [entry])

    async def test_a_fire_completing_mid_resolution_does_not_erase_the_room_id(self):
        self._write_file(1, [self._job()])
        store = self._store()
        entry = _entry(resolves={"general": _room("room-1")})

        def _a_fire_lands(s):
            # The REAL fire's semantics: a copy taken before the await, written
            # back through the fields it owns. Re-reading `s.get(...)` here — as
            # this helper used to — cannot hold a stale copy and so cannot
            # express the interleaving at all (found by review).
            fired = copy.copy(self._fire_copy_taken_before)
            fired.run_count = 1
            fired.last_run = "2026-09-01T09:00:00+00:00"
            s.write_fields(fired, FIRE_OWNED_FIELDS)

        report = await self._migrate_with_a_fire_in_the_middle(
            store, entry, _a_fire_lands)

        self.assertEqual(report.changed, 1)
        self.assertEqual(store.get("acg-1").room_id, "room-1",
                         "the report said it was written")
        self.assertEqual(
            json.loads(self.path.read_text())["jobs"][0]["room_id"], "room-1")
        # And the fire's own progress survived — the migration did not roll it back.
        self.assertEqual(store.get("acg-1").run_count, 1)

    async def test_a_completed_job_is_not_resurrected(self):
        self._write_file(1, [self._job(times=5, run_count=4)])
        store = self._store()
        entry = _entry(resolves={"general": _room("room-1")})

        def _the_last_fire_completes(s):
            done = copy.copy(self._fire_copy_taken_before)
            done.run_count = 5
            done.status = JobStatus.COMPLETED
            done.next_run = None
            s.write_fields(done, FIRE_OWNED_FIELDS)

        await self._migrate_with_a_fire_in_the_middle(
            store, entry, _the_last_fire_completes)

        job = store.get("acg-1")
        self.assertEqual(job.status, JobStatus.COMPLETED, "brought back to life")
        self.assertIsNone(job.next_run, "would fire again, in the past")
        self.assertEqual(job.run_count, 5)

    async def test_a_deletion_mid_run_costs_only_that_job(self):
        """`update` raised `KeyError`, which the control handler turned into a
        bare error — losing the report for every job already migrated."""
        self._write_file(1, [self._job("acg-1"), self._job("acg-2", "rc:dev")])
        store = self._store()
        entry = _entry(resolves={"general": _room("room-1"),
                                 "dev": _room("room-dev")})

        def _delete_the_first(s):
            s.remove("acg-1")

        report = await self._migrate_with_a_fire_in_the_middle(
            store, entry, _delete_the_first)

        self.assertIsNone(store.get("acg-1"), "stays deleted, not resurrected")
        self.assertEqual(store.get("acg-2").room_id, "room-dev",
                         "the other job was still migrated")
        # Reported, but NOT attention-worthy: the job is gone, so there is
        # nothing to fix and nothing to hold the schema version back for.
        deleted = [o for o in report.outcomes if "deleted while" in o.detail]
        self.assertEqual(len(deleted), 1, report.outcomes)
        self.assertFalse(deleted[0].needs_attention)
        self.assertEqual(report.unresolved, [])


class TestFinalIsSaidDifferentlyFromRetryable(_MigrateCase):
    """The operator's next move depends on which it was: delete the job, or run
    the command again. `Connector.room_ref_by_id`'s docstring makes the same
    distinction load-bearing, and this module used to collapse both into "the
    connector could not resolve X" (review).

    It matters more now that the schema version is not stamped while anything
    needs attention: a permanently unresolvable job pins the file at version 1,
    so the startup warning never clears until it is dealt with.
    """

    def _entry_raising(self, exc):
        entry = _entry()
        entry.connector.resolve_room = AsyncMock(side_effect=exc)
        return entry

    async def test_a_room_that_does_not_exist_says_delete_the_job(self):
        from gateway.connectors.rocketchat.rest import RoomNotFoundError

        self._write_file(1, [self._job()])
        store = self._store()

        report = await migrate(store, [self._entry_raising(RoomNotFoundError("no"))])

        detail = report.unresolved[0].detail
        self.assertIn("cannot be migrated", detail)
        # The exact advice, not the substring "again" — that also appears inside
        # "recreate it AGAINst a current watcher", which is how a loose
        # assertion passes for the wrong reason.
        self.assertNotIn("schedule migrate' again", detail)

    async def test_mattermosts_own_class_is_recognised_too(self):
        """There are TWO unrelated `RoomNotFoundError` classes. Importing either
        would have treated the other platform's final answer as retryable."""
        from gateway.connectors.mattermost.rest import RoomNotFoundError

        self._write_file(1, [self._job()])
        store = self._store()

        report = await migrate(store, [self._entry_raising(RoomNotFoundError("no"))])

        self.assertIn("cannot be migrated", report.unresolved[0].detail)

    async def test_a_transport_failure_says_run_it_again(self):
        self._write_file(1, [self._job()])
        store = self._store()

        report = await migrate(store, [self._entry_raising(OSError("network"))])

        detail = report.unresolved[0].detail
        self.assertIn("run 'schedule migrate' again", detail)
        self.assertNotIn("cannot be migrated", detail)

    async def test_either_way_the_version_stays_put(self):
        self._write_file(1, [self._job()])
        store = self._store()

        await migrate(store, [self._entry_raising(OSError("network"))])

        self.assertEqual(json.loads(self.path.read_text())["version"], 1)
