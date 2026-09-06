# Config Reload

`agent-chat-gateway config reload` applies changes in `config.yaml` to the
running daemon: it validates the whole file, diffs it against the active
configuration, restarts the affected connectors and agents, and runs the same
record reconciliation boot runs — re-materializing records against the current
rules (keeping their sessions) and expiring records no rule covers (session id
logged). This document records the design; the dynamic-watcher design's §2.4
owns the reconciliation engine it reuses.

## 1. Motivation

Before this, every change to `config.yaml` — a new rule, a changed agent
working directory, a rotated connector token — required `restart`, which
stops every connector, backend and watcher, including the ones the edit did
not touch. A daemon serving several rooms went dark for the full restart
window because one rule changed.

Two smaller gaps came with it. Nothing said what the daemon was actually
running: `status` showed a pid and uptime, `config validate` checked the file
on disk, and an edit that was never applied looked identical to one that was.
And none of the config commands produced machine-readable output, which blocks
letting an onboarding agent operate the gateway.

## 2. Design

### 2.1 Command surface

```
config reload [--dry-run] [--json]
config show   [--json]
config validate [--json]        # --json is new
status                          # gains the active digest, load time, degraded sections
```

No `--yes`, no interactive confirmation. The dry run is the preview; execution
is explicit. **There is deliberately no binding between a dry run and a later
execution** — execution reads the file as it is at that moment, and the plan
printed before the apply is the operator's last look. Nothing watches the
file, no signal handler exists, and saving in the config TUI does not trigger
a reload.

The reasoning for dropping the confirmation step: the only sessions that can
be lost between a dry run and an execution are ones created in that window,
the platform keeps every message in the room, and history handoff re-feeds
recent context on the next session.

### 2.2 Control protocol

The control socket stays strictly **one request → one response → close**. Two
commands are added:

- `config-reload {dry_run, config_path}` — the daemon reads its own file
  once, validates, plans, and (unless `dry_run`) applies within the same
  request; the response is the plan document (§2.7). `config_path` must be
  the path the daemon was started from — a different one is refused rather
  than silently reloading another file.
- `config-show {include_config}` — the active digest, load time, degraded
  sections and (with `include_config`) the redacted resolved config. `status`
  is this command with `include_config: false`.

### 2.3 Offline dry run

When the daemon is not running, the CLI computes the record-level plan itself
from the state files and the file (`reload_plan.boot_plan`), labelled as the
plan the next start will execute, with the connector/agent restart section
marked not applicable. This is the same engine boot uses over the same files
boot reads — including files of connectors the config no longer names, which
boot sweeps — so the offline plan and the boot are one computation.

Without `--dry-run` and with no daemon, the plan is still printed (it is what
`start` will do) and the command exits 1: nothing is running to apply it to.

When the daemon **appears** to be running but its socket cannot be reached,
the command errors out. It never falls back to the offline plan: a daemon in
that state is the thing to fix first.

### 2.4 Validation first, active config, diff

The whole-file validator (`validate_config`) runs on the candidate. Any error
returns the findings and changes nothing; warnings are carried into the plan.
The env-to-config migration and the config-permission hardening are boot-only.
`ValidationResult` now carries the parsed config when it is clean, so the
daemon validates and diffs from one read.

