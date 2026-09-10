"""Boot reconciles every persisted record against the current rules (#143).

Seam: a real `SessionManager` (`make_manager`) booted through `sync_only`,
observed through `dispatch_command` and the saved state — what an operator
sees after `coop restart` followed by `list`.
"""

from tests.helpers import (
    ENG_ROOM as ROOM,
)
from tests.helpers import (
    IsolatedTestCase,
    MockAgentBackend,
    install_record,
    make_core_config,
    make_manager,
    make_record_from_rule,
    make_rule,
    make_rule_derived_record,
    patch_persisted,
)


# Local on purpose: these three compose the shared builders for this suite's
# one seam (boot a real manager on given records and rules, read it back through
# `list` and the log). Nothing outside this file boots a manager this way; the
# reload suite (#144) should lift `_booted` into helpers when it needs it too.
def _booted(records, rules, agents=("a", "b"), config=None):
    """A manager whose state store hands back `records` and whose config holds
    `rules`; call `sync_only()` on it to boot."""
    mgr = make_manager(agents={n: MockAgentBackend() for n in agents},
                       watcher_rules=list(rules), config=config)
    return mgr, patch_persisted(records)


async def _listed(mgr, room_id):
    reply = await mgr.dispatch_command({"cmd": "list"})
    assert reply["ok"], reply
    return next(row for row in reply["data"] if row["room_id"] == room_id)


class TestRuleEditsReachExistingRecords(IsolatedTestCase):

    async def test_a_changed_rule_re_materializes_its_record_and_keeps_the_session(self):
        before = make_rule(room="eng-backend", name="eng", agent="a")
        record = make_record_from_rule(before, ROOM, session_id="sess-keep",
                                       dropped_at="2026-09-01T01:00:00-07:00")
        after = make_rule(room="eng-backend", name="eng", agent="b")
        mgr, loaded = _booted([record], [after])

        with loaded:
            await mgr.sync_only()

        row = await _listed(mgr, "eng-backend")
        self.assertEqual(row["agent_name"], "b", "the rule's new agent reached the record")
        self.assertEqual(row["session_id"], "sess-keep", "same room, same session")


class TestRecordsNoRuleCoversAreExpired(IsolatedTestCase):

    async def test_a_record_no_rule_matches_is_expired_and_its_session_id_logged(self):
        rule = make_rule(room="eng-backend", name="eng", agent="a")
        record = make_record_from_rule(rule, ROOM, session_id="sess-gone-9f3a",
                                       dropped_at="2026-09-01T01:00:00-07:00")
        mgr, loaded = _booted([record], [])  # the rule was deleted

        with loaded, self.assertLogs("coop.core", level="INFO") as logs:
            await mgr.sync_only()

        reply = await mgr.dispatch_command({"cmd": "list"})
        self.assertEqual([r["room_id"] for r in reply["data"]], [],
                         "nothing in config describes the room any more")
        audit = [line for line in logs.output if "AUDIT" in line and "sess-gone-9f3a" in line]
        self.assertEqual(len(audit), 1, logs.output)
        self.assertIn("no-rule-matches", audit[0])


def _audit_lines(logs, session_id):
    return [line for line in logs.output if "AUDIT: session released" in line and session_id in line]


