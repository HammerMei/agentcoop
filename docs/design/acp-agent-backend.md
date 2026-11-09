# ACP agent backend

Status: **design, not implemented.** The primary subject is driving opencode
2.x through the Agent Client Protocol (ACP). The secondary subject is the
shape of a backend that can drive any ACP agent. Claude Code and Codex are
the worked examples (§5).

Evidence basis. Gateway claims cite the current tree. opencode claims cite the
`v2.2.5` tag of `anomalyco/opencode` (the same repository renders as
`sst/opencode`) or a run of 2.2.6. Every 2.x statement carries one of these
tags:

- `[src]`: read in the named file at `v2.2.5`
- `[docs]`: from `opencode.ai/v2/docs` or `agentclientprotocol.com`
- `[live 2.2.6]`: observed on a local 2.2.6 `opencode acp` over stdio
  (macOS). Appendix A holds the frames
- `[inferred]` / `[unverified]`: the checklist in §8 must settle it before
  the implementation is called done

---

## 1. Problem

### 1.1 What opencode 2.x breaks

The `opencode` backend (`gateway/agents/opencode/adapter.py`) drives
`opencode serve` over its HTTP API and the `/event` SSE stream. It gates tool
calls through `permission.asked` and `POST /permission/{id}/reply`
(`gateway/agents/opencode/broker.py`). Both were verified against opencode
1.18.13.

The 2.x line changes every one of those contracts. Its first release is
`v2.2.0` (2026-10-27). The 1.19.x line is released in parallel. npm carries
only 1.19.x. Homebrew carries 2.2.6 as of 2026-11-07.

| 1.x contract the gateway uses | 2.x | Consequence | Source |
|---|---|---|---|
| `POST /session {directory}` | `POST /api/session {location: {directory}, permissions?, agent?, model?, …}`. Every route is under `/api` | fails loudly | `[src]` `packages/server/src/handlers/session.ts` `session.create`, `packages/protocol/src/groups/session.ts` |
| `POST /session/{id}/message {parts, system?}` (blocking) | removed. `POST /api/session/:id/prompt {text, files?, delivery?, …}` is asynchronous and has **no `system` field** | fails loudly, and the durable-instructions channel is gone | `[src]` `handlers/session.ts` `session.prompt`, `packages/schema/src/session-inbox.ts` |
| `GET /event` events `message.part.delta`, `message.part.updated`, `session.status`, `session.error` | replaced by `session.text.delta`, `session.tool.*`, `session.step.*`, `session.execution.*`, `session.compaction.*` | the stream parser produces nothing, so every turn times out | `[src]` `packages/cli/src/acp/translate.ts` |
| `permission.asked {properties: {permission, patterns, metadata, always, tool}}` answered by `POST /permission/{id}/reply {reply}` | `permission.asked {data: {action, resources, metadata?, source?, save?}}` answered by `POST /api/session/:id/permission/:requestID/reply {decision}` | the broker never answers an ask, so the tool waits for the agent timeout (#165 again) | `[src]` `packages/schema/src/permission.ts`, `handlers/permission.ts` |
| HTTP without authentication | basic auth is always on (`OPENCODE_PASSWORD`, user `opencode`) | every request gets 401 | `[docs]` migrate-v1 guide, `[src]` `packages/server/src/auth.ts` |
| `plugin: ["file:///…/role-enforcement.ts"]` with a v1 hook object | the plugin API is replaced (`Plugin.define`). A **file** path is skipped with a log line, because only directories load | **silent**: the plugin disappears and nothing fails | `[docs]` `build/plugins/migrate-v1`, `[src]` `packages/core/src/config/normalize.ts` |
| tool `bash` | tool `shell`. `bash` is an alias on input | allow-list rules written as `tool: bash` match nothing that is named `shell` | `[src]` `packages/cli/src/acp/tool.ts` `V1Aliases`, changelog v2.2.5 |
| `external_directory` asks by default | allowed by default | a write outside the working directory no longer reaches the broker | `[docs]` migrate-v1 guide |

Two things survive. opencode still reads `OPENCODE_CONFIG_CONTENT` as the
inline config layer (precedence 6 of 8). It also migrates a v1 `permission`
map to the v2 ordered `permissions` list, renaming `bash` to `shell` `[src]`
`packages/core/src/config.ts`, `normalize.ts` `migratePermissions`.

Re-targeting the HTTP backend at the 2.x API is possible. The 2.x API is
documented only as "the generated API reference", and its permission routes
are annotated experimental `[src]` `packages/protocol/src/groups/permission.ts`.
The gateway would be the only consumer of that contract.

### 1.2 Why ACP

ACP (agentclientprotocol.com) is the protocol editors use to drive coding
agents. The transport is JSON-RPC 2.0, newline-delimited, over the agent's
stdio. The client spawns the agent. Zed and JetBrains govern the protocol
jointly. The current protocol version is 1, and a v2 draft exists.

Who speaks it:

- opencode ships `opencode acp` since 0.15.10 and documents it as a
  first-class surface.
- Adapters in the protocol's own GitHub organisation serve Claude Code and
  Codex.
- Gemini CLI, Copilot, goose, Kiro and Cursor expose it natively.
- The protocol's client registry lists a dozen chat bridges (Slack, Telegram,
  Discord, Matrix) built as ACP clients. A chat gateway as an ACP client is
  an established pattern.

What ACP gives the gateway, per agent:

- session creation and resumption
- a prompt turn with streamed updates
- tool-call events
- a permission request the client must answer
- token usage
- cancellation

What ACP does not give (§3.7, §3.8):

- a system prompt (stable v1 has none)
- per-turn usage as a stable field
- any say over *which* tool calls the agent asks about

### 1.3 Requirements

- **R1**: An opencode 2.x agent reaches the level of integration the 1.x
  backend has. That means persistent sessions that survive gateway restarts,
  streamed intermediate events, every gated tool call decided by the
  gateway's permission broker (ADR-0002), durable per-watcher instructions,
  usage in the log, and structured errors.
- **R2**: The protocol layer is agent-agnostic. Nothing in it names
  opencode. Everything agent-specific lives behind one interface, the
  **agent profile** (§3.7). For each seam, this document says whether the
  seam is in ACP or outside it.
- **R3**: A new ACP agent is added by writing a profile and a config entry.
  Adding one does not edit the protocol layer, and it edits `service.py`'s
  dispatch by one registry line at most.
- **R4**: The gateway is the only permission gate for the new backend, as it
  is for the existing two (ADR-0002). Where an agent cannot be made to ask
  about everything the gateway gates, the document states the gap.
- **R5**: This increment does not break operators of `type: opencode` on
  the 1.x line. The new backend never silently reuses the state of a watcher
  that ran on the old backend.

### 1.4 The increment

**Add an `acp` agent backend that drives any ACP agent over stdio, with
opencode 2.x as its first profile.** §5 specifies the Claude and Codex
profiles. This increment does not build them.

---

## 2. Vocabulary

- **ACP agent**: the subprocess the backend spawns and speaks ACP to.
  Examples: `opencode acp`, `claude-agent-acp`, `codex-acp`.
- **connection**: one ACP agent process plus the JSON-RPC session over its
  stdio. The backend holds one per `agents:` entry (§3.2).
- **ACP session**: a conversation inside an agent, identified by the agent's
  `sessionId`. For opencode, Claude and Codex the ACP id *is* the agent's own
  session or thread id `[src]` `packages/cli/src/acp/service.ts`. §5 cites
  the adapter sources.
