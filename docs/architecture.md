# AgentCoop Architecture

A comprehensive guide to the internal design of AgentCoop—a standalone Python daemon that bridges chat platforms to AI agent backends with role-based access control and human-in-the-loop tool approval.

**Target audience:** Developers who want to understand the system internals, extend with new connectors or agents, or contribute to the codebase.

---

## System Overview

**AgentCoop** is a long-running daemon that:

1. Connects to chat platforms (Rocket.Chat, and extensible to Slack/Discord/etc.) via platform-specific **Connectors**
2. Routes inbound messages through **per-room message queues** for serial, race-condition-free processing
3. Sends messages to configurable **AI agent backends** (Claude CLI, OpenCode, extensible)
4. Applies **role-based access control** (OWNER/GUEST/ANONYMOUS) to enforce tool allow-lists and require human approval for sensitive operations
5. Posts agent responses back to the chat platform
6. Exposes a **Unix socket CLI control interface** for daemon management (add/stop watchers, check status)

### Naming: AgentCoop and the gateway

**AgentCoop** is the product — this repository, the `coop` command, the
documentation. **The gateway** is the daemon process AgentCoop runs: the thing
`coop start` starts, that holds watcher records, talks to the connectors and
drives the agents. It is one component of AgentCoop (the Python package is
`gateway/` for the same reason), in the way OpenClaw has a gateway component
inside a larger product. Operator-facing output says "Gateway: not running"
because it is describing that process, not the product. The short form of the
product name is **Coop** — the agent-facing session header is
`## Coop Session Identity`, and `COOP_*` is the environment-variable prefix.
AgentCoop was previously known as agent-chat-gateway, or ACG.

### Key Properties

- **Per-room serial processing** — One async queue per room prevents race conditions
- **Multi-connector support** — Run multiple Rocket.Chat instances (or mixed platforms) in a single daemon
- **Multi-agent support** — Different rooms can use different agent backends
- **Stateful** — Agent sessions and watcher state persist to `~/.agentcoop/state.<connector>.json`
- **Graceful shutdown** — Drains queues with 30-second grace period before terminating agent subprocesses
- **Security by design** — Roles resolved by connector (never from message content), permission broker is fail-closed (no broker = no watcher start)

---

## Architecture Diagram

```mermaid
graph TD
    A["Chat Platform<br/>(Rocket.Chat, Slack, etc.)"]
    B["Connector<br/>(DDP WebSocket + REST)"]
    C["SessionManager<br/>(per-connector orchestrator)"]
    D["MessageDispatcher<br/>(routes → per-room processor)"]
    E["MessageProcessor<br/>(per-room async queue)"]
    F["InjectedContextBuilder<br/>(build header+files; ensure durable delivery)"]
    G["PromptBuilder<br/>(role prefix + text)"]
    H["AgentTurnRunner<br/>(typing → send → deliver)"]
    I["AgentBackend<br/>(Claude/OpenCode)"]
    J["PermissionBroker<br/>(HTTP hook / SSE)"]
    K["StateStore<br/>(persist watcher state)"]
    L["WatcherLifecycle<br/>(start/stop/pause/resume)"]

    A -->|WebSocket events| B
    B -->|IncomingMessage| D
    D -->|enqueue| E
    E -->|process loop| F
    F -->|inject context| G
    G -->|build prompt| H
    H -->|send prompt| I
    I -->|tool call| J
    J -->|permission?| H
    H -->|response| B
    B -->|send_text| A
    E -->|session mgmt| K
    C -->|orchestrate| L
    L -->|start/stop| E
    K -->|load/save| L
    C -->|delegate| D
    C -->|delegate| F
```

---

## Module Structure and Responsibilities