class TestEveryReleasedSessionIsLoggedOnce(IsolatedTestCase):
    """The universal rule: whichever path lets go of a session, one AUDIT line
    with the full id. One test per path."""

    async def _dormant(self, session_id):
        """A manager holding one idle record — installed directly, not booted:
        the script connector is eager and a boot would start the room's
        watcher, and a resident record is exactly what expiry refuses."""
        rule = make_rule(room="eng-backend", name="eng", agent="a")
        record = make_record_from_rule(rule, ROOM, session_id=session_id,
                                       dropped_at="2026-09-01T01:00:00-07:00")
        mgr, _ = _booted([], [rule])
        install_record(mgr._lifecycle, record)
        return mgr, record

    async def test_reset(self):
        mgr, record = await self._dormant("sess-reset-1111")
        with self.assertLogs("coop.core", level="INFO") as logs:
            reply = await mgr.dispatch_command({"cmd": "reset", "watcher_name": record.watcher_name})
        self.assertTrue(reply["ok"], reply)
        lines = _audit_lines(logs, "sess-reset-1111")
        self.assertEqual(len(lines), 1, logs.output)
        self.assertIn("reset by operator", lines[0])

    async def test_reclamation_after_the_bot_is_removed_from_the_room(self):
        """The membership-removal path (the operator's `expire` verb runs the
        same `reclaim_room`, and is refused on the eager script connector)."""
        mgr, record = await self._dormant("sess-removed-2222")
        with self.assertLogs("coop.core", level="INFO") as logs:
            await mgr._on_membership_removed(record.room_id)
        lines = _audit_lines(logs, "sess-removed-2222")
        self.assertEqual(len(lines), 1, logs.output)
        self.assertIn("removed from the room", lines[0])

    async def test_idle_expiry_by_the_sweep(self):
        from datetime import datetime, timedelta
        mgr, record = await self._dormant("sess-idle-3333")
        far = datetime.fromisoformat("2026-09-01T01:00:00-07:00") + timedelta(days=400)
        with self.assertLogs("coop.core", level="INFO") as logs:
            expired = await mgr._lifecycle.expire_idle(record.watcher_name, now=far)
        self.assertTrue(expired)
        lines = _audit_lines(logs, "sess-idle-3333")
        self.assertEqual(len(lines), 1, logs.output)
        self.assertIn("session_expire_days", lines[0])

    async def test_static_era_prune_at_boot(self):
        static = make_rule_derived_record(name="old-static", room_id="r-static",
                                          connector="default", session_id="sess-static-4444",
                                          rule_name="", rule={}, config={})
        mgr, loaded = _booted([static], [])
        with loaded, self.assertLogs("coop.core", level="INFO") as logs:
            await mgr.sync_only()
        lines = _audit_lines(logs, "sess-static-4444")
        self.assertEqual(len(lines), 1, logs.output)
        self.assertIn("static-era", lines[0])


class TestReconciliationEdges(IsolatedTestCase):

    async def test_a_rule_inserted_above_takes_the_room_over(self):
        """Ownership drift: no rule's content changed, but first-match now
        picks a different rule for the room."""
        eng = make_rule(room="eng-backend", name="eng", agent="a")
        record = make_record_from_rule(eng, ROOM, session_id="sess-own",
                                       dropped_at="2026-09-01T01:00:00-07:00")
        above = make_rule(room="eng-*", name="all-eng", agent="b")
        mgr, loaded = _booted([record], [above, eng])

        with loaded, self.assertLogs("coop.core", level="INFO") as logs:
            await mgr.sync_only()

        row = await _listed(mgr, "eng-backend")
        self.assertEqual(row["agent_name"], "b")
        self.assertEqual(row["session_id"], "sess-own")
        self.assertTrue(any("from rule 'eng' to rule 'all-eng'" in line for line in logs.output),
                        logs.output)

    async def test_an_unchanged_fleet_is_a_no_op_and_says_so(self):
        eng = make_rule(room="eng-backend", name="eng", agent="a")
        record = make_record_from_rule(eng, ROOM, session_id="sess-same",
                                       dropped_at="2026-09-01T01:00:00-07:00")
        mgr, loaded = _booted([record], [eng])

        with loaded, self.assertLogs("coop.core.session_manager", level="INFO") as logs:
            await mgr.sync_only()

        summary = [line for line in logs.output if "Reconciliation" in line]
        self.assertEqual(len(summary), 1, logs.output)
        self.assertIn("1 kept, 0 re-materialized, 0 expired — nothing to change", summary[0])

    async def test_a_paused_record_stays_paused_through_re_materialization(self):
        before = make_rule(room="eng-backend", name="eng", agent="a")
        record = make_record_from_rule(before, ROOM, session_id="sess-paused", paused=True)
        after = make_rule(room="eng-backend", name="eng", agent="b")
        mgr, loaded = _booted([record], [after])

        with loaded:
            await mgr.sync_only()

        row = await _listed(mgr, "eng-backend")
        self.assertEqual(row["agent_name"], "b")
        self.assertEqual(row["state"], "paused")
        self.assertEqual(row["session_id"], "sess-paused")

    async def test_a_damaged_rule_snapshot_is_rewritten_not_fatal(self):
        eng = make_rule(room="eng-backend", name="eng", agent="a")
        record = make_record_from_rule(eng, ROOM, session_id="sess-dmg",
                                       dropped_at="2026-09-01T01:00:00-07:00")
        record.rule = "not a dict"
        mgr, loaded = _booted([record], [eng])

        with loaded, self.assertLogs("coop.core.session_manager", level="INFO") as logs:
            await mgr.sync_only()

        self.assertTrue(any("re-materialized from rule 'eng' to rule 'eng'" in line
                            for line in logs.output), logs.output)
        self.assertIsInstance(mgr._lifecycle.record_for_room("eng-backend").rule, dict)


