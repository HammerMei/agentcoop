# Supported Features & Roadmap

This document clearly communicates what agent-chat-gateway supports today, what is known to be limited, and what is planned for future releases.

---

## Currently Supported Features

### Chat Platform Connectors

#### Rocket.Chat
- ✅ **Message routing** via DDP WebSocket protocol
  - Real-time message subscriptions per watched room
  - Automatic reconnect with exponential backoff
  - Per-room message deduplication (watermark-based)
  - Multiple concurrent rooms per connector
  - Multiple Rocket.Chat instances (multi-connector setup)

- ✅ **Message triggering**
  - Direct message (DM) activation — all DMs to bot are forwarded to agent
  - Channel/group activation — requires `@mention` of bot username
  - Room-wide `@all` activation — treated as explicit permission for broader multi-agent fan-out

- ✅ **Attachments**
  - Inbound attachment download (files, images, documents)
  - File size and timeout limits enforced
  - Attachment metadata injected into agent prompt as text context
  - Multiple attachments per message supported

- ✅ **Typing & Status Indicators**
  - Typing indicator while agent processes message
  - Online/offline notifications per watcher (optional)
  - Configurable notification suppression per watcher

- ✅ **Multi-connector support**
  - Run multiple Rocket.Chat instances simultaneously
  - Each with independent connector config, roles, and watchers

---