- **turn**: one `session/prompt` request plus the `session/update`
  notifications that arrive before its response. One turn is in flight per
  session.
- **ask**: a `session/request_permission` request from the agent. The client
  must answer every one.
- **agent profile**: the per-agent object holding everything outside ACP
  (§3.7). It is not called "adapter", because the backend is the adapter and
  a profile configures it.

---

## 3. Design

### 3.1 Shape

```
gateway/agents/acp/
  __init__.py      AcpBackend (AgentBackend), profile registry
  connection.py    JSON-RPC over stdio: requests, notifications, agent→client requests
  turn.py          session/prompt + update fold → AgentEvent / AgentResponse
  broker.py        AcpPermissionBroker (PermissionBroker), AcpCallablePermissionBroker
  profile.py       AgentProfile interface and the generic defaults
  profiles/
    opencode.py    the opencode profile (this increment)
```

Config:

```yaml
agents:
  oc:
    type: acp
    profile: opencode          # names a registered AgentProfile
    command: opencode          # the profile supplies the subcommand/args
    working_directory: /path/to/project
    # everything else unchanged: permissions, owner/guest_allowed_tools,
    # context_inject_files, timeout, session_prefix
```

`type: acp` is new. `type: opencode` continues to mean the HTTP backend for
the 1.x line (R5). A later increment retires it, not this one. A config with
`type: acp` and no `profile` is a load error that names the registered
profiles. `command` keeps its meaning (the executable). `profile` decides the
argv around it and everything in §3.7.

`working_directory` may be empty today, meaning "inherit the gateway's cwd"
(`gateway/core/config.py:126`). ACP's `cwd` is mandatory and absolute on every
session call. For `type: acp` the config loader therefore resolves an empty
value to the gateway's own canonicalised cwd at load time. `backend_identity`
and invariant 4 use the resolved path. The backend never passes an empty
value through.

**Backend identity.** `backend_identity(agent_type, working_directory)`
(`gateway/core/state.py:506`) scopes a stored session id to the store that
minted it. With one type serving several agents, `type` alone does not name
the store, because a Claude session id must not be replayed into a Codex
process after a config edit. The identity for this backend is `acp:<profile>`
plus the canonicalised working directory. Switching an agent from
`type: opencode` to `type: acp` is therefore an identity change. The
lifecycle refuses the stored session with its existing wording
(`gateway/core/watcher_lifecycle.py:84-89`), re-provisions the record, and
names the old identity in the AUDIT line.

That is the intended migration behaviour, not a defect, because a 1.x
session row and a 2.x session row live in different stores. (2.x migrates
the 1.x database on first start `[src]` `packages/server/src/routes.ts`
`V1Migration`. Whether `session/load` can load a migrated id is
`[unverified]`.) `context_inject_files` and history handoff carry continuity,
as they do for any identity change today.

These places enumerate agent types today, and each gains `acp`:

- `service.py:100-124`
- `gateway/cli.py:318`
- `gateway/configtool/screens/agent_detail.py:122`
- `gateway/configtool/screens/overview.py:59`
- `gateway/configtool/modals.py:153`
- `gateway/tools/tui.py:91`
- `gateway/config.py:908`

The TUI and `AgentSession` path builds the backend without a
`GatewayBrokerConfig` and attaches a callable broker after `start()`, exactly
as for the two existing backends.

### 3.2 Process topology

**The backend runs one ACP agent process per `agents:` entry and multiplexes
sessions over it.** This is the 1.x sidecar topology: `OpenCodeBackend.start()`
spawns one `opencode serve` for every watcher bound to the agent. The ACP
session model supports it. A session id is on every request. opencode keeps a
session map and a per-session turn fiber, so different sessions prompt
concurrently `[src]` `packages/cli/src/acp/sessions.ts`, `turn.ts` `FiberMap`.
The spec states that one agent can run several independent sessions `[docs]`.

The alternative is one process per watcher. It would make process-global
mechanisms usable for per-watcher content (opencode's `instructions` config
key, or an agent/mode with a `system` prompt). This increment rejects it for
the reason the 1.x adapter gives for not using `config.instructions`
(`gateway/agents/opencode/adapter.py`, `ensure_durable_instructions`
docstring): an `agents:` entry is many-to-one with watchers, so anything
scoped to the process leaks one watcher's identity header into another's
session. It would also multiply memory by the number of rooms, because an
`opencode acp` process is two processes, the bridge and a private server
`[src]` `packages/cli/src/services/standalone.ts`. The idle/expiry lifecycle
(`docs/design/dynamic-watcher-design.md` §2.5) would make per-watcher
processes affordable later. The profile interface does not preclude it.

Whether `claude-agent-acp` and `codex-acp` serve concurrent top-level
sessions in one process is `[unverified]`, because their READMEs do not say.
The backend therefore carries a profile flag, `sessions_per_process:
many | one`. With `one`, the connection pool is keyed by session id. opencode
is `many`.

### 3.3 The connection

**Spawn.** The backend runs `profile.argv(command)` with
`profile.env(sidecar_env)` and `cwd=working_directory`. stdout and stdin carry
ACP only. The backend drains stderr to the log as `_drain_pipe` does today.
Shutdown is "close stdin, wait, terminate, kill", with the same bounded waits
as `OpenCodeBackend.stop()`. Self-heal on a dead process reuses the
`_ensure_live_runtime` design (restart lock, circuit breaker after
`_MAX_RESTART_FAILURES`), with one difference: after a restart, the new
process knows none of the sessions the gateway holds until the backend
re-attaches them (§3.11).

**Handshake.** The backend sends `initialize` with `protocolVersion: 1` and
these client capabilities, fixed for every profile:

```json
{"fs": {"readTextFile": false, "writeTextFile": false},
 "terminal": false,
 "auth": {"terminal": false},
 "session": {"compaction": {}}}
```

Why each value:

- `fs` and `terminal` are off. The agent then must not call `fs/*` or
  `terminal/*` `[docs]`, and it does its own I/O inside its own tools, which
  is what the gateway's permission model assumes. opencode never calls them
  regardless `[src]` `packages/cli/src/acp/connection.ts`.
- `auth.terminal` is off, because a terminal-type auth method requires
  relaunching the agent interactively and a daemon cannot do that. Every
  agent is authenticated out of band (§3.7, P2).
- `session.compaction` is on, because compaction notifications are the
  signal rung (c) of §3.8 may use later.
- The backend does **not** set `_meta["opencode/child-session-updates"]`.
  Without it, opencode projects a subagent's asks onto the parent session
  `[src]` `packages/cli/src/acp/child.ts`, and the parent session is the one
  the gateway can route to a room.

The backend keeps the response's `agentCapabilities` for the connection's
lifetime. §3.4 and §3.8 branch on it.

**Client library (D1).** The backend consumes eight methods and answers two.
`connection.py` implements them as a hand-rolled client: newline-delimited
JSON-RPC with id multiplexing, notification dispatch, and agent→client
request dispatch. That is on the order of a hundred lines for the surface
used here. It stays auditable and adds no dependency.

The alternative is `agent-client-protocol` from PyPI, the SDK in the
protocol's own organisation. Its stable release is 0.12.1 (2026-08-16), with
1.0.0rc3 (2026-10-12) still changing which methods it routes. It depends on
`pydantic>=2.7`, which is not a gateway dependency. The gateway owns the
handling of the `[unverified]` wire details in §8 either way. `connection.py`
is the only module the choice touches, so the SDK can replace the hand-rolled
client at its 1.0 without reaching the rest of the backend.

