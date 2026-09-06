"""The reload plan: record-level planning, the offline (next-boot) plan, and
its two renderings (#144).

Pure except `boot_plan`, which reads state files under an isolated runtime dir.
"""

from __future__ import annotations

import json
import unittest

from gateway.config_diff import EntityChanges
from gateway.core.state import save_state
from gateway.core.watcher_manager import RoomRef
from gateway.core.watcher_rule import RoomKind
from gateway.reload_plan import (
    Degraded,
    ReloadPlan,
    WatcherChange,
    boot_plan,
    connector_removed_changes,
    plan_connector_records,
)
from tests.helpers import (
    isolate_runtime_dir,
    make_record_from_rule,
    make_rule,
    write_gateway_config,
)

ENG = RoomRef(id="r-eng", kind=RoomKind.CHANNEL, name="eng")
OPS = RoomRef(id="r-ops", kind=RoomKind.CHANNEL, name="ops")


class TestPlanConnectorRecords(unittest.TestCase):

    def setUp(self):
        self.eng = make_rule(room="eng", name="eng", connector="rc", agent="a")
        self.ops = make_rule(room="ops", name="ops", connector="rc", agent="b")
        self.rec_eng = make_record_from_rule(self.eng, ENG, session_id="sess-eng")
        self.rec_ops = make_record_from_rule(self.ops, OPS, session_id="sess-ops")

    def test_unchanged_rules_and_nothing_restarting_plan_nothing(self):
        out = plan_connector_records("rc", [self.rec_eng, self.rec_ops], [self.eng, self.ops])
        self.assertEqual(out, [])

    def test_a_removed_rule_expires_its_record_with_the_session_id(self):
        out = plan_connector_records("rc", [self.rec_eng, self.rec_ops], [self.eng])
        self.assertEqual([(w.action, w.handle, w.reason, w.session_id) for w in out],
                         [("expire", self.rec_ops.watcher_name, "no-rule-matches", "sess-ops")])

    def test_a_changed_rule_rematerializes_from_and_to(self):
        eng2 = make_rule(room="eng", name="eng", connector="rc", agent="b")
        out = plan_connector_records("rc", [self.rec_eng], [eng2])
        self.assertEqual([(w.action, w.from_rule, w.to_rule) for w in out],
                         [("rematerialize", "eng", "eng")])

    def test_a_connector_restart_restarts_only_resident_records(self):
        out = plan_connector_records(
            "rc", [self.rec_eng, self.rec_ops], [self.eng, self.ops],
            resident={self.rec_eng.room_id}, restart_all=True)
        self.assertEqual([(w.action, w.handle, w.reason) for w in out],
                         [("restart", self.rec_eng.watcher_name, "connector restarts")])

    def test_an_agent_restart_restarts_resident_records_on_that_agent(self):
        out = plan_connector_records(
            "rc", [self.rec_eng, self.rec_ops], [self.eng, self.ops],
            resident={self.rec_eng.room_id, self.rec_ops.room_id},
            restarted_agents={"b"})
        self.assertEqual([(w.action, w.handle, w.reason) for w in out],
                         [("restart", self.rec_ops.watcher_name, "agent 'b' restarts")])

    def test_a_rematerialized_resident_record_is_listed_once(self):
        eng2 = make_rule(room="eng", name="eng", connector="rc", agent="b")
        out = plan_connector_records("rc", [self.rec_eng], [eng2],
                                     resident={self.rec_eng.room_id}, restart_all=True)
        self.assertEqual([w.action for w in out], ["rematerialize"])

    def test_connector_removed_expires_every_record(self):
        out = connector_removed_changes("rc", [self.rec_eng, self.rec_ops])
        self.assertEqual({(w.action, w.reason) for w in out}, {("expire", "connector-removed")})
        self.assertEqual({w.session_id for w in out}, {"sess-eng", "sess-ops"})