class TestSessionsAcrossReMaterialization(IsolatedTestCase):

    async def test_a_same_agent_move_keeps_the_session(self):
        eng = make_rule(room="eng-backend", name="eng", agent="a")
        record = make_record_from_rule(eng, ROOM, session_id="sess-same-agent",
                                       dropped_at="2026-09-01T01:00:00-07:00")
        renamed = make_rule(room="eng-backend", name="eng-v2", agent="a")
        mgr, loaded = _booted([record], [renamed])

        with loaded:
            await mgr.sync_only()

        row = await _listed(mgr, "eng-backend")
        self.assertEqual(row["session_id"], "sess-same-agent")
        self.assertEqual(mgr._lifecycle.record_for_room("eng-backend").rule_name, "eng-v2")

    async def test_a_move_to_a_different_backend_starts_fresh_and_logs_the_old_id(self):
        """The engine rewrites `agent`; the next provisioning's backend-identity
        check decides the session — and says so with the full id."""
        from gateway.config import AgentConfig

        eng = make_rule(room="eng-backend", name="eng", agent="a")
        record = make_record_from_rule(eng, ROOM, session_id="sess-old-backend-42")
        moved = make_rule(room="eng-backend", name="eng", agent="b")
        config = make_core_config(agents={
            "a": AgentConfig(), "b": AgentConfig(working_directory="/elsewhere")})
        mgr, loaded = _booted([record], [moved], config=config)

        with loaded, self.assertLogs("coop.core", level="INFO") as logs:
            await mgr.sync_only()  # the eager loop starts the room under agent b

        row = await _listed(mgr, "eng-backend")
        self.assertEqual(row["agent_name"], "b")
        self.assertNotEqual(row["session_id"], "sess-old-backend-42")
        self.assertTrue(any("not reusing session=sess-old-backend-42" in line
                            for line in logs.output), logs.output)
        lines = _audit_lines(logs, "sess-old-backend-42")
        self.assertEqual(len(lines), 1, "an abandoned id is a released id")
        self.assertIn("abandoned at provisioning", lines[0])

    async def test_a_second_boot_after_a_change_is_a_no_op(self):
        before = make_rule(room="eng-backend", name="eng", agent="a")
        record = make_record_from_rule(before, ROOM, session_id="sess-twice",
                                       dropped_at="2026-09-01T01:00:00-07:00")
        after = make_rule(room="eng-backend", name="eng", agent="b")
        mgr, loaded = _booted([record], [after])
        with loaded:
            await mgr.sync_only()
        rewritten = mgr._lifecycle.record_for_room("eng-backend")

        mgr2, loaded2 = _booted([rewritten], [after])
        with loaded2, self.assertLogs("coop.core.session_manager", level="INFO") as logs:
            await mgr2.sync_only()

        self.assertTrue(any("nothing to change" in line for line in logs.output), logs.output)

    async def test_an_uncovered_paused_record_is_expired_with_the_override_audited(self):
        eng = make_rule(room="eng-backend", name="eng", agent="a")
        record = make_record_from_rule(eng, ROOM, session_id="sess-paused-gone", paused=True)
        mgr, loaded = _booted([record], [])

        with loaded, self.assertLogs("coop.core", level="INFO") as logs:
            await mgr.sync_only()

        reply = await mgr.dispatch_command({"cmd": "list"})
        self.assertEqual(reply["data"], [])
        self.assertTrue(any("pause is being overridden" in line for line in logs.output))
        self.assertEqual(len(_audit_lines(logs, "sess-paused-gone")), 1)

    async def test_a_record_with_no_room_name_is_kept_and_said_so(self):
        """The matcher does not fall back to the opaque id, and "nothing
        matches" is destructive here — so a nameless record is left alone."""
        eng = make_rule(room="eng-backend", name="eng", agent="a")
        record = make_record_from_rule(eng, ROOM, session_id="sess-nameless",
                                       dropped_at="2026-09-01T01:00:00-07:00")
        record.room_name = ""
        mgr, loaded = _booted([record], [])

        with loaded, self.assertLogs("coop.core.session_manager", level="WARNING") as logs:
            await mgr.sync_only()

        row = await _listed(mgr, "eng-backend")
        self.assertEqual(row["session_id"], "sess-nameless")
        self.assertTrue(any("no room name recorded" in line for line in logs.output), logs.output)