### 3.4 Backend method map

| `AgentBackend` (`gateway/agents/__init__.py`) | ACP | Notes |
|---|---|---|
| `start()` / `stop()` | spawn + `initialize`. Close stdin + terminate | idempotent, as today |
| `create_session(working_directory, extra_args, session_title)` | `session/new {cwd, mcpServers: []}` | Returns the agent's id. **No init prompt**, because `session/new` returns an id without a model turn, unlike `claude -p`. The 1.x opencode adapter's `_INIT_PROMPT` came from the CLI era and is dropped. `extra_args` and `session_title` have no ACP equivalent. The backend logs them at DEBUG if given. A profile may map `session_title` to an agent-specific call. No profile does in this increment. |
| re-attach after a restart (§3.11) | `session/resume {sessionId, cwd}` if `sessionCapabilities.resume`, else `session/load {sessionId, cwd, mcpServers: []}` with the replayed updates discarded | `cwd` must equal the session's directory, or opencode rejects with `invalid_params` `[src]` `service.ts` `getSession` |
| `delete_session(session_id)` | `session/delete` if `sessionCapabilities.delete`, else return `False` | opencode cancels the turn and removes the row `[src]` `service.ts` `deleteSession` |
| `send()` | `stream()` consumed to its final event | one code path, as the base class already allows |
| `stream()` | `session/prompt {sessionId, prompt: [...]}` + `session/update` fold (§3.5) | |
| `ensure_durable_instructions()` / `reclaim_durable_instructions()` | §3.8 | the file-under-`RUNTIME_DIR/system-prompts/<path_key>.md` contract is kept verbatim |
| `create_gateway_broker()` / `create_callable_broker()` | §3.6 | |
| `supports_per_message_env` | `False` | the process is spawned once. Role is the broker's business, as for the 1.x sidecar |
| `typical_session_retention_days()` | `profile.session_retention_days` | opencode: `None` (SQLite rows, no expiry) |

### 3.5 The turn

**Prompt.** The backend sends these content blocks, in order:

1. the durable instructions, if the profile's strategy is a per-turn one
   (§3.8)
2. one `text` block with the prompt the gateway already builds (trusted
   connector prefix + body)
3. one `resource_link {uri: file://<abs path>, name}` per attachment (§3.9)

**Fold.** The backend folds `session/update` into `AgentEvent`, mirroring what
the 1.x SSE parser emits (`gateway/agents/opencode/adapter.py`
`_parse_sse_events`):

| update | event |
|---|---|
| `agent_message_chunk` | accumulated into the reply text. No event |
| `agent_thought_chunk` | first chunk per `messageId` → `thinking` (`💭 …`), as the 1.x parser emits one per reasoning part |
| `tool_call` | `tool_call` (`🔧 <name or title>`), once per `toolCallId` |
| `tool_call_update` with `status: completed \| failed` | `tool_result` (`✓ <name>`), once per `toolCallId` |
| `usage_update`, `plan`, `available_commands_update`, `config_option_update`, `current_mode_update`, `session_info_update`, `compaction_*`, `notice`, `user_message_chunk` | consumed, not emitted. The backend records `compaction_update` for §3.8 |

**Final.** The `session/prompt` response ends the turn.

- `stopReason` `end_turn` and `max_tokens` are a normal reply.
- `refusal` is a reply with `is_error=True`.
- `cancelled` occurs only when the gateway cancelled (below).
- Text is the joined `agent_message_chunk`s. Empty text becomes
  `(empty response)`. The backend sets `is_error` when no tool call was seen
  either, which is the rule `_parse_sse_events` applies.
- `num_turns` is the number of distinct `tool_call` ids plus one. That is a
  different measure from the 1.x step count (`step-finish` parts), stated
  here so the log is not read as comparable.
- `usage` comes from `profile.usage(response)`. Stable ACP v1 has **no
  per-turn token field**. The `usage` object on the prompt response is a
  Draft RFD that opencode `[src]` `translate.ts` `response`,
  claude-agent-acp and codex-acp already emit. The generic default reads it
  when present and leaves `usage` `None` otherwise.
- `cost_usd` likewise. opencode sends the session's cumulative cost on
  `usage_update`. The profile may diff it, or leave cost `None`.
- `duration_ms` is wall-clock from the backend's own timer.

**Timeout and cancel.** The 1.x adapters raise `asyncio.TimeoutError` and
abandon the turn. Under ACP the backend has to end the turn on the wire:

1. Send `session/cancel {sessionId}`, a notification.
2. Answer every pending ask for that session with `{outcome: "cancelled"}`.
   The spec requires it, and opencode maps it to `reject` `[src]` `turn.ts`
   `queueAsk`.
3. Wait, bounded, for the prompt response, which must carry
   `stopReason: cancelled`. opencode's own drain is 5 s `[src]` `turn.ts`
   `CancelDrainTimeout`.
4. Raise `asyncio.TimeoutError`, so `AgentTurnRunner._execute_agent`
   (`gateway/core/agent_turn_runner.py`) posts its timeout message as today.

A second `session/prompt` on a session with a turn in flight is an error on
opencode `[src]` `turn.ts` `prompt`. The per-room serial queue already
prevents it on the message path. The backend also holds a per-session lock,
so the scheduler and the context injector cannot race a chat turn.

### 3.6 The permission broker

The broker's *shape* changes. Its decisions do not.

In 1.x the opencode broker is a separate SSE listener with its own HTTP reply
client (`OpenCodePermissionBroker._listen_sse`, `_reply_to_opencode`). Under
ACP an ask is a request on the same connection the turn runs on. The broker is
the handler the connection invokes, and its return value *is* the reply. That
is the structure of `ClaudePermissionBroker`, not of the opencode one. Of
`PermissionBroker` (`gateway/core/permission.py`), the backend reuses
`request_permission()` unchanged (line 82: id, registry, notify, await with
timeout).

**Ladder.** The broker decides each ask in this order. Both existing brokers
implement the same ladder.

1. No room for the session → deny. Exception: owner + `skip_owner_approval`
   → allow, because no room is needed for that.
2. Role from `session_role_map`, default `guest` (fail closed).
3. Guest → allow when `guest_allowed_tools` matches, else deny. A guest never
   reaches a human.
4. Owner + `skip_owner_approval` → allow.
5. Owner + `owner_allowed_tools` match → allow.
6. Owner + `human_approval` false → deny.
7. Owner → `request_permission()`. Timeout or notification failure → deny.

The ladder is currently written out twice (`claude/broker.py`,
`opencode/broker.py`). This backend would be the third copy. The increment
extracts it into one predicate in `gateway/core` that all three brokers call,
in the same spirit as `is_mention()`. The same change re-points the two
existing brokers at it, and their tests prove the decision table did not move.

**What the ask carries.** `toolCall` is a `ToolCallUpdate`: `toolCallId`,
`title`, `name?`, `kind?`, `rawInput?`, `locations?`, `content?`. The ladder
needs `(tool_name, param_strings)` for `all_params_match_any`
(`gateway/core/tool_match.py`). `profile.normalize_ask(tool_call) ->
(tool_name, tool_input)` (§3.7) produces the first step of that. The existing
`get_param_strings_for_claude(tool_name, tool_input, working_directory)`
(`tool_match.py:480`) then produces the param strings.