| Module | Purpose | Key Classes/Functions |
|---|---|---|
| **daemon.py** | Unix double-fork daemonization, PID lock, signal handling | `is_running()`, `daemonize()`, `run_daemon()` |
| **cli.py** | argparse CLI entry point (start/stop/restart/status/list/pause/resume/reset/send/onboard/upgrade) | `main()`, command dispatch |
| **service.py** | Top-level orchestrator; wires connectors + agents + permission brokers | `GatewayService`, `AgentRuntimeManager`, `ConnectorEntry` |
| **control.py** | Unix socket ControlServer; routes CLI commands to daemon | `ControlServer`, `handle_cli_command()` |
| **config.py** | YAML loader with cross-validation | `GatewayConfig`, `AgentConfig`, `PermissionConfig` |
| **runtime_lock.py** | Shared PID file and runtime directory utilities | `RUNTIME_DIR`, `LOCK_FILE`, `acquire()`, `release()` |
| **onboard.py** | Interactive setup wizard for initial configuration | `run_wizard()` |
| **upgrade.py** | Self-upgrade logic for daemon updates | `upgrade_if_needed()` |
| **state.py** | Legacy state compatibility helpers | — |
| **core/connector.py** | Platform-agnostic Connector ABC and normalized message types | `Connector` ABC, `IncomingMessage`, `Room`, `User`, `UserRole` |
| **core/session_manager.py** | Thin orchestrator delegating to collaborators; wires connector + agents + state | `SessionManager` |
| **core/watcher_lifecycle.py** | Watcher state machines (start/pause/resume/reset/stop) | `WatcherLifecycle`, `start_watcher()`, `stop_watcher()` |
| **core/dispatch.py** | Routes inbound messages to per-room processor; intercepts approve/deny | `MessageDispatcher` |
| **core/message_processor.py** | Per-room async queue + orchestration of one turn | `MessageProcessor`, `enqueue()`, `_process()` |
| **core/agent_turn_runner.py** | Execute one agent turn (typing → send → deliver) | `AgentTurnRunner`, `run_turn()` |
| **core/injected_context_builder.py** | Build identity header + context files; ensure durable delivery via AgentBackend.ensure_durable_instructions() | `InjectedContextBuilder` |
| **core/prompt_builder.py** | Pure prompt assembly (no I/O); role-aware prefix | `build_prompt()` |
| **core/attachment_workspace.py** | Per-watcher attachment symlink workspaces | `AttachmentWorkspace`, `localize_attachment_paths()` |
| **core/state_store.py** | Persist WatcherState to JSON; watermark management | `StateStore` |
| **core/state.py** | WatcherState dataclass (session_id, paused, watermark) | `WatcherState` |
| **core/session_maps.py** | Shared routing maps (session → room/role/connector/thread) | `SessionMaps` |
| **core/config.py** | Core config types (CoreConfig, WatcherConfig, etc.) | `CoreConfig`, `WatcherConfig`, `PluginConfig` |
| **core/permission.py** | PermissionBroker ABC (orchestrates state + presenter + notifier) | `PermissionBroker` ABC |
| **core/permission_state.py** | PermissionRequest dataclass, PermissionRegistry in-memory store | `PermissionRegistry`, `PermissionRequest` |
| **core/permission_presenter.py** | Format permission notification messages for users | `format_request_msg()`, `format_timeout_msg()` |
| **core/permission_notifier.py** | Deliver permission notifications to chat rooms with retry | `PermissionNotifier`, `ConnectorPermissionNotifier` |
| **core/expiry_task.py** | Background task: auto-deny timed-out permission requests | `run_expiry_task()` |
| **core/tool_match.py** | Tool allow-list matching (regex + tree-sitter bash AST) | `match_tool()` |
| **core/adapter_utils.py** | Shared helpers (attachment prompt injection) | `format_attachments_for_prompt()` |
| **agents/__init__.py** | AgentBackend ABC (start/stop/create_session/send/create_gateway_broker) | `AgentBackend` ABC, `GatewayBrokerConfig` |
| **agents/response.py** | AgentResponse and TokenUsage dataclasses | `AgentResponse`, `TokenUsage` |
| **agents/session.py** | AgentSession: thin async context manager for scripting | `AgentSession` |
| **agents/errors.py** | Classified exception hierarchy (RateLimited, Permission, Unavailable) | `AgentError`, `AgentRateLimitedError`, etc. |
| **agents/claude/adapter.py** | ClaudeBackend: drives Claude CLI subprocess with stream-json | `ClaudeBackend` |
| **agents/claude/broker.py** | ClaudePermissionBroker: HTTP PreToolUse hook server | `ClaudePermissionBroker` |
| **agents/claude/callable_broker.py** | Callable broker wrapper for AgentSession use | `CallableClaudePermissionBroker` |
| **agents/claude/settings_adapter.py** | Generate temp settings.json with PreToolUse hook URL | `generate_settings_file()` |
| **agents/opencode/adapter.py** | OpenCodeBackend: drives opencode HTTP server | `OpenCodeBackend` |
| **agents/opencode/broker.py** | OpenCodePermissionBroker: SSE listener + reply API | `OpenCodePermissionBroker` |
| **agents/opencode/callable_broker.py** | Callable broker wrapper for AgentSession use | `CallableOpenCodePermissionBroker` |
| **connectors/rocketchat/connector.py** | RocketChatConnector: all RC-specific logic | `RocketChatConnector` |
| **connectors/rocketchat/websocket.py** | DDP WebSocket client (subscriptions, reconnect, keepalive) | `DDPClient` |
| **connectors/rocketchat/rest.py** | RC REST client (login, send, upload, search) | `RocketChatREST` |
| **connectors/rocketchat/normalize.py** | RC DDP doc → IncomingMessage (dedup, attachment download) | `normalize_rc_message()` |
| **connectors/rocketchat/outbound.py** | RC outbound helpers (send_text, typing, online) | `send_message()`, `notify_typing()` |
| **connectors/rocketchat/policy.py** | RC message filtering policy (bot, edits, threads) | `should_process_message()` |
| **connectors/mattermost/connector.py** | MattermostConnector: all Mattermost-specific logic | `MattermostConnector` |
| **connectors/mattermost/websocket.py** | Mattermost Realtime WebSocket client (no per-channel subscribe — one stream covers every channel the bot is in) | `MattermostWebSocketClient` |
| **connectors/mattermost/rest.py** | Mattermost REST v4 client (dual auth, post, upload, history) | `MattermostREST` |
| **connectors/mattermost/normalize.py** | Mattermost post → IncomingMessage (mention/dedup filtering, attachment download) | `filter_mm_message()`, `normalize_mm_message()` |
| **connectors/mattermost/outbound.py** | Mattermost outbound helpers (chunked send, media upload) | `send_text()`, `send_media()` |
| **connectors/mattermost/policy.py** | Re-export of the shared `apply_thread_policy()` (see `core/thread_policy.py`) | `apply_thread_policy` |
| **connectors/script/connector.py** | ScriptConnector: in-memory connector for tests/scripting | `ScriptConnector` |

