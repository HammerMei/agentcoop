"""What changed between two resolved configurations, and how to name one (#144).

`config reload` compares the configuration the daemon is running against the
one on disk. The comparison is over **parsed dataclasses**, never YAML text:
comments, key order, quoting and a `description:` field (which every entity
parser drops) register as nothing, and a template edit registers as a change
to every entry inheriting it, because inheritance is flattened at parse time.

Three things live here, all pure:

* **the action table** — every field of every config entity is classified once
  (`RELOAD_ACTIONS`), so a new field cannot arrive without saying what a reload
  does about it; `tests/unit/test_config_diff.py` enumerates the dataclasses
  against it;
* **the diff** — `diff_configs(active, candidate)`: entities by `name`, a
  rename read as a removal plus an addition, and the two top-level values
  that are swapped in place;
* **the digest** — `config_digest(config)`: SHA-256 over a canonical
  serialization of the resolved config, so two files that mean the same thing
  hash the same, and `flatten_config` for `config show`, with secrets redacted.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from .config import AgentConfig, ConnectorConfig, GatewayConfig, SchedulerConfig
from .core.room_pattern import RoomPattern
from .core.watcher_rule import WatcherRule

# What a reload does when a field of that entity changes.
#   restart-connector — the connector and its session manager are rebuilt
#   restart-agent     — the backend (and an OpenCode sidecar) restarts, its
#                       broker is rebuilt, its resident processors restart
#   reconcile         — every record is re-matched against the current rules
#   value             — the value is replaced in the running service
#   identity          — the field IS the entity's name; a change is a removal
#                       plus an addition, not a change to one entity
#   section           — a top-level block whose entries are classified on
#                       their own dataclass
ReloadAction = Literal[
    "restart-connector", "restart-agent", "reconcile", "value", "identity", "section",
]

RELOAD_ACTIONS: dict[type, dict[str, ReloadAction]] = {
    ConnectorConfig: {
        "name": "identity",
        "type": "restart-connector",
        "raw": "restart-connector",
        "context_inject_files": "restart-connector",
    },
    AgentConfig: {
        "name": "identity",
        "type": "restart-agent",
        "command": "restart-agent",
        "new_session_args": "restart-agent",
        "working_directory": "restart-agent",
        "session_prefix": "restart-agent",
        "lazy_instruction_loading": "restart-agent",
        "context_inject_files": "restart-agent",
        "owner_allowed_tools": "restart-agent",
        "guest_allowed_tools": "restart-agent",
        "timeout": "restart-agent",
        "permissions": "restart-agent",
    },
    WatcherRule: {
        # A rule's name is its identity too, but ownership is recomputed by
        # re-matching, so a renamed rule is harmless: the record it created
        # re-materializes to the new name (#143's classification test).
        "name": "reconcile",
        "connector": "reconcile",
        "agent": "reconcile",
        "rooms": "reconcile",
        "session_idle_days": "reconcile",
        "session_expire_days": "reconcile",
        "context_inject_files": "reconcile",
        "history_handoff": "reconcile",
    },
    GatewayConfig: {
        "connectors": "section",
        "agents": "section",
        "watcher_rules": "section",
        "max_queue_depth": "value",
        "scheduler": "section",
    },
    SchedulerConfig: {
        "completed_job_ttl_days": "value",
    },
}


@dataclass
class EntityChanges:
    """Names added, changed and removed for one kind of entity."""

    added: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.added or self.changed or self.removed)

    def to_dict(self) -> dict:
        """The `--json` shape of one entity kind's changes."""
        return {"added": list(self.added), "changed": list(self.changed),
                "removed": list(self.removed)}


@dataclass(frozen=True)
class ValueChange:
    """One top-level value swapped in place."""

    path: str
    old: Any
    new: Any


