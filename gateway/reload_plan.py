"""The plan `config reload` prints, in one shape for every route to it (#144).

A plan is what an operator reads before (dry run), during (the apply prints
the plan it is about to execute) and after (the same plan, with what
actually happened to it) a reload — and what `--json` hands to a script or
an onboarding agent. Three producers fill it:

* the daemon, from its running fleet and the candidate file;
* the CLI with the daemon stopped (`--dry-run` only), from the state files
  and the file — labelled as the plan the **next boot** will execute, because
  the record-level engine here IS boot's (`gateway.core.reconcile`), so the
  offline plan and the boot are one computation, not two;
* the apply, which adds degraded sections and marks the plan applied.

The record-level half is pure and shared: `plan_connector_records` runs the
reconciliation engine and folds in what a reload adds on top — a resident
watcher restarted because its connector or agent restarts.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Collection, Iterable, Literal

from .config import GatewayConfig
from .config_diff import ConfigDiff, EntityChanges, config_digest
from .core.reconcile import orphan_decisions, reconcile_records
from .core.state import WatcherState, load_state
from .core.watcher_rule import WatcherRule

WatcherAction = Literal["restart", "rematerialize", "expire"]

# The reason a connector restart carries for its records, and the note the
# plan shows for it: the rooms a scope change drops cannot be listed before
# the connector reconnects (a `Connector.probe_rooms` would be a new contract
# method — out of scope, see the issue), so the plan says an expiry may follow.
SCOPE_REVALIDATION_NOTE = (
    "connector '{connector}' restarts: its {count} record(s) are re-validated "
    "against the connector's scope after it reconnects — a room the connector "
    "no longer serves is expired then, with its session id logged"
)


@dataclass(frozen=True)
class WatcherChange:
    """One record's fate under the reload."""

    connector: str
    room_id: str
    handle: str
    agent: str
    action: WatcherAction
    from_rule: str = ""
    to_rule: str = ""
    session_id: str = ""
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Degraded:
    """A section the apply could not bring back."""

    kind: Literal["connector", "agent"]
    name: str
    error: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ReloadPlan:
    """What a reload does — planned, applied, or refused — in one shape."""

    dry_run: bool
    # Computed with no daemon, from the state files: the next boot's plan. The
    # connector/agent restart section does not apply — a boot restarts all.
    offline: bool = False
    ok: bool = True
    error: str = ""
    # Validation findings as `config validate --json` renders them: warnings
    # when the file is valid, the errors when it is not.
    findings: list[dict] = field(default_factory=list)
    connectors: EntityChanges = field(default_factory=EntityChanges)
    agents: EntityChanges = field(default_factory=EntityChanges)
    rules: EntityChanges = field(default_factory=EntityChanges)
    rules_reordered: bool = False
    values: list[dict] = field(default_factory=list)
    watchers: list[WatcherChange] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    degraded: list[Degraded] = field(default_factory=list)
    applied: bool = False
    digest: str = ""

    @property
    def has_changes(self) -> bool:
        return bool(self.connectors or self.agents or self.rules or self.rules_reordered
                    or self.values or self.watchers)

    @property
    def exit_code(self) -> int:
        """0 applied cleanly or nothing to do; 1 refused or invalid; 2 degraded."""
        if not self.ok:
            return 1
        if self.degraded:
            return 2
        return 0

    def of(self, action: WatcherAction) -> list[WatcherChange]:
        """The watcher changes of one kind."""
        return [w for w in self.watchers if w.action == action]

    @classmethod
    def refused(cls, error: str, *, dry_run: bool, findings: Iterable[dict] = ()) -> "ReloadPlan":
        """A plan that never got to planning: invalid file, lock held, wrong path."""
        return cls(dry_run=dry_run, ok=False, error=error, findings=list(findings))

    def take_diff(self, diff: ConfigDiff) -> None:
        """Copy the entity-level changes out of a `ConfigDiff`."""
        self.connectors = diff.connectors
        self.agents = diff.agents
        self.rules = diff.rules
        self.rules_reordered = diff.rules_reordered
        self.values = [{"path": v.path, "old": v.old, "new": v.new} for v in diff.values]

    def to_dict(self) -> dict:
        """The `--json` document; `from_dict` is its inverse."""
        return {
            "ok": self.ok,
            "dry_run": self.dry_run,
            "offline": self.offline,
            "applied": self.applied,
            "exit_code": self.exit_code,
            "error": self.error,
            "digest": self.digest,
            "validation": {"findings": list(self.findings)},
            "changes": {
                "connectors": self.connectors.to_dict(),
                "agents": self.agents.to_dict(),
                "rules": {**self.rules.to_dict(), "reordered": self.rules_reordered},
                "values": list(self.values),
            },
            "watchers": [w.to_dict() for w in self.watchers],
            "notes": list(self.notes),
            "degraded": [d.to_dict() for d in self.degraded],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ReloadPlan":
        """The daemon's response, back into a plan the CLI can render."""
        changes = data.get("changes") or {}

        def entity(key: str) -> EntityChanges:
            block = changes.get(key) or {}
            return EntityChanges(added=list(block.get("added", [])),
                                 changed=list(block.get("changed", [])),
                                 removed=list(block.get("removed", [])))

        plan = cls(
            dry_run=bool(data.get("dry_run")),
            offline=bool(data.get("offline")),
            ok=bool(data.get("ok", True)),
            error=str(data.get("error") or ""),
            findings=list((data.get("validation") or {}).get("findings", [])),
            connectors=entity("connectors"),
            agents=entity("agents"),
            rules=entity("rules"),
            rules_reordered=bool((changes.get("rules") or {}).get("reordered")),
            values=list(changes.get("values", [])),
            watchers=[WatcherChange(**w) for w in data.get("watchers", [])],
            notes=list(data.get("notes", [])),
            degraded=[Degraded(**d) for d in data.get("degraded", [])],
            applied=bool(data.get("applied")),
            digest=str(data.get("digest") or ""),
        )
        return plan

    # ── Human rendering ──────────────────────────────────────────────────────

    def render(self) -> str:
        """The four blocks an operator reads: validation, entity changes,
        watcher actions, degraded sections — then one line saying what this
        plan is (a dry run, the next boot's plan, applied, refused)."""
        lines: list[str] = []
        # Severity tags lead every line an operator must not skim past (owner,
        # 2026-09-05): a degraded section is a failed reload, and an untagged
        # line in a block of otherwise routine output is easy to miss.
        if not self.ok:
            lines.append(f"[ERROR] {self.error}")
            for f in self.findings:
                tag = "[ERROR]" if f.get("level") == "error" else "[WARNING]"
                lines.append(f"  {tag} {f.get('message', '')}")
            return "\n".join(lines)

        warnings = [f for f in self.findings if f.get("level") == "warning"]
        if warnings:
            lines.append(f"Validation: {len(warnings)} warning(s)")
            for f in warnings:
                lines.append(f"  [WARNING] {f.get('message', '')}")

        if not self.has_changes:
            lines.append("No changes — the running configuration already matches the file."
                         if not self.offline else
                         "No changes — the next start will keep every record as it is.")
            return "\n".join(lines)

        lines.append("Changes:")
        lines.extend(self._entity_lines("connectors", self.connectors,
                                        changed_verb="restart" if not self.offline else "changed"))
        lines.extend(self._entity_lines("agents", self.agents,
                                        changed_verb="restart" if not self.offline else "changed"))
        lines.extend(self._entity_lines("rules", self.rules, changed_verb="changed"))
        if self.rules_reordered:
            lines.append("  rules: reordered (first match wins — records are re-matched)")
        for v in self.values:
            lines.append(f"  {v['path']}: {v['old']} → {v['new']}")
        if self.offline:
            lines.append("  (connector and agent restarts do not apply — a start restarts all)")

        if self.watchers:
            lines.append("Watchers:")
            for w in self.watchers:
                lines.append("  " + self._watcher_line(w))
        for note in self.notes:
            lines.append(f"Note: {note}")
        if self.degraded:
            lines.append("Degraded:")
            for d in self.degraded:
                lines.append(f"  [ERROR] {d.kind} '{d.name}': {d.error}")

        # Two levels, both named: a degraded connector that comes back has no
        # resident watcher to list, so "0 restart" alone read as "nothing
        # happened" to an operator who had just watched it reconnect.
        sections = []
        if self.connectors.changed:
            sections.append(f"{len(self.connectors.changed)} connector(s) restarted")
        if self.agents.changed:
            sections.append(f"{len(self.agents.changed)} agent(s) restarted")
        counts = "; ".join(sections + [
            f"watchers: {len(self.of('restart'))} restart, "
            f"{len(self.of('rematerialize'))} re-materialize, "
            f"{len(self.of('expire'))} expire"])
        if self.offline:
            lines.append(f"Record-level plan the next start executes ({counts}); nothing changed.")
        elif self.dry_run:
            lines.append(f"Dry run ({counts}); nothing changed.")
        elif self.applied and self.degraded:
            lines.append(f"[ERROR] Applied with {len(self.degraded)} degraded section(s) ({counts}) — "
                         f"fix the file and reload again.")
        elif self.applied:
            lines.append(f"Applied ({counts}).")
        else:
            lines.append(f"Applying ({counts})…")
        return "\n".join(lines)

    @staticmethod
    def _entity_lines(kind: str, changes: EntityChanges, *, changed_verb: str) -> list[str]:
        out = []
        for name in changes.added:
            out.append(f"  {kind}: + {name} (added)")
        for name in changes.changed:
            out.append(f"  {kind}: ~ {name} ({changed_verb})")
        for name in changes.removed:
            out.append(f"  {kind}: - {name} (removed)")
        return out

    @staticmethod
    def _watcher_line(w: WatcherChange) -> str:
        if w.action == "rematerialize":
            detail = f"rematerialize {w.from_rule or '?'} → {w.to_rule}"
        elif w.action == "expire":
            detail = f"expire {w.reason}"
        else:
            detail = "restart" + (f" ({w.reason})" if w.reason else "")
        session = f"  session={w.session_id}" if w.session_id and w.action == "expire" else ""
        return f"{w.handle:<40} {detail}{session}"


# ── Record-level planning ──────────────────────────────────────────────────────


def plan_connector_records(
    connector: str,
    records: Iterable[WatcherState],
    rules: list[WatcherRule],
    *,
    resident: Collection[str] = (),
    restart_all: bool = False,
    restarted_agents: Collection[str] = (),
) -> list[WatcherChange]:
    """What the reload does to one connector's records.

    The reconciliation engine decides re-materialize and expire; a reload adds
    `restart` for a **resident** record (a running processor, by room id)
    whose connector restarts as a whole or whose agent restarts. A
    record that is re-materialized is restarted too if resident — the
    re-materialization line already implies it, so it is not listed twice. A
    record no processor holds is not "restarted": its next wake reads
    whatever the reload wrote.
    """
    records = list(records)
    by_room = {r.room_id: r for r in records}
    rules_by_name = {r.name: r for r in rules}
    plan = reconcile_records(records, rules, connector=connector)
    out: list[WatcherChange] = []
    resident = set(resident)
    restarted_agents = set(restarted_agents)
    for action in plan.actions:
        record = by_room[action.room_id]
        common = dict(connector=connector, room_id=action.room_id,
                      handle=action.watcher_name, agent=action.agent,
                      session_id=action.session_id, from_rule=action.from_rule,
                      to_rule=action.to_rule)
        if action.action == "expire":
            out.append(WatcherChange(action="expire", reason=action.reason, **common))
        elif action.action == "rematerialize":
            # The agent the record will run on AFTER the apply — what a script
            # reading the plan wants to know; an expiry keeps the agent it had.
            common["agent"] = rules_by_name[action.to_rule].agent
            out.append(WatcherChange(action="rematerialize", **common))
        elif action.room_id in resident and (
                restart_all or record.agent in restarted_agents):
            out.append(WatcherChange(
                action="restart",
                reason=("connector restarts" if restart_all else f"agent '{record.agent}' restarts"),
                **common))
    return out


def static_era_changes(connector: str, records: Iterable[WatcherState]) -> list[WatcherChange]:
    """The records hydration prunes — neither `rule_name` nor `config` — as the
    expiries they are. Boot and an added connector's `settle_records` both
    release them, so both plans list them."""
    return [
        WatcherChange(connector=connector, room_id=r.room_id, handle=r.watcher_name,
                      agent=r.agent, action="expire", session_id=r.session_id,
                      reason="static-era record pruned at boot")
        for r in records if not (r.rule_name or r.config)
    ]


def connector_removed_changes(connector: str, records: Iterable[WatcherState]) -> list[WatcherChange]:
    """Every record of a connector the candidate no longer names expires."""
    return [
        WatcherChange(
            connector=connector, room_id=r.room_id, handle=r.watcher_name, agent=r.agent,
            action="expire", from_rule=r.rule_name, session_id=r.session_id,
            reason="connector-removed",
        )
        for r in records
    ]


def orphan_removals(
    configured: Iterable[str], *, skip: Collection[str] = (),
) -> tuple[list[str], list[WatcherChange], list[str]]:
    """The connectors whose state files boot's orphan sweep removes, the
    expiries that sweep releases, and a note for each file it KEEPS — one loop
    for the daemon's plan and the offline one, deciding with the sweep's own
    function so the plan never advertises a removal the sweep declines (a
    file with a record it could not parse stays, for repair by hand). `skip`
    names connectors planned elsewhere (a live entry being removed plans from
    its in-memory records, not its file)."""
    names: list[str] = []
    changes: list[WatcherChange] = []
    notes: list[str] = []
    for decision in orphan_decisions(configured):
        if decision.connector in skip:
            continue
        if decision.keep_reason:
            notes.append(
                f"state file {decision.path.name} (connector '{decision.connector}', no "
                f"longer configured) is KEPT because {decision.keep_reason} — fix or delete "
                f"it by hand; its {len(decision.records)} readable record(s) are not released")
            continue
        names.append(decision.connector)
        changes.extend(connector_removed_changes(decision.connector, decision.records))
    return names, changes, notes


def plan_persisted_records(connector: str, config: GatewayConfig) -> list[WatcherChange]:
    """What settling a connector's state FILE does: the prune of static-era
    records, then the reconciliation. Boot for every connector; a reload for a
    connector it adds — a fresh manager hydrates the file the same way."""
    records = load_state(connector)
    return (static_era_changes(connector, records)
            + plan_connector_records(connector, records, config.rules_for(connector)))


def boot_plan(config: GatewayConfig) -> ReloadPlan:
    """The record-level plan the next start executes, from the state files.

    What the CLI prints for `config reload --dry-run` when no daemon is
    running: the rule reconciliation and the orphan sweep are the same code
    boot runs, over the same files boot reads, and static-era records (the
    prune) are listed too. Connector and agent restarts are not planned (a
    start restarts everything), and residency is unknown, so no `restart`
    lines appear. Not modelled, because they need the runtime: a room the
    connector no longer serves (#141, needs the connection) and a file the
    sweep keeps because a record in it did not parse (`load_state` skips it
    silently). Raises what `load_state` raises on a file this build cannot
    read — boot would refuse on it too.
    """
    plan = ReloadPlan(dry_run=True, offline=True, digest=config_digest(config))
    configured = [c.name for c in config.connectors]
    for name in configured:
        plan.watchers.extend(plan_persisted_records(name, config))
    names, changes, notes = orphan_removals(configured)
    plan.connectors.removed.extend(names)
    plan.watchers.extend(changes)
    plan.notes.extend(notes)
    return plan