That extractor keys on Claude's names and fields. Its bash branch fires on
`tool_lower == "bash"` and reads `command`. Its file-tool branch reads the
field in `_CLAUDE_PARAM_FIELD`, which is `file_path`. A profile's
`normalize_ask` therefore returns the tool name and input **in that
vocabulary**, with the agent's names and fields mapped onto Claude's. The
ladder, the built-in rules (`tool: "Bash"`, `gateway/core/config.py`) and an
operator's rules then all see one spelling. The mapping is per profile (§4,
P6, for opencode). The tree-sitter split of a compound command is the
gateway's own, as it already had to be (fail-closed, #175), because
opencode's per-sub-command `patterns` are not on the ACP wire.

**Reply.** The broker answers `{outcome: {outcome: "selected", optionId}}`
with the option whose `kind` is `allow_once` or `reject_once`.
`profile.select_option(options, approved)` picks it, because agents differ.
Codex offers two `reject_once` options: "Decline" continues the turn and
"Cancel" ends it (§5).

**Invariant: the gateway never selects `allow_always`.** On opencode,
`always` persists the tool's proposed patterns as project-scoped allow rules
in the shared SQLite store. Those rules outlive the process and apply to the
operator's own interactive opencode in that directory `[docs]` v2
permissions, `[src]` `packages/cli/src/acp/permission.ts` (it returns
`once|always|reject`). On Claude, `always` writes `updatedPermissions` into
settings. The gateway's allow-lists are the only durable policy (ADR-0002).

**Invariant: the broker answers an ask within the turn that raised it.** The
registry entry's lifetime is the turn's. `session/cancel` answers all of a
session's pending asks `cancelled` before the backend awaits the prompt
response (§3.5). On broker `stop()`, the broker answers pending asks
`cancelled`, as today's `reject_on_cancel` does.

**Callable broker** (the TUI / `AgentSession` path). It is the same handler
with the user's `(tool_name, tool_input) -> bool` in place of the ladder. The
session attaches it after `start()` via `attach_callable_broker`. For this
backend that call just swaps the handler on the connection. There is no
settings file and no second listener.

### 3.7 The agent profile: every seam outside ACP

This is what R2 asks for: the list of things ACP does not carry, each as one
attribute or method of `AgentProfile`, with the generic default. A profile
overrides what its agent needs. "In ACP" means the generic code handles the
seam from the agent's advertised capabilities with no profile involvement.

| # | Seam | In ACP? | `AgentProfile` member | Generic default |
|---|---|---|---|---|
| P1 | launch: argv around `command`, env, cwd | no | `argv(command) -> list[str]`, `env(base: dict) -> dict` | `[command]`, `base` unchanged |
| P2 | authentication | partly. `initialize` lists `authMethods`, and `session/new` fails `-32000 auth_required` | `auth_hint: str` (what the operator must run, e.g. `opencode auth login`) | the error maps to `AgentPermissionError` carrying the hint. Nothing is attempted in-process |
| P3 | **force-ask**: make the agent raise an ask for every call the gateway gates (ADR-0002) | **no**. ACP forwards only what the agent's own policy escalates | `gate_env(base) -> dict` and/or `session_meta() -> dict` (the `_meta` on `session/new`) | none. A profile that leaves this unset gets a load-time warning naming the gap |
| P4 | sessions per process | no (the spec allows many, agents vary) | `sessions_per_process: Literal["many","one"]` | `"many"` |
| P5 | durable instructions | **no** in stable v1. Preview RFD `sessionCapabilities.systemPrompt` | `instructions: InstructionsStrategy` (§3.8) | the capability-probed ladder in §3.8 |
| P6 | ask normalisation: `toolCall` → `(tool_name, tool_input)` | partly. The field names are standard, their content is the agent's | `normalize_ask(tool_call) -> tuple[str, dict]` | `name or kind or "other"`, `rawInput or {}` |
| P7 | reply option choice | partly. `kind` is a UI hint `[docs]` | `select_option(options, approved) -> optionId` | first `allow_once` / first `reject_once` |
| P8 | usage and cost | Draft RFD | `usage(prompt_response, updates) -> TokenUsage \| None`, `cost_usd(...)` | read `response.usage` if present |
| P9 | error classification | partly. JSON-RPC codes `-32000` auth, `-32002` not found, `-32800` cancelled, `-32603` internal | `classify_error(code, message, data) -> AgentExecutionError` | the code map. Rate-limit detection needs the agent's `data.errorName` or message, so it is a profile concern |
| P10 | session retention | no | `session_retention_days: int \| None` | `None` |
| P11 | attachments | yes. `resource_link` is mandatory for every agent `[docs]` | `attachments: "resource_link" \| "inline_text"` | `resource_link` |
| P12 | reject semantics (informational: what the model sees after a deny) | no | documented per profile, not code | — |

P3 is the seam that decides whether R4 holds for an agent, and it is the one
with no generic answer at all. §4 and §5 fill it per agent.

### 3.8 Durable instructions

The contract (`AgentBackend.ensure_durable_instructions`,
`InjectedContextBuilder.ensure`, `message_processor.py:634`) is this. The
backend writes `content` to `RUNTIME_DIR/system-prompts/<path_key>.md` and
returns the path. The processor re-supplies that path on every turn through
`append_system_prompt_file`. Both existing backends work this way. This
backend keeps the contract verbatim, including `reclaim_durable_instructions`.
What differs per agent is how the file's content reaches the model on a turn.
The backend probes capabilities and takes the first rung that applies:

- **(a) `sessionCapabilities.systemPrompt` advertised.** The backend sends the
  Preview RFD's `systemPrompt: {mode: "append", content: [{type: text, …}]}`
  on `session/new`, `session/load` and `session/resume`. That is a real system
  prompt, compaction-resistant, outside history. The RFD's implementation
  table `[docs]` says claude-agent-acp (≥ 0.92, append and override) and
  codex-acp (≥ 2.3, append only) advertise it. That is `[unverified]`
  against the adapters' own changelogs. opencode 2.2.5 does **not** advertise
  it `[src]` `service.ts` `initialize`. The content is attached when the
  session is attached, so a change requires `session/close` plus re-attach,
  and the backend does that. (A room rename re-issues the header,
  `message_processor.py` `rename`.)
- **(b) profile strategy.** The agent has a lever ACP does not expose
  generically. opencode's is in §4.
- **(c) per-turn prefix.** The backend sends the file's content as the first
  `text` block of every `session/prompt`. This rung is always available. It is
  exactly the 1.x discipline ("re-supplied on every call") minus one
  property: the content is in the conversation history, so it costs tokens
  per turn and compaction treats it like everything else. That is tolerable
  precisely because the backend re-sends it every turn. The
  `session.compaction` capability in the handshake exists so a later
  increment can send it only on the first turn and after each
  `compaction_update: completed`. This increment sends it every turn and
  measures.

The backend logs the rung in use once per session at INFO, because it is a
security- and cost-relevant property an operator should be able to read off.

### 3.9 Attachments

The backend sends one `resource_link {uri: "file://…", name, mimeType?}` per
attached file. The spec makes `resource_link` mandatory for every agent.
opencode turns a `file://` link into a file part on the prompt `[src]`
`packages/cli/src/acp/content.ts` `resourceLinkToPart`. This is a gain over
1.x, where the HTTP backend had no upload and injected paths as text
(`build_attachment_prompt`). That helper stays as the `inline_text` fallback
for a profile that opts out (P11).

### 3.10 Errors

