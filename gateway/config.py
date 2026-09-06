"""Configuration loader.

Shared config dataclasses (``PermissionConfig``, ``ToolRule``, ``AgentConfig``,
``ConnectorConfig``, ``WatcherConfig``) are defined in ``gateway.core.config``
and re-exported here so existing import paths continue to work.

``GatewayConfig.from_file()`` no longer resolves ``$VAR``/``${VAR}`` in
config values (docs/design/config-tool.md decision 6, final revision) —
secrets live directly in config.yaml (``chmod 0600``), and any pre-existing
``.env``-backed config is auto-migrated into that form on the first
``agent-chat-gateway start`` (``gateway/config_migrate.py``) or the config
TUI's launch, both enforced, not optional. An audit before removing this
found ambient (non-``.env``) ``$VAR`` resolution had no real caller anywhere
in this project — no systemd unit, no K8s manifest, no doc recommending it,
no committed example using it; only unit tests exercising the mechanism
itself. ``_expand_env_vars()``/``ENV_VAR_REF_RE`` below are KEPT (not dead
code) — ``gateway/config_migrate.py``'s one-time migration still needs them
to resolve a legacy ``.env``-backed value into a literal at migration time;
they're simply no longer called from the normal load path. Once migrated
(or if a value merely happens to look like ``${SOMETHING}``), it is treated
as a plain string like any other — deliberately, so a password that
happens to resemble a placeholder is never silently misinterpreted.
"""

import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

# Re-export core config types — canonical definitions in gateway.core.config
from .core.config import (  # noqa: F401 — re-exports
    AgentConfig,
    ConnectorConfig,
    HistoryHandoffConfig,
    PermissionConfig,
    ToolRule,
    WatcherConfig,
)
from .core.connector import (
    SUPPORTED_CONNECTOR_TYPES,
    TYPES_WITH_UNSOLICITED_INBOUND,
)
from .core.room_pattern import (
    InvalidRoomPattern,
    RoomPattern,
    union_intersects,
    union_subsumes,
)
from .core.watcher_rule import RoomMatcher, WatcherRule

# v0.2's global `*_defaults:` blocks (removed in v0.3 — see docs/migration-0.3.md) merged
# flatly and unconditionally into EVERY entry of a kind, regardless of type: setting
# `command`/`type` there to give claude agents a custom wrapper silently broke any
# opencode agent that didn't override it (gateway/agents/opencode/adapter.py execs
# `agent_cfg.command` directly as the sidecar binary). Named `*_templates:` + a per-entry
# `inherits:` field (below) replace them — a template only ever applies to entries that
# explicitly opt in, so type-specific fields are finally safe to share. A leftover old key
# is a hard, actionable error rather than a silent behavior change.
_REMOVED_DEFAULTS_KEYS: dict[str, str] = {
    "agent_defaults": "agent_templates",
    "connector_defaults": "connector_templates",
    "watcher_defaults": "watcher_templates",
}

# Single source of truth for the identity keys a named `*_templates:` block may
# not set — the keys that identify one specific entry, and so cannot be shared
# by everything inheriting the template.  Passed to _parse_templates_block().
#
# This existed as four byte-identical `frozenset({"name", "room", "rooms"})`
# literals (two here, one in config_validate, one inline in the
# config tool) plus a fifth copy in a dict there, with a comment claiming unit
# tests kept them in sync.  No such test existed, and nothing imported anything
# — they were four hand-maintained copies of one rule.  The connector and agent
# sets were duplicated the same way.
#
# Kind strings are plain ("agent"/"connector"/"watcher"), not the
# `<kind>_templates` block names, and not the retired `*_defaults` names above.
#
# Every key here is one that names a specific entry. Removed fields are NOT
# listed: `session_id` used to be, so that a template carrying it was named in
# the error rather than each entry inheriting it, but a removed field does not
# earn its own rejection path — it is simply not a key, and the unknown-key
# check says so. One behaviour for every key nothing accepts, rather than one
# per field we used to have.
# Every key config.yaml may set at its top level. An unknown one is a hard error
# rather than something quietly skipped, which is what makes the `watchers:` →
# `watcher_rules:` rename safe: an old config does not need a special case here,
# it simply names a key that no longer exists and gets told so along with the
# list of keys that do.
#
# Before this, ANY unrecognised top-level key was ignored in silence. A typo in
# `connectors:` or `agents:` was caught only incidentally, by the separate "must
# define at least one" rules — and with a misleading message, since the entries
# were sitting right there under the misspelled key. `watchers:` had no such
# rule, correctly (a config with no rules is legal), so a mistyped key there
# meant a daemon that started and watched nothing.
TOP_LEVEL_KEYS: frozenset[str] = frozenset({
    "connectors",
    "connector_templates",
    "agents",
    "agent_templates",
    "tool_presets",
    "watcher_rules",
    "watcher_templates",
    "max_queue_depth",
    "scheduler",
})


def unknown_top_level_keys(raw: Mapping) -> list[str]:
    """The top-level keys config.yaml sets that mean nothing, sorted, as strings.

    YAML admits non-string keys (`1: value`, `true: x`); rendered with `str`
    so the message and its `difflib` hint never see a mixed-type list — an
    integer key raised TypeError out of both loaders instead of the normal
    unknown-key error (Codex, PR #140).
    """
    return sorted(str(k) for k in raw if k not in TOP_LEVEL_KEYS)


def unknown_top_level_message(unknown: list[str]) -> str:
    """One sentence for both loaders, so they cannot drift.

    Names a likely intended key when one is close — `watchers` against
    `watcher_rules` is the case this was written for, and the same near-miss
    help the connector-type check gives.
    """
    import difflib

    valid = ", ".join(f"'{k}'" for k in sorted(TOP_LEVEL_KEYS))
    hints = []
    for key in unknown:
        close = difflib.get_close_matches(key, TOP_LEVEL_KEYS, n=1, cutoff=0.6)
        if close:
            hints.append(f"'{key}' — did you mean '{close[0]}'?")
    suffix = (" " + " ".join(hints)) if hints else ""
    keys = ", ".join(f"'{k}'" for k in unknown)
    return (
        f"config.yaml sets {keys}, which this gateway does not use. "
        f"Valid top-level keys are: {valid}.{suffix}"
    )


TEMPLATE_FORBIDDEN_KEYS: dict[str, frozenset[str]] = {
    "connector": frozenset({"name"}),
    "agent": frozenset(),
    # `rooms` used to be here. It was not an identity key like `name` — it was
    # forbidden because the loader decided whether an entry was a rule by looking
    # for a `rooms:` mapping on the RAW entry, before templates were merged, so an
    # entry taking its rooms from a template did not read as a rule at all. The
    # `watcher_rules:` rename removed that test, and the restriction with it: a
    # template can now supply `rooms`, and `_deep_merge` means an entry's own
    # `rooms` merges key by key over it (`{direct: true}` in the template plus
    # `{include: [...]}` in the entry gives both).
    # Only `name` — the one key that genuinely identifies an entry. `room` was
    # here too, and is now simply not a key: the unknown-key check reports it and
    # lists `rooms` among the valid ones, which is better than the dedicated
    # message it replaced ("'room' cannot be combined with a 'rooms:' block",
    # said even to entries that had no `rooms:` block).
    "watcher": frozenset({"name"}),
}

# Single source of truth for history_handoff's per-field defaults: read from
# HistoryHandoffConfig's OWN dataclass field defaults below, not re-typed as
# separate literals here. These two drifted apart once, for over two months
# (commit 31f966d flipped only the dataclass default to enabled=True — opt-out,
# not opt-in — and missed this loader, which stayed hardcoded at enabled=False).
_HH_DEFAULTS = HistoryHandoffConfig()

# Distinguishes "key absent" from "key present with a falsy value". Needed
# wherever an explicit `null` means something different from omission, and
# wherever a falsy non-string must be rejected rather than read as "not set".
_MISSING_FIELD = object()

# Per-type fallback for `command` when an agent (or its template) sets `type`
# but not `command`. Deliberately NOT a single hardcoded string (e.g. always
# "claude") — that was the other half of the bug _REMOVED_DEFAULTS_KEYS above
# describes: a fixed fallback is wrong for whichever type it doesn't match
# (an opencode agent silently defaulting to command "claude" would still exec
# the wrong binary). `type` itself has no fallback and is required below,
# same as `working_directory` — so this map only ever needs to cover known
# types; an unrecognized `type` value surfaces at runtime instead
# (gateway/service.py's "Unknown agent type" check), unchanged from before.
# Keys that used to live on an agent and now live on a watcher rule. Reported as a
# hard error naming the new home — see the check in _parse_one_agent().
_MOVED_TO_RULE_KEYS: frozenset[str] = frozenset({
    "session_idle_days",
    "session_expire_days",
})

_AGENT_TYPE_DEFAULT_COMMAND: dict[str, str] = {
    "claude": "claude",
    "opencode": "opencode",
}


@dataclass
class AttachmentConfig:
    max_file_size_mb: float = 10.0  # files larger than this are skipped (0 = no limit)
    download_timeout: int = 30  # seconds per file download
    cache_dir: str = "agent-chat.cache"  # relative to watcher's working_directory (legacy; unused when cache_dir_global is set)
    cache_dir_global: str = "~/.agent-chat-gateway/attachments"  # connector-global base dir for attachment downloads


@dataclass
class SchedulerConfig:
    """Configuration for the built-in job scheduler.

    completed_job_ttl_days:
        How long to retain COMPLETED jobs in jobs.json before purging them.
        0 = remove immediately when completed.
        Default: 7 days.
    """
    completed_job_ttl_days: int = 7     # days to keep completed jobs (0 = delete immediately)


