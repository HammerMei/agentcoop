"""Pilot-based tests for gateway/configtool/app.py — the config TUI.

No prior Textual-app test precedent existed in this repo before this file;
the pattern here (async test functions using `App.run_test()`/`Pilot`,
plain pytest style rather than unittest.TestCase — pytest-asyncio's
asyncio_mode="auto" does not play well with unittest's own event-loop
management) is the one to follow for future config TUI phases.

Known, empirically-confirmed limitation: `App.suspend()` raises
`SuspendNotSupported` under `run_test()`'s headless driver (verified while
building this suite) — so the $EDITOR round-trip's actual suspend+subprocess
line cannot be exercised here (see `# pragma: no cover` in app.py). What
*is* tested here: that `open_editor_and_reload()` handles that failure
gracefully (notifies, doesn't crash) rather than propagating it, and that
`reload_config()`/`repaint_from_memory()` — the part of the round-trip that
matters for correctness — work correctly on their own.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
from textual.widgets import DataTable, Static

from gateway.configtool.app import ConfigToolApp
from gateway.configtool.modals import ConfirmModal, MessageModal
from gateway.configtool.screens.agent_detail import AgentDetailScreen
from gateway.configtool.screens.connector_detail import ConnectorDetailScreen
from gateway.configtool.screens.overview import OverviewScreen
from gateway.configtool.screens.rule_detail import RuleDetailScreen
from gateway.configtool.screens.template_detail import TemplateDetailScreen
from gateway.configtool.screens.tool_presets import ToolPresetsScreen


def _write_config(tmp_path: Path, yaml_text: str) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(yaml_text))
    return str(path)


def _valid_config_text(work_dir: Path) -> str:
    return f"""\
        tool_presets:
          readonly:
            - tool: Read
        connector_templates:
          standard:
            type: rocketchat
        connectors:
          - name: rc-home
            inherits: standard
            description: "Main bot"
            server: {{url: "http://localhost:3000", username: bot, password: pw}}
            allowed_users: {{owners: [alice], guests: []}}
        agent_templates:
          standard:
            type: claude
            timeout: 1800
            permissions: {{enabled: true, timeout: 300}}
            owner_allowed_tools: [readonly]
        agents:
          my-agent:
            inherits: standard
            working_directory: {work_dir}
        watcher_rules:
          - name: my-rooms
            connector: rc-home
            agent: my-agent
            rooms:
              include: [general, dev]
              direct: true
    """


@pytest.fixture
def work_dir(tmp_path: Path) -> Path:
    d = tmp_path / "work"
    d.mkdir()
    return d


class TestOverviewRender:
    """Instantiate a fresh App per test (Textual issue #4998: run_test() is
    awkward to share across tests via a fixture — construct/enter it inside
    each test function instead)."""

    async def test_valid_config_renders_all_tables(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _valid_config_text(work_dir))
        app = ConfigToolApp(config_path, lint=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)

            banner = app.screen.query_one("#banner", Static)
            assert "valid" in str(banner.render())
            assert "1 rule" in str(banner.render())

            assert app.screen.query_one("#connectors-table", DataTable).row_count == 1
            assert app.screen.query_one("#agents-table", DataTable).row_count == 1
            # One row per RULE, straight off the raw document.
            assert app.screen.query_one("#rules-table", DataTable).row_count == 1
            # 1 connector_templates entry + 1 agent_templates entry (no
            # watcher_templates in this fixture).
            assert app.screen.query_one("#templates-table", DataTable).row_count == 2
            assert app.screen.query_one("#presets-table", DataTable).row_count == 1

    async def test_focus_starts_on_the_list_not_the_tab_bar(self, tmp_path, work_dir):
        """User-requested UX change: focus lands directly on the active
        tab's list on mount, so the very first keypress (arrow keys, e/d)
        already acts on a row — no longer requires pressing 'tab' first to
        reach the list. Left/right (below) now handle tab switching
        instead, so 'tab'/'shift+tab' cycling through focusable widgets is
        no longer the primary way to reach the list — see
        test_tab_binding_is_still_available_as_a_fallback for confirmation
        that binding wasn't removed, just no longer the FIRST thing needed."""
        config_path = _write_config(tmp_path, _valid_config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.focused, DataTable)
            assert app.focused.id == "connectors-table"

    async def test_tab_binding_is_still_available_as_a_fallback(self, tmp_path, work_dir):
        """'tab' still cycles focus (Screen's own app.focus_next, rebound
        here with show=True) — kept as a fallback/for moving off the list
        (e.g. onto the tab bar itself), even though it's no longer required
        to reach the list on mount."""
        config_path = _write_config(tmp_path, _valid_config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            bound = app.screen.active_bindings.get("tab")
            assert bound is not None
            assert bound.binding.show is True

    async def test_q_key_is_a_visible_quit_binding(self, tmp_path, work_dir):
        """'q' is the documented, discoverable quit key (App's own ctrl+q
        default stays too, but is hidden — show=False — so it doesn't count
        as discoverable on its own)."""
        config_path = _write_config(tmp_path, _valid_config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            bound = app.screen.active_bindings.get("q")
            assert bound is not None
            assert bound.binding.show is True
            assert bound.binding.action == "app.quit"

            await pilot.press("q")
            await pilot.pause()
            assert app.is_running is False

    async def test_escape_is_a_hidden_quit_alias_on_the_overview(self, tmp_path, work_dir):
        """User-requested: every detail/form screen already uses Escape to
        go back, and OverviewScreen is the one screen with nowhere further
        "back" to go — Escape here quits (same as 'q'), matching the habit
        those other screens train. show=False since 'q' remains the one
        documented, visible quit key (same treatment ctrl+q already gets)."""
        config_path = _write_config(tmp_path, _valid_config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            bound = app.screen.active_bindings.get("escape")
            assert bound is not None
            assert bound.binding.show is False
            assert bound.binding.action == "app.quit"

            await pilot.press("escape")
            await pilot.pause()
            assert app.is_running is False

    async def test_duplicate_connector_names_do_not_crash_the_table(self, tmp_path, work_dir):
        """Regression: connectors_raw is the raw, pre-validation list — two
        connectors sharing a name used to crash repaint_from_memory() with
        Textual's DuplicateKey (add_row(key=name) on a repeated key). Rows
        are now keyed by list position instead."""
        config_path = _write_config(tmp_path, f"""\
            connectors:
              - name: rc-home
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
              - name: rc-home
                type: rocketchat
                server: {{url: "http://localhost:3001", username: bot2, password: pw2}}
            agents:
              default:
                type: claude
                working_directory: {work_dir}
            watcher_rules:
              - name: w1
                connector: rc-home
                agent: default
                room: general
        """)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()  # must not raise DuplicateKey
            table = app.screen.query_one("#connectors-table", DataTable)
            assert table.row_count == 2

            table.focus()
            table.move_cursor(row=1)
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ConnectorDetailScreen)
            body = str(app.screen.query_one("#connector-detail-body", Static).render())
            assert "3001" in body  # drilled into the SECOND row, not the first

    async def test_connectors_missing_name_do_not_crash_the_table(self, tmp_path, work_dir):
        """Regression: two connectors both missing 'name:' both fell back to
        the placeholder "?" and hit the same DuplicateKey crash."""
        config_path = _write_config(tmp_path, f"""\
            connectors:
              - type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
              - type: rocketchat
                server: {{url: "http://localhost:3001", username: bot2, password: pw2}}
            agents:
              default:
                type: claude
                working_directory: {work_dir}
            watcher_rules:
              - name: w1
                connector: rc-home
                agent: default
                room: general
        """)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()  # must not raise DuplicateKey
            table = app.screen.query_one("#connectors-table", DataTable)
            assert table.row_count == 2

    async def test_connector_type_inherited_from_template_is_shown_not_a_placeholder(
        self, tmp_path, work_dir
    ):
        """Regression: the connector row must show the MERGED type even when
        'type' is only set via its inherits: template, not on the entry
        itself — this crashed/showed '?' before the merged_entry() fix."""
        config_path = _write_config(tmp_path, _valid_config_text(work_dir))
        app = ConfigToolApp(config_path, lint=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one("#connectors-table", DataTable)
            row = table.get_row_at(0)
            assert row[1] == "rocketchat"

    async def test_missing_config_shows_global_error_banner_not_a_crash(self, tmp_path):
        config_path = str(tmp_path / "does-not-exist.yaml")
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            banner = app.screen.query_one("#banner", Static)
            assert "does not currently load" in str(banner.render())
            # Tables must be empty, not crash the screen.
            assert app.screen.query_one("#connectors-table", DataTable).row_count == 0

    async def test_invalid_config_from_file_failure_does_not_crash_rules_table(
        self, tmp_path, work_dir
    ):
        """A config that parses as YAML but fails per-entity validation
        (a leftover static-shape watcher entry) must not crash while
        populating the Rules table — and unlike the old Watchers tab, the
        broken entry still gets its row, wearing its error in the Status
        column instead of silently disappearing."""
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
              - name: w1
                connector: rc
                agent: default
                room: general
        """)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            banner = app.screen.query_one("#banner", Static)
            assert "error" in str(banner.render()).lower()
            rules_table = app.screen.query_one("#rules-table", DataTable)
            assert rules_table.row_count == 1
            assert "error" in str(rules_table.get_row_at(0)[5]).lower()

    async def test_refresh_action_picks_up_on_disk_changes(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _valid_config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.query_one("#agents-table", DataTable).row_count == 1

            # Appending to the END of the file lands under `watchers:` —
            # a second RULE, reflected in the banner count and the table.
            with open(config_path, "a") as f:
                f.write("  - name: extra\n    connector: rc-home\n"
                        "    agent: my-agent\n    rooms:\n      include: [extra]\n")

            await pilot.press("r")
            await pilot.pause()
            banner = str(app.screen.query_one("#banner", Static).render())
            assert "2 rule" in banner

    async def test_lint_findings_counted_in_banner_only_when_lint_enabled(
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
                timeout: 360
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                room: general
        """)
        app_no_lint = ConfigToolApp(config_path, lint=False)
        async with app_no_lint.run_test() as pilot:
            await pilot.pause()
            banner = str(app_no_lint.screen.query_one("#banner", Static).render())
            assert "lint finding" not in banner

        app_lint = ConfigToolApp(config_path, lint=True)
        async with app_lint.run_test() as pilot:
            await pilot.pause()
            banner = str(app_lint.screen.query_one("#banner", Static).render())
            assert "lint finding" in banner


class TestStatusColumnPerEntity:
    """User-reported: the per-row Status column (OK/WARN/ERROR) only ever
    worked for connectors — agent/watcher rows always showed OK regardless
    of real problems, since a GatewayConfig.from_file() failure used to
    collapse into one entity_kind="global" Finding that StatusIndex
    (gateway/configtool/model.py) never attributes to any specific row.
    Now backed by gateway/config.py's collect_config() (fault-tolerant,
    per-entity), which validate_config() uses instead of catching a single
    from_file() exception."""

    async def test_agent_with_a_real_problem_shows_error_others_stay_ok(
        self, tmp_path, work_dir
    ):
        broken_dir = tmp_path / "does-not-exist"
        config_path = _write_config(tmp_path, f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              broken-agent:
                type: claude
                working_directory: {broken_dir}
              working-agent:
                type: claude
                working_directory: {work_dir}
            watcher_rules:
              - name: w1
                connector: rc
                agent: working-agent
                rooms:
                  include: [general]
        """)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            # A real per-entity problem now populates the tables normally
            # (not the global "unavailable" fallback) — confirms
            # collect_config() returned a usable partial config.
            table = app.screen.query_one("#agents-table", DataTable)
            assert table.row_count == 2
            rows = {table.get_row_at(i)[0]: table.get_row_at(i) for i in range(2)}
            assert "ERROR" in rows["broken-agent"][3]
            assert "OK" in rows["working-agent"][3]

    async def test_rule_with_a_real_problem_shows_error_others_still_display(
        self, tmp_path, work_dir
    ):
        """The old Watchers tab dropped a broken entry's rows entirely (no
        row, no explanation anywhere on the tab) and its Status keys never
        matched a rule's finding spellings, so what DID render showed OK.
        Every entry is a row now; the broken one wears its own error."""
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
              - name: bad-rule
                connector: rc
                agent: nonexistent-agent
                rooms:
                  include: [dev]
        """)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one("#rules-table", DataTable)
            assert table.row_count == 2
            rows = {table.get_row_at(i)[1]: table.get_row_at(i) for i in range(2)}
            assert "ERROR" in str(rows["bad-rule"][5])
            assert "OK" in str(rows["good-rule"][5])

            banner = str(app.screen.query_one("#banner", Static).render())
            assert "1 error(s)" in banner

    async def test_two_independently_broken_agents_both_show_error(
        self, tmp_path
    ):
        """The actual point of this change: BOTH agents' rows show ERROR in
        one pass, not just whichever one from_file() would have hit first."""
        broken_dir_1 = tmp_path / "nope1"
        broken_dir_2 = tmp_path / "nope2"
        config_path = _write_config(tmp_path, f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              agent1:
                type: claude
                working_directory: {broken_dir_1}
              agent2:
                type: claude
                working_directory: {broken_dir_2}
            watcher_rules:
              - connector: rc
                agent: agent1
                room: general
        """)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one("#agents-table", DataTable)
            assert table.row_count == 2
            for i in range(2):
                assert "ERROR" in table.get_row_at(i)[3]


class TestValidationDetailsModal:
    """User-reported: the banner only ever showed a bare count ("✗ 1
    error(s)") — the actual message text (e.g. "Agent 'x': working_directory
    is required") was computed by validate_config() but never surfaced
    anywhere, leaving no way to find out what to fix short of running
    `coop config validate` in a separate terminal. 'v' opens a
    MessageModal with the full text; the banner only advertises it (in the
    banner text itself, per the user's own request — not a permanent
    footer entry) when there's actually something to view."""

    async def test_missing_working_directory_shows_the_real_message_via_v(
        self, tmp_path, work_dir
    ):
        # A second, working agent alongside 'broken-agent' — keeps this
        # test's assertion focused on "exactly one real error, and 'v' shows
        # its message" rather than also exercising the (separately tested,
        # tests/unit/test_config_validate.py) "the only agent fails -> zero
        # agents left -> a SECOND, global 'must define at least one agent'
        # finding too" edge case.
        config_path = _write_config(tmp_path, f"""\
            agents:
              broken-agent:
                type: claude
              working-agent:
                type: claude
                working_directory: {work_dir}
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            watcher_rules:
              - name: w1
                connector: rc
                agent: working-agent
                rooms:
                  include: [general]
        """)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            banner = str(app.screen.query_one("#banner", Static).render())
            assert "1 error(s)" in banner
            assert "press 'v' to view details" in banner

            await pilot.press("v")
            await pilot.pause()

            assert isinstance(app.screen, MessageModal)
            body = str(app.screen.query_one("#message-body").render())
            assert "broken-agent" in body
            assert "working_directory is required" in body

            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)

    async def test_valid_config_banner_gives_no_view_details_hint(
        self, tmp_path, work_dir
    ):
        # A connector name unlikely to collide with any real
        # state.<name>.json left on this machine from an unrelated run —
        # _check_state_orphans() (gateway/config_validate.py) reads from a
        # fixed runtime dir, not one scoped to this test's tmp_path, so a
        # reused/common connector name here could spuriously pick up a
        # warning having nothing to do with this test.
        config_path = _write_config(tmp_path, f"""\
            agents:
              clean-agent:
                type: claude
                working_directory: {work_dir}
            connectors:
              - name: view-details-hint-test-conn-9f3a1c
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            watcher_rules:
              - name: w1
                connector: view-details-hint-test-conn-9f3a1c
                agent: clean-agent
                rooms:
                  include: [general]
        """)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            banner = str(app.screen.query_one("#banner", Static).render())
            assert "press 'v' to view details" not in banner

            await pilot.press("v")
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)  # no-op, nothing to show

    async def test_v_is_a_no_op_right_after_a_load_error(self, tmp_path):
        """The load_error banner already shows its full message inline —
        _last_validate_result is reset to None in that branch so 'v' can't
        pop up a stale modal from a PREVIOUS successful validate."""
        config_path = str(tmp_path / "does-not-exist.yaml")
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            banner = str(app.screen.query_one("#banner", Static).render())
            assert "does not currently load" in banner

            await pilot.press("v")
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)  # no-op, no crash