| condition | error |
|---|---|
| spawn failure, handshake timeout, process exit | `AgentUnavailableError` (self-heal eligible) |
| JSON-RPC `-32000` | `AgentPermissionError` with `auth_hint` |
| `-32002` on `session/load` or `session/resume` | `AgentExecutionError("session not found")`. The backend never recreates the session silently (requirements §12.3.3) |
| `-32603` with a profile-recognised rate-limit marker | `AgentRateLimitedError` |
| any other JSON-RPC error | `AgentExecutionError`. The message is sanitised to code + `errorName`, never the raw `data` |
| malformed frame | `AgentProtocolError` |

### 3.11 Restart, idle, expiry

A gateway restart or a self-heal restart spawns a new agent process that
holds none of the sessions the state file names. The backend re-attaches
lazily. The first `stream()` or `ensure_durable_instructions()` for a session
the connection has not attached runs `session/resume` (or `session/load`, with
the updates discarded) and records the session as attached. On opencode,
`session/resume` in a fresh process replays nothing, and the next prompt on
that session answers from the earlier turns `[live 2.2.6]`. A `session/load`
replays the whole history before responding (eleven updates for a two-turn
session `[live 2.2.6]`), so on an agent without `resume` that time would
count against the first turn's timeout. The backend therefore
runs the re-attach under its own bounded wait (the start-up timeout, not the
turn timeout) before it sends the prompt. A failed re-attach surfaces as the
structured error in §3.10. The lifecycle's existing handling of a dead stored
session then applies, with the AUDIT line (`gateway/core/session_release.py`).

Idle (§2.5 of the dynamic-watcher design) releases the processor, not the
session. The session stays attached to the connection until the process
exits, which costs nothing on the gateway side. Expiry calls `delete_session`
(`watcher_lifecycle.py:731`) and `reclaim_durable_instructions` (`:751`) as
today.

---

## 4. The opencode profile

The increment's deliverable, seam by seam.

**P1 launch.** `argv = [command, "acp"]`, with cwd = `working_directory`. 2.x
has no `--cwd` flag on `opencode acp` (1.x had one), so the process cwd is the
only way to set the directory `[live 2.2.6]`. The process spawns a private
`opencode serve --stdio --port 0` child with a random `OPENCODE_PASSWORD` and
reaches it over HTTP itself `[src]` `packages/cli/src/services/standalone.ts`,
`commands/handlers/acp.ts`. The child inherits the parent's environment
(`extendEnv: true`), so `env` set on the `opencode acp` process reaches the
config loader `[src]` `standalone.ts`. The inline config takes effect in the
private server `[live 2.2.6]`, see P3. The gateway cannot reach that private
server, because its port and password are random. There is no HTTP side
channel in this topology, and the design does not want one.