@dataclass
class GatewayConfig:
    connectors: list[ConnectorConfig]
    agents: dict[str, AgentConfig]
    # The parsed `watcher_rules:` block. A rule names no room: it describes which
    # rooms an agent may be drawn into, and the watcher manager materializes one
    # watcher per room as rooms turn up.
    #
    # There used to be a `watchers: list[WatcherConfig]` field beside this one,
    # holding the static shape during the cutover. It has been empty since that
    # parser was deleted — `config_validate` said so in a comment while still
    # reading it — and an always-empty field named `watchers` next to
    # `watcher_rules` is the same confusion the config key was renamed to remove.
    # `WatcherConfig` itself stays: it is what a rule materializes INTO.
    watcher_rules: list[WatcherRule] = field(default_factory=list)
    max_queue_depth: int = 100  # max pending messages per room queue; 0 = unbounded
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)

    def rules_for(self, connector: str) -> list[WatcherRule]:
        """This connector's rules, in file order — the list its session manager runs."""
        return [r for r in self.watcher_rules if r.connector == connector]

    @staticmethod
    def from_file(path: str | Path) -> "GatewayConfig":
        """The real, production config loader — deliberately fail-fast: stops
        at the FIRST problem found (single, clear, actionable error), same
        as always. Per-entity parsing (one connector/agent/watcher rule at a
        time) is delegated to `_parse_one_connector()`/`_parse_one_agent()`/
        `_parse_one_watcher_rule()` (module-level functions below) so this
        method and `collect_config()`'s fault-tolerant counterpart (used by
        `gateway/config_validate.py` for the config TUI's Status column and
        the pre-save "did this edit make anything new go wrong" check) share
        exactly one implementation of every validation rule — never two
        copies that could quietly drift apart."""
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

        with open(path) as f:
            raw = yaml.safe_load(f) or {}

        if not isinstance(raw, dict):
            raise ValueError(
                f"Config file '{path}' must contain a YAML mapping at the top level, "
                f"got {type(raw).__name__}."
            )

        for old_key, new_key in _REMOVED_DEFAULTS_KEYS.items():
            if old_key in raw:
                raise ValueError(
                    f"config.yaml '{old_key}:' is no longer supported (removed) — "
                    f"define shared fields under '{new_key}:' instead and add "
                    "'inherits: <template-name>' to each entry that should use "
                    "them. See docs/migration-0.3.md."
                )

        # AFTER the renamed-block checks above, deliberately. Those name the exact
        # replacement (`agent_defaults:` → `agent_templates:`), and the generic
        # message cannot: its near-miss guess for `agent_defaults` is `agents`,
        # which is the wrong block. A key with a known replacement gets the
        # specific message; everything else falls through to here.
        #
        # `watchers:` deliberately has NO entry above. It is reported as what it
        # is — a key this gateway does not use — and the near-miss lands on
        # `watcher_rules` on its own, so the rename needs no special case.
        unknown = unknown_top_level_keys(raw)
        if unknown:
            raise ValueError(unknown_top_level_message(unknown))

        # No $VAR/${VAR} expansion here — see module docstring. Any such
        # string in a loaded config is treated as a plain literal.

        config_dir = Path(path).parent

        # ── Connectors ────────────────────────────────────────────────────────

        connectors_raw = raw.get("connectors", [])
        if not connectors_raw:
            raise ValueError(
                "config.yaml must define at least one connector under 'connectors:'"
            )
        if not isinstance(connectors_raw, list):
            raise ValueError(
                f"config.yaml 'connectors:' must be a list (got {type(connectors_raw).__name__})."
            )

        connector_templates = _parse_templates_block(
            raw, "connector_templates", TEMPLATE_FORBIDDEN_KEYS["connector"]
        )

        connectors: list[ConnectorConfig] = []
        seen_connector_names: set[str] = set()
        for i, cc_raw in enumerate(connectors_raw):
            connectors.append(
                _parse_one_connector(cc_raw, i, connector_templates, config_dir, seen_connector_names)
            )

        # ── Agents ────────────────────────────────────────────────────────────

        agents_raw = raw.get("agents") or {}
        if not isinstance(agents_raw, dict):
            raise ValueError(
                f"config.yaml 'agents:' must be a mapping (got {type(agents_raw).__name__}). "
                f"Expected a dict of agent names to config blocks."
            )
        agent_templates = _parse_templates_block(raw, "agent_templates", TEMPLATE_FORBIDDEN_KEYS["agent"])
        tool_presets = _parse_tool_presets(raw)

        agents: dict[str, AgentConfig] = {}
        for agent_name, agent_raw_entry in agents_raw.items():
            agents[agent_name] = _parse_one_agent(
                agent_name, agent_raw_entry, agent_templates, tool_presets, config_dir
            )

        if not agents:
            raise ValueError(
                "config.yaml must define at least one agent under 'agents:'"
            )

        # ── Watchers ──────────────────────────────────────────────────────────

        connector_names = {c.name for c in connectors}
        watcher_rules: list[WatcherRule] = []
        watchers_raw = raw.get("watcher_rules", [])
        # None BEFORE the type check, and the type check without a truthiness
        # gate — this file's own rule, stated on _resolve_watcher_connector:
        # "the type check must come BEFORE any truthiness test". The gate let
        # every FALSY non-list through: a bare `watchers:` (explicit null,
        # the natural way to empty the block) then reached `enumerate(None)`
        # and raised a raw TypeError, so the daemon failed to start and
        # `agent-chat-gateway config validate` crashed instead of reporting — on a config an
        # operator writes by deleting their rules. `0`/`""` took the same
        # path and now get the clean message.
        if watchers_raw is None:
            watchers_raw = []
        if not isinstance(watchers_raw, list):
            raise ValueError(
                "config.yaml 'watcher_rules:' must be a list (got "
                f"{type(watchers_raw).__name__})."
            )

        watcher_templates = _parse_templates_block(
            raw, "watcher_templates", TEMPLATE_FORBIDDEN_KEYS["watcher"]
        )

        # Every entry under `watcher_rules:` is a rule, by virtue of the block it
        # is in. There used to be a shape test here — a `rooms:` MAPPING meant a
        # rule, anything else meant the removed static shape — because one
        # `watchers:` block had to carry both during the cutover. Keying the
        # decision on `rooms:` had a cost that outlived its reason: `rooms`
        # could not be inherited from a template, since a template is merged
        # AFTER the shape is decided, so an entry taking its rooms from a
        # template did not look like a rule at all. The rename removed the need
        # for the test and the restriction with it.
        seen_rule_names: set[str] = set()
        for i, wc_raw in enumerate(watchers_raw):
            if not isinstance(wc_raw, Mapping):
                raise ValueError(
                    f"watcher_rules[{i}] must be a mapping (got "
                    f"{type(wc_raw).__name__})."
                )
            watcher_rules.append(
                _parse_one_watcher_rule(
                    wc_raw, i,
                    connectors=connectors,
                    connector_names=connector_names,
                    agents=agents,
                    config_dir=config_dir,
                    templates=watcher_templates,
                    seen_rule_names=seen_rule_names,
                )
            )

        # The cross-watcher duplicate-session_id pass that used to sit here is gone
        # with the field: `session_id:` is not a key at all now, so no watcher can
        # carry one and a duplicate cannot exist. The hazard it guarded — two
        # watchers sharing one id, silently overwriting the session→room and
        # session→connector routing maps so permission notifications land in the
        # wrong room — now has no way to arise from config. The runtime-assigned
        # `WatcherState.session_id` is unaffected and stays unique by construction.

        max_queue_depth = _parse_max_queue_depth(raw)

        # ── Scheduler ─────────────────────────────────────────────────────────

        scheduler_cfg = _parse_scheduler(raw)

        return GatewayConfig(
            connectors=connectors,
            agents=agents,
            watcher_rules=watcher_rules,
            max_queue_depth=max_queue_depth,
            scheduler=scheduler_cfg,
        )


def _deep_copy(value):
    """Recursively copy dicts/lists so merged config entries never alias."""
    if isinstance(value, Mapping):
        return {k: _deep_copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_deep_copy(v) for v in value]
    return value


def _deep_merge(base: Mapping, override: Mapping) -> dict:
    """Deep-merge two mappings; ``override`` wins at every level.

    - Both values are dicts -> recursively merged.
    - Otherwise (list, scalar, or ``None``) -> the override value replaces
      the base value verbatim. An explicit ``null`` in ``override``
      intentionally suppresses a base value, rather than being treated as
      "unset".
    - Always returns a brand-new nested structure so the result never shares
      a mutable dict/list with ``base`` or ``override``. This matters
      because per-entry parsing later mutates dicts in place (e.g. resolving
      ``attachments.cache_dir_global`` to an absolute path) — without a deep
      copy, that mutation would leak into a shared template block
      (referenced by another entry's ``inherits:``) and corrupt it.
    """
    merged = {k: _deep_copy(v) for k, v in base.items()}
    for k, v in override.items():
        if k in merged and isinstance(merged[k], dict) and isinstance(v, Mapping):
            merged[k] = _deep_merge(merged[k], v)
        else:
            merged[k] = _deep_copy(v)
    return merged


def _parse_templates_block(
    raw: dict, key: str, forbidden_keys: frozenset[str]
) -> dict[str, dict]:
    """Parse and validate a top-level ``<x>_templates:`` mapping: named,
    reusable field blocks an entry opts into via its own ``inherits:``
    field (resolved by ``_resolve_inherits`` below), replacing v0.2's single
    global ``*_defaults:`` block per kind (see ``_REMOVED_DEFAULTS_KEYS``
    above for why). Modeled directly on ``_extract_defaults_block``'s own
    validation conventions, applied per named template instead of once.

    Returns ``{}`` if the key is absent. Raises ValueError if the block, or
    any one named template within it, is not a mapping; if a named template
    sets a forbidden key (an identity field belonging to one specific
    entry, e.g. ``name`` — never safe to share, mirrors
    ``_extract_defaults_block``'s own message); or if a named template sets
    ``inherits`` itself — templates cannot nest (mirrors
    ``_parse_tool_presets``'s "no preset-of-presets" rule below).

    Deliberately does NOT check for any field being "required" — a template
    is meant to be a partial field set (e.g. a template need not set
    ``working_directory``, since that's inherently per-agent); "is
    everything required actually present" is checked only once, on the
    fully-resolved (template ∪ entry) dict downstream, exactly where it
    already ran before this mechanism existed.

    An optional ``description`` on each named template (annotating it,
    shown by the config TUI) is stripped before being returned — same rule
    ``_extract_defaults_block`` applies, for the same reason: it must never
    deep-merge into an entry that inherits this template.
    """
    templates_raw = raw.get(key, {}) or {}
    if not isinstance(templates_raw, Mapping):
        raise ValueError(
            f"config.yaml '{key}:' must be a mapping (got {type(templates_raw).__name__})."
        )
    templates: dict[str, dict] = {}
    for name, block in templates_raw.items():
        if not isinstance(block, Mapping):
            raise ValueError(
                f"{key}['{name}'] must be a mapping (got {type(block).__name__})."
            )
        if "inherits" in block:
            raise ValueError(
                f"{key}['{name}'] must not set 'inherits' — templates cannot "
                "inherit from another template (no nested templates)."
            )
        bad = sorted(forbidden_keys & block.keys())
        if bad:
            # One sentence now that removed fields are not listed here: every key
            # this can report names a specific entry, so the advice is always the
            # same. The two-branch version existed only to avoid telling someone
            # to "set it per-entry" for a field nothing accepts.
            names, supply = (
                ("those name", "them") if len(bad) > 1 else ("that names", "it")
            )
            raise ValueError(
                f"{key}['{name}'] must not set {_key_list(bad)} — {names} one "
                f"specific entry, so a shared template cannot supply {supply}. "
                f"Set {supply} on each entry that needs {supply} instead."
            )
        result = dict(block)
        result.pop("description", None)
        templates[name] = result
    return templates


