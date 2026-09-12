# AgentCoop: Functional Specification

## 1. Purpose and Scope

The `coop` daemon bridges messaging platforms (Rocket.Chat and others) to persistent AI agent sessions. The system accepts user messages from monitored chat rooms or direct messages, forwards eligible messages to a configured agent backend, and posts the agent's response back to the originating chat destination.

**Scope:** This specification defines externally observable behavior and user-facing requirements. It intentionally avoids internal architecture and implementation details except where necessary to clarify requirements.

**High-level goals:**
- Accept chat messages from one or more messaging platforms
- Maintain persistent agent sessions keyed by chat room or conversation
- Apply role-based access control to enforce tool usage policies
- Provide a CLI interface for operational control
- Persist watcher state across daemon restarts

---

## 2. Core Behavioral Requirements

### 2.1 Message Routing

The gateway SHALL:
1. Watch one or more configured chat rooms or direct message conversations (watchers)
2. When an authorized user sends a qualifying message, forward that message to the configured agent session
3. Post the agent response back to the same room or conversation context
4. Process messages for each watched room sequentially to preserve order
5. Maintain persistent sessions such that later messages in the same watcher continue the same agent conversation unless explicitly reset

### 2.2 Message Eligibility

The gateway SHALL:
1. Apply sender allow-list rules before forwarding a message to the agent
2. In Rocket.Chat channels or groups, require the bot to be mentioned (via `@mention`) before treating a message as intended for the gateway
3. In direct messages, NOT require a mention
4. Reject messages from anonymous users without forwarding
5. Resolve sender role from the trusted connector context, NOT from user-provided message content

### 2.3 Message Content and Attachments

The gateway SHALL:
1. Support inbound messages with text, file attachments, or both
2. Download accepted file attachments before invoking the agent
3. When a message contains only attachments with no text, still provide usable attachment context to the agent
4. When a message contains neither usable text nor usable attachment context, still produce a non-empty placeholder message for the agent
5. Enforce configured attachment download limits (file size, timeout)

### 2.4 Reply Delivery

The gateway SHALL:
1. Send normal agent replies as chat messages
2. When thread reply behavior is enabled by configuration, deliver replies in the configured thread mode
3. When online/offline notifications are enabled by configuration, post those notifications to the monitored room
4. When a watcher-specific notification is set to null, suppress that notification

---

## 3. Watcher Lifecycle

### 3.1 Watcher Definition and Startup

A watcher SHALL:
1. Bind exactly one chat room or conversation (in one connector) to exactly one agent configuration
2. Have a unique name within its connector scope
3. Start automatically when the daemon starts
4. Persist its runtime state across daemon restarts

### 3.2 Session Identity

The gateway SHALL:
1. Assign every watcher's session identity itself — from the agent backend on first
   start, or by reusing the persisted one. Session IDs SHALL NOT be configurable:
   `session_id` is removed from a watcher rule, and setting it SHALL be a load
   error. It needs no message of its own: a rule accepts a closed set of keys, so
   an unrecognised one is already refused and the error lists the keys that are
   valid (see 8.4)
2. Persist that session identity across daemon restarts so the same agent
   conversation continues
3. When a watcher is reset, clear the stored session identity and create a fresh
   session on the next message. There is no exemption from reset: the "fixed session
   IDs preserved across reset" rule went with the removed field
4. Support carrying context into a new session by file — an operator MAY have the
   agent summarise a session to a file and list it in `context_inject_files`. This
   replaces session pinning and, unlike it, survives the backend expiring the session
   it was written from

### 3.3 Watcher State Persistence

