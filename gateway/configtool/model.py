"""EditableConfig — the pre-merge raw document the config TUI reads (and, in
later phases, writes).

This is the keystone decision recorded in docs/design/config-tool.md: the
editor operates on the raw, as-authored YAML structure, never on the
post-merge ``GatewayConfig``. That's the only place two things the TUI needs
are still visible:

  1. Provenance — whether a field on an entry is explicit, inherited from a
     named ``*_templates:`` block via the entry's own ``inherits:`` field, or
     an explicit ``null`` suppressing a template's value.
     ``GatewayConfig.from_file`` already applied the merge by the time it
     returns; the distinction is gone.
  2. Which entry each value was WRITTEN on. A rule and the template it
     inherits from are one merged mapping by the time ``from_file`` returns,
     so the editor could no longer tell which of the two to write an edit
     back to — and ``rooms:`` is inheritable, which makes that true of the
     matcher as well as of the scalar fields.

     (This slot used to describe raw ``rooms: [a, b, c]`` groupings being
     expanded into one ``WatcherConfig`` per room. That data shape is gone:
     a ``watcher_rules:`` entry is a rule, a list-shaped ``rooms:`` is a load
     error, and nothing is expanded at load time any more.)

``EditableConfig`` loads via plain ``yaml.safe_load`` — never via
``GatewayConfig.from_file`` — for the two structural reasons above. Note that
the raw document is what the editor WRITES; where a value came from is still
computed on demand, and ``merged_entry()`` below is what screens use when they
need the effective value to DISPLAY. Historically this also mattered for
a third reason: ``from_file()`` used to expand ``$VAR``/``${VAR}``
environment references, and loading through it would have written a
resolved secret in plain text on save. That's no longer a live concern —
``from_file()`` doesn't resolve ``$VAR`` at all anymore (docs/design/
config-tool.md decision 6, final revision: secrets live directly in
config.yaml, any ``.env``-backed config is auto-migrated on first use) — but
the plain-``yaml.safe_load``/plain-``yaml.dump`` round-trip stays exactly as
important for the two reasons that remain.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import yaml

from ..config import (
    TEMPLATE_FORBIDDEN_KEYS,
    GatewayConfig,
    _parse_templates_block,
    _resolve_inherits,
)
from ..config_validate import Finding, ValidationResult, validate_config

# kind -> the top-level *_templates: key it reads. Kind strings are plain
# ("agent"/"connector"/"watcher") — not "agent_defaults" etc. — since these are
# template kinds, not defaults-block kinds.
_TEMPLATES_KEY: dict[str, str] = {
    "connector": "connector_templates",
    "agent": "agent_templates",
    "watcher": "watcher_templates",
}

# The forbidden-key sets are the LOADER's rule, so they are imported rather than
# re-typed here.  This module used to hold its own copy, above a comment
# claiming unit tests kept the two in sync "by importing both from the same
# source, not by hand" — neither half of which was true: nothing imported
# anything, and no such test existed.  Aliased rather than replaced at the use
# sites so the local name keeps working.
_TEMPLATES_FORBIDDEN_KEYS = TEMPLATE_FORBIDDEN_KEYS


class Provenance(Enum):
    """Where one field's value on an entry actually comes from.

    Computed at the granularity the MERGE actually has, which for a nested
    field is per-sub-key: ``_deep_merge`` recurses whenever both sides hold a
    dict (``gateway/config.py``), so an entry setting ``rooms.direct`` over a
    template's ``rooms.include`` leaves the include INHERITED and makes only
    the flag EXPLICIT. Lists and scalars are still replaced wholesale, so
    ``rooms.include`` is binary — a rule's list does not extend a template's.

    This used to be computed per whole top-level field, justified as matching
    "``_deep_merge`` treating nested dicts as a single mergeable unit" — which
    was never true of the merge, and became visible once the nested sub-keys
    became individually editable and resettable: every field under a `rooms:`
    block an entry partly set read ``(explicit)``, including the sub-keys the
    template supplied, and a ctrl+r on one of them could not change that while
    any sibling remained.

    DEFAULT collapses two distinct cases into one enum member: the entry has
    no ``inherits:`` at all, and the entry has ``inherits:`` set but the
    named template doesn't set this particular field. Both fall through to
    the code-level dataclass default; they're distinguished at display time
    (the label composer takes the entry's template name, if any) rather than
    as separate enum values, since no caller needs to branch on which case it
    is — only on whether to mention a template name in the label.
    """

    EXPLICIT = "explicit"
    INHERITED = "inherited"
    EXPLICIT_SUPPRESSING = "explicit_suppressing"
    DEFAULT = "default"


@dataclass
class EditableConfig:
    """The raw config.yaml document, kept in its pre-merge, as-authored form.

    ``document`` is the literal top-level mapping from ``yaml.safe_load`` —
    keys like ``connectors``, ``agents``, ``watcher_rules``, ``connector_templates``,
    ``tool_presets``, etc. Phase 1 only reads it; later phases mutate it and
    call ``save()``.
    """

    document: dict
    path: Path
    # Code review item 8: templates() (and, transitively, merged_entry()/
    # field_provenance()) re-ran _parse_templates_block from scratch on
    # every single call — repaint_from_memory() alone calls it once per
    # connector/agent/watcher row PLUS once per templates-table row, all for
    # the same 3 blocks. Cached here, keyed by kind, invalidated by
    # load()/reload()/mark_dirty() (the only ways `document` changes). Not
    # part of equality/repr — it's a memoization detail, not observable state.
    _templates_cache: dict[str, dict] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    # Code review item 7: whether `document` has unsaved changes since the
    # last load()/reload()/save(). There is deliberately no per-field mutation
    # API here (e.g. `set_entry_field()`) — Phase 2's edit screens mutate
    # `document` (and the raw dicts reachable from it) directly, in whatever
    # shape each form needs, and then call `mark_dirty()`. That is the ONE
    # sanctioned seam: it is where cache invalidation and dirty-tracking both
    # live, so every future mutation path — a single field, a whole entry
    # replace, a list append/remove — stays correct by calling it, without
    # this class needing to anticipate each mutation shape up front.
    dirty: bool = field(default=False, init=False, compare=False)

    @classmethod
    def load(cls, path: str | Path) -> "EditableConfig":
        """Load config.yaml as a plain dict — no env-var expansion, no merge.

        Raises FileNotFoundError / ValueError the same way GatewayConfig.from_file
        does for a missing file or a non-mapping top level, so callers can
        handle both the same way they already handle from_file's errors.
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")
        with open(path) as f:
            document = yaml.safe_load(f) or {}
        if not isinstance(document, dict):
            raise ValueError(
                f"Config file '{path}' must contain a YAML mapping at the "
                f"top level, got {type(document).__name__}."
            )
        return cls(document=document, path=path)

    def reload(self) -> None:
        """Re-read `document` from disk in place (e.g. after the $EDITOR
        round-trip, or a manual 'refresh' action)."""
        fresh = EditableConfig.load(self.path)
        self.document = fresh.document
        self._templates_cache.clear()
        self.dirty = False

    def mark_dirty(self) -> None:
        """Call this after mutating `document` (or any raw dict reachable
        from it) directly. See the `dirty`/`_templates_cache` field comments
        above — this is the one required step after any in-place edit."""
        self._templates_cache.clear()
        self.dirty = True

    def save(self) -> None:
        """Validate-before-write via a same-directory temp file
        (docs/design/config-tool.md decision 5):

        1. Serialize `document` to `<path>.tmp`, BESIDE the real file (never
           /tmp — `working_directory`/`context_inject_files` in the config
           resolve relative to the real file's directory, and a temp file
           elsewhere would validate paths that don't mean the same thing
           once moved).
        2. Run the real `validate_config()` against that temp file. If it
           doesn't validate, compare its findings against `validate_config()`
           run on the CURRENT on-disk file — a pre-existing problem
           belonging to some OTHER, untouched entity does not block this
           save; only a genuinely NEW problem does (see
           `_new_errors_introduced_by_this_save()` below). User-reported:
           the previous all-or-nothing gate meant a config with two
           independently-broken connectors could never be fixed (or even
           deleted) through the TUI at all — saving a fix to connector1 was
           rejected because connector2 was still broken, and vice versa.
        3. Only on success: copy the real file to a timestamped backup under
           `<config_dir>/.config-backups/` (`config.yaml.bak.<unix-ts>`,
           matching gateway/onboard.py's own backup step, which writes to
           the same directory) and atomically replace it with the temp file
           (`os.replace` — atomic on POSIX, so a reader/the daemon never
           observes a partially-written config.yaml).

        `config.yaml` and every backup snapshot can hold a plaintext secret
        — secrets are stored directly in config.yaml (docs/design/
        config-tool.md decision 6 revisited; a not-yet-migrated `.env`
        reference gets folded in as a literal value by
        `gateway/config_migrate.py`, never the other direction). Both
        `config.yaml` itself and each backup file are chmod'd 0600 here
        (matching the treatment `.env` used to get) — `config.yaml`
        specifically needs this on EVERY save because writing `tmp_path` via
        plain `open(..., "w")` takes the process umask, not whatever
        permissions the real file had before; without this line, a manual
        `chmod 600 config.yaml` would silently revert to the umask default
        the very next time this method runs. The backup directory itself is
        chmod'd 0700 for the same reason a bare `.gitignore` entry on
        `config.yaml.bak.*` isn't enough by itself: it also keeps the whole
        deployment's history of past secrets out of a directory readers
        might casually `ls`/glob/back up without expecting a pile of
        `config.yaml.bak.*` files that will keep growing on every future
        save either.

        Raises FileNotFoundError if `path` doesn't exist yet (nothing to
        back up) — Phase 2/3 forms only ever call save() on an already-loaded
        EditableConfig, so this should not happen in practice; surfaced
        rather than silently skipping the backup step.
        """
        if not self.path.exists():
            raise FileNotFoundError(
                f"Cannot save: {self.path} no longer exists (nothing to back up)."
            )

        tmp_path = self.path.with_name(self.path.name + ".tmp")
        try:
            # 0600 from the first byte, created exclusively: the document holds
            # the secrets, and the chmod on `self.path` below would otherwise
            # leave them world-readable (under umask 022) from here until the
            # replace. A stale `.tmp` from an interrupted earlier save is ours
            # and is cleared first, or O_EXCL would refuse every save after it.
            with contextlib.suppress(FileNotFoundError):
                tmp_path.unlink()
            with open(os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
                yaml.dump(self.document, f, sort_keys=False, allow_unicode=True)

            after = validate_config(str(tmp_path))
            if not after.ok:
                new_errors = self._new_errors_introduced_by_this_save(after)
                if new_errors:
                    raise ValueError(
                        "Refusing to save — this change introduces a new "
                        "problem:\n" + "\n".join(f.message for f in new_errors)
                    )
                # else: every error in `after` already existed on disk before
                # this edit (or there are strictly fewer now) — allow the
                # save. The pre-existing problem(s) stay exactly as they
                # were; this save just doesn't ALSO fix them.

            backup_dir = self.path.parent / ".config-backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_dir.chmod(0o700)
            # Nanoseconds, not seconds: two saves in one second (a scripted
            # `coop config patch` twice) named the same backup, and the second
            # copy2 overwrote the first — the very snapshot it promised to keep.
            backup_path = backup_dir / f"{self.path.name}.bak.{time.time_ns()}"
            shutil.copy2(self.path, backup_path)
            backup_path.chmod(0o600)
            os.replace(tmp_path, self.path)
            self.path.chmod(0o600)
        finally:
            # Only ever removes OUR OWN temp file, not the real config: if
            # os.replace() above succeeded, tmp_path no longer exists at this
            # path (it WAS renamed to self.path) and unlink is a no-op.
            tmp_path.unlink(missing_ok=True)

        self.dirty = False

    def _new_errors_introduced_by_this_save(self, after: ValidationResult) -> list[Finding]:
        """Which of `after`'s errors are genuinely NEW — not present when
        `validate_config()` runs against the CURRENT on-disk file (i.e. the
        state right before this save). Compares structured
        `(entity_kind, entity_name, message)` tuples, not raw strings and
        not "same entity therefore ignore" — an error only counts as
        "pre-existing, therefore ignorable" if the EXACT same problem
        already existed on disk; a different, new problem on an
        already-broken entity still blocks the save. `self.path` reflects
        whatever was last successfully saved (or originally loaded) —
        nothing else is expected to touch it mid-session.

        Known, accepted limitation (PR review): `entity_name` is the raw
        `name` field, so RENAMING an entity (e.g. a connector's `name:`)
        that ALSO has an unrelated pre-existing problem makes that problem
        look "new" under the new name and blocks the save — the old and new
        names never match as the same tuple key. Safe (never silently loses
        data — the save is refused, not silently allowed with the old
        problem intact under a new label) but can be surprising: the fix is
        to resolve the pre-existing problem in the SAME save as the rename,
        or rename in a separate save before/after fixing it. Not fixed here:
        doing so would need a stable per-entity identity that survives a
        rename (e.g. list position for connectors — but agents are
        inherently name-keyed in `agents:`, where a rename IS
        indistinguishable from delete+create), which is a bigger design
        question than this fix's scope."""
        before = validate_config(str(self.path))
        before_keys = {
            (f.entity_kind, f.entity_name, f.message)
            for f in before.findings
            if f.severity == "error"
        }
        return [
            f
            for f in after.findings
            if f.severity == "error" and (f.entity_kind, f.entity_name, f.message) not in before_keys
        ]

    # ── Raw entry accessors (pre-merge, as-authored) ─────────────────────────

    @property
    def connectors_raw(self) -> list[dict]:
        return [c for c in (self.document.get("connectors") or []) if isinstance(c, dict)]

    @property
    def agents_raw(self) -> dict[str, dict]:
        agents = self.document.get("agents") or {}
        return {k: v for k, v in agents.items() if isinstance(v, dict)}

    @property
    def watcher_entries(self) -> list:
        """The raw `watcher_rules:` document list ITSELF — the same object living
        in `document` when it is a list (so callers can mutate it), [] when
        it is anything else. Codex review of #129: `document.get("watcher_rules")
        or []` scattered across callers let a malformed scalar through —
        `watcher_rules: 5` crashed the overview repaint with a TypeError
        mid-iteration, and a string produced one bogus row per character.
        EditableConfig.load() deliberately accepts structurally invalid
        documents (that's how the TUI can display their validation errors),
        so the list-ness check has to live on every read — stated once,
        here."""
        watchers = self.document.get("watcher_rules")
        return watchers if isinstance(watchers, list) else []

    @property
    def watchers_raw(self) -> list[dict]:
        return [w for w in self.watcher_entries if isinstance(w, dict)]

    @property
    def tool_presets_raw(self) -> dict[str, list]:
        return dict(self.document.get("tool_presets") or {})

    def templates(self, kind: str) -> dict[str, dict]:
        """Return the parsed `<kind>_templates:` block — name -> field dict,
        `description` stripped, same validation the real loader applies
        (mapping-ness, no forbidden identity keys, no nested `inherits:`) —
        'connector' | 'agent' | 'watcher'. Cached per kind (see
        `_templates_cache`); the underlying `document` never changes except
        via load()/reload()/mark_dirty(), all of which invalidate the cache.
        Reuses the real `_parse_templates_block` verbatim — never
        reimplemented."""
        if kind not in self._templates_cache:
            self._templates_cache[kind] = _parse_templates_block(
                self.document, _TEMPLATES_KEY[kind], _TEMPLATES_FORBIDDEN_KEYS[kind]
            )
        return self._templates_cache[kind]

    def raw_template(self, kind: str, name: str) -> dict | None:
        """The named template's RAW entry straight from `document` — unlike
        `templates()` above, `description` is NOT stripped here.

        PR review finding: `TemplateDetailScreen` used to be constructed
        with `templates(kind).get(name)` (the stripped dict) as its OWN
        `self.entry`, then `action_save()` did `target_entry = dict(self.entry)`
        + updates and wrote THAT wholesale over `document[...][name]` —
        silently deleting any on-disk `description` on every save of an
        existing template, even a no-op edit, since the stripped dict never
        had it to begin with. `TemplateDetailScreen` needs its own source of
        truth to carry `description` through the round-trip (exactly like
        `DefaultsScreen`, this screen's predecessor, read directly from the
        raw block instead of a merged/stripped view) — this accessor is
        that source of truth, used by `OverviewScreen` at both of its
        TemplateDetailScreen call sites for view/edit (create mode has no
        existing raw entry to read, so it isn't needed there)."""
        block = self.document.get(_TEMPLATES_KEY[kind]) or {}
        entry = block.get(name)
        return entry if isinstance(entry, dict) else None

    def entry_template_name(self, entry_raw: dict) -> str | None:
        """The entry's own `inherits:` value, or None if unset. A tiny
        accessor so callers building display labels (provenance text, the
        inherits-picker's current selection) don't reach into raw dicts
        directly."""
        name = entry_raw.get("inherits")
        return name if isinstance(name, str) and name else None

    # ── Provenance / effective value (reuses the real merge, never reimplemented) ──

    def merged_entry(self, kind: str, entry_raw: dict) -> dict:
        """`entry_raw` resolved against its own `inherits:` template (if
        set) — the exact value GatewayConfig.from_file would compute for
        this one entry before its own further per-entry processing (path
        resolution, tool-preset resolution, etc). Uses the real
        `_resolve_inherits` (which pops `inherits` and deep-merges the named
        template with the entry, the entry winning on conflict) — never
        reimplemented. `entity_kind`/`entity_label` are cosmetic (only used
        in `_resolve_inherits`'s own ValueError messages); every caller here
        already wraps this in `try/except (ValueError, FileNotFoundError)`,
        so generic placeholders are fine.

        Deliberately NOT cached (unlike templates() above): the merge is
        cheap (a couple of dict copies), and caching it would need a key
        derived from entry_raw's identity, which stops being safe the moment
        a later phase starts mutating entries in place for editing.
        templates() is document-scoped and only invalidated by
        load()/reload()/mark_dirty(), a much simpler invariant to keep
        correct."""
        return _resolve_inherits(
            entry_raw, self.templates(kind), _TEMPLATES_KEY[kind], "entry", "?"
        )

    def field_provenance(self, kind: str, entry_raw: dict, field: str) -> Provenance:
        """Where `entry_raw[field]` (or its absence) actually comes from.

        kind: 'connector' | 'agent' | 'watcher' (selects which `*_templates:`
        block the entry's own `inherits:` name is looked up against).

        `field` may be a one-level-nested dotted key (`rooms.direct`,
        `server.team`, `permissions.timeout`), answered per sub-key because
        that is how `_deep_merge` merges it — see `Provenance`. Decided by
        MEMBERSHIP, not by reading the value: a sub-key present with the value
        `null` suppresses the template's, and a sub-key that is simply absent
        inherits it, and both read as `None`.
        """
        template_name = self.entry_template_name(entry_raw)
        template = self.templates(kind).get(template_name, {}) if template_name else {}
        if "." not in field:
            if field in entry_raw:
                if entry_raw[field] is None and field in template:
                    return Provenance.EXPLICIT_SUPPRESSING
                return Provenance.EXPLICIT
            if field in template:
                return Provenance.INHERITED
            return Provenance.DEFAULT

        parent_key, sub_key = field.split(".", 1)
        entry_parent = entry_raw.get(parent_key)
        template_parent = template.get(parent_key)
        # `isinstance` on both sides: a hand-edited config (or template) can
        # hold anything here, and this runs while rendering it.
        template_has = isinstance(template_parent, dict) and sub_key in template_parent

        if parent_key in entry_raw and not isinstance(entry_parent, dict):
            # The entry states a non-dict for the whole block — `null`, or a
            # malformed list. `_deep_merge` only recurses when BOTH sides are
            # dicts, so this replaces the template's block verbatim and every
            # sub-key under it is the entry's doing.
            if entry_parent is None and template_has:
                return Provenance.EXPLICIT_SUPPRESSING
            # An explicit `null` over a block the template never set suppresses
            # nothing, but the entry did write it — reported EXPLICIT, matching
            # how a top-level `null` with no template value is reported.
            return Provenance.EXPLICIT

        if isinstance(entry_parent, dict) and sub_key in entry_parent:
            if entry_parent[sub_key] is None and template_has:
                return Provenance.EXPLICIT_SUPPRESSING
            return Provenance.EXPLICIT
        return Provenance.INHERITED if template_has else Provenance.DEFAULT

    # ── Read-only validated view ─────────────────────────────────────────────

    def validated_view(self) -> GatewayConfig:
        """The fully-parsed, merged GatewayConfig — for display and cross-
        reference only (e.g. "this watcher's agent is X"). Loads via the
        real gateway loader; never mutate anything based on what this
        returns — only `document` is ever written back to disk."""
        return GatewayConfig.from_file(self.path)

    # ── Watcher rules (design §5.5's Rules tab) ──────────────────────────────
    #
    # A `watcher_rules:` entry is a RULE (name + connector + agent + a `rooms:`
    # matcher), not a room — which rooms it claims is only known at runtime,
    # so there is nothing to "expand" here any more. The Watcher Rules tab shows one
    # row per raw entry, keyed by LIST INDEX: order within `watcher_rules:` is
    # load-bearing (first match wins, design §2.1), which is also why rules
    # are displayed in document order and why reordering is a first-class
    # mutation (`move_watcher_rule()` below) rather than an $EDITOR-only
    # operation. Add/edit/delete need no primitives of their own: a rule is
    # one entry in one list, so RuleDetailScreen uses the same
    # trial-entry/install/rollback pattern ConnectorDetailScreen already
    # uses on `document["connectors"]`.

    def move_watcher_rule(self, index: int, delta: int) -> int | None:
        """Swap the rule at `index` with its neighbour `delta` (+1/-1) away.

        Returns the rule's new index, or None if the move is a no-op
        (index out of range, already at the edge it would move past, or
        `watcher_rules:` is not a list at all — see `watcher_entries`).
        Calls `mark_dirty()` on an actual move; callers own `save()` —
        same contract as every other document mutation here."""
        watchers = self.watcher_entries
        target = index + delta
        if not (0 <= index < len(watchers)) or not (0 <= target < len(watchers)):
            return None
        watchers[index], watchers[target] = watchers[target], watchers[index]
        self.mark_dirty()
        return target


class StatusIndex:
    """Groups a ValidationResult's structured `findings` by (entity_kind,
    entity_name) for cheap per-row lookup in the TUI's tables.
    """

    _SEVERITY_RANK = {"error": 3, "warning": 2, "lint": 1}

    def __init__(self, findings: list[Finding]):
        self._by_entity: dict[tuple[str, str], list[Finding]] = {}
        for f in findings:
            if f.entity_name is not None:
                self._by_entity.setdefault((f.entity_kind, f.entity_name), []).append(f)

    def findings_for(self, kind: str, name: str) -> list[Finding]:
        return self._by_entity.get((kind, name), [])

    def status_for(self, kind: str, name: str) -> str:
        """'error' | 'warning' | 'lint' | 'ok', highest severity present."""
        items = self.findings_for(kind, name)
        if not items:
            return "ok"
        return max(items, key=lambda f: self._SEVERITY_RANK.get(f.severity, 0)).severity

    # ── Rule rows (index-keyed) ──────────────────────────────────────────────
    #
    # Rules tab rows are keyed by LIST INDEX (order is load-bearing), but a
    # rule's findings are filed under THREE different entity_name spellings,
    # one per producer:
    #   - the rule's own `name` (shadowing/DM-overlap warnings, and
    #     collect_config() errors on a NAMED entry)
    #   - `(index {i})` (collect_config() errors on an entry with no usable
    #     `name` — gateway/config.py's watcher loop)
    #   - `watchers[{i}]` (_lint_config()'s placeholder for the same case)
    # A row lookup that consulted only the name is exactly how the previous
    # Watchers tab displayed OK on broken rows (a key mismatch, not a
    # missing finding) — so the bridge is stated once, here, and every rule
    # row goes through it.

    @staticmethod
    def _rule_keys(index: int, raw_entry: dict) -> list[str]:
        keys: list[str] = []
        name = raw_entry.get("name")
        if isinstance(name, str) and name:
            keys.append(name)
            # The parser CANONICALIZES the name (strips whitespace) before
            # attributing findings to it — an externally-authored
            # `name: " padded "` would otherwise show OK on its row while
            # the banner reports its warning (Codex review of #129, round 3;
            # same canonicalization the stranded-count lookup applies).
            stripped = name.strip()
            if stripped and stripped != name:
                keys.append(stripped)
        keys.append(f"(index {index})")
        keys.append(f"watchers[{index}]")
        return keys

    def findings_for_rule(self, index: int, raw_entry: dict) -> list[Finding]:
        found: list[Finding] = []
        for key in self._rule_keys(index, raw_entry):
            found.extend(self.findings_for("watcher", key))
        return found

    def status_for_rule(self, index: int, raw_entry: dict) -> str:
        """'error' | 'warning' | 'lint' | 'ok', highest severity present
        across every entity_name spelling this rule's findings can carry."""
        items = self.findings_for_rule(index, raw_entry)
        if not items:
            return "ok"
        return max(items, key=lambda f: self._SEVERITY_RANK.get(f.severity, 0)).severity