def _resolve_inherits(
    entry_raw: Mapping,
    templates: dict[str, dict],
    templates_key: str,
    entity_kind: str,
    entity_label: str,
) -> dict:
    """Resolve one entry's ``inherits:`` field (if set) against the parsed
    ``templates`` dict (from ``_parse_templates_block`` above): deep-merge
    the named template's fields with the entry's own fields, the entry
    winning on conflict (via the same ``_deep_merge`` the old ``*_defaults``
    blocks used — the merge algorithm itself needed no changes for this new
    purpose). No ``inherits:`` set -> the entry's own fields, unchanged.
    ``inherits`` is always popped from the result — it is never itself a
    real field downstream, whether resolved or absent.
    """
    entry = dict(entry_raw)
    template_name = entry.pop("inherits", None)
    if template_name is None:
        return entry
    if not isinstance(template_name, str) or not template_name:
        raise ValueError(
            f"{entity_kind} '{entity_label}': 'inherits' must be a non-empty "
            f"string naming a {templates_key} entry (got {template_name!r})."
        )
    template = templates.get(template_name)
    if template is None:
        available = ", ".join(sorted(templates)) or "(none defined)"
        raise ValueError(
            f"{entity_kind} '{entity_label}': unknown {templates_key} "
            f"'{template_name}'. Available templates: {available}"
        )
    # User-reported: nothing stopped an entry from declaring its own 'type'
    # (e.g. a rocketchat connector, or a claude agent) while `inherits:`
    # pointed at a template written for a DIFFERENT type (e.g. a mattermost
    # connector template, or an opencode agent template) — the entry's own
    # type silently won the merge, but the rest of that template's fields
    # were written for the wrong protocol/backend entirely. Only an actual
    # CONTRADICTION is an error: a template with no 'type' opinion of its
    # own (a genuinely generic, shared field set) is still a legitimate
    # thing to inherit regardless of the entry's type, and an entry with no
    # 'type' of its own is meant to inherit the template's type outright
    # (the config TUI's "switch template to switch type" feature relies on
    # exactly that case).
    entry_type = entry.get("type")
    template_type = template.get("type")
    if entry_type and template_type and entry_type != template_type:
        raise ValueError(
            f"{entity_kind} '{entity_label}': type '{entry_type}' does not "
            f"match {templates_key}['{template_name}']'s own type "
            f"'{template_type}' — an entry cannot inherit a template "
            "written for a different type."
        )
    return _deep_merge(template, entry)


def _parse_tool_presets(raw: dict) -> dict[str, list["ToolRule"]]:
    """Parse and validate the top-level ``tool_presets:`` block.

    Each preset is a named list of inline tool-rule dicts (same shape as
    ``owner_allowed_tools``/``guest_allowed_tools`` entries). Presets are
    flat: a preset's rule list may not reference another preset by name.
    All presets are parsed and regex-validated eagerly here, even if unused
    by any agent, so a broken preset fails fast at config load.
    """
    presets_raw = raw.get("tool_presets", {}) or {}
    if not isinstance(presets_raw, Mapping):
        raise ValueError(
            f"config.yaml 'tool_presets:' must be a mapping "
            f"(got {type(presets_raw).__name__})."
        )
    presets: dict[str, list[ToolRule]] = {}
    for preset_name, rules_raw in presets_raw.items():
        if not isinstance(rules_raw, list):
            raise ValueError(
                f"tool_presets['{preset_name}'] must be a list of tool rules "
                f"(got {type(rules_raw).__name__})."
            )
        rules: list[ToolRule] = []
        for i, entry in enumerate(rules_raw):
            if isinstance(entry, str):
                raise ValueError(
                    f"tool_presets['{preset_name}'][{i}]: presets cannot reference "
                    f"another preset ('{entry}') — a preset must be a flat list of "
                    "inline tool rules."
                )
            try:
                rules.append(ToolRule.from_config(entry))
            except ValueError as e:
                raise ValueError(
                    f"tool_presets['{preset_name}']: invalid tool rule at index {i}: {e}"
                ) from e
        presets[preset_name] = rules
    return presets


def _resolve_tool_entries(
    raw_list: list,
    presets: dict[str, list["ToolRule"]],
    agent_name: str,
    field_name: str,
) -> list["ToolRule"]:
    """Resolve one agent's owner/guest_allowed_tools list into ToolRule objects.

    Each entry is either a string (the name of a ``tool_presets:`` entry,
    expanded in place) or a dict (an inline ``{tool, params}`` rule). Both
    forms may be freely mixed; list order is preserved.
    """
    rules: list[ToolRule] = []
    for i, entry in enumerate(raw_list):
        if isinstance(entry, str):
            preset = presets.get(entry)
            if preset is None:
                available = ", ".join(sorted(presets)) or "(none defined)"
                raise ValueError(
                    f"Agent '{agent_name}': unknown tool preset '{entry}' in "
                    f"{field_name}[{i}]. Available presets: {available}"
                )
            rules.extend(preset)
            continue
        try:
            rules.append(ToolRule.from_config(entry))
        except ValueError as e:
            raise ValueError(
                f"Agent '{agent_name}': invalid tool rule at index {i} in "
                f"{field_name}: {e}"
            ) from e
    return rules


def _resolve_paths(paths: object, base_dir: Path, label: str = "context_inject_files") -> list[str]:
    """Resolve a list of path strings relative to base_dir.

    Validates the container before iterating it, because iterating the wrong type
    fails in two quiet ways. A bare string is iterated **per character**, so
    `context_inject_files: notes.md` becomes eight paths ending `/n`, `/o`, `/t`
    … rather than one error — a config that looks accepted and injects nothing.
    A non-string element reaches `Path()` and raises `TypeError`, which
    `collect_config()` does not catch (it catches `ValueError`), so a single bad
    entry aborts the whole validation pass instead of being reported as one issue
    with the other entries still checked.

    The check lives here rather than at each call site because this is the
    function that iterates, and all three context layers -- connector, agent and
    watcher -- were affected identically.
    """
    if isinstance(paths, str) or not isinstance(paths, Sequence):
        raise ValueError(
            f"{label} must be a list of paths (got {type(paths).__name__}); "
            "a bare string would be read one character at a time."
        )
    resolved = []
    for p in paths:
        if not isinstance(p, str):
            raise ValueError(
                f"{label} entries must be strings (got {type(p).__name__})."
            )
        if p and not Path(p).is_absolute():
            resolved.append(str((base_dir / p).resolve()))
        elif p:
            resolved.append(p)
    return resolved


_config_logger = logging.getLogger("agent-chat-gateway.config")

# $VAR / ${VAR} reference pattern — the one place this is defined.
# gateway/config_migrate.py's migration imports this directly (code-review
# finding: it used to keep its own independent copy of this exact regex,
# which had already drifted out of sync once).
ENV_VAR_REF_RE = re.compile(r"\$\{?\w+")


def _expand_env_vars(obj, _path: str = ""):
    """Recursively expand $ENV_VAR and ${ENV_VAR} in string values.

    NOT called by `GatewayConfig.from_file()` (see module docstring) — the
    real gateway loader treats `$VAR`/`${VAR}` as a plain literal string,
    same as everything else. This function's only remaining caller is
    `gateway/config_migrate.py`'s one-time migration, which uses it to
    resolve a legacy `.env`-backed value into its literal form.

    Raises ValueError when an unresolved placeholder (e.g. ``${MISSING_VAR}``)
    is detected, so a migration fails loudly rather than silently writing
    the literal placeholder string into config.yaml as if it were the real
    secret value.
    """
    if isinstance(obj, str):
        expanded = os.path.expandvars(obj)
        # Check for unresolved placeholders on the *original* string, not the
        # expanded result.  Scanning the expanded value causes false positives
        # when a resolved secret itself contains a $WORD pattern (e.g. a
        # password like "myPass$HM").  A placeholder is truly unresolved only
        # when it still appears verbatim in the expanded output.
        unresolved = [
            m.group()
            for m in ENV_VAR_REF_RE.finditer(obj)
            if m.group() in expanded
        ]
        if unresolved:
            raise ValueError(
                f"Unresolved environment variable in config key '{_path}': {expanded!r}. "
                f"Set the environment variable or remove the placeholder from config.yaml."
            )
        return expanded
    elif isinstance(obj, dict):
        return {
            k: _expand_env_vars(v, f"{_path}.{k}" if _path else k)
            for k, v in obj.items()
        }
    elif isinstance(obj, list):
        return [_expand_env_vars(item, f"{_path}[{i}]") for i, item in enumerate(obj)]
    return obj


# ── Per-entity parsing, extracted out of GatewayConfig.from_file() ──────────
#
# Each function below is EXACTLY the per-connector/per-agent/per-watcher body
# that used to be inlined directly in from_file()'s own for-loops — same
# checks, same exact error messages, same order — just given a name so it
# can be called from two places instead of one: from_file() itself (still
# fail-fast: the first ValueError raised here propagates straight out,
# stopping the whole load, exactly as before) and collect_config() below
# (fault-tolerant: wraps each call in its own try/except and keeps going).
# This is the ONLY place each of these rules is implemented — from_file()'s
# production fail-fast behavior and collect_config()'s multi-error
# collection can never quietly drift apart, because they share the code.