class TestAnExpiryThatDidNotApplyIsLoud(IsolatedTestCase):

    async def test_a_failed_reclamation_is_reported_as_still_running(self):
        """The shared tail swallows a failed reclamation because its other
        callers are re-discovered later; nothing re-discovers this one before
        the next boot, so the record still being installed is an ERROR."""
        from unittest.mock import AsyncMock

        eng = make_rule(room="eng-backend", name="eng", agent="a")
        record = make_record_from_rule(eng, ROOM, session_id="sess-stuck",
                                       dropped_at="2026-09-01T01:00:00-07:00")
        mgr, loaded = _booted([record], [])
        mgr._lifecycle.reclaim_room = AsyncMock(side_effect=OSError("disk full"))

        with loaded, self.assertLogs("coop.core.session_manager", level="ERROR") as logs:
            await mgr.sync_only()

        row = await _listed(mgr, "eng-backend")
        self.assertEqual(row["session_id"], "sess-stuck", "still installed, honestly")
        self.assertTrue(any("could NOT be expired" in line for line in logs.output), logs.output)


class TestAuditFollowsTheDurableStep(IsolatedTestCase):
    """A release is announced only once it has actually happened: after the
    prune is saved, after the new record is committed, with the job
    cancellation told the real reason."""

    async def test_a_static_prune_whose_save_fails_announces_nothing(self):
        from unittest.mock import MagicMock

        static = make_rule_derived_record(name="old-static", room_id="r-static",
                                          connector="default", session_id="sess-static-fail",
                                          rule_name="", rule={}, config={})
        mgr, loaded = _booted([static], [])
        mgr._lifecycle._state_store.save = MagicMock(side_effect=OSError("disk full"))

        with loaded, self.assertLogs("coop.core", level="INFO") as logs:
            with self.assertRaises(OSError):
                await mgr.sync_only()

        self.assertEqual(_audit_lines(logs, "sess-static-fail"), [],
                         "the record is still on disk; the next boot prunes it again")

    async def test_a_failed_start_after_an_identity_change_keeps_the_old_id_unannounced(self):
        from unittest.mock import AsyncMock

        from gateway.config import AgentConfig

        eng = make_rule(room="eng-backend", name="eng", agent="a")
        record = make_record_from_rule(eng, ROOM, session_id="sess-kept-on-failure")
        moved = make_rule(room="eng-backend", name="eng", agent="b")
        config = make_core_config(agents={
            "a": AgentConfig(), "b": AgentConfig(working_directory="/elsewhere")})
        mgr, loaded = _booted([record], [moved], config=config)
        mgr._lifecycle._agents["b"].create_session = AsyncMock(side_effect=RuntimeError("backend down"))

        with loaded, self.assertLogs("coop.core", level="INFO") as logs:
            await mgr.sync_only()  # the eager start fails; boot survives it

        self.assertEqual(_audit_lines(logs, "sess-kept-on-failure"), [],
                         "the prior record was rolled back with its id — nothing was released")
        row = await _listed(mgr, "eng-backend")
        self.assertEqual(row["session_id"], "sess-kept-on-failure")

    async def test_jobs_of_a_reconciled_away_room_are_cancelled_for_the_right_reason(self):
        from unittest.mock import MagicMock

        eng = make_rule(room="eng-backend", name="eng", agent="a")
        record = make_record_from_rule(eng, ROOM, session_id="sess-jobs",
                                       dropped_at="2026-09-01T01:00:00-07:00")
        mgr, loaded = _booted([record], [])
        mgr._cancel_jobs = MagicMock()

        with loaded:
            await mgr.sync_only()

        mgr._cancel_jobs.assert_called_once()
        self.assertEqual(mgr._cancel_jobs.call_args.args[0], "eng-backend")
        self.assertIn("reconciliation", mgr._cancel_jobs.call_args.kwargs["reason"])


class TestSessionlessRecordsReleaseNothing(IsolatedTestCase):

    async def test_an_idle_record_that_never_had_a_session_is_expired_without_an_audit(self):
        """A membership-add registers a record with no session; if its rule goes
        before the room ever speaks, there is nothing to release — and saying
        there was would be a false event."""
        eng = make_rule(room="eng-backend", name="eng", agent="a")
        record = make_record_from_rule(eng, ROOM, session_id="",
                                       dropped_at="2026-09-01T01:00:00-07:00")
        mgr, loaded = _booted([record], [])

        with loaded, self.assertLogs("coop.core", level="INFO") as logs:
            await mgr.sync_only()

        reply = await mgr.dispatch_command({"cmd": "list"})
        self.assertEqual(reply["data"], [], "expired all the same")
        self.assertFalse(any("AUDIT: session released" in line for line in logs.output), logs.output)