---

## Data Flow: From Chat Message to Agent Response

### Complete Message Lifecycle

```
┌─ User sends @mention in Rocket.Chat ─────────────────────┐
│                                                             │
└─→ RC WebSocket DDP event                                   │
    └─→ connectors/rocketchat/websocket.py (DDPClient)       │
        ├─ Subscription updates ("room-messages")            │
        ├─ Download attachments (if enabled)                 │
        ├─ Dedup check (platform message ID)                 │
        └─→ Connector.message_handler callback               │
            └─→ normalize_rc_message()                       │
                └─→ IncomingMessage(                          │
                    id, timestamp, room, sender, role, text, │
                    attachments, raw)                        │
                                                              │
    └─→ SessionManager.run()                                 │
        └─→ MessageDispatcher.dispatch(message)              │
            ├─→ [Intercept approve/deny commands]            │
            │   ├─ PermissionRegistry.resolve()              │
            │   └─ Unblock tool Future                       │
            │                                                 │
            └─→ MessageProcessor.enqueue(message)            │
                └─→ asyncio.Queue.put(message)               │
                                                              │
    └─→ MessageProcessor._process()  [queue consumer loop]   │
        ├─→ InjectedContextBuilder.build()+.ensure()  [every watcher start] │
        │   └─ Read ~/.agentcoop/contexts/*.md      │
        │   └─ Deliver via agent.ensure_durable_instructions() │
        │      (Claude: --append-system-prompt-file; else: one-time send) │
        │                                                     │
        ├─→ PromptBuilder.build_prompt()                     │
        │   ├─ Trusted header:                               │
        │   │   "[Rocket.Chat #room | from: alice | role:    │
        │   │    owner]"                                     │
        │   ├─ Message text                                  │
        │   └─ Attachment paths                              │
        │                                                     │
        ├─→ AgentTurnRunner.run_turn()                       │
        │   ├─→ notify_typing(True)                          │
        │   │                                                 │
        │   ├─→ AgentBackend.send()                          │
        │   │   └─ Claude:                                   │
        │   │     └─ Spawn: claude -p --resume <id>          │
        │   │        --output-format stream-json             │
        │   │        [--settings <hook_config>]              │
        │   │     └─ Stream JSON events                      │
        │   │     └─ Extract text blocks + metadata          │
        │   │   └─ OpenCode:                                 │
        │   │     └─ HTTP POST /session/{id}/message         │
        │   │     └─ Parse JSON response                     │
        │   │                                                 │
        │   │   ┌─ [If tool call detected] ─────────────┐   │
        │   │   │                                        │   │
        │   │   └─→ PermissionBroker._decide()          │   │
        │   │       ├─ Guest + allowed tool?            │   │
        │   │       │  → auto-allow                     │   │
        │   │       ├─ Guest + denied tool?             │   │
        │   │       │  → auto-deny (no notif)           │   │
        │   │       ├─ Owner + skip_owner_approval?     │   │
        │   │       │  → auto-allow                     │   │
        │   │       ├─ Owner + tool in allow-list?      │   │
        │   │       │  → auto-allow                     │   │
        │   │       └─ Else:                            │   │
        │   │          └─ request_permission()          │   │
        │   │             ├─ Generate 4-char ID        │   │
        │   │             ├─ Register Future            │   │
        │   │             ├─ Post to RC chat:           │   │
        │   │             │   "🔐 Tool: Bash            │   │
        │   │             │    [a3k9]"                  │   │
        │   │             ├─ Pause agent subprocess     │   │
        │   │             ├─ Await owner "approve a3k9" │   │
        │   │             ├─ [Or auto-deny after        │   │
        │   │             │  permissions.timeout]       │   │
        │   │             └─ Unblock → resume           │   │
        │   │                                            │   │
        │   │   └─────────────────────────────────────────┘   │
        │   │                                                 │
        │   ├─→ connector.send_text(response)               │
        │   │   ├─ Chunk by text_chunk_limit               │
        │   │   ├─ POST to RC REST /api/v1/chat.postMessage │
        │   │   └─ or equivalent for other connectors       │
        │   │                                                 │
        │   ├─→ Log token usage (if response.usage)         │
        │   │   "Agent usage [@alice] in=1234 out=256 ..."   │
        │   │                                                 │
        │   └─→ notify_typing(False)                        │
        │                                                     │
        ├─→ Update session state                            │
        │   └─ StateStore.save() to JSON                    │
        │                                                     │
        └─→ Continue loop (dequeue next message or wait)     │
```

### Key Interception Points

1. **Message normalization** — `normalize_rc_message()` runs BEFORE the core sees the message; deduplication, attachment download, role resolution all happen here
2. **Permission interception** — `MessageDispatcher.dispatch()` intercepts "approve" and "deny" commands BEFORE they reach the queue
3. **Tool execution interception** — `PermissionBroker` uses backend-specific hooks (HTTP for Claude, SSE for OpenCode) to pause agent execution mid-turn
4. **Graceful shutdown** — `MessageProcessor.stop()` drains the queue with a 30-second grace period

