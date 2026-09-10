# fetch-history — On-Demand Channel History

You can pull channel history at any point during your session using the
`coop fetch-history` command. This complements the startup
history injection (which is a fixed snapshot taken when your session began)
by letting you reach further back or refresh your view mid-session.

## When to use

- The startup history snapshot doesn't go back far enough for the current task.
- You need to look up something specific from earlier in the conversation.
- Your session has been running a while and you want a fresher view of recent messages.

## Usage

```bash
# Fetch recent history for your own watcher (most common)
coop fetch-history --watcher <your-watcher-name> --count 50

# Fetch messages from a specific point forward (most intuitive)
coop fetch-history --watcher <your-watcher-name> --count 50 --after "2026-05-10T19:25:00+08:00"

# Page backwards — fetch messages older than a given timestamp
coop fetch-history --watcher <your-watcher-name> --count 50 --before "2026-05-10T10:00:00+08:00"

# Fetch a specific time window (combine --after and --before)
coop fetch-history --watcher <your-watcher-name> --count 100 --after "2026-05-10T08:00:00+08:00" --before "2026-05-10T20:00:00+08:00"

# Control how many messages are shown verbatim vs condensed
coop fetch-history --watcher <your-watcher-name> --count 100 --verbatim 30
```

Your watcher name is in your `Coop Session Identity` context block (e.g. `hammer-mei`).

## Flags

| Flag | Default | Description |
|---|---|---|
| `--watcher NAME` | *(required)* | Your watcher name from Coop Session Identity |
| `--count N` | `50` | Max messages to retrieve |
| `--after TS` | *(not set)* | ISO 8601 timestamp — fetch messages from this point forward (inclusive) |
| `--before TS` | *(not set)* | ISO 8601 timestamp — fetch messages older than this (exclusive) |
| `--verbatim N` | `15` | Last N messages shown in full; older messages condensed to one line each |

`--after` and `--before` can be combined to fetch a specific time window.

## Navigation

**Forward (most common):** use `--after` with the timestamp of the oldest message
in your current context to get everything after that point:

```bash
# "Show me everything from 7:25 PM onwards"
coop fetch-history --watcher hammer-mei --count 50 --after "2026-05-10T19:25:00+08:00"
```

**Backward pagination:** use `--before` with the oldest timestamp in your current
result to page further back:

```bash
# Step 1: get recent history
coop fetch-history --watcher hammer-mei --count 50
# → note the oldest ts shown, e.g. "2026-05-10T08:15:00+08:00"

# Step 2: page back further
coop fetch-history --watcher hammer-mei --count 50 --before "2026-05-10T08:15:00+08:00"
```

## Output format

The output uses the same Rocket.Chat message header format as live messages,
so you can parse it with the same rules you already know:

```
[HISTORY FETCH — on-demand 2026-05-10T14:32:00+08:00]

**Earlier messages (condensed):**
[Rocket.Chat #nest | from: alice | role: owner | day: Sun | ts: 2026-05-10T08:00:00+08:00] Can you look into the auth issue?

**Recent messages:**
[Rocket.Chat #nest | from: alice | role: owner | day: Sun | ts: 2026-05-10T14:30:00+08:00]
What did you find?
```

## Notes

- Output comes through Bash stdout (tool-result content), not trusted system context.
- The server caps `--count` at `max_fetch_count` (default 200) to protect your context window. A warning is shown in the output when the count is clamped.
- Only messages from users in the owner/guest allowlist are included — same filtering as live message processing.