class TestRecordsThatCannotBeReMatchedHonestly(IsolatedTestCase):

    async def test_an_unknown_room_kind_is_kept_not_judged_as_a_channel(self):
        """The runtime degrades an unknown kind to CHANNEL; a match that can
        expire the record must not — a garbled DM is not a channel."""
        eng = make_rule(room="eng-backend", name="eng", agent="a")
        record = make_record_from_rule(eng, ROOM, session_id="sess-odd-kind",
                                       dropped_at="2026-09-01T01:00:00-07:00")
        record.room_kind = "channel_typo"
        mgr, loaded = _booted([record], [])  # nothing would match a channel either

        with loaded, self.assertLogs("coop.core.session_manager", level="WARNING") as logs:
            await mgr.sync_only()

        row = await _listed(mgr, "eng-backend")
        self.assertEqual(row["session_id"], "sess-odd-kind")
        self.assertTrue(any("channel_typo" in line and "kept" in line for line in logs.output), logs.output)

    async def test_re_materialization_writes_the_current_connector(self):
        """A state file copied under a renamed connector keeps the old name in
        every record's column, and `config_from_record` prefers the column."""
        eng = make_rule(room="eng-backend", name="eng", agent="a")
        record = make_record_from_rule(eng, ROOM, session_id="sess-moved",
                                       dropped_at="2026-09-01T01:00:00-07:00",
                                       connector="old-name")
        renamed = make_rule(room="eng-backend", name="eng-v2", agent="a")
        mgr, loaded = _booted([record], [renamed])

        with loaded:
            await mgr.sync_only()

        row = await _listed(mgr, "eng-backend")
        self.assertEqual(row["connector"], "default")
        self.assertEqual(mgr._lifecycle.record_for_room("eng-backend").connector, "default")

    async def test_a_record_with_no_room_kind_is_kept_too(self):
        eng = make_rule(room="eng-backend", name="eng", agent="a")
        record = make_record_from_rule(eng, ROOM, session_id="sess-no-kind",
                                       dropped_at="2026-09-01T01:00:00-07:00")
        record.room_kind = ""
        mgr, loaded = _booted([record], [])

        with loaded, self.assertLogs("coop.core.session_manager", level="WARNING") as logs:
            await mgr.sync_only()

        self.assertEqual((await _listed(mgr, "eng-backend"))["session_id"], "sess-no-kind")
        self.assertTrue(any("no room kind recorded" in line for line in logs.output), logs.output)

    async def test_a_record_from_an_older_config_schema_is_rewritten(self):
        eng = make_rule(room="eng-backend", name="eng", agent="a")
        record = make_record_from_rule(eng, ROOM, session_id="sess-old-schema",
                                       dropped_at="2026-09-01T01:00:00-07:00")
        record.config_schema_version = 0  # written by a build with an older materialization
        mgr, loaded = _booted([record], [eng])  # the rule itself is unchanged

        with loaded, self.assertLogs("coop.core.session_manager", level="INFO") as logs:
            await mgr.sync_only()

        from gateway.core.state import CONFIG_SCHEMA_VERSION
        self.assertEqual(mgr._lifecycle.record_for_room("eng-backend").config_schema_version,
                         CONFIG_SCHEMA_VERSION)
        self.assertTrue(any("re-materialized from rule 'eng' to rule 'eng'" in line
                            for line in logs.output), logs.output)


class TestAbandonedIdsAreAuditedOnEveryPathThatReplacesTheRecord(IsolatedTestCase):

    async def test_a_subscription_failure_that_keeps_the_new_record_still_audits_the_old_id(self):
        """This failure path deliberately installs the NEW record (session and
        context flag preserved for the next start), so the old id is gone from
        the index even though the start failed."""
        from unittest.mock import AsyncMock

        from gateway.config import AgentConfig

        eng = make_rule(room="eng-backend", name="eng", agent="a")
        record = make_record_from_rule(eng, ROOM, session_id="sess-old-on-subfail")
        moved = make_rule(room="eng-backend", name="eng", agent="b")
        config = make_core_config(agents={
            "a": AgentConfig(), "b": AgentConfig(working_directory="/elsewhere")})
        mgr, loaded = _booted([record], [moved], config=config)
        mgr._connector.subscribe_room = AsyncMock(side_effect=RuntimeError("subscribe down"))

        with loaded, self.assertLogs("coop.core", level="INFO") as logs:
            await mgr.sync_only()  # the eager start fails at subscription; boot survives

        lines = _audit_lines(logs, "sess-old-on-subfail")
        self.assertEqual(len(lines), 1, logs.output)
        self.assertNotEqual(mgr._lifecycle.record_for_room("eng-backend").session_id,
                            "sess-old-on-subfail", "the new record is what remains")