class TestBootPlan(unittest.TestCase):

    def setUp(self):
        self.tmp, self.runtime = isolate_runtime_dir(self)

    def test_the_offline_plan_reads_state_files_and_orphans(self):
        config = write_gateway_config(self.tmp)  # one script connector, rule w1 on room "script"
        rule = config.watcher_rules[0]
        kept = make_record_from_rule(
            rule, RoomRef(id="r1", kind=RoomKind.CHANNEL, name="script"), session_id="s-kept")
        gone = make_record_from_rule(
            make_rule(room="other", name="w2", connector="script", agent="default"),
            RoomRef(id="r2", kind=RoomKind.CHANNEL, name="other"), session_id="s-gone")
        save_state("script", [kept, gone])
        ghost = make_record_from_rule(
            make_rule(room="x", name="g", connector="ghost", agent="default"),
            RoomRef(id="r3", kind=RoomKind.CHANNEL, name="x"), session_id="s-ghost")
        save_state("ghost", [ghost])

        plan = boot_plan(config)

        self.assertTrue(plan.offline and plan.dry_run and plan.ok)
        self.assertEqual(plan.connectors.removed, ["ghost"])
        self.assertEqual(sorted((w.action, w.reason, w.session_id) for w in plan.watchers), [
            ("expire", "connector-removed", "s-ghost"),
            ("expire", "no-rule-matches", "s-gone"),
        ])
        self.assertEqual(plan.of("restart"), [], "residency is unknown offline")
        self.assertEqual(len(plan.digest), 64)

    def test_an_orphan_file_the_sweep_keeps_is_not_planned_as_removed(self):
        import dataclasses
        import json

        from gateway.core.state import STATE_FORMAT_VERSION
        config = write_gateway_config(self.tmp)
        good = make_record_from_rule(
            make_rule(room="x", name="g", connector="ghost", agent="default"),
            RoomRef(id="r3", kind=RoomKind.CHANNEL, name="x"), session_id="s-ghost")
        bad = dict(dataclasses.asdict(good), room_id="r4", paused="yes please")  # unparseable
        (self.runtime / "state.ghost.json").write_text(json.dumps({
            "version": STATE_FORMAT_VERSION,
            "watchers": [dataclasses.asdict(good), bad]}))

        plan = boot_plan(config)

        self.assertEqual(plan.connectors.removed, [], "boot keeps that file")
        self.assertEqual(plan.of("expire"), [], "so nothing in it is released")
        self.assertTrue(any("KEPT" in n and "state.ghost.json" in n for n in plan.notes), plan.notes)

    def test_a_rematerialized_record_reports_its_destination_agent(self):
        eng_a = make_rule(room="eng", name="eng", connector="rc", agent="a")
        eng_b = make_rule(room="eng", name="eng", connector="rc", agent="b")
        record = make_record_from_rule(eng_a, ENG)
        out = plan_connector_records("rc", [record], [eng_b])
        self.assertEqual([(w.action, w.agent) for w in out], [("rematerialize", "b")])
        gone = plan_connector_records("rc", [record], [])
        self.assertEqual([(w.action, w.agent) for w in gone], [("expire", "a")])

    def test_static_era_records_are_listed_as_the_prune_boot_runs(self):
        from gateway.core.state import WatcherState
        config = write_gateway_config(self.tmp)
        save_state("script", [WatcherState(watcher_name="legacy", session_id="s-legacy",
                                           room_id="r-legacy")])
        plan = boot_plan(config)
        self.assertEqual([(w.action, w.reason, w.session_id) for w in plan.watchers],
                         [("expire", "static-era record pruned at boot", "s-legacy")])

    def test_no_state_files_is_no_changes(self):
        plan = boot_plan(write_gateway_config(self.tmp))
        self.assertFalse(plan.has_changes)
        self.assertIn("No changes", plan.render())


class TestRendering(unittest.TestCase):

    def _plan(self, **kw) -> ReloadPlan:
        plan = ReloadPlan(dry_run=kw.pop("dry_run", True), **kw)
        return plan

    def test_json_round_trips_through_from_dict(self):
        plan = self._plan(
            connectors=EntityChanges(changed=["rc"]),
            watchers=[WatcherChange("rc", "r1", "rc:eng", "a", "expire",
                                    from_rule="eng", session_id="s-1", reason="no-rule-matches")],
            degraded=[Degraded("connector", "rc", "boom")],
            notes=["n"], digest="d" * 64,
        )
        doc = json.loads(json.dumps(plan.to_dict()))
        back = ReloadPlan.from_dict(doc)
        self.assertEqual(back.to_dict(), plan.to_dict())
        self.assertEqual(doc["exit_code"], 2)
        self.assertEqual(doc["watchers"][0]["session_id"], "s-1")

    def test_exit_codes(self):
        self.assertEqual(self._plan().exit_code, 0)
        self.assertEqual(self._plan(degraded=[Degraded("agent", "a", "x")]).exit_code, 2)
        self.assertEqual(ReloadPlan.refused("nope", dry_run=False).exit_code, 1)

    def test_render_shows_the_full_session_id_on_an_expiry(self):
        plan = self._plan(watchers=[WatcherChange(
            "rc", "r1", "rc:eng", "a", "expire", session_id="sess-" + "7" * 30,
            reason="no-rule-matches")])
        text = plan.render()
        self.assertIn("sess-" + "7" * 30, text)
        self.assertIn("expire no-rule-matches", text)
        self.assertIn("Dry run", text)

    def test_render_of_a_refusal_marks_errors_apart_from_warnings(self):
        plan = ReloadPlan.refused("config.yaml: 1 error(s)", dry_run=False,
                                  findings=[{"level": "error", "message": "bad thing"},
                                            {"level": "warning", "message": "shadowed rule"}])
        text = plan.render()
        self.assertIn("[ERROR] config.yaml: 1 error(s)", text)
        self.assertIn("  [ERROR] bad thing", text)
        self.assertIn("  [WARNING] shadowed rule", text)

    def test_render_of_an_apply_with_degraded_says_so(self):
        plan = self._plan(dry_run=False, applied=True,
                          connectors=EntityChanges(changed=["rc"]),
                          degraded=[Degraded("connector", "rc", "refused")])
        text = plan.render()
        self.assertIn("connectors: ~ rc (restart)", text)
        self.assertIn("  [ERROR] connector 'rc': refused", text)
        self.assertIn("[ERROR] Applied with 1 degraded section", text)

    def test_the_closing_line_counts_connectors_and_agents_not_only_watchers(self):
        """A degraded connector that comes back has no resident watcher to list;
        '0 restart' alone read as 'nothing happened'."""
        plan = self._plan(dry_run=False, applied=True,
                          connectors=EntityChanges(changed=["mm"]))
        self.assertIn("Applied (1 connector(s) restarted; watchers: 0 restart", plan.render())

    def test_offline_render_marks_restarts_as_not_applicable(self):
        plan = self._plan(offline=True, connectors=EntityChanges(removed=["ghost"]))
        text = plan.render()
        self.assertIn("connectors: - ghost (removed)", text)
        self.assertIn("do not apply", text)
        self.assertIn("next start", text)

    def test_offline_render_says_so_even_with_record_changes_only(self):
        plan = self._plan(offline=True, watchers=[WatcherChange(
            "rc", "r1", "rc:eng", "a", "expire", session_id="s", reason="no-rule-matches")])
        self.assertIn("do not apply", plan.render(), "the section is marked, not dropped")


if __name__ == "__main__":
    unittest.main()