@dataclass
class ConfigDiff:
    """What changed between the active and the candidate config, by entity."""

    connectors: EntityChanges = field(default_factory=EntityChanges)
    agents: EntityChanges = field(default_factory=EntityChanges)
    rules: EntityChanges = field(default_factory=EntityChanges)
    # A rule list whose entries are all unchanged but stand in a different
    # order routes differently (first match wins), so it is a change to the
    # rules block even though no entity is added, changed or removed.
    rules_reordered: bool = False
    values: list[ValueChange] = field(default_factory=list)

    @property
    def rules_changed(self) -> bool:
        return bool(self.rules) or self.rules_reordered

    def __bool__(self) -> bool:
        return bool(self.connectors or self.agents or self.rules_changed or self.values)


def _entity_changes(old: dict[str, Any], new: dict[str, Any]) -> EntityChanges:
    return EntityChanges(
        added=[n for n in new if n not in old],
        changed=[n for n in new if n in old and old[n] != new[n]],
        removed=[n for n in old if n not in new],
    )


def diff_configs(active: GatewayConfig, candidate: GatewayConfig) -> ConfigDiff:
    """Compare two resolved configurations entity by entity.

    Identity is `name` for connectors, agents and rules: a renamed entity is a
    removal plus an addition. For a connector that means every record under
    the old name expires and its state file goes; the plan renderer says so.
    Equality is the dataclasses' own — `ConnectorConfig.raw` is compared as
    the dict it is, a rotated token is a change, a re-indented one is not.
    """
    diff = ConfigDiff()
    diff.connectors = _entity_changes(
        {c.name: c for c in active.connectors}, {c.name: c for c in candidate.connectors})
    diff.agents = _entity_changes(dict(active.agents), dict(candidate.agents))
    diff.rules = _entity_changes(
        {r.name: r for r in active.watcher_rules}, {r.name: r for r in candidate.watcher_rules})
    if not diff.rules:
        diff.rules_reordered = (
            [r.name for r in active.watcher_rules] != [r.name for r in candidate.watcher_rules])
    if active.max_queue_depth != candidate.max_queue_depth:
        diff.values.append(ValueChange(
            "max_queue_depth", active.max_queue_depth, candidate.max_queue_depth))
    for f in fields(SchedulerConfig):
        old, new = getattr(active.scheduler, f.name), getattr(candidate.scheduler, f.name)
        if old != new:
            diff.values.append(ValueChange(f"scheduler.{f.name}", old, new))
    return diff


# ── Digest and dump ─────────────────────────────────────────────────────────────


