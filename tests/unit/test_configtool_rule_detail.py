"""Pilot-based tests for RuleDetailScreen and the Rules tab — the config
TUI's rewrite onto watcher rules (design §5.5, `impl/config-tooling`).

These replace the old test_configtool_watcher_detail.py suite wholesale: the
behaviours that suite pinned (merge-on-add, split-on-edit, rooms: groups,
rename-as-remove-plus-add) belonged to the static shape, which no longer
exists as data — a rule is one entry in one list, so its CRUD is the plain
trial-entry pattern connectors already use, plus the two things rules have
that nothing else does: load-bearing ORDER (first match wins → '['/']'
reorder on the list) and runtime strands (the delete warning's
stranded-session/orphaned-job counts).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml
from textual.widgets import Checkbox, DataTable, Input, Select, Static

from gateway.configtool.app import ConfigToolApp
from gateway.configtool.modals import ConfirmModal, MessageModal
from gateway.configtool.screens.overview import OverviewScreen
from gateway.configtool.screens.rule_detail import RuleDetailScreen


def _write_config(tmp_path: Path, yaml_text: str) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(yaml_text))
    return str(path)


@pytest.fixture
def work_dir(tmp_path: Path) -> Path:
    d = tmp_path / "work"
    d.mkdir()
    return d


def _config_with_rules(work_dir: Path) -> str:
    """Two rules under rc/default (order matters between them), plus a
    second connector+agent pairing for reassignment scenarios."""
    return f"""\
        connectors:
          - name: rc
            type: rocketchat
            server: {{url: "http://localhost:3000", username: bot, password: pw}}
          - name: rc2
            type: rocketchat
            server: {{url: "http://localhost:3001", username: bot2, password: pw2}}
        agents:
          default:
            type: claude
            working_directory: {work_dir}
          other:
            type: claude
            working_directory: {work_dir}
        watcher_rules:
          - name: eng-rooms
            connector: rc
            agent: default
            rooms:
              include: ["eng-*"]
          - name: catch-all
            connector: rc
            agent: default
            rooms:
              include: ["*"]
              except_for: ["*-noise"]
    """


def _config_with_no_rules(work_dir: Path) -> str:
    return f"""\
        connectors:
          - name: rc
            type: rocketchat
            server: {{url: "http://localhost:3000", username: bot, password: pw}}
        agents:
          default:
            type: claude
            working_directory: {work_dir}
        watcher_rules: []
    """


async def _open_rules_tab(pilot, app) -> None:
    app.screen.query_one("TabbedContent").active = "tab-rules"
    await pilot.pause()

async def _choose_connector_and_agent(pilot, app, connector: str, agent: str) -> None:
    """Pick both required Selects in a CREATE form.

    They compose blank now: neither has a loader default, so preselecting the
    first option would be the document-order binding the loader stopped making —
    and a preselection the diff cannot see is what let a rule save without an
    agent at all. Every create test therefore chooses, the way an operator does.
    """
    app.screen.query_one("#field-connector", Select).value = connector
    app.screen.query_one("#field-agent", Select).value = agent
    await pilot.pause()



async def _open_rule_row(pilot, app, row: int, key: str) -> None:
    """key: 'e' to edit directly, 'enter' to view first."""
    await _open_rules_tab(pilot, app)
    table = app.screen.query_one("#rules-table", DataTable)
    table.focus()
    table.move_cursor(row=row)
    await pilot.press(key)
    await pilot.pause()


class TestRulesTable:
    async def test_rules_render_in_document_order_never_sorted(self, tmp_path, work_dir):
        """Order IS the semantics (first match wins) — the one tab that
        must NOT sort by name, or the display lies about routing."""
        config_path = _write_config(tmp_path, _config_with_rules(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rules_tab(pilot, app)
            table = app.screen.query_one("#rules-table", DataTable)
            names = [table.get_row_at(i)[1] for i in range(table.row_count)]
            # 'catch-all' < 'eng-rooms' alphabetically — document order wins.
            assert names == ["eng-rooms", "catch-all"]

    async def test_a_broken_rule_shows_its_error_and_the_good_rule_still_renders(
        self, tmp_path, work_dir
    ):
        """The old Watchers tab displayed OK on broken rows (findings filed
        under a name/index spelling no row key matched) and dropped rule
        rows entirely, contradicting the banner. Every entry gets a row;
        the broken one's Status column carries the error."""
        config_path = _write_config(tmp_path, f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {work_dir}
            watcher_rules:
              - name: good-rule
                connector: rc
                agent: default
                rooms:
                  include: [general]
              - name: broken-rule
                connector: rc
                agent: default
                rooms:
                  include: []
        """)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rules_tab(pilot, app)
            table = app.screen.query_one("#rules-table", DataTable)
            assert table.row_count == 2
            rows = {table.get_row_at(i)[1]: str(table.get_row_at(i)[5]) for i in range(2)}
            assert "error" in rows["broken-rule"].lower()
            assert "error" not in rows["good-rule"].lower()

    async def test_the_rooms_column_summarizes_the_matcher(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_rules(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rules_tab(pilot, app)
            table = app.screen.query_one("#rules-table", DataTable)
            assert table.get_row_at(1)[4] == "* (except: *-noise)"


class TestRuleReorder:
    async def test_bracket_keys_swap_the_rule_and_persist_the_new_order(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_rules(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rules_tab(pilot, app)
            table = app.screen.query_one("#rules-table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("]")
            await pilot.pause()

            raw = yaml.safe_load(Path(config_path).read_text())
            assert [w["name"] for w in raw["watcher_rules"]] == ["catch-all", "eng-rooms"]
            # The cursor follows the moved rule to its new position.
            assert table.cursor_row == 1

            await pilot.press("[")
            await pilot.pause()
            raw = yaml.safe_load(Path(config_path).read_text())
            assert [w["name"] for w in raw["watcher_rules"]] == ["eng-rooms", "catch-all"]

    async def test_moving_past_the_edge_is_a_no_op(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_rules(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rules_tab(pilot, app)
            table = app.screen.query_one("#rules-table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("[")
            await pilot.pause()
            raw = yaml.safe_load(Path(config_path).read_text())
            assert [w["name"] for w in raw["watcher_rules"]] == ["eng-rooms", "catch-all"]


class TestRuleCreate:
    async def test_creating_a_rule_writes_name_connector_agent_and_matcher(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_no_rules(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rules_tab(pilot, app)
            await pilot.press("n")
            await pilot.pause()
            assert isinstance(app.screen, RuleDetailScreen)
            assert app.screen.mode == "create"

            app.screen.query_one("#field-name", Input).value = "my-rule"
            await _choose_connector_and_agent(pilot, app, "rc", "default")
            app.screen.query_one("#field-rooms-include", Input).value = "general, eng-*"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            (entry,) = raw["watcher_rules"]
            assert entry["name"] == "my-rule"
            # connector/agent are written EXPLICITLY even untouched — never
            # left to the loader's config-order-dependent fallback.
            assert entry["connector"] == "rc"
            assert entry["agent"] == "default"
            assert entry["rooms"] == {"include": ["general", "eng-*"]}

    async def test_a_dm_only_rule_needs_no_include_patterns(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_no_rules(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rules_tab(pilot, app)
            await pilot.press("n")
            await pilot.pause()

            app.screen.query_one("#field-name", Input).value = "dm-rule"
            await _choose_connector_and_agent(pilot, app, "rc", "default")
            app.screen.query_one("#field-rooms-direct", Checkbox).value = True
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            (entry,) = raw["watcher_rules"]
            assert entry["rooms"] == {"direct": True}

    async def test_a_rule_with_no_name_is_refused_with_a_message(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_no_rules(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rules_tab(pilot, app)
            await pilot.press("n")
            await pilot.pause()
            app.screen.query_one("#field-rooms-include", Input).value = "general"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, MessageModal)
            assert "name" in str(app.screen.query_one("#message-body").render()).lower()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, RuleDetailScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["watcher_rules"] == []

    async def test_a_rule_that_can_never_match_is_refused_before_the_generic_gate(
        self, tmp_path, work_dir
    ):
        """No include patterns and no DM opt-in — the loader refuses it too;
        the form says it in form terms first."""
        config_path = _write_config(tmp_path, _config_with_no_rules(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rules_tab(pilot, app)
            await pilot.press("n")
            await pilot.pause()
            app.screen.query_one("#field-name", Input).value = "matchless"
            await _choose_connector_and_agent(pilot, app, "rc", "default")
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, MessageModal)
            body = str(app.screen.query_one("#message-body").render())
            assert "include" in body or "direct" in body

    async def test_a_duplicate_rule_name_is_refused(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_rules(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rules_tab(pilot, app)
            await pilot.press("n")
            await pilot.pause()
            app.screen.query_one("#field-name", Input).value = "eng-rooms"
            await _choose_connector_and_agent(pilot, app, "rc", "default")
            app.screen.query_one("#field-rooms-include", Input).value = "general"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, MessageModal)
            assert "already exists" in str(app.screen.query_one("#message-body").render())
            await pilot.press("enter")
            await pilot.pause()
            raw = yaml.safe_load(Path(config_path).read_text())
            assert len(raw["watcher_rules"]) == 2  # nothing phantom appended


class TestRuleEdit:
    async def test_editing_the_include_list_updates_the_entry_in_place(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_rules(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rule_row(pilot, app, row=0, key="e")
            assert isinstance(app.screen, RuleDetailScreen)
            assert app.screen.mode == "edit"

            app.screen.query_one("#field-rooms-include", Input).value = "eng-*, ops-*"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["watcher_rules"][0]["rooms"]["include"] == ["eng-*", "ops-*"]
            # Still exactly two entries, order untouched.
            assert [w["name"] for w in raw["watcher_rules"]] == ["eng-rooms", "catch-all"]

    async def test_renaming_a_rule_keeps_its_position_and_other_fields(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_rules(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rule_row(pilot, app, row=0, key="e")

            app.screen.query_one("#field-name", Input).value = "engineering"
            await _choose_connector_and_agent(pilot, app, "rc", "default")
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            assert [w["name"] for w in raw["watcher_rules"]] == ["engineering", "catch-all"]
            assert raw["watcher_rules"][0]["rooms"] == {"include": ["eng-*"]}

    async def test_renaming_onto_an_existing_name_is_refused_and_rolls_back(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_rules(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rule_row(pilot, app, row=0, key="e")

            app.screen.query_one("#field-name", Input).value = "catch-all"
            await _choose_connector_and_agent(pilot, app, "rc", "default")
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, MessageModal)
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, RuleDetailScreen)
            # Nothing reached memory or disk.
            assert app.editable_config.watchers_raw[0]["name"] == "eng-rooms"
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["watcher_rules"][0]["name"] == "eng-rooms"

    async def test_a_rejected_save_leaves_the_document_clean_for_a_retry(
        self, tmp_path, work_dir
    ):
        """Trial-entry rollback: a save the gate refuses must not leave the
        invalid trial sitting in the document — and a corrected retry from
        the SAME screen session must succeed."""
        config_path = _write_config(tmp_path, _config_with_rules(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rule_row(pilot, app, row=0, key="e")

            # except_for without include overlap → loader refuses → gate blocks.
            app.screen.query_one("#field-rooms-except_for", Input).value = "zzz-nothing"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert isinstance(app.screen, MessageModal)
            await pilot.press("enter")
            await pilot.pause()
            assert "except_for" not in app.editable_config.watchers_raw[0]["rooms"]

            # Retry with a fixed value from the same open form.
            app.screen.query_one("#field-rooms-except_for", Input).value = "eng-noise"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["watcher_rules"][0]["rooms"]["except_for"] == ["eng-noise"]

    async def test_the_session_ttls_are_editable_rule_fields(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_rules(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rule_row(pilot, app, row=0, key="e")

            app.screen.query_one("#field-session_idle_days", Input).value = "30"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["watcher_rules"][0]["session_idle_days"] == 30


class TestRuleView:
    async def test_view_mode_shows_the_matcher_summary(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_rules(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rule_row(pilot, app, row=1, key="enter")
            assert isinstance(app.screen, RuleDetailScreen)
            assert app.screen.mode == "view"
            body = app.screen._body_text()
            assert "catch-all" in body
            assert "* (except: *-noise)" in body
            assert "connector: rc" in body


class TestRuleDelete:
    async def test_deleting_a_rule_removes_its_entry(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_rules(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rule_row(pilot, app, row=0, key="enter")
            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("tab", "enter")  # focus Delete, press it
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            assert [w["name"] for w in raw["watcher_rules"]] == ["catch-all"]

    async def test_the_delete_confirm_warns_with_the_stranded_counts(
        self, tmp_path, work_dir, monkeypatch
    ):
        """Design §5.5: deleting a rule warns with the persisted sessions it
        strands and the scheduled jobs it orphans — counted read-only off
        the daemon's own files (state_peek), never the control socket."""
        import gateway.configtool.screens.rule_detail as rule_detail_mod

        monkeypatch.setattr(
            rule_detail_mod, "stranded_by_rule", lambda name: (3, 2)
        )
        config_path = _write_config(tmp_path, _config_with_rules(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rule_row(pilot, app, row=0, key="enter")
            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            message = str(app.screen.query_one("#confirm-message").render())
            assert "3 persisted session record(s)" in message
            assert "2 scheduled job(s)" in message
            # The recovery instruction must be the PUBLIC CLI spelling —
            # `coop schedule delete <job_id>` (nested subcommand), not the
            # internal control-socket command name `schedule-delete`
            # (Codex round 3: following the displayed instruction failed
            # at argument parsing exactly when the operator needed it).
            assert "schedule delete" in message
            assert "schedule-delete" not in message
            # The clause the operator reads before deleting, and it has been
            # wrong twice. It first promised the jobs kept running forever (the
            # sweep's old exemption). It was then rewritten to say they "stop
            # delivering once no rule claims those rooms" — also false, and in
            # the direction that matters: a room that still has a record is
            # recreated FROM THAT RECORD, not from the rules
            # (`WatcherManager._recreate`, sticky binding §2.4; pinned by
            # `test_watcher_manager_runtime.py::TestStickyBinding`). So the agent
            # keeps replying in those rooms. And a job fire is ACTIVITY —
            # scheduled injection funnels through the same
            # `MessageProcessor.enqueue` that advances `last_activity_at` — so a
            # job firing more often than `session_idle_days` keeps its OWN
            # record alive and runs forever. An operator who deleted the rule IN
            # ORDER to stop the bot must not be told it has stopped, and must not
            # be given a date either: an earlier version of this test asserted
            # "up to 15 days", which was wrong in the reassuring direction.
            assert "NOT deleted" in message
            assert "does not stop them" in message
            assert "session_idle_days" in message
            assert "indefinitely" in message
            assert "stop delivering" not in message, (
                "the old, false claim — deleting a rule does not stop the jobs"
            )
            assert "exempt from expiry" not in message

    async def test_no_strands_means_the_plain_confirm_message(
        self, tmp_path, work_dir, monkeypatch
    ):
        import gateway.configtool.screens.rule_detail as rule_detail_mod

        monkeypatch.setattr(
            rule_detail_mod, "stranded_by_rule", lambda name: (0, 0)
        )
        config_path = _write_config(tmp_path, _config_with_rules(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rule_row(pilot, app, row=0, key="enter")
            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            message = str(app.screen.query_one("#confirm-message").render())
            assert "session record" not in message


class TestReorderAroundABrokenRule:
    async def test_moving_a_rule_past_a_broken_one_is_refused_and_swapped_back(
        self, tmp_path, work_dir
    ):
        """Known limitation, pinned deliberately: a rule parse error embeds
        the rule's LIST INDEX in its message ("Watcher rule at index N:
        ..."), and the save gate compares exact (kind, name, message)
        tuples — so moving a rule past a pre-existing broken one shifts the
        broken rule's index, its message changes, and the gate reads the
        old problem as a NEW one and refuses the move (loud, safe, nothing
        written; the notify carries the broken rule's own error). This
        contradicts the gate's "pre-existing problems never block unrelated
        saves" intent, but the fix belongs to the gate/parser message
        contract, not to this tab — if this test starts failing because the
        move now SUCCEEDS, that contract changed: update the config-tool
        docs' known-limitations note along with this test. (Owner-ratified
        as not-a-bug, 2026-08-19; the refusal notify says outright that a
        pre-existing broken rule blocks reordering — a pure swap cannot
        genuinely introduce a new per-entry error.)"""
        config_path = _write_config(tmp_path, f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {work_dir}
            watcher_rules:
              - name: good-rule
                connector: rc
                agent: default
                rooms:
                  include: [general]
              - name: broken-rule
                connector: rc
                agent: default
                rooms:
                  include: []
        """)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rules_tab(pilot, app)
            table = app.screen.query_one("#rules-table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("]")
            await pilot.pause()

            # Refused and swapped back — disk and memory both untouched.
            raw = yaml.safe_load(Path(config_path).read_text())
            assert [w["name"] for w in raw["watcher_rules"]] == ["good-rule", "broken-rule"]
            assert [w["name"] for w in app.editable_config.watchers_raw] == [
                "good-rule", "broken-rule",
            ]


class TestEmptyPrerequisiteGuards:
    """Internal review (lens B): the connector/agent dropdowns are the first
    enum fields whose options come from a config list that can be EMPTY,
    and Textual's Select(allow_blank=False) raises EmptySelectError at
    construction on empty options — mid-compose, taking the app down. Every
    entry into a non-view mode notifies instead."""

    def _config_with_no_connectors(self, work_dir: Path) -> str:
        return f"""\
            connectors: []
            agents:
              default:
                type: claude
                working_directory: {work_dir}
            watcher_rules:
              - name: orphan-rule
                connector: rc-gone
                agent: default
                rooms:
                  include: [general]
        """

    async def test_n_with_zero_connectors_notifies_instead_of_crashing(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, self._config_with_no_connectors(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rules_tab(pilot, app)
            await pilot.press("n")
            await pilot.pause()
            assert app.is_running is True
            assert isinstance(app.screen, OverviewScreen)

    async def test_e_on_a_rule_with_zero_connectors_notifies_instead_of_crashing(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, self._config_with_no_connectors(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rule_row(pilot, app, row=0, key="e")
            assert app.is_running is True
            assert isinstance(app.screen, OverviewScreen)

    async def test_view_to_edit_with_zero_connectors_stays_in_view_mode(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, self._config_with_no_connectors(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rule_row(pilot, app, row=0, key="enter")
            assert isinstance(app.screen, RuleDetailScreen)
            assert app.screen.mode == "view"
            await pilot.press("e")
            await pilot.pause()
            assert app.is_running is True
            assert isinstance(app.screen, RuleDetailScreen)
            assert app.screen.mode == "view"


class TestCodexRound1Fixes:
    async def test_an_untouched_agent_select_is_refused_not_guessed(
        self, tmp_path, work_dir
    ):
        """Codex review of #129 (P1) found that the agent prefill used
        `next(iter(agents))` — the FIRST agent — while create mode force-wrote
        the selection, so an untouched Agent field silently bound a new rule to
        the wrong backend. That fix made the prefill honour `default_agent:`.

        Both the fallback and the prefill are gone now, and this asserts what
        replaced them: an untouched required Select is BLANK, and Save says so
        rather than choosing. The earlier fix picked a better guess; this stops
        guessing, which is the same objection one level up.
        """
        config_path = _write_config(tmp_path, f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              aaa-first:
                type: claude
                working_directory: {work_dir}
              zother:
                type: claude
                working_directory: {work_dir}
            watcher_rules: []
        """)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rules_tab(pilot, app)
            await pilot.press("n")
            await pilot.pause()
            agent_select = app.screen.query_one("#field-agent", Select)
            assert agent_select.value is Select.NULL, (
                f"an untouched required Select must be blank, got {agent_select.value!r}"
            )

            app.screen.query_one("#field-name", Input).value = "my-rule"
            app.screen.query_one("#field-rooms-include", Input).value = "general"
            app.screen.query_one("#field-connector", Select).value = "rc"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert app.screen.__class__.__name__ == "MessageModal", "must be refused"
            body = str(app.screen.query_one("#message-body").render())
            assert "needs an agent" in body, body
            await pilot.press("enter")
            await pilot.pause()

            # Choosing is what writes it — and the second agent, so the
            # assertion cannot pass by accident of document order.
            app.screen.query_one("#field-agent", Select).value = "zother"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)

        raw = yaml.safe_load(Path(config_path).read_text())
        (entry,) = raw["watcher_rules"]
        assert entry["agent"] == "zother"
        from gateway.config import GatewayConfig

        assert GatewayConfig.from_file(config_path).watcher_rules[0].agent == "zother"

    async def test_a_typed_name_survives_an_inherits_template_switch(
        self, tmp_path, work_dir
    ):
        """Codex review of #129 (P2): `name` is a generic FieldSpec on this
        screen, so the base recompute rebuilt its Input from the (empty)
        entry — a typed-but-unsaved name vanished on every template switch,
        and Save was then refused in create mode."""
        config_path = _write_config(tmp_path, f"""\
            watcher_templates:
              slow:
                session_idle_days: 30
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {work_dir}
            watcher_rules: []
        """)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rules_tab(pilot, app)
            await pilot.press("n")
            await pilot.pause()
            app.screen.query_one("#field-name", Input).value = "my-rule"
            await _choose_connector_and_agent(pilot, app, "rc", "default")
            await pilot.pause()

            # Switch templates the way the picker's confirm path does.
            app.screen._inherits_current = "slow"
            await app.screen._recompute_form()
            await pilot.pause()

            assert app.screen.query_one("#field-name", Input).value == "my-rule"

    async def test_the_stranded_lookup_uses_the_canonical_stripped_name(
        self, tmp_path, work_dir, monkeypatch
    ):
        """Codex review of #129 (P2): the loader strips a rule's name before
        persisting it as records' rule_name — an externally-authored
        `name: " padded "` must be stripped before the stranded lookup, or
        the disclosure is silently suppressed."""
        import gateway.configtool.screens.rule_detail as rule_detail_mod

        seen: list[str] = []

        def capture(name):
            seen.append(name)
            return (0, 0)

        monkeypatch.setattr(rule_detail_mod, "stranded_by_rule", capture)
        config_path = _write_config(tmp_path, f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {work_dir}
            watcher_rules:
              - name: " padded "
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rule_row(pilot, app, row=0, key="enter")
            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            assert seen == ["padded"]

    async def test_a_malformed_watchers_scalar_renders_zero_rows_and_move_is_a_no_op(
        self, tmp_path, work_dir
    ):
        """Codex review of #129 (P2): `watcher_rules: 5` parses as YAML but is
        not a list — iterating it crashed the overview repaint with a
        TypeError, and `[`/`]` crashed move_watcher_rule. Normalized to an
        empty view everywhere (EditableConfig.watcher_entries); the banner
        still reports the config's real error."""
        config_path = _write_config(tmp_path, f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {work_dir}
            watcher_rules: 5
        """)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.is_running is True
            await _open_rules_tab(pilot, app)
            table = app.screen.query_one("#rules-table", DataTable)
            assert table.row_count == 0
            await pilot.press("]")
            await pilot.pause()
            assert app.is_running is True
            # Nothing was written — the scalar is still on disk, untouched.
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["watcher_rules"] == 5


class TestCodexRound2Fixes:
    def _config_with_template(self, work_dir: Path) -> str:
        return f"""\
            watcher_templates:
              slow:
                session_idle_days: 30
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {work_dir}
            watcher_rules:
              - name: old-name
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """

    async def test_a_cleared_name_survives_a_template_switch(self, tmp_path, work_dir):
        """Codex round 2: `_name_live` truthiness can't distinguish 'cleared
        to empty' from 'never touched' — clearing the name and then picking
        a template silently resurrected the old identity."""
        config_path = _write_config(tmp_path, self._config_with_template(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rule_row(pilot, app, row=0, key="e")
            app.screen.query_one("#field-name", Input).value = ""
            await _choose_connector_and_agent(pilot, app, "rc", "default")
            await pilot.pause()

            app.screen._inherits_current = "slow"
            await app.screen._recompute_form()
            await pilot.pause()

            assert app.screen.query_one("#field-name", Input).value == ""

    async def test_an_untouched_name_still_shows_the_entrys_own_after_a_switch(
        self, tmp_path, work_dir
    ):
        """The other half of the same gate: an untouched form ('' because
        nothing was ever typed) must keep showing the entry's own name
        after a template switch, not get blanked by an over-eager restore."""
        config_path = _write_config(tmp_path, self._config_with_template(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rule_row(pilot, app, row=0, key="e")

            app.screen._inherits_current = "slow"
            await app.screen._recompute_form()
            await pilot.pause()

            assert app.screen.query_one("#field-name", Input).value == "old-name"


class TestCodexRound3Fixes:
    async def test_creating_over_a_mapping_watchers_block_is_refused_not_overwritten(
        self, tmp_path, work_dir
    ):
        """Codex round 3: `watcher_rules:` as a MAPPING (an operator omitted the
        '-' before an otherwise complete rule) holds recoverable data —
        normalizing it to [] and appending would pass the save gate (the
        structural error vanishes with the data) and silently delete the
        rule the user meant to keep."""
        config_path = _write_config(tmp_path, f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {work_dir}
            watcher_rules:
              name: forgot-the-dash
              connector: rc
              agent: default
              rooms:
                include: [general]
        """)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rules_tab(pilot, app)
            await pilot.press("n")
            await pilot.pause()
            app.screen.query_one("#field-name", Input).value = "new-rule"
            await _choose_connector_and_agent(pilot, app, "rc", "default")
            app.screen.query_one("#field-rooms-include", Input).value = "dev"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, MessageModal)
            body = str(app.screen.query_one("#message-body").render())
            assert "not a list" in body
            await pilot.press("enter")
            await pilot.pause()
            # The mapping — the user's recoverable rule — is untouched.
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["watcher_rules"]["name"] == "forgot-the-dash"

    def _config_with_a_garbage_entry(self, work_dir: Path) -> str:
        return f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {work_dir}
            watcher_rules:
              - "stray yaml, not a mapping"
              - name: good-rule
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """

    async def test_d_deletes_a_non_mapping_entry_by_index(self, tmp_path, work_dir):
        """Codex round 3: the non-mapping entry renders as an ERROR row on
        purpose — 'visible but unremovable' was half a fix. 'd' deletes it
        by list index (there is no dict for a detail screen to open)."""
        config_path = _write_config(tmp_path, self._config_with_a_garbage_entry(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rules_tab(pilot, app)
            table = app.screen.query_one("#rules-table", DataTable)
            assert table.row_count == 2
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("tab", "enter")  # Delete
            await pilot.pause()

            raw = yaml.safe_load(Path(config_path).read_text())
            assert [w.get("name") for w in raw["watcher_rules"]] == ["good-rule"]

    async def test_enter_and_e_on_a_non_mapping_entry_notify_instead_of_dead_keys(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, self._config_with_a_garbage_entry(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rules_tab(pilot, app)
            table = app.screen.query_one("#rules-table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)  # no crash, no push
            await pilot.press("e")
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)
            assert app.is_running is True


class TestCodexRound4Fixes:
    async def test_editing_a_rule_whose_list_field_is_a_scalar_does_not_crash(
        self, tmp_path, work_dir
    ):
        """Codex round 4: the validator marks `rooms.include: 5` an ERROR
        row and the form is how the operator is meant to repair it — but
        composing it passed the raw int to list_to_text(), raising
        TypeError mid-compose and taking the whole TUI down."""
        config_path = _write_config(tmp_path, f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {work_dir}
            watcher_rules:
              - name: broken-list
                connector: rc
                agent: default
                rooms:
                  include: 5
        """)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rule_row(pilot, app, row=0, key="e")
            assert app.is_running is True
            assert isinstance(app.screen, RuleDetailScreen)
            assert app.screen.mode == "edit"
            # Shown verbatim as one item — visible and repairable, never
            # silently rewritten.
            assert app.screen.query_one("#field-rooms-include", Input).value == "5"

    async def test_deleting_a_malformed_row_above_another_broken_rule_explains_the_order(
        self, tmp_path, work_dir
    ):
        """Owner-ratified as the same known limitation the reorder refusal
        carries (2026-08-19): a removal renumbers later entries, so a broken
        rule BELOW has its index-embedded error shift and the save gate
        reads the pre-existing problem as new. Not fixed at the gate — the
        refusal is loud, the row is restored, and the message states the
        bottom-up repair order instead of just quoting the gate."""
        config_path = _write_config(tmp_path, f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {work_dir}
            watcher_rules:
              - "stray yaml, not a mapping"
              - name: also-broken
                connector: rc
                agent: default
                rooms:
                  include: []
        """)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rules_tab(pilot, app)
            table = app.screen.query_one("#rules-table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("tab", "enter")  # Delete
            await pilot.pause()

            assert isinstance(app.screen, MessageModal)
            body = str(app.screen.query_one("#message-body").render())
            assert "LOWEST ERROR row first" in body
            await pilot.press("enter")
            await pilot.pause()
            # Rolled back — the garbage row is still there, nothing written.
            raw = yaml.safe_load(Path(config_path).read_text())
            assert len(raw["watcher_rules"]) == 2
            assert raw["watcher_rules"][0] == "stray yaml, not a mapping"

    async def test_deleting_the_lowest_broken_row_succeeds(self, tmp_path, work_dir):
        """The other half of the documented order: bottom-up works."""
        config_path = _write_config(tmp_path, f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {work_dir}
            watcher_rules:
              - name: good-rule
                connector: rc
                agent: default
                rooms:
                  include: [general]
              - "stray yaml, not a mapping"
        """)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rules_tab(pilot, app)
            table = app.screen.query_one("#rules-table", DataTable)
            table.focus()
            table.move_cursor(row=1)
            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("tab", "enter")
            await pilot.pause()

            raw = yaml.safe_load(Path(config_path).read_text())
            assert [w.get("name") for w in raw["watcher_rules"]] == ["good-rule"]


class TestCodexRound5Fixes:
    """Untouched means untouched, and operator text is displayed verbatim."""

    async def test_saving_an_unrelated_field_does_not_activate_a_quoted_ttl_rule(
        self, tmp_path, work_dir
    ):
        """Codex round 5: `session_idle_days: "15"` makes the rule fail to
        parse entirely (verified at the loader: 0 rules, 1 issue). The int
        field rendered `15`, read back the int `15`, compared unequal to the
        string, and merely pressing Save normalized it — taking a rule that
        was inert LIVE. A routing change nobody asked for."""
        config_path = _write_config(tmp_path, f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {work_dir}
            watcher_rules:
              - name: inert-rule
                connector: rc
                agent: default
                session_idle_days: "15"
                rooms:
                  include: [general]
        """)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rule_row(pilot, app, row=0, key="e")
            assert isinstance(app.screen, RuleDetailScreen)
            # Edit something UNRELATED, then save.
            app.screen.query_one("#field-description", Input).value = "a note"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            raw = yaml.safe_load(Path(config_path).read_text())
            entry = raw["watcher_rules"][0]
            assert entry["description"] == "a note"       # the real edit landed
            assert entry["session_idle_days"] == "15"     # still quoted, still inert
            # And it is still reported as broken, not silently repaired.
            from gateway.config import collect_config
            cfg, issues = collect_config(config_path)
            assert [i for i in issues if i.entity_kind == "watcher"]

    async def test_saving_an_unrelated_field_does_not_split_a_comma_bearing_pattern(
        self, tmp_path, work_dir
    ):
        """Codex round 5: a comma is an ordinary literal in the pattern
        language, so `include: ["team,one"]` is legal and loads cleanly —
        and so does the two-pattern shape it used to be rewritten into, so
        the save gate had nothing to object to and the rule silently began
        claiming two different rooms."""
        config_path = _write_config(tmp_path, f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {work_dir}
            watcher_rules:
              - name: comma-rule
                connector: rc
                agent: default
                rooms:
                  include: ["team,one"]
        """)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rule_row(pilot, app, row=0, key="e")
            app.screen.query_one("#field-description", Input).value = "a note"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            raw = yaml.safe_load(Path(config_path).read_text())
            entry = raw["watcher_rules"][0]
            assert entry["description"] == "a note"
            assert entry["rooms"]["include"] == ["team,one"]  # NOT split

    async def test_a_rule_named_with_markup_characters_opens_instead_of_crashing(
        self, tmp_path, work_dir
    ):
        """Codex round 5: rule names are unrestricted, and a name like `[/]`
        raised Rich's MarkupError when its row was opened — making that rule
        unviewable, uneditable and undeletable through the TUI."""
        config_path = _write_config(tmp_path, f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {work_dir}
            watcher_rules:
              - name: "[/]"
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rules_tab(pilot, app)
            assert app.is_running is True
            await _open_rule_row(pilot, app, row=0, key="enter")
            assert app.is_running is True
            assert isinstance(app.screen, RuleDetailScreen)
            # And the delete confirm — which interpolates the label too.
            await pilot.press("d")
            await pilot.pause()
            assert app.is_running is True
            assert isinstance(app.screen, ConfirmModal)

    async def test_a_character_class_pattern_is_displayed_in_full(
        self, tmp_path, work_dir
    ):
        """The worse half of the same finding: `[…]` is documented,
        first-class pattern syntax, and `eng-[ab]` rendered as `eng-` —
        the display CONCEALED which rooms the rule actually claims."""
        config_path = _write_config(tmp_path, f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {work_dir}
            watcher_rules:
              - name: class-rule
                connector: rc
                agent: default
                rooms:
                  include: ["eng-[ab]"]
        """)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rules_tab(pilot, app)
            table = app.screen.query_one("#rules-table", DataTable)
            # The cell renders through Rich; its VISIBLE text must keep the
            # class intact.
            from rich.markup import render
            assert render(str(table.get_row_at(0)[4])).plain == "eng-[ab]"

            await _open_rule_row(pilot, app, row=0, key="enter")
            # Static.render() hands back the ALREADY-PARSED Text, so its str
            # is the visible plain text — the class must survive into it.
            # (Rendering it a second time would re-parse the brackets and is
            # the mistake this comment exists to stop being repeated.)
            body = str(app.screen.query_one("#rule-detail-body", Static).render())
            assert "eng-[ab]" in body


class TestCodexRound6Fixes:
    """Round 5 made an UNTOUCHED comma-bearing item safe; round 6 covers the
    other half — editing the list for any reason used to re-split it."""

    def _config_with(self, work_dir: Path, rooms_or_files: str) -> str:
        return f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {work_dir}
            watcher_rules:
              - name: comma-rule
                connector: rc
                agent: default
{rooms_or_files}
        """

    async def test_appending_to_a_comma_bearing_pattern_list_is_refused_loudly(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, self._config_with(
            work_dir,
            '                rooms:\n                  include: ["team,one"]\n',
        ))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rule_row(pilot, app, row=0, key="e")
            box = app.screen.query_one("#field-rooms-include", Input)
            assert box.value == "team,one"
            box.value = "team,one, new-room"     # the operator appends an item
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, MessageModal)
            body = str(app.screen.query_one("#message-body").render())
            assert "comma" in body and "EDITOR" in body
            await pilot.press("enter")
            await pilot.pause()
            # Nothing written — the original single pattern survives intact.
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["watcher_rules"][0]["rooms"]["include"] == ["team,one"]

    async def test_editing_a_comma_bearing_context_file_is_refused(
        self, tmp_path, work_dir
    ):
        """The genuinely load-bearing case, and why this is fixed rather
        than declined: a FILE PATH may legitimately contain a comma, and
        splitting `my,notes.md` silently stops injecting the real file.
        (A comma in a room pattern, by contrast, is provably inert — no
        room name on either platform can contain one.)"""
        (work_dir / "my,notes.md").write_text("hello")
        config_path = _write_config(tmp_path, self._config_with(
            work_dir,
            '                context_inject_files: ["my,notes.md"]\n'
            '                rooms:\n                  include: [general]\n',
        ))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rule_row(pilot, app, row=0, key="e")
            box = app.screen.query_one("#field-context_inject_files", Input)
            assert box.value == "my,notes.md"
            box.value = "my,notes.md, other.md"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, MessageModal)
            await pilot.press("enter")
            await pilot.pause()
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["watcher_rules"][0]["context_inject_files"] == ["my,notes.md"]

    async def test_an_ordinary_list_field_still_edits_normally(
        self, tmp_path, work_dir
    ):
        """The guard must not have been widened into 'list fields are
        read-only' — only a delimiter-bearing item blocks."""
        config_path = _write_config(tmp_path, self._config_with(
            work_dir,
            '                rooms:\n                  include: [general]\n',
        ))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rule_row(pilot, app, row=0, key="e")
            app.screen.query_one("#field-rooms-include", Input).value = "general, dev"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["watcher_rules"][0]["rooms"]["include"] == ["general", "dev"]


class TestCodexRound7Fixes:
    async def test_the_refusal_names_a_bracketed_item_faithfully_and_does_not_crash(
        self, tmp_path, work_dir
    ):
        """Codex round 7: the refusal message interpolated the operator's own
        value into a markup-parsing modal. `my,[notes].md` was rendered as
        `my,.md` (naming the WRONG item) and `my,[/].md` raised MarkupError,
        crashing the modal and so defeating the loud refusal itself.

        The value need not name a real file — the loader resolves
        context_inject_files paths without requiring existence, and `[/]`
        cannot appear inside a single filename anyway (it spans a path
        separator), so this pins the hostile SPELLING rather than a real
        file."""
        config_path = _write_config(tmp_path, f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {work_dir}
            watcher_rules:
              - name: hostile-path
                connector: rc
                agent: default
                context_inject_files: ["my,[/].md"]
                rooms:
                  include: [general]
        """)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rule_row(pilot, app, row=0, key="e")
            box = app.screen.query_one("#field-context_inject_files", Input)
            assert box.value == "my,[/].md"        # displayed raw, not split
            box.value = "my,[/].md, other.md"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert app.is_running is True           # the modal did not crash
            assert isinstance(app.screen, MessageModal)
            body = str(app.screen.query_one("#message-body").render())
            assert "my,[/].md" in body              # and it names the real item


class TestCodexRound8Fixes:
    async def test_a_rolled_back_move_does_not_leave_the_config_dirty(
        self, tmp_path, work_dir
    ):
        """Codex round 8: the move and the counter-move both call
        mark_dirty(), so a REJECTED reorder restored the document
        byte-for-byte but left the flag set — and the quit gate then asked
        the operator to discard changes that no longer existed."""
        config_path = _write_config(tmp_path, f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {work_dir}
            watcher_rules:
              - name: good-rule
                connector: rc
                agent: default
                rooms:
                  include: [general]
              - name: broken-rule
                connector: rc
                agent: default
                rooms:
                  include: []
        """)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.editable_config.dirty is False
            await _open_rules_tab(pilot, app)
            table = app.screen.query_one("#rules-table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("]")            # refused: shifts the broken rule
            await pilot.pause()

            raw = yaml.safe_load(Path(config_path).read_text())
            assert [w["name"] for w in raw["watcher_rules"]] == ["good-rule", "broken-rule"]
            assert app.editable_config.dirty is False, (
                "a rolled-back move must not leave the config looking edited"
            )

    async def test_a_rolled_back_malformed_row_delete_does_not_leave_it_dirty(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {work_dir}
            watcher_rules:
              - "stray yaml, not a mapping"
              - name: also-broken
                connector: rc
                agent: default
                rooms:
                  include: []
        """)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rules_tab(pilot, app)
            table = app.screen.query_one("#rules-table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("tab", "enter")   # Delete -> refused below
            await pilot.pause()
            assert isinstance(app.screen, MessageModal)
            await pilot.press("enter")
            await pilot.pause()
            assert app.editable_config.dirty is False


class TestCodexRound10Fixes:
    async def test_a_rejected_rule_save_restores_dirty_and_the_document_shape(
        self, tmp_path, work_dir
    ):
        """Codex round 10: the rule form's own save rollback left
        `cfg.dirty` set, so quitting offered to discard changes that no
        longer existed. This site is MINE and I missed it when scoping
        round 8's fix (the pre-existing siblings are issue #131). A
        rejected CREATE also left an empty `watcher_rules: []` where the key had
        been absent."""
        config_path = _write_config(tmp_path, f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {work_dir}
        """)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert "watcher_rules" not in app.editable_config.document
            await _open_rules_tab(pilot, app)
            await pilot.press("n")
            await pilot.pause()
            app.screen.query_one("#field-name", Input).value = "new-rule"
            await _choose_connector_and_agent(pilot, app, "rc", "default")
            # An except_for with no overlapping include is refused by the loader.
            app.screen.query_one("#field-rooms-include", Input).value = "general"
            app.screen.query_one("#field-rooms-except_for", Input).value = "zzz-nothing"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, MessageModal)
            await pilot.press("enter")
            await pilot.pause()
            assert app.editable_config.dirty is False, (
                "a rejected save must not leave the config looking edited"
            )
            assert "watcher_rules" not in app.editable_config.document, (
                "a rejected create must not leave an empty watchers list behind"
            )

    async def test_an_unparseable_number_reports_the_value_without_crashing(
        self, tmp_path, work_dir
    ):
        """Codex round 10: read_widget_value() quotes the operator's own
        text into the error ("got '[/]'"), and that message went into a
        markup-parsing modal — so the report of a bad value crashed on the
        value. The static markup check missed it because the argument is not
        an f-string; that blind spot is now closed too."""
        config_path = _write_config(tmp_path, f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {work_dir}
            watcher_rules:
              - name: r1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rule_row(pilot, app, row=0, key="e")
            app.screen.query_one("#field-session_idle_days", Input).value = "[/]"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert app.is_running is True, "the field-error modal killed the app"
            assert isinstance(app.screen, MessageModal)
            body = str(app.screen.query_one("#message-body").render())
            assert "[/]" in body, "the modal must name the offending value"


class TestCodexRound11Fixes:
    """A rejected create must restore the document EXACTLY as loaded —
    including the difference between an absent `watcher_rules:` key and an
    explicit `watcher_rules:` (null), which `.get()` cannot tell apart."""

    def _config(self, work_dir: Path, watchers_line: str) -> str:
        return f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {work_dir}
{watchers_line}
        """

    async def _reject_a_create(self, pilot, app) -> None:
        await _open_rules_tab(pilot, app)
        await pilot.press("n")
        await pilot.pause()
        app.screen.query_one("#field-name", Input).value = "new-rule"
        app.screen.query_one("#field-rooms-include", Input).value = "general"
        # An except_for that cannot overlap the include is refused by the loader.
        app.screen.query_one("#field-rooms-except_for", Input).value = "zzz-nothing"
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, MessageModal)
        await pilot.press("enter")
        await pilot.pause()

    async def test_an_explicit_null_watchers_key_survives_a_rejected_create(
        self, tmp_path, work_dir
    ):
        """Round 10 popped the key here, because `.get()` reports None for
        an explicit null exactly as it does for an absent key — so the
        operator's own `watcher_rules:` line would have vanished from the file on
        the next unrelated successful save."""
        config_path = _write_config(tmp_path, self._config(work_dir, "            watcher_rules:"))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert "watcher_rules" in app.editable_config.document
            assert app.editable_config.document["watcher_rules"] is None
            await self._reject_a_create(pilot, app)

            doc = app.editable_config.document
            assert "watcher_rules" in doc, "the explicit key was popped"
            assert doc["watcher_rules"] is None, "the explicit null was replaced"
            assert app.editable_config.dirty is False

    async def test_an_absent_watchers_key_stays_absent_after_a_rejected_create(
        self, tmp_path, work_dir
    ):
        """The other half, so the fix cannot drift into always restoring a
        key that was never there."""
        config_path = _write_config(tmp_path, self._config(work_dir, ""))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert "watcher_rules" not in app.editable_config.document
            await self._reject_a_create(pilot, app)
            assert "watcher_rules" not in app.editable_config.document
            assert app.editable_config.dirty is False
