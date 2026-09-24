"""Small display-formatting helpers shared across config TUI screens."""

from __future__ import annotations

from rich.markup import escape as _rich_escape

from .model import Provenance


def markup_safe(value: object) -> str:
    """`value` as text that Rich will render VERBATIM.

    Every `Static`/`DataTable` cell in this TUI parses Rich markup, so a
    square bracket in operator-authored data is read as a tag. That is not
    exotic here: `[…]` is documented, first-class room-pattern syntax
    (`gateway/core/room_pattern.py`), and rule names are unrestricted
    strings. Both failures were confirmed (Codex review of #129, round 5):

    * `eng-[ab]` — a legitimate character-class pattern — rendered as
      `eng-`, so the display CONCEALED which rooms the rule actually
      claims. The worse of the two: a reader is told the wrong routing.
    * A rule named `[/]` raised Rich's `MarkupError` when its row was
      opened, making that rule unviewable, uneditable and undeletable
      through the TUI.

    Wrap any dynamic value interpolated into markup-bearing content in
    this; deliberately NOT applied inside `format_value()`/
    `rule_rooms_summary()` themselves, so those stay usable for plain-text
    callers (tests compare their raw output) and escaping stays visible at
    the point where markup is actually being built.
    """
    return _rich_escape("" if value is None else str(value))

# Template kinds whose display name differs from their internal one. The kind
# strings are config-key fragments — every writer builds `f"{kind}_templates"`
# from one — so the name shown to an operator cannot simply BE the kind, and a
# screen that interpolates a bare `kind` into a sentence shows the internal
# spelling by accident. That is how the Templates tab came to offer "watcher"
# next to a "Watcher Rules" tab and a config key called `watcher_rules`.
_KIND_LABELS = {"watcher": "watcher rule"}


def kind_label(kind: str) -> str:
    """The display name for a template kind (`agent`/`connector`/`watcher`).

    Lowercase, so it reads correctly mid-sentence ("Delete watcher rule
    template 'x'?"); capitalise at the call site when it starts one.
    """
    return _KIND_LABELS.get(kind, kind)


# Key names whose values are masked when rendered — credentials only, never
# url/host/team/username.
_SECRET_KEY_NAMES = frozenset({"password", "secret", "token"})

def provenance_label(provenance: Provenance, template_name: str | None = None) -> str:
    """Render a provenance value for display. `template_name` is the
    entry's own `inherits:` value (from `EditableConfig.entry_template_name()`),
    if any — threaded through so INHERITED/EXPLICIT_SUPPRESSING can name which
    template is involved, and so DEFAULT can distinguish "no inherits: at
    all" from "inherits: set, but that template doesn't set this field".

    Deliberately terse (not e.g. "inherited from template '{name}'") — this
    renders inside a fixed-width `.field-provenance` column
    (`test_provenance_markers_are_within_the_visible_terminal_width` pins
    this at a 120-col terminal), and a form with several DEFAULT-with-
    template fields (any template that only sets a couple of keys leaves
    most fields in this state) renders one of these per row — verbose
    wording here multiplies into real, visible overflow, not just a cosmetic
    nit on one row."""
    if provenance == Provenance.EXPLICIT:
        return "explicit"
    # Escaped here rather than at each call site: this is the single point
    # where a template NAME (an unrestricted YAML key) reaches markup, and
    # every screen's field rows go through it.
    safe_name = markup_safe(template_name)
    if provenance == Provenance.INHERITED:
        return f"from '{safe_name}'"
    if provenance == Provenance.EXPLICIT_SUPPRESSING:
        return f"clears '{safe_name}'"
    # Provenance.DEFAULT
    if template_name is None:
        return "default"
    return f"default (no '{safe_name}')"


def mask_if_secret(key: str, value: object) -> str:
    """Render `value`, masking it if `key` looks like a credential field."""
    if key.lower() in _SECRET_KEY_NAMES and value:
        return "•" * 8
    return format_value(value)


def format_value(value: object) -> str:
    """Compact single-line rendering of a scalar/list/dict value for display."""
    if value is None:
        return "(none)"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, list):
        return ", ".join(format_value(v) for v in value) or "(empty)"
    if isinstance(value, dict):
        return ", ".join(f"{k}={mask_if_secret(k, v)}" for k, v in value.items()) or "(empty)"
    return str(value)


def status_badge(status: str) -> str:
    """Rich markup for a status string ('ok' | 'warning' | 'error' | 'lint')."""
    return {
        "ok": "[green]OK[/green]",
        "warning": "[yellow]WARN[/yellow]",
        "error": "[red]ERROR[/red]",
        "lint": "[cyan]lint[/cyan]",
    }.get(status, status)