The gateway SHALL:
1. Persist watcher runtime state across restarts, including at minimum the active session identity and paused/unpaused status
2. Recover gracefully from missing or corrupted state files rather than crashing — such a file carries no recoverable state either way, so refusing to start over it would trade a graceful degradation for an outage
3. Mark the persisted format with a version, and **refuse to start** on a state file whose version it cannot read, naming the file and the upgrade procedure. This is deliberately not case 2: a readable file in an older format holds real sessions, and reading it as empty would abandon them while looking like a successful boot. There SHALL be no automatic conversion — see docs/design/dynamic-watcher-design.md §5.3 for why one cannot be written honestly
4. Report that refusal from `config validate` as an error rather than skipping it, since that command is what an operator runs before starting the gateway

---

## 4. CLI Interface Requirements

### 4.1 Operational Commands

The gateway SHALL support the following commands via the `coop` CLI:

| Command | Purpose |
|---------|---------|
| `start [--config FILE]` | Start the daemon service |
| `stop` | Stop the daemon service |
| `restart [--config FILE]` | Restart the daemon service |
| `status` | Show daemon status (running/not running, uptime, watcher count) |
| `list [--connector NAME]` | List all watchers and their status |
| `pause WATCHER [--connector NAME]` | Pause a watcher (stops processing messages) |
| `resume WATCHER [--connector NAME]` | Resume a paused watcher |
| `reset WATCHER [--connector NAME]` | Reset a watcher session and runtime state |
| `send ROOM [MESSAGE] [--file FILE] [--attach FILE] [--connector NAME]` | Send a message or file to a room |

### 4.2 Operational Behavior

The gateway SHALL:
1. Report watchers with their status (active/inactive/paused), agent assignment, session ID, and connector
2. When paused, stop a watcher from processing new messages until resumed
3. When resumed, re-enable message processing for a paused watcher
4. When reset, clear runtime session state according to the session identity rules in Section 3
5. When status is requested, indicate whether the daemon is running and display uptime if running
6. When a command requires a running daemon and the daemon is not running, fail clearly with a non-success exit code

### 4.3 Multi-Connector Support

When multiple connectors are configured, the gateway SHALL:
1. Allow the `list` command to show watchers across all connectors
2. Allow selective listing by connector with the `--connector` flag
3. Resolve commands that name a **watcher** without needing `--connector` at all — watcher names are unique across connectors, so `pause`/`resume`/`reset`/`expire` find their connector from the name
4. For commands that name a **room** rather than a watcher (`send`), accept a `--connector` flag to disambiguate — optional when exactly one connector is configured, **required** when there are several, since the room name alone does not identify one and the daemon refuses to guess
5. Return partial results with per-connector errors when some connectors fail during aggregated operations

---

## 5. Direct Message and File Upload

The gateway SHALL support sending messages and files to chat rooms outside the normal watcher flow:

1. Users SHALL be able to send a text message to a room via the `send` command
2. Users SHALL be able to upload a file with an optional caption via the `send` command with `--attach`
3. A send operation MAY include text, a file, or both
4. The system SHALL accept room identifiers in the formats supported by the configured connector (e.g., `#channel`, `@username`, room ID)
5. The system SHALL validate that local files exist before attempting to send or upload
6. The system SHALL reject conflicting input modes (e.g., inline text and `--file` simultaneously)
7. The system SHALL reject a send request that contains neither message content nor a file attachment

---

## 6. Role-Based Access Control (RBAC)

### 6.1 Roles and Assignment

The gateway SHALL support at least two distinct roles for users:

| Role | Assignment | Purpose |
|------|-----------|---------|
| Owner | Assigned by connector from trusted platform identity | Administrator; may use pre-approved tools and manage permissions |
| Guest | Assigned by connector from trusted platform identity | Limited user; restricted to configured allowed tools only |

Additionally, the gateway SHALL:
1. Recognize an anonymous role for unauthenticated users and reject their messages
2. Assign sender role from the trusted connector context, NOT from user-provided message content

### 6.2 Owner Tool Access

When an owner uses a tool:
1. If the tool is in the owner's auto-approved allow-list, the tool executes without further approval
2. If permissions are enabled and the tool is NOT in the auto-approved allow-list, the tool enters the human approval workflow
3. If permissions are disabled, all tools are auto-approved for owners