One check in the validator had to learn boot's order to be usable here. Boot
sweeps orphaned state files *before* its session-uniqueness check (#143), so
a renamed connector whose state was copied to the new name boots — the old
file's duplicate ids are released, not refused. The validator's uniqueness
check used to scan every file and refuse that layout. It now leaves out the
files boot's sweep will remove, deciding which with the sweep's own function
(`core.reconcile.orphan_decisions`, executed by boot and by the reload apply,
predicted by validate) — and keeps refusing a layout boot refuses, an orphan
the sweep keeps because a record in it did not parse. Validate writes
nothing; it predicts.

The service **retains the resolved configuration it started with** and
replaces it after a successful apply, together with a SHA-256 digest of its
canonical serialization and the time it was loaded. Diffing
(`gateway/config_diff.py`) compares parsed dataclasses, never YAML text:
comments, key order and `description:` (which every entity parser drops)
register as nothing; a template edit registers as a change to every entry
inheriting it, because inheritance is flattened at parse time.

Entity identity is `name` for connectors, agents and rules. **A rename is a
removal plus an addition.** For rules that is harmless — ownership is
recomputed by re-matching, and the record re-materializes to the new name.
For a connector it expires every record under the old name and deletes its
state file; the dry run shows this.

Every field of every config entity is classified once, in `RELOAD_ACTIONS`,
and `tests/unit/test_config_diff.py` enumerates the dataclasses against it:
a new field cannot arrive without saying what a reload does about it.

A rule-only reload connects nothing, so the identity barrier would never run
— yet a rule can change what a kept connector claims of its account's direct
messages, and two Mattermost connectors on one bot account, safe while their
teams kept them apart, could both opt into `direct:`. The plan step therefore
runs the barrier's own check over the kept connectors with the candidate's
rule-derived claims and **refuses** the reload before anything is touched.
An entry the runtime holds that neither the active nor the candidate config
names — a failed apply's leftover once the file was put back — is removed like
any other. The config path is kept absolute but unresolved, so a repointed
`config.yaml` symlink is followed by the next reload.

### 2.5 Diff → action table

| Change | Action |
|---|---|
| Any connector field | Restart that connector as a unit: shut its manager down (drain processors, save, disconnect), build a new connector and manager from the candidate, settle, connect, sync. Its records are re-validated against the connector's scope on reconnect (#141), so a scope change expires out-of-scope rooms then — the plan says so; it cannot list them in advance. |
| Connector removed | Shut down; every record expires through the shared release method (`connector-removed`); state file deleted by the same orphan sweep boot runs. |
| Connector added | Build, connect, sync. A state file already carrying its name is hydrated and reconciled. |
| Any agent field | Stop the backend (an OpenCode sidecar restarts) and its broker, rebuild both, restart every resident processor on that agent. A changed backend identity (type or working directory) is settled by the existing provisioning check: a fresh session, the old id on the AUDIT line. |
| Agent removed | Stop and drop it. Config loading refuses a rule naming an unknown agent, so its records have already re-materialized or expired. |
| Any change in the rules block, including a reorder | Reconcile every record of every kept connector (§2.4 of the dynamic-watcher design). A re-materialized record with a resident processor is restarted; on an eager connector the new rules' literal rooms are started. |
| `max_queue_depth`, `scheduler.*` | Replace the value in place. A processor's queue is sized when it is built, so `max_queue_depth` reaches watchers started from then on; the plan says so. |
| `description` on any entity | Nothing — it is not a field. |

### 2.6 Apply order

The apply reuses the boot and shutdown orders — one stop pass, one start pass,
never one pass per change:

0. **Construct before destroying.** Every new backend and connector is built
   first; a factory that raises refuses the whole reload with nothing
   touched.
1. **Pause the scheduler** (as shutdown does). A job due in the window fires
   on the restart's catch-up pass; nothing is cancelled, and the scheduler
   never observes a half-swapped mapping. This is how "a degraded
   connector keeps its scheduled jobs" is met, with no new scheduler state.
2. **Quiesce every kept manager**: no new wake, join or operator verb — each
   refused with "a config reload is in progress" — and in-flight ones are
   waited out; the sweep is stopped. A bot-membership removal that arrives
   meanwhile is queued and handled when the manager re-arms (the sweep's
   reconciliation covers paused and idle records only; an active watcher
   removed from its room mid-reload would otherwise keep its session and
   jobs). Processors keep running: an in-flight agent turn finishes on its
   own, and a processor the reload restarts is drained by its own stop.
3. **Stop pass**, in shutdown order. A removed connector's records go
   through the shared reclamation tail first, while its manager and the
   backends are still alive (backend session deleted where the backend can,
   prompt file and attachment workspace removed, jobs cancelled, one AUDIT
   line each — what boot's orphan sweep cannot do for a file whose connector
   is gone). Then the going managers shut down; then the processors of every
   changed *or removed* agent are drained, concurrently, while their backends
   are still alive; then those backends and brokers stop. **Every stop is
   retried** a few times, a few seconds apart (`core.retry_stop`), and that
   is the whole recovery. What still will not stop is a **leftover**: kept
   tracked (disconnected or stopped once more at the next reload and at
   shutdown — never saved again, its records belong to the replacement), named
   by `status`, reported on the plan as a degraded finding that tells the
   operator to look at the process now. The replacement proceeds regardless.
   A permission broker that will not stop is that agent's failure, exactly
   like its backend. Then the orphaned state files are swept.
4. **Rebuild**: the one shared agents dict and the core config are updated in
   place (every lifecycle holds them by reference); new entries replace old
   ones in candidate order, in the same list and dict the control server and
   scheduler hold; kept managers take the candidate's rules.
5. **Start pass**, in boot order: agents first. Then the kept managers
   **re-arm** (the lifecycle's disarm was a one-way shutdown flag until
   this; `rearm_transitions` is the inverse) and reconcile against the new
   rules — *before* the new connectors, because the identity barrier folds
   each connector's persisted DM records into its claim, and a DM record a
   deleted rule left behind must be gone by then (boot's own order: settle,
   connect, identity). A reconciliation that raises degrades that entry.
   Then the new connectors settle their records and connect concurrently
   (as boot's phases do — slow logins must not add up inside one request),
   pass the identity barrier one at a time, and sync concurrently; a failure
   leaves a **degraded** entry, never a half-started one.

   **One room down is reported, never swallowed.** Every per-watcher failure
   on the apply path — a re-materialized processor that would not restart, a
   room of a changed agent that would not start, an eager room a new rule
   names, a new connector's own start errors — becomes a degraded finding on
   the plan (exit 2) naming the remedy, without marking the connector
   degraded: its other rooms are answering, and a degraded entry refuses
   every verb. `list` shows the room failed; `resume`, or its next message,
   brings it back. Rooms of an agent that did not come up are not reported
   again one by one — the agent's own finding covers them.
6. Kept managers start what step 3 stopped — wherever each record now
   points, since the reconciliation may have moved it to an agent that did
   not change — and every *was-active* record of a changed agent, including
   rooms an earlier reload left down because the agent did not come up then
   (not twice for a record the reconciliation already restarted; an idle
   room stays idle). Then the scheduler, the active config, the digest.

The processors of a changing agent are stopped in step 3 **before** their
backend, while it is still alive: a processor's stop drains its queue by
processing it, and against a stopped sidecar every drained message would
fail into the room. The kept lifecycles are also told which agents came up
(`set_blocked_agents`) — boot writes that set once; a reload rewrites it.

**Nothing refuses after construction, and nothing is rolled back** (owner,
2026-09-05). A rollback can fail too, and even one that succeeds has not
reloaded the config; another way of killing a process that would not stop
forks the behaviour without knowing why it was stuck. So when something
unexpected happens the apply stops treating it as its problem: it logs, marks
the section degraded, tells the operator which connector, agent or watcher
may no longer be working and that action is needed now, and exits 2 — a
reload with a degraded section is a failed reload, whatever else applied. The
same rule holds for a section that fails to *start* at reload.

If the apply itself raises part-way (a defect — nothing in it raises by
design), the daemon is left consistent rather than half-swapped: the kept
managers are re-armed, the scheduler restarted, every connector the candidate
names — and every one the apply was tearing down — has an entry (the ones it
lost, marked degraded with the error), and the **previous** config stays
active. Kept managers may hold half-applied rules or a half-swapped core
config, and nothing short of a restart says which — so they are marked
degraded too, and the next reload, even of the file put back, restarts them
whole; `config show` says the file is not applied, and that reload re-diffs
everything, replaces the leftover entries (an existing entry under an *added*
name is torn down first) and retries what did not land.

`shutdown()` takes the reload lock after closing the control socket: a
reload already applying finishes first, because its stop and start passes
touch the very entries the teardown is about to visit.

A room offer that arrives on a kept connector during the window **parks**
rather than declines (`WatcherManager._park_if_reloading` raises a retryable
error): a declined offer remembers the buffered ids, which is right for
shutdown and wrong for a kept connector whose dedup set survives the reload —
the next wake's replay would die on them. The connector retries for a few
seconds and, if the reload is still applying, leaves the ids unknown so the
replay recovers the frames.

The control server refuses `pause`/`resume`/`reset`/`expire` for the whole
apply window — not only against the restarting connector: it serves clients
concurrently, the window is short, and "which connector is mid-restart" is a
moving target the operator cannot see. `list`, `send`, `status` and
`schedule-*` are not refused. A second `config reload` during an apply is
refused by the apply lock.

### 2.7 Failure semantics and output

Apply is per section. A connector that fails to connect or sync, an agent
whose backend fails to start, or a new connector caught by the identity
barrier (boot fails fast there; a reload cannot take the running connectors
down for it, so the barrier runs once per new connector against the fleet
accepted so far, and the one whose *addition* makes the conflict is refused
— the others still come up) is left as a **degraded** entry:
present in the mapping, no processors, visible in `status`, no automatic
retry. The operator fixes what was wrong and reloads again — and **every
reload retries the degraded sections**, whether or not their own entry
changed, because the fix is often not in the file (a server reachable again,
a sidecar binary put back); the plan notes the retry. Degraded is a
reload-only state; boot remains fail-fast for connectors.

Human output has four blocks: validation warnings (`[WARNING] …`);
entity-level added/changed/removed per connector, agent and rule (plus
"reordered" and value swaps); one line per affected watcher — `restart (why)`,
`rematerialize <from> → <to>`, `expire <reason>  session=<full id>`; degraded
sections, each line `[ERROR] <kind> '<name>': <what and what to do>`. The
final line says what the plan is: a dry run, the next start's plan, applied,
`[ERROR] Applied with N degraded section(s)`, or `[ERROR] <refusal>`. Two
rules of voice (owner, 2026-09-05): a line an operator must not skim past
starts with its severity tag; and the output describes the current state
only — a degraded section that this reload brought back is not mentioned,
because there is nothing left to act on.

Exit codes: **0** applied cleanly or nothing to do, **1** validation failure
or refusal, **2** any section degraded.

`--json` returns the same information as a document: `ok`, `dry_run`,
`offline`, `applied`, `exit_code`, `error`, `digest`, `validation.findings`
(the `config validate --json` finding shape: `level`, `entity_kind`,
`entity_name`, `field`, `message`), `changes` (per entity kind: `added`,
`changed`, `removed`; rules add `reordered`; `values` as `path`/`old`/`new`),
`watchers` (`connector`, `room_id`, `handle`, `agent`, `action`,
`from_rule`, `to_rule`, `session_id`, `reason`), `notes`, `degraded`
(`kind`, `name`, `error`). The CLI renders the daemon's document; nothing is
computed twice.

### 2.8 Digest and `config show`

The digest is SHA-256 over a canonical serialization (sorted keys) of the
**resolved** config — templates and inheritance expanded, connectors keyed
by name and room patterns in their canonical spelling (`RoomPattern.canonical`,
the form `==` compares) so that whatever the diff calls unchanged the digest
does too (rule order stays significant in both); a date or other non-JSON
scalar in a connector's open `raw` block serializes type-tagged, so
`build_date: 2026-09-05` and `build_date: "2026-09-05"` — different dicts to
the diff — are different to the digest too — so semantically identical files
hash identically and comments never matter. The offline dry run carries the
file's validation warnings like the online one. It is over the
unredacted values: a rotated secret changes it, which is the point of a
fingerprint. `config show` prints it with a flattened dump (connectors keyed
by name so two machines' dumps line up) in which values under a key naming a
`password`, `token` or `secret` (case-insensitive substring, at any depth) are
`***`. With the daemon running it also fetches the active digest and warns
when the file differs — "I edited but forgot to reload" made visible.

## 3. What it does and does not guarantee

- Message loss during the apply window is accepted: on a restarted connector
  it is equivalent to that connector restarting; on a kept connector a room
  whose watcher is being created or woken during the window parks and is
  recovered by the next wake's replay, while its resident rooms keep
  answering. The requirements promise graceful handling of transient
  connector failures and no delivery guarantee.
- A reload cannot be interrupted once the request is sent; the plan printed
  before the apply is the last look.
- `restart` and `reload` converge on the same runtime state, because both run
  the same reconciliation over the same records and the same orphan sweep.
- The exact rooms a scope-changed connector will drop are not listed in the
  dry run — that needs a new connector contract method (follow-up).

## 4. Out of scope

`reset-watcher-config <handle>` (a per-watcher reload); triggering reload from
the config TUI, a file watcher or SIGHUP; automatic retry or rollback of a
degraded section; rename detection heuristics; pattern-matching batch
`reset`/`pause`/`resume`/`expire`.

## 5. Testing

Primary seam: `ControlServer.dispatch_command` on a real `GatewayService`
booted from a config file with Script connectors and mock agents
(`tests/integration/test_config_reload.py`, harness
`tests/helpers.boot_gateway_service`). Scenarios cover the action table
through dry runs and applies: re-materialization with the session kept, expiry
with the id logged, an agent rebuilt with its processors restarted, a changed
working directory starting a fresh session with the old id on the AUDIT line,
a connector added / removed / restarted, a degraded connector with exit code 2
and `status`, a scheduled job surviving its connector's restart, concurrent
reload refused, verbs refused during the apply, no-change reload, path
mismatch, invalid file.

Per-field enumeration (`tests/unit/test_config_diff.py`): every field of
`ConnectorConfig`, `AgentConfig`, `WatcherRule`, `GatewayConfig` and
`SchedulerConfig` has a row in `RELOAD_ACTIONS` and a change to it is seen by
the diff. Secondary seam, the CLI entry point (`tests/integration/test_cli.py`):
offline dry run from state files, execute refused without a daemon, error
when the daemon has no reachable socket, `config validate --json`,
`config show` digest and redaction, the `status` digest line.
