# AgentCoop Scheduling Commands

AgentCoop (previously known as agent-chat-gateway, or ACG) lets you schedule recurring or one-time tasks using the `coop schedule` CLI. When a user asks you to set up a recurring task, reminder, or automated job, use these commands.

> **IMPORTANT — watcher name**: The `<watcher>` argument is the watcher's **runtime handle** — `<connector>:<room label>`, e.g. `rc:general` or `mm:dm:alice` — which is what your own message header identifies you as and what `coop list --all` shows. It is **not** the rule name from `config.yaml`: a rule creates one watcher per room, and `schedule create` rejects a rule name. Do NOT invent or guess a handle; if you are unsure, run `coop list --all` first and use one shown there.
>
> **Check the STATE column before scheduling.** A scheduled message is injected into the watcher's running session, so a watcher that is not running cannot receive it at that moment. This does **not** mean you should refuse — it means you should tell the user and confirm.
>
> | STATE | What happens to a fire |
> |---|---|
> | `active` | Delivered normally. |
> | `idle` | Fine — the watcher is woken on demand. |
> | `paused` | Skipped quietly and retried at the job's **next scheduled occurrence**. A finite job's remaining run count is **not** consumed. |
> | `failed` | Same retry. A ⚠️ notice is posted into the room **only if the gateway loaded that watcher this run** — one whose agent was unavailable at startup never did, so its misses are silent. Do not promise the user they will be told. |
> | not listed at all | `schedule create` is refused — a job needs a record with a room id. Tell the user to send one message in that room first, then run `coop list --all` and use the handle it shows. |
>
> For `paused` or `failed`, say that the agent in that room is currently paused / not running, **and say when the retry would actually land** — "the next scheduled occurrence" means very different things:
>
> - `--every 5m --times 1` → retries every 5 minutes until it gets through.
> - `--every 1w` → next week.
> - `--starting "YYYY-MM-DD HH:MM"` with **no** `--every` → the same date **next year**. A one-shot anchored to a specific date that misses its slot effectively does not happen at all.
>
> So a reminder for a time when the watcher will be back is fine — an ops pause today and a reminder next week works exactly as intended. A one-shot whose only fire lands inside the outage is not; say so and let the user decide.

## Create a scheduled task

```bash
coop schedule create <watcher> "<message>" [OPTIONS]
```

**Options:**
- `--every INTERVAL` — Recurrence interval. Accepts any `Nm` (1–59 minutes) or `Nh` (1–23 hours), plus `1d` and `1w`. Examples: `2m`, `7m`, `30m`, `3h`, `1d`, `1w`. Use `--starting` to set a time anchor.
- `--starting TIME` — Time anchor / start time. With `--every`: sets the first run time and (for `1d`/`1w`) pins the cron time-of-day. Without `--every`: specific datetime for a one-shot task. Accepts smart partial inputs (see below).
- `--times N` — Number of runs. `0` = forever (default). `N` = stop after N runs.
- `--tz TIMEZONE` — IANA timezone (e.g. `America/New_York`, `Europe/Berlin`, `UTC`). The `--starting` time is interpreted in this timezone. Only relevant for daily/weekly jobs anchored to a specific local time — **omit for sub-hourly intervals** (`1m`–`12h`), which fire on a fixed cadence regardless of timezone.

**`--starting` accepts smart partial inputs — the user does NOT need to type full dates:**
- `"09:00"` → today at 09:00 (auto-advances to tomorrow if already past)
- `"Apr 15 09:00"` or `"04-15 09:00"` → this year at that date
- `"Mon 09:00"` → next Monday at 09:00 (for `--every 1w`)
- `"2026-05-01 09:00"` → explicit full datetime (for cross-year scheduling)

**Examples:**

```bash
# Remind the user in 5 minutes (one-shot relative reminder)
coop schedule create rc:general "提醒：去喝水！" --every 5m --times 1

# Remind the user in 1 hour (one-shot)
coop schedule create rc:general "Time to take a break!" --every 1h --times 1

# Run a daily standup check at 09:00, starting today
coop schedule create rc:general "Run the daily standup summary" --every 1d --starting "09:00" --times 0

# Check CI status every hour, 24 times (one day)
coop schedule create rc:ops "Check CI pipeline status" --every 1h --times 24

# Weekly report every Monday at 10:00 AM
coop schedule create rc:general "Generate the weekly report" --every 1w --starting "Mon 10:00" --times 0

# One-time reminder at a specific date/time
coop schedule create rc:general "Reminder: feature freeze today" --starting "2026-04-10 15:30"

# Monitor every 30 minutes, forever (no --tz needed for sub-hourly jobs)
coop schedule create rc:ops "Check server health" --every 30m --times 0

# Daily standup at 09:00 in a specific timezone — use --tz here
coop schedule create rc:general "Run daily standup" --every 1d --starting "09:00" --tz "America/New_York"

# Start firing every minute, 5 times, beginning at 14:00 today
coop schedule create rc:general "Pulse check" --every 1m --times 5 --starting "14:00"
```

## List scheduled tasks

```bash
coop schedule list              # Show active and paused tasks
coop schedule list --all        # Also show recently completed or cancelled tasks
coop schedule list --connector rc-home  # Filter by connector
```

## Delete a scheduled task

```bash
coop schedule delete <job-id>   # e.g.: coop schedule delete acg-a3f2b1c0
```

## Pause and resume

```bash
coop schedule pause <job-id>    # Temporarily stop a recurring task
coop schedule resume <job-id>   # Re-enable a paused task, or restore a cancelled one
```

## Notes

- The job's **message is a prompt to you**, delivered into your own session when the job fires, with the header `from: scheduler | … | to: me`. The prompt itself is never shown in the room — **your reply is what gets posted**. So write the message as an instruction to yourself ("Post one computer part with a one-line fact"), not as the finished text you want to appear; a message that already reads as the announcement gives you nothing to add, and answering it with `<end-of-agent-chain>` posts nothing at all, every run.
- The minimum scheduling interval is 1 minute.
- Job IDs look like `acg-a3f2b1c0`. Use `coop schedule list` to find a job's ID.
- If the gateway is restarted, any jobs missed during downtime will be fired immediately on startup.
- Forever jobs (`--times 0`) only stop when you explicitly delete them.

## ⚠️ If --starting time has already passed

If the user gives a starting time that is already in the past (e.g. "start at 9am" when it is already 2pm), do NOT silently create the job. Instead, confirm:
"It's already [current time]. Did you mean [tomorrow/next occurrence] at [time]?"
Wait for user confirmation before running the schedule create command.

AgentCoop will also print a warning to stderr showing what was requested and what the actual next fire time will be — the job is created with the auto-advanced time.

## ⚠️ Important: Relative reminders — use `--every` + `--times 1`, NOT `$(date ...)`

When the user asks for a reminder "in N minutes" or "in N hours", **always** use `--every` with `--times 1`.
**Never** use shell command substitution like `$(date ...)` in `--starting` — it is not needed and causes permission errors.

```bash
# ✅ CORRECT — any number of minutes works for one-shot reminders
coop schedule create <watcher> "<message>" --every 3m --times 1
coop schedule create <watcher> "<message>" --every 7m --times 1
coop schedule create <watcher> "<message>" --every 23m --times 1

# ✅ CORRECT — hours also work
coop schedule create <watcher> "<message>" --every 2h --times 1

# ❌ WRONG — never do this
coop schedule create <watcher> "<message>" --starting "$(date -v+3M '+%Y-%m-%d %H:%M')"
```
