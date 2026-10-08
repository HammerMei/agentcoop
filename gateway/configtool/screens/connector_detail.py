"""ConnectorDetailScreen — view, edit, and create a single connector.

Connector `raw` is deliberately type-flexible in the schema
(gateway/schema/config.schema.json's connector definition has no
`additionalProperties: false`) — unlike Agent/RuleDetailScreen, there's
no single closed field list. The design originally called for a generic
recursive tree editor to handle arbitrary/unknown keys; **deferred** here in
favor of per-type fixed field lists (`FIELDS_BY_TYPE` below), one level of
nesting matching every real connector type's actual raw shape exactly
(`server.url`, `allowed_users.owners`, etc. — verified against all 4 types'
own `from_connector_config()` before choosing this). The generic tree editor
would only earn its complexity for truly arbitrary/unknown keys, and the
`$EDITOR` escape hatch already covers that case (docs/design/config-tool.md's
screen inventory: "covers what forms don't") — build it later if per-type
forms plus `$EDITOR` turn out not to be enough in practice, not preemptively.

`type` is immutable once a connector exists (only chosen via `TypePickerModal`
at creation, through `OverviewScreen.action_new_entity`) — rocketchat's and
mattermost's raw shapes differ enough that letting `type` change in place
would mean the form reshaping itself around one of its own fields' value.
Changing a connector's type after creation is a rare, advanced operation;
`$EDITOR` remains available for it. `name` is likewise immutable in edit
mode — watchers reference a connector by name (`connector: <name>`), so a
rename would silently orphan them.
"""

from __future__ import annotations

from typing import Literal

from textual import work
from textual.app import ComposeResult
from textual.containers import Container, Horizontal, VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import Button, Input, Select, Static

from ..formatting import markup_safe, mask_if_secret, provenance_label
from ..modals import (
    ConfirmModal,
    InheritsPickerModal,
    MessageModal,
    TextPromptModal,
    TypePickerModal,
)
from ..model import EditableConfig
from .form_common import (
    FieldSpec,
    FormScreen,
    apply_update,
    find_referencing_watcher_labels,
    sort_required_first,
    widget_id,
)

# NOTE: TemplateDetailScreen (screens/template_detail.py) is deliberately
# imported LOCALLY inside _open_inherits_picker() below, not at module level —
# template_detail.py itself imports FIELDS_BY_TYPE/DATACLASS_DEFAULTS_BY_TYPE
# from this module, so a module-level import here would be circular.

CONNECTOR_TYPES = ("rocketchat", "mattermost", "voice", "script")

# Connector types flagged "(experimental)" in every TypePickerModal that
# offers a connector type choice — user-requested. 'voice' matches
# docs/supported-features.md's own "Experimental" status row (POC-quality,
# known timeout race); 'script' is stable for AD-HOC scripting
# (docs/architecture.md's "Testing and Scripting" section) but declaring
# one as a CONFIGURED connector — the headless-scheduling use case
# (docs/scheduling.md) — is comparatively undocumented/untested next to
# rocketchat/mattermost, so it gets the same caveat here.
_EXPERIMENTAL_CONNECTOR_TYPES = frozenset({"voice", "script"})

# (value, display label) pairs for TypePickerModal — value is what actually
# gets written as `type:` (see TypePickerModal.__init__()'s own docstring:
# dismiss() always returns the VALUE half, never the label), label is what
# the picker shows.
CONNECTOR_TYPE_PICKER_OPTIONS: list[tuple[str, str]] = [
    (t, f"{t} (experimental)" if t in _EXPERIMENTAL_CONNECTOR_TYPES else t)
    for t in CONNECTOR_TYPES
]

# Shared by both rocketchat and mattermost — gateway/core/agent_chain.py's
# AgentChainConfig is platform-agnostic and both connectors' *Config
# dataclasses embed it identically (raw.get("agent_chain", {})). Previously
# missing from both field lists entirely (user-reported: "I do not see agent
# chain can be configured in connector template or connector") — a plain
# gap, not a deliberate cut (docs/agent-chain.md documents it as a first-
# class, hand-edit-only feature until now).
_AGENT_CHAIN_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("agent_chain.agent_usernames", "list", "Agent-chain usernames (comma-separated)"),
    FieldSpec("agent_chain.max_turns", "int", "Agent-chain max turns"),
    FieldSpec("agent_chain.ttl_seconds", "float", "Agent-chain TTL (seconds)"),
)