---

## Key Abstractions and Interfaces

### Connector ABC

All chat platform integrations implement this interface:

```python
class Connector(ABC):
    """Base class for chat platform adapters."""

    @abstractmethod
    async def connect(self) -> None:
        """Establish platform connection."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Tear down platform connection."""
        ...

    @abstractmethod
    def register_handler(self, handler: Callable[[IncomingMessage], Awaitable[None]]) -> None:
        """Register callback for inbound messages."""
        ...

    @abstractmethod
    async def send_text(self, room_id: str, response: AgentResponse) -> None:
        """Post agent response text to a room."""
        ...

    @abstractmethod
    async def resolve_room(self, room_name: str) -> Room:
        """Look up room metadata by name."""
        ...

    @abstractmethod
    def format_prompt_prefix(self, msg: IncomingMessage) -> str:
        """Return trusted header prefix for prompt."""
        ...
```

**Security principle:** The `format_prompt_prefix()` method must return a server-controlled header that the agent can read but never the message sender can forge. This is the foundation of RBAC.

### AgentBackend ABC

All AI agent integrations implement this interface:

```python
class AgentBackend(ABC):
    """Base class for AI agent backends."""

    @abstractmethod
    async def create_session(
        self,
        working_directory: str,
        extra_args: list[str] | None = None,
        session_title: str | None = None,
    ) -> str:
        """Start a new session. Return opaque session_id."""
        ...

    @abstractmethod
    async def send(
        self,
        session_id: str,
        prompt: str,
        working_directory: str,
        timeout: int,
        attachments: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> AgentResponse:
        """Send a message to an existing session.

        Raise asyncio.TimeoutError if timeout exceeded.
        """
        ...

    @abstractmethod
    async def start(self) -> None:
        """Start any backend services (e.g., permission broker server)."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Shut down backend services."""
        ...
```

### PermissionBroker ABC

Backend-specific tool interception logic:

```python
class PermissionBroker(ABC):
    """Intercepts tool calls and requests owner approval."""

    @abstractmethod
    async def start(self) -> None:
        """Start background listeners (HTTP server, SSE client, etc.)."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Shut down background listeners."""
        ...

    async def request_permission(
        self,
        tool_name: str,
        tool_input: dict,
        session_id: str,
        room_id: str,
        thread_id: str | None = None,
    ) -> bool:
        """Post permission request and block until resolved.

        Returns True if approved, False if denied or timed out.
        """
        ...
```

### IncomingMessage

Normalized message format that the core library receives:

```python
@dataclass
class IncomingMessage:
    id: str                                      # Platform message ID
    timestamp: str                               # ISO 8601
    room: Room                                   # Channel/conversation
    sender: User                                 # Message author
    role: UserRole                               # OWNER/GUEST/ANONYMOUS
    text: str                                    # Message body
    attachments: list[Attachment] = field(default_factory=list)
    raw: dict = field(default_factory=dict)     # Original platform data
```

**Security:** `role` is resolved by the Connector (never by the core). The Connector examines the platform's user record to determine access level.

---

## Role-Based Access Control (RBAC) System

A **4-layer defense-in-depth** approach:

### Layer 1: Role Resolution (Connector)

The Connector maps platform user identity to `UserRole`:

- **OWNER** — Full access to all tools
- **GUEST** — Restricted to tool allow-list from config (e.g., Read, Grep, Glob only)
- **ANONYMOUS** — Rejected at message intake; never reaches queue

```python
# Example: RocketChatConnector
def _get_user_role(self, username: str, room_name: str) -> UserRole:
    if username in self.config.allowed_users.owners:
        return UserRole.OWNER
    if username in self.config.allowed_users.guests:
        return UserRole.GUEST
    return UserRole.ANONYMOUS
```

### Layer 2: Trusted Prompt Prefix (Connector)

The Connector injects a server-controlled header that the agent reads but cannot be forged:

```python
# In RocketChatConnector
def format_prompt_prefix(self, msg: IncomingMessage) -> str:
    return f"[Rocket.Chat #{msg.room.name} | from: {msg.sender.username} | role: {msg.role.value}]"
```

This prefix is **never** sourced from user input or tool output. The agent parses this prefix to determine what the sender is allowed to do.

### Layer 3: Tool Interception (PermissionBroker)

When the agent attempts a sensitive tool call (Bash, Write, Edit, etc.), the permission broker intercepts it:

