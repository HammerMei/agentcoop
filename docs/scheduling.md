# Built-in Task Scheduler

## Overview

The scheduler lets AgentCoop proactively trigger tasks on a fixed cadence or at a specific time — without waiting for a human to send a message. You can use it to:

- Have the AI remind you of something in 5 minutes
- Run a daily standup prompt every weekday morning
- Send a weekly summary every Friday afternoon
- Fire a one-shot task at a precise datetime, then forget it

Jobs are owned by a watcher. When a job fires, AgentCoop injects a message directly into that watcher's agent session — bypassing the normal self-message filter — and the agent responds as if a user sent the message.

The scheduler polls every 60 seconds, so jobs fire within one minute of their scheduled time.

---

## Teaching the Agent to Schedule

No configuration is required. AgentCoop automatically injects scheduling context into every agent session at startup via `contexts/scheduling-context.md`. The agent already knows how to run `coop schedule create` commands.

**Example interaction:**

> **You:** Remind me to review the deployment logs in 15 minutes.
>
> **Agent:** Sure! I'll set a reminder for 15 minutes from now.
> *(runs: `coop schedule create rc:general "Reminder: review the deployment logs" --every 15m --times 1`)*
>
> **Agent:** Done — you'll get a reminder in 15 minutes. Job ID: `acg-3a7f1c90`.

The agent creates the job itself using the CLI. From your side, all you do is ask in natural language.

---

## Creating Scheduled Tasks

### Basic syntax

```bash
coop schedule create WATCHER MESSAGE [OPTIONS]
```

`WATCHER` is the name of the watcher (chat room binding) that will receive the injected message.
`MESSAGE` is the prompt that gets sent to the agent when the job fires.

**Write `MESSAGE` as an instruction to the agent, not as the text you want to
see.** The message is delivered into the agent's own session (headed
`from: scheduler | … | to: me`) and is never shown in the room; **the agent's
reply is what gets posted**. `"Post one computer part of the day with a
one-line fact"` produces a post every run; `"🖥️ Computer part: CPU cooler"`
gives the agent nothing to add, and an agent with nothing to add answers with
its silence token — the job then fires on schedule and posts nothing. The
daemon logs a WARNING naming the room when that happens.

### Options

| Option | Description |
|---|---|
| `--every INTERVAL` | Recurring interval: any `Nm` (1–59 minutes), any `Nh` (1–23 hours), `1d`, `1w` |
| `--starting TIME` | Time anchor / start time. With `--every`: sets the first run and (for `1d`/`1w`) pins the cron time-of-day. Without `--every`: one-shot specific datetime. Accepts smart partial inputs: `"09:00"`, `"Apr 15 09:00"`, `"04-15 09:00"`, `"Mon 09:00"`, `"2026-05-01 09:00"`. |
| `--times N` | Max number of runs. `0` means run forever (default). `1` means run once then mark completed. |
| `--tz TIMEZONE` | IANA timezone, e.g. `"America/New_York"`, `"Europe/Berlin"`, `"UTC"`. The `--starting` time is interpreted in this timezone; if omitted, a `--starting` time is read in the gateway's local timezone. The connector's `timezone` setting applies only to schedules with no `--starting`. Only relevant for daily/weekly schedules — omit for sub-hourly intervals. |

### Smart date inference for `--starting`

You do **not** need to type full dates in most cases:

| Input | Meaning |
|---|---|
| `"09:00"` | Today at 09:00; auto-advances to tomorrow if already past |
| `"Apr 15 09:00"` | This year, April 15 at 09:00; advances one year if past |
| `"04-15 09:00"` | This year, April 15 at 09:00 (MM-DD format) |
| `"Mon 09:00"` | Next Monday at 09:00 |
| `"2026-05-01 09:00"` | Explicit full datetime (for cross-year scheduling) |

If the resolved time is already in the past, AgentCoop prints a warning and automatically advances to the next sensible occurrence (tomorrow for `HH:MM`, next year for `MM-DD` or `Mon DD`, etc.).

### Examples