_ROCKETCHAT_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("server.url", "str", "Server URL"),
    FieldSpec("server.username", "str", "Bot username"),
    FieldSpec("server.password", "str", "Bot password", secret=True),
    FieldSpec("allowed_users.owners", "list", "Owners (comma-separated)"),
    FieldSpec("allowed_users.guests", "list", "Guests (comma-separated)"),
    *_AGENT_CHAIN_FIELDS,
    FieldSpec("reply_in_thread", "bool", "Reply in thread"),
    FieldSpec("permission_reply_in_thread", "bool", "Permission replies in thread"),
    FieldSpec("require_mention", "bool", "Require @mention"),
    FieldSpec("filter_sender", "bool", "Filter by allow-list"),
    FieldSpec("timezone", "str", "Timezone (IANA, optional)"),
)
_MATTERMOST_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("server.url", "str", "Server URL"),
    FieldSpec("server.team", "str", "Team"),
    FieldSpec("server.token", "str", "API token", secret=True),
    FieldSpec("server.username", "str", "Bot username"),
    FieldSpec("server.password", "str", "Bot password", secret=True),
    FieldSpec("allowed_users.owners", "list", "Owners (comma-separated)"),
    FieldSpec("allowed_users.guests", "list", "Guests (comma-separated)"),
    *_AGENT_CHAIN_FIELDS,
    FieldSpec("reply_in_thread", "bool", "Reply in thread"),
    FieldSpec("permission_reply_in_thread", "bool", "Permission replies in thread"),
    FieldSpec("require_mention", "bool", "Require @mention"),
    FieldSpec("filter_sender", "bool", "Filter by allow-list"),
    FieldSpec("timezone", "str", "Timezone (IANA, optional)"),
)
_VOICE_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("port", "int", "Port"),
    FieldSpec("host", "str", "Bind host"),
    FieldSpec("secret", "str", "Bearer secret (optional)", secret=True),
    FieldSpec("timeout", "int", "Reply timeout (seconds)"),
)
_SCRIPT_FIELDS: tuple[FieldSpec, ...] = ()  # ScriptConnector never reads raw

FIELDS_BY_TYPE: dict[str, tuple[FieldSpec, ...]] = {
    "rocketchat": _ROCKETCHAT_FIELDS,
    "mattermost": _MATTERMOST_FIELDS,
    "voice": _VOICE_FIELDS,
    "script": _SCRIPT_FIELDS,
}

# Mattermost's dual-mode auth (MattermostConfig.__post_init__: exactly one of
# 'token' or 'username'+'password') used to surface as a plain informational
# Static line, with the real enforcement left entirely to save()'s
# validate_config() — user-reported, with the actual error message quoted:
# "I wonder if we can have a dropdown that select Auth method: token or
# username... If we can do this, we do not need to add this extra message."
# _compose_mm_auth_section()/on_select_changed()/_apply_mm_auth_method_exclusivity()
# below implement that: these 3 fields are pulled out of the uniform
# _compose_form() loop and rendered under an "Auth method" Select instead,
# which shows only the active group and force-blanks the inactive one at
# Save time so the two modes can't collide in the common case. The real
# validate_config() remains the backstop for edge cases this UI layer can't
# fully prevent (e.g. a value coming from an inherits: template rather than
# this entry — see _apply_mm_auth_method_exclusivity()'s docstring).
_MM_AUTH_FIELD_KEYS = frozenset({"server.token", "server.username", "server.password"})

# Each connector type's own dataclass defaults (gateway/connectors/*/config.py)
# — used ONLY to prefill the form with the true effective value when a field
# is set by neither the entry nor its own inherits: template.
DATACLASS_DEFAULTS_BY_TYPE: dict[str, dict[str, object]] = {
    "rocketchat": {
        "server.url": "", "server.username": "", "server.password": "",
        "allowed_users.owners": [], "allowed_users.guests": [],
        "agent_chain.agent_usernames": [], "agent_chain.max_turns": 5,
        "agent_chain.ttl_seconds": 3600.0,
        "reply_in_thread": False, "permission_reply_in_thread": True,
        "require_mention": True, "filter_sender": True, "timezone": "",
    },
    "mattermost": {
        "server.url": "", "server.team": "", "server.token": "",
        "server.username": "", "server.password": "",
        "allowed_users.owners": [], "allowed_users.guests": [],
        "agent_chain.agent_usernames": [], "agent_chain.max_turns": 5,
        "agent_chain.ttl_seconds": 3600.0,
        "reply_in_thread": False, "permission_reply_in_thread": True,
        "require_mention": True, "filter_sender": True, "timezone": "",
    },
    "voice": {"port": 8765, "host": "0.0.0.0", "secret": "", "timeout": 45},
    "script": {},
}