- **Guests + tool in allow-list** → allow immediately
- **Guests + tool NOT in allow-list** → deny immediately (no owner notification—guest doesn't know the tool exists)
- **Owners + skip_owner_approval** → allow immediately (sandbox mode)
- **Owners + tool in owner_allowed_tools** → allow immediately
- **All others** → post notification, await owner approval

### Layer 4: Tool Parameter Matching (tool_match.py)

Optional fine-grained tool validation using:

- **Regex matching** on tool name and parameters
- **Tree-sitter AST parsing** for Bash commands (safely splits args without shell interpretation)
- **Path normalization** to prevent `../` directory traversal

---

## Permission Workflow

When a tool call requires approval:

```
Agent attempts sensitive tool call
├─ PermissionBroker._decide()
│  ├─ Check guest_allowed_tools (auto-allow or auto-deny)
│  └─ Check owner_allowed_tools (auto-allow or request)
│
├─ request_permission()
│  ├─ Generate 4-char collision-free ID: `a3k9`
│  ├─ PermissionRegistry.register(id) → asyncio.Future
│  ├─ PermissionNotifier.post() to RC chat:
│  │  ```
│  │  🔐 **Permission required** `[a3k9]`
│  │  **Tool:** `Bash`
│  │  **Params:** `command='rm ./build'`
│  │  Reply `approve a3k9` or `deny a3k9`
│  │  ```
│  └─ Start auto-deny timer (permissions.timeout seconds)
│
├─ Agent subprocess blocks (paused at HTTP hook or SSE)
│
├─ Owner types in RC chat: "approve a3k9"
│  ├─ MessageDispatcher.dispatch() intercepts
│  ├─ PermissionRegistry.resolve(id) → True
│  ├─ Future resolved → agent subprocess unblocked
│  └─ Tool executes
│
└─ [Or: No response within timeout]
   ├─ expiry_task auto-denies
   ├─ PermissionRegistry.resolve(id) → False
   ├─ Future resolved → agent subprocess resumes
   └─ Tool is skipped
```

### Approval Command Format

Owners reply with these exact formats (no `/` prefix—that would be intercepted by the RC client):

- `approve a3k9` — Allow the tool call
- `deny a3k9` — Block the tool call

The MessageDispatcher intercepts these commands **before** they reach the message processor queue, so the agent never sees them.

### Concurrency Guarantees

- Only one tool call can be pending approval at a time per room
- While a tool is pending, new messages are queued but not processed
- The `approve` or `deny` command unblocks the queue immediately

---

## Agent Backends

### ClaudeBackend

Drives the Claude CLI via subprocesses.

**Session creation:**
```bash
claude -p \
  --output-format json \
  --agent assistance \
  --session-prefix agent-chat
```
Parses response JSON to extract `session_id`.

**Message sending:**
```bash
claude -p \
  --resume <session-id> \
  --output-format stream-json \
  --verbose \
  [--settings <path>]  # injected when permissions enabled
```

Streams one JSON object per line; extracts text from content blocks and metadata (tokens, cost, duration).

**Permission handling:** When permissions enabled, a temporary `settings.json` file is generated with an HTTP hook URL and passed via `--settings`. Claude CLI calls the hook before executing sensitive tools.

**Environment isolation:** Strips `CLAUDECODE` from subprocess environment; injects `COOP_ROLE` and `COOP_ALLOWED_TOOLS` for per-message RBAC.

### OpenCodeBackend

Drives the opencode CLI.

**Session creation:**
```bash
opencode run --format json <new_session_args>
```

**Message sending:**
```bash
opencode run -s <session-id> --format json [-f <file> ...]
```

**Permission handling:** Uses opencode's native `permission.asked` SSE events triggered by the `role-enforcement.ts` plugin. The plugin sets `output.status = "ask"` on sensitive tool calls, firing the SSE event.

**Attachments:** Native `-f` flag support (unlike Claude which requires inline path injection).

---

## Configuration and Startup/Shutdown

### Configuration Hierarchy

```
config.yaml (user-editable)
    ├─ tool_presets: {name: [ToolRule, ...]}              # named, reusable tool-rule lists
    ├─ connector_templates / agent_templates / watcher_templates  # named; opted into per-entry via inherits:
    ├─ connectors: [ConnectorConfig, ...]
    ├─ agents: {name: AgentConfig, ...}
    ├─ watcher_rules: [WatcherRule, ...]                  # which rooms an agent may serve;
    │                                                     # one WatcherConfig is materialized
    │                                                     # per room as rooms turn up
    ├─ max_queue_depth: 100
    └─ scheduler: {completed_job_ttl_days: 7}
         → GatewayConfig.from_file()
            1. deep-merge *_defaults into each connector/agent/watcher entry
            2. resolve tool_presets references in owner/guest_allowed_tools
            3. expand watcher rooms: into one WatcherConfig per room
               (auto-name: "<connector>-<room>" unless name: is explicit)
            4. validate cross-references (unknown connector/agent, dup names, ...)
            → GatewayConfig (validated dataclass)
               → AgentConfig, ConnectorConfig, WatcherConfig, PermissionConfig
                  → CoreConfig (passed to SessionManager)
```

Secrets are stored directly in config.yaml as literal values (chmod'd
`0600`). `$VAR`/`${VAR}` is NOT expanded — a value that happens to look
like one is used as a plain string, same as any other. A legacy config
still using a colocated `.env` file with `$VAR`/`${VAR}` references is
auto-migrated to literal values on first `coop start` (or
before the config TUI opens) — see `gateway/config_migrate.py`. See
`docs/migration-0.2.md` for the compact-format rationale and
`gateway/schema/config.schema.json` for the field-level JSON Schema. Run
`coop config validate --lint` to check a config.yaml without
starting the daemon.

### Startup Sequence

```
daemon.py:daemonize()
├─ Fork twice (true daemon)
├─ Acquire PID lock (~/ag-cg/gateway.pid)
├─ Redirect stdout/stderr to ~/ag-cg/gateway.log
└─ Call service.py:GatewayService(config)

GatewayService.__init__()
├─ Parse config.yaml → GatewayConfig
├─ Instantiate AgentRuntimeManager
├─ Instantiate per-Connector SessionManager
└─ Instantiate ControlServer (Unix socket)

GatewayService.run()
├─ AgentRuntimeManager.start_all()
│  ├─ Start agent backends (subprocesses, etc.)
│  └─ Start permission brokers (HTTP servers, SSE listeners)
├─ SessionManager.connect_only()  [every connector, concurrently]
│  └─ Connector.connect() [login, DDP WebSocket, etc.]
├─ identity barrier — Connector.bot_identity() for all, duplicates refused
├─ SessionManager.sync_only()     [every connector, concurrently]
│  ├─ resume persisted watchers, resolve rooms, subscribe
│  └─ Connector.start_inbound() [begin reading; no-op where delivery is per-room]
└─ ControlServer.run() [accept CLI commands]
```

**Startup ordering rationale:**
1. Backends first — need to be running before messages arrive
2. Permission brokers second — only if backend succeeded
3. Connectors authenticate third — no subscription yet, so nothing is delivered
4. **Identity barrier fourth** — two connectors logged in as one bot account receive
   the identical stream, so every shared room would get two agents answering. Only this
   loop sees all connectors at once (a SessionManager owns exactly one), and it has to
   run before *any* of them subscribes: a check that fires after one has started is
   checking something that is already happening. A connector that cannot report its own
   identity stops startup rather than starting unchecked (design §4.5)
5. Watchers restore and subscribe fifth — can now safely dispatch messages to agents
6. **Inbound stream starts last within that phase.** A connector whose transport
   delivers every room the account can see (Mattermost) discards events for rooms it
   has no state for, and nothing replays them — so reading before the restore turns
   "not subscribed yet" into "lost". The socket is open throughout, so arrivals are
   buffered rather than missed. Rocket.Chat gates delivery per room and needs nothing
7. Control socket last — ready to accept CLI commands

`SessionManager.run_once()` still runs both phases back to back, for a standalone
single-connector embedding where there is no second connector to collide with.

### Shutdown Sequence (Reverse Order)

```
Signal: SIGTERM

daemon.py:_signal_handler()
├─ Set shutdown flag
└─ Call service.py:GatewayService.stop()

GatewayService.stop()
├─ ControlServer.stop()
├─ SessionManager.stop()  [all connectors]
│  └─ For each active MessageProcessor:
│     ├─ Enqueue _DRAIN_SENTINEL
│     ├─ Wait up to 30 seconds for queue drain
│     └─ Cancel remaining tasks
├─ AgentRuntimeManager.stop_all()
│  ├─ SIGTERM agent subprocesses
│  └─ SIGKILL after 5-second grace
└─ Release PID lock

daemon.py:_exit()
```

**Grace period:** 30 seconds for message queues to drain before force-killing agents. This allows in-flight tool calls to complete or timeout naturally.

---

## State Persistence

All state files live in `~/.agentcoop/`:

| File | Contents |
|---|---|
| `gateway.pid` | Process ID of running daemon |
| `gateway.log` | All daemon output (append mode) |
| `control.sock` | Unix domain socket for CLI commands |
| `state.<connector>.json` | Per-connector watcher state (session_id, paused, watermark) |
| `<room>_<session>.lock` | Per-watcher lock file (prevents duplicate sessions) |

### WatcherState JSON

```json
{
  "watchers": [
    {
      "id": "watcher-abc123",
      "room_name": "agent-testing",
      "agent_name": "assistance",
      "session_id": "s-rc4d91a9",
      "working_directory": "/path/to/project",
      "paused": false,
      "watermark": "1234567890000",
      "context_files": ["README.md", "ARCHITECTURE.md"]
    }
  ]
}
```

- **watermark** — Last processed message timestamp; used to skip duplicates on restart
- **paused** — If true, messages are queued but not processed
- **session_id** — Opaque string from agent backend; used to resume sessions
- **backend_identity** — The backend type and working directory the session was
  created against. Checked before `session_id` is resumed: an id is only meaningful
  inside the store that issued it, so a mismatch starts a fresh session instead of
  replaying the id into a different one

---

## Design Principles

### 1. **Separation of Concerns**

Each module has a single responsibility:

- **SessionManager** — delegates to collaborators, never implements business logic
- **MessageDispatcher** — routes to per-room processor; intercepts commands
- **MessageProcessor** — queue orchestration and session bookkeeping
- **AgentTurnRunner** — single turn execution (prompt → agent → reply)
- **InjectedContextBuilder** — context file I/O (build) + durable delivery (ensure)
- **PromptBuilder** — prompt assembly (no I/O, pure function)
- **StateStore** — persistence (no business logic)

### 2. **Platform Agnosticism**

The core library (`gateway/core/`) never imports anything platform-specific. It only uses the normalized types from `core/connector.py` and `agents/response.py`. This makes adding new connectors or agents trivial.

### 3. **Connector Ownership of Security Decisions**

The connector is responsible for:

- Resolving user role (OWNER/GUEST/ANONYMOUS)
- Implementing `format_prompt_prefix()` with server-controlled header
- Handling attachment downloads
- Deduplicating messages

The core never touches raw platform user data or makes RBAC decisions.

### 4. **Fail-Closed Permission System**

If a permission broker cannot start, the daemon refuses to start watchers. This prevents messages reaching an agent with no tool approval layer active.

```python
if agent_cfg.permissions.enabled and not broker:
    raise RuntimeError(f"Permission broker failed for agent {agent_name}")
```

### 5. **Per-Room Serial Processing**

One async queue per `(connector, room)` pair ensures:

- No race conditions on session state
- Predictable ordering of messages
- Simple message deduplication (watermark check)

### 6. **Dependency Injection**

Collaborators are passed to SessionManager, MessageProcessor, etc., not instantiated internally. This enables:

- Testability (mock connectors, agents, brokers)
- Flexible composition (different agents per room)
- Isolation (no global state)

### 7. **Graceful Degradation**

If an agent backend or permission broker fails at startup:

- Log the error
- Mark the agent as unavailable
- Refuse CLI requests to use that agent
- Continue with other agents

If a connector fails mid-run:

- Log the error
- Stop all watchers for that connector
- Attempt reconnection with exponential backoff

---

## Extending the Gateway

### Adding a New Connector

Implement `Connector` ABC:

```python
from gateway.core.connector import Connector, IncomingMessage, Room, User, UserRole

class DiscordConnector(Connector):
    async def connect(self) -> None:
        # Connect to Discord API / WebSocket
        ...

    async def disconnect(self) -> None:
        # Tear down connection
        ...

    def register_handler(self, handler) -> None:
        # Store handler, call it on inbound messages
        ...

    async def send_text(self, room_id: str, response: AgentResponse) -> None:
        # POST response to Discord channel
        ...

    async def resolve_room(self, room_name: str) -> Room:
        # Look up room metadata
        ...

    def format_prompt_prefix(self, msg: IncomingMessage) -> str:
        return f"[Discord #{msg.room.name} | from: {msg.sender.username} | role: {msg.role.value}]"
```

Register in `gateway/connectors/__init__.py`'s `connector_factory()` (imported and called from `service.py`, but the factory itself lives here). It currently has four branches (`rocketchat`, `script`, `voice`, `mattermost`) — add a fifth:

```python
def connector_factory(cc: ConnectorConfig) -> Connector:
    if cc.type == "rocketchat":
        ...
    if cc.type == "script":
        ...
    if cc.type == "voice":
        ...
    if cc.type == "mattermost":
        ...
    if cc.type == "discord":
        from .discord import DiscordConnector
        from .discord.config import DiscordConfig
        return DiscordConnector(DiscordConfig.from_connector_config(cc))
    raise ValueError(f"Unknown connector type: {cc.type!r}")
```

Add to `config.yaml`:

```yaml
connectors:
  - name: discord-main
    type: discord
    server:
      bot_token: "your-discord-bot-token"
    allowed_users:
      owners:
        - alice
```

### Adding a New Agent Backend

Implement `AgentBackend` ABC:

```python
from gateway.agents import AgentBackend

class AnthropicAPIBackend(AgentBackend):
    async def create_session(self, working_directory, extra_args=None, session_title=None) -> str:
        # POST to Anthropic API, return session_id
        ...

    async def send(self, session_id, prompt, working_directory, timeout, attachments=None, env=None) -> AgentResponse:
        # POST message to API, stream response, return AgentResponse
        ...

    async def ensure_durable_instructions(
        self, session_id, working_directory, timeout, content, *,
        path_key, already_delivered,
    ) -> str | None:
        # Required in practice, though not @abstractmethod: the base raises
        # NotImplementedError, because a backend must choose *how* content reaches the
        # model rather than inherit a fallback that is not compaction-resistant.
        #
        # `path_key` is OPAQUE — use it verbatim as a file name and derive nothing from
        # it. It is scoped to the watcher in a room, so two watchers bound to one room do
        # not overwrite each other's instructions. Do not substitute
        # gateway.core.paths.room_path_key: that one keys the attachment workspace, which
        # is per room by design.
        #
        # Return None if this backend delivered the content itself (a one-time side
        # effect); return a path/value for the caller to re-supply on every turn.
        ...

    async def start(self) -> None:
        ...

    async def stop(self) -> None:
        ...
```

`GatewayService` preflights every backend's `ensure_durable_instructions` signature at
startup, so an implementation left on the pre-rename `watcher_name` parameter fails with
an actionable message instead of a bare `TypeError` on the first watcher start.

Register in `service.py`:

```python
def _build_agent_backend(agent_cfg: AgentConfig) -> AgentBackend:
    if agent_cfg.type == "anthropic-api":
        return AnthropicAPIBackend(...)
    ...
```

Add to `config.yaml`:

```yaml
agents:
  research:
    type: anthropic-api
    api_key: "your-anthropic-api-key"
    model: claude-3-opus-20240229
    new_session_args: []
```

---

## Testing and Scripting

### ScriptConnector

For unit tests and scripting without network I/O:

```python
from gateway.connectors.script.connector import ScriptConnector
from gateway.core.session_manager import SessionManager
from gateway.core.config import CoreConfig

# In-memory connector with no network calls
connector = ScriptConnector()

# Configure agent backend
from gateway.agents.claude.adapter import ClaudeBackend
agent = ClaudeBackend(command="claude", new_session_args=[], timeout=60)
agents = {"default": agent}

# Create session manager
config = CoreConfig(timeout=60, agents=agents)
manager = SessionManager(connector, agents, config)

# Simulate conversation
async def test_agent():
    await manager.run_once()
    await manager.add_session("test-room", None, "/tmp")
    await connector.inject("Hello, what's in this directory?")
    reply = await connector.receive_reply()
    print(reply.text)
```

### AgentSession

For one-off scripting without the full gateway stack:

```python
from gateway.agents.session import AgentSession
from gateway.agents.claude.adapter import ClaudeBackend

async with AgentSession(
    ClaudeBackend("claude", ["--agent", "assistance"], 120),
    cwd="/my/project"
) as session:
    response = await session.send("Summarize the codebase")
    print(response)  # __str__ returns response.text
```

---

## Troubleshooting Guide

### Daemon won't start

Check `~/.agentcoop/gateway.log` for errors. Common issues:

1. **Config YAML syntax error** — Run `python -m yaml config.yaml` to validate
2. **Agent binary missing** — Ensure Claude CLI or opencode is in PATH
3. **Port already in use** — Permission broker HTTP server conflicts; check netstat
4. **Permission denied** — Ensure daemon can write to `~/.agentcoop/`

### Messages not being processed

1. **Check the room is being watched at all** — `coop list --all`
   (plain `list` hides idle watchers). No row means no state record survived;
   the reason is in the startup log. See step 3 before concluding anything more
   than that from it.
2. **Read the STATE column.** `paused` means an operator muted it. **`failed`
   means a record exists and nothing is running for it.** The startup errors in
   the log say why. To recover: `resume` retries the start in place — except
   when the agent backend or its permission broker failed to start, which is
   decided once at boot and which `resume` refuses fail-closed. **Restart the
   daemon for that one.**
3. **No row at all?** Then there is no state to act on. That is *not* proof the
   start never got far: context injection, the attachment workspace and session
   binding all roll their record back, so a watcher can fail well into startup
   and still leave nothing. The log is authoritative, `list` is not.
4. **Check daemon logs** — `tail -f ~/.agentcoop/gateway.log`
5. **Check connector logs** — Filter by `connectors.rocketchat` in logs

### Permission requests timing out

1. **Approval command syntax** — Type `approve a3k9` (no slash)
2. **Check permission timeout config** — Must be < global timeout
3. **Check if broker is running** — Look for HTTP server on port in logs

### Agent crashes or hangs

1. **Check agent subprocess logs** — `claude` or `opencode` may have internal errors
2. **Increase timeout** — `timeout: 600` in config
3. **Check working directory** — Agent must be able to cd into it

---

## Performance Considerations

### Message Throughput

- Per-room serial processing means room is blocked while agent is responding
- Multiple rooms process in parallel (separate queues)
- Typical bottleneck is agent subprocess latency (5-30 seconds per message)

### Memory Usage

- One async task per room (message consumer)
- One subprocess per active agent session
- Permission registry stores pending requests in memory (cleared after resolution)
- State files are small (< 10 KB per watcher typically)

### Scaling

- **Horizontal:** Run multiple daemon instances on different connectors
- **Vertical:** Increase max file descriptors for many rooms: `ulimit -n 8192`

---

## Security Model

### Threat Model

1. **Malicious chat user** — Tries to craft messages that trick the agent into dangerous actions
   - **Defense:** Role prefix is server-injected, never user-controllable

2. **Compromised chat platform account** — Account hijacked to impersonate owner
   - **Defense:** RBAC is connector-level (resolve role from platform user record), not message content

3. **Tool injection via agent output** — Agent outputs a tool call in response text
   - **Defense:** Permission broker intercepts actual tool execution before it occurs

4. **Path traversal in attachments** — User uploads symlink to /etc/passwd
   - **Defense:** Connector downloads to isolated workspace, agent sees only local paths

### Isolation Guarantees

- **Chat platform isolation** — Each connector runs independently; breach of one doesn't affect others
- **Session isolation** — Agent sessions don't share history; each room has its own session
- **Filesystem isolation** — Working directory is per-watcher; agent can't escape (modulo agent's own sandbox)

---

## Summary

AgentCoop provides a modular, extensible bridge between chat platforms and AI agents with enterprise-grade RBAC and approval workflows. Its architecture emphasizes:

- **Modularity** — Swap connectors and agents without touching core logic
- **Security** — Defense-in-depth via role resolution, prompt injection, broker interception, and path normalization
- **Reliability** — Graceful degradation, state persistence, and queue draining on shutdown
- **Simplicity** — Minimal dependencies, focused modules, clear interfaces

The system is designed for contributions: adding a new connector or agent requires implementing one ABC and registering it—no changes to core libraries needed.

