# agent-chat-gateway User Guide

## What is agent-chat-gateway?

Inspired by [OpenClaw](https://github.com/openclaw/openclaw)'s vision of making AI agents accessible from any messaging app, `agent-chat-gateway` bridges your existing AI agent tools — Claude CLI, OpenCode, or any custom backend — to your team's chat platform. When someone messages the bot in a watched room, the message is forwarded to the configured agent and the response is posted back.

Think of it as a persistent bridge: set up once, configure which rooms to watch, and your AI assistant becomes available across your entire chat workspace (Rocket.Chat or Mattermost — mix both if you use more than one) — with full support for role-based access control, human-in-the-loop permission approvals, and file attachments.

> **How it compares to Claude Code Channels:** Claude Code's native [Channels](https://code.claude.com/docs/en/channels) feature (v2.1.80+) lets a single Claude Code session receive messages from Telegram, Discord, or iMessage — a great fit for personal use. `agent-chat-gateway` was developed independently before Channels shipped and targets a different layer: team deployments with multiple agent backends (not just Claude Code), Owner/Guest roles with per-tool allow-lists, and a shared workspace where multiple people can interact with the same or different agents across multiple rooms simultaneously.

### Key Concepts

| Term | Meaning |
|--|--|
| **Connector** | Adapter for a chat platform (Rocket.Chat, Mattermost). Handles incoming messages and posts replies back. |
| **Agent backend** | The AI tool being dispatched to (e.g., Claude CLI subprocess, OpenCode subprocess). |
| **Watcher** | A binding between one chat room and one agent backend. One watcher per room. |

---

## Prerequisites

Before installing, ensure you have:

- **Python 3.12 or later** — verify with `python3 --version`
- **A chat server** — Rocket.Chat or Mattermost, with your workspace/team URL
- **Claude CLI or OpenCode installed** — at least one agent backend available
  - Claude CLI: https://claude.ai/download
  - OpenCode: https://github.com/anthropics/opencode
- **A bot account on your chat server** — with permissions to post messages and read room history
  - Rocket.Chat: a bot user account (username + password)
  - Mattermost: a Bot Account access token, or a regular account's username + password — see [Connectors](#connectors) below
- **At least one owner username** — someone who can approve/deny tool calls in chat

---

## Installation

For detailed installation instructions, see [install-agent.md](install-agent.md).

Quick summary:
```bash
pip install agent-chat-gateway
mkdir -p ~/.agent-chat-gateway
# Create config.yaml (see Configuration section below)
agent-chat-gateway start
```

---

## Quick Start

### Minimal Working Config

Create `~/.agent-chat-gateway/config.yaml`:

```yaml
connectors:
  - name: rc-home
    type: rocketchat
    server:
      url: "https://chat.example.com"
      username: "mybot"
      password: "${RC_PASSWORD}"
    allowed_users:
      owners:
        - alice
      guests: []

agents:
  claude:
    type: claude
    command: claude
    working_directory: ~/.agent-chat-gateway/work
    timeout: 360
    permissions:
      enabled: true
      timeout: 300

watcher_rules:
  - name: general
    connector: rc-home
    rooms:
      include: [general]
    agent: claude
```

### Start the Daemon

```bash
# Set the password via environment
export RC_PASSWORD="your_bot_password"

# Start the gateway
agent-chat-gateway start

# Check status
agent-chat-gateway status
```

### Send a Message

```bash
# Direct message (bypasses the agent)
agent-chat-gateway send general "Hello from the CLI"

# Or read from stdin
echo "Hello from stdin" | agent-chat-gateway send general -
```

---

## Use Cases

### Use Case 1 — Build a Super-Powered Chatbot for Your Team

Connect your existing agent to multiple rooms with different roles and responsibilities. Use context injection to give each room its own system prompt, and enable human-in-the-loop approval so sensitive operations always require owner sign-off.

**Example: Engineering team with Claude for general chat and OpenCode for development**

```yaml
connectors:
  - name: rc-company
    type: rocketchat
    server:
      url: "${RC_URL}"
      username: "${RC_BOT_USER}"
      password: "${RC_BOT_PASS}"
    allowed_users:
      owners:
        - alice
        - bob
      guests:
        - charlie
        - dan

agents:
  claude:
    type: claude
    command: claude
    working_directory: ~/.agent-chat-gateway/work
    timeout: 360
    permissions:
      enabled: true
      timeout: 300
    owner_allowed_tools:
      - tool: "Read"
      - tool: "WebFetch"
        params: "https?://(www\\.)?github\\.com/.*"
      - tool: "Bash"
        params: "git (log|diff|status|show).*"
    guest_allowed_tools:
      - tool: "Read"
      - tool: "Glob"

  opencode:
    type: opencode
    command: opencode
    working_directory: ~/.agent-chat-gateway/opencode-work
    timeout: 360
    permissions:
      enabled: true
      timeout: 300

watcher_rules:
  - name: general
    connector: rc-company
    rooms:
      include: [general]
    agent: claude
    context_inject_files:
      - contexts/team-assistant.md    # team-specific system prompt

  - name: dev
    connector: rc-company
    rooms:
      include: [dev]
    agent: opencode
    context_inject_files:
      - contexts/engineering-context.md

  - name: support
    connector: rc-company
    rooms:
      include: [support]
    agent: claude
    context_inject_files:
      - contexts/support-runbook.md
```

**Key settings for this use case:**
- Set `permissions.enabled: true` to keep humans in the loop for sensitive tool calls
- Use `context_inject_files` at the watcher level for room-specific personas or knowledge bases
- Use `guest_allowed_tools` to restrict what non-owners can ask the agent to do
- Add a room profiles context file so the agent knows who it's talking to — see the
  [Room Member Profiles](#example-room-member-profiles) section below and the template at
  [`contexts/rc-room-profiles.example.md`](../contexts/rc-room-profiles.example.md)

---

### Use Case 2 — Access Your Agent from a Messaging App

Already have Claude CLI or OpenCode running on your machine? Expose it to your team via Rocket.Chat with minimal configuration. No special RBAC setup needed if it's just you — set yourself as the sole owner and you're good to go.

**Example: Single-user personal agent bridge**

```yaml
connectors:
  - name: rc-personal
    type: rocketchat
    server:
      url: "${RC_URL}"
      username: "${RC_BOT_USER}"
      password: "${RC_BOT_PASS}"
    allowed_users:
      owners:
        - alice          # Only you — no guests

agents:
  claude:
    type: claude
    command: claude
    working_directory: ~/my-agent-work
    timeout: 360
    permissions:
      enabled: false     # Skip approval prompts for personal use

watcher_rules:
  - name: my-assistant
    connector: rc-personal
    rooms:
      direct: true       # 1:1 DMs — only people in `owners`/`guests` get through
    agent: claude
```

**Key settings for this use case:**
- Use `rooms.direct: true` to serve DMs instead of a channel — a DM has no
  room name for a pattern to match, and the connector's `owners` list gates
  who can talk
- Set `permissions.enabled: false` for personal use where approval friction isn't needed
- Set yourself as the sole owner; omit `guests` entirely

> **Similar to Claude Code Channels:** Claude Code's [Channels](https://code.claude.com/docs/en/channels) feature (v2.1.80+) also connects external platforms (Telegram, Discord, iMessage) to a local Claude Code session via `claude --channels`. The key differences: Channels is Claude Code-specific and single-user focused, while `agent-chat-gateway` supports any agent backend (Claude CLI, OpenCode, custom), multi-user RBAC, and is designed for team-shared chat workspaces (Rocket.Chat or Mattermost). If you only use Claude Code and only need personal access, Channels may be simpler to set up; if you need team access or a different agent backend, `agent-chat-gateway` is the better fit.

---

### Use Case 3 — Carry an Existing Agent Session's Context into Chat

If you have a long-running agent session already in progress (e.g. a Claude session
you started locally), hand its context over to a watcher so your messaging app picks
up where you left off.

> **Similar to Claude Code's Remote Control:** Claude Code's [Remote Control](https://code.claude.com/docs/en/remote-control) feature (`claude --remote-control`) lets you drive a local session from `claude.ai/code` or the Claude mobile app. `agent-chat-gateway` takes a complementary approach: instead of a personal remote interface, your session becomes accessible from your team's shared chat room — with RBAC and permission approval so others can interact safely too.

**Use a handoff, not a pinned session id.** Earlier versions accepted
`watchers[].session_id` to attach a watcher to one specific backend session.
That field has been removed, and setting it is now a config error. Two reasons:

- **It could not survive the backend.** A pinned id names a session the backend is
  free to expire — Claude Code's default `cleanupPeriodDays` is 30 days. Once that
  happens the id refers to nothing, and the watcher starts empty with no warning.
- **There is nothing to pin it to.** A watcher is created per room as rooms are
  discovered, so a single id in config cannot say which room it belongs to.

A handoff has neither problem: it is a file, so it outlives any session, and each
watcher reads it on its own session start.

**Example: hand off a local session's context**

```bash
# 1. Resume the existing session, have it PRINT the summary, and redirect that to
#    the file yourself.
#
#    Two details that both bite silently if you skip them:
#    - `--resume <id>` (or `-c` for the most recent session) is required. A bare
#      `claude -p` starts a NEW session — that is exactly how this project creates
#      one — so it would summarise nothing, successfully.
#    - Redirect stdout rather than asking Claude to write the file. In
#      non-interactive `-p` mode the `Write` tool needs prior approval, so a run
#      without it finishes without creating anything.
claude --resume ses_abc123def456 -p "Summarise everything we have established in
this session — decisions, constraints, open questions, and where we left off.
Write it for another instance of yourself with no memory of this conversation.
Output only the summary." > /Users/me/project/HANDOFF.md

# Session ids are printed by `claude -p --output-format json`; `claude -c -p`
# resumes the most recent session without needing one.

# Check the contents, not just that the file is there: `>` creates the file before
# the command runs, so a failed or wrong-session run still leaves one behind.
wc -l /Users/me/project/HANDOFF.md && head -5 /Users/me/project/HANDOFF.md
```

```yaml
# 2. Point the watcher at that file. Context files are read and sent to the agent
#    on session start, so it begins with the context rather than discovering it.
watcher_rules:
  - name: my-project
    connector: rc-home
    rooms:
      direct: true
    agent: claude
    context_inject_files:
      - /Users/me/project/HANDOFF.md
```

```bash
# 3. Start the gateway and continue from your messaging app.
agent-chat-gateway start
```

**Notes:**

- `context_inject_files` paths are resolved relative to `config.yaml`'s directory, so
  a relative path written from a project shell will not resolve — use an absolute
  path, or write the handoff next to `config.yaml`.
- Context files are re-read on every watcher start, so rewriting `HANDOFF.md` takes
  effect the next time the watcher starts. Run `agent-chat-gateway reset <watcher>`
  as well if you want the updated context to open a *fresh* conversation instead of
  continuing the existing one.
- Session *continuity* across daemon restarts needs no configuration: the gateway
  persists each watcher's runtime session id in its state file and resumes it. The
  removed field was only ever about pinning a session id chosen by hand.

## Configuration Reference

### Top-Level Configuration

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `max_queue_depth` | integer | No | 100 | Per-room message queue size; 0 = unlimited |
| `connectors` | list | Yes | (none) | Chat platform connections |
| `agents` | dict | Yes | (none) | AI agent backend definitions |
| `watcher_rules` | list | Yes | (none) | Rules declaring which rooms an agent serves — see [Watchers](#watchers) |
| `tool_presets` | dict | No | `{}` | Named, reusable tool-rule lists — see [Tool Allow-Lists](#tool-allow-lists) |
| `connector_templates` | dict | No | `{}` | Named, reusable field blocks for connectors — each entry opts in via its own `inherits: <name>` |
| `agent_templates` | dict | No | `{}` | Named, reusable field blocks for agents — each entry opts in via its own `inherits: <name>` |
| `watcher_templates` | dict | No | `{}` | Named, reusable field blocks for watcher rules — each entry opts in via its own `inherits: <name>`. May set `rooms:`, with rules that are easy to get wrong — see [Templates and `rooms` inheritance](#templates-and-rooms-inheritance). Cannot set `name` |

None of the four templates/preset fields above are required — a single
connector/agent/watcher setup rarely needs them. They exist to avoid
repeating the same block across many connectors/agents/watchers in larger
deployments — see `docs/migration-0.3.md` for the reasoning and before/after
recipes. `config.example.yaml` has a worked example. A leftover
`connector_defaults:`/`agent_defaults:`/`watcher_defaults:` key (the pre-v0.3
mechanism) is a hard load-time error, not silently ignored.
Check your config any time with `agent-chat-gateway config validate --lint`.

### Connectors

Each connector represents a connection to one chat platform. Rocket.Chat and Mattermost
are both fully supported today; a daemon can run one, the other, or both at once (see
[Multi-Connector Setup](#multi-connector-setup)).

> ⚠️ **One bot account per connector.** Two connectors that log in as the same account
> on the same server receive the *identical* message stream, so any room they both cover
> gets two agents replying to every message. The daemon checks this after logging in —
> against the account id the server reports, not what config says, because a token can
> authenticate an account without naming it — and refuses to start before either
> connector subscribes to anything. Give each connector its own bot account.
>
> **Mattermost is the one exception**: two connectors on one account are allowed when
> each is scoped to a **different team**, since each ignores the other team's channels.
> Even then their direct messages must not overlap — a DM belongs to no team, so the
> server delivers it to every connection the account has open. Watching `@alice` on one
> and `@bob` on the other is fine; the same person on both is not, and a rule with
> `direct:` takes *every* DM, which overlaps with any of them.

```yaml
connectors:
  - name: rc-main                    # Unique identifier
    type: rocketchat
    server:
      url: "https://chat.example.com"
      username: "bot-username"
      password: "${RC_PASSWORD}"      # Use env-var expansion for secrets
    allowed_users:
      owners:
        - alice                       # Full access; approve/deny tool calls
        - bob
      guests:                         # Restricted access; guest tool allow-list only
        - charlie
    attachments:
      max_file_size_mb: 50           # 0 = no limit
      download_timeout: 30            # Seconds
      cache_dir_global: ~/.agent-chat-gateway/attachments  # connector-global cache directory
    reply_in_thread: false            # Start new thread for replies
    permission_reply_in_thread: true  # Post permission requests in thread
    context_inject_files: []          # Files sent to agent on session start

  - name: mm-main                     # A second connector — Mattermost, in this case
    type: mattermost
    server:
      url: "https://chat.example.com"
      team: myteam                    # Mattermost channels are team-scoped; one connector = one team
      token: "${MM_BOT_TOKEN}"        # Bot Account / Personal Access Token — OR username+password below, not both
      # username: "bot-username"
      # password: "${MM_PASSWORD}"
    allowed_users:
      owners:
        - alice
      guests:
        - charlie
    attachments:
      max_file_size_mb: 50
      download_timeout: 30
      cache_dir_global: ~/.agent-chat-gateway/attachments
    reply_in_thread: false
    permission_reply_in_thread: true
    context_inject_files: []
```

> Mattermost has no onboarding CLI wizard support yet — this block must be hand-written
> (unlike Rocket.Chat, which `agent-chat-gateway onboard` can generate for you).

**Connector Fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Unique connector identifier (used in CLI commands and watcher references) |
| `type` | string | Yes | Platform type: `rocketchat` or `mattermost` |
| `server.url` | string | Yes | Chat server URL (e.g., `https://chat.example.com`) |
| `server.username` | string | Yes (RC); one of username/password or token (Mattermost) | Bot account username |
| `server.password` | string | Yes (RC); one of username/password or token (Mattermost) | Bot account password (plaintext — `config.yaml` is chmod'd `0600`) |
| `server.team` | string | Yes (Mattermost only) | Team name the bot's channels belong to — Mattermost channels are team-scoped, unlike Rocket.Chat |
| `server.token` | string | One of token or username/password (Mattermost only) | Bot Account / Personal Access Token — used directly, no login call, no expiry handling needed |
| `allowed_users.owners` | list | No | Usernames with full tool access |
| `allowed_users.guests` | list | No | Usernames with restricted tool access |
| `attachments.max_file_size_mb` | integer | No | Maximum file size; 0 = unlimited |
| `attachments.download_timeout` | integer | No | Seconds to wait per file download |
| `attachments.cache_dir_global` | string | No | Download cache directory (default: `~/.agent-chat-gateway/attachments`; only needed to override the default) |
| `reply_in_thread` | boolean | No | Reply in thread for every message |
| `permission_reply_in_thread` | boolean | No | Post permission requests in threads |
| `context_inject_files` | list | No | Context files for all sessions on this connector |

### Agents

Each agent backend represents a CLI tool (Claude, OpenCode, etc.) that the gateway can dispatch to.

```yaml
agents:
  claude:
    type: claude
    command: claude
    working_directory: ~/.agent-chat-gateway/work
    new_session_args: []
    session_prefix: "agent-chat"
    lazy_instruction_loading: true
    context_inject_files: []

    owner_allowed_tools:
      - tool: "Read"
      - tool: "Bash"
        params: "git (log|diff|status).*"
      - tool: "WebFetch"
        params: "https?://.*github.*"

    guest_allowed_tools:
      - tool: "Read"
      - tool: "Glob"

    timeout: 360
    permissions:
      enabled: true
      timeout: 300
      skip_owner_approval: false
```

**Agent Fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | string | Yes | Backend type: `claude` or `opencode` |
| `command` | string | Yes | CLI command to invoke (e.g., `claude`, `opencode`) |
| `working_directory` | string | Yes | Working directory for the agent subprocess |
| `new_session_args` | list | No | Extra CLI args for new sessions |
| `session_prefix` | string | No | Prefix for session titles |
| `lazy_instruction_loading` | boolean | No | If `true` (default), injects a short tool index and lets agents load bundled scheduling/history docs on demand with `agent-chat-gateway instructions ...`; if `false`, injects the full bundled tool docs at session start. |
| `context_inject_files` | list | No | Context files injected on every session |
| `owner_allowed_tools` | list | No | Auto-approved tools for owners (see Tool Allow-Lists below) |
| `guest_allowed_tools` | list | No | Auto-approved tools for guests (see Tool Allow-Lists below) |
| `timeout` | integer | Yes | Seconds to wait for agent response (must be > `permissions.timeout`) |
| `permissions.enabled` | boolean | No | Enable human-in-the-loop tool approval |
| `permissions.timeout` | integer | No | Seconds before auto-denying unanswered requests (must be < agent `timeout`) |
| `permissions.skip_owner_approval` | boolean | No | If `true`, owners bypass approval prompts (guests still enforced); only use in trusted sandbox environments |

### Watchers

A `watcher_rules:` entry is a **rule**: it declares which rooms an agent serves,
and the gateway creates each room's watcher on demand — on the room's first
message for Rocket.Chat and Mattermost, or eagerly at startup for connectors
with no inbound stream (voice, script — their rules must name literal rooms).

```yaml
watcher_rules:
  - name: general-assistant     # the RULE's name — required
    connector: rc-main
    rooms:
      include: [general]        # glob patterns work: [eng-*, general]
      # except_for: [eng-private]  # subtracted from this rule's include
      # direct: true               # also serve 1:1 DMs
      # group_direct: true         # also serve multi-party DMs (mentions required)
    agent: claude
```

Each created watcher is named `<connector>:<room>` — that derived name is
what `list` shows and what `pause`/`resume`/`reset`/`expire` act on. It follows
the room on Rocket.Chat and Mattermost: rename a channel or private group and,
from the next message in it, `list` shows the new name and the old one no longer
resolves (the log carries an `AUDIT` line).

**"Name" here is the room's URL name, not its display name.** On Mattermost that
is the channel `name` — the last segment of `…/channels/<name>`, editable in the
channel's rename/settings dialog under *URL* — and on Rocket.Chat the room
`name` (not `fname`). Changing only the Display Name does **not** rename the
watcher, by design: display names admit any character and two rooms may share
one, while the URL name is restricted and unique within a team, which is what
makes a watcher handle unambiguous. DM labels keep the counterpart's name as of
creation, and a new name still held by another room's stale record is not taken
until that record goes. Scripts should not store a watcher name; the room is the
identity. Rules
match top-down; the first rule that claims a room wins, and `agent-chat-gateway config
validate` warns when an earlier rule shadows a later one completely.

A quiet room is dropped after `session_idle_days` (default 15 — the session
is kept and the next message resumes it) and reclaimed entirely after a
further `session_expire_days` (default 15). Pause a watcher to exempt it from
both timers.

**A config written for the old static watchers fails at load, and the first
error is the key name.** The list used to be called `watchers:`, which this
gateway no longer has a use for, so it is reported the way any unrecognised
top-level key is:

```
config.yaml sets 'watchers', which this gateway does not use.
Valid top-level keys are: ... 'watcher_rules', 'watcher_templates'.
```

Rename the key first; only then do the per-entry errors become visible
(`room: general` is refused as an unknown key of a rule, and a list-shaped
`rooms:` is refused as the wrong type). See
[docs/migration-dynamic-watchers.md](migration-dynamic-watchers.md) for the
rest of the rewrite.

> ⚠️ **One watcher per room, per connector.** Two rules cannot both serve a
> room — first-match precedence gives it to the earlier rule, and validate
> warns about the shadowed one. To put two agents in one room, give each its
> own bot account and its own connector, which is the supported multi-agent
> setup.

> ℹ️ **A watcher's name is display only.** State is keyed by the room id, so a
> room rename keeps the session, the message watermark, the attachment workspace
> and the system-prompt file; only the handle `list` shows (and that you type into
> `pause|resume|reset|expire`) changes, from the next message in the room.

**Watcher Rule Fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Rule identifier (frozen into each created watcher's record) |
| `connector` | string | **Yes*** | Must match a connector name above. *Required on the rule as it is finally resolved — a rule may take it from its `inherits:` template instead of stating it. There is no implicit default |
| `rooms.include` | list[string] | Yes* | Room-name globs this rule claims (e.g. `[eng-*, general]`). *May be empty only when a DM flag below is set |
| `rooms.except_for` | list[string] | No | Globs subtracted from this rule's `include` |
| `rooms.direct` | bool | No | Also serve 1:1 DMs (whole class) |
| `rooms.group_direct` | bool | No | Also serve group DMs (whole class; mentions required) |
| `agent` | string | **Yes*** | Agent backend this rule's rooms run on. *Required on the rule as it is finally resolved — a rule may take it from its `inherits:` template instead of stating it. There is no implicit default |
| `session_idle_days` | int | No | Days without a message before the room's runtime is dropped (session kept); default 15 |
| `session_expire_days` | int | No | Days idle before the record and session are reclaimed entirely; default 15 |
| `context_inject_files` | list | No | Rule-specific context files (frozen into each created watcher) |

#### Templates and `rooms` inheritance

A `watcher_templates:` entry can carry `rooms:`, and every subkey of it is
inheritable — `include`, `except_for`, `direct` and `group_direct`. What makes
this worth its own section is that `rooms:` is a **matcher**, not a settings
block, so two rules inheriting the same matcher can end up fighting over the
same room. Each rule below behaves the way it does for that reason.

**A rule's own `rooms:` merges over the template's, key by key.** Keys the rule
does not mention are inherited as-is:

```yaml
watcher_templates:
  channels:
    connector: rc-main
    rooms: {direct: true}
watcher_rules:
  - {name: eng, inherits: channels, rooms: {include: ['eng-*']}}
  # -> include: [eng-*]  AND  direct: true — both survive
```

**A list the rule sets replaces the template's list outright; the two are not
concatenated.** A template `include: [a-*]` under a rule that sets
`include: [b-*]` yields `[b-*]` only. Same for `except_for`. If you want both
patterns, list both in the rule.

**To switch off an inherited flag, write `false` — not `null`.** The field is
read as a boolean, and `null` is a load error (*"'rooms.direct' must be true or
false"*), not a way to unset it.

**A template cannot supply `except_for` on its own.** `except_for` subtracts
from `include`, so a rule that inherits only an exclusion matches nothing and
is refused:

```
Watcher rule at index 0 ('r1') can never match any room: 'rooms.include'
is empty and neither 'rooms.direct' nor 'rooms.group_direct' is set.
```

**An inherited `except_for` pattern must be able to match something the
inheriting rule includes.** An exclusion that cannot overlap the rule's
`include` looks like protection but removes nothing, so it is a hard error
rather than a silent no-op:

```yaml
watcher_templates:
  channels: {connector: rc-main, rooms: {except_for: ['*-secret']}}
watcher_rules:
  - {name: eng, inherits: channels, rooms: {include: ['eng-*']}}   # ok: eng-secret overlaps
  - {name: ops, inherits: channels, rooms: {include: ['ops-*']}}   # ok: ops-secret overlaps
```

Written as `except_for: ['ops-secret']` instead, the same template would break
the `eng` rule — no room named `eng-*` can ever be called `ops-secret`:

```
'rooms.except_for' entry 'ops-secret' does nothing here, because this rule's
'include' never matches a room by that name.
```

So a shared exclusion has to be phrased broadly enough (a suffix like
`*-secret`) to bite on every rule that inherits it.

**Do not put `direct: true` or `group_direct: true` in a template several rules
inherit.** Only the first rule that matches a room serves it, and the DM classes
are not name-matched, so the first inheriting rule takes every DM and the DM half
of the later ones is dead. It loads, and `config validate` warns:

> Watcher rule 'ops' will never see one-to-one direct messages, because those
> are already handled by 'eng', which is listed above it.

Give DMs their own rule that does not inherit the template, as in the
[migration guide's example](migration-dynamic-watchers.md#the-rewrite).

### Tool Allow-Lists

`owner_allowed_tools` and `guest_allowed_tools` control which tools can be used without requiring permission approval.

Each entry is either:
- an object with **`tool`** (required, regex matched against the tool name)
  and **`params`** (optional, regex matched against the tool's primary
  parameter), or
- a **string** naming a top-level `tool_presets:` entry — expanded in place,
  so the same rule set can be shared across agents without repeating it. See
  [permission-reference.md](permission-reference.md#named-presets) for details.

**Example:**

```yaml
owner_allowed_tools:
  - tool: "Read"                      # Allow all Read calls
  - tool: "Bash"
    params: "git (log|diff|status).*"  # Allow specific git commands
  - tool: "WebFetch"
    params: "https?://.*github\\.com.*"  # Allow GitHub only
  - tool: ".*"                        # Allow all tools (use with care!)

guest_allowed_tools:
  - tool: "Read"
    params: ".*\\.md$"                # Allow markdown files only
  - tool: "Glob"                      # Allow any glob pattern
```

**Parameter Extraction:**

- **Bash** — `tool_input["command"]` (the shell command)
- **WebFetch** — `tool_input["url"]` (the full URL)
- **Read/Edit/Write** — `tool_input["file_path"]` (the file path)
- **MCP/Unknown tools** — full `tool_input` serialized as JSON

**Security Note:**

Always use explicit domain patterns to prevent SSRF attacks. Avoid `params: ".*"` for WebFetch:

```yaml
# ❌ DANGEROUS — allows localhost, internal networks
- tool: "WebFetch"
  params: ".*"

# ✅ SAFE — explicit whitelist
- tool: "WebFetch"
  params: "https?://(www\\.)?github\\.com/.*"
```

### Secrets and Environment Variables

Store credentials directly in `config.yaml` — no separate file needed:

```yaml
server:
  url: "https://chat.example.com"
  username: "mybot"
  password: "your_bot_password"
```

`config.yaml` is chmod'd `0600` automatically, both by the config TUI on every
save and by `agent-chat-gateway start` — as long as you don't commit your
filled-in copy to version control, plaintext here is safe.

The gateway does **not** expand `$VAR`/`${VAR}` in config values — a string
that happens to look like a placeholder is used exactly as written, like
any other string. If you're upgrading from an older setup that used a
`.env` file with `${VAR}` references, the next `agent-chat-gateway start`
(or opening `agent-chat-gateway config`) folds those values into `config.yaml`
as literal text and removes `.env` automatically — one-time, no action
needed. Run `agent-chat-gateway config migrate-env` first if you'd rather
do that as a manual step or a dry run.

---

## CLI Commands

### Daemon Lifecycle

```bash
# Start the daemon
agent-chat-gateway start [--config path/to/config.yaml]

# Stop the daemon
agent-chat-gateway stop

# Restart (picks up config and code changes)
agent-chat-gateway restart [--config path/to/config.yaml]

# Check status — pid, uptime, watcher count, the active config digest and load
# time, and any section a reload could not bring back
agent-chat-gateway status
```

### Reloading Configuration

`config reload` applies changes in `config.yaml` to the running daemon without
restarting the parts the edit did not touch:

```bash
# Preview: what a reload would do, changing nothing
agent-chat-gateway config reload --dry-run

# Apply: prints the plan it is about to execute, then executes it
agent-chat-gateway config reload

# Machine-readable, for scripts and agents
agent-chat-gateway config reload --dry-run --json
```

What it does, in order: validates the whole file (an invalid file is rejected
in full and the daemon is left exactly as it was); diffs it against the
configuration the daemon is running; restarts only the connectors and agents
whose definition changed; and reconciles every watcher record against the
current rules exactly as a start does — a record whose rule changed is
re-materialized **with its session kept**, a record no rule covers any more is
expired with its full session id logged (`AUDIT: session released`).

| You changed… | What restarts |
|---|---|
| A connector's fields (token, server, team…) | That connector only; its records are re-validated against its scope after reconnect |
| A connector's `name` | Treated as remove + add: every record under the old name expires, its state file is deleted — the dry run shows this |
| An agent's fields | That agent's backend (an OpenCode sidecar restarts) and the watchers on it, sessions kept |
| An agent's `type` or `working_directory` | As above, but those watchers start fresh sessions; the old ids are logged |
| A rule (edit, add, remove, reorder) | Nothing restarts; every record is re-matched — re-materialized or expired as the plan shows |
| `max_queue_depth`, `scheduler.*` | Nothing; the value is swapped in place |
| `description` on anything | Nothing at all — a free edit |

**Exit codes:** `0` applied cleanly, or no changes; `1` the file is invalid or
the command was refused (nothing changed); `2` applied, but a section is
**degraded** — a connector that failed to reconnect, an agent whose backend
or permission broker failed to start or to stop, a watcher that did not come
back. Treat `2` as a failed reload and act on it now: the output names the
section and what to do; `status` keeps showing it. Nothing is rolled back and
nothing is force-killed — a stop is retried a few times, and what still will
not stop is left for you to look at (it is stopped again at the next reload
and at shutdown). Fix what was wrong and reload again: every reload retries
the degraded sections, even when the file did not change.

**What it does not guarantee.** Messages arriving on a connector while it
restarts may be lost, the same as for any connector restart. A reload cannot
be interrupted once sent — the plan printed before the apply is your last
look, and a dry run is not binding on a later apply (the file is read again).
Lifecycle verbs (`pause`/`resume`/`reset`/`expire`) and a second `reload` are
refused while one applies. Saving in the config TUI does **not** reload.

With the daemon stopped, `--dry-run` prints the plan the next `start` will
execute (the same reconciliation), and a reload without `--dry-run` refuses.
If the daemon appears to be running but cannot be reached, the command fails
rather than guessing.

`config show` prints a SHA-256 digest of the *resolved* configuration
(templates expanded; comments and key order do not matter) and its flattened
contents with passwords, tokens and secrets redacted — safe to paste into a
bug report or compare between machines. With the daemon running it also shows
the active digest and warns when the file differs, so "I edited but forgot to
reload" is visible. `config validate --json` and `config reload --json` emit
structured output.

### Watcher Control

All commands require the daemon to be running.

```bash
# List watchers — active, failed and paused by default
agent-chat-gateway list [--connector NAME]

# Include idle watchers (released on purpose, nothing running),
# or ask for one state at a time
agent-chat-gateway list --all
agent-chat-gateway list --idle
agent-chat-gateway list --failed
agent-chat-gateway list --active --paused

# Pause a watcher (stops processing messages)
agent-chat-gateway pause <watcher-name>

# Resume a paused watcher
agent-chat-gateway resume <watcher-name>

# Reset a watcher (clear state, create new session)
agent-chat-gateway reset <watcher-name>

# Expire a watcher now: reclaim its record and files; the room's next message
# recreates it. Refused on voice/script connectors — nothing arrives on its
# own there to bring the watcher back — use `reset` on those instead.
agent-chat-gateway expire <watcher-name>
```

`list` reports the watchers the gateway has **state records** for, not the
entries in `config.yaml`.

**The rule is simply: a watcher appears if a state record exists for it.**
Nothing more — not whether it started, not how far it got.

That is worth stating as a rule rather than as a list of failures, because the
list has too many cases to keep straight. A record is written partway through
starting a watcher; some later failures keep it, so the watcher shows as
`failed`, and others roll it back deliberately, so that a half-built watcher
cannot be resumed as though it were whole. A watcher that has run successfully
**before** has a record on disk regardless, so the same fault can show as
`failed` on one machine and as nothing at all on another that has never got it
running.

Two consequences to hold on to:

* **No row does not mean "the start never got far".** It means there is nothing
  left to act on — no session, no watermark. The startup errors and the gateway
  log are where you find out what actually failed; `list` only shows what
  survived.
* **A `failed` row does not mean the failure was recent.** It may be a record
  from a boot weeks ago that has not started successfully since.

A failed watcher is retried on **every daemon start**. If the underlying problem
is unfixed it fails again and says so again — deliberately, so the gateway never
quietly settles for a broken watcher.

**To recover one:** `resume` or `reset` retries the start in place, which is
what you want when the fault was outside the gateway — a room that had gone, a
server that was down. **If the agent backend itself was unavailable, restart the
daemon instead:** agent availability is decided once at startup, and `resume`
and `reset` deliberately refuse rather than start a watcher whose permission
broker never came up.

To stop a watcher being retried at all, `pause` it; that is also how a watcher
with no record is kept from being started, since pausing one creates a paused
record.

The four states are:

| State | Meaning |
|---|---|
| `active` | A record exists and a processor is running for it |
| `failed` | A record exists and nothing is running — a start that got as far as writing the record and then raised. **The one state that means something is wrong**, so it is in the default view |
| `paused` | Muted by `pause`, waiting on a human decision — shown by default for that reason |
| `idle` | The gateway knows the room, but it was released on purpose and nothing is running |

`status` counts every state; `list` shows the three an operator is likely to
act on — everything except `idle` — unless asked otherwise.

### Direct Messaging

Send messages directly to a room (bypasses the agent):

```bash
# Send text directly
agent-chat-gateway send <room> "message text"

# Read from stdin
echo "Hello" | agent-chat-gateway send <room> -

# Send from file
agent-chat-gateway send <room> --file message.txt

# Attach files
agent-chat-gateway send <room> --attach document.pdf --file caption.txt

# Specify connector
agent-chat-gateway send <room> --connector rc-main "message"
```

### Setup

```bash
# Interactive setup wizard (creates config interactively)
agent-chat-gateway onboard [--repo-path PATH]

# Interactive config TUI — edit an existing config.yaml (see docs/config-tool.md)
agent-chat-gateway config

# Check for and install updates
agent-chat-gateway upgrade
```

---

## Role-Based Access Control

### Three Roles

**Owner** — Configured in `allowed_users.owners`
- Full tool access subject to `owner_allowed_tools` and permission approval
- Can approve or deny pending permission requests in chat
- Can use all agent features

**Guest** — Configured in `allowed_users.guests`
- Restricted to `guest_allowed_tools` only
- Tools not in the allow-list are auto-denied without owner notification
- Cannot approve/deny requests

**Anonymous** — Not in either list
- Messages are rejected entirely
- No access to the agent

### How It Works

Every message sent to the agent is prefixed with a trusted header. The exact format is
connector-specific (see `CLAUDE.md`'s connector prefix-format table for the authoritative
list) — Rocket.Chat's looks like:

```
[Rocket.Chat #<room> | from: <username> | role: owner|guest]  <message text>
```

and Mattermost's like:

```
[Mattermost #<channel> | from: <username> | role: owner|guest]  <message text>
```

The agent uses this header to determine:
1. Who sent the message
2. What tools they can use
3. Whether to require approval for sensitive operations

---

## User-Aware Responses

Every message the gateway forwards to the agent is prefixed with a trusted header
(format varies slightly by connector — see above):

```
[Rocket.Chat #general | from: alice | role: owner]  Hey, can you review this PR?
```

The `from: <username>` field tells the agent exactly who sent the message. Combined with a
**room profiles context file**, the agent can greet people by name, reply in their preferred
language, match their communication style, and adjust detail level based on their background —
automatically, for every message.

### How It Works

1. **The gateway injects** sender identity and role on every message (trusted, cannot be spoofed)
2. **The agent reads** the `from:` field to look up the sender's profile
3. **The agent personalizes** tone, language, and response style accordingly

The profile file is just a plain text context file you inject at session start — no code
changes needed.

### Example Profile File

Create a file like `contexts/rc-room-profiles.md` (you can copy the template from
[`contexts/rc-room-profiles.example.md`](../contexts/rc-room-profiles.example.md)):

```markdown
## Chat Room Profiles

**IMPORTANT — scope:** The profiles below apply **only** when interacting via the
gateway (i.e., when a trusted `[Rocket.Chat #<room> | ...]` or `[Mattermost #<channel> | ...]`
message prefix is present). Do NOT apply these profiles during CLI/terminal sessions.

Cross-reference the `from: <username>` field in the message prefix with the profiles
below to personalize your tone, language, and response style for each person in the room.

---

### alice
- **Display name:** Alice
- **Title:** Engineering Lead
- **Language:** English
- **Notes:** Prefers concise technical answers. Comfortable with code snippets.
  Appreciates bullet points over paragraphs.

### bob
- **Display name:** Bob
- **Title:** Product Manager
- **Language:** English
- **Notes:** Non-technical — avoid jargon, use plain language and analogies.
  Focuses on business impact, not implementation details.

### charlie
- **Display name:** Charlie
- **Language:** English / Traditional Chinese (reply in whichever language Charlie writes in)
- **Notes:** Guest role. Primarily asks questions about docs and project status.
  Keep responses factual; do not share internal system details.
```

Then add it to your config. `rc-gateway-context.md` belongs at the **connector level** so it
applies to every room automatically. Room profiles are **watcher-level** since each room has
its own set of people:

```yaml
connectors:
  - name: rc-main
    ...
    context_inject_files:
      - contexts/rc-gateway-context.md   # Gateway behavior rules — shared across all rooms

watcher_rules:
  - name: general
    connector: rc-main
    rooms:
      include: [general]
    agent: claude
    context_inject_files:
      - contexts/rc-room-profiles.md     # Room member profiles — specific to this room
```

Restart or reset the watcher to load the new context:

```bash
agent-chat-gateway reset rc-main:general
```

> **Tip:** `contexts/rc-gateway-context.md` (included in the repo) sets up baseline gateway
> behavior: message format parsing, injection protection, response length, and guest access
> rules. Placing it at the connector level ensures every room benefits from it without
> repeating it in each watcher.

---

## Permission Approval System

When `permissions.enabled: true` and a user attempts a tool call not in their allow-list, the gateway intercepts it and requires explicit approval from an owner.

### How It Works

1. **User sends message** → Agent attempts a tool call (e.g., `Bash`)
2. **Gateway intercepts** → Pauses the agent, posts approval request to chat:
   ```
   🔐 **Permission required** [a3k9]
   **Tool:** Bash
   **Params:** command='rm ./build'
   Reply: approve a3k9 / deny a3k9
   ```
3. **Owner responds** → Types `approve a3k9` or `deny a3k9` in the chat
4. **Gateway resolves** → Agent either executes or skips the tool call
5. **Message continues** → Agent response is posted back to the room

### Responding to Permission Requests

In the chat room, type directly (no slash prefix):

```
approve a3k9    ← Allow the tool call to proceed
deny a3k9       ← Block the tool call
```

**Important:** These are NOT slash commands. If you type `/approve`, the chat client itself
(Rocket.Chat or Mattermost — both reserve the `/` prefix for their own built-in commands)
will intercept it before it ever reaches the gateway.

### Error Messages

- **Invalid ID length** — `⚠️ Invalid ID — expected 4 characters`
- **Unknown request ID** — `⚠️ No pending permission request with ID`
- **Request timed out** — `⏱️ Permission a3k9 timed out — auto-denied.`

### Timeout Behavior

If no approval arrives within `permissions.timeout` seconds, the request is automatically denied:

```yaml
permissions:
  timeout: 300  # 5 minutes
```

### Message Queuing

While a permission request is pending, new messages are queued and will not be processed until the pending request is resolved. Only `approve`/`deny` commands bypass the queue.

### Disabling Approval for Owners

In sandbox environments where interactive approval isn't practical, you can skip owner approval:

```yaml
permissions:
  enabled: true
  skip_owner_approval: true  # ⚠️ WARNING: Disables human-in-the-loop for owners only
  timeout: 300
```

With `skip_owner_approval: true`:
- **Owners:** All tool calls are auto-approved (no RC notification)
- **Guests:** Still subject to `guest_allowed_tools` enforcement

Only use this in trusted, sandboxed environments where interactive approval is not feasible.

---

## Context Files

Context files are injected into the agent session to provide domain knowledge, system prompts, or other guidance. Three levels of context are supported:

ACG also injects built-in gateway context automatically. You do **not** need to list
`gateway/contexts/rc-gateway-context.md`, `gateway/contexts/mm-gateway-context.md`, or
other bundled context files in your config — the right one is chosen automatically based
on the connector's `type`. The built-in context teaches agents how to read trusted
message headers, roles, `to:` addressing, injection-protection rules, and gateway commands.

### Three-Level Injection

1. **Connector-level** (`connectors[].context_inject_files`) — Shared across all watchers on this connector
2. **Agent-level** (`agents[].context_inject_files`) — Applied to all sessions using this agent
3. **Watcher-level** (`watcher_rules[].context_inject_files`) — Specific to this watcher's session

Files are injected in this order, so watcher-level context overrides agent-level, which overrides connector-level.

### Built-in Tool Instructions and Lazy Loading

By default, agents receive a compact built-in tool index instead of the full scheduling
and history-fetching instructions. This keeps new sessions lighter while still making
the full docs available on demand:

```bash
agent-chat-gateway instructions scheduling
agent-chat-gateway instructions fetch-history
```

Agents should run the matching `instructions` command before using advanced gateway
commands such as `agent-chat-gateway schedule ...` or `agent-chat-gateway fetch-history ...`.
These read-only instruction commands are auto-approved for owners and guests.

If an agent backend performs better with all tool instructions present up front, set:

```yaml
agents:
  claude:
    lazy_instruction_loading: false
```

When disabled, ACG injects the full bundled scheduling and fetch-history context at
session start instead of the compact tool index.

### Example

```yaml
connectors:
  - name: rc-main
    context_inject_files:
      - docs/connector-context.txt   # Layer 1: shared context

agents:
  claude:
    context_inject_files:
      - docs/system-prompt.txt       # Layer 2: agent instructions

watcher_rules:
  - name: general
    connector: rc-main
    agent: claude
    rooms:
      include: [general]
    context_inject_files:
      - docs/domain-context.txt      # Layer 3: room-specific context
```

### Context File Format

Context files are plain text or Markdown. Include them as-is in the agent prompt:

```
# contexts/system-prompt.md
You are an assistant for our engineering team.
- Be concise in responses
- Prioritize code review over feature requests
- Reference our GitHub repo when suggesting changes
```

### Example: Room Member Profiles

A common pattern is to add a profiles context file that tells the agent who is in the room —
their display name, title, language preference, and communication style. This lets the agent
personalize its tone and language for each person automatically.

The repo ships with a ready-to-use template at
[`contexts/rc-room-profiles.example.md`](../contexts/rc-room-profiles.example.md).

To use it:

```bash
# Copy and customize the template
cp contexts/rc-room-profiles.example.md contexts/rc-room-profiles.md
# Edit rc-room-profiles.md with your team's actual profiles
```

Then reference it in your config. Built-in gateway behavior context is injected
automatically; room profiles are **watcher-level** because they are room-specific:

```yaml
connectors:
  - name: rc-main
    ...

watcher_rules:
  - name: general
    connector: rc-main
    rooms:
      include: [general]
    agent: claude
    context_inject_files:
      - contexts/rc-room-profiles.md     # Room member profiles — specific to this room
```

The built-in gateway context (matched to the watcher's connector type) sets up baseline gateway behavior automatically:
response length, message format parsing, `to:` addressing, injection protection, and
guest access rules. Use your own context files only for custom behavior such as room
member profiles, project knowledge, or team-specific tone.

### Limits

- **Per-file:** 256 KB maximum
- **Total:** 512 KB maximum across all three levels

If context files exceed the limit, the gateway will log a warning and skip the largest files.

---

## Attachment Handling

When users upload files (on Rocket.Chat or Mattermost), the gateway automatically downloads them and injects them into the agent prompt.

### Configuration

```yaml
connectors:
  - name: rc-main
    attachments:
      max_file_size_mb: 50          # Skip files larger than this
      download_timeout: 30           # Seconds to wait per download
      cache_dir_global: ~/.agent-chat-gateway/attachments  # preferred: connector-global cache
```

### What Happens

1. **User uploads file** to a watched room
2. **Gateway detects** attachment in incoming message
3. **Download file** from the chat platform (respects size limits and timeout)
4. **Cache locally** (prevents repeated downloads)
5. **Inject path** into agent prompt: `Attachment: /path/to/file`

### Supported File Types

All file types are supported. The gateway injects the file path into the prompt text, and the agent can read it using the `Read` tool if needed.

### Caching

Files are cached globally in the `cache_dir` and symlinked into each watcher's working directory. This prevents re-downloading the same file across multiple watchers.

---

## Sessions and State

### Automatic Sessions

By default, each watcher creates its own persistent session with the agent backend. The session ID is stored in `~/.agent-chat-gateway/state.<connector>.json` and reused across daemon restarts.

The session is reused only while the agent still resolves to the same **backend type and physical working directory** — the pair that scopes where the backend keeps its sessions. Change either one and the watcher starts a fresh session and logs why, rather than replaying an id into a store that never issued it, where it would find nothing or, worse, an unrelated session with the same id. The earlier conversation is not deleted; it stays in the backend it was created against. The gateway will not re-attach it, though — the state record now holds the new session, so changing the setting back starts a third session rather than returning to the first. Recovering that conversation means resuming it with the backend's own tooling (for Claude Code, `claude --resume <id>` from the original working directory), using the id from the log line that reported the change.

> ⚠️ **If `working_directory` points through a symlink** — a `current -> release-N`
> deploy link, say — repointing that link swaps the session too, even though
> `config.yaml` did not change. This is not incidental: a process launched there
> reports the physical path, so the backend genuinely keeps its sessions per target
> directory. Point `working_directory` at a stable path if you want conversations to
> survive a deploy.

No configuration is involved: a watcher's session id is assigned by the backend and
persisted by the gateway, never written by hand.

### Pinned Sessions Are Removed

`watchers[].session_id` used to tie a watcher to one specific backend session.
Setting it is now a config error. A pinned id names a session the backend is free to
expire (Claude Code's default `cleanupPeriodDays` is 30 days), after which the
watcher silently starts empty — and with watchers created per room, one id in config
cannot say which room it belongs to.

To carry context into a session, use a handoff file instead — see
[Use Case 3](#use-case-3--carry-an-existing-agent-sessions-context-into-chat).

### Resetting State

To clear a watcher's state and create a fresh session:

```bash
agent-chat-gateway reset <watcher-name>
```

This:
- Clears the stored session ID
- Starts a fresh session immediately (the watcher is restarted)
- Preserves the watcher's record and its frozen configuration
- Is refused while the watcher is paused — resume it first

### Viewing Runtime State

Runtime state is stored in `~/.agent-chat-gateway/`:

| File | Contents |
|---|---|
| `gateway.pid` | PID of the running daemon |
| `gateway.log` | Daemon log output |
| `control.sock` | Unix domain socket for CLI commands |
| `state.<connector>.json` | Persisted watcher definitions per connector |

Check the logs:

```bash
tail -f ~/.agent-chat-gateway/gateway.log
```

View persisted state:

```bash
cat ~/.agent-chat-gateway/state.rc-main.json | jq .
```

---

## Troubleshooting

### Gateway won't start

**Symptom:** `agent-chat-gateway start` returns immediately, `status` shows offline.

**Solution:**
1. Check the log: `tail ~/.agent-chat-gateway/gateway.log`
2. Verify Python 3.12+: `python3 --version`
3. Verify agent backends: `claude --version` and/or `opencode --version`
4. Validate config: `python3 -c "import yaml; yaml.safe_load(open('$HOME/.agent-chat-gateway/config.yaml'))" && echo OK`

### Agent not responding

**Symptom:** You message the bot but get no reply.

**Solution:**
1. Check status: `agent-chat-gateway status`
2. Check logs: `tail -f ~/.agent-chat-gateway/gateway.log`
3. Verify the bot account is a member of the watched room/channel (Mattermost also requires the bot to be a member of the `server.team` itself — `mmctl team add <team> <username>`)
4. Try the CLI: `agent-chat-gateway send <room> "test"` (should post immediately)
5. Verify agent backend: `claude -p` or `opencode run` (should start a session)

### Permission request hangs

**Symptom:** Permission request posted but owner response has no effect.

**Solution:**
1. Verify exact ID format (4 characters, e.g., `a3k9`)
2. Confirm you typed without a leading slash: `approve a3k9` (not `/approve a3k9`)
3. Check logs for errors: `tail ~/.agent-chat-gateway/gateway.log`
4. Wait for timeout if needed; request will auto-deny after `permissions.timeout` seconds

### High token usage

**Symptom:** Unexpectedly high Claude API costs.

**Solution:**
1. Check agent logs for repeated context injection: `grep "context_inject" ~/.agent-chat-gateway/gateway.log`
2. Reduce context file sizes (keep under 256 KB per file, 512 KB total)
3. Consider disabling context for specific watchers: set `context_inject_files: []`

### Connection failures

**Symptom:** `ERROR: RC websocket disconnected` (Rocket.Chat) or a WebSocket reconnect loop
(Mattermost) in logs, frequent reconnects.

**Solution:**
1. Verify the chat server is reachable: `curl https://chat.example.com/api/version` (Rocket.Chat) or `curl https://chat.example.com/api/v4/system/ping` (Mattermost)
2. Verify bot credentials: `username`/`password`, or Mattermost's `token`, in config
3. Check bot account has required permissions (Rocket.Chat admin panel) or team membership (Mattermost — see "Agent not responding" above)
4. Check network connectivity: `ping chat.example.com`
5. Review the chat server's logs for auth failures

### Files not downloading

**Symptom:** Attachments mentioned in messages but not passed to agent.

**Solution:**
1. Check max file size: `agent-chat-gateway` skips files larger than `attachments.max_file_size_mb`
2. Check timeout: increase `attachments.download_timeout` if slow network
3. Verify cache directory exists: `mkdir -p ~/.agent-chat-gateway/attachments`
4. Check logs: `grep "attach" ~/.agent-chat-gateway/gateway.log`

### Config validation errors

**Symptom:** `Error loading config.yaml: ...`

**Solution:**
1. Validate YAML syntax: `python3 -c "import yaml; yaml.safe_load(open('$HOME/.agent-chat-gateway/config.yaml'))"`
2. Check for missing required fields (see Configuration Reference above)
3. Verify all connector/agent references match defined names

---

## Advanced Topics

### Multi-Agent Setup

You can define multiple agents and route different watchers to different backends:

```yaml
agents:
  claude:
    type: claude
    command: claude
    working_directory: ~/.agent-chat-gateway/work
    timeout: 360
    permissions:
      enabled: true
      timeout: 300

  opencode:
    type: opencode
    command: opencode
    working_directory: ~/.agent-chat-gateway/opencode-work
    timeout: 360
    permissions:
      enabled: true
      timeout: 300

watcher_rules:
  # Each rule names its own connector and agent — neither has a default.
  - name: general
    connector: rc-main
    agent: claude          # General discussions
    rooms: {include: [general]}
  - name: development
    connector: rc-main
    agent: opencode        # Code development
    rooms: {include: [dev]}
  - name: research
    connector: rc-main
    agent: claude          # Research tasks
    rooms: {include: [research]}
```

### Multi-Connector Setup

Connectors are independent — run several instances of the same platform, or mix
platforms entirely, in one daemon. `connector` names in `watcher_rules` are what tie a
room/channel to a specific connector instance.

For teams using multiple Rocket.Chat servers or workspaces:

```yaml
connectors:
  - name: rc-company
    server:
      url: https://chat.company.com
      username: bot
      password: "${RC_PASSWORD_COMPANY}"
    allowed_users:
      owners:
        - alice
        - bob

  - name: rc-partner
    server:
      url: https://chat.partner.com
      username: bot
      password: "${RC_PASSWORD_PARTNER}"
    allowed_users:
      owners:
        - charlie

watcher_rules:
  - name: company-general
    connector: rc-company
    rooms:
      include: [general]
    agent: claude

  - name: partner-collab
    connector: rc-partner
    rooms:
      include: [general]
    agent: claude
```

Or mix Rocket.Chat and Mattermost in the same daemon — e.g. a team migrating between
platforms, or with different groups on each:

```yaml
connectors:
  - name: rc-main
    type: rocketchat
    server:
      url: https://chat.company.com
      username: bot
      password: "${RC_PASSWORD}"
    allowed_users:
      owners: [alice]

  - name: mm-main
    type: mattermost
    server:
      url: https://mattermost.company.com
      team: engineering
      token: "${MM_BOT_TOKEN}"
    allowed_users:
      owners: [bob]

watcher_rules:
  - name: rc-general
    connector: rc-main
    rooms:
      include: [general]
    agent: claude

  - name: mm-general
    connector: mm-main
    rooms:
      include: [town-square]
    agent: claude
```

### Tool Regex Patterns

Tool allow-list patterns use Python regex (fullmatch). Some examples:

```yaml
owner_allowed_tools:
  # Exact match
  - tool: "Read"

  # Pattern match
  - tool: "bash"
    params: "ls.*"

  # Multiple alternatives
  - tool: "Bash"
    params: "git (log|diff|status|show).*"

  # Match all in a namespace (MCP)
  - tool: "mcp__rocketchat__.*"
    params: ".*\"action\":\\s*\"(get|list)\".*"

  # Unsafe: match all tools (use with care!)
  - tool: ".*"
```

### Working Directory Isolation

Each agent's `working_directory` is where the agent subprocess runs. This isolates agent work:

```yaml
agents:
  claude:
    working_directory: ~/.agent-chat-gateway/claude-work
  opencode:
    working_directory: /data/agent-sessions/opencode
```

The gateway ensures the directory exists and uses it as the agent's current working directory (`cwd`).

### Debugging Mode

The gateway logs at INFO level by default. To see detailed output, tail the log file while the daemon is running:

```bash
tail -f ~/.agent-chat-gateway/gateway.log
```

---

## Getting Help

- **Documentation:** See [install-agent.md](install-agent.md) for installation details
- **Logs:** `tail -f ~/.agent-chat-gateway/gateway.log`
- **GitHub:** https://github.com/HammerMei/agent-chat-gateway/issues
- **Community:** Discuss on Anthropic's community forum

---

## Summary

`agent-chat-gateway` provides a flexible, secure bridge from your chat platform (Rocket.Chat or Mattermost) to AI agents. Start with a minimal config, use role-based access control to grant appropriate permissions, and leverage the permission approval system to ensure human oversight of sensitive operations.

For production deployments, carefully review your tool allow-lists, set appropriate timeouts, and monitor your logs for errors and token usage.

Happy chatting!