def _parse_one_connector(
    cc_raw: object,
    index: int,
    connector_templates: dict[str, dict],
    config_dir: Path,
    seen_connector_names: set[str],
) -> ConnectorConfig:
    if not isinstance(cc_raw, Mapping):
        raise ValueError(
            f"Connector entry at index {index} must be a mapping "
            f"(got {type(cc_raw).__name__})."
        )
    cc = _resolve_inherits(
        cc_raw, connector_templates, "connector_templates",
        "Connector entry", f"index {index}",
    )
    name = cc.get("name", "")
    connector_type = cc.get("type", "")
    if not name:
        raise ValueError("Each connector entry must have a 'name' field")
    if not isinstance(name, str):
        # PR review finding: a truthy-but-non-string 'name' (e.g. a YAML
        # list) is technically not caught by `not name` above, and used to
        # reach `name in seen_connector_names` below unchecked — an
        # uncaught `TypeError: unhashable type` (for a list/dict) instead
        # of a clean ValueError every caller expects. Pre-existing in
        # from_file() too (this function is extracted verbatim from it),
        # not something this session's collect_config() work introduced —
        # just never exercised until collect_config()'s own per-entity
        # attribution (gateway/config_validate.py's ConfigIssue.entity_name)
        # started reading a NAME off of every connector, whether or not it
        # goes on to parse successfully.
        raise ValueError(
            f"Connector entry at index {index}: 'name' must be a string "
            f"(got {type(name).__name__})."
        )
    if not connector_type:
        raise ValueError(f"Connector '{name}' must have a 'type' field")
    if not isinstance(connector_type, str):
        # PR review finding: same class of bug as the 'name' check above,
        # on the field one line down — a truthy-but-non-string 'type'
        # (e.g. a YAML list) reached ConnectorConfig.type unchecked, later
        # crashing gateway/config_validate.py's
        # `_CONNECTOR_VALIDATORS.get(connector.type)` with
        # `TypeError: unhashable type` on every validate_config() call, not
        # just --lint. _parse_one_agent()'s equivalent 'type' check already
        # does this; this one was simply missed in the same sweep.
        raise ValueError(
            f"Connector '{name}': 'type' must be a string (got {type(connector_type).__name__})."
        )
    if ":" in name:
        # ':' is the watcher-handle divider (`<connector>:<room label>`,
        # §2.3) — the boundary is only unforgeable because a connector name
        # can never carry one. Everything else in a name passes through:
        # room labels are percent-encoded on THEIR side of the divider.
        raise ValueError(
            f"Connector name '{name}' contains ':' — that character is "
            f"reserved as the watcher-name divider (<connector>:<room>). "
            f"Rename the connector."
        )
    if name in seen_connector_names:
        raise ValueError(
            f"Duplicate connector name '{name}' found. "
            "Each connector must use a unique name."
        )
    seen_connector_names.add(name)

    # Resolve connector-level context_inject_files
    raw_ctx = cc.get("context_inject_files", [])
    ctx_files = _resolve_paths(raw_ctx, config_dir, f"Connector '{name}': 'context_inject_files'")

    # Resolve attachments.cache_dir_global relative to config dir
    # (consistent with working_directory resolution below)
    attach_raw = cc.get("attachments", {})
    if isinstance(attach_raw, dict):
        cache_dir_global = attach_raw.get("cache_dir_global", "")
        if (
            cache_dir_global
            and not cache_dir_global.startswith("~")
            and not Path(cache_dir_global).is_absolute()
        ):
            attach_raw["cache_dir_global"] = str(
                (config_dir / cache_dir_global).resolve()
            )
        # Write the resolved value back into the raw config
        cc["attachments"] = attach_raw

    # Store everything except name/type/context_inject_files/description
    # as the raw connector config. 'description' is an optional,
    # informational-only annotation (shown by the config TUI) — it
    # must never leak into connector `raw` (which is passed verbatim
    # to each connector type's from_connector_config()).
    connector_raw = {
        k: v
        for k, v in cc.items()
        if k not in ("name", "type", "context_inject_files", "description")
    }
    return ConnectorConfig(
        name=name,
        type=connector_type,
        raw=connector_raw,
        context_inject_files=ctx_files,
    )


def _parse_one_agent(
    agent_name: str,
    agent_raw_entry: object,
    agent_templates: dict[str, dict],
    tool_presets: dict[str, list["ToolRule"]],
    config_dir: Path,
) -> AgentConfig:
    if not isinstance(agent_raw_entry, Mapping):
        raise ValueError(
            f"Agent '{agent_name}' config must be a mapping "
            f"(got {type(agent_raw_entry).__name__})."
        )
    agent_raw = _resolve_inherits(
        agent_raw_entry, agent_templates, "agent_templates", "Agent", agent_name,
    )
    perm_raw = agent_raw.get("permissions", {})
    if perm_raw and not isinstance(perm_raw, Mapping):
        raise ValueError(
            f"Agent '{agent_name}': permissions must be a mapping "
            f"(got {type(perm_raw).__name__})."
        )

    # Resolve context_inject_files (list) relative to the config file's directory
    raw_ctx = agent_raw.get("context_inject_files", [])
    ctx_files = _resolve_paths(raw_ctx, config_dir, f"Agent '{agent_name}': 'context_inject_files'")

    # Resolve working_directory: expand a leading ~ first (matching
    # the cache_dir_global handling above), then resolve relative to
    # the config file's directory if still not absolute.
    working_directory = agent_raw.get("working_directory", "")
    if working_directory:
        working_directory = str(Path(working_directory).expanduser())
        if not Path(working_directory).is_absolute():
            working_directory = str((config_dir / working_directory).resolve())

    # Validate: working_directory is required and must exist
    if not working_directory:
        raise ValueError(
            f"Agent '{agent_name}': working_directory is required. "
            f"Set it to the directory where the agent should run."
        )
    if not Path(working_directory).is_dir():
        raise ValueError(
            f"Agent '{agent_name}': working_directory does not exist "
            f"or is not a directory: '{working_directory}'"
        )

    lazy_instruction_loading = agent_raw.get("lazy_instruction_loading", True)
    if not isinstance(lazy_instruction_loading, bool):
        raise ValueError(
            f"Agent '{agent_name}': lazy_instruction_loading must be a boolean"
        )

    # Validate: type is required (directly or via an inherits: template) —
    # no fallback. A silent default here is exactly the failure mode
    # _AGENT_TYPE_DEFAULT_COMMAND's docstring describes: an agent that
    # forgot `inherits:` (or a template) would otherwise look "valid"
    # while silently running as the wrong type.
    agent_type = agent_raw.get("type", "")
    if not agent_type:
        raise ValueError(
            f"Agent '{agent_name}': type is required (directly or via "
            "an inherits: agent_templates entry). Set it to 'claude' "
            "or 'opencode'."
        )
    if not isinstance(agent_type, str):
        raise ValueError(
            f"Agent '{agent_name}': type must be a string "
            f"(got {type(agent_type).__name__})."
        )
    command = agent_raw.get("command") or _AGENT_TYPE_DEFAULT_COMMAND.get(
        agent_type, ""
    )

    # session_idle_days / session_expire_days moved from the agent to the watcher
    # rule (design §5.4). A leftover key here is a hard, actionable error rather
    # than a silent behaviour change — the same treatment `_REMOVED_DEFAULTS_KEYS`
    # gives a retired top-level block, and for the same reason: the value would
    # otherwise be read, ignored, and the lifecycle the operator asked for would
    # never happen. Checked here rather than at the top level so collect_config()
    # attributes it to the agent that carries it; an inherited value from an
    # `agent_templates:` entry is caught too, since `agent_raw` is already merged.
    for moved_key in _MOVED_TO_RULE_KEYS:
        if moved_key in agent_raw:
            raise ValueError(
                f"Agent '{agent_name}': '{moved_key}' is not an agent setting any "
                f"more — move it to the 'watcher_rules:' entry that uses this "
                f"agent. "
                f"It lives there now so that two entries sharing one agent can "
                f"have different session timeouts."
            )

    agent_cfg = AgentConfig(
        name=agent_name,
        type=agent_type,
        command=command,
        new_session_args=agent_raw.get("new_session_args", []),
        working_directory=working_directory,
        session_prefix=agent_raw.get("session_prefix", "agent-chat"),
        lazy_instruction_loading=lazy_instruction_loading,
        context_inject_files=ctx_files,
        owner_allowed_tools=_resolve_tool_entries(
            agent_raw.get("owner_allowed_tools", []),
            tool_presets,
            agent_name,
            "owner_allowed_tools",
        ),
        guest_allowed_tools=_resolve_tool_entries(
            agent_raw.get("guest_allowed_tools", []),
            tool_presets,
            agent_name,
            "guest_allowed_tools",
        ),
        timeout=agent_raw.get("timeout", 360),
        permissions=PermissionConfig(
            enabled=perm_raw.get("enabled", False),
            timeout=perm_raw.get("timeout", 300),
            skip_owner_approval=perm_raw.get("skip_owner_approval", False),
        ),
    )

    # Validate that agent.timeout > permissions.timeout when permissions are
    # enabled — folded in here (previously a separate pass over the fully-
    # built `agents` dict, AFTER from_file()'s agent loop finished) since it
    # only ever needs this one agent's own already-built AgentConfig;
    # per-entity exactly like every other check above, so collect_config()
    # below can attribute it to the right agent instead of stopping the
    # whole load. If agent.timeout <= permissions.timeout, the HTTP call can
    # time out while a permission request is still pending, leaving an
    # orphaned PermissionRequest in the registry.
    #
    # PR review finding, accepted trade-off: folding this in HERE (instead
    # of leaving it as its own pass after every agent has already been
    # built) changes which agent's error `from_file()` raises FIRST when
    # MULTIPLE agents are simultaneously broken for DIFFERENT reasons — e.g.
    # agent_a with a bad timeout/permissions.timeout relationship, followed
    # in the YAML by agent_b missing working_directory, now raises agent_a's
    # error first (this check runs inline, per-agent) instead of agent_b's
    # (which the old code always hit first, since the separate timeout pass
    # only ran after every agent had ALREADY been individually validated and
    # built). No test in tests/unit/test_config_loading.py pins this
    # specific cross-agent ordering (only ever one problem per fixture), and
    # `from_file()` was never a "report every problem" API to begin with —
    # but it IS a real, verified difference in the exact single error message
    # a multi-broken-agent config's from_file() call raises. Accepted because
    # the alternative (a second, separate implementation of this exact check
    # in collect_config()'s own agent loop, just to preserve from_file()'s
    # incidental ordering) would reintroduce the very rule-duplication risk
    # this whole extraction exists to eliminate.
    if agent_cfg.permissions.enabled and agent_cfg.timeout <= agent_cfg.permissions.timeout:
        raise ValueError(
            f"Agent '{agent_name}': timeout ({agent_cfg.timeout}s) must be greater than "
            f"permissions.timeout ({agent_cfg.permissions.timeout}s). "
            f"Suggested: set timeout to at least {agent_cfg.permissions.timeout + 60}s."
        )

    return agent_cfg