# Which fields have no default and MUST be set for a valid connector of each
# type (gateway/connectors/*/config.py's own dataclass fields with no
# default — RocketChatConfig.server_url/username/password,
# MattermostConfig.server_url/team; voice/script have none). Deliberately
# excludes mattermost's server.token/server.username/server.password: those
# are dual-mode/mutually-exclusive (exactly one of 'token' or
# 'username'+'password' is required, never all three) — marking all three
# '*' would misleadingly suggest every one of them is mandatory. The "Auth
# method" row's own label is marked '*' by hand instead (_compose_mm_auth_section()
# below), since SOME auth method is always required.
REQUIRED_FIELD_KEYS_BY_TYPE: dict[str, frozenset[str]] = {
    "rocketchat": frozenset({"server.url", "server.username", "server.password"}),
    "mattermost": frozenset({"server.url", "server.team"}),
    "voice": frozenset(),
    "script": frozenset(),
}


class ConnectorDetailScreen(FormScreen):
    BODY_ID = "connector-detail-body"

    DEFAULT_CSS = """
    ConnectorDetailScreen .hidden {
        display: none;
    }
    /* Container's own DEFAULT_CSS is `height: 1fr` (fill available space),
    not `auto` — user-reported, with a screenshot: the mattermost auth
    groups' field rows rendered squished/overlapping the row after them,
    since #mm-auth-token-group/#mm-auth-userpass-group were competing for a
    FRACTION of the VerticalScroll's space instead of sizing to their own
    1-2 field rows. Same failure family as `.field-row Input`'s width:1fr
    override above (a Textual default meant for filling a viewport, wrong
    for a wrapper around a fixed few rows of content). */
    ConnectorDetailScreen #mm-auth-token-group,
    ConnectorDetailScreen #mm-auth-userpass-group {
        height: auto;
    }
    """

    def __init__(
        self,
        cfg: EditableConfig,
        entry: dict,
        mode: Literal["view", "edit", "create"] = "view",
    ):
        super().__init__()
        self.cfg = cfg
        self.entry = entry
        self.mode = mode
        # See _open_inherits_picker()/action_save() below. Unlike every
        # other field, switching this one triggers a full
        # _recompute_form() (form_common.py) rather than a snapshot-once-
        # at-open value — see AgentDetailScreen's module docstring for why.
        self._inherits_initial: str | None = self.cfg.entry_template_name(self.entry)
        self._inherits_current: str | None = self._inherits_initial
        # Mattermost-only (see _MM_AUTH_FIELD_KEYS above) — harmless no-op
        # for every other connector type, which never reads this attribute.
        self._mm_auth_method: str = self._compute_mm_auth_method()
        # Whether the user has actually picked a mode THIS session (vs. it
        # just being whatever _compute_mm_auth_method() inferred from the
        # existing entry) — see _apply_mm_auth_method_exclusivity()'s
        # docstring for why this gates the force-clear at Save.
        self._mm_auth_method_touched = False
        if self.mode != "view":
            self._compute_initial_values(self._current_entry())
            self._description_live = self._initial_values.get("description") or ""
            self._populating = True

    def _entity_noun(self) -> str:
        return "connector"

    def _entity_label(self) -> str:
        return self.entry.get("name", "?")

    def _current_entry(self) -> dict:
        """The PROBE entry: `self.entry` with `inherits:` swapped to
        whatever the Inherits picker currently has selected — see
        AgentDetailScreen._current_entry()'s identical docstring for why."""
        probe = dict(self.entry)
        if self._inherits_current is None:
            probe.pop("inherits", None)
        else:
            probe["inherits"] = self._inherits_current
        return probe

    def _find_own_index(self) -> int:
        # Matched by object IDENTITY, not equality — connectors_raw is a
        # fresh list each call but wraps the SAME dict objects living in
        # `document`, and two connectors could (in a broken config) have
        # byte-identical raw content; identity is the only way to be sure
        # this is the exact entry this screen was opened on.
        connectors = self.cfg.document.get("connectors") or []
        return next(i for i, c in enumerate(connectors) if c is self.entry)

    def _remove_entry_from_document(self) -> None:
        self._deleted_index = self._find_own_index()
        del self.cfg.document["connectors"][self._deleted_index]

    def _reinsert_entry_into_document(self) -> None:
        connectors = self.cfg.document.setdefault("connectors", [])
        connectors.insert(self._deleted_index, self.entry)

    def _install_trial_entry(self, target_entry: dict) -> None:
        self._edit_index = self._find_own_index()
        self.cfg.document["connectors"][self._edit_index] = target_entry

    def _rollback_trial_entry(self) -> None:
        self.cfg.document["connectors"][self._edit_index] = self.entry

    def _referencing_watcher_labels(self) -> list[str]:
        return find_referencing_watcher_labels(self.cfg, connector_name=self._entity_label())

    def _on_enter_edit_mode(self) -> None:
        self._inherits_initial = self.cfg.entry_template_name(self.entry)
        self._inherits_current = self._inherits_initial
        self._mm_auth_method = self._compute_mm_auth_method()
        self._mm_auth_method_touched = False
        self._compute_initial_values(self._current_entry())
        self._description_live = self._initial_values.get("description") or ""

    def _connector_type(self) -> str:
        # Reads the MERGED type against the LIVE probe (self._current_entry()),
        # not self.entry directly — a connector whose 'type' comes only from
        # its inherits: template (never set on the entry itself) still needs
        # to select the right per-type field list/dataclass-defaults below,
        # and switching to a DIFFERENT template with a different type must
        # reshape the form to match (part of the same full _recompute_form()
        # this whole picker redesign already does for every other field).
        #
        # PR review finding: same misleading-fallback bug
        # AgentDetailScreen._agent_type() was fixed for — a connector whose
        # ONLY type source was an inherits: template (a normal, supported
        # shape — see _open_inherits_picker()'s own comment above) can end
        # up with NO resolvable type at all if that template is cleared via
        # the picker's "(none)" option. Falling back to a real type name
        # ("rocketchat") here doesn't just mislabel a header — it's also
        # what _field_specs()/_dataclass_defaults() key off of, so the form
        # would silently RESHAPE to the wrong type's fields (losing any
        # already-typed values for fields the wrong type doesn't have, e.g.
        # a mattermost connector's own 'server.token'). "(unset)" matches
        # no real FIELDS_BY_TYPE/DATACLASS_DEFAULTS_BY_TYPE key, so both
        # correctly degrade to empty (show nothing) rather than the wrong
        # thing — Save is blocked either way by the "must have a 'type'
        # field" check, same as it always was.
        try:
            merged = self.cfg.merged_entry("connector", self._current_entry())
        except (ValueError, FileNotFoundError):
            merged = self._current_entry()
        return merged.get("type") or "(unset)"

    def _compute_mm_auth_method(self) -> str:
        """Which of the two mutually-exclusive credential groups the Auth
        method Select should show — derived from the MERGED (not raw) entry,
        same reasoning as `_connector_type()`: a connector whose credentials
        come only from its `inherits:` template still needs the right group
        selected. 'token' wins the ambiguous case (both or neither set) —
        the simpler, no-expiry option `MattermostConfig`'s own docstring
        lists first."""
        try:
            merged = self.cfg.merged_entry("connector", self._current_entry())
        except (ValueError, FileNotFoundError):
            merged = self._current_entry()
        server = merged.get("server") or {}
        if not server.get("token") and server.get("username") and server.get("password"):
            return "username_password"
        return "token"

    def _required_field_keys(self) -> frozenset[str]:
        return REQUIRED_FIELD_KEYS_BY_TYPE.get(self._connector_type(), frozenset())

    def _field_specs(self) -> tuple[FieldSpec, ...]:
        return sort_required_first(
            FIELDS_BY_TYPE.get(self._connector_type(), ()), self._required_field_keys()
        )

    def _template_kind(self) -> str:
        return "connector"

    def _dataclass_defaults(self) -> dict[str, object]:
        return DATACLASS_DEFAULTS_BY_TYPE.get(self._connector_type(), {})

    # ── view mode ────────────────────────────────────────────────────────────

    def _body_text(self) -> str:
        name = self.entry.get("name", "?")
        description = self.entry.get("description")
        template_name = self.cfg.entry_template_name(self.entry)
        try:
            merged = self.cfg.merged_entry("connector", self.entry)
            type_provenance = self.cfg.field_provenance("connector", self.entry, "type")
        except (ValueError, FileNotFoundError):
            merged = self.entry
            type_provenance = None
        conn_type = merged.get("type", "?")

        type_suffix = (
            f"  [dim]({provenance_label(type_provenance, template_name)})[/dim]"
            if type_provenance
            else ""
        )
        lines = [
            f"[bold]{markup_safe(name)}[/bold]  "
            f"(type: {markup_safe(conn_type)}){type_suffix}"
        ]
        if description:
            lines.append(f"[dim]{markup_safe(description)}[/dim]")
        lines.append(
            f"inherits: {markup_safe(template_name) if template_name else '(none)'}"
        )
        lines.append("")

        # 'type' itself is shown in the header above (with its own provenance
        # marker); everything else is a plain dump of this entry's OWN raw
        # fields — inherits: template values that this entry simply inherits
        # (and never overrides) are intentionally not repeated here, since
        # raw is type-flexible and there's no fixed field list to merge
        # against field-by-field the way agent/watcher detail screens do.
        for key, value in self.entry.items():
            if key in ("name", "type", "description", "inherits"):
                continue
            lines.append(self._render_field(key, value, indent=0))
        return "\n".join(lines)

    def _render_field(self, key: str, value: object, indent: int) -> str:
        prefix = "  " * indent
        if isinstance(value, dict):
            sub = "\n".join(
                self._render_field(k, v, indent + 1) for k, v in value.items()
            )
            return f"{prefix}{markup_safe(key)}:\n{sub}"
        return f"{prefix}{markup_safe(key)}: {markup_safe(mask_if_secret(key, value))}"

    # ── edit/create form ─────────────────────────────────────────────────────

    def _compose_form(self) -> ComposeResult:
        conn_type = self._connector_type()
        # can_focus=False: see AgentDetailScreen's identical comment — the
        # container was itself the first stop in the Tab cycle, needing an
        # extra Tab press to reach the first real field.
        with VerticalScroll(classes="entity-form", can_focus=False):
            if self.mode == "create":
                yield Static(f"[bold]New {markup_safe(conn_type)} connector[/bold]")
                with Horizontal(classes="field-row"):
                    yield Static("Name *", classes="field-label")
                    yield Input(
                        id="field-name", value=self._name_live, placeholder="connector name"
                    )
            else:
                name = self.entry.get("name", "?")
                yield Static(
                    f"[bold]{markup_safe(name)}[/bold]  "
                    f"(type: {markup_safe(conn_type)}, editing)"
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

            if not self._field_specs():
                yield Static(
                    f"[dim]'{markup_safe(conn_type)}' connectors have no "
                    "type-specific fields to configure here.[/dim]"
                )

            for spec in self._field_specs():
                if conn_type == "mattermost" and spec.key in _MM_AUTH_FIELD_KEYS:
                    continue  # composed specially by _compose_mm_auth_section() below
                yield from self._compose_field_row(spec, self._current_entry())
                if conn_type == "mattermost" and spec.key == "server.team":
                    yield from self._compose_mm_auth_section()

    def _compose_mm_auth_section(self) -> ComposeResult:
        """Mattermost's dual-mode auth (token XOR username+password) — see
        _MM_AUTH_FIELD_KEYS's module-level comment for the full rationale.
        Both credential groups are always composed (so _collect_field_updates()/
        _any_field_overridden(), which iterate _field_specs() uniformly, keep
        working unmodified) but only the ACTIVE one is visible; the inactive
        one is hidden via the `.hidden` CSS class, not skipped entirely."""
        with Horizontal(classes="field-row"):
            yield Static("Auth method *", classes="field-label")
            yield Select(
                [("API token", "token"), ("Username + password", "username_password")],
                value=self._mm_auth_method,
                allow_blank=False,
                id="mm-auth-method-select",
            )
        specs_by_key = {spec.key: spec for spec in self._field_specs()}
        with Container(
            id="mm-auth-token-group",
            classes="" if self._mm_auth_method == "token" else "hidden",
        ):
            yield from self._compose_field_row(specs_by_key["server.token"], self._current_entry())
        with Container(
            id="mm-auth-userpass-group",
            classes="" if self._mm_auth_method == "username_password" else "hidden",
        ):
            yield from self._compose_field_row(
                specs_by_key["server.username"], self._current_entry()
            )
            yield from self._compose_field_row(
                specs_by_key["server.password"], self._current_entry()
            )

    # ── mattermost auth-method toggle ────────────────────────────────────────

    def on_select_changed(self, event: Select.Changed) -> None:
        if (event.select.id or "") == "mm-auth-method-select":
            # Select fires Changed once at initial mount too (module
            # docstring's "Textual gotcha") — guarded the same way every
            # other field's on_*_changed handler already is, so opening the
            # form doesn't spuriously mark it dirty.
            if self._populating:
                return
            self._mm_auth_method = event.value
            self._mm_auth_method_touched = True
            self._update_mm_auth_visibility()
            self._form_dirty = True
            return
        super().on_select_changed(event)

    def _update_mm_auth_visibility(self) -> None:
        try:
            token_group = self.query_one("#mm-auth-token-group")
            userpass_group = self.query_one("#mm-auth-userpass-group")
        except NoMatches:
            return
        token_group.display = self._mm_auth_method == "token"
        userpass_group.display = self._mm_auth_method == "username_password"

    def _apply_mm_auth_method_exclusivity(self) -> None:
        """Called at the top of action_save() when the user has actually
        picked a mode THIS session (`self._mm_auth_method_touched` — see
        action_save()'s guard): force-blank the now-INACTIVE group's Input
        widgets before _collect_field_updates() reads them, so a value left
        over from BEFORE the switch never gets silently saved alongside the
        newly-active group.

        Gated on `_mm_auth_method_touched` specifically to avoid a real,
        found-via-review data-loss bug: a hidden field's widget can ONLY
        hold a non-blank value the user never typed if it was already there
        when the form opened (Textual's focus chain skips `display: none`
        widgets, so the inactive group literally cannot be edited through
        this UI). If a pre-existing, already-invalid entry has BOTH token
        AND username+password set (e.g. hand-edited, or a half-finished
        migration between modes — `_compute_mm_auth_method()` picks 'token'
        for that ambiguous case), saving an UNRELATED field with this method
        unconditionally applied would silently delete the username/password
        the user never touched or even necessarily knew was still there.
        Only force-clearing after a deliberate Select change confines the
        blast radius to what the user actually asked to happen. Blanking
        writes an explicit "revert to inherited" (see apply_update()'s
        docstring), not an explicit empty override — if the INACTIVE
        group's effective value actually comes from an inherits: template
        rather than this entry, that template value stays in effect and
        save()'s validate_config() (the real MattermostConfig.__post_init__)
        remains the backstop, exactly as it already was before this Select
        existed."""
        try:
            token_input = self.query_one("#" + widget_id("server.token"), Input)
            username_input = self.query_one("#" + widget_id("server.username"), Input)
            password_input = self.query_one("#" + widget_id("server.password"), Input)
        except NoMatches:
            return
        if self._mm_auth_method == "token":
            username_input.value = ""
            password_input.value = ""
        else:
            token_input.value = ""

    # ── inherits: picker ─────────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if (event.button.id or "") == "inherits-change-button":
            self._open_inherits_picker()

    @work
    async def _open_inherits_picker(self) -> None:  # noqa: C901
        if self.mode == "view":
            return
        # User-reported: this used to list EVERY connector template
        # regardless of type, letting a rocketchat connector pick a
        # mattermost-typed template (or vice versa) straight from the
        # picker — gateway/config.py's _resolve_inherits() now rejects that
        # combination outright at save time, but filtering it out of the
        # picker here catches the mistake before the user even fills in the
        # rest of the form.
        #
        # Filtered against `self.entry.get("type")` — the entry's OWN raw
        # type — NOT `self._connector_type()` (the merged/current EFFECTIVE
        # type). This connector may have no own 'type' at all, relying
        # entirely on whichever template it currently inherits from (a
        # perfectly normal way to write one — see
        # test_switching_to_a_different_type_template_reshapes_the_form):
        # such a connector must still be free to switch to ANY template,
        # including one of a different type (that's the whole point of the
        # "switch template to switch type entirely" feature this same
        # picker supports). The mismatch this filter exists to prevent only
        # arises when the entry ITSELF pins an explicit type that a
        # candidate template's own explicit type would then contradict.
        all_templates = self.cfg.templates("connector")
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
            # One-way detour, not a return-with-result flow — same precedent
            # as AgentDetailScreen._open_inherits_picker()'s "new_template"
            # branch. Connector templates always pick a type up front (this
            # screen has no generic tree editor — see module docstring), so
            # prompt for one before the name, same order
            # OverviewScreen.action_new_entity() uses for a brand-new
            # connector.
            new_type = await self.app.push_screen_wait(
                TypePickerModal(
                    "New connector template — pick a type", CONNECTOR_TYPE_PICKER_OPTIONS
                )
            )
            if new_type is None:
                return
            new_name = await self.app.push_screen_wait(
                TextPromptModal("New connector template — name")
            )
            if new_name is None:
                return
            if new_name in self.cfg.templates("connector"):
                await self.app.push_screen_wait(
                    MessageModal(
                        f"A connector template named '{markup_safe(new_name)}' "
                        "already exists.",
                        title="Could not create",
                    )
                )
                return
            from .template_detail import TemplateDetailScreen

            self.app.push_screen(
                TemplateDetailScreen(
                    self.cfg, "connector", new_name, {"type": new_type}, mode="create"
                )
            )
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
        # A different template can change 'type' entirely (rocketchat has no
        # dual-auth concept at all) or bring its own token/username/password
        # values — recompute which auth-method group should show BEFORE
        # _recompute_form() rebuilds the form around the new merged entry.
        # Also resets the "touched" flag: this is a fresh baseline reflecting
        # the NEW template, not a deliberate in-session exclusivity choice —
        # see _apply_mm_auth_method_exclusivity()'s docstring.
        self._mm_auth_method = self._compute_mm_auth_method()
        self._mm_auth_method_touched = False
        await self._recompute_form()
        self._form_dirty = True

    # ── save ─────────────────────────────────────────────────────────────────

    @work
    async def action_save(self) -> None:  # noqa: C901
        if self.mode == "view":
            return

        if self._connector_type() == "mattermost" and self._mm_auth_method_touched:
            self._apply_mm_auth_method_exclusivity()

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

        name = self.entry.get("name")
        if self.mode == "create":
            name = self.query_one("#field-name", Input).value.strip()
            if not name:
                await self.app.push_screen_wait(
                    MessageModal("Name is required.", title="Could not save")
                )
                return
            existing_names = {c.get("name") for c in self.cfg.connectors_raw}
            if name in existing_names:
                await self.app.push_screen_wait(
                    MessageModal(
                        f"A connector named '{markup_safe(name)}' already exists.",
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

        # inherits: lives outside the FieldSpec/apply_update() pipeline (it's
        # driven by InheritsPickerModal, not an Input/Checkbox/Select widget)
        # — diffed here directly against the snapshot taken when the form
        # opened (or last re-entered edit mode).
        if self._inherits_current != self._inherits_initial:
            apply_update(target_entry, "inherits", self._inherits_current)

        inserted_index: int | None = None
        if self.mode == "create":
            target_entry["name"] = name
            connectors = self.cfg.document.setdefault("connectors", [])
            connectors.append(target_entry)
            inserted_index = len(connectors) - 1
        else:
            self._install_trial_entry(target_entry)
        self.cfg.mark_dirty()

        try:
            self.cfg.save()
        except (ValueError, FileNotFoundError) as exc:
            if self.mode == "create" and inserted_index is not None:
                # Nothing existed under this name before this screen ever
                # ran — remove it so a failed save doesn't leave a phantom
                # half-created connector sitting in memory.
                del self.cfg.document["connectors"][inserted_index]
            else:
                self._rollback_trial_entry()
            await self.app.push_screen_wait(MessageModal(markup_safe(exc), title="Could not save"))
            return

        self.entry = target_entry
        self.app.pop_screen()
        app = self.app
        app.notify(f"Saved connector '{name}'.", severity="information")
        app.reload_config()  # type: ignore[attr-defined]
