---
name: coop-apply-config
description: How every configuration change is planned, locked, written and applied — dry run, plan.lock, --if-digest write, reload dry-run guard, reload/start/stop. Use for any change to config.yaml, including single-step ones (a room list, a timeout), and as the last step of coop-add-bot and coop-remove-bot.
---

# coop-apply-config

One configuration write per step. Nothing is written before the operator's yes.
A write applies only to the file it was planned against.

## 1. Compose the fragment

Put everything the step changes in `config.yaml` into one YAML fragment and
write it under this directory, `./.plans/<step-name>.yaml` (create `.plans/`
with `mkdir -p` if absent). The fragment follows JSON merge-patch:

- a mapping merges into a mapping; `null` deletes a key; anything else replaces
  the value — a list is replaced **wholesale**, so a list you change carries its
  complete new contents;
- `connectors:` and `watcher_rules:` entries are merged **by `name`**: a new
  name is appended, an existing one merged into, `op: add` refuses an existing
  name, `op: remove` deletes the entry and only it. `op` never reaches the file;
- `agents:` and the template blocks are mappings keyed by name, so
  `agents: {bob: null}` removes an agent;
- a credential is written as `{from_file: <path>}` under a `password`, `token`
  or `secret` key and nowhere else. The command reads the file. Never put a
  value, and never the masked value `***`, in a fragment.

For a single value, `--set`/`--unset` with `--entry connector:<name>` or
`--entry rule:<name>` do the same job without a file; the steps below are the
same.

## 2. Dry run — the plan's configuration section

```
coop config patch --file ./.plans/<step>.yaml --dry-run --json
```

Read the result:

- `ok: false` → the merged file does not validate. The `findings` say why. Fix
  the fragment or report the problem; do not ask for confirmation of a plan
  that cannot be written. One exception: a plan that creates an agent's
  directory as its first step (`coop-add-bot`) dry-runs before that directory
  exists, so "working directory does not exist" and the rule that follows
  from it are expected there — and the dry run is repeated, and must be
  clean, once the directory exists and before the write.
- `file_digest` → keep it; the write needs it.
- `config` → the whole merged, masked document. This is the exact final state:
  show the relevant part of it in the plan, not a paraphrase.

A `config.yaml` that does not exist yet dry-runs against the empty deployment
(`file_digest` of no bytes) and is created `0600` by the write.

## 3. Ask once

Print the plan — every account, room, persona, configuration and runtime step
— and ask for a yes. On no, delete the fragment; nothing else exists.

A yes covers the plan that was shown, nothing else. If the fragment has to
change after the yes — a field was misread, the dry run was not clean — the
yes is void: show the corrected plan and ask again. Never ask for a yes on a
plan whose dry run failed.

**A plan with more than one write** (a removal's two steps, the second-team
connector and its rule) is confirmed once, on all its fragments together. Its
first write is planned and written like any single write. Each later fragment
is dry-run before the yes too, and may report **only** the findings the
earlier write removes — the rule that still names a connector about to go,
the rule that names a connector about to be created — which the plan says;
anything else is a real problem. After the earlier write, the later fragment
takes its own clean dry run and writes with the `file_digest` **that dry run
returns**, which must equal the `file_digest` the plan's own previous write
returned — the same file, changed only by this plan. A different digest
means someone else wrote in between: stop and re-plan from step 2.

## 4. Take the lock — first thing after the yes

The lock covers the **whole** plan, not only the configuration write: take it
before the plan's first step (a directory, an account, a room join) and
release it after the last (an account deletion, a directory removal), so two
keepers cannot both create server state and then have one lose the digest
race. Other skills say "take the lock" at their step 1 and "release" at their
end; this is what they mean.

```
mkdir ~/.agentcoop/agents/plan.lock
```

`mkdir` is atomic: it fails when the directory exists, which means another
plan holds the lock. When it fails, read `~/.agentcoop/agents/plan.lock/holder`
**again, now** — never reuse an earlier read; the holder may have changed or
expired since. If its `expires` is in the past (compare with
`date -u +%Y-%m-%dT%H:%M:%SZ`), remove the directory with
`rm -r ~/.agentcoop/agents/plan.lock` and take it again; otherwise tell the
operator who holds it and until when, and stop.

Once taken, write `~/.agentcoop/agents/plan.lock/holder`:

```text
plan: <one line describing this plan>
started: <now, UTC ISO-8601>
expires: <now + 10 minutes>
```

The lock guards the deployment, so it lives under `~/.agentcoop/agents/`, not
in this directory — a keeper copied under `user/` takes the same lock. Only
keepers honour it; the config TUI and hand edits are caught by the digest.

## 5. Write

```
coop config patch --file ./.plans/<step>.yaml --if-digest <file_digest> --json
```

- `ok: true` → written; one timestamped backup was taken under
  `.config-backups/`.
- Refused on the digest → the file changed since the dry run (the TUI, a hand
  edit, another keeper). Nothing was written. Release the lock, re-run the dry
  run, show the operator what is different, and ask again.
- Refused on validation → cannot happen after a clean dry run unless the file
  changed; treat as the digest case.

## 6. Guard

```
coop config reload --dry-run --json
```

This is the gateway's own account of what the write will do — connectors
restarted, records expired, sessions kept. With the gateway stopped it is the
plan the next `start` executes. Compare it with the plan you showed. If it
names anything the plan did not — a connector restart the operator was not told
about, a record expiring on another bot — **stop here**, show the difference,
and leave the operator to decide. The file is already written; say so.

## 7. Apply

Exactly one of, as the plan said:

- gateway running → `coop config reload`
- gateway stopped, deployment has rules → `coop start`
- the plan ends with an empty deployment → `coop config reload` (it stops the
  last connector and expires its records), then `coop stop`, and say the
  deployment is empty; `coop start` would refuse it.

`coop status` says which state the gateway is in.

## 8. Release and clean up

```
rm -r ~/.agentcoop/agents/plan.lock
rm ./.plans/<step>.yaml
```

Then report what was done, step by step, in the words of the plan.

## When a step fails

Stop at the failing step. Release the lock and report **before** you
investigate anything: say exactly which steps completed and which did not,
quote the error, name any kept file, and undo nothing. Do not start a repair
— not even a "reversible" one — until the operator has seen the report and
confirmed a repair step. A written configuration
stays written; a created account stays created; a kept password file is named
with its path. Repair is a new conversation of individually confirmed steps,
using the same commands.

## Persona-only changes

A change to an agent's `AGENTS.md` under `~/.agentcoop/agents/user/<agent>/`
is a plan of its own with no configuration write: show the new text, ask, write
the file. Then offer `coop reset '<connector>:*'` for each connector of the
agent (from `coop config show --json`: connectors named by rules whose `agent`
is this agent) as an optional step: bots on either backend read the rewritten
file on their next turn without it, and the plan says so; the reset gives a
bot a clean session instead of a mid-conversation change of voice. An agent whose
working directory is outside `agents/user/` is not yours to edit: say where the
file is and leave it to the operator. A working directory shared by several
agents is a shared thing: name every agent the edit reaches.
