"""RuleDetailScreen — view, edit, and create a single watcher RULE.

A `watcher_rules:` entry is a rule (gateway/core/watcher_rule.py): a required
unique `name`, a `connector`/`agent` pair, and a `rooms:` matcher
(`include`/`except_for` globs plus the `direct`/`group_direct` DM opt-ins).
It names no room — which rooms it claims is only known at runtime — so this
screen is a plain one-entry-in/one-entry-out form over one element of the
`document["watcher_rules"]` list, using the exact trial-entry install/rollback
pattern `ConnectorDetailScreen` already uses for the connectors list. The
merge-on-add / split-on-edit machinery the old expanded-watcher screen
needed does not exist here because the problem it solved (N rooms sharing
one raw dict) no longer exists as data.

`name` IS editable, unlike a connector's (which watchers reference by
name). Nothing in config.yaml references a rule by name; persisted session
records do (`rule_name`), but a rename is equivalent to delete+create from
the runtime's perspective and both halves of that are legal operations —
the delete-rule warning below is the same disclosure a rename implies.

Deleting a rule warns with what it strands (design §5.5): the persisted
session records carrying this rule's name and the scheduled jobs targeting
those records' watchers — counted read-only off the daemon's own files
(gateway/configtool/state_peek.py), never via the control socket (owner
decision 2026-08-18: the config tool operates on config.yaml only).
"""

from __future__ import annotations

from typing import Literal

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import Button, Input, Select, Static

from ...config import HistoryHandoffConfig
from ...core.watcher_rule import RoomMatcher, WatcherRule
from ..formatting import format_value, markup_safe, provenance_label
from ..modals import ConfirmModal, InheritsPickerModal, MessageModal, TextPromptModal
from ..model import EditableConfig
from ..state_peek import stranded_by_rule
from .form_common import (
    FieldSpec,
    FormScreen,
    apply_update,
    read_widget_value,
    sort_required_first,
    widget_id,
)

# The `rooms:` matcher fields. Shared with a watcher-rule TEMPLATE, not
# rule-only: `gateway/config.py` used to forbid `rooms` on a template, but that
# restriction existed only because the loader decided whether an entry was a
# rule by looking for a `rooms:` mapping on the RAW entry, before templates were
# merged (see TEMPLATE_FORBIDDEN_KEYS there). The `watcher_rules:` rename
# removed that test and the restriction with it, so `rooms` is now inheritable
# and belongs in the template form — an exclusion every rule should carry is
# exactly the kind of thing a template is for. What sharing it does and does
# not do is documented under "Templates and `rooms` inheritance" in
# docs/user-guide.md.
_ROOMS_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("rooms.include", "list", "Rooms: include patterns (comma-separated)"),
    FieldSpec("rooms.except_for", "list", "Rooms: except-for patterns (comma-separated)"),
    FieldSpec("rooms.direct", "bool", "Rooms: claim 1:1 DMs (direct)"),
    FieldSpec("rooms.group_direct", "bool", "Rooms: claim group DMs (group_direct)"),
)

# A watcher template's own field list — reused by TemplateDetailScreen, and by
# the rule form below, which adds only its three identity fields on top.
# `name` is the one key a template may not carry: it pins one SPECIFIC rule.
# Everything else a rule has is legitimately shareable, including the two
# session TTLs (first-class rule fields since the dynamic-watcher cutover) and
# the `rooms:` matcher above.
WATCHER_TEMPLATE_FIELDS: tuple[FieldSpec, ...] = (
    *_ROOMS_FIELDS,
    FieldSpec("session_idle_days", "int", "Session idle days"),
    FieldSpec("session_expire_days", "int", "Session expire days"),
    FieldSpec("context_inject_files", "list", "Context inject files (comma-separated)"),
    FieldSpec("history_handoff.enabled", "bool", "History handoff enabled"),
    FieldSpec("history_handoff.fetch_count", "int", "History handoff fetch count"),
    FieldSpec("history_handoff.verbatim_tail", "int", "History handoff verbatim tail"),
)
# Defaults are read live from the owning dataclasses (HistoryHandoffConfig,
# WatcherRule) rather than re-typed as literals, so this preview can never
# drift out of sync with the loader — the drift already happened once
# (commit 31f966d flipped only the dataclass default and missed the loader).
_HH_DEFAULTS = HistoryHandoffConfig()
_RULE_FIELD_DEFAULTS = WatcherRule.__dataclass_fields__
# Same rule for the matcher: `RoomMatcher`'s own field defaults, so "no
# patterns, no DM opt-in" cannot be asserted here while the loader means
# something else.
_ROOM_DEFAULTS = RoomMatcher()

