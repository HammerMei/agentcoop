"""Shared machinery for the config TUI's entity edit/create forms.

`AgentDetailScreen` was the first (Phase 2); `ConnectorDetailScreen` the
second; `RuleDetailScreen` is the third. Extracted here once a second
concrete user existed, rather than guessed at up front — code review item 10
already flagged the cost of letting screens duplicate this kind of machinery
independently.

Implements docs/design/config-tool.md decision 2 ("editing an inherited
field always writes an explicit per-entry override"): nothing is written to
`EditableConfig.document` until Save. Every field is snapshotted when the
form opens (the effective/merged value — an inherited field displays its
real current value, not blank) and diffed against the widget's value at
Save time; only fields that actually changed get written. Clearing a field
back to blank reverts it to inherited (pops the key) rather than writing an
explicit null — see `apply_update()`.

A subclass provides:
  - `_field_specs() -> tuple[FieldSpec, ...]` — which fields this form shows
    right now (may depend on entity-specific state, e.g. connector `type`).
  - `_template_kind() -> str` — the `*_templates:` block this entry's own
    `inherits:` field (if set) is resolved against (`"agent"` /
    `"connector"` / `"watcher"`).
  - `_dataclass_defaults() -> dict[str, object]` — the true effective value
    for a field set by neither the entry nor its `inherits:` template (a
    form needs to show what a field would actually evaluate to; view mode
    gets away with just omitting the line).
  - `_compose_form() -> ComposeResult` — the form body (typically a
    `VerticalScroll` wrapping `_compose_field_row()` calls plus whatever
    entity-specific chrome — a name Input for create mode, etc.).
  - `action_save()` — entity-specific: where a new entry gets inserted
    (`document["agents"][name]` is a dict keyed by name; `document["connectors"]`
    is a list where each entry carries its own `name` field) differs enough
    that forcing a shared implementation would be more awkward than it's
    worth. Call `self._collect_field_updates()` for the generic diff, then
    apply/insert/save however this entity needs to.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import Checkbox, Footer, Header, Input, Select, Static

from ..formatting import markup_safe, provenance_label
from ..modals import ConfirmModal, MessageModal
from ..model import EditableConfig, Provenance
from .base import DetailScreen

# Distinguishes "caller did not supply a provenance" from "provenance is
# None" (a real value meaning: could not be computed) in _field_annotation().
_UNSET = object()


@dataclass(frozen=True)
class FieldSpec:
    key: str  # dotted for a one-level-nested sub-field, e.g. "server.password"
    kind: Literal["str", "int", "float", "bool", "list", "enum"]
    label: str
    options: tuple[str, ...] | None = None
    # Masks the widget's display (Input(password=True)). docs/design/
    # config-tool.md decision 6, final revision: secrets are stored
    # directly in config.yaml (chmod 0600) and $VAR/${VAR} is never
    # resolved by anything but the one-time migration
    # (gateway/config_migrate.py) — by the time this screen opens, a
    # pre-existing .env-backed config has already been migrated (the TUI
    # launch path triggers it, same as `coop start`), so a
    # secret field's value is always its real, literal value here.
    secret: bool = False


def widget_id(key: str) -> str:
    return "field-" + key.replace(".", "-")


def get_nested(d: dict, dotted_key: str) -> object:
    if "." not in dotted_key:
        return d.get(dotted_key)
    parent_key, sub_key = dotted_key.split(".", 1)
    parent = d.get(parent_key)
    return parent.get(sub_key) if isinstance(parent, dict) else None


def list_to_text(value: object) -> str:
    """Render a "list"-kind field's value for its Input box.

    Anything that is not a genuine list/tuple renders as ONE item, because
    the two ways a hand-edited config gets this wrong both used to fail
    silently or loudly on the bare `for v in value` this replaces
    (Codex review of #129, round 4 — pre-existing, and the same failure
    pair `gateway/config.py`'s `_resolve_paths` documents at the loader):

    * A truthy non-iterable (`rooms.include: 5`, `owners: 5`) raised
      TypeError mid-compose, taking the whole TUI down on a row the
      validator had just marked ERROR and invited the operator to repair.
    * A bare string (`context_inject_files: notes.md`) was iterated **per
      character**, displaying `n, o, t, e, s, ., m, d` — and saving that
      box wrote eight bogus one-character paths over the operator's one
      real value.

    Both now show the value verbatim as a single item: visible, repairable,
    and never silently rewritten. `save()`'s validate-before-write gate
    remains the backstop for whatever the operator then types.
    """
    if not value:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value)


def text_to_list(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


def list_value_is_lossy(value: object) -> bool:
    """Does this list value contain an item the comma-joined box cannot
    represent — i.e. an item containing the join delimiter itself?

    `list_to_text`/`text_to_list` are only inverses while no item contains a
    comma. Round-tripping the snapshot (see `round_trip_value`) keeps such an
    item safe while the box is UNTOUCHED, but the moment the operator edits
    the list for any reason the re-parse splits it (Codex review of #129,
    round 6). Callers use this to refuse that edit loudly instead of
    silently rewriting the item.

    Reachability differs sharply by field, which is why this is fixed rather
    than declined:

    * For a ROOM PATTERN it is provably inert. Both platforms build room
      names from slugs that exclude commas (Mattermost: lowercase
      alphanumeric plus `-`/`_`; Rocket.Chat: `[0-9a-zA-Z-_.]+`), so a
      literal `team,one` can never match any room, and a comma inside a
      character class (`eng-[a,b]`) matches exactly what `eng-[ab]` does.
    * For `context_inject_files` it is real: a filesystem path may
      legitimately contain a comma, and splitting `my,notes.md` into `my`
      and `notes.md` silently stops injecting the operator's actual file.
    """
    if not isinstance(value, (list, tuple)):
        return False
    return any("," in str(item) for item in value)


def apply_update(entry: dict, dotted_key: str, value: object) -> None:
    """Write `value` (or clear, if None) into `entry` at `dotted_key`.

    A top-level key: set it, or pop it entirely if `value` is None (revert
    to inherited/default). A one-level-nested key (`"server.password"`):
    read-modify-write that sub-dict, dropping it entirely once its last
    explicit sub-key is cleared, so a fully-cleared sub-dict reverts to
    inheriting from the matching `*_defaults` block again, not an empty
    `{}` stub sitting in the entry forever.
    """
    if "." not in dotted_key:
        if value is None:
            entry.pop(dotted_key, None)
        else:
            entry[dotted_key] = value
        return
    parent_key, sub_key = dotted_key.split(".", 1)
    parent = entry.get(parent_key)
    parent = dict(parent) if isinstance(parent, dict) else {}
    if value is None:
        parent.pop(sub_key, None)
    else:
        parent[sub_key] = value
    if parent:
        entry[parent_key] = parent
    else:
        entry.pop(parent_key, None)


def read_widget_value(spec: FieldSpec, widget: object) -> object:
    if spec.kind == "bool":
        return widget.value
    if spec.kind == "enum":
        # `Select.BLANK` is a sentinel, not a config value — "nothing chosen"
        # has to read back as None so the diff, the provenance refresh and the
        # save gate all see an unset field rather than an opaque object.
        return None if widget.value is Select.NULL else widget.value
    if spec.kind == "list":
        return text_to_list(widget.value) or None
    if spec.kind == "int":
        text = widget.value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            raise ValueError(f"{spec.label}: must be a whole number, got {text!r}") from None
    if spec.kind == "float":
        text = widget.value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            raise ValueError(f"{spec.label}: must be a number, got {text!r}") from None
    text = widget.value.strip()
    return text or None


class _ValueShim:
    """Minimal stand-in for a widget, so `round_trip_value()` can reuse
    `read_widget_value()` verbatim instead of restating its conversions."""

    __slots__ = ("value",)

    def __init__(self, value: object) -> None:
        self.value = value


def round_trip_value(spec: FieldSpec, value: object) -> object:
    """What a freshly-composed widget for `spec` would read back for `value`.

    The snapshot `_compute_initial_values()` diffs against must be the
    ROUND-TRIPPED value, not the raw one off disk, or an untouched field
    reads as edited and Save rewrites it. Two confirmed ways that happened
    (Codex review of #129, round 5 — one root cause, both verified against
    the loader):

    * A quoted number (`session_idle_days: "15"`) rendered as `15`, read
      back as the int `15`, and compared unequal to the string `"15"`. That
      entry does not parse at all, so merely pressing Save on an unrelated
      field normalized it and took a rule that was inert **live** — a
      routing change nobody asked for.
    * A list item containing the join delimiter (`include: ["team,one"]` —
      a comma is an ordinary literal in the pattern language) rendered as
      `team,one` and read back as TWO patterns, `team` and `one`. Both
      shapes load cleanly, so the save gate had nothing to object to and
      the rule silently began claiming two different rooms.

    Composing and reading are the pair that must agree, so this applies the
    same render step `_compose_field_row()` uses and then
    `read_widget_value()` itself. An unparseable value (`"abc"` in an int
    field) is returned raw: Save is refused loudly on it either way, and
    inventing a value here would be the silent rewrite this exists to stop.

    Consequence worth stating: a malformed value cannot be repaired by
    merely opening the form and saving — that is the point (it is what made
    the rewrite silent). The row shows ERROR with the real message, and
    `ctrl+e`/`$EDITOR` remains the documented way to fix the raw file.
    """
    if spec.kind == "bool":
        return bool(value)
    if spec.kind == "enum":
        # The third site that snapped an unset enum to `options[0]`, and the one
        # that mattered most: this is the SNAPSHOT Save diffs against, so a rule
        # with no `agent:` snapshotted as the first agent in the file, the widget
        # showed it, nothing compared unequal, and Save wrote nothing. None in,
        # None out — a composed widget renders BLANK for it and reads BLANK back.
        options = spec.options or ()
        return value if value in options else None
    if spec.kind == "list":
        text = list_to_text(value)
    else:
        text = "" if value is None else str(value)
    try:
        return read_widget_value(spec, _ValueShim(text))
    except ValueError:
        return value


def set_widget_value(spec: FieldSpec, widget: object, value: object) -> None:
    """Set `widget`'s displayed value for `spec` — the inverse of
    `read_widget_value()`. Used by `action_reset_field()` to show what a
    field would display with zero explicit override (pure `*_defaults` /
    dataclass fallback), on an ALREADY-MOUNTED, focused widget — unlike
    `_compose_field_row()`, which sets the initial value via constructor
    kwargs before the widget ever mounts."""
    if spec.kind == "bool":
        widget.value = bool(value)
    elif spec.kind == "enum":
        options = spec.options or ()
        # Same rule as composition: no value means blank, not the first option.
        if value in options:
            widget.value = value
        elif widget._allow_blank:
            widget.clear()
        # A Select composed with a real value has allow_blank=False and cannot be
        # cleared; leaving it as-is is right, because "reset to no value" is not
        # a state the loader accepts for a required field anyway.
    elif spec.kind == "list":
        widget.value = list_to_text(value)
    else:
        widget.value = "" if value is None else str(value)


def sort_required_first(
    specs: tuple[FieldSpec, ...], required_keys: frozenset[str]
) -> tuple[FieldSpec, ...]:
    """Stable-sort `specs` so every key in `required_keys` comes first,
    preserving each group's own relative order — user-requested: required
    fields up front make it obvious at a glance which ones can't be left
    blank. A subclass's `_field_specs()` calls this with its own
    `_required_field_keys()`; a no-op (original order preserved) when
    `required_keys` is empty, e.g. `TemplateDetailScreen`, which never
    overrides `_required_field_keys()`."""
    return tuple(sorted(specs, key=lambda s: s.key not in required_keys))


def find_referencing_watcher_labels(
    cfg: EditableConfig, *, connector_name: str | None = None, agent_name: str | None = None
) -> list[str]:
    """Which watcher RULES currently reference the given connector and/or
    agent name — one label per rule, labelled by the rule's own `name`
    (required on a rule; a malformed entry without one falls back to its
    document position). Checked against the MERGED view (entry resolved
    against its own `inherits:` template) rather than the raw entry alone,
    so a rule whose `connector:`/`agent:` comes only from a template still
    blocks that connector/agent's deletion.

    A rule that names neither anywhere blocks nothing, and that is now the whole
    story: both fallbacks are gone, so such a rule is a load error in its own
    right rather than something silently bound to whatever came first in the
    file. The paragraph this replaces existed to engineer that outcome by hand —
    it made a fallback rule block the fallback entity's deletion, because
    deleting it left the config VALID while rebinding the rule without a word
    (Codex review of #129). Removing the fallbacks removes the need: the config
    no longer stays valid, so `save()`'s own validation is the backstop it was
    always claimed to be.
    """
    fallback_connector = None
    fallback_agent = None
    labels = []
    # The UNFILTERED document list, not `watchers_raw` — the `watchers[i]`
    # fallback label must use the same index space as every other consumer
    # of that spelling (the Rules tab's row numbers, the validator's
    # "(index i)"/"watchers[i]" attributions), and watchers_raw drops
    # non-mapping entries, shifting its indices relative to all of those.
    for i, entry in enumerate(cfg.watcher_entries):
        if not isinstance(entry, dict):
            continue
        try:
            merged = cfg.merged_entry("watcher", entry)
        except (ValueError, FileNotFoundError):
            merged = entry
        ref_connector = merged.get("connector") or fallback_connector
        ref_agent = merged.get("agent") or fallback_agent
        if connector_name is not None and ref_connector != connector_name:
            continue
        if agent_name is not None and ref_agent != agent_name:
            continue
        name = entry.get("name")
        labels.append(name if isinstance(name, str) and name else f"watchers[{i}]")
    return labels


def find_agents_referencing_preset(cfg: EditableConfig, preset_name: str) -> list[str]:
    """Which agents currently reference the given `tool_presets` entry, via
    either `owner_allowed_tools` or `guest_allowed_tools` — checked against
    the MERGED view (entry resolved against its own `inherits:` template, if
    any), not the raw entry alone, so a preset referenced only via an
    agent's `inherits:` template (common: shared across every agent that
    doesn't override its own tool list) still shows up as "used by" that
    agent. Used by both `ToolPresetsScreen` (its own used-by display) and
    `OverviewScreen`'s direct-delete-a-preset flow (blocks the delete with
    this list, same pre-check pattern `find_referencing_watcher_labels()`
    above uses for connectors/agents). If the config doesn't currently load,
    returns [] — same reasoning as that function: a delete pre-check has
    nothing useful to say about a config that doesn't parse; `save()`'s own
    validation remains the backstop.
    """
    used_by = []
    for name, entry in cfg.agents_raw.items():
        try:
            merged = cfg.merged_entry("agent", entry)
        except (ValueError, FileNotFoundError):
            merged = entry
        owner_tools = merged.get("owner_allowed_tools") or []
        guest_tools = merged.get("guest_allowed_tools") or []
        if preset_name in owner_tools or preset_name in guest_tools:
            used_by.append(name)
    return used_by


def find_entries_referencing_template(
    cfg: EditableConfig, kind: str, template_name: str
) -> list[tuple[str, dict]]:
    """Which entries of `kind` ('agent' | 'connector' | 'watcher') currently
    have `inherits: == template_name` — the blast-radius / delete-precheck
    primitive for a NAMED template, scoped to just that one template (unlike
    the old global `*_defaults` blocks, where "who's affected" meant "every
    entry in the config" — the whole point of named templates is that
    different entries can inherit different templates, so blast radius must
    be scoped accordingly). Direct analogue of
    `find_agents_referencing_preset()` above, generalized across kinds.

    Returns `(label, raw_entry)` pairs, not just labels — `TemplateDetailScreen`
    needs each entry's own dict too (to check "does this entry already
    override the field being changed"), not merely a count/name list. Callers
    that only need labels do `[name for name, _ in ...]`.
    """
    if kind == "agent":
        entries = list(cfg.agents_raw.items())
    elif kind == "connector":
        entries = [(e.get("name", "?"), e) for e in cfg.connectors_raw]
    else:
        # A rule's own `name` is required and unique (gateway/config.py's
        # rule parser), so it IS the label — no expanded-name derivation
        # left to do. A malformed entry without one falls back to its
        # document position — the UNFILTERED document index, matching
        # find_referencing_watcher_labels() and the validator's spellings.
        entries = []
        for i, w in enumerate(cfg.watcher_entries):
            if not isinstance(w, dict):
                continue
            name = w.get("name")
            label = name if isinstance(name, str) and name else f"watchers[{i}]"
            entries.append((label, w))
    return [(name, entry) for name, entry in entries if entry.get("inherits") == template_name]


class FormScreen(DetailScreen):
    """Base for the config TUI's view/edit/create entity screens. See
    module docstring for the subclass contract."""

    BINDINGS = [
        Binding("e", "edit", "Edit", show=True),
        Binding("d", "delete", "Delete", show=True),
        Binding("ctrl+s", "save", "Save", show=True),
        # Screen already binds tab/shift+tab to app.focus_next/focus_previous
        # with show=False (textual/screen.py) — re-bound here with show=True
        # (same pattern OverviewScreen uses for its own tab hint) so the
        # footer tells the user how to move between fields. An Up/Down
        # alternative was tried on AgentDetailScreen and reverted after
        # real-terminal testing showed it was unreliable in a way Pilot's
        # headless driver didn't catch — Tab is the one mechanism proven to
        # actually work everywhere.
        Binding("tab", "app.focus_next", "Next field", show=True),
        # User-reported gap: str/int/list fields can revert an explicit
        # override back to inherited by clearing the box to blank, but a
        # Checkbox/Select has no "blank" state — once touched, a bool/enum
        # field stayed explicit forever, even set back to the same value
        # the default already has. ctrl+r (not a plain letter — those get
        # swallowed by whichever Input has focus, same reason Up/Down
        # navigation didn't work) resets the FOCUSED field specifically,
        # regardless of kind.
        # Footer label deliberately says "to default", not just "Reset
        # field" (user-reported: the shorter wording reads as "undo to
        # whatever this field was before you started editing," which is
        # NOT what this does — see action_reset_field()'s own docstring).
        Binding("ctrl+r", "reset_field", "Reset to default", show=True),
        # User-requested (nice-to-have, not a bug): a way to check what's
        # actually in a masked secret field before saving. ctrl+t (NOT
        # ctrl+p — that's Textual's own App.COMMAND_PALETTE_BINDING, which
        # takes priority over any screen-level binding for the same key and
        # silently ate every keypress until this was caught by a failing
        # test) toggles the FOCUSED field's Input.password reactive —
        # masking is display-only (Input(password=True) never affects
        # .value), so this is purely cosmetic and doesn't touch anything
        # read_widget_value() or the diff logic sees.
        Binding("ctrl+t", "toggle_password_visibility", "Show/hide password", show=True),
    ]

    # Field-row layout (.entity-form/.field-row/.field-label/.field-provenance/
    # Checkbox/Input widths) moved to DetailScreen.DEFAULT_CSS (base.py) once
    # DefaultsScreen needed the exact same layout without extending
    # FormScreen — Textual's CSS type selectors match by ancestry, so
    # `DetailScreen .field-row` (the common ancestor) covers both without
    # duplicating the block here.

    def __init__(self):
        super().__init__()
        self.mode: Literal["view", "edit", "create"] = "view"
        # True when this screen was pushed ALREADY in edit mode (the list
        # page's direct-edit shortcut — see OverviewScreen.action_edit_row())
        # rather than reached via view mode's own 'e' key. Consulted by
        # action_back(): a screen that skipped view mode entirely has no
        # view state to "fall back" to — Escape (or a successful/cancelled
        # delete) must pop straight back to the list, not flip to a view
        # rendering of a screen the user never asked to see.
        self._started_in_edit_mode = False
        self._form_dirty = False
        self._initial_values: dict[str, object] = {}
        self._last_field_error: str | None = None
        # Fields explicitly reset via ctrl+r (action_reset_field()), mapped
        # to the value the field was set to display AT reset time. Consulted
        # by _collect_field_updates(): if the widget's CURRENT value still
        # matches, the field is written as "clear/revert to inherited"
        # regardless of what _initial_values says — the fix for the
        # bool/enum revert-to-inherited gap (str/int/list already had this
        # via clearing the box to blank). If the widget has since changed
        # away from the reset value, normal diffing takes back over.
        self._reset_keys: dict[str, object] = {}
        # list-kind fields whose RAW on-disk value contains the join
        # delimiter, mapped to that raw value. The comma-joined box cannot
        # represent them, so editing such a field is refused rather than
        # silently re-split — see list_value_is_lossy(). Populated by
        # _compute_initial_values(), consulted by _collect_field_updates().
        self._lossy_list_values: dict[str, object] = {}
        # Input/Select fire their own Changed message once at initial mount
        # with whatever value the constructor was given (confirmed
        # empirically — Checkbox does not, but Input/Select do). Without this
        # guard, simply OPENING the edit form would immediately mark it
        # dirty, incorrectly prompting a discard-confirmation on Escape even
        # though the user never touched anything. Cleared via
        # call_after_refresh, which runs after that initial burst of Changed
        # messages has already been processed.
        self._populating = False
        # Name/Description are entry-level fields NEVER set by a template
        # (no *_templates: block has a name or description of its own that
        # would deep-merge into an entry) — unlike every other field, an
        # inherits: switch's _recompute_form() must NOT touch them. Tracked
        # here (updated live by on_input_changed() below) rather than
        # re-derived from `_initial_values`/`entry` at every compose, since
        # `_recompute_form()` calls `_compute_initial_values(self._current_entry())`
        # — which only knows about `self.entry`, never about whatever the
        # user has typed into these two boxes but not yet saved — and
        # `_compose_form()` used to source both Inputs' `value=` from that
        # stale, recomputed state, silently discarding an in-progress Name/
        # Description edit the instant a template was picked. Seeded once at
        # the two legitimate "fresh start" points (`__init__`/
        # `_on_enter_edit_mode()`), deliberately untouched by
        # `_recompute_form()` so they survive any number of template
        # switches in between.
        self._description_live: str = ""
        self._name_live: str = ""

    def on_mount(self) -> None:
        if self._populating:
            self.call_after_refresh(self._stop_populating)

    def _stop_populating(self) -> None:
        self._populating = False

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Hide 'Edit'/'Delete' from the footer once already editing/creating
        (both are no-ops there; a footer hint for a no-op key reads as
        broken, not just redundant), and hide 'Save' while still in view
        mode (nothing to save yet)."""
        if action in ("edit", "delete"):
            return self.mode == "view"
        if action in ("save", "reset_field", "toggle_password_visibility"):
            return self.mode != "view"
        return True

    # ── abstract hooks subclasses must implement ────────────────────────────

    def _field_specs(self) -> tuple[FieldSpec, ...]:
        raise NotImplementedError

    def _template_kind(self) -> str:
        raise NotImplementedError

    def _dataclass_defaults(self) -> dict[str, object]:
        raise NotImplementedError

    def _required_field_keys(self) -> frozenset[str]:
        """Which of _field_specs()'s own keys are genuinely required to Save
        right now (may depend on entity-specific state, e.g. connector
        `type` — mirrors `_field_specs()` itself). Used by
        `_compose_field_row()` to append a trailing '*' to the label, and by
        subclasses' own `_field_specs()` to sort required fields first
        (`sort_required_first()` below). Empty by default: TemplateDetailScreen
        never overrides this — nothing in a template is truly "required," it's
        an optional override layer merged into whatever entry inherits it."""
        return frozenset()

    def _compose_form(self) -> ComposeResult:
        raise NotImplementedError

    def _entity_label(self) -> str:
        """Used in the delete-confirmation message and the post-delete
        notification (e.g. an agent/connector's name)."""
        raise NotImplementedError

    def _current_entry(self) -> dict:
        """This screen's own raw entry dict (`self.entry` on every concrete
        subclass) — used by `action_reset_field()`, which has no natural
        parameter-passing path (it's a key-binding handler, not called from
        `_compose_form()` the way `_compose_field_row()` is)."""
        raise NotImplementedError

    def _remove_entry_from_document(self) -> None:
        """Delete this entity's raw entry from `self.cfg.document` in place.
        Must record whatever this subclass needs (e.g. the entry's index in
        a list) to support `_reinsert_entry_into_document()` undoing it."""
        raise NotImplementedError

    def _reinsert_entry_into_document(self) -> None:
        """Undo `_remove_entry_from_document()` — called when `save()`
        rejects the deletion (e.g. a watcher still references this entity)."""
        raise NotImplementedError

    def _install_trial_entry(self, target_entry: dict) -> None:
        """EDIT mode only: temporarily substitute `target_entry` (a COPY of
        `self.entry` with this Save's updates already applied) into
        `self.cfg.document`, in place of the original. This runs BEFORE
        `save()` — never mutate `self.entry` itself here, or a rejected
        save leaves invalid data sitting in the document even though
        nothing was ever written to disk (a real bug: user-reported that
        setting an invalid value, having Save fail, then pressing Back
        still showed the invalid value — because the old code mutated
        the SAME dict object `document` already held). Call
        `_rollback_trial_entry()` if `save()` rejects it."""
        raise NotImplementedError

    def _rollback_trial_entry(self) -> None:
        """Undo `_install_trial_entry()` — restore the ORIGINAL `self.entry`
        (untouched) into `document`. Called when `save()` rejects the trial."""
        raise NotImplementedError

    def _referencing_watcher_labels(self) -> list[str]:
        """Which watchers (if any) currently reference this entity — checked
        BEFORE the destructive confirm, so a blocked delete gets a clear
        reason instead of the generic validator error `save()` would
        otherwise surface. Subclasses call `find_referencing_watcher_labels()`
        with their own kind of name."""
        raise NotImplementedError

    def _delete_blocker_noun(self) -> str:
        """The noun used in the delete-blocked message ("still used by
        {noun}(s): ..."). Unchanged default for Agent/ConnectorDetailScreen
        (blocked by referencing watcher rules — `_referencing_watcher_labels()`
        returns RULE names); `TemplateDetailScreen` overrides this to its own
        `kind` (blocked by referencing agents/connectors/rules instead)."""
        return "watcher rule"

    def _delete_confirm_message(self) -> str:
        """The ConfirmModal text `_do_delete()` shows. Overridable so a
        subclass can append entity-specific consequences (RuleDetailScreen
        adds the stranded-session/orphaned-job counts design §5.5 requires)
        without duplicating the whole delete flow."""
        return (
            f"Delete {self._entity_noun()} '{markup_safe(self._entity_label())}'? "
            "This cannot be undone."
        )

    async def action_save(self) -> None:
        raise NotImplementedError

    # ── compose dispatch ─────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        if self.mode == "view":
            yield VerticalScroll(Static(self._body_text(), id=self.BODY_ID))
        else:
            yield from self._compose_form()
        yield Footer()

    # ── generic field snapshot / provenance / row rendering ─────────────────

    def _snapshot_source(self, entry: dict) -> dict:
        """Which mapping `_compute_initial_values()` reads field values from.

        The ONE thing that differed between this class's snapshot logic and
        `TemplateDetailScreen`'s, which used to override the whole method to
        change it — and that fork is what made a fix to field handling land
        in one copy only (Codex review of #129, round 7: the raw
        delimiter-bearing display reached the entry forms and not the
        template form). Expressed as a hook so there is one implementation of
        the snapshot itself.

        An entry resolves against its `inherits:` template; a template has
        nothing to resolve against (no nested templates), so it overrides
        this to return the raw entry.
        """
        try:
            return self.cfg.merged_entry(self._template_kind(), entry)
        except (ValueError, FileNotFoundError):
            return dict(entry)

    def _compute_initial_values(self, entry: dict) -> None:
        self._reset_keys = {}  # fresh edit session — no lingering reset markers
        self._lossy_list_values = {}
        merged = self._snapshot_source(entry)
        dataclass_defaults = self._dataclass_defaults()
        for spec in self._field_specs():
            value = get_nested(merged, spec.key)
            if value is None:
                value = dataclass_defaults.get(spec.key)
            # ROUND-TRIPPED, not raw: the snapshot has to be what a
            # freshly-composed widget for this field would read back, or an
            # untouched field compares unequal and Save rewrites it. This
            # replaces (and subsumes) a narrower "str"/"list" falsy-to-None
            # normalization that lived here: an effective value of "" or []
            # renders as an empty box, whose readback is None, so the raw
            # value looked "changed" to both _collect_field_updates() (Save
            # writing a semantically wrong explicit null onto an untouched
            # field) and _field_has_override() (a false-positive "you'll
            # lose this edit" confirm on an inherits: switch). Two further
            # cases the narrow version missed — a quoted number, and a list
            # item containing the join delimiter — are documented on
            # round_trip_value(), which is now the single place this
            # render-and-read-back agreement is expressed.
            if spec.kind == "list" and list_value_is_lossy(value):
                self._lossy_list_values[spec.key] = value
            self._initial_values[spec.key] = round_trip_value(spec, value)
        # Coerced to TEXT, because `description` is informational and the
        # loader does not type-check it — `description: [note]` loads fine
        # (verified: 1 rule parsed, 0 issues) and then reached
        # `Input(value=[...])`, which raises AttributeError DURING COMPOSE and
        # takes the TUI down on the very row the operator opened to inspect
        # it (Codex review of #129, round 9).
        #
        # Round-trip consistent, like every other snapshot here (see
        # `round_trip_value`): the box shows `str(value)` and reads that same
        # text back, so an untouched Save writes nothing and the odd on-disk
        # value is preserved rather than silently repaired into a string.
        raw_description = entry.get("description")
        self._initial_values["description"] = (
            raw_description
            if raw_description is None or isinstance(raw_description, str)
            else str(raw_description)
        )

    def _field_provenance(self, spec: FieldSpec, entry: dict) -> Provenance | None:
        # The FULL key, dots included — `field_provenance()` answers a nested
        # field per sub-key, because that is how the merge treats it. Passing
        # only the top key reported one verdict for every sub-key of a block,
        # so a template-supplied `rooms.include` read "(explicit)" next to the
        # `rooms.direct` the entry had actually set.
        try:
            return self.cfg.field_provenance(self._template_kind(), entry, spec.key)
        except (ValueError, FileNotFoundError):
            return None

    def _field_annotation(
        self, spec: FieldSpec, entry: dict, provenance: Provenance | None | object = _UNSET
    ) -> str:
        """The trailing annotation rendered beside `spec`'s widget.

        The ONLY thing `TemplateDetailScreen`'s row rendering differed in,
        which used to justify overriding the whole of `_compose_field_row()`
        — the fork that let a field-handling fix land in one renderer only
        (Codex review of #129, round 7). A hook instead, so there is one
        renderer.

        This class annotates with the field's PROVENANCE (explicit /
        inherited / default); a template annotates with its BLAST RADIUS
        instead, since its own fields have no provenance to speak of.
        `provenance` may be passed explicitly by the live-refresh path,
        which computes a would-be-on-Save value rather than the entry's
        current one.
        """
        if provenance is _UNSET:
            provenance = self._field_provenance(spec, entry)
        if not provenance:
            return ""
        if provenance is Provenance.DEFAULT and spec.key in self._required_field_keys():
            # "(default)" names a fallback value this field does not have: a
            # required field left unset does not evaluate to anything, the config
            # simply fails to load. Saying "default" told the operator the form
            # was showing them a working value — which is how a rule with no
            # `agent:` read as fine while being unloadable (user-reported).
            return "[dim](not set — required)[/dim]"
        template_name = self.cfg.entry_template_name(entry)
        return f"[dim]({provenance_label(provenance, template_name)})[/dim]"

    def _compose_field_row(self, spec: FieldSpec, entry: dict) -> ComposeResult:
        prov_text = self._field_annotation(spec, entry)
        initial = self._initial_values.get(spec.key)
        # A delimiter-bearing list value is DISPLAYED raw, not round-tripped:
        # the snapshot for `["my,notes.md"]` is the split `["my","notes.md"]`,
        # which would render as `my, notes.md` and show two entries where the
        # file has one — misrepresenting a load-bearing value (a path may
        # legitimately contain a comma). The box therefore shows what is
        # really on disk, and editing it is refused rather than re-split
        # (see list_value_is_lossy()). Readback is unaffected: both spellings
        # parse to the same list, so an untouched field still compares equal.
        if spec.key in self._lossy_list_values:
            initial = self._lossy_list_values[spec.key]
        # User-requested: a trailing '*' marks a field that's actually
        # required to Save right now — not obvious otherwise which fields
        # can be safely left blank.
        label = spec.label + " *" if spec.key in self._required_field_keys() else spec.label
        with Horizontal(classes="field-row"):
            yield Static(label, classes="field-label")
            if spec.kind == "bool":
                widget = Checkbox(value=bool(initial), id=widget_id(spec.key))
            elif spec.kind == "enum":
                options = spec.options or ()
                # A field with no value anywhere renders BLANK, and only then is
                # blank offered. Snapping to `options[0]` instead is what let a
                # rule with no `agent:` display the first agent in the file:
                # the snapshot took that invented value, Save diffed against it,
                # saw no change, and wrote nothing — leaving a rule that still
                # would not load (user-reported). A field that HAS a value keeps
                # `allow_blank=False`, since clearing one to blank would only
                # produce a config the loader refuses.
                has_value = initial in options
                widget = Select(
                    # (label, value): the LABEL is escaped because Textual
                    # renders it as markup inside the Select itself (a
                    # connector/agent named `agent-[/]` raised MarkupError
                    # from Select._watch_value, killing the form) — the VALUE
                    # stays raw, since that is what gets written to config.
                    [(markup_safe(o), o) for o in options],
                    # `Select.NULL`, NOT `Select.BLANK` — the latter is a
                    # deprecated alias equal to `False` in Textual 8.x, which
                    # the widget then rejects as an illegal value.
                    value=initial if has_value else Select.NULL,
                    allow_blank=not has_value,
                    id=widget_id(spec.key),
                )
            elif spec.kind == "list":
                widget = Input(value=list_to_text(initial), id=widget_id(spec.key))
            else:
                widget = Input(
                    value="" if initial is None else str(initial),
                    id=widget_id(spec.key),
                    password=spec.secret,
                )
            # Tagged so a focused widget can be mapped back to its FieldSpec
            # (action_reset_field()) without unmunging widget_id()'s
            # dot-to-dash id transform, which would be ambiguous for any
            # future field key containing a literal dash.
            widget.field_key = spec.key
            yield widget
            # Stable id (not just the shared .field-provenance class) so
            # _refresh_provenance_display() below can update THIS ONE row's
            # label live, without touching any other row's.
            yield Static(prov_text, id=f"prov-{widget_id(spec.key)}", classes="field-provenance")

    # ── dirty tracking (per-screen, not EditableConfig.dirty — nothing is
    # written to `document` until Save, so cfg.dirty stays False the whole
    # time the user is mid-form; this local flag is what Escape checks) ──────

    def on_input_changed(self, event: Input.Changed) -> None:
        if self._populating:
            return
        if event.input.id == "field-description":
            self._description_live = event.input.value
        elif event.input.id == "field-name":
            self._name_live = event.input.value
        self._form_dirty = True
        self._refresh_provenance_display(getattr(event.input, "field_key", None))

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if self._populating:
            return
        self._form_dirty = True
        self._refresh_provenance_display(getattr(event.checkbox, "field_key", None))

    def on_select_changed(self, event: Select.Changed) -> None:
        if self._populating:
            return
        self._form_dirty = True
        self._refresh_provenance_display(getattr(event.select, "field_key", None))

    def _field_has_override(self, spec: FieldSpec) -> bool:
        """Has THIS field's widget been changed from the value it was
        prefilled with? Used both by the live provenance-label refresh
        below and by the Inherits-switch confirm (agent_detail.py/
        connector_detail.py): "would switching templates silently discard
        something I already typed?" A field whose value fails to parse
        (e.g. mid-typing a bad int) counts as overridden — safer to over-
        warn than to silently treat unparseable input as untouched."""
        try:
            widget = self.query_one("#" + widget_id(spec.key))
        except NoMatches:
            return False
        try:
            current = read_widget_value(spec, widget)
        except ValueError:
            return True
        return current != self._initial_values.get(spec.key)

    def _any_field_overridden(self) -> bool:
        return any(self._field_has_override(spec) for spec in self._field_specs())

    def _refresh_provenance_display(self, field_key: str | None) -> None:
        """Keep the changed field's provenance label truthful to what Save
        would actually do with it RIGHT NOW — user-reported: picking a new
        inherits: template, then typing into a field, left its label
        showing the OLD "(from '<template>')"/"(default)" text even though
        the field is now explicit (about to be written as an override on
        Save). Mirrors _collect_field_updates()'s own reset-vs-changed
        logic exactly, so the label is never wrong relative to Save: a
        field ctrl+r'd back to its reset value shows what it WOULD BECOME
        on Save (Save pops the key entirely — computed against a probe with
        this field removed, not against `self._current_entry()` as-is,
        which still has the key and would otherwise keep reporting EXPLICIT
        forever after a ctrl+r); any other value that differs from the
        snapshot shows EXPLICIT; unchanged shows the ORIGINAL provenance
        unchanged. A no-op if `field_key` isn't a real field (the Name/
        Description inputs) or the row wasn't actually composed with a
        label (TemplateDetailScreen's _field_provenance() always returns
        None, so its rows have no label text to touch)."""
        if field_key is None:
            return
        spec = next((s for s in self._field_specs() if s.key == field_key), None)
        if spec is None:
            return
        try:
            prov_widget = self.query_one(f"#prov-{widget_id(spec.key)}", Static)
            widget = self.query_one("#" + widget_id(spec.key))
        except NoMatches:
            return
        try:
            current = read_widget_value(spec, widget)
        except ValueError:
            return  # mid-typing an invalid value — leave the label as-is

        entry = self._current_entry()
        if field_key in self._reset_keys and current == self._reset_keys[field_key]:
            # PR review finding: for a dotted (one-level-nested) spec.key
            # like "permissions.timeout", popping the WHOLE top-level
            # parent (as a previous version of this did) diverges from
            # what Save actually does — apply_update() only removes the
            # one sub-key, keeping the parent dict (and its still-explicit
            # sibling sub-keys) if anything remains in it. Mirrored here so
            # a reset field's live label matches what Save would really
            # produce instead of contradicting its own explicit siblings.
            reverted_probe = dict(entry)
            if "." in spec.key:
                parent_key, sub_key = spec.key.split(".", 1)
                parent = dict(entry.get(parent_key) or {})
                parent.pop(sub_key, None)
                if parent:
                    reverted_probe[parent_key] = parent
                else:
                    reverted_probe.pop(parent_key, None)
            else:
                reverted_probe.pop(spec.key, None)
            provenance = self._field_provenance(spec, reverted_probe)
        elif current != self._initial_values.get(field_key):
            provenance = Provenance.EXPLICIT
        else:
            provenance = self._field_provenance(spec, entry)
        # Through the same annotation hook the row was composed with, so a
        # screen that annotates differently (a template's blast radius) has
        # its annotation refreshed correctly instead of blanked by
        # provenance logic that does not apply to it.
        prov_widget.update(self._field_annotation(spec, entry, provenance))

    def action_reset_field(self) -> None:
        """ctrl+r: reset the FOCUSED field to its pure-template/dataclass
        value (no explicit override on THIS entry) — see the `_reset_keys`
        field comment for how this becomes an actual "revert to inherited"
        on Save, regardless of field kind. A no-op if focus isn't on a
        resettable field (e.g. the Name/Description inputs, which aren't
        tagged with `field_key` — see `_compose_field_row()` — since neither
        has an `inherits:` concept).

        Resolves against the entry's OWN `inherits:` name (if any) — NOT
        against an empty entry — so resetting one field doesn't lose the
        entry's other fields' effective values from whichever template it
        actually opted into (an empty-entry probe would silently behave as
        "no inherits: at all," which is only correct for the field being
        reset itself, not a valid stand-in for the whole entry)."""
        widget = self.focused
        field_key = getattr(widget, "field_key", None)
        if field_key is None:
            return
        spec = next((s for s in self._field_specs() if s.key == field_key), None)
        if spec is None:
            return

        inherits_name = self._current_entry().get("inherits")
        probe = {"inherits": inherits_name} if inherits_name is not None else {}
        try:
            defaults_only = self.cfg.merged_entry(self._template_kind(), probe)
        except (ValueError, FileNotFoundError):
            defaults_only = {}
        value = get_nested(defaults_only, spec.key)
        if value is None:
            value = self._dataclass_defaults().get(spec.key)

        set_widget_value(spec, widget, value)
        self._reset_keys[spec.key] = read_widget_value(spec, widget)
        self._form_dirty = True
        # Called directly, NOT left to the widget's own Changed event. When the
        # reset value equals what the field already displayed — a template that
        # sets the same `direct: true` the entry sets explicitly, say — Textual
        # fires no Changed event, so the label kept reading "(explicit)" next to
        # a toast promising it would revert. Reset is the one path where the
        # label must change while the value does not.
        self._refresh_provenance_display(spec.key)
        self.notify(f"{spec.label}: will revert to inherited on Save.", severity="information")

    def action_toggle_password_visibility(self) -> None:
        """ctrl+t: reveal/re-mask the FOCUSED secret field. A no-op if focus
        isn't on a masked Input (Input.password is always False for a
        non-secret field, so toggling it there would be a silent, confusing
        no-visible-effect action — checked explicitly via the field's own
        FieldSpec.secret, not just "is this an Input")."""
        widget = self.focused
        field_key = getattr(widget, "field_key", None)
        if field_key is None or not isinstance(widget, Input):
            return
        spec = next((s for s in self._field_specs() if s.key == field_key), None)
        if spec is None or not spec.secret:
            return
        widget.password = not widget.password

    async def _recompute_form(self) -> None:
        """Rebuild every field's prefilled value AND provenance label from
        scratch against `self._current_entry()` — used when something
        changes that affects EVERY other field's effective value (an
        `inherits:` template switch, in `agent_detail.py`/
        `connector_detail.py`), unlike a normal single-field edit (which
        only ever affects itself, handled by `_refresh_provenance_display()`
        above). Discards any of the user's own not-yet-saved edits to OTHER
        fields made earlier in this same session — callers are expected to
        confirm that with the user first (`_any_field_overridden()`) before
        calling this."""
        self._compute_initial_values(self._current_entry())
        self._populating = True
        await self.recompose()
        self.call_after_refresh(self._stop_populating)
        self.refresh_bindings()

    # ── navigation ───────────────────────────────────────────────────────────

    async def action_edit(self) -> None:
        if self.mode != "view":
            return
        self.mode = "edit"
        self._form_dirty = False
        self._on_enter_edit_mode()
        # recompose() does NOT re-trigger on_mount (that only fires once, for
        # the screen's own initial push) — so the populating guard has to be
        # armed and disarmed around this recompose explicitly, the same way
        # on_mount handles it for a screen pushed directly in create mode.
        self._populating = True
        await self.recompose()
        self.call_after_refresh(self._stop_populating)
        # Footer subscribes to Screen.bindings_updated_signal in ITS OWN
        # on_mount — recompose() mounts a brand-new Footer instance, but
        # nothing re-publishes that signal just because a new subscriber
        # showed up, so the fresh Footer's `_bindings_ready` reactive stays
        # False forever and it renders as a blank bar (confirmed empirically
        # while building AgentDetailScreen: 4 FooterKey children at first
        # mount, 0 after this recompose, permanently, across every later
        # transition too). refresh_bindings() is Screen's own public method
        # for exactly this: it re-publishes the signal so every current
        # subscriber (including the new Footer) recomputes — and also
        # re-evaluates check_action() for every binding, so the 'e'/'ctrl+s'
        # visibility flips immediately on this same recompose.
        self.refresh_bindings()

    def _on_enter_edit_mode(self) -> None:
        """Hook for subclass-specific setup right before the edit-mode
        recompose (AgentDetailScreen recomputes _initial_values here)."""

    @work
    async def action_back(self) -> None:
        if self.mode == "view":
            self.app.pop_screen()
            return
        if self._form_dirty:
            discard = await self.app.push_screen_wait(
                ConfirmModal(
                    f"Discard unsaved changes to this {self._entity_noun()}?",
                    confirm_label="Discard",
                )
            )
            if not discard:
                return
        if self.mode == "create" or self._started_in_edit_mode:
            # create: no view state to fall back to (never had one).
            # started_in_edit_mode: this screen skipped view mode entirely
            # (list page's direct-edit shortcut) — falling back to a view
            # rendering here would show the user a screen they never asked
            # to see instead of returning them to the list, as every other
            # exit from this shortcut (Save, a blocked/cancelled delete)
            # already does.
            self.app.pop_screen()
        else:
            self.mode = "view"
            self._form_dirty = False
            await self.recompose()
            self.refresh_bindings()  # see action_edit()'s comment — same fix

    def _entity_noun(self) -> str:
        """Used in the discard- and delete-confirmation messages ("... to
        this agent?" / "... to this connector?")."""
        return "entry"

    @work
    async def action_delete(self) -> None:
        """'d', view mode only (see check_action). Thin @work wrapper around
        _do_delete() — kept separate so OverviewScreen's direct-delete-from-
        the-list shortcut (action_delete_row()) can call _do_delete()
        directly as a plain coroutine instead of nesting one @work worker
        inside another. Nesting a second @work call and awaiting it via
        Worker.wait() was tried first and found to be fragile: if the outer
        worker (or the whole app/test) is torn down while the inner one is
        still suspended at a push_screen_wait(), Worker.wait() re-raises
        that as WorkerCancelled INSIDE the outer worker's own body — an
        unrelated-looking crash with no bug in the delete logic itself.
        A plain awaited coroutine has no such failure mode."""
        await self._do_delete()

    async def _do_delete(self) -> None:
        """Checks for referencing watchers FIRST (a clear, specific reason
        beats a generic validator error) — if any exist, shows that reason
        and stops before even offering the destructive confirm. Otherwise:
        confirm -> remove from `document` -> save(). save() remains the
        backstop even after the pre-check (belt-and-suspenders, not a
        replacement for it) — if it still rejects the deletion for some
        reason the pre-check didn't anticipate, the entry is reinserted so
        a rejected delete never leaves `document` silently missing
        something that's still on disk.
        """
        if self.mode != "view":
            return

        blockers = self._referencing_watcher_labels()
        if blockers:
            await self.app.push_screen_wait(
                MessageModal(
                    # Escaped: the blockers are RULE names now, which are
                    # unrestricted strings (see markup_safe()).
                    f"Cannot delete {self._entity_noun()} "
                    f"'{markup_safe(self._entity_label())}' — still used by "
                    f"{self._delete_blocker_noun()}(s): "
                    f"{', '.join(markup_safe(b) for b in blockers)}.",
                    title="Cannot delete",
                )
            )
            return

        confirmed = await self.app.push_screen_wait(
            ConfirmModal(self._delete_confirm_message(), confirm_label="Delete")
        )
        if not confirmed:
            return

        self._remove_entry_from_document()
        self.cfg.mark_dirty()
        try:
            self.cfg.save()
        except (ValueError, FileNotFoundError) as exc:
            self._reinsert_entry_into_document()
            await self.app.push_screen_wait(MessageModal(markup_safe(exc), title="Could not delete"))
            return

        self.app.pop_screen()
        app = self.app
        app.notify(f"Deleted {self._entity_noun()} '{self._entity_label()}'.", severity="information")
        app.reload_config()  # type: ignore[attr-defined]

    # ── generic diff collection (Save calls this, then applies the result
    # however this entity needs to — see module docstring) ──────────────────

    def _collect_field_updates(self) -> dict[str, object] | None:
        """Diff every form widget against `_initial_values`. Returns
        {dotted_key: new_value_or_None} for changed fields only (None means
        "clear it — revert to inherited/default"), or None if a field fails
        to parse — the message is stashed in `self._last_field_error` rather
        than shown directly (this method is sync; the caller, action_save(),
        is the one in a position to `await` a `MessageModal`)."""
        self._last_field_error: str | None = None
        updates: dict[str, object] = {}
        for spec in self._field_specs():
            widget = self.query_one("#" + widget_id(spec.key))
            try:
                new_value = read_widget_value(spec, widget)
            except ValueError as exc:
                self._last_field_error = str(exc)
                return None
            # A field ctrl+r-reset earlier this session (action_reset_field())
            # always clears to inherited on Save, REGARDLESS of
            # _initial_values — this is what makes bool/enum fields able to
            # revert to inherited at all (they have no "blank" state to
            # clear, unlike str/int/list). Only holds if the widget still
            # shows the value reset set it to; if the user changed it again
            # since, this falls through to the normal diff below.
            if spec.key in self._reset_keys and new_value == self._reset_keys[spec.key]:
                updates[spec.key] = None
                continue
            if new_value != self._initial_values.get(spec.key):
                # An edit to a list field whose raw value holds an item
                # containing the join delimiter cannot be applied without
                # silently re-splitting that item (the box has no way to
                # express it). Refused loudly, via the same error path a
                # bad int takes — see list_value_is_lossy(). An UNTOUCHED
                # field never reaches here: its round-tripped snapshot
                # equals the readback, so the comparison above is False.
                raw = self._lossy_list_values.get(spec.key)
                if raw is not None:
                    # NOT escaped here: `_last_field_error` is escaped once
                    # at its sink, where every producer's message is (round
                    # 10). This message used to escape its own value too,
                    # and the two together double-escaped it — the modal
                    # displayed `my,\[/].md`, a backslash the file does not
                    # contain. Escape at the boundary, exactly once.
                    self._last_field_error = (
                        f"{spec.label}: this list contains an item with a "
                        f"comma in it ({list_to_text(raw)!r}), which this "
                        "comma-separated box cannot represent — editing it "
                        "here would split that item in two. Edit this field "
                        "in $EDITOR instead (ctrl+e on the list screen)."
                    )
                    return None
                updates[spec.key] = new_value

        desc_widget = self.query_one("#field-description", Input)
        new_desc = desc_widget.value.strip() or None
        if new_desc != self._initial_values.get("description"):
            updates["description"] = new_desc
        return updates