Two prior reports say the opposite about inline config under `opencode acp`:
anomalyco/opencode#34638 (1.17.11, Linux, auto-closed) and #52259 (2.0.3 to
2.2.0, Windows). The maintainers could not reproduce either on macOS
(PR #52286). The §8 checklist therefore keeps item 1 for Linux, where the
production gateway runs.

**P2 auth.** `auth_hint = "opencode auth login"`. `session/new` loads the
model catalog first and fails `auth_required` when no provider is usable
`[src]` `service.ts` `newSession`, `error.ts`.

**P3 force-ask.** The profile injects inline config through
`OPENCODE_CONFIG_CONTENT`. That is the mechanism `_build_safe_opencode_config`
already implements (`gateway/agents/opencode/adapter.py`), rewritten in the v2
vocabulary:

```json
{"permissions": [
  {"action": "shell",              "resource": "*", "effect": "ask"},
  {"action": "edit",               "resource": "*", "effect": "ask"},
  {"action": "webfetch",           "resource": "*", "effect": "ask"},
  {"action": "websearch",          "resource": "*", "effect": "ask"},
  {"action": "external_directory", "resource": "*", "effect": "ask"}
]}
```

The list is ordered and the last match wins `[docs]` v2 permissions. The
profile merges a user `OPENCODE_CONFIG_CONTENT` the way the 1.x function
merges it:

- the user's own rules follow the catch-alls
- invalid JSON is a load error
- a user catch-all for an action is respected

`external_directory` is new in the list, because v2 flipped its default to
allow.

The profile does **not** inject the role-enforcement plugin, for three
reasons:

- Its guest path only ever applied to a `COOP_ROLE=guest` process, and the
  sidecar never is one.
- Its owner path was inert on 1.18.13 (ADR-0002).
- 2.x skips a v1 file-path plugin anyway (§1.1).

`gateway/agents/opencode/hooks/` stays for the 1.x backend. The
`memory-bootstrap.ts` in that directory is referenced by nothing but a test
string. This document notes it as orphaned and does not remove it.

Asks reach the gateway because `turn.ts` subscribes to the private server's
events for the turn and forwards every `permission.asked` as
`session/request_permission` `[src]`. `allow` rules never ask. `deny` rules
never ask. With the ruleset above, a `shell` call and a `write` call each
produced a `session/request_permission` before running `[live 2.2.6]`.

**P6 normalisation.** When `tool_call.name` is present, it is the opencode
**tool** name: `shell`, `edit`, `write`, `patch`, `read`, `webfetch`,
`websearch`, `subagent`, … `[src]` `tool.ts`. `rawInput` is that tool's
input: `{command, cwd}` for `shell`, `{path, …}` for file tools (`filePath` on
sessions migrated from 1.x `[src]` `tool.ts` `filePath()`), `{url}`, `{query}`,
`{pattern, path}`. Two things about the 1.x rules change here. The profile
absorbs both, so an existing allow-list keeps meaning what it meant.

- *Names.* 1.x rules were written against opencode's **permission** name
  (`docs/permission-reference.md:119`): `tool: bash`, and `tool: edit`
  covering the write, edit and multiedit tools. ACP carries the tool name, so
  `write` and `patch` asks no longer arrive as `edit`. The profile maps the
  ask onto the Claude vocabulary the extractor and the built-in rules use:
  - `shell` → `Bash`
  - `edit` → `Edit`, `write` → `Write`, `patch` → `Edit`
  - `read` → `Read`
  - `webfetch` → `WebFetch`, `websearch` → `WebSearch`
  - `subagent` → `Task`

  Rule matching accepts the 1.x spellings as aliases. A rule `tool: bash`
  matches a normalised `Bash`. A rule `tool: edit` matches `Edit` **and**
  `Write`, which is the 1.x meaning of that rule. That is the one alias that
  is not a plain rename, and the migration note states it.
- *Fields.* The profile renames `path` / `filePath` to `file_path`.
  `command`, `url`, `query` and `pattern` already match `_CLAUDE_PARAM_FIELD`.
  Without the path rename every file-tool ask would produce the empty param
  string and fail every `params` rule. That is fail-closed, but the
  `scratch-dir` preset would stop auto-approving.

**The action gap.** The ask carries the **tool** that triggered it, never the
permission **action** that fired. `permission.ts` `ask` builds `name` from
`input.tool?.name` and nothing on the request carries `event.data.action`
`[src]`. A `write` to `/tmp/x` under the ruleset above produced **two**
asks, one for `external_directory` and one for `edit`, and both arrived with
`name: "write"`, the same `rawInput {path, content}` and the same `locations`
`[live 2.2.6]`. The 1.x broker matched the action string (`tool:
external_directory`, which the `scratch-dir` preset in `config.example.yaml`
uses and `docs/permission-reference.md:119` documents). That string is not on
the ACP wire.

Consequences:

- A rule on the tool and the path covers both asks. After P6 normalisation
  the `scratch-dir` preset is `tool: Write` (or the 1.x alias `tool: edit`)
  with `params: /tmp/.*`, and it auto-approves the `external_directory` ask
  and the `edit` ask alike. The migration note rewrites the preset.
- A rule written on the action alone (`tool: external_directory`) matches
  nothing on this backend. Config loading warns on such a rule for a
  `type: acp` agent.
- The broker evaluates the ladder once per ask, so an external write that
  needs a human produces two approval requests in chat, as it did on 1.x
  (one `external_directory`, one `edit`).
- An ask with no tool source would arrive with no `name`. The profile then
  uses `kind` as the tool name and the `locations` paths as the param
  strings. No such ask was observed. `doom_loop` is the candidate and is
  `[unverified]` (§8, item 2).

The clean fix is upstream: an `_meta["opencode/permission"] = {action,
resources}` on the request, which the protocol's extensibility rules invite.
That is anomalyco/opencode#62715 (D4). Related upstream history:
anomalyco/opencode#30554 asked for context on `external_directory` asks, and
PR #30567 added the command, path and `locations`. The action itself was not
part of that fix.

**P7 options.** Exactly `once` (`allow_once`), `always` (`allow_always`) and
`reject` (`reject_once`) `[src]` `permission.ts`. The default selector is
correct.

**P8 usage.** `response.usage {inputTokens, outputTokens, totalTokens,
thoughtTokens?, cachedReadTokens?, cachedWriteTokens?}` is summed over the
turn's steps `[src]` `translate.ts` `response`, `[live 2.2.6]`. The profile
maps it to `TokenUsage` with `reasoning_tokens = thoughtTokens` and
`cache_read_tokens = cachedReadTokens`. It ignores `totalTokens`, because
that field counts the cached read as well (observed: `inputTokens: 294`,
`outputTokens: 19`, `cachedReadTokens: 10752`, `totalTokens: 11084`) and
`TokenUsage.total_tokens` is defined as input plus output. For cost,
`usage_update.cost.amount` is the session's cumulative cost in USD `[src]`
`turn.ts` `sendUsageUpdate`. The profile keeps the last seen value per session
and reports the difference as `cost_usd`. It reports `None` on the first turn
after attach.

**P5 instructions.** Rung (c), with one opencode-specific refinement
available as strategy (b). opencode splits off a `text` block carrying
`annotations: {audience: ["assistant"]}` and delivers it as a *synthetic*
steer message before the prompt. The model sees it, a UI's transcript hides
it, and it is not a system prompt `[src]` `content.ts` `contentBlockToParts`,
`prompt.ts`, `turn.ts` `submit`. The profile uses that annotation for the
durable content, so the header does not read as user text. The content is
still in history and still re-sent per turn.

opencode 2.2 added a compaction-resistant mechanism: per-session instruction
entries (`session.instructions.entry.put`, `[src]` `handlers/session.ts`). It
is reachable only over the private server's HTTP, so it is out of reach (P1).
anomalyco/opencode#62716 asks opencode to implement the systemPrompt RFD on
top of those entries (D4). Related upstream history: anomalyco/opencode#27500
and PR #27478 tried, before 2.x, to forward a `_meta.systemPrompt` from
`session/new` into each prompt's `system` field. The value was never stored on
the session, and the PR was closed without merging. When #62716 lands, the
opencode profile moves to rung (a) with no gateway change beyond the
capability probe.

**P9 errors.** opencode's error names ride `error.data.errorName`
(`ServerUnavailable`, provider error types) `[src]` `error.ts`
`toRequestError`. `provider.auth` arrives as `-32000`. A rate-limit marker is
`[unverified]`. The profile starts with the spirit of the 1.x HTTP mapping
(429-like names → `AgentRateLimitedError`) and logs the unmapped case.

**P12 reject.** A `reject` ends the model's turn with no text, as on 1.x
(`docs/permission-reference.md`), so the chat sees an empty reply. The
behaviour is unchanged and documented.

**Dropped from the 1.x backend:**

- the init prompt
- the `GET /config` plugin verification, because there is no plugin
- the SSE stream parser and the blocking POST path
- the HTTP reply client

**Kept in spirit:**

- the config merge rules
- self-heal
- orphan tracking (`session/delete` best-effort on a failed start)
- the per-watcher instructions file

**Migration notes for the operator guide.** The guide is written when the
increment ships, not here. Its contents:

- `type: opencode` stays on 1.19.x.
- A 2.x install takes `type: acp` + `profile: opencode`.
- Sessions are re-provisioned (§3.1).
- Allow-list rules on `tool: bash` and `tool: edit` keep working through the
  P6 aliases, and `tool: edit` still covers writes as it did.
- A rule on `tool: external_directory` matches nothing on this backend. The
  `scratch-dir` preset becomes a path rule on the write tool (§4, the action
  gap).
- coop-keeper's `agents/coop-keeper/opencode.json` is v1-shaped
  (`permission` map, `default_agent`). 2.x auto-migrates it `[src]`
  `normalize.ts`. It ships from this repository and is due a v2 review. That
  review is out of this increment.

---

## 5. Adding another ACP agent

The steps come first, then the two examples. Each row in the examples is
tagged.

1. Write `gateway/agents/acp/profiles/<name>.py` implementing `AgentProfile`
   (§3.7). Every seam not overridden gets the generic default. Leaving P3
   unset is a load-time warning.
2. Register it in the profile registry (one line).
3. Document P3 in `docs/permission-reference.md`: what the agent asks about
   and what it never will. That is the table an operator reads to know
   whether their allow-list means what they think.
4. Add the profile's wire fixtures to the fake ACP agent (§10), and a test
   per seam that differs from the default.
5. Run the §8 checklist against the live agent.

### 5.1 Claude Code via `@agentclientprotocol/claude-agent-acp`

npm deprecates the `@zed-industries/claude-code-acp` package in favour of
this one. v0.100.0 (2026-11-02) is built on the Claude Agent SDK. The project
releases several times a week through release-please `[docs]`. No native
`claude --acp` exists `[docs]` CLI reference, changelog.

| seam | Claude | tag |
|---|---|---|
| P1 | `npx @agentclientprotocol/claude-agent-acp` (or the installed `claude-agent-acp`). `CLAUDE_CODE_EXECUTABLE` picks the CLI. `CLAUDE_CONFIG_DIR` is honoured | `[docs]` README, `[src]` `src/claude-cli.ts` |
| P2 | The SDK child inherits the environment, so `ANTHROPIC_API_KEY`, `apiKeyHelper` or the `~/.claude` login all work. `auth_hint = "claude /login"` | `[src]` `acp-agent.ts` env spread |
| P3 | **The hard one.** The adapter sends an ask only when the SDK calls `canUseTool`, and the SDK auto-approves before that: deny/ask/allow rules from settings, the permission mode, working-directory reads, Glob/Grep, read-only Bash. Mode alone cannot force asks. Route: `session/new._meta.claudeCode.options = {settingSources: [], settings: <gateway-owned settings>}`, where the settings carry a `PreToolUse` command hook (matcher `.*`) that answers `permissionDecision: "ask"`. That is the gateway-owned settings idea of today's `settings_adapter.py`, delivered through `_meta` instead of `--settings`. Whether a command hook inside the programmatic `settings` tier fires with `settingSources: []` is **`[unverified]`**. The documented route is a project `.claude/settings.json` with `settingSources: ["project"]`, which touches the operator's project directory. `allowDangerouslySkipPermissions` is not involved, because the adapter refuses to auto-allow on the client's behalf | `[docs]` agent-sdk permissions, `[src]` `acp-agent.ts` `canUseTool`, options merge |
| P4 | `[unverified]`. Start with `one` | — |
| P5 | rung (a). `systemPrompt` append/override is advertised (≥ 0.92 per the RFD table). The CLI records a conversation's system prompt on the first request and applies a changed one at the next compaction | `[docs]` RFD, `[src]` `src/client-system-prompt.ts` |
| P6 | `name` is the Claude tool name (`Bash`, `Edit`, `Read`, …) and `rawInput` is the tool input, so `get_param_strings_for_claude` applies unchanged | `[src]` |
| P7 | `allow_once`, `allow_always` (writes `updatedPermissions`), `reject_once`. Plan-mode asks offer mode switches. Select `reject_once` for those | `[src]` `src/permissions/options/*` |
| P8 | `response.usage` + `_meta.quota` (per-model usage, spend) | `[src]` |
| P12 | `reject_once` → a deny message, and **the turn continues**. The model sees the refusal as the tool result | `[src]` `src/permissions/effects.ts` |

Session ids are Claude Code session UUIDs. `session/load` reads the
transcript under `~/.claude/projects/*`, so a session created here is
resumable with `claude --resume`, and vice versa `[src]`
`src/resumed-session.ts`. This profile is a *second* Claude path. The
existing `type: claude` (`claude -p` + HTTP PreToolUse hook +
`--dangerously-skip-permissions`) is today the stricter, verified gate, and
this profile does not supersede it.

### 5.2 Codex via `@agentclientprotocol/codex-acp`

v2.5.0 (2026-10-29), TypeScript. It is a bridge in front of `codex
app-server`. app-server is OpenAI's own JSON-RPC protocol (`thread/start`,
`turn/start`, `item/*/requestApproval`), not ACP `[src]`
`codex-rs/app-server/README.md`. The Rust `zed-industries/codex-acp` is
archived. No native `codex acp` exists `[docs]`.