_WATCHER_TEMPLATE_DEFAULT_VALUES: dict[str, object] = {
    "rooms.include": list(_ROOM_DEFAULTS.include),
    "rooms.except_for": list(_ROOM_DEFAULTS.except_for),
    "rooms.direct": _ROOM_DEFAULTS.direct,
    "rooms.group_direct": _ROOM_DEFAULTS.group_direct,
    "session_idle_days": _RULE_FIELD_DEFAULTS["session_idle_days"].default,
    "session_expire_days": _RULE_FIELD_DEFAULTS["session_expire_days"].default,
    "context_inject_files": [],
    "history_handoff.enabled": _HH_DEFAULTS.enabled,
    "history_handoff.fetch_count": _HH_DEFAULTS.fetch_count,
    "history_handoff.verbatim_tail": _HH_DEFAULTS.verbatim_tail,
}
# The key set is derived from WATCHER_TEMPLATE_FIELDS so the two halves of
# what is really one table cannot drift apart — a spec without a default
# raises KeyError at import, not at some later render.
WATCHER_TEMPLATE_DATACLASS_DEFAULTS: dict[str, object] = {
    spec.key: _WATCHER_TEMPLATE_DEFAULT_VALUES[spec.key]
    for spec in WATCHER_TEMPLATE_FIELDS
}

_RULE_REQUIRED_FIELD_KEYS = frozenset({"name", "connector", "agent"})


def rule_rooms_summary(entry: dict) -> str:
    """One-line summary of an entry's `rooms:` matcher for table rows and the
    view body — e.g. `general, dev-* (except: *-noise) +dm +group_dm`.

    **Both callers pass the MERGED entry**, not the raw one: `rooms` is
    inheritable, so a rule that takes its matcher from a template has no `rooms`
    key of its own and the raw entry summarises as "(none)" — a rule serving
    `eng-*` and every 1:1 DM displayed as serving nothing.

    Defensive against a malformed entry (rooms not a mapping): shows what it
    can, the row's own Status column carries the actual error."""
    rooms = entry.get("rooms")
    if not isinstance(rooms, dict):
        return "?" if rooms is not None else "(none)"
    parts: list[str] = []
    include = rooms.get("include")
    if isinstance(include, list) and include:
        parts.append(", ".join(str(p) for p in include))
    except_for = rooms.get("except_for")
    if isinstance(except_for, list) and except_for:
        parts.append(f"(except: {', '.join(str(p) for p in except_for)})")
    if rooms.get("direct"):
        parts.append("+dm")
    if rooms.get("group_direct"):
        parts.append("+group_dm")
    return " ".join(parts) if parts else "(none)"