```bash
# One-shot reminder in 5 minutes
coop schedule create rc:general "Reminder: check the oven" --every 5m --times 1

# Daily standup at 09:00 every day
coop schedule create rc:general "Run the daily standup" --every 1d --starting "09:00" --tz "Asia/Taipei"

# Weekly report every Friday — infer "this Friday" automatically
coop schedule create rc:ops "Generate weekly ops summary" --every 1w --starting "Fri 17:00" --tz "America/New_York"

# One-shot at a specific datetime
coop schedule create rc:general "Review Q2 roadmap" --starting "2026-04-10 15:30" --tz "Asia/Taipei"

# Health check every 30 minutes, forever
coop schedule create rc:ops "Check server health and report status" --every 30m

# Run exactly 3 times, every hour
coop schedule create rc:general "Hourly check-in" --every 1h --times 3

# Start firing every minute, 5 times, beginning at 14:00
coop schedule create rc:general "Pulse check" --every 1m --times 5 --starting "14:00"
```

---

## Common Use Cases

### Relative reminder ("in N minutes")

Use `--every` with `--times 1`. This fires once after the interval and then marks the job completed.

```bash
coop schedule create rc:general "Reminder: stand up and stretch" --every 15m --times 1
```

Or ask the agent directly:
> "Remind me to call back the client in 30 minutes."

### Daily recurring task at a fixed time

```bash
coop schedule create rc:general "Good morning! Summarize yesterday's GitHub activity." \
  --every 1d --starting "09:00" --tz "Asia/Taipei"
```

### Weekly report

```bash
coop schedule create rc:ops "Generate weekly infrastructure cost report and post summary." \
  --every 1w --starting "Fri 16:00" --tz "America/New_York"
```

### One-shot at a specific future datetime

```bash
coop schedule create rc:general "It's launch day — post the release announcement." \
  --starting "2026-04-15 10:00" --tz "Europe/Berlin"
```

Once the job fires, it is automatically marked `completed` and will not run again.

---

## Headless Scheduling (No Chat Platform)

Every example above targets a watcher bound to a real chat connector — the
agent's reply actually posts to a Rocket.Chat/Mattermost room. If you want a
scheduled job that runs an agent purely for its own sake (a background
check, a periodic task whose effect is a file/API side effect rather than a
chat message) with **no chat platform account needed at all**, bind the
watcher to a `script` connector (`type: script`) instead of a real one.

`script` is an in-process connector with no network I/O — see
`docs/architecture.md`'s "Testing and Scripting" section for its other,
unrelated use (calling it directly from your own Python code, bypassing
`config.yaml` entirely). Declaring one in `config.yaml` instead gives the
scheduler a named, no-platform identity to inject into:

```yaml
connectors:
  - name: headless
    type: script

agents:
  worker:
    type: claude
    working_directory: /path/to/project

watcher_rules:
  - name: cron
    connector: headless
    agent: worker
    rooms:
      include: [cron]   # never a real room — the script connector has nowhere to post
```

```bash
coop schedule create headless:cron "Check disk usage and log anything over 80%." --every 1h
```

The watcher is `headless:cron` — connector name, colon, room — not the rule
name; `schedule create` refuses a rule name.

The job fires exactly like any other — `SessionManager.inject_message()`
runs the agent turn the same way regardless of connector type — but the
reply is just queued in the script connector's in-memory buffer and never
delivered anywhere. This only makes sense when you don't need the output
visible in a chat room (e.g. the agent's own tool calls write to a file, a
log, or an API as a side effect); if you want the result posted somewhere
a human sees it, bind the watcher to a real connector instead.

---

## Listing and Managing Jobs

### List all jobs

```bash
coop schedule list
```

Output:

```
ID              WATCHER               STATUS      CRON              RUNS          NEXT RUN (UTC)          MESSAGE
acg-bb47e7f4    rc:general            active      0 9 * * 1-5       3/∞           2026-04-10 09:00:00     Run daily standup
acg-2f6cb289    rc:ops                paused      */30 * * * *      12/∞          2026-04-10 09:30:00     Check server health
acg-b8c2a409    rc:general            completed   * * * * *         1/1           done 2026-04-09 07:23   提醒：去刷牙
```

- **RUNS** shows `run_count / max_runs` (∞ means no limit).
- **NEXT RUN** shows `done <timestamp>` for completed jobs and `cancelled <timestamp>` for cancelled ones. A paused job still shows the time it would have fired; it is not consulted while paused.

### Filter by connector