| seam | Codex | tag |
|---|---|---|
| P1 | `npx -y @agentclientprotocol/codex-acp`. `CODEX_PATH`, `CODEX_CONFIG` (a JSON overlay on the session config), `INITIAL_AGENT_MODE` | `[docs]` README |
| P2 | `CODEX_API_KEY` or `OPENAI_API_KEY`, or the ChatGPT login. `NO_BROWSER=1` | `[docs]` |
| P3 | The adapter sends an ask only when app-server emits an approval request, i.e. what Codex's `approval_policy` + sandbox escalate. The four built-in modes pin `on-request` / `never` on every `turn/start`. The only route to `untrusted` ("commands require approval unless an exec-policy rule allows them") is a custom permission profile (`INITIAL_AGENT_MODE=permission-profile:<id>`, policy read from `config/read`), and Codex's own trusted exec-policy list still auto-runs. **Partial gate, `[inferred]`.** `permission-reference.md` documents it as such | `[src]` `src/AgentMode.ts`, `CodexAcpClient.ts` |
| P4 | `[unverified]`. Start with `one` | — |
| P5 | rung (a). `systemPrompt` append only (≥ 2.3) → app-server `developerInstructions` | `[src]` `src/ClientSystemPrompt.ts` |
| P6 | Command-execution and file-change asks carry the command or paths in `rawInput`. Map by `kind` (`execute` → `Bash`-shaped, `edit` → path) | `[src]` `src/permissions/CodexApprovalHandler.ts` |
| P7 | `allow_once` (accept), `allow_always`, and two `reject_once`: **Decline** (the turn continues) and **Cancel** (the turn ends). Select Decline for a gateway deny | `[src]` `src/permissions/options.ts`, `codex-rs` `ReviewDecision` |
| P8 | `response.usage` from `thread/tokenUsage/updated`. No USD cost | `[src]` `src/TokenCount.ts` |

Session ids are Codex thread ids. `session/load` is `thread/resume` plus a
history replay `[src]`.

### 5.3 What the two examples show about the interface