#### Mattermost
- ✅ **Message routing** via the Mattermost Realtime API (WebSocket)
  - One authenticated connection streams every channel the bot is a member of
    (no per-channel subscribe/unsubscribe handshake, unlike Rocket.Chat's DDP)
  - Automatic reconnect with exponential backoff, followed by REST history
    replay of messages missed during the outage
  - Per-channel message deduplication (watermark-based)
  - Multiple concurrent channels per connector

- ✅ **Authentication** — dual mode, configured per connector instance:
  - Personal Access Token / Bot Account access token (no login call, no
    expiry/re-login logic needed)
  - Username + password session login, with automatic re-login on token expiry

- ✅ **Message triggering**
  - Direct message (DM) activation — all DMs to bot are forwarded to agent
  - Channel activation — requires `@mention` of bot username, checked against
    the server-computed mentions list on the live WebSocket event (not a text
    regex — more robust than pattern-matching the message body)
  - Channel-wide `@channel`/`@all`/`@here` activation — treated as explicit
    permission for broader multi-agent fan-out

- ✅ **Attachments**
  - Inbound attachment download (files, images, documents)
  - File size and timeout limits enforced
  - Multiple attachments per message supported

- ✅ **Typing & Status Indicators**
  - Typing indicator while agent processes message (WebSocket `user_typing` action)
  - Online/offline notifications per watcher (optional)

- ✅ **Multi-agent / agent-chain support**
  - Shared turn-budget loop protection with Rocket.Chat (same underlying
    `TurnStore`), so two ACG agents in the same channel can converse without
    looping forever

- ⚠️ **Team-scoped**: one connector instance serves exactly one Mattermost
  team (channels are team-scoped on Mattermost, unlike Rocket.Chat). A
  multi-team deployment runs one connector instance per team.

---

### Voice Gateway (Experimental) 🧪

A lightweight HTTP endpoint that turns any ACG-connected agent into a voice assistant
accessible from Siri via iOS Shortcuts — no custom hardware, no wake word infrastructure.

```
"Hey Siri, run Ask 老妹"
    ↓
iOS Shortcut: Dictate Text
    ↓
POST /ask/<room>   ←  VoiceConnector
    ↓
Agent processes
    ↓
Plain-text reply returned
    ↓
Speak Text (iOS TTS)
```

**Config:**
```yaml
connectors:
  - name: siri-voice
    type: voice
    port: 8765
    secret: "$VOICE_SECRET"

watcher_rules:
  - name: siri-watcher
    connector: siri-voice
    rooms:
      include: [voice-room]    # literal (no stream to match patterns against)
                               # → POST /ask/voice-room
    agent: my-agent
    context_inject_files:
      - gateway/contexts/voice-context.md
```

#### Supported

- ✅ **Plain-text HTTP endpoint** — `POST /ask/<room>` returns plain-text; room maps directly to watcher `room:` config
- ✅ **Path-based room routing** — one port, N agents: `/ask/laomei`, `/ask/xiaomei`, etc.
- ✅ **JSON and plain-text body** — accepts both (iOS Shortcuts has no plain-text body option; use JSON `{"text": "..."}`)
- ✅ **Bearer token auth** — constant-time `hmac.compare_digest` comparison
- ✅ **Per-room serialization** — same-room requests serialized; different rooms run concurrently
- ✅ **Voice-safe replies** — `gateway/contexts/voice-context.md` enforces plain text, no markdown, no emoji
- ✅ **Zero new dependencies** — stdlib `asyncio.start_server` only

#### Configuration notes

- ⚠️ **Requires `skip_owner_approval: true`** (or `permissions.enabled: false`) — there is no human in the loop to approve tool requests over a voice channel. Document this in your config; the gateway logs a warning if a permission notification is received on a voice room.
- ⚠️ **Network security** — binds to `0.0.0.0` by default; gate at the network level (VPN / firewall) in addition to the bearer token.

#### Known limitations

- 🔶 **Subprocess latency** — each query spawns a new `claude -p` process (~0.5–2 s overhead); a persistent-session backend (e.g. `poor-claude`) would eliminate this
- 🔶 **Cross-request reply mixup on timeout** — if a request times out and the agent turn finishes late, the late reply may be delivered to the next request; root cause requires per-dispatch queue correlation (deferred; narrow window for sequential Siri use)
- 🔶 **Unbounded room map** — `_rooms` dict grows one entry per distinct room name ever POSTed; no eviction (negligible in practice, more relevant when `secret` is unset)

---

### Agent Backends

#### Claude CLI Backend (`claude`)
- ✅ Session creation and persistent conversation history
- ✅ Message sending with `--output-format stream-json`
- ✅ Tool calling via PreToolUse hook for permission approval integration
- ✅ Attachment context injection (as text references in prompt)
- ✅ Timeout enforcement per message
- ✅ Response streaming and completion detection

#### OpenCode CLI Backend (`opencode`)
- ✅ Session creation and persistent conversation history
- ✅ HTTP API message sending
- ✅ Tool calling via SSE `permission.asked` event for approval integration
- ✅ Attachment context injection (as text references in prompt)
- ✅ Per-message environment variable overrides
- ✅ Rate limit detection and reporting
- ✅ Server recovery on reconnect

#### Backend Behavior
- ✅ Normalized response format across backends
- ✅ Explicit session lifecycle (create, send, reset)
- ✅ Non-empty response guarantee (placeholder message if needed)
- ✅ Structured error reporting

---

### Session Management

#### Persistence & Recovery
- ✅ Persistent watcher state across daemon restarts (`state.json`)
- ✅ Auto-created session IDs retained across restarts
- ❌ Pinning a session ID from config (`session_id` on a watcher rule) — removed;
  setting it is a load error, reported as an unrecognised key like any other.
  Carry context into a session with a handoff file instead
  (`context_inject_files`), which also survives the backend expiring the session
- ✅ Graceful recovery from corrupted state files

#### Session Operations
- ✅ Multiple rooms per session (session reuse across different chat rooms)
- ✅ Per-room message queue (serial processing, no race conditions)
- ✅ Queue depth limiting with graceful backpressure rejection
- ✅ Watcher pause/resume (temporarily pause agent invocation)
- ✅ Session reset (clear conversation history, start fresh)

#### Programmatic Access
- ✅ `AgentSession` — lightweight async context manager for scripting
- ✅ `ScriptConnector` — in-memory connector for agent-to-agent pipelines
- ✅ Agent-to-agent piping via `pipe_to()` method
- ✅ Explicit session lifecycle boundaries
- ✅ Attachment support in programmatic sends

---

### Role-Based Access Control (RBAC)

#### Roles
- ✅ **OWNER** — Full tool access (subject to optional approval)
- ✅ **GUEST** — Limited tool access (only tools in guest allow-list)
- ✅ **ANONYMOUS** — No agent access (messages rejected)

#### Configuration
- ✅ Per-connector owners/guests list (user ID-based)
- ✅ Tool allow-lists per role (regex-based matching)
- ✅ Parameter-based tool matching (path normalization, regex patterns)
- ✅ File path normalization (prevents `../` bypass attacks)
- ✅ Case-insensitive tool name matching where applicable

#### Enforcement
- ✅ Role resolved from trusted connector context (not from message text)
- ✅ Bash command parsing via tree-sitter AST (secure, not string split)
- ✅ Automatic guest tool rejection (no owner notification for guest denials)
- ✅ Owner tool matching checked against allow-list

---

### Human-in-the-Loop Permission Approval

#### Approval Workflow
- ✅ Automatic triggering when tool call matches neither owner nor guest allow-lists
- ✅ Permission request visible in chat (Rocket.Chat notification)
- ✅ 4-character approval ID system (`approve a3k9` / `deny a3k9`)
- ✅ Case-insensitive approval ID matching
- ✅ Chat-based approval commands intercepted (not forwarded to agent)

#### Configuration
- ✅ Global permission timeout (auto-deny if owner doesn't respond)
- ✅ Per-request timeout enforcement
- ✅ Auto-approval for tools matching owner allow-lists
- ✅ `skip_owner_approval` option for fully-trusted environments (sandbox mode)
- ✅ Owner-only access to approve/deny commands

#### Queueing & Pause
- ✅ Message queue pauses while approval pending
- ✅ Auto-denial on timeout with visible notification
- ✅ Multiple pending approvals supported (per session)

#### Backend Integration
- ✅ Claude CLI backend via HTTP PreToolUse hook
- ✅ OpenCode backend via SSE `permission.asked` event and reply API

---

### Context Injection

#### File-based Context
- ✅ Three-layer context system
  - Connector-level context (shared across all watchers)
  - Agent-level context (per agent backend)
  - Watcher-level context (per specific room/watcher)

#### Behavior
- ✅ Injected on session start (one-time, not per-message)
- ✅ Built-in Rocket.Chat gateway context injected automatically
- ✅ Lazy instruction loading for bundled scheduling/history docs via `agent-chat-gateway instructions ...`
- ✅ 256 KB per file limit
- ✅ 512 KB total context limit
- ✅ Multiple context files supported (concatenated)

---

### CLI Operations

#### Daemon Lifecycle
- ✅ `start` — Start daemon in background
- ✅ `stop` — Graceful shutdown
- ✅ `restart` — Restart daemon
- ✅ `status` — Check if daemon is running; shows the active config digest,
  when it was loaded, and any section a `config reload` left degraded
- ✅ `config reload [--dry-run] [--json]` — apply `config.yaml` changes to the
  running daemon: validate the whole file, diff against the active config,
  restart only the affected connectors and agents, reconcile every record
  (sessions kept where the rule changed, expired with the id logged where no
  rule covers them). Exit 0/1/2 = applied or no-op / refused / degraded.
  With the daemon stopped, `--dry-run` prints the next start's plan
- ✅ `config show [--json]` — SHA-256 digest of the resolved config plus its
  flattened, secret-redacted contents; warns when the running daemon differs

#### Watcher Control
- ✅ `list` — List watcher records and their state; everything but `idle` by
  default (`--active/--idle/--paused/--failed/--all`; multi-connector aggregation)
- ✅ `pause <watcher>` — Pause watcher (stop processing messages)
- ✅ `resume <watcher>` — Resume paused watcher
- ✅ `reset <watcher>` — Clear session state

#### Direct Operations
- ✅ `send <room> <text>` — Send text message to room
- ✅ `send <room> --file <path>` — Send file/attachment to room
- ✅ `send <room> -` — Send stdin content to room (pass `-` as the message argument)
- ✅ Combined text + attachment sends

#### Configuration & Upgrade
- ✅ Interactive onboard wizard (first-run setup)
- ✅ Self-upgrade via CLI command

---

### Configuration

#### Features
- ✅ YAML configuration file
- ✅ Secrets stored directly in `config.yaml` (chmod'd `0600` automatically —
  both by the config TUI and by `agent-chat-gateway start`)
- ✅ Auto-migration: a legacy `.env`-backed config (`$VAR`/`${VAR}` references
  resolved from a colocated `.env` file) is folded into `config.yaml` as
  literal values on first start (or before the config TUI opens), then
  `.env` is removed (one-time; also available as `agent-chat-gateway config
  migrate-env` for a manual run). After migration — or for any config
  written from scratch — `$VAR`/`${VAR}` is not a recognized syntax; a value
  that merely looks like one is a plain string, used as written.
- ✅ Multi-connector setup (multiple chat instances)
- ✅ Multi-agent setup (different agents per watcher)
- ✅ Cross-field validation (e.g., agent timeout > permission timeout)
- ✅ Relative path resolution (relative to config file location)
- ✅ `connector_templates` / `agent_templates` / `watcher_templates` — named,
  reusable field blocks; an entry opts in via its own `inherits: <name>`
  field (v0.3; see `docs/migration-0.3.md`). A leftover pre-v0.3
  `connector_defaults:`/`agent_defaults:`/`watcher_defaults:` key is a hard
  load-time error, not silently ignored
- ✅ `tool_presets` — named, reusable tool-rule lists referenced by name from
  `owner_allowed_tools` / `guest_allowed_tools`
- ✅ Watcher `rooms: [a, b, ...]` — one connector+agent pair expands into one
  watcher per room, with an auto-derived name (`<connector>-<room>`)
- ✅ JSON Schema (`gateway/schema/config.schema.json`) for editor
  autocomplete and inline typo-checking

#### Configuration Validation
- ✅ Connector names must be unique
- ✅ Watcher names must be unique (including names auto-derived from `rooms:`)
- ✅ Watchers must reference existing connectors and agents
- ✅ Default agent must reference existing agent (if specified)
- ✅ Required paths must exist at validation time
- ✅ Queue depth settings reject invalid values
- ✅ A `watchers[].session_id` left over from an earlier version is rejected with the
  replacement named, never silently ignored (the cross-watcher uniqueness check it
  used to need is gone with the field)
- ✅ `*_defaults` blocks reject identity fields (`name`, `room`/`rooms`) that must be
  set per-entry, not inherited — and the removed `session_id`, reported as removed
  rather than as "set it per-entry"
- ✅ `tool_presets` are regex-validated eagerly at load, even if unused
- ✅ `agent-chat-gateway config validate [--lint] [--json]` — checks config.yaml
  without starting the daemon: structural validation, per-connector-type
  credential checks (e.g. empty Rocket.Chat/Mattermost `server:` fields, or
  a `server.url` that doesn't look like a URL — a lenient scheme+netloc
  check, so it catches plain typos without rejecting unusual schemes/ports),
  and a warning when persisted `state.<connector>.json` references a watcher
  name no longer in the config

---

### Testing & Scripting

#### Unit & Integration Tests
- ✅ Comprehensive test suite covering core functionality
- ✅ Unit tests for connector, permission, session management
- ✅ Integration tests for multi-component workflows

#### Scripting APIs
- ✅ `AgentSession` — Direct session management without connectors
- ✅ `ScriptConnector` — In-memory connector for testing and automation
- ✅ Agent-to-agent piping for multi-stage workflows

---

## Known Limitations & Constraints

### Message Delivery

- 🔶 **No zero-loss guarantee under sustained overload.** ACG applies backpressure: when
  every processor queue for a room is full, an inbound message is refused rather than
  queued without bound. A refused message is normally recoverable — its dedup id is
  forgotten and a mark is left below it so the next reconnect re-fetches it — but that
  mark lives only in memory. If the gateway restarts *before* the next reconnect, the
  message is not recovered and nothing reports it.
  - Requires all queues full **and** a restart before the next socket reconnect, so it
    needs sustained overload rather than a burst.
  - A live message refused by the capacity preflight still gets a "server busy" reply, so
    the sender knows to resend. A message refused while replaying a reconnect window does
    not — 200 such replies would be worse than the loss.
  - **Deliberate.** Persisting the mark was assessed and declined: guaranteeing delivery
    through overload is not a goal ACG trades complexity for. Both chat connectors behave
    the same way here.

### Platform Support

- ❌ **No Slack, Discord, Microsoft Teams, or WhatsApp connectors**
  - Rocket.Chat and Mattermost are the production-ready chat connectors
  - Webhook-based (push) connectors not yet implemented
  - Both chat connectors are pull-based (persistent WebSocket)
  - Voice gateway connector is experimental — see [Voice Gateway](#voice-gateway-experimental-) section
  - Mattermost's onboarding CLI wizard (`agent-chat-gateway onboard`) and a
    real E2E docker test harness are not yet implemented (config.yaml must be
    hand-written for now) — planned as a follow-up

### Agent Backends

- ❌ **Attachment handling**: Files injected as text references only
  - No native binary attachment passing to agent
  - Both Claude CLI and OpenCode backends affected
  - Agent receives attachment as text context, not as file blob

- ❌ **No direct Anthropic API integration**
  - Claude backend requires Claude CLI subprocess
  - No library-level API integration

- ❌ **Response streaming**: Responses posted to chat only after agent completes full turn
  - No streaming message updates
  - User sees final response once, not incremental chunks

---

### Rocket.Chat Specific

- ❌ **Message character limit**: Rocket.Chat 4,000 character limit
  - Long responses automatically chunked
  - Very long messages may split mid-sentence (no intelligent wrapping)

- ✅ **Thread replies** — configurable via `reply_in_thread` (default: false) and
  `permission_reply_in_thread` (default: true for approval notifications)

- ❌ **Slash command conflict**: Permission approve/deny cannot use `/` prefix
  - Rocket.Chat intercepts `/` commands
  - Workaround: use `approve` and `deny` without prefix

---

### Mattermost Specific

- ⚠️ **History pagination is best-effort, not exact**: Mattermost's channel
  history API pages by post ID, not timestamp — there is no direct equivalent
  of Rocket.Chat's `latest`/`oldest` ISO-timestamp parameters. `before_ts`/
  `after_ts` are applied as a client-side filter over the most recent page of
  results rather than true server-side pagination; very deep history lookups
  may not reach far enough back.

- ⚠️ **Reconnect-replay mention detection is text-based only**: Mattermost's
  REST history API returns bare Post objects with no mention data at all (the
  `mentions` field only exists as a live WebSocket notification-time
  computation, not part of the stored Post). Messages replayed after a
  reconnect are matched against the bot's username via text regex instead —
  this only detects a mention of the bot itself, not other agents mentioned
  in the same message, so the `to:` field is less complete for replayed
  messages than for live ones.

- ⚠️ **`@channel`/`@all`/`@here` mention-gate bypass is possible for
  already-allow-listed senders** (found in code review): Mattermost gives no
  ID-based/trusted signal for these special mention keywords at all (unlike a
  real `@botname` mention, which is checked against the server-computed
  `mentions` ID array). Detection falls back to a text regex over the raw
  message body, so any sender already in the owner/guest allow-list can type
  the literal string `@channel` to satisfy the `require_mention` gate and
  make peer agents see `to: @all`, regardless of whether Mattermost actually
  delivered a real channel-wide notification. This does **not** allow a
  sender outside the allow-list in, and does not break the trusted
  `format_prompt_prefix` header — it only weakens the require_mention gate's
  integrity for senders already trusted enough to talk to the bot. No better
  technical fix exists without Mattermost exposing a trusted signal for these
  keywords; see `gateway/connectors/mattermost/mentions.py`'s SECURITY NOTE.

- ❌ **Slash command conflict**: same as Rocket.Chat — permission approve/deny
  cannot use `/` prefix; use `approve`/`deny` without it.

---

### Security & Sandbox

- ❌ **End-to-end encryption**: Session state not encrypted at rest
  - Persisted state readable by any process with file access

- ❌ **Sandbox enforcement separation**: Claude Bash tool sandbox is independent
  - Permission approval and Claude Code sandbox are separate systems
  - Approved commands may still be blocked by Claude Code's native sandbox

---

### Configuration & Operations

- ✅ **Config reload** (`config reload`): applies `config.yaml` to the running
  daemon, restarting only what changed — see `docs/design/config-reload-design.md`
  - Operator-triggered only: no file watcher, no SIGHUP, no TUI-save trigger
  - Messages arriving on a connector while it restarts may be lost, as for any
    connector restart; a degraded section is retried by the next reload, not on
    its own

- ❌ **Web UI**: No monitoring or configuration dashboard
  - CLI-only operations

- ❌ **Distributed deployment**: Single-process only
  - No multi-node or horizontal scaling
  - Cannot run multiple daemon instances on same config

---

### Observability

- ❌ **Structured logging**: Not implemented (text logs only)
- ❌ **Metrics/observability**: No Prometheus endpoint or metrics collection
- ❌ **Audit logging**: No dedicated audit trail for permission approvals or sensitive operations

---

## Roadmap (Planned & Under Consideration)

### High Priority (Planned Next)

#### Additional Chat Connectors
- 🔄 **Slack connector** — Real-time message routing via Slack API/WebSocket
- 🔄 **Discord connector** — Message routing via Discord API
- 🔄 **Generic webhook connector** — Support push-based events from any platform

#### Operational Features
- 🔄 **Structured logging** — JSON-formatted logs with machine parsing
- 🔄 **Metrics endpoint** — Prometheus-compatible metrics (message count, latency, errors)

---

### Under Consideration (Future)

#### Advanced Features
- 💡 **Persistent memory across sessions** — Agent remembers past conversations (conversation history)
- 💡 **Heartbeat / proactive agent** — Agent can send unprompted messages to chat
- 💡 **Web UI** — Dashboard for monitoring, configuration, and approval management
- 💡 **Multiple agent sessions per room** — Fan-out to multiple agents simultaneously
- 💡 **Message filtering plugins** — User-defined message preprocessing/filtering

#### Scalability
- 💡 **Multi-node support** — Horizontal scaling (multiple daemons, shared state)
- 💡 **Message broker integration** — Redis/RabbitMQ for distributed queue management

#### Security & Privacy
- 💡 **End-to-end encryption** — Encrypt persisted session state
- 💡 **Audit logging** — Dedicated audit trail for sensitive operations
- 💡 **Role-based API access** — HTTP API with RBAC for external tools

---

## Feature Stability

| Feature | Stability | Notes |
|---------|-----------|-------|
| Rocket.Chat connector | Stable | Production-ready |
| Claude CLI backend | Stable | Production-ready |
| OpenCode backend | Stable | Production-ready |
| Permission approval system | Stable | Production-ready |
| RBAC and tool allow-lists | Stable | Production-ready |
| Context injection | Stable | Production-ready |
| CLI operations | Stable | Production-ready |
| Persistence & recovery | Stable | Production-ready |
| Scripting API | Stable | Stable for scripting |
| Voice gateway connector | **Experimental** | POC-quality; sequential Siri use; known timeout race |

---

## How to Request Features

If you'd like to see a feature implemented:

1. **Check this document** — Verify it's not already planned
2. **Search issues** — Look for existing feature requests on GitHub
3. **Open a discussion** — Start a GitHub discussion to gauge community interest
4. **Submit an issue** — File a feature request with your use case and motivation

For security-related features or constraints, please contact the maintainers privately via the security reporting process.