```bash
coop schedule list --connector rc-home
```

### Show all jobs including completed

```bash
coop schedule list --all
```

### Pause a job

```bash
coop schedule pause acg-bb47e7f4
```

Paused jobs do not fire until resumed. The `NEXT RUN` column keeps the time the job would have fired; it is not consulted while paused.

A job does not keep its watcher's room from being reclaimed, and does not need
to. A job that **records the room it targets** resolves that room on its next run
and recreates the watcher through the same path a message would — a 9am job on a
reclaimed room brings its watcher back at 9am. Pause still outranks a schedule: a
paused watcher is not woken by a job.

Two conditions, and a job that misses either one cannot bring its watcher back —
it fails at every slot instead, logging each time:

- **it has to have a room id.** Jobs created before that field existed do not,
  and `schedule migrate` is what records it. Until then such a job resolves its
  watcher by name, which works only while a live record answers to that name —
  so if the room's record is reclaimed first, the job stops delivering for good.

  In practice that needs an **infrequent** job: a fire counts as activity, so a
  job running more often than `session_idle_days` (15 by default) keeps its own
  watcher's record alive, and the record has to sit idle for the full idle leg
  and then the expiry leg — about a month from last activity — before anything
  reclaims it. So the exposure is a job whose interval exceeds that, in a room
  with no other traffic, during the window before you migrate. That is the
  once-a-quarter or once-a-year job, which is the same case that makes
  `schedule migrate` a command you run rather than something done lazily at fire
  time. Run it after upgrading and the window never opens.
- **its connector has to be able to look a room up by id.** All four shipped
  connectors can (`Connector.room_ref_by_id`); for voice and script a room's id
  is its name, so the lookup is the identity. A connector that cannot would be
  named here — `tests/unit/test_job_room_identity.py` walks every supported type
  and fails unless it either overrides the lookup or is declared as unable.

### Resume a paused job

```bash
coop schedule resume acg-bb47e7f4
```

### After upgrading: `schedule migrate`

```bash
coop schedule migrate
```

Each job records the room it targets, so it keeps working when the room is
renamed or its watcher's record is reclaimed. Jobs created before that field
existed do not have it: they still work, by resolving their watcher's name, but
they lose the job if that name moves. This records the room id for them.

**Run it before renaming any rooms.** The migration finds each job's room
*through* its watcher name, so a name that has already moved to a different room
would point the job at the wrong one. Right after an upgrade is the moment when
the names still mean what they meant.

Safe to re-run, and it never guesses: a job whose room cannot be identified is
reported and left exactly as it was, so you can fix the cause and run it again.
A group DM's watcher name contains a digest of its room id rather than a name —
nothing can resolve that, so those jobs are named in the output and have to be
deleted and recreated.

The daemon warns at startup while there is anything to migrate, and the warning
stays until the last job is resolved — the schema version deliberately does not
move while any job still needs attention, which is what keeps `schedule migrate`
worth running again. So a run that reports some jobs changed and others needing
attention says `jobs.json is STILL at schema version 1`, not that it migrated:
the jobs it fixed keep their room ids, and the ones it could not are unchanged.

### Delete a job

```bash
coop schedule delete acg-bb47e7f4
```

If the bot is **removed from a room**, that room's pending jobs are cancelled —
marked `cancelled`, with an audit log line each — because a job pointing at a
room the bot cannot reach would fire at nothing forever. **A cancelled job is
kept, not deleted**: `schedule list --all` shows it with `cancelled <time>`, and
`data/jobs.json` carries `cancelled_at` and `cancel_reason`, so a job the
gateway cancelled by mistake can be seen and undone — `schedule resume <id>`
restores it. Cancelled jobs age out after `completed_job_ttl_days`, like
completed ones (with the TTL set to `0` they are purged on the next tick, so
there is nothing to restore). Only `schedule delete` removes a record early.

If a job's **connector is removed from `config.yaml`** (or renamed), the job is
cancelled the same way at its next slot — marked, kept, with an audit line. It is never handed to another connector that happens to serve the same
room: under one account per agent that would run the job as a different agent,
with that agent's tools and account. Restore the connector under its old name
before the next slot and the job fires as before; `schedule resume` on the
cancelled job while its connector is still absent only gets it cancelled again
at the next slot.

