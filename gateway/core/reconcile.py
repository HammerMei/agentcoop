"""Reconcile persisted watcher records against the current rules (design §2.4).

A record freezes the rule it was created from. Between reconciliations that
freeze is authoritative — a rule edit does not reach a running watcher — and
at each reconciliation (boot, and `config reload`) every record is re-matched
against the current ordered rule list:

* the same rule still wins and its snapshot is unchanged → **keep**;
* a rule wins whose name or content differs from what the record froze →
  **rematerialize**: the frozen field group is rewritten from the winning
  rule, the session and the lifecycle clocks are left alone;
* no rule wins → **expire**: the record is reclaimed, the session id logged.

Ownership drift and content drift are one check here, not two: re-running
first-match over the current list catches a rule inserted *above* mine (no
content changed anywhere) and a rule whose body changed alike; the snapshot
digest only decides whether a same-named winner is a change at all.

Pure: this module reads records and rules and returns a plan. Applying it —
rewriting fields, reclaiming records, saving — is the session manager's, so
`config reload --dry-run` can render the same plan without acting on it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal

from .state import (
    CONFIG_SCHEMA_VERSION,
    WatcherState,
    connector_name_of,
    load_state,
    room_kind_or_channel,
    state_files,
)
from .watcher_manager import (
    RoomRef,
    first_matching_rule,
    materialize,
    rule_bound_fields,
    rule_snapshot,
    snapshot_digest,
)
from .watcher_rule import RoomKind, WatcherRule

Action = Literal["keep", "rematerialize", "expire"]
ExpireReason = Literal["no-rule-matches", "connector-removed"]


@dataclass(frozen=True)
class RecordAction:
    """One record's fate under the current rules."""

    room_id: str
    watcher_name: str
    agent: str
    session_id: str
    action: Action
    from_rule: str = ""
    to_rule: str = ""
    reason: str = ""


@dataclass
class ReconcilePlan:
    """What one connector's records should become under the current rules.

    `connector`, and each action's `agent` and `session_id`, are not read by
    boot — they are the fields a rendered plan (`config reload --dry-run`,
    #144) shows an operator, carried so the renderer does not re-derive them.
    """

    connector: str
    actions: list[RecordAction] = field(default_factory=list)

    def of(self, action: Action) -> list[RecordAction]:
        """The actions of one kind."""
        return [a for a in self.actions if a.action == action]

    @property
    def changes(self) -> list[RecordAction]:
        """Every action that is not a keep — what boot applies and logs."""
        return [a for a in self.actions if a.action != "keep"]

    @staticmethod
    def format_counts(kept: int, rematerialized: int, expired: int) -> str:
        """The one rendering of a reconciliation's counts, planned or applied."""
        return f"{kept} kept, {rematerialized} re-materialized, {expired} expired"

    def summary(self) -> str:
        """One line of the planned counts, for the log."""
        return self.format_counts(
            len(self.of("keep")), len(self.of("rematerialize")), len(self.of("expire")))


def room_ref_of(record: WatcherState) -> RoomRef:
    """The room a record describes, shaped the way the rule matcher wants it."""
    return RoomRef(
        id=record.room_id,
        kind=room_kind_or_channel(record),
        name=record.room_name,
        participants=tuple(record.participants),
    )


def rematerialized_fields(record: WatcherState, rule: WatcherRule) -> dict[str, Any]:
    """The frozen fields a re-materialization rewrites, from the winning rule.

    `materialize` and `rule_bound_fields` are the creation path's own, so the
    two writers cannot drift. `room_kind`, `participants` and `created_at` are
    frozen too but describe the room and the birth, not the rule, and stay;
    session-scoped fields and lifecycle clocks are never touched — same room,
    same session, same idle clock. Connector and agent are the rule's: the
    rule's connector IS this manager's (the rules were filtered to it) and
    wins over the record's column — a state file copied under a renamed
    connector still carries the old name, and `config_from_record` prefers
    the column, so a stale one would key prompt and attachment state under a
    connector that no longer exists.
    """
    wc = materialize(rule, room_ref_of(record))
    return rule_bound_fields(
        wc, rule,
        connector_name=rule.connector,
        agent_name=rule.agent,
    )


_KNOWN_KINDS = frozenset(k.value for k in RoomKind)


def unmatchable_reason(record: WatcherState) -> str | None:
    """Why this record cannot be re-matched honestly — or None if it can.

    The one place that decides it — a new condition belongs here, not in
    another `if` at a call site. A record that fails is **kept** with the
    reason: the runtime degrades these cases to
    something workable (an unknown or missing kind reads as CHANNEL, a wake
    resolves a nameless room through the connector), which is fine for a
    path that cannot destroy anything and wrong for one that expires.

    * no `room_kind` recorded — the runtime's CHANNEL fallback would judge a
      DM as a channel;
    * a `room_kind` this build does not know — same;
    * a named room with no name — the matcher deliberately does not fall
      back to the opaque id.
    """
    if not record.room_kind:
        return "no room kind recorded — cannot re-match, left as it is"
    if record.room_kind not in _KNOWN_KINDS:
        return (f"room kind {record.room_kind!r} is not one this build knows — "
                f"cannot re-match, left as it is")
    if (RoomKind(record.room_kind) not in (RoomKind.DM, RoomKind.GROUP_DM)
            and not record.room_name):
        return "no room name recorded — cannot re-match, left as it is"
    return None


