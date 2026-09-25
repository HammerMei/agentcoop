"""AgentDetailScreen — view, edit, and create a single agent.

Unlike connectors, the agent schema is complete (additionalProperties:
false in gateway/schema/config.schema.json's $defs/agent), so this shows a
fixed field list with a provenance marker per field (explicit / inherited
from the agent's own `inherits:` template / explicit-null-suppressing)
instead of a generic dump.
`_FORM_FIELDS`/`_PERMISSIONS_FORM_FIELDS` below are a manually-maintained
mirror of that schema (matching Phase 1's `_KNOWN_FIELDS`, not a runtime
JSON-schema interpreter) — safe because the schema is closed
(`additionalProperties: false`), so there's no drift risk from a field this
form doesn't know about sneaking in.

Tool lists (`owner_allowed_tools`/`guest_allowed_tools`) render read-only in
view mode (`_body_text()`), but are directly editable in edit/create mode via
two `ListView`s with dedicated "+ Add"/"Edit"/"- Remove" `Button`s beside each list
(user-reported: the previous 'a'/'x' single-key bindings silently typed
those letters into whatever Input happened to have focus instead of
triggering the action — a real risk of quietly corrupting an unrelated
field's value; buttons have no such conflict). They live OUTSIDE the
`FieldSpec`/`apply_update()` diffing pipeline (that machinery is scalar-
field-shaped; a list of preset-references/inline-rule-dicts doesn't fit it)
— the actual widget/state machinery lives in `.tool_list_editor`
(`ToolListEditorMixin`), extracted there once `TemplateDetailScreen` became
a second concrete user (agent templates can set these two fields too).
`_tool_list_starting_values()` below returns the MERGED starting value
(same "what's currently in effect" semantics `_compute_initial_values()`
uses for every other field), and the mixin's `action_save()` hook diffs the
FINAL local list against that snapshot itself, writing an explicit override
only if it actually changed (matching decision 2: "editing an inherited
field always writes an explicit per-entry override" — untouched stays
untouched, exactly as `_collect_field_updates()` already does for every
scalar field).

The Inherits row is likewise a `Button` (same 'i'-key-conflict reasoning),
not a `FieldSpec`-pipeline field — picking a new template affects the
EFFECTIVE value of every OTHER field (unlike any single scalar field, which
only affects itself), so unlike every other field's "snapshot once at open,
diff at Save" semantics, picking a new template triggers a full
`_recompute_form()` (form_common.py): every field's prefilled value and
provenance label are recomputed against the NEWLY selected template and the
form is recomposed from scratch. If the user had already typed unsaved
edits into OTHER fields before switching, those would be silently discarded
by that recompute — `_any_field_overridden()` is checked first and, if
true, a `ConfirmModal` warns before proceeding. `_current_entry()` below
returns a PROBE entry (`self.entry` with `inherits:` swapped to whatever the
picker currently has selected, not yet saved) — every live-display
computation (compose, provenance labels, ctrl+r, tool-list prefill) reads
through this probe, not `self.entry` directly, so they all reflect the
picker's current state rather than only updating after Save.

Edit/create + Save/dirty/navigation machinery lives in `.form_common`
(`FormScreen`) — shared with `ConnectorDetailScreen`. This module supplies
the agent-specific pieces: which fields exist, their dataclass defaults, and
`action_save()`'s entity-shaped insertion (`document["agents"]` is a dict
keyed by name, unlike connectors' list).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Input, Static

from ...config import config_base_dir, resolve_working_directory
from ..formatting import format_value, markup_safe, provenance_label
from ..modals import ConfirmModal, InheritsPickerModal, MessageModal, TextPromptModal
from ..model import EditableConfig
from .form_common import (
    FieldSpec,
    FormScreen,
    apply_update,
    find_referencing_watcher_labels,
    sort_required_first,
)
from .tool_list_editor import TOOL_LIST_WIDGET_IDS, ToolListEditorMixin, format_tool_rule

# NOTE: TemplateDetailScreen (screens/template_detail.py) is deliberately
# imported LOCALLY inside _open_inherits_picker() below, not at module level —
# template_detail.py itself imports AGENT_FORM_FIELDS/AGENT_DATACLASS_DEFAULTS
# from this module, so a module-level import here would be circular.

if TYPE_CHECKING:
    from ..app import ConfigToolApp

# Top-level agent fields worth a dedicated provenance-annotated line, in the
# same order as AgentConfig's own fields (gateway/core/config.py). View mode
# only — the form below has its own, edit-oriented field list.
_KNOWN_FIELDS = [
    "type", "command", "working_directory", "session_prefix",
    "lazy_instruction_loading", "new_session_args", "context_inject_files",
    "timeout", "permissions",
]

# AgentConfig/PermissionConfig's own dataclass defaults (gateway/core/
# config.py) — used ONLY to prefill the form with the true effective value
# when a field is set by neither the entry nor its inherits: template.
# Unlike view mode (which simply omits a line for an absent field), a form
# editing that field needs to show what it would actually evaluate to right
# now. Public (no leading underscore): TemplateDetailScreen reuses this dict
# too, as the "no dataclass default" fallback value for editing an agent
# template's own fields.
AGENT_DATACLASS_DEFAULTS: dict[str, object] = {
    "type": "claude",
    "command": "claude",
    "working_directory": "",
    "session_prefix": "agent-chat",
    "lazy_instruction_loading": True,
    "new_session_args": [],
    "context_inject_files": [],
    "timeout": 360,
    "permissions.enabled": False,
    "permissions.timeout": 300,
    "permissions.skip_owner_approval": False,
}

_FORM_FIELDS: list[FieldSpec] = [
    FieldSpec("type", "enum", "Type", options=("claude", "opencode")),
    FieldSpec("command", "str", "Command"),
    FieldSpec("working_directory", "str", "Working directory"),
    FieldSpec("session_prefix", "str", "Session prefix"),
    FieldSpec("lazy_instruction_loading", "bool", "Lazy instruction loading"),
    FieldSpec("new_session_args", "list", "New session args (comma-separated)"),
    FieldSpec("context_inject_files", "list", "Context inject files (comma-separated)"),
    FieldSpec("timeout", "int", "Timeout (seconds)"),
]
_PERMISSIONS_FORM_FIELDS: list[FieldSpec] = [
    FieldSpec("permissions.enabled", "bool", "Permissions enabled"),
    FieldSpec("permissions.timeout", "int", "Permissions timeout (seconds)"),
    FieldSpec("permissions.skip_owner_approval", "bool", "Skip owner approval"),
]
# Public (no leading underscore): also reused by TemplateDetailScreen to
# edit an agent template with the exact same field set, schema-derived-so-
# zero-drift-risk reasoning applies just as much there — every one of these
# keys is legal in an agent template too (gateway/config.py's forbidden-keys
# set for agent_templates is empty). Deliberately still includes 'type':
# unlike an actual agent (see _ENTITY_FORM_FIELDS below), a TEMPLATE has no
# TypePickerModal-driven creation step of its own and no immutability
# precedent — it's just an ordinary optional field a template may or may not
# set (mirrors agent_templates' forbidden-keys set being empty, on purpose).
AGENT_FORM_FIELDS = (*_FORM_FIELDS, *_PERMISSIONS_FORM_FIELDS)

# AgentDetailScreen only (see its _field_specs()/_compose_form() overrides):
# 'type' is immutable once an agent exists — chosen via TypePickerModal at
# creation (OverviewScreen.action_new_entity(), baked into the entry before
# this screen ever opens), same precedent as
# ConnectorDetailScreen/`type is immutable once a connector exists`. Shown as
# a read-only header suffix instead, matching connector's own `(type: ...)`
# treatment. AGENT_FORM_FIELDS above (used by TemplateDetailScreen for agent
# TEMPLATES) is untouched — templates keep 'type' as an ordinary editable
# field.
_ENTITY_FORM_FIELDS: list[FieldSpec] = [f for f in _FORM_FIELDS if f.key != "type"]

# 'type' is required too (gateway/config.py: "type is required (directly or
# via an inherits: agent_templates entry)") but never a _field_specs() row at
# all (immutable, shown as a read-only header suffix instead — see
# _agent_type()) — nothing left to mark '*' or sort for it here.
_AGENT_REQUIRED_FIELD_KEYS = frozenset({"working_directory"})


def _working_directory_warning(raw_value: str) -> str:
    """Early, non-blocking heads-up only — NOT a substitute for save()'s own
    validate_config() call, which still hard-fails if the directory is
    missing at save time (GatewayConfig.from_file requires it to exist;
    that enforcement is intentionally left alone here, see
    docs/design/config-tool.md's Phase 2 status notes)."""
    text = raw_value.strip()
    if not text:
        return ""
    # The loader's own resolution (`~`, then the base — #182), not a mirror of
    # it: the mirror this replaced already differed from the loader in one
    # detail (it `resolve()`d the config path first), the drift a copy invites.
    resolved = Path(resolve_working_directory(text, config_base_dir()))
    if not resolved.is_dir():
        # The resolved PATH comes from the operator's own working_directory,
        # and this string is rendered as markup — found by the static markup
        # check when it was extended to whole-argument sinks (Codex review of
        # #129, round 10), not by any review round.
        return f"[yellow]⚠ does not exist yet: {markup_safe(resolved)}[/yellow]"
    return ""


class AgentDetailScreen(ToolListEditorMixin, FormScreen):
    BODY_ID = "agent-detail-body"

    def __init__(
        self,
        cfg: EditableConfig,
        name: str,
        entry: dict,
        mode: Literal["view", "edit", "create"] = "view",
    ):
        super().__init__()
        self.cfg = cfg
        self.agent_name = name
        self.entry = entry
        self.mode = mode
        self._init_tool_lists()
        # See _open_inherits_picker()/action_save() below. Unlike every
        # other field, switching this one triggers a full
        # _recompute_form() (form_common.py) rather than a snapshot-once-
        # at-open value — see module docstring for why.
        self._inherits_initial: str | None = self.cfg.entry_template_name(self.entry)
        self._inherits_current: str | None = self._inherits_initial
        if self.mode != "view":
            self._compute_initial_values(self._current_entry())
            self._description_live = self._initial_values.get("description") or ""
            self._tool_list_state()
            self._populating = True

    def _entity_noun(self) -> str:
        return "agent"

    def _entity_label(self) -> str:
        return self.agent_name

    def _current_entry(self) -> dict:
        """The PROBE entry: `self.entry` (the on-disk explicit fields) with
        `inherits:` swapped to whatever the Inherits picker currently has
        selected — NOT necessarily what's saved yet. Every live-display
        computation (compose, provenance labels, ctrl+r, tool-list prefill)
        reads through this, not `self.entry` directly, so switching
        templates is reflected immediately rather than only after Save."""
        probe = dict(self.entry)
        if self._inherits_current is None:
            probe.pop("inherits", None)
        else:
            probe["inherits"] = self._inherits_current
        return probe

    def _any_field_overridden(self) -> bool:
        # Tool lists live outside the FieldSpec pipeline (see module
        # docstring) — FormScreen's own check only covers _field_specs(),
        # so this also counts an in-progress, unsaved tool-list edit as an
        # override the Inherits-switch confirm needs to warn about.
        return super()._any_field_overridden() or self._any_tool_list_overridden()

    def _remove_entry_from_document(self) -> None:
        del self.cfg.document["agents"][self.agent_name]

    def _reinsert_entry_into_document(self) -> None:
        self.cfg.document.setdefault("agents", {})[self.agent_name] = self.entry

    def _referencing_watcher_labels(self) -> list[str]:
        return find_referencing_watcher_labels(self.cfg, agent_name=self.agent_name)

    def _install_trial_entry(self, target_entry: dict) -> None:
        self.cfg.document.setdefault("agents", {})[self.agent_name] = target_entry

    def _rollback_trial_entry(self) -> None:
        self.cfg.document.setdefault("agents", {})[self.agent_name] = self.entry

    def _on_enter_edit_mode(self) -> None:
        self._inherits_initial = self.cfg.entry_template_name(self.entry)
        self._inherits_current = self._inherits_initial
        self._compute_initial_values(self._current_entry())
        self._description_live = self._initial_values.get("description") or ""
        self._tool_list_state()
        self._tool_list_ever_selected = dict.fromkeys(TOOL_LIST_WIDGET_IDS, False)

    def _required_field_keys(self) -> frozenset[str]:
        return _AGENT_REQUIRED_FIELD_KEYS

    def _field_specs(self) -> tuple[FieldSpec, ...]:
        return sort_required_first(
            (*_ENTITY_FORM_FIELDS, *_PERMISSIONS_FORM_FIELDS), _AGENT_REQUIRED_FIELD_KEYS
        )

    def _agent_type(self) -> str:
        # Reads the MERGED type against the LIVE probe (self._current_entry()),
        # same reasoning as ConnectorDetailScreen._connector_type(): an agent
        # whose 'type' comes only from its inherits: template still needs the
        # right value shown in the read-only header suffix, and kept in sync
        # if the Inherits picker switches to a different template.
        #
        # PR review finding: unlike a connector (whose 'type' is ALWAYS
        # present — immutable, chosen at creation via TypePickerModal, see
        # ConnectorDetailScreen._connector_type()'s identical-looking
        # fallback), an agent can legitimately have NO type at all — its
        # only source might be an inherits: template that's since been
        # cleared (test_picking_none_clears_inherits_and_recomputes_to_
        # dataclass_defaults). Defaulting to a real type name ("claude")
        # here actively LIES about the agent's state in exactly that
        # scenario: 'type' is no longer shown as an editable field anywhere
        # in this form, so this header is the ONLY indicator of it, right
        # as Save is about to fail with "type is required". "(unset)" is
        # never itself a valid type, so it can't be confused with one.
        try:
            merged = self.cfg.merged_entry("agent", self._current_entry())
        except (ValueError, FileNotFoundError):
            merged = self._current_entry()
        return merged.get("type") or "(unset)"

    def _template_kind(self) -> str:
        return "agent"

    def _dataclass_defaults(self) -> dict[str, object]:
        return AGENT_DATACLASS_DEFAULTS

    def _tool_list_starting_values(self) -> dict[str, list]:
        """The MERGED (effective) value of both tool-list keys — same
        semantics `_compute_initial_values()` uses for every scalar field:
        the form shows what's ACTUALLY in effect right now (inherited from
        the agent's own `inherits:` template — or whichever template the
        Inherits picker currently has selected, see `_current_entry()` — or
        explicit on this entry)."""
        try:
            merged = self.cfg.merged_entry(self._template_kind(), self._current_entry())
        except (ValueError, FileNotFoundError):
            merged = dict(self._current_entry())
        return {key: merged.get(key) or [] for key in TOOL_LIST_WIDGET_IDS}

    # ── inherits: picker ─────────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "inherits-change-button":
            self._open_inherits_picker()
        else:
            self._dispatch_tool_list_button(button_id)

    @work
    async def _open_inherits_picker(self) -> None:
        if self.mode == "view":
            return
        # User-reported: this used to list EVERY agent template regardless
        # of type, letting a claude agent pick an opencode-typed template
        # (or vice versa) straight from the picker — gateway/config.py's
        # _resolve_inherits() now rejects that combination outright at save
        # time, but filtering it out of the picker here catches the mistake
        # before the user fills in the rest of the form.
        #
        # Filtered against `self.entry.get("type")` — the entry's OWN raw
        # type — NOT `self._agent_type()` (the merged/current EFFECTIVE
        # type), mirroring ConnectorDetailScreen._open_inherits_picker()'s
        # identical reasoning: an agent may have no own 'type' at all,
        # relying entirely on whichever template it currently inherits from
        # (test_agent_inherits_template's `default: {inherits: standard}` is
        # exactly this) — such an agent must still be free to switch to ANY
        # template. The mismatch this filter exists to prevent only arises
        # when the entry ITSELF pins an explicit type that a candidate
        # template's own explicit type would then contradict.
        all_templates = self.cfg.templates("agent")
        entry_type = self.entry.get("type")
        template_names = sorted(
            name
            for name, template in all_templates.items()
            if not entry_type or not template.get("type") or template.get("type") == entry_type
        )
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
                TextPromptModal("New agent template — name")
            )
            if new_name is None:
                return
            if new_name in self.cfg.templates("agent"):
                await self.app.push_screen_wait(
                    MessageModal(
                        f"An agent template named '{markup_safe(new_name)}' "
                        "already exists.",
                        title="Could not create",
                    )
                )
                return
            # One-way detour, not a return-with-result flow (see
            # InheritsPickerModal's docstring): the user fills in the new
            # template over there, presses Escape to come back HERE, then
            # clicks the Inherits button again to actually reference it.
            from .template_detail import TemplateDetailScreen

            self.app.push_screen(TemplateDetailScreen(self.cfg, "agent", new_name, {}, mode="create"))
            return
        else:
            return

        if new_value == self._inherits_current:
            return  # no actual change — nothing to warn about or recompute

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
        self._tool_list_state()
        self._tool_list_ever_selected = dict.fromkeys(TOOL_LIST_WIDGET_IDS, False)
        await self._recompute_form()
        self._form_dirty = True

    # ── view mode ────────────────────────────────────────────────────────────

    def _body_text(self) -> str:
        description = self.entry.get("description")
        lines = [f"[bold]{markup_safe(self.agent_name)}[/bold]"]
        if description:
            lines.append(f"[dim]{markup_safe(description)}[/dim]")
        template_name = self.cfg.entry_template_name(self.entry)
        lines.append(
            f"inherits: {markup_safe(template_name) if template_name else '(none)'}"
        )
        lines.append("")

        try:
            merged = self.cfg.merged_entry("agent", self.entry)
        except (ValueError, FileNotFoundError) as exc:
            lines.append(
                f"[red]Could not compute effective values: {markup_safe(exc)}[/red]"
            )
            return "\n".join(lines)

        for key in _KNOWN_FIELDS:
            if key not in merged:
                continue
            provenance = self.cfg.field_provenance("agent", self.entry, key)
            lines.append(
                f"{markup_safe(key)}: {markup_safe(format_value(merged[key]))}  "
                f"[dim]({provenance_label(provenance, template_name)})[/dim]"
            )

        for label, field_key in (
            ("owner_allowed_tools", "owner_allowed_tools"),
            ("guest_allowed_tools", "guest_allowed_tools"),
        ):
            if field_key not in merged:
                continue
            provenance = self.cfg.field_provenance("agent", self.entry, field_key)
            lines.append("")
            lines.append(f"{label}:  [dim]({provenance_label(provenance, template_name)})[/dim]")
            for item in merged.get(field_key) or []:
                lines.append(f"  {markup_safe(format_tool_rule(item))}")

        return "\n".join(lines)

    # ── edit/create form ─────────────────────────────────────────────────────

    def _compose_form(self) -> ComposeResult:
        # can_focus=False: otherwise this container is itself the first
        # focusable widget (user-reported: needed Tab TWICE to reach the
        # first real field — once to focus this container, once to move
        # past it). The container isn't meant to be focused on its own;
        # scrolling still works via the mouse wheel/PageUp/PageDown.
        agent_type = self._agent_type()
        with VerticalScroll(classes="entity-form", can_focus=False):
            if self.mode == "create":
                yield Static(f"[bold]New agent[/bold]  (type: {markup_safe(agent_type)})")
                with Horizontal(classes="field-row"):
                    yield Static("Name *", classes="field-label")
                    yield Input(id="field-name", value=self._name_live, placeholder="agent name")
            else:
                yield Static(
                    f"[bold]{markup_safe(self.agent_name)}[/bold]  "
                    f"(type: {markup_safe(agent_type)}, editing)"
                )

            with Horizontal(classes="field-row"):
                yield Static("Description", classes="field-label")
                yield Input(
                    id="field-description",
                    value=self._description_live,
                )

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

            # self._field_specs() (NOT the raw _ENTITY_FORM_FIELDS/
            # _PERMISSIONS_FORM_FIELDS module lists) — required fields
            # (working_directory) sorted first, per sort_required_first().
            # The "Permissions" header is inserted right before the first
            # permissions.* field rather than as a separate loop, so it stays
            # correctly placed regardless of where the required-first sort
            # puts the ordinary agent fields relative to it.
            in_permissions = False
            for spec in self._field_specs():
                if spec.key.startswith("permissions.") and not in_permissions:
                    yield Static("[bold]Permissions[/bold]")
                    in_permissions = True
                yield from self._compose_field_row(spec, self._current_entry())
                if spec.key == "working_directory":
                    yield Static(
                        _working_directory_warning(
                            str(self._initial_values.get(spec.key) or "")
                        ),
                        id="wd-warning",
                    )

            for key, label in (
                ("owner_allowed_tools", "Owner allowed tools"),
                ("guest_allowed_tools", "Guest allowed tools"),
            ):
                yield Static(f"[bold]{label}[/bold]")
                yield from self._compose_tool_list_widget(key)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "field-working_directory":
            self.query_one("#wd-warning", Static).update(
                _working_directory_warning(event.input.value)
            )
        super().on_input_changed(event)

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

        name = self.agent_name
        if self.mode == "create":
            name = self.query_one("#field-name", Input).value.strip()
            if not name:
                await self.app.push_screen_wait(
                    MessageModal("Name is required.", title="Could not save")
                )
                return
            if name in self.cfg.agents_raw:
                await self.app.push_screen_wait(
                    MessageModal(
                        f"An agent named '{markup_safe(name)}' already exists.",
                        title="Could not save",
                    )
                )
                return

        # ALWAYS a trial copy, never self.entry directly — even for "edit",
        # where self.entry is the SAME object already living in
        # cfg.document. Mutating it here, before save() has even run, would
        # leave invalid data sitting in the document if save() then fails
        # (a real bug: reported as "Save failed, but Back still showed the
        # invalid value" — the fix is never mutating the original until
        # save() has actually succeeded).
        target_entry = dict(self.entry)
        for key, value in updates.items():
            apply_update(target_entry, key, value)

        # inherits: lives outside the FieldSpec/apply_update() pipeline too
        # (it's driven by InheritsPickerModal, not an Input/Checkbox/Select
        # widget) — diffed here directly against the snapshot taken when the
        # form opened (or last re-entered edit mode), same "only write what
        # actually changed" semantics as every other field.
        if self._inherits_current != self._inherits_initial:
            apply_update(target_entry, "inherits", self._inherits_current)

        # Tool lists live outside the FieldSpec/apply_update() pipeline (see
        # module docstring / tool_list_editor.py) — diffed directly against
        # the MERGED snapshot _tool_list_state() took when the form opened.
        self._collect_tool_list_updates(target_entry)

        if self.mode == "create":
            self.cfg.document.setdefault("agents", {})[name] = target_entry
        else:
            self._install_trial_entry(target_entry)
        self.cfg.mark_dirty()

        try:
            self.cfg.save()
        except (ValueError, FileNotFoundError) as exc:
            if self.mode == "create":
                # Nothing existed under this name before this screen ever
                # ran — remove it so a failed save doesn't leave a phantom
                # half-created agent sitting in memory.
                del self.cfg.document["agents"][name]
            else:
                self._rollback_trial_entry()
            await self.app.push_screen_wait(MessageModal(markup_safe(exc), title="Could not save"))
            return

        self.entry = target_entry
        self.app.pop_screen()
        app: "ConfigToolApp" = self.app  # type: ignore[assignment]
        app.notify(f"Saved agent '{name}'.", severity="information")
        app.reload_config()