def canonical(value: Any) -> Any:
    """A resolved config as JSON-safe data, walking dataclass fields — the form
    the digest hashes.

    **Every leaf is typed**: `[type name, value]`. That makes the form
    injective over what the YAML loader can produce, which a bare-value form
    is not: `build_date: 2026-09-05` (a `date`) and `build_date: "2026-09-05"`
    (a `str`) are different `raw` dicts to the diff — which restarts the
    connector — and any tag spelled INSIDE the value space (a mapping such as
    `{$type: date, ...}`) is itself a legal `raw` value an operator could
    write. With the type outside the value, at every leaf, two configs
    canonicalize alike only if they compare alike. Containers stay containers;
    `untagged` strips the tags for anything a human or a script reads.

    `RoomPattern` contributes its canonical spelling (`RoomPattern.canonical` —
    the form `==` compares, so equivalent spellings digest alike); an enum its
    value; a path, date or time its ISO string; anything else the loader could
    produce falls back to `str`, so a config that loaded always digests.
    """
    if isinstance(value, RoomPattern):
        return ["pattern", value.canonical()]
    if isinstance(value, Enum):
        return ["enum", value.value]
    if isinstance(value, Path):
        return ["path", str(value)]
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return [type(value).__name__, value.isoformat()]
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: canonical(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, dict):
        # Keys typed too: YAML allows `1:` and `'1':` in an open `raw` block,
        # and they are different dicts to the diff. A string key keeps its
        # spelling; any other key is `<type>:<repr>`, which no string key can
        # spell without the type prefix reading as part of the string.
        return {(k if isinstance(k, str) else f"{type(k).__name__}:{k!r}"): canonical(v)
                for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [canonical(v) for v in value]
        return sorted(items, key=repr) if isinstance(value, (set, frozenset)) else items
    if value is None or isinstance(value, (bool, int, float, str)):
        return [type(value).__name__, value]
    return [type(value).__name__, str(value)]


def _is_leaf(value: Any) -> bool:
    """A typed leaf: a two-list whose first element is a bare string. No real
    list canonicalizes to that shape — a real list's elements are themselves
    lists or dicts, never bare strings."""
    return isinstance(value, list) and len(value) == 2 and isinstance(value[0], str)


def untagged(value: Any) -> Any:
    """The canonical form with the leaf tags stripped — plain values, for the
    dump and the JSON document."""
    if _is_leaf(value):
        return value[1]
    if isinstance(value, dict):
        return {k: untagged(v) for k, v in value.items()}
    if isinstance(value, list):
        return [untagged(v) for v in value]
    return value


def _identity_keyed(config: GatewayConfig) -> dict:
    """The canonical form with connectors keyed by name — the shape both the
    digest and the dump use.

    Connectors are an identity-keyed set, and the diff treats them as one; the
    digest must agree, or reordering two connectors would be "no changes" to
    reload and "differs" to `config show`, forever. Rule order stays
    significant — first match wins — and is not touched.
    """
    data = canonical(config)
    data["connectors"] = {untagged(c["name"]): c for c in data["connectors"]}
    return data


def config_digest(config: GatewayConfig) -> str:
    """SHA-256 (hex) of the canonical serialization of the RESOLVED config.

    Templates and inheritance are already expanded in a `GatewayConfig`, so a
    file rewritten with the same meaning — reordered keys, added comments, a
    value moved into a template — keeps its digest, and a rotated secret
    changes it (the digest is over the unredacted values; it is a fingerprint,
    not a dump).
    """
    payload = json.dumps(_identity_keyed(config), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


# Case-insensitive substrings of a key that mark its value as a secret.
SECRET_KEY_MARKERS = ("password", "token", "secret")
REDACTED = "***"


def is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in SECRET_KEY_MARKERS)


def _redact(value: Any, key: str = "") -> Any:
    """The one redaction walk: WHATEVER sits under a key that names a password,
    token or secret becomes `***` — a scalar, a list of them, or a mapping such
    as `client_secret: {value: …}` — at any depth, a connector's type-specific
    `raw` block included. Once a key is secret its whole subtree is."""
    if key and is_secret_key(key):
        return REDACTED
    if isinstance(value, dict):
        return {k: _redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, key) for v in value]
    return value


# The top-level blocks whose immediate keys are entity NAMES, not field names:
# an agent called `secretary` or a connector called `token-bot` is not a secret.
_NAMED_BLOCKS = ("agents", "connectors")


def _redact_config(data: dict) -> dict:
    """`_redact` over a canonical config, with entity names exempt from the
    key test — the marker match applies to field names only."""
    out = {}
    for key, value in data.items():
        if key in _NAMED_BLOCKS and isinstance(value, dict):
            out[key] = {name: _redact(entity) for name, entity in value.items()}
        elif key in _NAMED_BLOCKS and isinstance(value, list):
            out[key] = [_redact(entity) for entity in value]
        else:
            out[key] = _redact(value, key)
    return out


def redacted_config(config: GatewayConfig) -> dict:
    """The resolved config as plain values with secrets redacted, for `--json`."""
    return _redact_config(untagged(canonical(config)))


def flatten_config(config: GatewayConfig) -> list[tuple[str, Any]]:
    """`(dotted.path, value)` pairs over the redacted canonical form, for `config show`.

    Lists index as `[n]`; connectors are keyed by name rather than position so
    two machines' dumps line up.
    """
    out: list[tuple[str, Any]] = []

    def walk(prefix: str, value: Any) -> None:
        if isinstance(value, dict):
            if not value:
                out.append((prefix, {}))
            for k in sorted(value):
                walk(f"{prefix}.{k}" if prefix else k, value[k])
            return
        if isinstance(value, list):
            if not value:
                out.append((prefix, []))
            for i, item in enumerate(value):
                walk(f"{prefix}[{i}]", item)
            return
        out.append((prefix, value))

    walk("", _redact_config(untagged(_identity_keyed(config))))
    return out