def orphaned_state_files(configured: Iterable[str]) -> list[tuple[Path, str]]:
    """State files on disk whose connector `config.yaml` no longer names.

    Enumerates files, as `config validate`'s orphan check does, so a renamed
    connector is found by its old file and not by config. Each is
    `(path, connector name)`; the caller decides what to do with the records.
    """
    known = set(configured)
    return [(path, connector_name_of(path)) for path in state_files()
            if connector_name_of(path) not in known]


@dataclass(frozen=True)
class OrphanDecision:
    """What the orphan sweep does with one state file no connector owns.

    `keep_reason` is None when the sweep removes the file (releasing every
    record with an AUDIT line); otherwise the file stays for repair by hand
    and the reason says why. `records` are the ones `load_state` could read.
    """

    path: Path
    connector: str
    records: list[WatcherState]
    keep_reason: str | None = None

    @property
    def swept(self) -> bool:
        return self.keep_reason is None


def orphan_decisions(configured: Iterable[str]) -> list[OrphanDecision]:
    """Decide, for every orphaned state file, whether the sweep removes it.

    THE decision — boot executes it (`GatewayService._reclaim_orphaned_state_files`),
    `config reload` executes it again, and `config validate` predicts it: the
    session-uniqueness check ignores the files this says will go, because
    their duplicate ids are released before boot's own uniqueness check runs
    (#143 ordered the sweep first). Validate deciding on its own would drift
    from boot exactly where the rename-and-copy layout is concerned.

    A file is kept, not swept, when its records could not all be read:
    deleting it would delete a record's only session reference without a
    line saying so. Raises what `load_state` raises on a file this build
    cannot read — a format refusal, which boot's preflight makes first.
    """
    return [orphan_decision_for(path, name) for path, name in orphaned_state_files(configured)]


def orphan_decision_for(path: Path, name: str) -> OrphanDecision:
    """The sweep's decision for ONE orphaned file — so a caller that already
    walks the files one by one (`config validate`, guarding each load) does
    not have to load every other orphan to learn about this one."""
    records = load_state(name)
    try:
        raw = json.loads(path.read_text()).get("watchers")
    except (OSError, ValueError, AttributeError):
        raw = None
    if not isinstance(raw, list):
        reason = "its records could not be read at all"
    elif len(raw) != len(records):
        reason = (f"{len(raw) - len(records)} of its {len(raw)} record(s) could not be "
                  f"parsed and would be lost without a trace")
    else:
        reason = None
    return OrphanDecision(path, name, records, reason)


def reconcile_records(
    records: Iterable[WatcherState],
    rules: list[WatcherRule],
    *,
    connector: str,
) -> ReconcilePlan:
    """Plan what the current rules say about each rule-derived record.

    `rules` is the connector's ordered list (a rule names an agent that exists
    — config loading refuses one that does not — so no agent check is needed
    here). Static-era records (neither `rule_name` nor `config`) are not this
    module's — boot prunes them before reconciling — and are skipped. A record
    that cannot be re-matched honestly (`unmatchable_reason`) is kept as it
    is, with the reason: "nothing matches" is destructive here.
    """
    plan = ReconcilePlan(connector=connector)
    for record in records:
        if not (record.rule_name or record.config):
            continue
        common = dict(
            room_id=record.room_id,
            watcher_name=record.watcher_name,
            agent=record.agent,
            session_id=record.session_id,
            from_rule=record.rule_name,
        )
        unmatchable = unmatchable_reason(record)
        if unmatchable:
            plan.actions.append(RecordAction(
                action="keep", reason=unmatchable, to_rule=record.rule_name, **common))
            continue
        winner = first_matching_rule(rules, connector, room_ref_of(record))
        if winner is None:
            plan.actions.append(RecordAction(
                action="expire", reason="no-rule-matches", **common))
            continue
        # A snapshot that is not a dict cannot be compared — a damaged record
        # reads as "changed" and is rewritten, never a crash at boot.
        frozen = record.rule if isinstance(record.rule, dict) else None
        unchanged = (
            record.rule_name == winner.name
            and frozen is not None
            and snapshot_digest(frozen) == snapshot_digest(rule_snapshot(winner))
            # The per-record schema marker exists for exactly this: a build
            # that changed what a config field means rewrites every record
            # once, not only the ones whose rule happened to change too.
            and record.config_schema_version == CONFIG_SCHEMA_VERSION
        )
        plan.actions.append(RecordAction(
            action="keep" if unchanged else "rematerialize",
            to_rule=winner.name, **common))
    return plan