`coop expire` does **not** cancel them. It clears a session and reclaims a
record; it does not stop a rule watching the room (that is a rules edit, or
removing the bot). So the room is still there, the job still records its id, and
the job brings the watcher back on its next run.

Deletion is permanent. Completed jobs can also be deleted to clean up the list.

---

## Tool Allow-List Rules

### Owners

Owners have `coop send`, `coop schedule`, and `date` auto-approved. No configuration is needed. When the agent runs a schedule command on your behalf, it is never blocked waiting for your approval.

### Guests

Guest rules are intentionally all-manual — there are no built-in guest approvals. You decide explicitly what guests can trigger.

If you want guests to be able to request schedules (i.e., ask the agent to create a job), add rules to the agent's `guest_allowed_tools` in your `config.yaml`:

```yaml
agents:
  my-agent:
    guest_allowed_tools:
      # Let guests ask the agent to run schedule commands
      - tool: "Bash"
        params: "coop\\s+schedule\\s+.*"
      # Let the agent use date to compute relative times (used in --starting values)
      - tool: "Bash"
        params: "date(\\s+.*)?"
```

Without these entries, the agent will pause and ask an owner to approve each `schedule` command a guest triggers.

> **Tip:** If guests should only be able to *see* scheduled output (the agent responds in the room when a job fires) but not *create* new jobs themselves, no guest rule changes are needed — scheduled jobs fire in the owner context and post to the room normally.

---

## Catch-Up Behavior on Restart

When AgentCoop restarts (e.g., after a system reboot or a config change), any job that was due while the daemon was down is fired immediately on startup. This means:

- A daily job that was supposed to run at 09:00 while AgentCoop was offline will fire as soon as AgentCoop comes back up.
- If multiple jobs were missed, all of them fire in sequence at startup.

This is intentional — no missed reminders, no silent skips. If you want to avoid catch-up fires for a specific job, pause it before stopping AgentCoop.

---

## Storage

All job state is persisted in:

```
~/.agentcoop/data/jobs.json
```

Each job record contains:

| Field | Description |
|---|---|
| `id` | Unique job identifier, format `acg-xxxxxxxx` |
| `watcher` | The watcher handle as of creation (display; `schedule list` shows the current one) |
| `connector` | The connector that owns the job |
| `room_id` | The platform room the job fires into — the identity a fire resolves through; empty on a pre-schema-2 job until `schedule migrate` records it |
| `cron` | The cron expression derived from `--every` or `--starting` |
| `timezone` | IANA timezone string |
| `times` | Max runs (`0` = forever) |
| `run_count` | How many times the job has fired so far |
| `status` | `active`, `paused`, `completed`, or `cancelled` |
| `cancelled_at` / `cancel_reason` | Set when the gateway cancelled the job (bot removed from the room, connector gone from the config); cleared by `schedule resume` |

You can inspect or back up `data/jobs.json` directly. Do not edit it while AgentCoop is running — restart AgentCoop after any manual edits.

> **Docker users:** mount `./data:/root/.agentcoop/data` as a directory volume to persist jobs across container recreates (upgrades).

---

## Timezone Handling

All times you specify with `--starting` are interpreted in the timezone given by `--tz`. If `--tz` is omitted, a `--starting` time is read in the **AgentCoop server's local timezone**. The connector's `timezone` setting is used only for schedules with no `--starting` (a bare `--every 1d` fires at 09:00 in the connector's zone).

Set a connector's default timezone in `config.yaml`:

```yaml
connectors:
  - name: rc-main
    type: rocketchat
    timezone: "Asia/Taipei"   # used for schedule --tz fallback and message timestamps
    ...
```

```bash
# Fires at 09:00 Taipei time every day
coop schedule create rc:general "Morning briefing" --every 1d --starting "09:00" --tz "Asia/Taipei"

# Fires at 09:00 in the gateway's local timezone every day (no --tz)
coop schedule create rc:general "Morning briefing" --every 1d --starting "09:00"
```

`NEXT RUN` in `schedule list` is always displayed in UTC regardless of the job's configured timezone.

Use any valid [IANA timezone name](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones), such as:

- `UTC`
- `Asia/Taipei`
- `America/New_York`
- `Europe/Berlin`
- `Asia/Tokyo`
- `America/Los_Angeles`