class TestArrowKeyTabSwitching:
    """User-requested: left/right switch tabs directly, even while focus is
    on the list itself (the default focus target now — see
    TestOverviewRender.test_focus_starts_on_the_list_not_the_tab_bar) —
    without the user needing to move focus onto the tab bar first."""

    async def test_right_switches_to_the_next_tab_and_focuses_its_table(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _valid_config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.query_one("TabbedContent").active == "tab-connectors"
            assert app.focused.id == "connectors-table"

            await pilot.press("right")
            await pilot.pause()

            assert app.screen.query_one("TabbedContent").active == "tab-agents"
            assert isinstance(app.focused, DataTable)
            assert app.focused.id == "agents-table"

    async def test_left_switches_to_the_previous_tab_and_focuses_its_table(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _valid_config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-rules"
            await pilot.pause()

            await pilot.press("left")
            await pilot.pause()

            assert app.screen.query_one("TabbedContent").active == "tab-agents"
            assert app.focused.id == "agents-table"

    async def test_right_wraps_from_the_last_tab_to_the_first(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _valid_config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-presets"
            await pilot.pause()

            await pilot.press("right")
            await pilot.pause()

            assert app.screen.query_one("TabbedContent").active == "tab-connectors"
            assert app.focused.id == "connectors-table"

    async def test_left_wraps_from_the_first_tab_to_the_last(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _valid_config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.query_one("TabbedContent").active == "tab-connectors"

            await pilot.press("left")
            await pilot.pause()

            assert app.screen.query_one("TabbedContent").active == "tab-presets"
            assert app.focused.id == "presets-table"

    async def test_left_right_take_priority_over_the_focused_tables_own_binding(
        self, tmp_path, work_dir
    ):
        """DataTable itself binds left/right to cell/column cursor movement
        — this must not win just because the table has focus (which it
        does, by default, now). priority=True on OverviewScreen's own
        bindings is what makes this correct."""
        config_path = _write_config(tmp_path, _valid_config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one("#connectors-table", DataTable)
            table.focus()
            await pilot.pause()

            await pilot.press("right")
            await pilot.pause()

            assert app.screen.query_one("TabbedContent").active == "tab-agents"

    async def test_clicking_a_tab_also_focuses_its_table(self, tmp_path, work_dir):
        """on_tabbed_content_tab_activated() fires for ANY way the active
        tab changes, not just the new left/right actions — switching via
        the TabbedContent.active reactive directly (what a mouse click
        does under the hood) must behave identically."""
        config_path = _write_config(tmp_path, _valid_config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-templates"
            await pilot.pause()

            assert isinstance(app.focused, DataTable)
            assert app.focused.id == "templates-table"


class TestDetailScreenNavigation:
    async def test_connector_row_pushes_and_pops_detail_screen(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _valid_config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one("#connectors-table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ConnectorDetailScreen)
            body = str(app.screen.query_one("#connector-detail-body", Static).render())
            assert "rc-home" in body
            assert "pw" not in body  # password must be masked

            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)

    async def test_agent_row_pushes_detail_screen_with_provenance(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _valid_config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one("#agents-table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, AgentDetailScreen)
            body = str(app.screen.query_one("#agent-detail-body", Static).render())
            assert "from 'standard'" in body
            assert "preset: readonly" in body

    async def test_selecting_a_rule_row_after_config_becomes_invalid_does_not_crash(
        self, tmp_path, work_dir
    ):
        """Regression (inherited from the Watchers tab): selecting a row
        after an external edit invalidated the file on disk used to crash
        the whole app. The Rules tab reads the raw in-memory document — no
        loader call sits on the selection path at all — so selection keeps
        working against the state the table was painted from, exactly like
        the Connectors/Agents tabs; save()'s validate gate remains the
        write-side backstop."""
        config_path = _write_config(tmp_path, _valid_config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one("#rules-table", DataTable)
            assert table.row_count == 1

            # Invalidate the file on disk without going through the app's
            # own reload path (mirrors an external process/editor).
            with open(config_path) as f:
                text = f.read()
            text = text.replace(f"working_directory: {work_dir}", "")
            with open(config_path, "w") as f:
                f.write(text)

            table.focus()
            table.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause()

            assert app.is_running is True
            assert isinstance(app.screen, RuleDetailScreen)

    async def test_rule_row_pushes_detail_view(self, tmp_path, work_dir):
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
              - name: standalone
                connector: rc
                agent: default
                rooms:
                  include: [dev]
        """)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one("#rules-table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, RuleDetailScreen)
            assert app.screen.mode == "view"
            body = str(app.screen.query_one("#rule-detail-body", Static).render())
            assert "standalone" in body
            assert "dev" in body

    async def test_template_row_pushes_detail_with_used_by_and_blast_radius(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _valid_config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one("#templates-table", DataTable)
            table.focus()
            # Row order: TEMPLATE_KINDS = (agent, connector, watcher) — row 0
            # is agent_templates.standard (this fixture has no watcher
            # templates), row 1 is connector_templates.standard.
            table.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, TemplateDetailScreen)
            assert app.screen.kind == "agent"
            body = str(app.screen.query_one("#template-detail-body", Static).render())
            assert "my-agent" in body  # "used by" — the only agent inheriting it

    async def test_preset_row_pushes_detail_showing_used_by(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _valid_config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one("#presets-table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ToolPresetsScreen)
            body = str(app.screen.query_one("#preset-detail-body", Static).render())
            assert "my-agent" in body


class TestEditorEscapeHatch:
    async def test_suspend_not_supported_is_caught_and_notified_not_crashed(
        self, tmp_path, work_dir
    ):
        """App.suspend() raises SuspendNotSupported under run_test()'s
        headless driver (confirmed empirically) — open_editor_and_reload()
        must catch that (or any) failure and notify rather than propagate."""
        config_path = _write_config(tmp_path, _valid_config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            # Must not raise.
            app.open_editor_and_reload()
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)

    async def test_malformed_editor_env_var_is_caught_and_notified_not_crashed(
        self, tmp_path, work_dir
    ):
        """Regression: resolve_editor_command() (shlex.split on $EDITOR) used
        to be called OUTSIDE the try/except meant to catch every editor-
        launch failure — an $EDITOR with an unbalanced quote raised ValueError
        before the try block was ever entered, crashing the app instead of
        producing the intended notification."""
        config_path = _write_config(tmp_path, _valid_config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            with patch.dict("os.environ", {"EDITOR": "vim '"}, clear=False):
                app.open_editor_and_reload()  # must not raise
            await pilot.pause()
            assert app.is_running is True
            assert isinstance(app.screen, OverviewScreen)

    async def test_reload_config_after_external_edit_refreshes_overview(
        self, tmp_path, work_dir
    ):
        """The part of the $EDITOR round-trip that matters for correctness —
        reloading from disk and repainting — tested directly, since the
        suspend+subprocess line itself can't run under a headless driver."""
        config_path = _write_config(tmp_path, _valid_config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            banner = str(app.screen.query_one("#banner", Static).render())
            assert "1 rule" in banner

            with open(config_path, "a") as f:
                f.write("  - name: extra\n    connector: rc-home\n"
                        "    agent: my-agent\n    rooms:\n      include: [extra]\n")

            app.reload_config()
            await pilot.pause()
            banner = str(app.screen.query_one("#banner", Static).render())
            assert "2 rule" in banner


class TestDirtyQuitGating:
    """ConfigToolApp.action_quit() (Phase 2 foundation: no edit screen exists
    yet to ever set `dirty` for real, but the gating mechanism itself is
    built now, alongside save()/dirty tracking, per docs/design/config-tool.md
    decision 5's ConfirmModal — exercised here via `mark_dirty()` directly,
    the same seam a real edit screen will use."""

    async def test_quit_with_no_unsaved_changes_exits_immediately_no_modal(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _valid_config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.editable_config.dirty is False

            await pilot.press("q")
            await pilot.pause()
            assert app.is_running is False

    async def test_quit_with_unsaved_changes_shows_confirm_modal(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _valid_config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.editable_config.mark_dirty()

            await pilot.press("q")
            await pilot.pause()
            assert app.is_running is True  # not exited yet — modal is up
            assert isinstance(app.screen, ConfirmModal)

    async def test_confirming_the_modal_discards_and_quits(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _valid_config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.editable_config.mark_dirty()

            await pilot.press("q")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)

            # Cancel holds focus by default (safe-by-default) — tab to the
            # Confirm button, then Enter presses whichever button is
            # focused (Button's own key handling, not a screen binding).
            await pilot.press("tab", "enter")
            await pilot.pause()
            assert app.is_running is False

    async def test_cancelling_the_modal_via_its_default_focused_button(
        self, tmp_path, work_dir
    ):
        """Enter with no other input presses the default-focused Cancel
        button — the safe outcome when a user just reflexively hits Enter."""
        config_path = _write_config(tmp_path, _valid_config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.editable_config.mark_dirty()

            await pilot.press("q")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)

            await pilot.press("enter")
            await pilot.pause()
            assert app.is_running is True
            assert isinstance(app.screen, OverviewScreen)

    async def test_cancelling_the_modal_keeps_the_app_running(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _valid_config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.editable_config.mark_dirty()

            await pilot.press("q")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)

            await pilot.press("escape")  # ConfirmModal's "cancel" binding
            await pilot.pause()
            assert app.is_running is True
            assert isinstance(app.screen, OverviewScreen)
            assert app.editable_config.dirty is True  # unsaved change untouched


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