def _key_list(keys) -> str:
    """Format a key set for an error message.

    Stringifies before sorting because YAML keys need not be strings: `1: value`
    would otherwise make `sorted()` compare int with str, or `", ".join()` receive
    an int — raising TypeError out of the *error path*, which escapes
    collect_config()'s `except ValueError` and aborts the whole validation pass
    instead of reporting one bad entry.
    """
    return ", ".join(sorted(repr(str(k)) for k in keys))


_HH_FIELD_TYPES: dict[str, type] = {
    "enabled": bool,
    "fetch_count": int,
    "verbatim_tail": int,
}


def _parse_history_handoff(raw: object, where: str) -> HistoryHandoffConfig:
    """Validate a `history_handoff:` block — its keys as well as its values.

    Validating only the outer mapping was not enough, and the gap had the same
    shape as the bug it replaced: `history_handoff: {enable: false}` — one letter
    short — was silently ignored, leaving handoff **enabled**, the exact inversion
    `history_handoff: false` used to produce. Since `enabled` defaults to True,
    every typo in this block fails in the direction the operator did not want.

    Types are enforced too: `enabled: "false"` is a truthy string, and a negative
    or non-integer count would reach the handoff fetch unchallenged.

    Defaults are sourced from module-level `_HH_DEFAULTS` — see its docstring.
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValueError(
            f"{where}: 'history_handoff' must be a mapping "
            f"(got {type(raw).__name__}). To turn it off, set "
            "'history_handoff: {enabled: false}'."
        )
    unknown = set(raw) - set(_HH_FIELD_TYPES)
    if unknown:
        raise ValueError(
            f"{where}: unknown key(s) in 'history_handoff': "
            f"{_key_list(unknown)}. Valid keys are {_key_list(_HH_FIELD_TYPES)}."
        )
    values: dict[str, object] = {}
    for key, want in _HH_FIELD_TYPES.items():
        if key not in raw:
            continue
        value = raw[key]
        # bool is a subclass of int, so an int field must reject it explicitly and
        # a bool field must not accept 0/1.
        if want is bool and not isinstance(value, bool):
            raise ValueError(
                f"{where}: 'history_handoff.{key}' must be true or false "
                f"(got {type(value).__name__})."
            )
        if want is int and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError(
                f"{where}: 'history_handoff.{key}' must be an integer "
                f"(got {type(value).__name__})."
            )
        if want is int and value < 0:
            raise ValueError(
                f"{where}: 'history_handoff.{key}' must not be negative "
                f"(got {value})."
            )
        values[key] = value
    return HistoryHandoffConfig(
        enabled=values.get("enabled", _HH_DEFAULTS.enabled),
        fetch_count=values.get("fetch_count", _HH_DEFAULTS.fetch_count),
        verbatim_tail=values.get("verbatim_tail", _HH_DEFAULTS.verbatim_tail),
    )


def _resolve_watcher_connector(
    wc: Mapping,
    where: str,
    connector_names: set[str],
) -> str:
    """The rule's `connector:`, which it must state — there is no implicit default.

    A rule used to fall back to `connectors[0].name`, the first connector in the
    file. The docstring this replaces already argued the case against that, about
    a narrower bug: falling through to `connectors[0]` "is worse than the crash it
    was guarding, because it binds the watcher to the wrong account *silently*,
    and the canonical multi-agent setup gives every agent its own account." The
    same is true when nothing is wrong at all and the key is simply absent, so the
    fallback is gone rather than guarded.

    Shared with a template: a rule may take `connector` from its `inherits:`
    block, which is merged in before this runs — so "the rule must state it"
    means the MERGED rule.

    The type check comes BEFORE any truthiness test. An earlier review fixed only
    the truthy half — a YAML list reaching a set-membership test and crashing —
    and the `and` it used left the falsy half open: `connector: false`, `0`, or
    `[]` skipped the check and read as falsy. Those now reach the type error
    rather than a fallback.

    `null` used to mean "use the default", which is how an entry declined an
    inherited connector. Declining the only source now leaves the rule without
    one, which is the same error as omitting it — there is nothing to fall back
    to, and a rule that names no connector cannot be routed.
    """
    raw_connector = wc.get("connector", _MISSING_FIELD)
    if raw_connector is _MISSING_FIELD or raw_connector is None:
        raise ValueError(
            f"{where} has no 'connector' set. Add 'connector:' to it — one of: "
            f"{', '.join(sorted(connector_names))}. "
            f"(If several rules use the same connector, you can instead put it "
            f"in a 'watcher_templates:' entry and give each rule 'inherits:'.)"
        )
    if not isinstance(raw_connector, str):
        raise ValueError(
            f"{where}: 'connector' must be a string "
            f"(got {type(raw_connector).__name__})."
        )
    if raw_connector not in connector_names:
        raise ValueError(
            f"{where} references unknown connector '{raw_connector}'"
        )
    return raw_connector


def _validated_watcher_agent(wc: Mapping, where: str, agents: dict) -> str:
    """The rule's `agent:`, which it must state — there is no implicit default.

    A rule used to fall back to the top-level `default_agent:`, which itself fell
    back to whichever agent came first in the file. That is a silent binding
    decided by document order, and the same objection the connector fallback's
    own docstring raises above: the canonical multi-agent setup gives every agent
    its own account, so guessing picks the wrong one without saying so.

    Shared with a template: a rule may take `agent` from its `inherits:` block,
    which is merged in before this runs — so "the rule must state it" means the
    MERGED rule, and one shared template still expresses "these rules all use
    this agent" without any implicit rule.

    Same shape as the connector check above and shared for the same reason: a
    truthy-but-non-string `agent` (e.g. a YAML list) reached `watcher_agent not in
    agents` — a dict — unchecked, crashing with an uncaught TypeError instead of
    the clean ValueError every caller expects.
    """
    if "agent" not in wc or wc.get("agent") is None:
        raise ValueError(
            f"{where} has no 'agent' set. Add 'agent:' to it — one of: "
            f"{', '.join(sorted(agents))}. "
            f"(If several rules use the same agent, you can instead put it in a "
            f"'watcher_templates:' entry and give each rule 'inherits:'.)"
        )
    watcher_agent = wc["agent"]
    if not isinstance(watcher_agent, str):
        raise ValueError(
            f"{where}: 'agent' must be a string "
            f"(got {type(watcher_agent).__name__})."
        )
    if watcher_agent not in agents:
        raise ValueError(f"{where} references unknown agent '{watcher_agent}'")
    return watcher_agent




def _parse_pattern_list(
    raw: object, where: str, field_name: str
) -> tuple[RoomPattern, ...]:
    """Validate and compile one glob list from a rule's `rooms:` block.

    Patterns are compiled here, at load, so an invalid pattern can never surface
    on the message-delivery path where no operator is present to fix it (§2.1).
    """
    if raw is None:
        return ()
    if isinstance(raw, str) or not isinstance(raw, Sequence):
        raise ValueError(
            f"{where}: 'rooms.{field_name}' must be a list "
            f"of patterns (got {type(raw).__name__})."
        )
    out: list[RoomPattern] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str) or not item:
            raise ValueError(
                f"{where}: 'rooms.{field_name}' entries "
                "must be non-empty strings."
            )
        if item in seen:
            raise ValueError(
                f"{where}: 'rooms.{field_name}' contains "
                f"duplicate pattern '{item}'."
            )
        seen.add(item)
        try:
            out.append(RoomPattern(item))
        except InvalidRoomPattern as e:
            raise ValueError(
                f"{where}: 'rooms.{field_name}' pattern "
                f"'{item}' is not valid: {e}"
            ) from e
    return tuple(out)


def _parse_dm_flag(rooms_raw: Mapping, where: str, field_name: str) -> bool:
    """Read `rooms.direct` / `rooms.group_direct`.

    Only the boolean form is accepted. §2.7 reserves the object form
    (`direct: {include: [...], except_for: [...]}`) for when per-DM control is
    genuinely needed, and the JSON schema leaves room for it so that adding it
    later is additive. Until then a mapping here is rejected explicitly rather
    than being silently truthy — which is exactly how `direct: {}` would
    otherwise read as "DMs enabled".
    """
    value = rooms_raw.get(field_name, False)
    if isinstance(value, bool):
        return value
    if isinstance(value, Mapping):
        raise ValueError(
            f"{where}: 'rooms.{field_name}' does not yet "
            "accept the object form; use true or false. Per-DM include/except_for "
            "is a planned extension."
        )
    raise ValueError(
        f"{where}: 'rooms.{field_name}' must be true or "
        f"false (got {type(value).__name__})."
    )


def _enforce_literal_rooms(
    matcher: RoomMatcher,
    where: str,
    rule_name: str,
    resolved_connector: str,
    connectors: list[ConnectorConfig],
) -> None:
    """Refuse rules a connector without unsolicited inbound could never materialize.

    A rule is a *pattern*: it is matched against rooms as they turn up on the
    transport. Script's messages arrive by direct injection that bypasses the
    connector, and Voice's rooms arrive as HTTP path segments — neither has a stream
    to discover rooms from (design §2.6). A wildcard include, or a DM opt-in, on such
    a connector therefore describes rooms nothing will ever offer: the rule loads,
    looks correct, and silently never fires. `RoomPattern.is_literal` was added for
    exactly this check.

    Refused, specifically: a non-literal `include` pattern, an include that resolves
    to no literal room at all, and either DM opt-in. `except_for` is deliberately
    *not* restricted — §2.6 requires literal `include` entries, and a non-literal
    exclusion of rooms that are all literal is merely redundant, not unreachable.

    The check reads the **resolved** connector, not the written one: a rule with no
    `connector:` falls back to `connectors[0]`, so a config whose first connector is
    a voice connector must be caught even though the rule names nothing.

    An unrecognised connector type is treated as *not* having unsolicited inbound —
    A type this build does not recognise at all is a different matter and is left
    alone here: `config validate` reports the type itself, and saying anything about
    room patterns on top of that only buries the real problem. That was not
    theoretical — a config reading `mattrmost` produced a lecture about
    `rooms.direct` and never mentioned the missing letter, and the remedy the
    lecture suggested (use literal `rooms.include`) silenced the complaint while
    leaving the type wrong.
    """
    connector_type = next(
        (c.type for c in connectors if c.name == resolved_connector), None
    )
    if connector_type in TYPES_WITH_UNSOLICITED_INBOUND:
        return
    if connector_type is not None and connector_type not in SUPPORTED_CONNECTOR_TYPES:
        return  # the type is the error; something that can say so reports it

    kind = f"'{connector_type}'" if connector_type else "of unknown type"
    # One plain sentence, reused by all three cases below. The old wording led
    # with the mechanism ("has no unsolicited inbound stream — it never reports a
    # room the gateway did not already name"), which told the reader how the
    # gateway works instead of what to change.
    reason = (
        f"{where}: the {kind} connector '{resolved_connector}' "
        "cannot discover rooms by itself, so it needs each room listed by name."
    )
    for pattern in matcher.include:
        if not pattern.is_literal:
            raise ValueError(
                f"{reason} '{pattern.raw}' is a wildcard, so it will never match "
                "anything here — replace it in 'rooms.include' with the actual "
                "room names you meant."
            )
    if matcher.direct or matcher.group_direct:
        flag = "rooms.direct" if matcher.direct else "rooms.group_direct"
        raise ValueError(
            f"{reason} Remove '{flag}' and list the rooms in 'rooms.include' "
            "instead."
        )
    if not matcher.include:
        raise ValueError(
            f"{reason} The rule names no room at all, so nothing can ever match it — "
            "list the literal room name(s) in 'rooms.include'."
        )


def _parse_rule_ttl(wc: Mapping, where: str, field_name: str) -> int | None:
    """Read a per-rule session TTL.

    These live on the rule rather than the agent so two rules sharing one agent
    can differ (§5.4).
    """
    value = wc.get(field_name)
    if value is None:
        # An explicit null and an omitted key BOTH take the dataclass default
        # (15/15, §2.5): null is the documented template-inheritance suppress
        # (`_deep_merge`: "an explicit null intentionally suppresses a base
        # value"), so a child rule writes `session_expire_days: null` to undo
        # a template's value and return to the default. It is NOT a disable
        # switch — the pre-cutover disabled-TTL reading was the F1 defect's
        # side effect, never a documented meaning (Codex round 13, resolved
        # as documentation).
        return None
    # bool is an int subclass; `session_idle_days: true` is a mistake, not a 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"{where}: '{field_name}' must be a positive "
            f"integer (got {type(value).__name__})."
        )
    if value <= 0:
        raise ValueError(
            f"{where}: '{field_name}' must be a positive "
            f"integer (got {value})."
        )
    return value


# Every key a rule entry may carry. Kept in step with $defs/watcherRule in
# gateway/schema/config.schema.json by tests/unit/test_watcher_rule.py, because
# `agent-chat-gateway config validate` never runs the schema and would otherwise accept typos the
# schema rejects.
WATCHER_RULE_KEYS: frozenset[str] = frozenset({
    "description",
    "inherits",
    "name",
    "connector",
    "agent",
    "rooms",
    "session_idle_days",
    "session_expire_days",
    "context_inject_files",
    "history_handoff",
})


def _parse_one_watcher_rule(
    entry: object,
    index: int,
    *,
    connectors: list[ConnectorConfig],
    connector_names: set[str],
    agents: dict,
    config_dir: Path,
    templates: dict,
    seen_rule_names: set[str],
) -> WatcherRule:
    """Parse one rule-shaped `watchers:` entry into a `WatcherRule`.

    The only watcher parser left: the static shape is a hard load error at
    cutover, and its parser was deleted with the
    config TUI's rewrite onto rules. This returns exactly one object,
    because a rule is one thing — the expansion that used to turn
    `rooms: [a, b]` into several watchers now happens at runtime, per
    discovered room.
    """
    if not isinstance(entry, Mapping):
        raise ValueError(
            f"Watcher rule at index {index} must be a mapping "
            f"(got {type(entry).__name__})."
        )
    wc = _resolve_inherits(
        entry, templates, "watcher_templates", f"Watcher rule at index {index}",
        "watcher",
    )
    # Passed to the helpers this parser shares with the static one, which take the
    # message prefix rather than an index so there is one implementation of each
    # check instead of a rule-shaped copy beside a static-shaped one.
    where = f"Watcher rule at index {index}"

    # ── name: the RULE's identity, required and unique ──────────────────────
    # Required, unlike the static shape where it could be derived from the room.
    # A rule has no room to derive from, and the name is what shadowing warnings
    # and `rule_name` in persisted state refer to (§5.3).
    raw_name = wc.get("name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise ValueError(
            f"{where}: 'name' is required and must be a "
            "non-empty string. Rules are not auto-named: there is no single room "
            "to derive a name from."
        )
    rule_name = raw_name.strip()
    # From here on the rule has a name, so every message below says WHICH rule —
    # an index alone makes an operator count entries in their own file.
    where = f"{where} ('{rule_name}')"
    if rule_name in seen_rule_names:
        raise ValueError(f"Duplicate watcher rule name '{rule_name}'")

    # The loader guarantees this for entries it routes here, but this function is
    # also callable directly (the config tool calls
    # the static parser that way), so it is checked rather than asserted — an
    # assert would vanish under -O and leave a TypeError instead.
    # The schema sets additionalProperties: false on a rule, but `agent-chat-gateway config
    # validate` runs collect_config() rather than the JSON Schema, so a typo like
    # `session_expire_day: 30` would otherwise be silently ignored and the rule
    # would quietly have no expiry. Checked here so both paths agree; a test pins
    # this set against the schema so the two cannot drift.
    unknown_top = set(wc) - WATCHER_RULE_KEYS
    if unknown_top:
        raise ValueError(
            f"{where}: unknown key(s) "
            f"{_key_list(unknown_top)}. Valid keys are "
            f"{_key_list(WATCHER_RULE_KEYS)}."
        )

    rooms_raw = wc.get("rooms")
    if not isinstance(rooms_raw, Mapping):
        raise ValueError(
            f"{where}: 'rooms' must be a mapping with "
            f"include/except_for/direct/group_direct (got "
            f"{type(rooms_raw).__name__})."
        )
    unknown = set(rooms_raw) - {"include", "except_for", "direct", "group_direct"}
    if unknown:
        raise ValueError(
            f"{where}: unknown key(s) in 'rooms': "
            f"{_key_list(unknown)}. "
            "Valid keys are include, except_for, direct, group_direct."
        )

    matcher = RoomMatcher(
        include=_parse_pattern_list(rooms_raw.get("include"), where, "include"),
        except_for=_parse_pattern_list(rooms_raw.get("except_for"), where, "except_for"),
        direct=_parse_dm_flag(rooms_raw, where, "direct"),
        group_direct=_parse_dm_flag(rooms_raw, where, "group_direct"),
    )
    # An empty include is a hard error unless the rule is DM-only: with no
    # patterns and no DM opt-in the rule can never match anything, which is a
    # typo rather than an intention (§2.1).
    if not matcher.include and not matcher.claims_only_direct:
        raise ValueError(
            f"{where} can never match any "
            "room: 'rooms.include' is empty and neither 'rooms.direct' nor "
            "'rooms.group_direct' is set."
        )
    if matcher.except_for and not matcher.include:
        raise ValueError(
            f"{where}: 'rooms.except_for' has "
            "no effect without 'rooms.include' — it filters named rooms, "
            "and DM opt-ins are not name-matched."
        )
    # An except_for pattern that cannot overlap the include union is dead config
    # *reads* like protection. Excluding a room this rule never claimed does not
    # keep a later rule from claiming it, because a name the include misses is
    # NO_MATCH and falls through — so the operator who wrote it to block a room
    # got the opposite of what they intended, silently. Same typo class as the
    # empty-include error above, so it is refused the same way.
    for pattern in matcher.except_for:
        if not union_intersects([pattern], matcher.include):
            raise ValueError(
                f"{where}: "
                f"'rooms.except_for' entry '{pattern.raw}' does nothing here, "
                "because this rule's 'include' never matches a room by that "
                "name. 'except_for' only removes rooms from this rule's own "
                "'include' — it does not keep a room away from a later rule, "
                "which picks up whatever this one leaves. To keep a room away "
                "from every rule, list it in BOTH 'include' and 'except_for' "
                "here."
            )

    resolved_connector = _resolve_watcher_connector(wc, where, connector_names)
    watcher_agent = _validated_watcher_agent(wc, where, agents)
    _enforce_literal_rooms(matcher, where, rule_name, resolved_connector, connectors)

    idle_days = _parse_rule_ttl(wc, where, "session_idle_days")
    expire_days = _parse_rule_ttl(wc, where, "session_expire_days")
    # No ordering constraint between them, and the absence is deliberate. The
    # old rule — idle strictly less than expire — assumed both were measured
    # from the same origin, the moment the room went quiet. They are not:
    # expiry is measured from the moment the watcher becomes *idle* (§2.5), so
    # they are two independent legs of a sequence and `15/15` is the default.
    # Requiring an order here would reject that.
    #
    # Positivity is not re-checked here: `_parse_rule_ttl` already refuses a
    # zero or negative value, and stating one rule in two places is how it ends
    # up enforced in one.

    history_handoff = _parse_history_handoff(wc.get("history_handoff"), where)

    # Construct fully BEFORE registering the name. The static parser stages names
    # for exactly this reason, with a comment explaining it, and that lesson was
    # not carried over here: with the name added first, a rule that then failed
    # validation left its name registered even though no rule was returned, so a
    # later valid rule with the same name was rejected as a duplicate of something
    # that does not exist. Harmless under from_file()'s fail-fast loading, wrong
    # under collect_config(), which keeps going after a failed entry.
    # Omitted TTLs must fall through to the dataclass defaults (15/15, the
    # §2.5 ruling). Passing the parser's None explicitly OVERRODE them — every
    # config that did not spell the TTLs out had the whole idle/expiry
    # lifecycle silently off, and no test caught it because tests construct
    # WatcherRule directly, where the defaults apply. One source of truth:
    # the default lives on the dataclass, and this call simply does not
    # mention a field the operator did not mention.
    ttl_kwargs = {}
    if idle_days is not None:
        ttl_kwargs["session_idle_days"] = idle_days
    if expire_days is not None:
        ttl_kwargs["session_expire_days"] = expire_days
    rule = WatcherRule(
        name=rule_name,
        connector=resolved_connector,
        agent=watcher_agent,
        rooms=matcher,
        **ttl_kwargs,
        # _resolve_paths validates the container and every element itself, so
        # there is one implementation of this check rather than a rule-shaped copy
        # beside a static-shaped one.
        context_inject_files=_resolve_paths(
            wc.get("context_inject_files", []),
            config_dir,
            f"{where}: 'context_inject_files'",
        ),
        history_handoff=history_handoff,
    )
    seen_rule_names.add(rule_name)
    return rule


@dataclass(frozen=True)
class ShadowFinding:
    """One reach of one rule that an earlier rule has already taken.

    `scope` says how much of the rule is dead, because a rule can reach rooms in
    up to three independent ways and lose only some of them:

    * `"rule"` — every reach it has is already claimed; the rule can never fire.
    * `"named"` / `"direct"` / `"group_direct"` — that one reach is dead while the
      rule remains live for the others. Reported separately because a hybrid rule
      whose DM opt-in is dead looks perfectly healthy otherwise, and §2.1 asks for
      a warning when an earlier rule already claimed a DM class.
    """

    rule: WatcherRule
    shadowed_by: WatcherRule
    scope: str


def find_shadowed_rules(rules: list[WatcherRule]) -> list[ShadowFinding]:
    """Find reaches that can never fire because an earlier rule already takes them.

    Under first-match precedence a rule only ever sees rooms no earlier rule
    stopped, so anything already taken upstream is dead config — nearly always a
    mistake in ordering.

    **An earlier rule's blocking language is its `include`, not its
    `include` minus its `except_for`.** This is the part that is easy to get
    backwards. `except_for` produces `DECLINED`, which halts routing rather than
    falling through (§2.1) — so a room the earlier rule declines never reaches a
    later rule either. An earlier rule therefore blocks everything its `include`
    matches, whether it goes on to claim or decline it, and its own `except_for`
    is irrelevant to what it shadows. A deny rule (`include: [X]`,
    `except_for: [X]`) shadows later rules for `X` completely, which is precisely
    its purpose.

    DM classes are claimed by flag rather than by pattern, so an earlier rule
    takes this one's DM reach only by opting into the same class.

    Reported as warnings, never errors, and still deliberately under-reported in
    one case: a shadow formed only by **several** earlier rules together — `a*`
    plus `b*` covering `[ab]*` — is not reported, even though `union_subsumes`
    could prove it, because comparing one earlier rule at a time keeps each
    warning pointed at a single rule someone can go and read. A vaguer "these four
    rules collectively cover this one" is worth less than silence. Under-reporting
    is the right direction: a missed warning costs nothing, a false one sends
    someone hunting a rule that works.
    """
    out: list[ShadowFinding] = []
    for i, rule in enumerate(rules):
        earlier_rules = [e for e in rules[:i] if e.connector == rule.connector]

        # Which reaches this rule has, and the first earlier rule taking each.
        blockers: dict[str, WatcherRule] = {}
        if rule.rooms.include:
            blockers["named"] = next(
                (
                    e
                    for e in earlier_rules
                    if e.rooms.include
                    and union_subsumes(e.rooms.include, rule.rooms.include)
                ),
                None,
            )
        if rule.rooms.direct:
            blockers["direct"] = next(
                (e for e in earlier_rules if e.rooms.direct), None
            )
        if rule.rooms.group_direct:
            blockers["group_direct"] = next(
                (e for e in earlier_rules if e.rooms.group_direct), None
            )

        if not blockers:  # unreachable post-validation, but do not crash on it
            continue
        all_blocked = all(b is not None for b in blockers.values())
        one_blocker = len({id(b) for b in blockers.values()}) == 1
        if all_blocked and one_blocker:
            # A single earlier rule takes every reach, so "this rule is dead, see
            # that one" is both true and actionable.
            out.append(
                ShadowFinding(
                    rule=rule, shadowed_by=next(iter(blockers.values())), scope="rule"
                )
            )
        else:
            # Different earlier rules take different reaches. The rule is still
            # entirely dead when all_blocked, but collapsing would attribute it to
            # one blocker that does not claim the other reaches — a warning whose
            # suggested remedy would be wrong. Keep them separate so each points at
            # the rule actually responsible.
            for scope, by in blockers.items():
                if by is not None:
                    out.append(ShadowFinding(rule=rule, shadowed_by=by, scope=scope))
    return out


def _parse_max_queue_depth(raw: dict) -> int:
    """Parses and validates the top-level `max_queue_depth:` field. Shared
    by from_file() (lets a ValueError propagate immediately) and
    collect_config() (catches it, records a ConfigIssue, falls back to the
    default 100) — PR review finding: this was pasted twice, byte-identical,
    before this extraction; unlike connectors/agents/watchers, there was no
    single shared implementation for from_file()/collect_config() to drift
    apart from."""
    max_queue_depth = raw.get("max_queue_depth", 100)
    if not isinstance(max_queue_depth, int):
        raise ValueError(
            f"config.yaml 'max_queue_depth' must be an integer (got {type(max_queue_depth).__name__})."
        )
    if max_queue_depth < 0:
        raise ValueError("config.yaml 'max_queue_depth' must be >= 0")
    return max_queue_depth


def _parse_scheduler(raw: dict) -> "SchedulerConfig":
    """Parses and validates the top-level `scheduler:` block. Shared by
    from_file()/collect_config() — see _parse_max_queue_depth()'s docstring
    for why this extraction exists."""
    scheduler_raw = raw.get("scheduler", {}) or {}
    if not isinstance(scheduler_raw, Mapping):
        raise ValueError(
            f"config.yaml 'scheduler:' must be a mapping (got {type(scheduler_raw).__name__})."
        )
    scheduler_ttl = scheduler_raw.get("completed_job_ttl_days", 7)
    if not isinstance(scheduler_ttl, int) or scheduler_ttl < 0:
        raise ValueError(
            "config.yaml 'scheduler.completed_job_ttl_days' must be a non-negative integer."
        )
    return SchedulerConfig(completed_job_ttl_days=scheduler_ttl)


def _max_queue_depth_and_scheduler_or_defaults(
    raw: dict, issues: list["ConfigIssue"]
) -> tuple[int, "SchedulerConfig"]:
    """Best-effort max_queue_depth/scheduler parse, appending a ConfigIssue
    and falling back per-field (independently) on failure — same behavior
    collect_config()'s own final section already gives these two fields on
    its happy path. PR review finding: collect_config()'s several EARLIER
    structural early-return branches used to hardcode
    `max_queue_depth=100, scheduler=SchedulerConfig()` instead of calling
    this — silently discarding an otherwise-valid max_queue_depth/scheduler:
    value behind a completely unrelated structural problem elsewhere (e.g.
    a malformed `watchers:` block), the exact "don't hide an unrelated,
    already-successful value's own state behind a different issue" bug this
    whole function exists to avoid for connectors/agents/watchers. These
    two fields have no entity dependency at all, so there's no reason they
    couldn't always be parsed this way, everywhere collect_config() returns."""
    try:
        max_queue_depth = _parse_max_queue_depth(raw)
    except ValueError as exc:
        issues.append(ConfigIssue("global", None, str(exc)))
        max_queue_depth = 100

    try:
        scheduler_cfg = _parse_scheduler(raw)
    except ValueError as exc:
        issues.append(ConfigIssue("global", None, str(exc)))
        scheduler_cfg = SchedulerConfig()

    return max_queue_depth, scheduler_cfg