class RuleDetailScreen(FormScreen):
    BODY_ID = "rule-detail-body"

    @staticmethod
    def missing_prerequisites(cfg: EditableConfig) -> str | None:
        """Why this screen's edit/create form cannot open right now, or None.

        Internal review (lens B): the connector/agent dropdowns are the
        first enum FieldSpecs whose options come from a config list that can
        be EMPTY — and Textual's `Select(options, allow_blank=False)` raises
        EmptySelectError at construction on empty options, mid-compose,
        which takes the whole app down. Every entry point into a non-view
        mode (Overview's 'n'/'e' and this screen's own view→edit 'e') asks
        here first and notifies instead of composing."""
        missing = [
            noun
            for noun, present in (
                ("connector", bool(cfg.connectors_raw)),
                ("agent", bool(cfg.agents_raw)),
            )
            if not present
        ]
        if not missing:
            return None
        return (
            f"Watcher rules need at least one {' and one '.join(missing)} to exist "
            "first — create those before editing watcher rules."
        )

    async def action_edit(self) -> None:
        # The view→edit transition composes the Select widgets too — same
        # guard as the Overview's entry points (see missing_prerequisites()).
        message = self.missing_prerequisites(self.cfg)
        if message is not None and self.mode == "view":
            self.notify(message, severity="error")
            return
        await super().action_edit()

    def __init__(
        self,
        cfg: EditableConfig,
        entry: dict | None,
        mode: Literal["view", "edit", "create"] = "view",
    ):
        super().__init__()
        self.cfg = cfg
        # Create mode: nothing exists yet. The entry is never installed into
        # `cfg.document` until action_save() actually succeeds.
        self.entry = entry if entry is not None else {}
        self.mode = mode
        self._inherits_initial: str | None = self.cfg.entry_template_name(self.entry)
        self._inherits_current: str | None = self._inherits_initial
        # Whether the user actually touched the Name field this session —
        # `_name_live` alone can't distinguish "cleared to empty" from
        # "never touched" (both read ""), and a cleared name must survive a
        # template switch just like a typed one (Codex review of #129,
        # round 2). Reset alongside the other per-session state in
        # _on_enter_edit_mode().
        self._name_edited = False
        if self.mode != "view":
            self._compute_initial_values(self._current_entry())
            self._description_live = self._initial_values.get("description") or ""
            self._populating = True

    # ── FormScreen hooks ─────────────────────────────────────────────────────

    def _entity_noun(self) -> str:
        # "watcher rule", not "rule": the Tool Presets tab has rules too, and a
        # bare "Delete rule 'x'?" does not say which kind is about to go.
        return "watcher rule"

    def _entity_label(self) -> str:
        name = self.entry.get("name")
        if isinstance(name, str) and name:
            return name
        return "(new rule)" if self.mode == "create" else "?"

    def _current_entry(self) -> dict:
        """The PROBE entry: `self.entry` with `inherits:` swapped to whatever
        the Inherits picker currently has selected — see
        ConnectorDetailScreen._current_entry()'s identical docstring."""
        probe = dict(self.entry)
        if self._inherits_current is None:
            probe.pop("inherits", None)
        else:
            probe["inherits"] = self._inherits_current
        return probe

    def _find_own_index(self) -> int:
        # By object IDENTITY, not equality — same reasoning as
        # ConnectorDetailScreen._find_own_index(): two broken entries could
        # be byte-identical; identity is the only way to be sure this is
        # the exact entry this screen was opened on.
        watchers = self.cfg.watcher_entries
        return next(i for i, w in enumerate(watchers) if w is self.entry)

    def _remove_entry_from_document(self) -> None:
        self._deleted_index = self._find_own_index()
        del self.cfg.document["watcher_rules"][self._deleted_index]

    def _reinsert_entry_into_document(self) -> None:
        watchers = self.cfg.document.setdefault("watcher_rules", [])
        watchers.insert(self._deleted_index, self.entry)

    def _install_trial_entry(self, target_entry: dict) -> None:
        self._edit_index = self._find_own_index()
        self.cfg.document["watcher_rules"][self._edit_index] = target_entry

    def _rollback_trial_entry(self) -> None:
        self.cfg.document["watcher_rules"][self._edit_index] = self.entry

    def _referencing_watcher_labels(self) -> list[str]:
        # Nothing in config.yaml references a rule by name — the delete
        # pre-check has nothing to block on. What a deletion STRANDS at
        # runtime is a warning, not a blocker: see _delete_confirm_message().
        return []

    def _delete_confirm_message(self) -> str:
        base = super()._delete_confirm_message()
        name = self.entry.get("name")
        if not isinstance(name, str) or not name.strip():
            return base
        # Stripped, matching _parse_one_watcher_rule's canonicalization —
        # persisted records carry the STRIPPED name, so an externally-
        # authored `name: " my-rule "` would otherwise count zero strands
        # and silently suppress the disclosure (Codex review of #129).
        records, jobs = stranded_by_rule(name.strip())
        if not records and not jobs:
            return base
        strands = [f"{records} persisted session record(s)"]
        if jobs:
            strands.append(f"{jobs} scheduled job(s)")
        message = (
            base
            + f"\n\nThis rule currently has {' and '.join(strands)} "
            "on disk. They stop being claimed by any rule; idle sessions "
            "are reclaimed by the daemon's lifecycle sweeps."
        )
        if jobs:
            # Rewritten with the behaviour (owner, 2026-08-31). This used to say
            # a session with pending jobs was "exempt from expiry — its jobs keep
            # running until removed", which was true of the sweep's old job
            # exemption. That exemption is gone, and the outcome is now the
            # opposite: the record is expired like any other, and each fire then
            # resolves the room, finds no rule claiming it, and delivers nothing.
            # The jobs are not deleted — they just stop working, while still
            # listing as active, which is the part worth saying out loud.
            # And "once no rule claims those rooms" is NOT when the rule is
            # deleted. Two facts compose, and the second is the one that makes
            # this indefinite rather than merely delayed:
            #
            # * a room that still has a record is recreated from THAT RECORD's
            #   own persisted config — `WatcherManager._recreate`: "the current
            #   rules are never consulted" (sticky binding, §2.4);
            # * a job fire is ACTIVITY. Inbound and scheduled injection funnel
            #   through the same `MessageProcessor.enqueue`, which advances
            #   `last_activity_at` — and the idle leg measures from there.
            #
            # So a job firing more often than `session_idle_days` (15) keeps its
            # own watcher's record alive, which keeps the recreation source
            # alive, which keeps the job working. A daily job on a deleted rule
            # runs FOREVER. Only a job whose interval exceeds the idle TTL lets
            # the room go idle and then expire (15 more days from `dropped_at`,
            # so ~30 from last activity) and stop.
            #
            # An operator who deleted the rule in order to stop the bot must not
            # be told it has stopped, and must not be given a date either.
            message += (
                " Its scheduled jobs are NOT deleted, and deleting the rule "
                "does not stop them: a room that still has a record is "
                "recreated from that record, not from the rules, and each fire "
                "counts as activity that keeps the record alive. A job firing "
                "more often than session_idle_days (15) therefore keeps running "
                "indefinitely, while still showing as active. To stop it, delete "
                "the jobs with 'coop schedule delete <job_id>', or expire the "
                "watchers."
            )
        return message

    def _required_field_keys(self) -> frozenset[str]:
        return _RULE_REQUIRED_FIELD_KEYS

    def _field_specs(self) -> tuple[FieldSpec, ...]:
        connector_names = tuple(sorted(c.get("name", "?") for c in self.cfg.connectors_raw))
        agent_names = tuple(sorted(self.cfg.agents_raw))
        return sort_required_first(
            (
                FieldSpec("name", "str", "Watcher rule name"),
                FieldSpec("connector", "enum", "Connector", options=connector_names),
                FieldSpec("agent", "enum", "Agent", options=agent_names),
                # rooms + TTLs + history_handoff all come from
                # WATCHER_TEMPLATE_FIELDS — listing rooms here too would render
                # every matcher field twice.
                *WATCHER_TEMPLATE_FIELDS,
            ),
            _RULE_REQUIRED_FIELD_KEYS,
        )

    def _template_kind(self) -> str:
        return "watcher"

    def _dataclass_defaults(self) -> dict[str, object]:
        # `name`, `connector` and `agent` have NO default — None is the honest
        # answer for all three, and for the same reason: there is no value a
        # rule that omits them would evaluate to. It would not evaluate to
        # anything; the config would fail to load.
        #
        # This dict feeds `_compute_initial_values()`, which uses it as the
        # SNAPSHOT for a field the entry and its template both leave unset. A
        # made-up value there is not a harmless preselection — the snapshot is
        # what Save diffs against, so the form showed "agent-opencode" for a
        # rule that had no agent, the diff saw no change, and Save wrote
        # nothing while the rule stayed unloadable (user-reported). Worse, the
        # value it invented was the FIRST agent in the file: exactly the
        # document-order binding that was just removed from the loader.
        defaults: dict[str, object] = dict(WATCHER_TEMPLATE_DATACLASS_DEFAULTS)
        defaults["name"] = None
        defaults["connector"] = None
        defaults["agent"] = None
        defaults["rooms.include"] = []
        defaults["rooms.except_for"] = []
        defaults["rooms.direct"] = False
        defaults["rooms.group_direct"] = False
        return defaults

    # ── inherits: picker (mirrors agent/connector — watchers have no 'type') ──

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if (event.button.id or "") == "inherits-change-button":
            self._open_inherits_picker()

    @work
    async def _open_inherits_picker(self) -> None:
        if self.mode == "view":
            return
        template_names = sorted(self.cfg.templates("watcher"))
        choice = await self.app.push_screen_wait(
            InheritsPickerModal(template_names, self._inherits_current)
        )
        if choice is None:
            return
        kind, name = choice

        if kind == "template":
            new_value = name
        elif kind == "none":
            new_value = None
        elif kind == "new_template":
            new_name = await self.app.push_screen_wait(
                TextPromptModal("New watcher rule template — name")
            )
            if new_name is None:
                return
            if new_name in self.cfg.templates("watcher"):
                await self.app.push_screen_wait(
                    MessageModal(
                        f"A watcher rule template named '{markup_safe(new_name)}' "
                        "already exists.",
                        title="Could not create",
                    )
                )
                return
            from .template_detail import TemplateDetailScreen

            self.app.push_screen(
                TemplateDetailScreen(self.cfg, "watcher", new_name, {}, mode="create")
            )
            return
        else:
            return

        if new_value == self._inherits_current:
            return

        if self._any_field_overridden():
            confirmed = await self.app.push_screen_wait(
                ConfirmModal(
                    "Switching templates will reset any unsaved edits to "
                    "the fields below back to the new template's values. "
                    "Continue?",
                    confirm_label="Switch",
                )
            )
            if not confirmed:
                return

        self._inherits_current = new_value
        await self._recompute_form()
        self._form_dirty = True

    async def _recompute_form(self) -> None:
        """Codex review of #129: unlike the other forms, this screen models
        `name` as a generic FieldSpec, so the base recompute rebuilt the
        name Input from `self.entry` — silently discarding a typed-but-
        unsaved name on every `inherits:` switch (in create mode it went
        blank and Save was then refused). A watcher template can never
        supply `name` (forbidden key), so restoring the live value can't
        mask a template-provided one. `_name_live` is already maintained by
        FormScreen.on_input_changed() because widget_id("name") happens to
        be the exact "#field-name" id it tracks. Gated on `_name_edited`,
        not on `_name_live`'s truthiness (round 2): a name CLEARED to empty
        is an edit too, and restoring the old name over it would silently
        resurrect the identity the user just removed — while an untouched
        form ("" because nothing was ever typed) must keep showing the
        entry's own name."""
        await super()._recompute_form()
        if self._name_edited:
            try:
                self.query_one("#" + widget_id("name"), Input).value = self._name_live
            except NoMatches:
                pass

    def on_input_changed(self, event: Input.Changed) -> None:
        if not self._populating and (event.input.id or "") == "field-name":
            self._name_edited = True
        super().on_input_changed(event)

    def _on_enter_edit_mode(self) -> None:
        self._inherits_initial = self.cfg.entry_template_name(self.entry)
        self._inherits_current = self._inherits_initial
        self._name_edited = False  # fresh edit session — see __init__
        self._compute_initial_values(self._current_entry())
        self._description_live = self._initial_values.get("description") or ""

    # ── view mode ────────────────────────────────────────────────────────────

    def _body_text(self) -> str:
        entry = self.entry
        description = entry.get("description")
        template_name = self.cfg.entry_template_name(entry)

        # Every dynamic value here is operator-authored and reaches a
        # markup-parsing Static — rule names are unrestricted and patterns
        # legitimately contain `[…]` (see markup_safe()).
        lines = [f"[bold]{markup_safe(self._entity_label())}[/bold]"]
        if description:
            lines.append(f"[dim]{markup_safe(description)}[/dim]")
        try:
            merged = self.cfg.merged_entry("watcher", entry)
        except (ValueError, FileNotFoundError) as exc:
            # markup_safe: this message quotes the value that caused it —
            # `inherits: "[/]"` names an unknown template, and the loader's
            # error repeats that name, so an unescaped interpolation raised
            # MarkupError and crashed the row instead of EXPLAINING it
            # (Codex review of #129, round 8). The explanation path must not
            # be the one that fails.
            lines.append(
                f"[red]Could not compute effective values: {markup_safe(exc)}[/red]"
            )
            return "\n".join(lines)

        defaults = self._dataclass_defaults()
        lines.append(
            f"connector: {markup_safe(merged.get('connector') or defaults['connector'])}"
        )
        lines.append(f"agent: {markup_safe(merged.get('agent') or defaults['agent'])}")
        # `merged`, not `entry` — same reason as connector/agent above: an
        # inherited matcher is absent from the raw entry, and summarising the raw
        # entry reported "(none)" for a rule that claims rooms. The per-field
        # lines below carry where each part came from.
        lines.append(f"rooms: {markup_safe(rule_rooms_summary(merged))}")
        lines.append(
            f"inherits: {markup_safe(template_name) if template_name else '(none)'}"
        )
        lines.append("")

        for spec in WATCHER_TEMPLATE_FIELDS:
            # spec.key, not its top half: a nested field's provenance is its
            # own, since the merge is per sub-key.
            provenance = self.cfg.field_provenance("watcher", entry, spec.key)
            top_key = spec.key.split(".", 1)[0]
            value = merged.get(top_key)
            if "." in spec.key and isinstance(value, dict):
                value = value.get(spec.key.split(".", 1)[1])
            if value is None:
                value = WATCHER_TEMPLATE_DATACLASS_DEFAULTS.get(spec.key)
            lines.append(
                f"{spec.key}: {markup_safe(format_value(value))}  "
                f"[dim]({provenance_label(provenance, template_name)})[/dim]"
            )
        return "\n".join(lines)

    # ── edit/create form ─────────────────────────────────────────────────────

    def _compose_form(self) -> ComposeResult:
        with VerticalScroll(classes="entity-form", can_focus=False):
            if self.mode == "create":
                yield Static("[bold]New watcher rule[/bold]")
            else:
                yield Static(f"[bold]{markup_safe(self._entity_label())}[/bold]  (editing)")

            with Horizontal(classes="field-row"):
                yield Static("Description", classes="field-label")
                yield Input(id="field-description", value=self._description_live)

            with Horizontal(classes="field-row"):
                yield Static("Inherits", classes="field-label")
                yield Static(
                    # A template name is operator-authored and this Static
                    # parses markup (see markup_safe()).
                    markup_safe(self._inherits_current) if self._inherits_current
                    else "(none)",
                    id="inherits-value",
                    classes="field-value",
                )
                yield Button("Change…", id="inherits-change-button")

            for spec in self._field_specs():
                yield from self._compose_field_row(spec, self._current_entry())

    # ── save ─────────────────────────────────────────────────────────────────

    @work
    async def action_save(self) -> None:
        if self.mode == "view":
            return

        updates = self._collect_field_updates()
        if updates is None:
            await self.app.push_screen_wait(
                MessageModal(
                    # read_widget_value() quotes the operator's own text back
                    # ("must be a whole number, got '[/]'"), so the message
                    # reporting a bad value must not itself be parsed as
                    # markup (Codex review of #129, round 10).
                    markup_safe(self._last_field_error or "Invalid field."),
                    title="Could not save",
                )
            )
            return

        # ALWAYS a trial copy, never self.entry directly — see
        # ConnectorDetailScreen.action_save()'s identical comment.
        target_entry = dict(self.entry)
        for key, value in updates.items():
            apply_update(target_entry, key, value)
        if self._inherits_current != self._inherits_initial:
            apply_update(target_entry, "inherits", self._inherits_current)

        # connector/agent are ALWAYS written explicitly at creation (bypassing
        # diff semantics): a new rule has no template to fall back on unless the
        # operator chose one, and there is no loader default behind it either.
        if self.mode == "create":
            for key in ("connector", "agent"):
                value = read_widget_value(
                    next(spec for spec in self._field_specs() if spec.key == key),
                    self.query_one("#" + widget_id(key), Select),
                )
                if value:
                    target_entry[key] = value

        name = target_entry.get("name")
        if not isinstance(name, str) or not name.strip():
            await self.app.push_screen_wait(
                MessageModal("Watcher rule name is required.", title="Could not save")
            )
            return
        duplicate = any(
            w.get("name") == name
            for w in self.cfg.watchers_raw
            if w is not self.entry
        )
        if duplicate:
            await self.app.push_screen_wait(
                MessageModal(
                    f"A rule named '{markup_safe(name)}' already exists.",
                    title="Could not save",
                )
            )
            return

        # Required on the MERGED rule, in every mode — the same rule the rooms
        # gate below uses, and for the same reason: the loader judges the rule
        # after its template is merged in, so a rule that inherits `agent` must
        # save, and one that inherits nothing must not.
        #
        # Edit mode used to skip this entirely. Combined with a form that
        # invented a value for an unset field, that is how a rule with no
        # `agent:` could be opened, saved, and left exactly as broken as it was
        # found (user-reported).
        try:
            merged_required = self.cfg.merged_entry(self._template_kind(), target_entry)
        except (ValueError, FileNotFoundError):
            merged_required = target_entry
        missing = [k for k in ("connector", "agent") if not merged_required.get(k)]
        if missing:
            # Assembled here, from the literal tuple above — the modal then
            # interpolates one already-safe local rather than three expressions
            # (the markup lint matches on expression source, and a sentence
            # built out of conditionals is three separate things to justify).
            # "an agent", "a connector" — the article is per word, and getting
            # it wrong reads as a bug in the tool rather than a problem with the
            # config.
            def _with_article(field: str) -> str:
                return f"{'an' if field[0] in 'aeiou' else 'a'} {field}"

            missing_text = " and ".join(_with_article(k) for k in missing)
            await self.app.push_screen_wait(
                MessageModal(
                    f"This rule needs {missing_text}. Choose above, or give the "
                    f"rule an 'inherits:' template that sets it.",
                    title="Could not save",
                )
            )
            return

        # A rule that can never match anything is a typo, not an intention
        # (the loader refuses it too — this just says it in form terms
        # before a generic parser message would).
        #
        # Checked on the MERGED entry, which is the whole point: this gate's job
        # is to say what the loader would refuse, and the loader judges the rule
        # AFTER its template is merged in. Reading the raw entry made the form
        # stricter than the loader once `rooms` became inheritable — a rule whose
        # `include` lives in its template could not be saved at all after
        # reverting its own DM flags to inherited, which is exactly the operation
        # someone adopting a shared matcher performs. Only the check is merged;
        # `target_entry` is still what gets written to the document.
        try:
            merged_target = self.cfg.merged_entry(self._template_kind(), target_entry)
        except (ValueError, FileNotFoundError):
            merged_target = target_entry
        rooms = merged_target.get("rooms") or {}
        if not isinstance(rooms, dict) or not (
            rooms.get("include") or rooms.get("direct") or rooms.get("group_direct")
        ):
            await self.app.push_screen_wait(
                MessageModal(
                    "A rule needs at least one rooms include pattern, or one "
                    "of the DM opt-ins (direct / group_direct).",
                    title="Could not save",
                )
            )
            return

        inserted_index: int | None = None
        was_dirty = self.cfg.dirty  # captured BEFORE any mutation; see below
        watchers_key_was_absent = False
        watchers_original_value: object = None
        if self.mode == "create":
            existing = self.cfg.document.get("watcher_rules")
            if existing is not None and not isinstance(existing, list):
                # REFUSED, not normalized (Codex review of #129, round 3):
                # a malformed non-list `watcher_rules:` can hold RECOVERABLE rule
                # data — the classic shape is a mapping from an operator
                # omitting the '-' before an otherwise complete rule — and
                # replacing it with [] would pass the save gate (the
                # structural error disappears along with the data) and
                # silently delete work the user only asked to add to.
                # Only an absent or explicit-null key is normalized below.
                await self.app.push_screen_wait(
                    MessageModal(
                        "config.yaml's 'watcher_rules:' is not a list "
                        f"(got {type(existing).__name__}) — often a missing "
                        "'-' before a rule. Repair it in $EDITOR (ctrl+e on "
                        "the list screen) first; creating a rule here would "
                        "overwrite whatever it holds.",
                        title="Could not save",
                    )
                )
                return
            # KEY MEMBERSHIP, not the value: `document.get("watcher_rules")`
            # returns None both for an absent key and for an explicit
            # `watcher_rules:` (null), so round 10's `existing is None` popped a
            # key the operator had actually written — and a later unrelated
            # successful save then wrote the file without it (Codex review of
            # #129, round 11). Both the presence and the original value are
            # captured so a rejected create restores the document exactly as
            # loaded.
            watchers_key_was_absent = "watcher_rules" not in self.cfg.document
            watchers_original_value = self.cfg.document.get("watcher_rules")
            if not isinstance(existing, list):
                self.cfg.document["watcher_rules"] = []
            watchers = self.cfg.document["watcher_rules"]
            watchers.append(target_entry)
            inserted_index = len(watchers) - 1
        else:
            self._install_trial_entry(target_entry)
        self.cfg.mark_dirty()

        try:
            self.cfg.save()
        except (ValueError, FileNotFoundError) as exc:
            if self.mode == "create" and inserted_index is not None:
                del self.cfg.document["watcher_rules"][inserted_index]
                if watchers_key_was_absent:
                    self.cfg.document.pop("watcher_rules", None)
                elif not isinstance(watchers_original_value, list):
                    # It was there, holding something this form replaced with
                    # a list (an explicit null). Put that value back rather
                    # than the empty list standing in for it.
                    self.cfg.document["watcher_rules"] = watchers_original_value
            else:
                self._rollback_trial_entry()
            # Same rollback contract the reorder and malformed-row delete
            # already honour: restoring the document is only half of it, or
            # the quit gate goes on offering to discard changes that no
            # longer exist. This site was MINE and I missed it when scoping
            # round 8's fix — the sibling pre-existing paths are #131.
            self.cfg.dirty = was_dirty
            await self.app.push_screen_wait(MessageModal(markup_safe(exc), title="Could not save"))
            return

        self.entry = target_entry
        self.app.pop_screen()
        app = self.app
        app.notify(f"Saved watcher rule '{name}'.", severity="information")
        app.reload_config()  # type: ignore[attr-defined]
