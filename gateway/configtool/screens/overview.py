"""OverviewScreen — the config TUI's root screen.

Five tabs: Connectors, Agents, Watcher Rules, Templates, Tool Presets — the
latter two are first-class per docs/design/config-tool.md (shared
resources, not footnotes). Selecting a row (Enter) pushes a *DetailScreen in
view mode. 'e'/'d' act directly on the row under the cursor — edit opens
straight into edit mode (no view detour), delete runs the same
confirm/referencing-check/save flow FormScreen.action_delete() already has,
without requiring a screen push first (user-reported: 'e' used to be
shadowed by this screen's OWN 'e' binding for the $EDITOR escape hatch —
see action_edit_config() below, now on ctrl+e). 'n' (new_entity) creates an
entry on the active tab. 'd' additionally deletes the whole preset under
the cursor on the Tool Presets tab (there's no separate "edit mode" to give
'e' a meaning there — see tool_presets.py), and the whole template under
the cursor on the Templates tab (same reasoning — a template has no
separate "edit mode" distinct from "edit this named entity," see
template_detail.py).

The Watcher Rules tab (design §5.5) shows one row per `watcher_rules:` RULE, keyed and
displayed by LIST INDEX — order is load-bearing (first match wins, §2.1),
which is why this is the one tab NOT sorted by name, and why '['/']' move
the rule under the cursor up/down (persisted immediately, like every other
direct list mutation here). The runtime side — which sessions each rule has
actually materialized — is deliberately NOT shown here: the config tool
operates on config.yaml only (owner decision 2026-08-18); `coop list` is the
runtime view.

The Templates tab (v0.3 redesign) replaced the old Defaults tab (a fixed,
un-creatable, 3-row-per-kind global-block view) — it's now a flat list of
NAMED templates across all three kinds (Kind/Name/Fields set/Used by
columns, mirroring the Tool Presets tab being one flat list), fully
create/edit/delete-able like every other tab, since the whole point of the
templates/inherits redesign is that multiple templates can coexist per kind.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.coordinate import Coordinate
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static, TabbedContent, TabPane

from ...config_validate import ValidationResult
from ..formatting import kind_label, markup_safe, status_badge
from ..modals import ConfirmModal, MessageModal, TextPromptModal, TypePickerModal
from ..model import StatusIndex
from .agent_detail import AgentDetailScreen
from .connector_detail import CONNECTOR_TYPE_PICKER_OPTIONS, ConnectorDetailScreen
from .form_common import find_agents_referencing_preset, find_entries_referencing_template
from .rule_detail import RuleDetailScreen, rule_rooms_summary
from .template_detail import TEMPLATE_KINDS, TemplateDetailScreen
from .tool_presets import ToolPresetsScreen

_AGENT_TYPES = ("claude", "opencode")

# Tab IDs in display order — used by action_previous_tab()/action_next_tab()
# to wrap around, and to look up each tab's own DataTable id for focusing.
_TAB_ORDER = ("tab-connectors", "tab-agents", "tab-rules", "tab-templates", "tab-presets")
_TABLE_ID_FOR_TAB = {
    "tab-connectors": "connectors-table",
    "tab-agents": "agents-table",
    "tab-rules": "rules-table",
    "tab-templates": "templates-table",
    "tab-presets": "presets-table",
}

if TYPE_CHECKING:
    from ..app import ConfigToolApp


class OverviewScreen(Screen):
    """Root screen — never popped (quitting the app pops the whole stack)."""

    BINDINGS = [
        # User-reported: this used to be 'e', shadowing the row-level direct-
        # edit shortcut below every time — pressing 'e' hoping to edit the
        # selected connector/agent instead opened $EDITOR on the whole
        # config.yaml. Moved to ctrl+e (clear of every other single-letter
        # list-page/detail-screen binding: e/d/r/n/q here, ctrl+s/ctrl+r/
        # ctrl+t on FormScreen).
        Binding("ctrl+e", "edit_config", "Edit in $EDITOR", show=True),
        Binding("r", "refresh", "Refresh"),
        # Screen already binds tab/shift+tab to app.focus_next/focus_previous
        # with show=False (textual/screen.py) — on mount, focus starts on the
        # tab bar itself, not the list, so surfacing this in the footer (same
        # action, just visible) is the fix for "how do I get into the list?"
        Binding("tab", "app.focus_next", "Focus next / enter list", show=True),
        # App already binds ctrl+q -> quit (show=False, Textual's own
        # default) — 'q' here is the documented, discoverable quit key (the
        # design's original intent, missed at first implementation). Scoped
        # to OverviewScreen (not detail screens) since phase 2/3 add text
        # Input widgets on those screens, where a bare 'q' typed into a field
        # must not quit the app.
        Binding("q", "app.quit", "Quit", show=True),
        # User-requested: every detail/form screen already uses Escape to go
        # back, and this is the one screen with nowhere further "back" to
        # go — pressing Escape here to quit (same as 'q') matches the habit
        # those other screens already train. show=False: 'q' above remains
        # the one documented, visible quit key; this is a silent alias for
        # it, same treatment ctrl+q already gets.
        Binding("escape", "app.quit", "Quit", show=False),
        Binding("n", "new_entity", "New", show=True),
        # User-reported: the banner only ever showed a bare count ("✗ 1
        # error(s)") — result.errors/warnings/lint_findings (the actual
        # message text, e.g. "Agent 'x': working_directory is required")
        # were computed but never surfaced anywhere, leaving the user no
        # way to find out what to fix short of running `AgentCoop
        # config validate` in a separate terminal. show=False (per the
        # user's own request) — the banner text itself says "press 'v' to
        # view details" only when there's actually something to show,
        # rather than permanently advertising a key that's usually a no-op.
        Binding("v", "view_validation_details", "View details", show=False),
        # Direct edit/delete on the row under the cursor (Connectors/Agents
        # tabs only — the only ones with a real detail-screen mode="edit"/
        # delete flow) — user-requested, to skip "select row -> view page ->
        # press e/d" for the common case of just wanting to edit or delete
        # one entry. check_action() below hides these on any other tab so
        # the footer doesn't advertise a no-op.
        Binding("e", "edit_row", "Edit", show=True),
        Binding("d", "delete_row", "Delete", show=True),
        # Rules tab only (see check_action() below): rule order is
        # load-bearing (first match wins), so the list must be able to
        # express it — without these, reordering means a trip to $EDITOR.
        # Plain printable keys, not shift+up/down: a modifier-arrow chord is
        # exactly the kind of binding real-terminal testing has already
        # shown to be unreliable here (see FormScreen's tab-binding comment).
        Binding("[", "move_rule_up", "Move rule up", show=True),
        Binding("]", "move_rule_down", "Move rule down", show=True),
        # User-requested: focus starts on the list itself (see on_mount()),
        # not the tab bar, so left/right must be able to switch tabs WITHOUT
        # the user first moving focus off the list. priority=True is
        # required: DataTable (which has focus in the common case now) has
        # its OWN left/right bindings (cell/column cursor movement), and the
        # binding chain used to resolve a keypress starts at the FOCUSED
        # widget and walks up — without priority=True, DataTable's own
        # binding would always win and this one would never even be
        # considered. (cursor_type="row" everywhere in this screen, so
        # DataTable's left/right never actually do anything useful today
        # regardless — but priority=True is what makes this correct even if
        # that ever changes, not an accident of DataTable being otherwise
        # idle on these keys.)
        Binding("left", "previous_tab", "Previous tab", show=True, priority=True),
        Binding("right", "next_tab", "Next tab", show=True, priority=True),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            yield Static(id="banner")
            with TabbedContent():
                with TabPane("Connectors", id="tab-connectors"):
                    yield DataTable(id="connectors-table", cursor_type="row")
                with TabPane("Agents", id="tab-agents"):
                    yield DataTable(id="agents-table", cursor_type="row")
                with TabPane("Watcher Rules", id="tab-rules"):
                    yield DataTable(id="rules-table", cursor_type="row")
                with TabPane("Templates", id="tab-templates"):
                    yield DataTable(id="templates-table", cursor_type="row")
                with TabPane("Tool Presets", id="tab-presets"):
                    yield DataTable(id="presets-table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        # Populated by repaint_from_memory(); action_view_validation_details()
        # reads it back so the 'v' keybinding doesn't have to recompute
        # validate_config() a second time.
        self._last_validate_result: ValidationResult | None = None
        for table_id in (
            "#connectors-table", "#agents-table", "#rules-table",
            "#templates-table", "#presets-table",
        ):
            self.query_one(table_id, DataTable).cursor_type = "row"
        self.repaint_from_memory()
        # User-requested: default focus straight to the list, not the tab
        # bar — "tab: Focus next / enter list" (the BINDINGS comment above)
        # was the previous fix for reaching the list at all; this goes
        # further and puts focus there immediately, so the very first
        # keypress (arrow keys to move the cursor, 'e'/'d' to act on a row)
        # already lands on the table with no extra step.
        self._focus_active_tab_table()

    def _focus_active_tab_table(self) -> None:
        active_tab = self.query_one(TabbedContent).active
        table_id = _TABLE_ID_FOR_TAB.get(active_tab)
        if table_id is not None:
            self.query_one(f"#{table_id}", DataTable).focus()

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        """Fires whenever the active tab changes, by ANY means — clicking a
        tab, action_previous_tab()/action_next_tab() below, or a future
        programmatic switch — so the list-focus behavior stays correct
        without needing to be re-applied at every call site that changes
        tabs. User-requested: switching tabs should always leave focus
        ready on that tab's list, not on the tab bar."""
        self._focus_active_tab_table()

    def on_screen_resume(self) -> None:
        """Fires whenever this screen becomes the active one again after a
        pushed screen is popped — including Escape out of ToolPresetsScreen,
        which (unlike every other mutating flow in this app) doesn't pop
        itself after a successful add/delete-rule, so there's no single
        "just popped, call reload_config()" call site to hang a repaint off
        of. Repainting here instead covers that case (and is a harmless,
        idempotent no-op on every other screen-pop path, which already
        repaints explicitly right before or after popping)."""
        self.repaint_from_memory()

    # ── Actions ──────────────────────────────────────────────────────────────

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Templates is fully editable/deletable now (every kind is a real,
        named, creatable entity — no more per-kind "nothing editable yet"
        gate the old Defaults tab needed for connector_defaults). Tool
        Presets: user-requested, for consistency with every other tab —
        'e' here is just an alias for Enter (see action_edit_row()'s own
        docstring; ToolPresetsScreen still has no separate "edit mode" to
        enter, see tool_presets.py). '['/']' (move rule up/down) only mean
        anything where order is load-bearing — the Rules tab."""
        active_tab = self.query_one(TabbedContent).active
        if action == "edit_row":
            return active_tab in (
                "tab-connectors", "tab-agents", "tab-rules", "tab-templates", "tab-presets",
            )
        if action == "delete_row":
            return active_tab in (
                "tab-connectors", "tab-agents", "tab-rules", "tab-templates", "tab-presets",
            )
        if action in ("move_rule_up", "move_rule_down"):
            return active_tab == "tab-rules"
        return True

    def action_edit_config(self) -> None:
        app: "ConfigToolApp" = self.app  # type: ignore[assignment]
        app.open_editor_and_reload()

    def action_previous_tab(self) -> None:
        """'left' — wraps from the first tab to the last. Setting `.active`
        (rather than any lower-level Tabs API) triggers TabbedContent's own
        TabActivated message the same way a mouse click would, so
        on_tabbed_content_tab_activated() re-focuses the list for us — one
        path for "the active tab changed," not a second one special-cased
        for the keyboard."""
        tabs = self.query_one(TabbedContent)
        index = _TAB_ORDER.index(tabs.active)
        tabs.active = _TAB_ORDER[(index - 1) % len(_TAB_ORDER)]

    def action_next_tab(self) -> None:
        """'right' — wraps from the last tab to the first. See
        action_previous_tab()'s docstring for why this only sets `.active`
        rather than also handling focus here directly."""
        tabs = self.query_one(TabbedContent)
        index = _TAB_ORDER.index(tabs.active)
        tabs.active = _TAB_ORDER[(index + 1) % len(_TAB_ORDER)]

    @work
    async def action_edit_row(self) -> None:
        """'e' on the Connectors/Agents tabs: open the row under the cursor
        DIRECTLY in edit mode — no view-mode detour. User-requested: the
        common case is "I know which entry I want to change," and having to
        select it, land on a read-only page, then press 'e' again was an
        extra, pointless step for that case. (Selecting a row via Enter into
        a read-only view first is still available and unchanged, e.g. for
        just double-checking a value.)

        Tool Presets: user-requested, for consistency with every other
        tab having an 'e' shortcut — but ToolPresetsScreen has no
        view/edit-mode distinction at all (its own module docstring: "there
        is no separate edit mode... every add/edit/remove is a direct,
        immediately-saved mutation"), so 'e' here is just an alias for
        Enter (same push as on_data_table_row_selected()'s own
        "presets-table" branch), not a mode-skip like the other tabs."""
        app: "ConfigToolApp" = self.app  # type: ignore[assignment]
        cfg = app.editable_config
        if cfg is None:
            self.notify("Config does not currently load.", severity="error")
            return

        active_tab = self.query_one(TabbedContent).active
        if active_tab == "tab-presets":
            key = self._cursor_row_key("presets-table")
            if key is None:
                return
            self.app.push_screen(ToolPresetsScreen(cfg, key))
            return
        if active_tab == "tab-connectors":
            key = self._cursor_row_key("connectors-table")
            if key is None:
                return
            entry = self._connector_entry_for_key(cfg, key)
            if entry is None:
                return
            screen = ConnectorDetailScreen(cfg, entry, mode="edit")
        elif active_tab == "tab-agents":
            key = self._cursor_row_key("agents-table")
            if key is None:
                return
            entry = cfg.agents_raw.get(key)
            if entry is None:
                return
            screen = AgentDetailScreen(cfg, key, entry, mode="edit")
        elif active_tab == "tab-rules":
            key = self._cursor_row_key("rules-table")
            if key is None:
                return
            entry = self._rule_entry_for_key(cfg, key)
            if entry is None:
                self._notify_malformed_rule_row(cfg, key)
                return
            # Composing the edit form with zero connectors/agents would
            # crash mid-compose (empty Select) — see
            # RuleDetailScreen.missing_prerequisites().
            message = RuleDetailScreen.missing_prerequisites(cfg)
            if message is not None:
                self.notify(message, severity="error")
                return
            screen = RuleDetailScreen(cfg, entry, mode="edit")
        elif active_tab == "tab-templates":
            row_key = self._cursor_row_key("templates-table")
            if row_key is None:
                return
            kind, name = row_key.split(":", 1)
            # raw_template(), NOT templates() — the latter strips
            # `description` (see its own docstring); TemplateDetailScreen
            # needs the raw entry so description survives edit/Save
            # round-trips (PR review finding).
            entry = cfg.raw_template(kind, name)
            if entry is None:
                return
            screen = TemplateDetailScreen(cfg, kind, name, entry, mode="edit")
        else:
            return
        screen._started_in_edit_mode = True
        self.app.push_screen(screen)

    @work
    async def action_delete_row(self) -> None:
        """'d' on the Connectors/Agents/Tool-Presets tabs: delete the row
        under the cursor directly.

        Connectors/Agents reuse FormScreen.action_delete()'s existing
        confirm/referencing-watcher-check/save flow verbatim (no
        reimplementation) — just triggered without a screen push first.

        Pushes the target screen (in view mode — action_delete() requires
        it, see its own check) SILENTLY, immediately invokes its delete
        action, then pops back out to the list regardless of outcome
        (confirmed, cancelled, or blocked by a referencing watcher) —
        action_delete() itself already pops the screen on a SUCCESSFUL
        delete, so the extra pop_screen() below only fires for the
        cancelled/blocked paths, where action_delete() deliberately leaves
        the screen in place (it was designed to be reached via view mode,
        where staying put makes sense — reached from here, staying put
        would leave the user looking at a screen they never asked to see).

        Tool Presets has no FormScreen/detail-screen delete flow to reuse
        (ToolPresetsScreen itself only edits ONE preset's rules, never
        deletes the whole preset — see its own module docstring), so this
        deletes the preset directly, inline, via _delete_preset_row().
        """
        app: "ConfigToolApp" = self.app  # type: ignore[assignment]
        cfg = app.editable_config
        if cfg is None:
            self.notify("Config does not currently load.", severity="error")
            return

        active_tab = self.query_one(TabbedContent).active
        if active_tab == "tab-presets":
            await self._delete_preset_row(cfg)
            return
        if active_tab == "tab-templates":
            await self._delete_template_row(cfg)
            return
        if active_tab == "tab-connectors":
            key = self._cursor_row_key("connectors-table")
            if key is None:
                return
            entry = self._connector_entry_for_key(cfg, key)
            if entry is None:
                return
            screen = ConnectorDetailScreen(cfg, entry, mode="view")
        elif active_tab == "tab-agents":
            key = self._cursor_row_key("agents-table")
            if key is None:
                return
            entry = cfg.agents_raw.get(key)
            if entry is None:
                return
            screen = AgentDetailScreen(cfg, key, entry, mode="view")
        elif active_tab == "tab-rules":
            key = self._cursor_row_key("rules-table")
            if key is None:
                return
            entry = self._rule_entry_for_key(cfg, key)
            if entry is None:
                # A non-mapping entry has no detail screen to route the
                # delete through, but it still deserves 'd' — it renders as
                # an ERROR row on purpose, and "visible but unremovable" is
                # half a fix (Codex review of #129, round 3).
                await self._delete_malformed_rule_entry(cfg, key)
                return
            screen = RuleDetailScreen(cfg, entry, mode="view")
        else:
            return

        self.app.push_screen(screen)
        # Call _do_delete() directly (a plain coroutine) rather than the
        # @work-decorated action_delete() — nesting a second @work worker
        # inside this one (via action_delete().wait()) turned out to be
        # fragile: if this outer worker gets cancelled while the inner one
        # is suspended at a push_screen_wait(), Worker.wait() re-raises
        # that as WorkerCancelled INSIDE this method, an unrelated-looking
        # crash. _do_delete() has the exact same logic, just callable
        # without going through the worker system a second time.
        await screen._do_delete()
        if self.app.screen is screen:
            # Cancelled, or blocked by a referencing watcher — action_delete()
            # left the screen in place (correct for its OWN view-mode entry
            # point). Reached from the list directly, staying here would
            # strand the user on a screen they never asked to see — send
            # them back to the list instead, same as Escape would.
            self.app.pop_screen()

    async def _delete_malformed_rule_entry(self, cfg, key: str) -> None:
        """Delete a NON-MAPPING `watcher_rules:` entry by its list index — the one
        row shape RuleDetailScreen cannot represent (there is no dict to
        open a form on). Inline confirm + save + rollback, same shape as
        _delete_preset_row(). A stale index (the table shrank on disk since
        the paint) or a row that IS a mapping falls through silently — the
        mapping case is handled by the normal detail-screen path."""
        watchers = cfg.watcher_entries
        index = int(key)
        if not (0 <= index < len(watchers)) or isinstance(watchers[index], dict):
            return
        confirmed = await self.app.push_screen_wait(
            ConfirmModal(
                f"Row #{index + 1} is not a rule at all (a malformed, "
                "non-mapping entry — often stray YAML). Delete it? This "
                "cannot be undone.",
                confirm_label="Delete",
            )
        )
        if not confirmed:
            return
        was_dirty = cfg.dirty  # see _move_rule()'s note
        removed = watchers.pop(index)
        cfg.mark_dirty()
        try:
            cfg.save()
        except (ValueError, FileNotFoundError) as exc:
            watchers.insert(index, removed)
            cfg.dirty = was_dirty
            # Same known limitation the reorder refusal carries, and the
            # same owner ruling (2026-08-19): a removal renumbers every
            # LATER entry, so another broken rule below this row has its
            # index-embedded error message shift and the gate reads the
            # pre-existing problem as newly introduced. Not fixed at the
            # gate (that is the parser-message contract, out of this
            # increment); made honest here and documented in
            # docs/config-tool.md — repair several broken rows bottom-up,
            # or fix them in one $EDITOR pass.
            await self.app.push_screen_wait(
                MessageModal(
                    "Could not delete — another rule further down the file "
                    "has a pre-existing error, and removing this row shifts "
                    "its position, which the save safety-gate reads as a new "
                    "problem. Delete the LOWEST ERROR row first, or fix them "
                    f"together in $EDITOR (ctrl+e).\n\n{markup_safe(exc)}",
                    title="Could not delete",
                )
            )
            return
        self.notify("Deleted the malformed entry.", severity="information")
        app: "ConfigToolApp" = self.app  # type: ignore[assignment]
        app.reload_config()

    def _notify_malformed_rule_row(self, cfg, key: str) -> bool:
        """True (and a pointer notify) when the row under `key` is a
        non-mapping entry — Enter/'e' have no form to open for it, and a
        silent no-op reads as a dead key (Codex review of #129, round 3)."""
        watchers = cfg.watcher_entries
        index = int(key)
        if 0 <= index < len(watchers) and not isinstance(watchers[index], dict):
            self.notify(
                "This entry is not a rule mapping — there is no form to "
                "open. Press 'd' to delete it, or repair it in $EDITOR "
                "(ctrl+e).",
                severity="warning",
            )
            return True
        return False

    def action_move_rule_up(self) -> None:
        self._move_rule(-1)

    def action_move_rule_down(self) -> None:
        self._move_rule(+1)

    def _move_rule(self, delta: int) -> None:
        """'['/']' on the Rules tab: swap the rule under the cursor with its
        neighbour and persist immediately (same direct-mutation-then-save
        shape as _delete_preset_row()). Rule order is load-bearing — first
        match wins — so a move is a REAL semantic change, not cosmetics; it
        goes through save()'s validate-before-write gate like every other
        mutation, and a rejected save swaps straight back. On success the
        cursor follows the rule to its new position (row coordinate ==
        list index on this tab, the one tab displayed in document order)."""
        app: "ConfigToolApp" = self.app  # type: ignore[assignment]
        cfg = app.editable_config
        if cfg is None:
            self.notify("Config does not currently load.", severity="error")
            return
        if self.query_one(TabbedContent).active != "tab-rules":
            return
        key = self._cursor_row_key("rules-table")
        if key is None:
            return
        index = int(key)
        # Captured BEFORE the mutation: rolling the document back restores it
        # byte-for-byte, but both the move and the counter-move call
        # mark_dirty(), so the flag stayed True and the quit gate then asked
        # the operator to discard changes that no longer exist (Codex review
        # of #129, round 8).
        was_dirty = cfg.dirty
        new_index = cfg.move_watcher_rule(index, delta)
        if new_index is None:
            return  # already at the edge — nothing to do
        try:
            cfg.save()
        except (ValueError, FileNotFoundError) as exc:
            cfg.move_watcher_rule(new_index, -delta)  # swap straight back
            cfg.dirty = was_dirty
            # Honest wording (owner-ratified): a pure swap reorders the same
            # entries, and per-entry parse errors are order-independent — so
            # a refusal here is BY CONSTRUCTION a pre-existing broken rule
            # whose index-embedded message shifted, never a problem this
            # move created. Say so, instead of letting the gate's generic
            # "introduces a new problem" read as "your move broke something".
            # "This or another" (Codex review of #129): ERROR rows keep the
            # move bindings, so the broken rule under the cursor itself is a
            # normal way to arrive here — blaming "another rule" then sends
            # the user hunting for a culprit that is the row they're on.
            self.notify(
                "Could not move rule — a rule with a pre-existing error "
                "(this one or another; see the ERROR rows) blocks "
                f"reordering. Fix it first: {exc}",
                severity="error",
            )
            return
        app.reload_config()
        table = self.query_one("#rules-table", DataTable)
        if 0 <= new_index < table.row_count:
            table.move_cursor(row=new_index)

    async def _delete_preset_row(self, cfg) -> None:
        """Delete the WHOLE preset under the cursor on the Tool Presets tab
        (not a single rule — see ToolPresetsScreen's own module docstring
        for why deleting one preset's rules never deletes the preset
        itself). Checks find_agents_referencing_preset() FIRST, same
        pre-check-before-destructive-confirm pattern
        find_referencing_watcher_labels() gives connectors/agents — a
        blocked delete gets a clear, specific reason instead of a generic
        validator error."""
        key = self._cursor_row_key("presets-table")
        if key is None or key not in cfg.tool_presets_raw:
            return

        used_by = find_agents_referencing_preset(cfg, key)
        if used_by:
            await self.app.push_screen_wait(
                MessageModal(
                    f"Cannot delete tool preset '{markup_safe(key)}' — still used "
                    f"by agent(s): {', '.join(markup_safe(u) for u in used_by)}.",
                    title="Cannot delete",
                )
            )
            return

        confirmed = await self.app.push_screen_wait(
            ConfirmModal(
                f"Delete tool preset '{markup_safe(key)}'? This cannot be undone.",
                confirm_label="Delete",
            )
        )
        if not confirmed:
            return

        presets = cfg.document.get("tool_presets", {})
        removed = presets.pop(key, None)
        cfg.mark_dirty()
        try:
            cfg.save()
        except (ValueError, FileNotFoundError) as exc:
            if removed is not None:
                presets[key] = removed
            await self.app.push_screen_wait(MessageModal(markup_safe(exc), title="Could not delete"))
            return

        self.notify(f"Deleted tool preset '{key}'.", severity="information")
        app: "ConfigToolApp" = self.app  # type: ignore[assignment]
        app.reload_config()

    async def _delete_template_row(self, cfg) -> None:
        """Delete the WHOLE named template under the cursor on the Templates
        tab — direct analogue of _delete_preset_row() above (a template has
        no separate "edit mode" distinct from "edit this named entity," same
        as a preset). Checks find_entries_referencing_template() FIRST, same
        pre-check-before-destructive-confirm pattern used everywhere else in
        this screen — a blocked delete gets a clear, specific reason instead
        of a generic validator error."""
        row_key = self._cursor_row_key("templates-table")
        if row_key is None:
            return
        kind, name = row_key.split(":", 1)
        if name not in cfg.templates(kind):
            return

        used_by = [n for n, _ in find_entries_referencing_template(cfg, kind, name)]
        if used_by:
            await self.app.push_screen_wait(
                MessageModal(
                    f"Cannot delete {kind_label(kind)} template '{markup_safe(name)}' — still "
                    f"used by {kind_label(kind)}(s): "
                    f"{', '.join(markup_safe(u) for u in used_by)}.",
                    title="Cannot delete",
                )
            )
            return

        confirmed = await self.app.push_screen_wait(
            ConfirmModal(
                f"Delete {kind_label(kind)} template '{markup_safe(name)}'? This cannot be undone.",
                confirm_label="Delete",
            )
        )
        if not confirmed:
            return

        templates = cfg.document.get(f"{kind}_templates", {})
        removed = templates.pop(name, None)
        cfg.mark_dirty()
        try:
            cfg.save()
        except (ValueError, FileNotFoundError) as exc:
            if removed is not None:
                templates[name] = removed
            await self.app.push_screen_wait(MessageModal(markup_safe(exc), title="Could not delete"))
            return

        self.notify(f"Deleted {kind_label(kind)} template '{name}'.", severity="information")
        app: "ConfigToolApp" = self.app  # type: ignore[assignment]
        app.reload_config()

    @work
    async def action_new_entity(self) -> None:
        """'n' — scoped to whichever tab is active. Agents, Connectors,
        Rules, and Tool Presets support creation. Unsupported tabs just
        notify, rather than doing nothing silently or crashing."""
        app: "ConfigToolApp" = self.app  # type: ignore[assignment]
        if app.editable_config is None:
            self.notify("Config does not currently load — nothing to add to.", severity="error")
            return

        active_tab = self.query_one(TabbedContent).active
        if active_tab == "tab-agents":
            agent_type = await self.app.push_screen_wait(
                TypePickerModal("New agent — pick a type", list(_AGENT_TYPES))
            )
            if agent_type is None:
                return
            self.app.push_screen(
                AgentDetailScreen(app.editable_config, "", {"type": agent_type}, mode="create")
            )
        elif active_tab == "tab-connectors":
            connector_type = await self.app.push_screen_wait(
                TypePickerModal("New connector — pick a type", CONNECTOR_TYPE_PICKER_OPTIONS)
            )
            if connector_type is None:
                return
            self.app.push_screen(
                ConnectorDetailScreen(app.editable_config, {"type": connector_type}, mode="create")
            )
        elif active_tab == "tab-rules":
            # No type picker, no EntityPickerModal detour — connector/agent
            # are two plain Select dropdowns directly in the create form
            # itself (docs/design/config-tool.md's Phase 3 owner decision),
            # same as everything else this screen needs to know. Which is
            # also why creation needs both to exist first — an empty Select
            # crashes at compose (see missing_prerequisites()).
            message = RuleDetailScreen.missing_prerequisites(app.editable_config)
            if message is not None:
                self.notify(message, severity="error")
                return
            self.app.push_screen(RuleDetailScreen(app.editable_config, None, mode="create"))
        elif active_tab == "tab-presets":
            # No document/disk write here — a brand-new preset only
            # actually materializes once the first rule is added inside
            # ToolPresetsScreen (see its module docstring), so escaping out
            # without adding anything leaves no trace.
            name = await self.app.push_screen_wait(TextPromptModal("New tool preset — name"))
            if name is None:
                return
            if name in app.editable_config.tool_presets_raw:
                self.notify(f"A tool preset named '{name}' already exists.", severity="error")
                return
            self.app.push_screen(ToolPresetsScreen(app.editable_config, name))
        elif active_tab == "tab-templates":
            kind = await self.app.push_screen_wait(
                # (value, label) pairs, not bare strings: the picker must offer
                # "watcher rule" while still dismissing the `watcher` that names
                # the `watcher_templates:` key.
                TypePickerModal(
                    "New template — pick a kind",
                    [(k, kind_label(k)) for k in TEMPLATE_KINDS],
                )
            )
            if kind is None:
                return
            entry: dict = {}
            if kind == "connector":
                connector_type = await self.app.push_screen_wait(
                    TypePickerModal(
                        "New connector template — pick a type", CONNECTOR_TYPE_PICKER_OPTIONS
                    )
                )
                if connector_type is None:
                    return
                entry = {"type": connector_type}
            name = await self.app.push_screen_wait(
                TextPromptModal(f"New {kind_label(kind)} template — name")
            )
            if name is None:
                return
            if name in app.editable_config.templates(kind):
                self.notify(
                    f"A {kind_label(kind)} template named '{name}' already exists.",
                    severity="error",
                )
                return
            # No document/disk write here either — same precedent as Agent/
            # ConnectorDetailScreen's own create mode: nothing materializes
            # until the form actually saves.
            self.app.push_screen(
                TemplateDetailScreen(app.editable_config, kind, name, entry, mode="create")
            )
        else:
            self.notify("Creating a new entry isn't supported on this tab yet.", severity="warning")

    def action_refresh(self) -> None:
        # Must go through app.reload_config() (re-reads EditableConfig.document
        # from disk), NOT call self.repaint_from_memory() directly — that only
        # repaints from whatever EditableConfig already has in memory, which
        # run_validate() (reading the file fresh internally) can silently
        # disagree with once the file has changed on disk since app startup.
        app: "ConfigToolApp" = self.app  # type: ignore[assignment]
        app.reload_config()

    @work
    async def action_view_validation_details(self) -> None:
        """'v' — the actual message text behind the banner's bare count
        (user-reported: no way to find out WHAT to fix without running
        `coop config validate` in a separate terminal). A
        no-op if there's nothing to show (result is None — e.g. right after
        an app.load_error, which already shows its full message inline —
        or a clean validate with lint off)."""
        result = self._last_validate_result
        if result is None:
            return
        app: "ConfigToolApp" = self.app  # type: ignore[assignment]
        sections: list[str] = []
        # The section headers are this screen's own markup; the messages are
        # NOT — they quote the operator's own entity names and values back
        # (an unknown template, a bad pattern), so each one is escaped. This
        # is the view whose entire job is explaining what is wrong, which
        # makes it the worst possible place to fail on the wrongness itself.
        if result.errors:
            sections.append(
                "[bold red]Errors:[/bold red]\n"
                + "\n".join(f"  • {markup_safe(e)}" for e in result.errors)
            )
        if result.warnings:
            sections.append(
                "[bold yellow]Warnings:[/bold yellow]\n"
                + "\n".join(f"  • {markup_safe(w)}" for w in result.warnings)
            )
        if app.lint and result.lint_findings:
            sections.append(
                "[bold cyan]Lint findings:[/bold cyan]\n"
                + "\n".join(f"  • {markup_safe(lf)}" for lf in result.lint_findings)
            )
        if not sections:
            return
        await self.app.push_screen_wait(
            MessageModal("\n\n".join(sections), title="Validation details")
        )

    # ── Core refresh logic (the one testable seam per docs/design) ──────────

    def repaint_from_memory(self) -> None:
        """Redraw every tab from EditableConfig's CURRENT in-memory document —
        does not touch disk. Name is deliberate (code review item 9: the prior
        name `refresh_overview` invited exactly the bug action_refresh's
        comment above warns against — reaching for "the refresh method" and
        getting a stale repaint instead of a disk reload). Call
        `app.reload_config()` when the on-disk file may have changed."""
        app: "ConfigToolApp" = self.app  # type: ignore[assignment]
        banner = self.query_one("#banner", Static)

        connectors_table = self.query_one("#connectors-table", DataTable)
        agents_table = self.query_one("#agents-table", DataTable)
        rules_table = self.query_one("#rules-table", DataTable)
        templates_table = self.query_one("#templates-table", DataTable)
        presets_table = self.query_one("#presets-table", DataTable)
        for table in (connectors_table, agents_table, rules_table, templates_table, presets_table):
            table.clear(columns=True)

        if app.load_error is not None:
            self._last_validate_result = None
            banner.update(
                # The loader's own message, which quotes the file's content
                # back (a YAML syntax error, an offending value). This is the
                # config-does-not-load path — the one moment the operator has
                # nothing else to go on — so it is the worst place to fail on
                # the very text being reported. Found by the static markup
                # check, not by review: no walk-through reaches this banner,
                # because every behavioural fixture loads.
                f"[red]✗ config.yaml does not currently load:[/red] "
                f"{markup_safe(app.load_error)}"
            )
            return

        cfg = app.editable_config
        result = app.run_validate()
        self._last_validate_result = result

        if result.ok:
            summary = f"[green]✓ valid[/green] — {result.watcher_count} rule(s)"
        else:
            summary = f"[red]✗ {len(result.errors)} error(s)[/red]"
        if result.warnings:
            summary += f", {len(result.warnings)} warning(s)"
        has_lint = app.lint and result.lint_findings
        if has_lint:
            summary += f", {len(result.lint_findings)} lint finding(s)"
        # User-reported: the count alone gave no way to find out WHAT to
        # fix short of running `coop config validate`
        # separately — the actual message text (result.errors/warnings/
        # lint_findings) was computed but never shown anywhere. Only
        # advertised inline, in the banner itself, when there's actually
        # something to view — not a permanent footer entry for a key
        # that's usually a no-op (see the 'v' Binding's own comment).
        if not result.ok or result.warnings or has_lint:
            summary += "  [dim](press 'v' to view details)[/dim]"
        banner.update(summary)

        status = StatusIndex(result.findings)

        # Each table is populated defensively: run_validate() already caught
        # any GatewayConfig.from_file failure into `result` (shown in the
        # banner above), but several accessors here call the real loader
        # AGAIN independently (merged_entry/templates both replay
        # _parse_templates_block/_resolve_inherits) — the exact same failure
        # would otherwise raise a second, unhandled time here.

        # Keyed by list POSITION, not by name — unlike agents_raw (a dict,
        # inherently-unique keys), connectors_raw is the raw, pre-
        # validation list: two connectors can share a name, or both be
        # missing one (falling back to "?"), and Textual's DataTable.add_row
        # raises DuplicateKey on a repeated key — exactly the kind of config
        # mistake this tool exists to surface gracefully, not crash on.
        # Every table below EXCEPT Rules is sorted by name (user-requested —
        # a create flow can insert a new row anywhere in the underlying
        # list/dict, making a row hard to spot again by scrolling; sorting
        # display order makes it easy to find regardless of where it landed
        # in the raw document). Rules is the deliberate exception: its order
        # IS its semantics (first match wins), so it displays in document
        # order — which also makes row coordinate == list index there, the
        # property _move_rule() relies on. The row `key=` a cursor's action
        # resolves against stays the entry's own stable identity (list index
        # for connectors and rules, its own name for everything else) —
        # sorting elsewhere only changes DISPLAY order, never what a key
        # refers back to.
        connectors_table.add_columns("Name", "Type", "Status")
        for i, c in sorted(enumerate(cfg.connectors_raw), key=lambda pair: pair[1].get("name", "?")):
            name = c.get("name", "?")
            try:
                merged = cfg.merged_entry("connector", c)
            except (ValueError, FileNotFoundError):
                merged = c
            connectors_table.add_row(
                markup_safe(name), markup_safe(merged.get("type", "?")),
                status_badge(status.status_for("connector", name)),
                key=str(i),
            )

        agents_table.add_columns("Name", "Type", "Command", "Status")
        for name, entry in sorted(cfg.agents_raw.items()):
            try:
                merged = cfg.merged_entry("agent", entry)
            except (ValueError, FileNotFoundError):
                merged = entry
            agents_table.add_row(
                markup_safe(name),
                markup_safe(merged.get("type", "claude")),
                markup_safe(merged.get("command", "claude")),
                status_badge(status.status_for("agent", name)),
                key=name,
            )

        # One row per raw rule, in DOCUMENT order (never sorted — see the
        # comment block above), keyed by list index. The UNFILTERED document
        # list, not `watchers_raw` (which drops non-mapping entries) —
        # row/validator/move indices must all refer to the same positions,
        # and the validator numbers the unfiltered list. A malformed entry
        # still gets its row: its Status column carries the error (via
        # status_for_rule()'s three-spelling bridge), which is the whole
        # point — the previous Watchers tab silently dropped broken entries
        # AND every rule, leaving the table contradicting the banner.
        rules_table.add_columns("#", "Name", "Connector", "Agent", "Rooms", "Status")
        for i, raw in enumerate(cfg.watcher_entries):
            entry = raw if isinstance(raw, dict) else {}
            try:
                merged = cfg.merged_entry("watcher", entry)
            except (ValueError, FileNotFoundError):
                merged = entry
            name = entry.get("name")
            # DataTable cells parse markup too (that is how status_badge
            # renders), so every operator-authored cell is escaped — a
            # character-class pattern like `eng-[ab]` otherwise displayed as
            # `eng-`, concealing the rule's real routing (see markup_safe()).
            rules_table.add_row(
                str(i + 1),
                markup_safe(name) if isinstance(name, str) and name else "?",
                markup_safe(merged.get("connector") or "(default)"),
                markup_safe(merged.get("agent") or "(default)"),
                # MERGED, like connector/agent above it: a rule taking its
                # matcher from a template has no `rooms` of its own, and reading
                # the raw entry showed "(none)" for a rule that serves rooms.
                markup_safe(rule_rooms_summary(merged)),
                status_badge(status.status_for_rule(i, entry)),
                key=str(i),
            )

        templates_table.add_columns("Kind", "Name", "Fields set", "Used by")
        for kind in TEMPLATE_KINDS:
            try:
                templates = cfg.templates(kind)
            except (ValueError, FileNotFoundError):
                templates = {}
            for name, block in sorted(templates.items()):
                used_by = [n for n, _ in find_entries_referencing_template(cfg, kind, name)]
                templates_table.add_row(
                    # Displayed label; the ROW KEY keeps the internal kind, which
                    # is what edit/delete split back out to reach the document.
                    kind_label(kind), markup_safe(name), str(len(block)), str(len(used_by)),
                    key=f"{kind}:{name}",
                )

        presets_table.add_columns("Name", "Rules")
        for name, rules in sorted(cfg.tool_presets_raw.items()):
            presets_table.add_row(markup_safe(name), str(len(rules)), key=name)

    # ── Row selection → push detail screens ──────────────────────────────────

    def _cursor_row_key(self, table_id: str) -> str | None:
        """The row key under the cursor for the given table, or None if the
        table is empty/cursor isn't on a valid row — shared by the direct
        edit/delete actions below and (indirectly, via the same key lookup
        logic) on_data_table_row_selected()."""
        table = self.query_one(f"#{table_id}", DataTable)
        if table.row_count == 0:
            return None
        try:
            cell_key = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0))
        except Exception:
            return None
        return str(cell_key.row_key.value)

    def _connector_entry_for_key(self, cfg, key: str) -> dict | None:
        # key is the row's list position (see repaint_from_memory) — not
        # the connector's name, which isn't guaranteed unique/present.
        connectors = cfg.connectors_raw
        index = int(key)
        if 0 <= index < len(connectors):
            return connectors[index]
        return None

    def _rule_entry_for_key(self, cfg, key: str) -> dict | None:
        """The raw rule dict for a rules-table row key (its document list
        position — see repaint_from_memory()) — shared by Enter/edit/delete/
        move. The UNFILTERED document list, matching how the rows were
        painted. None for a stale index (table painted before an external
        shrink) or a non-mapping entry (RuleDetailScreen has nothing to
        show for it; its row's Status column already explains)."""
        watchers = cfg.watcher_entries
        index = int(key)
        if 0 <= index < len(watchers) and isinstance(watchers[index], dict):
            return watchers[index]
        return None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        app: "ConfigToolApp" = self.app  # type: ignore[assignment]
        cfg = app.editable_config
        if cfg is None:
            return
        table_id = event.data_table.id
        key = str(event.row_key.value)

        if table_id == "connectors-table":
            entry = self._connector_entry_for_key(cfg, key)
            if entry is not None:
                self.app.push_screen(ConnectorDetailScreen(cfg, entry, mode="view"))
        elif table_id == "agents-table":
            entry = cfg.agents_raw.get(key)
            if entry is not None:
                self.app.push_screen(AgentDetailScreen(cfg, key, entry, mode="view"))
        elif table_id == "rules-table":
            entry = self._rule_entry_for_key(cfg, key)
            if entry is not None:
                self.app.push_screen(RuleDetailScreen(cfg, entry, mode="view"))
            else:
                self._notify_malformed_rule_row(cfg, key)
        elif table_id == "templates-table":
            kind, name = key.split(":", 1)
            # raw_template(), NOT templates() — see action_edit_row()'s
            # identical comment.
            entry = cfg.raw_template(kind, name)
            if entry is not None:
                self.app.push_screen(TemplateDetailScreen(cfg, kind, name, entry, mode="view"))
        elif table_id == "presets-table":
            self.app.push_screen(ToolPresetsScreen(cfg, key))