### 6.3 Guest Tool Access

When a guest uses a tool:
1. The tool MAY execute only if it matches the guest allow-list policy
2. Tools outside the guest allow-list SHALL be denied automatically
3. Denied tool attempts SHALL NOT enter the human approval workflow (fail immediately)

### 6.4 Tool Matching Rules

Tool allow-list policies SHALL support:
1. Matching by tool name (required)
2. Optional parameter-based matching to further restrict tool usage
3. Case-insensitive tool name matching where required by tool ecosystems
4. Full-pattern parameter matching (not loose substring matching)
5. Normalized file paths so equivalent spellings do not bypass policy
6. For tools with multiple extracted parameters, all extracted values MUST satisfy the configured allow policy

---

## 7. Human Approval Workflow

### 7.1 Triggering Approval

When permissions are enabled, the gateway SHALL:
1. Intercept tool actions by owners that fall outside the auto-approved set
2. Post an approval request visible in chat to eligible owners (other owners in the same room)
3. Include a short, human-typable approval ID in the request message

### 7.2 Approval Commands

The gateway SHALL support two approval commands in chat:
- `approve ID` — approve a pending tool request
- `deny ID` — deny a pending tool request

The gateway SHALL:
1. Intercept these commands and NOT forward them to the agent as normal chat input
2. Support case-insensitive approval ID matching
3. Return a validation error if the approval ID format is invalid
4. Return a clear error if the supplied approval ID does not correspond to a pending request
5. Enforce owner-only access to approval commands

### 7.3 Approval Timeout and Queueing

The gateway SHALL:
1. Expire each approval request after the configured permission timeout
2. Auto-deny expired requests and generate a visible timeout notification
3. While an approval is pending for a watcher/session, queue later user messages for that watcher until the approval is resolved

---

## 8. Configuration Requirements

### 8.1 Configuration Structure

The gateway SHALL require:
1. At least one connector
2. At least one agent backend
3. At least one watcher

The gateway SHALL validate that:
1. Connector names are unique
2. Watcher names are unique within their connector scope
3. Each watcher references an existing connector
4. Each watcher references an existing agent
5. If a default agent is specified, it references an existing agent

### 8.2 Path and Environment Handling

The gateway SHALL:
1. Require that path-based configuration values (e.g., working directories) exist at validation time
2. Resolve relative paths relative to the configuration file location
3. Store secrets directly in the configuration file, restricting its permissions (`chmod 0600`) whenever the gateway or its config tool writes it
4. NOT expand `$VARIABLE`/`${VARIABLE}` references in configuration values — a value that happens to look like such a reference is used as a plain literal string, never resolved
5. Automatically migrate a legacy `.env`-backed configuration (one using `$VARIABLE`/`${VARIABLE}` references resolved from a colocated `.env` file) into literal values in the configuration file on first start, then remove `.env` — a one-time operation, also available as a standalone command for a manual/dry run, and also triggered before the interactive config tool opens

### 8.3 Configuration Validation

The gateway SHALL:
1. Reject queue depth settings with negative values
2. If permission timeouts are enabled, require that the overall agent timeout is greater than the permission timeout
3. Prevent two watchers from sharing one session identity in a way that would create ambiguous routing. Since session IDs are no longer configurable (3.2.1), this is now satisfied by construction at the configuration layer rather than by a cross-watcher check, and remains a runtime requirement on assignment

### 8.4 Templates, Tool Presets, and Watcher Rule Keys