- P3 has no generic answer, and it is partial for Codex.
- P5 is generic for both adapters and not for opencode.
- P7 differs in a way the ladder must know (Codex's two rejects).

Those three seams justify a profile object rather than a config table.
Everything else is a default.

---

## 6. Invariants

1. The gateway never selects `allow_always` (§3.6).
2. The broker answers every ask. A cancelled turn answers its asks
   `cancelled` before the backend awaits the prompt response (§3.5).
3. One prompt is in flight per session. The backend serialises per session.
4. `cwd` on `session/new`, `load` and `resume` is the agent's
   `working_directory`, canonicalised the way `backend_identity` does.
5. The instructions file is named by the opaque `path_key` and lives under
   `RUNTIME_DIR/system-prompts/`, as for both existing backends.
6. The backend never recreates a stored session id silently on `-32002`.
7. The backend logs the durable-instructions rung in use once per session.

---

## 7. Decisions

- **D1**: `connection.py` is a hand-rolled client (§3.3). The PyPI SDK
  `agent-client-protocol` is re-evaluated at its 1.0 release.
- **D2**: `type: acp` + `profile:`, with `type: opencode` left as the 1.x
  HTTP backend (§3.1, R5). Swapping the implementation under `type: opencode`
  would be a silent behaviour change for 1.19.x users, and it would remove
  the path back to the HTTP backend while the ACP backend is new. The 1.x
  backend's retirement is a separate, later increment.
- **D3**: per-agent process topology (§3.2), with `sessions_per_process` as
  the escape hatch. Per-watcher processes are a later option that the idle
  lifecycle would make affordable.
- **D4**: two proposals are filed upstream as opencode feature requests:
  anomalyco/opencode#62715, an `_meta` carrying the permission `action` and
  `resources` on `session/request_permission` (§4, the action gap), and
  anomalyco/opencode#62716, the client-provided systemPrompt RFD on top of
  session instruction entries (§4, P5). The design works without either.
  Each one, when it lands, removes a workaround named in §4.

---

## 8. Verification prerequisites

Each item is settled against a live opencode 2.x before the opencode profile
is called done. The state of each item on 2.2.6, macOS:

1. `OPENCODE_CONFIG_CONTENT` on the `opencode acp` process reaches its
   private `--stdio` server, and a `shell`, `edit` or `external_directory`
   call under the §4 ruleset produces `session/request_permission`.
   **Settled on macOS** `[live 2.2.6]`. **Open on Linux**, because #34638
   and #52259 report the variable inert there and on Windows, and Linux is
   where the production gateway runs.
2. The wire shape of an ask with no tool source. `external_directory` on a
   `/tmp` write turned out to be tool-sourced (`name: "write"`)
   `[live 2.2.6]`. **Open**: `doom_loop`, which needs the same tool call
   three times with identical input.
3. `session/resume` of a session created by a previous `opencode acp`
   process, same data directory, same `cwd`, followed by a prompt turn on it.
   **Settled** `[live 2.2.6]`.
4. `always` persists as a project allow rule in the shared store (the reason
   for invariant 1). **Open by choice**: the test writes a rule into the
   operator's store. The docs are the evidence until a disposable store is
   set up for it.
5. The handshake, `session/new`, a prompt turn and a permission round-trip.
   **Settled** `[live 2.2.6]`, Appendix A.

For the Claude and Codex profiles, the `[unverified]` rows in §5 are the
checklist.

---

## 9. Delivery

1. `connection.py`, the fake ACP agent, and unit tests for: handshake,
   request/response multiplexing, notifications, agent→client requests,
   stderr drain, shutdown, and the cancel path (which does not use
   `$/cancel_request`).
2. `turn.py`: the fold and the final response, against recorded fixtures.
   opencode's `packages/cli/test/acp/wire-fixture.ts` is the shape reference.
3. The shared permission predicate, extracted from the two existing brokers
   with their tests green. `broker.py` on top of it.
4. `AcpBackend`, `AgentProfile` and the opencode profile. `service.py`
   dispatch, the config schema, the `coop config` and TUI enumerations,
   `check_backend_signatures`.
5. The §8 checklist against 2.2.5. Then the docs: `docs/permission-reference.md`,
   `docs/architecture.md` ("Adding a New Agent Backend" becomes profiles),
   `docs/supported-features.md`, `config.example.yaml`.
6. Separately, later: the Claude and Codex profiles when wanted, the 1.x
   backend retirement, and the per-watcher topology if measurement of rung
   (c)'s cost says so.

---

## 10. Testing

- A **fake ACP agent** as a Python stdio subprocess in `tests/helpers.py`
  (CLAUDE.md: shared by default). It has scripted responses, scripted asks,
  scripted updates and configurable capabilities. Every unit test of
  `connection.py`, `turn.py`, `broker.py` and the backend talks to it. It is
  also how a profile's P3, P6 and P7 differences are tested without the
  agent.
- Broker tests reuse `test_claude_broker.py`'s ladder cases verbatim through
  the shared predicate. The ACP-specific cases are:
  - cancelled-on-cancel
  - `allow_always` never selected
  - unnamed-ask normalisation (§4)
  - the Codex two-reject selection
- Backend tests mirror `test_opencode_adapter.py`'s lifecycle cases
  (start/stop idempotence, self-heal, circuit breaker, orphan cleanup) and
  add re-attach after restart.
- A manual/e2e script under `tests/manual/` runs the §8 checklist and prints
  the frames, so the `[unverified]` items become `[live 2.2.5]` with evidence
  pasted into the PR.

---

## Appendix A: observed frames

All frames `[live 2.2.6]`, macOS, `opencode acp` spawned with the §4 ruleset
in `OPENCODE_CONFIG_CONTENT`. Long paths are shortened to `<cwd>`.

`initialize` response:

```json
{"protocolVersion": 1,
 "agentCapabilities": {
   "loadSession": true,
   "mcpCapabilities": {"http": true, "sse": false},
   "promptCapabilities": {"embeddedContext": true, "image": true},
   "sessionCapabilities": {"additionalDirectories": {}, "close": {}, "delete": {},
                           "fork": {}, "list": {}, "resume": {}},
   "_meta": {"opencode/child-session-updates": true}},
 "authMethods": [{"id": "opencode-login", "name": "Login with opencode",
                  "description": "Run `opencode auth login` in the terminal"}],
 "agentInfo": {"name": "OpenCode", "version": "2.2.6"}}
```

`session/new` response (the `options` list is elided):

```json
{"sessionId": "ses_e45d8fb8affenLVxphDSWqXG9k",
 "configOptions": [{"id": "model", "name": "Model", "category": "model", "type": "select",
                    "currentValue": "opencode/iceman-free", "options": ["…"]},
                   "…"]}
```

`session/request_permission` for the shell call (`printf hello`):

```json
{"sessionId": "ses_e45d8fb8affenLVxphDSWqXG9k",
 "toolCall": {"toolCallId": "514d98b0-…", "name": "shell", "title": "printf hello",
              "kind": "execute", "status": "pending",
              "locations": [{"path": "<cwd>"}],
              "rawInput": {"command": "printf hello", "cwd": "<cwd>"}},
 "options": [{"optionId": "once",   "kind": "allow_once",   "name": "Allow once"},
             {"optionId": "always", "kind": "allow_always", "name": "Always allow"},
             {"optionId": "reject", "kind": "reject_once",  "name": "Reject"}]}
```

`session/request_permission` for the write to `/tmp/coop-acp-probe.txt`. Two
arrived with the same `toolCallId`, one per permission action
(`external_directory`, then `edit`). The second one also carried a `content`
diff. Neither names the action:

```json
{"sessionId": "ses_e45d8fb8affenLVxphDSWqXG9k",
 "toolCall": {"toolCallId": "a1491130-…", "name": "write",
              "title": "/tmp/coop-acp-probe.txt", "kind": "edit", "status": "pending",
              "locations": [{"path": "/tmp/coop-acp-probe.txt"}],
              "rawInput": {"path": "/tmp/coop-acp-probe.txt", "content": "hi"},
              "content": [{"type": "diff", "path": "/tmp/coop-acp-probe.txt",
                           "oldText": null, "newText": "hi"}]},
 "options": ["same three as above"]}
```

`tool_call_update` on completion, shell:

```json
{"sessionUpdate": "tool_call_update", "toolCallId": "514d98b0-…", "status": "completed",
 "locations": [{"path": "<cwd>"}],
 "content": [{"type": "content", "content": {"type": "text", "text": "hello"}}],
 "rawOutput": {"metadata": {"status": "completed", "truncated": false, "exit": 0}}}
```

`usage_update`, then the `session/prompt` response, first turn:

```json
{"sessionUpdate": "usage_update", "used": 11022, "size": 262144,
 "cost": {"amount": 0, "currency": "USD"}}
```

```json
{"stopReason": "end_turn",
 "usage": {"inputTokens": 21851, "outputTokens": 53, "totalTokens": 21991, "thoughtTokens": 87},
 "_meta": {}}
```

`session/resume` response in a fresh process (no replay), then the response
of a prompt on the resumed session:

```json
{"configOptions": ["…"]}
```

```json
{"stopReason": "end_turn",
 "usage": {"inputTokens": 294, "outputTokens": 19, "totalTokens": 11084,
           "thoughtTokens": 19, "cachedReadTokens": 10752},
 "_meta": {}}
```

`session/load` of the same session replayed eleven `session/update`
notifications (`user_message_chunk`, `agent_message_chunk`, `tool_call`,
`tool_call_update`) before its `{configOptions: […]}` response.

## Appendix B: sources

- opencode `v2.2.5`: `packages/cli/src/acp/{agent,service,sessions,turn,permission,prompt,content,tool,translate,replay,config-option,child,error,capabilities}.ts`, `packages/cli/src/commands/handlers/acp.ts`, `packages/cli/src/services/standalone.ts`, `packages/server/src/handlers/{session,permission}.ts`, `packages/schema/src/{permission,session-inbox}.ts`, `packages/core/src/config/normalize.ts`, `CHANGELOG.md`. Docs: `cli/acp`, `permissions`, `config`, `migrate-v1`.
- ACP: `agentclientprotocol.com/protocol/v1/*`, `schema/v1/schema.json` (artifact `schema-v1.29.1`), RFDs `client-system-prompt` (Preview), `end-turn-token-usage` (Draft), `session-usage` (Completed). Registry: `cdn.agentclientprotocol.com/registry/v1/latest/registry.json`.
- Adapters: `agentclientprotocol/claude-agent-acp` @ `1d4418f` (v0.100.0), `agentclientprotocol/codex-acp` @ `519c267` (v2.5.0). Claude Agent SDK permissions docs. `openai/codex` `codex-rs/app-server`.
- Python SDK: `agentclientprotocol/python-sdk`, PyPI `agent-client-protocol` 0.12.1 / 1.0.0rc3.