@dataclass(frozen=True)
class ConfigIssue:
    """One structural/per-entity problem discovered by collect_config()'s
    fault-tolerant pass — the non-fail-fast counterpart to from_file()'s
    single ValueError. Deliberately dependency-free (no import of
    gateway/config_validate.py's own, richer `Finding` dataclass) since
    config_validate.py already imports FROM this module — the reverse
    import would be circular. `gateway/config_validate.py`'s
    `validate_config()` converts each `ConfigIssue` into its own `Finding`
    (always severity="error" — everything collect_config() reports is a
    from_file()-would-have-raised problem, same severity from_file() always
    implied by raising at all)."""

    entity_kind: Literal["connector", "agent", "watcher", "global"]
    entity_name: str | None
    message: str


def collect_config(path: str | Path) -> tuple["GatewayConfig | None", list[ConfigIssue]]:
    """Fault-tolerant counterpart to `GatewayConfig.from_file()`: instead of
    stopping at the FIRST bad connector/agent/watcher, collects a
    `ConfigIssue` for EVERY one that fails independently and keeps going —
    reusing `_parse_one_connector()`/`_parse_one_agent()`/
    `_parse_one_watcher_rule()` verbatim (never a second copy of any rule).

    Returns a best-effort `GatewayConfig` built from whatever entities DID
    parse successfully, so callers (`gateway/config_validate.py`'s
    `validate_config()`) can still run further per-entity checks against it
    (e.g. `_check_connectors()`'s empty-credential check) — this is what
    lets the config TUI's Overview attribute a real per-row error to the
    SPECIFIC agent/watcher/connector at fault, instead of one global
    "something is wrong" banner that never marks any row, and lets
    `EditableConfig.save()` compare "problems before this edit" against
    "problems after" on a genuinely per-entity basis.

    Only the three per-entity for-loops (connectors/agents/watchers) get
    this fault-tolerant treatment. Every STRUCTURAL check — is `connectors:`
    even a list, is there at least one agent, is `watcher_rules:` a list,
    `tool_presets:`/`*_templates:` blocks themselves
    well-formed, `max_queue_depth`/`scheduler:` shape — stays a hard,
    immediate stop: there's no meaningful "keep going with the other 9
    connectors" fallback when the document's basic shape is broken (e.g.
    which connector would you even skip if `connectors:` isn't a list at
    all?). `max_queue_depth`/`scheduler:` are the one partial exception —
    invalid values there don't gate any single entity's validity, so they're
    collected as issues and the returned config falls back to their safe
    defaults, rather than discarding all the connector/agent/watcher
    progress already made.

    Returns `(None, [issue])` ONLY when a structural check fails before ANY
    entity has even started parsing (missing file, malformed top level,
    `connectors:` itself not a list, etc.) — nothing usable to build yet.
    Returns `(config, [])` when everything succeeds — identical to what
    `from_file()` would return. Every OTHER structural check (agents:
    malformed, zero agents, `watcher_rules:` malformed) still returns as much
    of a partial `GatewayConfig` as had already parsed successfully BEFORE
    that check — e.g. a config with no usable connectors still returns every
    agent that parsed fine (with no rules, since a rule cannot be resolved
    without one) rather than discarding them too. This matters: `_check_connectors()` and
    friends (`gateway/config_validate.py`) run against whatever this
    returns, and an unrelated, already-successful entity's OWN problems
    must never be hidden behind a completely different structural issue
    elsewhere in the file — PR review caught this discarding real,
    unrelated connector-credential problems in exactly that way.

    In every case, `partial_config` (when not None) omits exactly the
    entities that individually failed (an entity referencing one of THOSE —
    e.g. a watcher whose `agent:` failed to parse — independently reports
    its own "unknown agent" issue too; not a duplicate, still useful, it
    tells you *that* watcher is also affected)."""
    path = Path(path)
    if not path.exists():
        return None, [ConfigIssue("global", None, f"Config file not found: {path}")]

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    if not isinstance(raw, dict):
        return None, [
            ConfigIssue(
                "global", None,
                f"Config file '{path}' must contain a YAML mapping at the top level, "
                f"got {type(raw).__name__}.",
            )
        ]

    for old_key, new_key in _REMOVED_DEFAULTS_KEYS.items():
        if old_key in raw:
            return None, [
                ConfigIssue(
                    "global", None,
                    f"config.yaml '{old_key}:' is no longer supported (removed) — "
                    f"define shared fields under '{new_key}:' instead and add "
                    "'inherits: <template-name>' to each entry that should use "
                    "them. See docs/migration-0.3.md.",
                )
            ]

    config_dir = path.parent
    issues: list[ConfigIssue] = []

    # Same check as from_file(), reported rather than raised: `agent-chat-gateway config validate`
    # and the config TUI both come through here, so an old `watchers:` block has
    # to be named on THIS path or it reads as a clean config with no rules.
    unknown_top = unknown_top_level_keys(raw)
    if unknown_top:
        issues.append(
            ConfigIssue("global", None, unknown_top_level_message(unknown_top))
        )

    # ── Connectors ────────────────────────────────────────────────────────
    connectors_raw = raw.get("connectors", [])
    if not connectors_raw:
        return None, [
            ConfigIssue(
                "global", None,
                "config.yaml must define at least one connector under 'connectors:'",
            )
        ]
    if not isinstance(connectors_raw, list):
        return None, [
            ConfigIssue(
                "global", None,
                f"config.yaml 'connectors:' must be a list (got {type(connectors_raw).__name__}).",
            )
        ]

    try:
        connector_templates = _parse_templates_block(
            raw, "connector_templates", TEMPLATE_FORBIDDEN_KEYS["connector"]
        )
    except ValueError as exc:
        return None, [ConfigIssue("global", None, str(exc))]

    connectors: list[ConnectorConfig] = []
    seen_connector_names: set[str] = set()
    for i, cc_raw in enumerate(connectors_raw):
        # PR review finding: `name:` itself might be malformed (e.g. a list
        # instead of a string) on an entry that ALSO fails for some other
        # reason — ConfigIssue.entity_name/Finding.entity_name are typed
        # `str | None` everywhere downstream, and EditableConfig's save-gate
        # (model.py's _new_errors_introduced_by_this_save()) puts this value
        # straight into a set of tuples, which raises an uncaught
        # `TypeError: unhashable type` for anything non-hashable (e.g. a
        # list). Only ever use name_hint when it's genuinely a usable
        # string; anything else falls back to the same "(index i)" label an
        # absent name already gets.
        name_hint = cc_raw.get("name") if isinstance(cc_raw, Mapping) else None
        if not isinstance(name_hint, str) or not name_hint:
            name_hint = None
        try:
            connectors.append(
                _parse_one_connector(cc_raw, i, connector_templates, config_dir, seen_connector_names)
            )
        except ValueError as exc:
            issues.append(ConfigIssue("connector", name_hint or f"(index {i})", str(exc)))

    # ── Agents ────────────────────────────────────────────────────────────
    # PR review finding: every early-return in this section used to
    # `return None, issues` — discarding the connectors already parsed
    # above, so validate_config()'s _check_connectors()/_check_state_orphans()
    # never ran on them even though they have nothing to do with an agents:-
    # section problem. Returned as a partial config (agents={}, no rules)
    # instead, so already-successful connectors keep getting checked.
    agents_raw = raw.get("agents") or {}
    if not isinstance(agents_raw, dict):
        issues.append(
            ConfigIssue(
                "global", None,
                f"config.yaml 'agents:' must be a mapping (got {type(agents_raw).__name__}). "
                f"Expected a dict of agent names to config blocks.",
            )
        )
        mqd, sched = _max_queue_depth_and_scheduler_or_defaults(raw, issues)
        return (
            GatewayConfig(
                connectors=connectors, agents={},
                max_queue_depth=mqd, scheduler=sched,
            ),
            issues,
        )

    try:
        agent_templates = _parse_templates_block(raw, "agent_templates", TEMPLATE_FORBIDDEN_KEYS["agent"])
        tool_presets = _parse_tool_presets(raw)
    except ValueError as exc:
        issues.append(ConfigIssue("global", None, str(exc)))
        mqd, sched = _max_queue_depth_and_scheduler_or_defaults(raw, issues)
        return (
            GatewayConfig(
                connectors=connectors, agents={},
                max_queue_depth=mqd, scheduler=sched,
            ),
            issues,
        )

    agents: dict[str, AgentConfig] = {}
    for agent_name, agent_raw_entry in agents_raw.items():
        try:
            agents[agent_name] = _parse_one_agent(
                agent_name, agent_raw_entry, agent_templates, tool_presets, config_dir
            )
        except ValueError as exc:
            issues.append(ConfigIssue("agent", agent_name, str(exc)))

    if not agents:
        # Every agent independently failed (or there were none defined) —
        # still return the connectors already parsed above (same reasoning
        # as the branches above/below: an unrelated connector problem must
        # not be hidden behind this).
        issues.append(
            ConfigIssue(
                "global", None,
                "config.yaml must define at least one agent under 'agents:'",
            )
        )
        mqd, sched = _max_queue_depth_and_scheduler_or_defaults(raw, issues)
        return (
            GatewayConfig(
                connectors=connectors, agents={},
                max_queue_depth=mqd, scheduler=sched,
            ),
            issues,
        )


    # ── Watchers ──────────────────────────────────────────────────────────
    connector_names = {c.name for c in connectors}
    if not connectors:
        # Every connector independently failed — from_file() could never
        # reach this point (an earlier raise would have stopped it first),
        # but collect_config() can legitimately get here. Nothing to
        # meaningfully resolve an implicit `connector:` against, so rules
        # are skipped (empty) — every AGENT that DID parse is still returned
        # rather than discarded wholesale, so its own checks keep running
        # against an unrelated, already-successful entity.
        issues.append(
            ConfigIssue(
                "global", None,
                "No connectors parsed successfully — cannot resolve watcher entries.",
            )
        )
        mqd, sched = _max_queue_depth_and_scheduler_or_defaults(raw, issues)
        return (
            GatewayConfig(
                connectors=[],
                agents=agents,
                    max_queue_depth=mqd,
                scheduler=sched,
            ),
            issues,
        )

    watcher_rules: list[WatcherRule] = []
    watchers_raw = raw.get("watcher_rules", [])
    # Same None-then-type ordering as from_file() above; a raw TypeError out
    # of THIS function is worse, since collecting problems instead of raising
    # them is its whole contract.
    if watchers_raw is None:
        watchers_raw = []
    if not isinstance(watchers_raw, list):
        # Connectors AND agents have already parsed successfully by this
        # point — same "don't hide an unrelated, already-successful entity's
        # checks behind this" reasoning as every branch above.
        issues.append(
            ConfigIssue(
                "global", None,
                "config.yaml 'watcher_rules:' must be a list (got "
                f"{type(watchers_raw).__name__}).",
            )
        )
        mqd, sched = _max_queue_depth_and_scheduler_or_defaults(raw, issues)
        return (
            GatewayConfig(
                connectors=connectors, agents=agents,
                max_queue_depth=mqd, scheduler=sched,
            ),
            issues,
        )

    try:
        watcher_templates = _parse_templates_block(
            raw, "watcher_templates", TEMPLATE_FORBIDDEN_KEYS["watcher"]
        )
    except ValueError as exc:
        issues.append(ConfigIssue("global", None, str(exc)))
        mqd, sched = _max_queue_depth_and_scheduler_or_defaults(raw, issues)
        return (
            GatewayConfig(
                connectors=connectors, agents=agents,
                max_queue_depth=mqd, scheduler=sched,
            ),
            issues,
        )

    # Mirrors from_file()'s routing, per entry and by shape — one `seen_*` set
    # per load, for the whole loop rather than per entry.
    seen_rule_names: set[str] = set()
    for i, wc_raw in enumerate(watchers_raw):
        # See the identical connector-loop comment above: only ever use
        # name_hint when it's genuinely a usable string.
        name_hint = wc_raw.get("name") if isinstance(wc_raw, Mapping) else None
        if not isinstance(name_hint, str) or not name_hint:
            name_hint = None
        # Every failure inside the parser is a ValueError and never a TypeError
        # precisely so this `except` can attribute it to one entry and keep going;
        # a TypeError escaping here would abort the whole validation pass and report
        # one global error instead of one bad entry among many good ones.
        try:
            if not isinstance(wc_raw, Mapping):
                raise ValueError(
                    f"watcher_rules[{i}] must be a mapping (got "
                    f"{type(wc_raw).__name__})."
                )
            watcher_rules.append(
                _parse_one_watcher_rule(
                    wc_raw, i,
                    connectors=connectors,
                    connector_names=connector_names,
                    agents=agents,
                    config_dir=config_dir,
                    templates=watcher_templates,
                    seen_rule_names=seen_rule_names,
                )
            )
        except ValueError as exc:
            issues.append(ConfigIssue("watcher", name_hint or f"(index {i})", str(exc)))

    # No cross-watcher duplicate-session_id pass here either — see from_file()'s
    # note where the same loop used to be.

    # max_queue_depth/scheduler: don't gate any single entity's validity —
    # collected as issues, falling back to safe defaults for the returned
    # config, rather than discarding all connector/agent/watcher progress.
    # Same helper every earlier structural early-return in this function
    # now also uses (PR review finding) — max_queue_depth/scheduler have no
    # entity dependency at all, so there's no reason an early return
    # elsewhere should ever have hardcoded these to their defaults instead
    # of actually parsing them.
    max_queue_depth, scheduler_cfg = _max_queue_depth_and_scheduler_or_defaults(raw, issues)

    config = GatewayConfig(
        connectors=connectors,
        agents=agents,
        watcher_rules=watcher_rules,
        max_queue_depth=max_queue_depth,
        scheduler=scheduler_cfg,
    )
    return config, issues