The gateway SHALL:
1. Support top-level `connector_templates`, `agent_templates`, and `watcher_templates` blocks — each a mapping of template name to a partial field block — referenced from an individual connector/agent/watcher entry via that entry's own `inherits: <template-name>` field, deep-merged with the entry's own fields taking precedence over the template's on conflict. An entry that omits `inherits:` is entirely unaffected by any template (v0.3; supersedes the v0.2 `connector_defaults`/`agent_defaults`/`watcher_defaults` blocks, which deep-merged unconditionally into every entry of a kind regardless of type — removed entirely, see requirement 9 below)
2. Reject a named template that sets an identity field belonging to a specific entry — `name`, for both `connector_templates` and `watcher_templates`. `rooms` is deliberately NOT an identity field: a `watcher_templates:` entry MAY supply it, and a rule's own `rooms` deep-merges over it, so a template can set `direct: true` for every rule that inherits it while each rule adds its own `include`
3. Support a top-level `tool_presets` block of named, reusable tool-rule lists, referenced by name from `owner_allowed_tools`/`guest_allowed_tools`, freely mixable with inline tool-rule entries
4. Validate every defined tool preset's rules at configuration load time, regardless of whether any agent references it
5. Reject a tool preset whose rule list itself references another preset by name (presets SHALL be flat)
6. Accept a closed set of keys on a `watcher_rules:` entry and reject any other, listing the valid ones. Removed fields — `room`, `rooms:` as a list, `session_id` — therefore need no rule of their own: each is simply not a key. (This supersedes the v0.5 requirements for expanding a `rooms` list into one watcher per room with a derived name, and for rejecting `room` alongside `rooms`; both belonged to the static watcher shape, which no longer exists)
7. Reject any unrecognised **top-level** key in config.yaml, listing the valid ones and naming a likely intended key when one is close. This is what reports a config still using the pre-rename `watchers:` block, so the rename needs no check of its own
7a. Reject a watcher entry that sets `session_id` at all — the field is removed, and the error SHALL name the handoff replacement rather than being silently ignored (a watcher entry's unknown keys are otherwise dropped, and this field was documented in v0.5.1)
8. Apply the same watcher-name uniqueness requirement to names produced by room expansion as to explicitly configured names
9. Reject a leftover top-level `connector_defaults`, `agent_defaults`, or `watcher_defaults` key immediately, with an error naming the replacement (`connector_templates`/`agent_templates`/`watcher_templates`) — never silently ignore it, since a silent no-op would silently drop whatever settings an operator still believes are shared
10. Reject an `inherits:` field naming a template that does not exist in the matching `*_templates` block, and reject a named template that itself sets `inherits:` (no nested templates)

---

## 9. Error Handling and Recovery

### 9.1 Message Queue and Backpressure

The gateway SHALL:
1. Maintain bounded message queues per watcher to prevent unbounded memory growth
2. Reject excess messages when a queue is full, rather than blocking indefinitely
3. Provide a user-visible, rate-limited failure message when a message is rejected due to queue fullness

### 9.2 Agent Timeout

The gateway SHALL:
1. Return a user-visible timeout message if the agent exceeds the configured timeout
2. Terminate the agent invocation and not wait indefinitely

### 9.3 Agent Backend Failures

The gateway SHALL:
1. Return a sanitized user-visible error message if the agent backend fails
2. NOT expose internal error details, stack traces, or backend-specific internals to chat users

### 9.4 Connection Recovery

The gateway SHALL:
1. Be restartable without losing watcher runtime state
2. Resume operations using persisted state across restarts
3. Handle transient connector failures gracefully (e.g., temporary network loss, platform API downtime)

### 9.5 Graceful Shutdown

The gateway SHALL:
1. On shutdown signal (SIGTERM), allow in-flight message processing to complete (configurable timeout)
2. Drain queued messages up to the configured timeout
3. Post offline notifications after messages are fully drained

---

## 10. Attachment Handling

The gateway SHALL:
1. Support inbound messages with file attachments
2. Download files to local disk before forwarding the message to the agent
3. Provide the local file path to the agent for processing
4. Enforce configured limits on file size and download timeout
5. Inject a human-readable description of attachments into the agent prompt when the agent cannot process files natively

---

## 11. Scripting and Programmatic Access

The gateway SHALL provide programmatic session access for scripts:

1. A programmatic session interface SHALL support explicit start and stop lifecycle boundaries
2. Multiple sends on the same programmatic session SHALL reuse the same underlying agent session
3. Sending on an unstarted session SHALL fail explicitly
4. Programmatic sends SHALL return a normalized agent response object regardless of backend
5. Programmatic sends MAY support optional attachments
6. Programmatic sends MAY support optional per-message environment overrides when supported by the selected agent backend

---

## 12. Agent Backend Requirements

### 12.1 Common Requirements

All supported agent backends SHALL:
1. Present a normalized response shape to the rest of the system
2. Support creating a new session and sending subsequent turns to an existing session
3. When returning no textual output, still produce a non-empty placeholder response
4. Surface backend failures as structured errors that the gateway can translate into user-visible messages

### 12.2 Claude Backend

The Claude backend SHALL:
1. Support explicit session creation and resumed message sending
2. Support permission-hook behavior when permissions are enabled
3. Accept attachment context via prompt injection even if native file uploads are unsupported
4. Respect the configured timeout and terminate invocations that exceed it

### 12.3 OpenCode Backend

The OpenCode backend SHALL:
1. Support explicit session creation and subsequent session messaging
2. Map backend rate limiting into a structured rate-limit failure that the gateway can handle
3. NOT silently recreate a missing session when a send targets an unknown session ID
4. Support recovery from temporary backend service unavailability when conditions permit
5. For per-message environment overrides, either support them or clearly indicate unsupported status

---

## 13. Non-Goals

This specification does not define:
- Internal module organization or class hierarchies
- Specific subprocess commands or HTTP endpoints used internally
- Binary file formats for internal state persistence
- Implementation-specific connector or agent backend internals
- Exact performance targets or throughput requirements

---

## 14. Operational Commitments

This section records what the project undertakes to do, and where that
undertaking stops short of a guarantee.

**"Not guaranteed" here means best effort — it does not mean indifference.**
Every item below is something the project actively works to preserve, tests for,
and treats a regression in as a defect worth fixing. What is withheld is the
promise that the outcome is always achieved, never the effort to achieve it.

The distinction cuts both ways, and both directions matter when ranking a
defect. A defect in a best-effort area is still a defect. A defect that breaches
a stated guarantee is more than an inconvenience. Rank against what is written
here rather than against an assumed standard.

### 14.1 Upgrades

The project SHALL:
1. Exercise the upgrade path before release, and treat an upgrade that breaks a
   supported configuration as a defect
2. Name breaking changes in the release notes, with a migration path where one
   exists
3. Report the reason and a non-zero exit when an upgrade cannot bring the
   gateway back up, rather than reporting success or failing silently

Not guaranteed: that an upgrade never interrupts a running gateway. Operators
SHOULD back up `config.yaml` and the runtime state directory before upgrading.

### 14.2 Configuration compatibility

The project SHALL:
1. Make a best effort to keep an existing `config.yaml` loading across releases
2. Provide an automatic migration where the change allows one
3. Name a change that requires operator action in the release notes

Not guaranteed: indefinite backward compatibility for every configuration key.

### 14.3 Availability

The project SHALL:
1. Degrade rather than exit where a section of the system can be isolated (§9)
2. Reconnect automatically after transient connector failures (§9.4)
3. Retry a degraded section on a later reload rather than requiring a restart

Not guaranteed: continuous availability. The gateway is a single daemon with no
failover, clustering or high-availability mode, and the project offers no
service-level objective. Stopping and starting it is an expected operation, not
a last resort — an operator who can restart early is better served than one kept
running through a fault.

### 14.4 Message delivery

Delivery is best effort, under the queue, timeout and recovery behaviour in §9.
Messages can be lost when a connector or the daemon fails; that outcome is
within what the system claims, and is not by itself a severe defect. Severity
comes from whether the outcome is unreasonable given what triggered it.

### 14.5 Deployment assumptions

AgentCoop is designed for self-hosted deployments administered by a small number
of operators who run their own instance. It is not a multi-tenant hosted
service, and nothing here should be read as a commitment appropriate to one.
