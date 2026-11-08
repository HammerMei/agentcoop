# ACP agent backend

Status: **design, not implemented.** Primarily a design for driving opencode
2.x through the Agent Client Protocol (ACP); secondarily the shape of a backend
that can drive any ACP agent, with Claude Code and Codex as the worked
examples (§5).

Evidence basis. Gateway claims cite the current tree. opencode claims cite the
`v2.2.5` tag of `anomalyco/opencode` (the same repository renders as
`sst/opencode`), read from source and not yet observed on a running 2.x, so
every 2.x statement carries one of these tags:

- `[src]` read in the named file at `v2.2.5`
- `[docs]` from `opencode.ai/v2/docs` or `agentclientprotocol.com`
- `[live 1.18.34]` observed on the local 1.18.34 `opencode acp` over stdio
- `[inferred]` / `[unverified]` — must be settled by the checklist in §8
  before the implementation is called done

---

## 1. Problem

### 1.1 What opencode 2.x breaks

The `opencode` backend (`gateway/agents/opencode/adapter.py`) drives
`opencode serve` over its HTTP API and the `/event` SSE stream, and gates tool
calls through `permission.asked` → `POST /permission/{id}/reply`
(`gateway/agents/opencode/broker.py`). Verified against opencode 1.18.13. The
2.x line (first release `v2.2.0`, 2026-10-27; `v2.2.5`, 2026-10-30; the 1.19.x
line is released in parallel and is what npm and Homebrew still carry) changes
every one of those contracts:

| 1.x contract the gateway uses | 2.x | Consequence | Source |
|---|---|---|---|
| `POST /session {directory}` | `POST /api/session {location: {directory}, permissions?, agent?, model?, …}`; every route is under `/api` | fails loudly | `[src]` `packages/server/src/handlers/session.ts` `session.create`; `packages/protocol/src/groups/session.ts` |
| `POST /session/{id}/message {parts, system?}` (blocking) | removed. `POST /api/session/:id/prompt {text, files?, delivery?, …}` is asynchronous and has **no `system` field** | fails loudly; the durable-instructions channel is gone | `[src]` `handlers/session.ts` `session.prompt`; `packages/schema/src/session-inbox.ts` |
| `GET /event` events `message.part.delta` / `message.part.updated` / `session.status` / `session.error` | replaced by `session.text.delta`, `session.tool.*`, `session.step.*`, `session.execution.*`, `session.compaction.*` | stream parser produces nothing; every turn times out | `[src]` `packages/cli/src/acp/translate.ts` |
| `permission.asked {properties: {permission, patterns, metadata, always, tool}}` → `POST /permission/{id}/reply {reply}` | `permission.asked {data: {action, resources, metadata?, source?, save?}}` → `POST /api/session/:id/permission/:requestID/reply {decision}` | broker never answers an ask; the tool waits for the agent timeout (#165 again) | `[src]` `packages/schema/src/permission.ts`; `handlers/permission.ts` |
| HTTP without authentication | basic auth is always on; `OPENCODE_PASSWORD`, user `opencode` | every request 401 | `[docs]` migrate-v1 guide; `[src]` `packages/server/src/auth.ts` |
| `plugin: ["file:///…/role-enforcement.ts"]` with a v1 hook object | plugin API replaced (`Plugin.define`); a **file** path is skipped with a log line, only directories load | **silent** — the plugin disappears and nothing fails | `[docs]` `build/plugins/migrate-v1`; `[src]` `packages/core/src/config/normalize.ts` |
| tool `bash` | tool `shell`; `bash` is an alias on input | allow-list rules written as `tool: bash` match nothing that is named `shell` | `[src]` `packages/cli/src/acp/tool.ts` `V1Aliases`; changelog v2.2.5 |
| `external_directory` asks by default | allowed by default | a write outside the working directory no longer reaches the broker | `[docs]` migrate-v1 guide |

Two things survive: `OPENCODE_CONFIG_CONTENT` is still read as the inline
config layer (precedence 6 of 8) and a v1 `permission` map is migrated to the
v2 ordered `permissions` list, with `bash` → `shell` `[src]`
`packages/core/src/config.ts`, `normalize.ts` `migratePermissions`.

Re-targeting the HTTP backend at the 2.x API is possible, but the 2.x API is
documented as "the generated API reference" and its permission routes are
annotated experimental `[src]` `packages/protocol/src/groups/permission.ts`.
It is a contract the gateway would be the only consumer of.

### 1.2 Why ACP

ACP (agentclientprotocol.com) is the protocol editors use to drive coding
agents: JSON-RPC 2.0, newline-delimited, over the agent's stdio; the client
spawns the agent. It is governed jointly by Zed and JetBrains, at protocol
version 1, with a v2 draft. opencode ships it as `opencode acp` since 0.15.10
and documents it as a first-class surface; Claude Code and Codex are served by
adapters in the same GitHub organisation as the protocol; Gemini CLI, Copilot,
goose, Kiro and Cursor expose it natively. The protocol's client registry
lists a dozen chat bridges (Slack, Telegram, Discord, Matrix) built as ACP
clients — a chat gateway as an ACP client is an established pattern, not a
novel one.

What ACP gives the gateway, per agent: session creation and resumption, a
prompt turn with streamed updates, tool-call events, a permission request
the client must answer, token usage, cancellation. What it does not give
(§3.8, §3.7): a system prompt (none in stable v1), per-turn usage as a stable
field, any say over *which* tool calls the agent asks about.

### 1.3 Requirements

- **R1** — An opencode 2.x agent reaches the level of integration the 1.x
  backend has: persistent sessions that survive gateway restarts, streamed
  intermediate events, every gated tool call decided by the gateway's
  permission broker (ADR-0002), durable per-watcher instructions, usage in
  the log, structured errors.
- **R2** — The protocol layer is agent-agnostic: nothing in it names
  opencode. Everything agent-specific lives behind one interface, the
  **agent profile** (§3.7), and the document says for each seam whether the
  seam is in ACP or outside it.
- **R3** — A new ACP agent is added by writing a profile and a config entry,
  not by editing the protocol layer or `service.py`'s dispatch beyond one
  registry line.
- **R4** — The gateway is the only permission gate for the new backend as it
  is for the existing two (ADR-0002). Where an agent cannot be made to ask
  about everything the gateway gates, the gap is stated, not hidden.
- **R5** — Existing operators of `type: opencode` on the 1.x line are not
  broken by this increment, and the state of a watcher on the old backend is
  not silently reused by the new one.

### 1.4 The increment

**Add an `acp` agent backend that drives any ACP agent over stdio, with
opencode 2.x as its first profile.** Claude and Codex profiles are
specified (§5) and not built.

---

## 2. Vocabulary

- **ACP agent** — the subprocess the backend spawns and speaks ACP to.
  `opencode acp`, `claude-agent-acp`, `codex-acp`.
- **connection** — one ACP agent process plus the JSON-RPC session over its
  stdio. One per `agents:` entry (§3.2).
- **ACP session** — a conversation inside an agent, identified by the
  agent's `sessionId`. For opencode, Claude and Codex the ACP id *is* the
  agent's own session/thread id `[src]` `packages/cli/src/acp/service.ts`;
  adapter sources per §5.
- **turn** — one `session/prompt` request and the `session/update`
  notifications that arrive before its response. One in flight per session.
- **ask** — a `session/request_permission` request from the agent; the
  client must answer every one.
- **agent profile** — the per-agent object holding everything outside ACP
  (§3.7). Not "adapter": the backend is the adapter; a profile configures it.

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

`type: acp` is new; `type: opencode` continues to mean the HTTP backend for
the 1.x line (R5) and is retired in a later increment, not this one. A
config with `type: acp` and no `profile` is a load error naming the
registered profiles. `command` keeps its meaning (the executable); `profile`
decides the argv around it and everything in §3.7. `working_directory` may
be empty today — "inherit the gateway's cwd" (`gateway/core/config.py:126`)
— but ACP's `cwd` is mandatory and absolute on every session call, so for
`type: acp` an empty value resolves at load time to the gateway's own
canonicalised cwd and the resolved path is what `backend_identity` and
invariant 4 use; it is never passed through empty.

**Backend identity.** `backend_identity(agent_type, working_directory)`
(`gateway/core/state.py:506`) scopes a stored session id to the store that
minted it. With one type serving several agents, `type` alone is not the
store: a Claude session id must not be replayed into a Codex process after a
config edit. The identity for this backend is `acp:<profile>` plus the
canonicalised working directory, so switching an agent from `type: opencode`
to `type: acp` is an identity change and the lifecycle refuses the stored
session with its existing wording (`gateway/core/watcher_lifecycle.py:84-89`)
— the record is re-provisioned, and the AUDIT line names the old identity.
That is the intended migration behaviour, not a defect: a 1.x session row
and a 2.x session row live in different stores (2.x migrates the 1.x
database on first start `[src]` `packages/server/src/routes.ts` `V1Migration`,
but whether a migrated id is loadable by `session/load` is `[unverified]`),
and continuity is carried by `context_inject_files` and history handoff as it
is for any identity change today.

The places that enumerate agent types today — `service.py:100-124`,
`gateway/cli.py:318`, `gateway/configtool/screens/agent_detail.py:122`,
`overview.py:59`, `modals.py:153`, `gateway/tools/tui.py:91`,
`gateway/config.py:908` — each gain `acp`. The TUI and `AgentSession` path
builds the backend without a `GatewayBrokerConfig` and attaches a callable
broker after `start()`, exactly as for the two existing backends.

### 3.2 Process topology

**One ACP agent process per `agents:` entry; sessions multiplexed over it.**
This is the 1.x sidecar topology (`OpenCodeBackend.start()` spawns one
`opencode serve` for every watcher bound to the agent) and the ACP session
model supports it: a session id is on every request, opencode keeps a
session map and a per-session turn fiber so different sessions prompt
concurrently `[src]` `packages/cli/src/acp/sessions.ts`, `turn.ts`
`FiberMap`; the spec states one agent can run several independent sessions
`[docs]`.

The alternative — one process per watcher — would make process-global
mechanisms (opencode's `instructions` config key, an agent/mode with a
`system` prompt) usable for per-watcher content. It is rejected for this
increment for the reason the 1.x adapter gives for not using
`config.instructions` (`gateway/agents/opencode/adapter.py`,
`ensure_durable_instructions` docstring): an `agents:` entry is many-to-one
with watchers, and anything scoped to the process leaks one watcher's
identity header into another's session. It also multiplies memory by the
number of rooms (an `opencode acp` process is two processes: the bridge and
a private server `[src]` `packages/cli/src/services/standalone.ts`). The
idle/expiry lifecycle (`docs/design/dynamic-watcher-design.md` §2.5) would
make per-watcher processes affordable later; the profile interface does not
preclude it.

Whether `claude-agent-acp` and `codex-acp` serve concurrent top-level
sessions in one process is `[unverified]` (their READMEs do not say). The
backend therefore carries a profile flag, `sessions_per_process: many | one`;
with `one`, the connection pool is keyed by session id. opencode is `many`.

### 3.3 The connection

**Spawn.** `profile.argv(command)` + `profile.env(sidecar_env)` +
`cwd=working_directory`. stdout and stdin carry ACP only; stderr is drained
to the log as `_drain_pipe` does today. Shutdown is "close stdin, wait,
terminate, kill" with the same bounded waits as `OpenCodeBackend.stop()`.
Self-heal on a dead process reuses the `_ensure_live_runtime` design
(restart lock, circuit breaker after `_MAX_RESTART_FAILURES`), with one
difference: after a restart every session the gateway holds is unknown to
the new process until re-attached (§3.11).

**Handshake.** `initialize` with `protocolVersion: 1` and these client
capabilities, fixed for every profile:

```json
{"fs": {"readTextFile": false, "writeTextFile": false},
 "terminal": false,
 "auth": {"terminal": false},
 "session": {"compaction": {}}}
```

`fs` and `terminal` off: the agent then must not call `fs/*` or `terminal/*`
`[docs]` and does its own I/O inside its own tools, which is what the
gateway's permission model assumes. opencode never calls them regardless
`[src]` `packages/cli/src/acp/connection.ts`. `auth.terminal` off: a
terminal-type auth method requires relaunching the agent interactively,
which a daemon cannot do; every agent is authenticated out of band (§3.7
"auth"). `session.compaction` on: compaction notifications are the signal
§3.8 rung (c) may use later. The backend does **not** set
`_meta["opencode/child-session-updates"]`: without it, a subagent's asks are
projected onto the parent session `[src]` `packages/cli/src/acp/child.ts`,
which is the session the gateway can route to a room.

The response's `agentCapabilities` is kept for the connection's lifetime;
§3.4 and §3.8 branch on it.

**Client library — decision D1, for the operator.** The backend consumes
eight methods and answers two. Two ways to speak them:

- `agent-client-protocol` from PyPI (the SDK in the protocol's own
  organisation): stable 0.12.1 (2026-08-16), 1.0.0rc3 (2026-10-12), Python
  ≥3.10, depends on `pydantic>=2.7` (not currently a gateway dependency).
  Client side is supported (`spawn_agent_process`, `ClientSideConnection`).
  It absorbs schema drift and the v2 negotiation when that lands.
- A hand-rolled client: newline-delimited JSON-RPC with id multiplexing,
  notification dispatch, and agent→client request dispatch — on the order of
  a hundred lines for the surface used here. It stays auditable and adds no
  dependency, and the gateway owns the handling of the `[unverified]` wire
  details in §8 either way.

The design does not depend on the choice; `connection.py` is the only module
that does.

### 3.4 Backend method map

| `AgentBackend` (`gateway/agents/__init__.py`) | ACP | Notes |
|---|---|---|
| `start()` / `stop()` | spawn + `initialize`; close stdin + terminate | idempotent, as today |
| `create_session(working_directory, extra_args, session_title)` | `session/new {cwd, mcpServers: []}` | returns the agent's id. **No init prompt**: `session/new` returns an id without a model turn, unlike `claude -p`; the 1.x opencode adapter's `_INIT_PROMPT` was inherited from the CLI era and is dropped. `extra_args` and `session_title` have no ACP equivalent and are logged at DEBUG if given; a profile may map `session_title` to an agent-specific call (none does in this increment). |
| re-attach after a restart (§3.11) | `session/resume {sessionId, cwd}` if `sessionCapabilities.resume`, else `session/load {sessionId, cwd, mcpServers: []}` with the replayed updates discarded | `cwd` must equal the session's directory or opencode rejects with `invalid_params` `[src]` `service.ts` `getSession` |
| `delete_session(session_id)` | `session/delete` if `sessionCapabilities.delete`, else return `False` | opencode cancels the turn, removes the row `[src]` `service.ts` `deleteSession` |
| `send()` | `stream()` consumed to its final event | one code path, as the base class already allows |
| `stream()` | `session/prompt {sessionId, prompt: [...]}` + `session/update` fold (§3.5) | |
| `ensure_durable_instructions()` / `reclaim_durable_instructions()` | §3.8 | the file-under-`RUNTIME_DIR/system-prompts/<path_key>.md` contract is kept verbatim |
| `create_gateway_broker()` / `create_callable_broker()` | §3.6 | |
| `supports_per_message_env` | `False` | the process is spawned once; role is the broker's business, as for the 1.x sidecar |
| `typical_session_retention_days()` | `profile.session_retention_days` | opencode: `None` (SQLite rows, no expiry) |

### 3.5 The turn

**Prompt.** The content blocks, in order:

1. durable instructions, if the profile's strategy is a per-turn one (§3.8);
2. one `text` block with the prompt the gateway already builds (trusted
   connector prefix + body);
3. one `resource_link {uri: file://<abs path>, name}` per attachment (§3.9).

**Fold.** `session/update` → `AgentEvent`, mirroring what the 1.x SSE parser
emits (`gateway/agents/opencode/adapter.py` `_parse_sse_events`):

| update | event |
|---|---|
| `agent_message_chunk` | accumulated into the reply text; no event |
| `agent_thought_chunk` | first chunk per `messageId` → `thinking` (`💭 …`), as the 1.x parser emits one per reasoning part |
| `tool_call` | `tool_call` (`🔧 <name or title>`), once per `toolCallId` |
| `tool_call_update` with `status: completed \| failed` | `tool_result` (`✓ <name>`), once per `toolCallId` |
| `usage_update`, `plan`, `available_commands_update`, `config_option_update`, `current_mode_update`, `session_info_update`, `compaction_*`, `notice`, `user_message_chunk` | consumed, not emitted; `compaction_update` is recorded for §3.8 |

**Final.** The `session/prompt` response ends the turn. `stopReason`
`end_turn` and `max_tokens` are a normal reply; `refusal` is a reply with
`is_error=True`; `cancelled` only occurs when the gateway cancelled (below).
Text is the joined `agent_message_chunk`s; empty text is `(empty response)`
with `is_error` set when no tool call was seen either, the rule
`_parse_sse_events` applies. `num_turns` is the number of distinct
`tool_call` ids plus one — a different measure from the 1.x step count
(`step-finish` parts), stated so the log is not read as comparable. `usage`
is read from `profile.usage(response)`:
stable ACP v1 has **no per-turn token field** — the `usage` object on the
prompt response is a Draft RFD that opencode `[src]` `translate.ts`
`response`, claude-agent-acp and codex-acp already emit; the generic default
reads it when present and leaves `usage` `None` otherwise. `cost_usd` likewise
(opencode sends session cost on `usage_update`, cumulative; the profile may
diff it, or leave cost `None`). `duration_ms` is wall-clock from the
backend's own timer.

**Timeout and cancel.** The 1.x adapters raise `asyncio.TimeoutError` and
abandon the turn. Under ACP the turn has to be ended on the wire:

1. send `session/cancel {sessionId}` (a notification);
2. answer every pending ask for that session with `{outcome: "cancelled"}`
   — the spec requires it, and opencode maps it to `reject` `[src]`
   `turn.ts` `queueAsk`;
3. wait, bounded (opencode's own drain is 5 s `[src]` `turn.ts`
   `CancelDrainTimeout`), for the prompt response, which must carry
   `stopReason: cancelled`;
4. then raise `asyncio.TimeoutError` so `AgentTurnRunner._execute_agent`
   (`gateway/core/agent_turn_runner.py`) posts its timeout message as today.

A second `session/prompt` on a session with a turn in flight is an error on
opencode `[src]` `turn.ts` `prompt`; the per-room serial queue already
prevents it on the message path, and the backend holds a per-session lock so
the scheduler and the context injector cannot race a chat turn.

### 3.6 The permission broker

The broker's *shape* changes, its decisions do not. In 1.x the opencode
broker is a separate SSE listener with its own HTTP reply client
(`OpenCodePermissionBroker._listen_sse`, `_reply_to_opencode`). Under ACP an
ask is a request on the same connection the turn runs on; the broker is the
handler the connection invokes, and its return value *is* the reply — the
structure of `ClaudePermissionBroker`, not of the opencode one. Of
`PermissionBroker` (`gateway/core/permission.py`), `request_permission()`
(line 82: id, registry, notify, await with timeout) is reused unchanged.

**Ladder**, per ask, in this order — the ladder both existing brokers
implement:

1. no room for the session → deny (owner + `skip_owner_approval` → allow,
   no room needed);
2. role from `session_role_map`, default `guest` (fail closed);
3. guest → `guest_allowed_tools` or deny, never a human;
4. owner + `skip_owner_approval` → allow;
5. owner + `owner_allowed_tools` match → allow;
6. owner + `human_approval` false → deny;
7. owner → `request_permission()`; timeout or notification failure → deny.

The ladder is currently written out twice (`claude/broker.py`,
`opencode/broker.py`); this backend would be the third copy. The increment
extracts it into one predicate in `gateway/core` that all three brokers
call, in the same spirit as `is_mention()`; the two existing brokers are
re-pointed at it in the same change, with their tests as the proof the
decision table did not move.

**What the ask carries.** `toolCall` is a `ToolCallUpdate`: `toolCallId`,
`title`, `name?`, `kind?`, `rawInput?`, `locations?`, `content?`. The ladder
needs `(tool_name, param_strings)` for `all_params_match_any`
(`gateway/core/tool_match.py`); the mapping from `toolCall` to those is
`profile.normalize_ask(tool_call) -> (tool_name, tool_input)` (§3.7), after
which the existing `get_param_strings_for_claude(tool_name, tool_input,
working_directory)` (`tool_match.py:480`) applies. That extractor keys on
Claude's names and fields: the bash branch fires on `tool_lower == "bash"`
and reads `command`; file tools read the field in `_CLAUDE_PARAM_FIELD`
(`file_path`). A profile's `normalize_ask` therefore returns the tool name
and input **in that vocabulary** — the agent's names and fields mapped onto
Claude's — and the ladder, the built-in rules (`tool: "Bash"`,
`gateway/core/config.py`) and an operator's rules all see one spelling. The
mapping is per profile (§4 P6 for opencode); the tree-sitter split of a
compound command is then the gateway's own, as it already had to be
(fail-closed, #175) — opencode's per-sub-command `patterns` are not on the
ACP wire.

**Reply.** `{outcome: {outcome: "selected", optionId}}` with the option whose
`kind` is `allow_once` or `reject_once`; `profile.select_option(options,
approved)` picks it, because agents differ (Codex offers two `reject_once`
options — "Decline" continues the turn, "Cancel" ends it; §5).

**Invariant: never select `allow_always`.** On opencode, `always` persists
the tool's proposed patterns as project-scoped allow rules in the shared
SQLite store, outliving the process and applying to the operator's own
interactive opencode in that directory `[docs]` v2 permissions; `[src]`
`packages/cli/src/acp/permission.ts` returns `once|always|reject`. On Claude
it writes `updatedPermissions` into settings. The gateway's allow-lists are
the only durable policy (ADR-0002).

**Invariant: an ask is answered within the turn that raised it.** The
registry entry's lifetime is the turn's; `session/cancel` answers all of a
session's pending asks `cancelled` before the prompt response is awaited
(§3.5). On broker `stop()`, pending asks are answered `cancelled` as today's
`reject_on_cancel` does.

**Callable broker** (TUI / `AgentSession` path): the same handler with the
user's `(tool_name, tool_input) -> bool` in place of the ladder; attached
after `start()` via `attach_callable_broker`, which for this backend just
swaps the handler on the connection — no settings file, no second listener.

### 3.7 The agent profile — every seam outside ACP

What R2 asks for: the list of things ACP does not carry, each as one
attribute or method of `AgentProfile`, with the generic default. A profile
overrides what its agent needs. "In ACP" means the generic code handles it
from the agent's advertised capabilities with no profile involvement.

| # | Seam | In ACP? | `AgentProfile` member | Generic default |
|---|---|---|---|---|
| P1 | launch: argv around `command`, env, cwd | no | `argv(command) -> list[str]`; `env(base: dict) -> dict` | `[command]`; `base` unchanged |
| P2 | authentication | partly — `initialize` lists `authMethods`, `session/new` fails `-32000 auth_required` | `auth_hint: str` (what the operator must run, e.g. `opencode auth login`) | the error maps to `AgentPermissionError` carrying the hint; nothing is attempted in-process |
| P3 | **force-ask**: make the agent raise an ask for every call the gateway gates (ADR-0002) | **no** — ACP forwards only what the agent's own policy escalates | `gate_env(base) -> dict` and/or `session_meta() -> dict` (the `_meta` on `session/new`) | none; a profile that leaves this unset gets a load-time warning naming the gap |
| P4 | sessions per process | no (spec allows many; agents vary) | `sessions_per_process: Literal["many","one"]` | `"many"` |
| P5 | durable instructions | **no** in stable v1; Preview RFD `sessionCapabilities.systemPrompt` | `instructions: InstructionsStrategy` (§3.8) | capability-probed ladder in §3.8 |
| P6 | ask normalisation: `toolCall` → `(tool_name, tool_input)` | partly — field names are standard, their content is the agent's | `normalize_ask(tool_call) -> tuple[str, dict]` | `name or kind or "other"`, `rawInput or {}` |
| P7 | reply option choice | partly — `kind` is a UI hint `[docs]` | `select_option(options, approved) -> optionId` | first `allow_once` / first `reject_once` |
| P8 | usage and cost | Draft RFD | `usage(prompt_response, updates) -> TokenUsage \| None`, `cost_usd(...)` | read `response.usage` if present |
| P9 | error classification | partly — JSON-RPC codes `-32000` auth, `-32002` not found, `-32800` cancelled, `-32603` internal | `classify_error(code, message, data) -> AgentExecutionError` | code map; rate-limit detection needs the agent's `data.errorName` / message, so it is a profile concern |
| P10 | session retention | no | `session_retention_days: int \| None` | `None` |
| P11 | attachments | yes — `resource_link` is mandatory for every agent `[docs]` | `attachments: "resource_link" \| "inline_text"` | `resource_link` |
| P12 | reject semantics (informational: what the model sees after a deny) | no | documented per profile, not code | — |

P3 is the seam that decides whether R4 holds for an agent, and it is the
one with no generic answer at all. §4 and §5 fill it per agent.

### 3.8 Durable instructions

The contract (`AgentBackend.ensure_durable_instructions`,
`InjectedContextBuilder.ensure`, `message_processor.py:634`) is: write
`content` to `RUNTIME_DIR/system-prompts/<path_key>.md`, return the path, and
the processor re-supplies it on every turn through
`append_system_prompt_file`. Both existing backends work this way; the
backend keeps it verbatim, including `reclaim_durable_instructions`. What
differs per agent is how the file's content reaches the model on a turn. A
capability-probed ladder, top rung first:

- **(a) `sessionCapabilities.systemPrompt` advertised** → the Preview RFD's
  `systemPrompt: {mode: "append", content: [{type: text, …}]}` on
  `session/new` / `session/load` / `session/resume`. A real system prompt,
  compaction-resistant, outside history. Advertised by claude-agent-acp
  (≥ 0.92, append and override) and codex-acp (≥ 2.3, append only) per the
  RFD's implementation table `[docs]` — `[unverified]` against the adapters'
  own changelogs; **not** advertised by opencode 2.2.5 `[src]` `service.ts`
  `initialize`. Because the content is attached at session attach time, a
  change (a room rename re-issues the header, `message_processor.py`
  `rename`) requires `session/close` + re-attach, which the backend does.
- **(b) profile strategy** — the agent has a lever ACP does not expose
  generically. opencode's is in §4.
- **(c) per-turn prefix** — the file's content as the first `text` block of
  every `session/prompt`. Always available; this is exactly the 1.x
  discipline ("re-supplied on every call") minus one property: it is in the
  conversation history, so it costs tokens per turn and is subject to
  compaction like everything else — which is tolerable precisely because it
  is re-sent every turn. The `session.compaction` capability in the
  handshake exists so a later increment can send it only on the first turn
  and after each `compaction_update: completed`; this increment sends it
  every turn and measures.

The rung used is logged once per session at INFO, because it is a security-
and cost-relevant property an operator should be able to read off.

### 3.9 Attachments

`resource_link {uri: "file://…", name, mimeType?}` per attached file. The
spec makes `resource_link` mandatory for every agent; opencode turns a
`file://` link into a file part on the prompt `[src]`
`packages/cli/src/acp/content.ts` `resourceLinkToPart`. This is a gain over
1.x, where the HTTP backend had no upload and injected paths as text
(`build_attachment_prompt`); that helper stays as the `inline_text` fallback
for a profile that opts out (P11).

### 3.10 Errors

| condition | error |
|---|---|
| spawn failure, handshake timeout, process exit | `AgentUnavailableError` (self-heal eligible) |
| JSON-RPC `-32000` | `AgentPermissionError` with `auth_hint` |
| `-32002` on `session/load`/`resume` | `AgentExecutionError("session not found")` — never recreated silently (requirements §12.3.3) |
| `-32603` with a profile-recognised rate-limit marker | `AgentRateLimitedError` |
| any other JSON-RPC error | `AgentExecutionError`; message sanitised to code + `errorName`, never the raw `data` |
| malformed frame | `AgentProtocolError` |

### 3.11 Restart, idle, expiry

A gateway restart or a self-heal restart spawns a new agent process that
holds none of the sessions the state file names. The backend re-attaches
lazily: the first `stream()` / `ensure_durable_instructions()` for a session
the connection has not attached runs `session/resume` (or `session/load`,
updates discarded) and records the session as attached. A `session/load`
replays the whole history before responding, so on an agent without
`resume` that time counts against the first turn's timeout; the backend
runs the re-attach under its own bounded wait (the start-up timeout, not
the turn timeout) before the prompt is sent. A failed re-attach
surfaces as the structured error in §3.10 — the lifecycle's existing
handling of a dead stored session applies, with the AUDIT line
(`gateway/core/session_release.py`).

Idle (§2.5 of the dynamic-watcher design) releases the processor, not the
session; the session stays attached to the connection until the process
exits, which costs nothing on the gateway side. Expiry calls
`delete_session` (`watcher_lifecycle.py:731`) and
`reclaim_durable_instructions` (`:751`) as today.

---

## 4. The opencode profile

The increment's deliverable, seam by seam.

**P1 launch.** `argv = [command, "acp"]`; cwd = `working_directory`. The
process spawns a private `opencode serve --stdio --port 0` child with a random
`OPENCODE_PASSWORD` and reaches it over HTTP itself `[src]`
`packages/cli/src/services/standalone.ts`, `commands/handlers/acp.ts`. The
child inherits the parent's environment (`extendEnv: true`), so `env` set on
the `opencode acp` process reaches the config loader `[src]`
`standalone.ts`; that the inline config then takes effect in the private
server is **`[unverified]`** (§8 item 1). That private server is not
reachable by the gateway (random port, random password) — there is no HTTP
side channel in this topology, and the design does not want one.

**P2 auth.** `auth_hint = "opencode auth login"`. `session/new` loads the
model catalog first and fails `auth_required` when no provider is usable
`[src]` `service.ts` `newSession`, `error.ts`.

**P3 force-ask.** Inline config through `OPENCODE_CONFIG_CONTENT`, the
mechanism `_build_safe_opencode_config` already implements
(`gateway/agents/opencode/adapter.py`), rewritten in the v2 vocabulary:

```json
{"permissions": [
  {"action": "shell",              "resource": "*", "effect": "ask"},
  {"action": "edit",               "resource": "*", "effect": "ask"},
  {"action": "webfetch",           "resource": "*", "effect": "ask"},
  {"action": "websearch",          "resource": "*", "effect": "ask"},
  {"action": "external_directory", "resource": "*", "effect": "ask"}
]}
```

Ordered, last match wins `[docs]` v2 permissions; a user `OPENCODE_CONFIG_CONTENT`
is merged the way the 1.x function merges it (its own rules follow the
catch-alls, invalid JSON is a load error, a user catch-all for an action is
respected). `external_directory` is new in the list because v2 flipped its
default to allow. The role-enforcement plugin is **not** injected: its guest
path only ever applied to a `COOP_ROLE=guest` process, which the sidecar
never is; its owner path was inert on 1.18.13 (ADR-0002); and a v1 file-path
plugin is skipped by 2.x anyway (§1.1). `gateway/agents/opencode/hooks/` is
left for the 1.x backend; `memory-bootstrap.ts` there is referenced by
nothing but a test string and is noted here as orphaned, not removed.

Asks reach the gateway because `turn.ts` subscribes to the private server's
events for the turn and forwards every `permission.asked` as
`session/request_permission` `[src]`; `allow` rules never ask, `deny` rules
never ask.

**P6 normalisation.** `tool_call.name` when present is the opencode **tool**
name — `shell`, `edit`, `write`, `patch`, `read`, `webfetch`, `websearch`,
`subagent`, … `[src]` `tool.ts` — and `rawInput` is that tool's input:
`{command, cwd}` for `shell`, `{path, …}` for file tools (`filePath` on
sessions migrated from 1.x `[src]` `tool.ts` `filePath()`), `{url}`,
`{query}`, `{pattern, path}`. Two things about the 1.x rules change here,
and the profile absorbs both so an existing allow-list keeps meaning what
it meant:

- *Names.* 1.x rules were written against opencode's **permission** name
  (`docs/permission-reference.md:119`): `tool: bash`, and `tool: edit`
  covering the write, edit and multiedit tools. ACP carries the tool name,
  so `write` and `patch` asks no longer arrive as `edit`. The profile maps
  the ask onto the Claude vocabulary the extractor and the built-in rules
  use — `shell` → `Bash`, `edit`/`write`/`patch` → the corresponding
  `Edit`/`Write`/`Edit`, `read` → `Read`, `webfetch` → `WebFetch`,
  `websearch` → `WebSearch`, `subagent` → `Task` — and rule matching
  accepts the 1.x spellings as aliases: a rule `tool: bash` matches a
  normalised `Bash`; a rule `tool: edit` matches `Edit` **and** `Write`
  (the 1.x meaning of that rule), which is the one alias that is not a
  plain rename and is stated in the migration note.
- *Fields.* `path` / `filePath` → `file_path`; `command`, `url`, `query`,
  `pattern` already match `_CLAUDE_PARAM_FIELD`. Without the path
  rename every file-tool ask would produce the empty param string and fail
  every `params` rule — fail-closed, but the `scratch-dir` preset would
  stop auto-approving.

**The named gap.** For asks that are not a tool call — `external_directory`,
`doom_loop`, a `.env` read — the ACP `toolCall` has **no `name`**: opencode
builds it from the permission `action` only as `title` (the description,
command or parent directory), `kind: other`, and `locations` `[src]`
`permission.ts` `ask` spreads `name: input.tool?.name`. The `action` string
the 1.x broker matched (`tool: external_directory`, used by the
`scratch-dir` preset in `config.example.yaml` and documented in
`docs/permission-reference.md`) is not on the wire. The profile handles an
unnamed ask as: tool name `external_directory` when `kind == "other"` and
`locations` is non-empty, else `other`; param strings from `locations`
paths. This recovers the `scratch-dir` case and leaves `doom_loop` to the
ladder's default (guest: deny; owner: human). The clean fix is upstream — an
`_meta["opencode/permission"] = {action, resources}` on the request, which
the protocol's extensibility rules invite — and is a proposal for the
operator to decide on, not something this increment files. The exact wire
shape of an unnamed ask is `[unverified]` on 2.2.5 (§8 item 2).

**P7 options.** Exactly `once` (`allow_once`), `always` (`allow_always`),
`reject` (`reject_once`) `[src]` `permission.ts`; the default selector is
correct.

**P8 usage.** `response.usage {inputTokens, outputTokens, totalTokens,
thoughtTokens?, cachedReadTokens?, cachedWriteTokens?}` summed over the
turn's steps `[src]` `translate.ts` `response` → `TokenUsage` with
`reasoning_tokens = thoughtTokens`. Cost: `usage_update.cost.amount` is the
session's cumulative cost in USD `[src]` `turn.ts` `sendUsageUpdate`; the
profile keeps the last seen value per session and reports the difference as
`cost_usd`, `None` on the first turn after attach.

**P5 instructions.** Rung (c), with one opencode-specific refinement
available as strategy (b): a `text` block carrying
`annotations: {audience: ["assistant"]}` is split off and delivered as a
*synthetic* steer message before the prompt — visible to the model, hidden
from a UI's transcript, not a system prompt `[src]` `content.ts`
`contentBlockToParts`, `prompt.ts`, `turn.ts` `submit`. The profile uses it
for the durable content so the header does not read as user text. Still in
history, still re-sent per turn. The compaction-resistant mechanism 2.2
added — per-session instruction entries (`session.instructions.entry.put`,
`[src]` `handlers/session.ts`) — is reachable only over the private server's
HTTP and so is out of reach (P1); it is the strongest argument for the
upstream `_meta` route or for opencode adopting the systemPrompt RFD.

**P9 errors.** opencode's error names ride `error.data.errorName`
(`ServerUnavailable`, provider error types) `[src]` `error.ts`
`toRequestError`; `provider.auth` arrives as `-32000`. A rate-limit marker
is `[unverified]` — the profile starts with the 1.x HTTP mapping's spirit
(429-like names → `AgentRateLimitedError`) and the log line for the
unmapped case.

**P12 reject.** A `reject` ends the model's turn with no text, as on 1.x
(`docs/permission-reference.md`): the chat sees an empty reply. Unchanged
behaviour, documented.

**Dropped from the 1.x backend:** the init prompt; `GET /config` plugin
verification (no plugin); the SSE stream parser and the blocking POST path;
the HTTP reply client. **Kept in spirit:** config merge rules, self-heal,
orphan tracking (`session/delete` best-effort on a failed start), the
per-watcher instructions file.

**Migration notes for the operator guide** (written when the increment
ships, not here): `type: opencode` stays on 1.19.x; a 2.x install takes
`type: acp` + `profile: opencode`; sessions are re-provisioned (§3.1); allow-
list rules on `tool: bash` and `tool: edit` keep working through the P6
aliases, `tool: edit` still covering writes as it did; coop-keeper's
`agents/coop-keeper/opencode.json` is v1-shaped (`permission` map,
`default_agent`) and is auto-migrated by 2.x `[src]` `normalize.ts`, but it
ships from this repository and is due a v2 review — out of this increment.

---

## 5. Adding another ACP agent

The steps, then the two examples. Each row in the examples is tagged.

1. Write `gateway/agents/acp/profiles/<name>.py` implementing `AgentProfile`
   (§3.7); every seam not overridden gets the generic default, and P3 left
   unset is a load-time warning.
2. Register it in the profile registry (one line).
3. Document P3 — what the agent asks about and what it never will — in
   `docs/permission-reference.md`, because that is the table an operator
   reads to know whether their allow-list means what they think.
4. Add the profile's wire fixtures to the fake ACP agent (§10) and a test
   per seam that differs from the default.
5. Run the §8 checklist against the live agent.

### 5.1 Claude Code via `@agentclientprotocol/claude-agent-acp`

The `@zed-industries/claude-code-acp` package is deprecated in favour of
this one (npm). v0.100.0 (2026-11-02), built on the Claude Agent SDK;
release-please cadence of several releases a week `[docs]`. No native
`claude --acp` exists `[docs]` CLI reference, changelog.

| seam | Claude | tag |
|---|---|---|
| P1 | `npx @agentclientprotocol/claude-agent-acp` (or the installed `claude-agent-acp`); `CLAUDE_CODE_EXECUTABLE` picks the CLI; `CLAUDE_CONFIG_DIR` honoured | `[docs]` README, `[src]` `src/claude-cli.ts` |
| P2 | the SDK child inherits the environment: `ANTHROPIC_API_KEY`, `apiKeyHelper`, or the `~/.claude` login all work; `auth_hint = "claude /login"` | `[src]` `acp-agent.ts` env spread |
| P3 | **the hard one.** An ask is sent only when the SDK calls `canUseTool`, and the SDK auto-approves before that: deny/ask/allow rules from settings, the permission mode, working-directory reads, Glob/Grep, read-only Bash. Mode alone cannot force asks. Route: `session/new._meta.claudeCode.options = {settingSources: [], settings: <gateway-owned settings>}` where the settings carry a `PreToolUse` command hook (matcher `.*`) that answers `permissionDecision: "ask"` — the same gateway-owned settings idea as today's `settings_adapter.py`, delivered through `_meta` instead of `--settings`. Whether a command hook inside the programmatic `settings` tier fires with `settingSources: []` is **`[unverified]`**; the documented route is a project `.claude/settings.json` with `settingSources: ["project"]`, which touches the operator's project directory. `allowDangerouslySkipPermissions` is not involved: the adapter refuses to auto-allow on the client's behalf | `[docs]` agent-sdk permissions; `[src]` `acp-agent.ts` `canUseTool`, options merge |
| P4 | `[unverified]`; start with `one` | — |
| P5 | rung (a): `systemPrompt` append/override advertised (≥ 0.92 per the RFD table); note the CLI records a conversation's system prompt on first request and applies a changed one at the next compaction | `[docs]` RFD; `[src]` `src/client-system-prompt.ts` |
| P6 | `name` is the Claude tool name (`Bash`, `Edit`, `Read`, …); `rawInput` is the tool input — `get_param_strings_for_claude` applies unchanged | `[src]` |
| P7 | `allow_once`, `allow_always` (writes `updatedPermissions`), `reject_once`; plan-mode asks offer mode switches — select `reject_once` for those | `[src]` `src/permissions/options/*` |
| P8 | `response.usage` + `_meta.quota` (per-model usage, spend) | `[src]` |
| P12 | `reject_once` → deny message, **turn continues**; the model sees the refusal as the tool result | `[src]` `src/permissions/effects.ts` |

Session ids are Claude Code session UUIDs; `session/load` reads the
transcript under `~/.claude/projects/*`, so a session created here is
resumable with `claude --resume` and vice versa `[src]`
`src/resumed-session.ts`. This profile is a *second* Claude path: the
existing `type: claude` (`claude -p` + HTTP PreToolUse hook +
`--dangerously-skip-permissions`) is today the stricter, verified gate and
is not superseded by it.

### 5.2 Codex via `@agentclientprotocol/codex-acp`

v2.5.0 (2026-10-29), TypeScript; a bridge in front of `codex app-server`,
which is OpenAI's own JSON-RPC protocol (`thread/start`, `turn/start`,
`item/*/requestApproval`), not ACP `[src]` `codex-rs/app-server/README.md`.
The archived Rust `zed-industries/codex-acp` is gone. No native `codex acp`
`[docs]`.

| seam | Codex | tag |
|---|---|---|
| P1 | `npx -y @agentclientprotocol/codex-acp`; `CODEX_PATH`, `CODEX_CONFIG` (JSON overlay on the session config), `INITIAL_AGENT_MODE` | `[docs]` README |
| P2 | `CODEX_API_KEY` or `OPENAI_API_KEY`, or the ChatGPT login; `NO_BROWSER=1` | `[docs]` |
| P3 | asks are sent only when app-server emits an approval request, i.e. what Codex's `approval_policy` + sandbox escalate. The four built-in modes pin `on-request` / `never` on every `turn/start`; the only route to `untrusted` ("commands require approval unless an exec-policy rule allows them") is a custom permission profile (`INITIAL_AGENT_MODE=permission-profile:<id>`, policy read from `config/read`), and Codex's own trusted exec-policy list still auto-runs. **Partial gate, `[inferred]`**: document it as such in `permission-reference.md` | `[src]` `src/AgentMode.ts`, `CodexAcpClient.ts` |
| P4 | `[unverified]`; start with `one` | — |
| P5 | rung (a): `systemPrompt` append only (≥ 2.3) → app-server `developerInstructions` | `[src]` `src/ClientSystemPrompt.ts` |
| P6 | command execution and file-change asks carry the command / paths in `rawInput`; map by `kind` (`execute` → `Bash`-shaped, `edit` → path) | `[src]` `src/permissions/CodexApprovalHandler.ts` |
| P7 | `allow_once` (accept), `allow_always`, two `reject_once`: **Decline** (turn continues) and **Cancel** (turn ends). Select Decline for a gateway deny | `[src]` `src/permissions/options.ts`; `codex-rs` `ReviewDecision` |
| P8 | `response.usage` from `thread/tokenUsage/updated`; no USD cost | `[src]` `src/TokenCount.ts` |

Session ids are Codex thread ids; `session/load` is `thread/resume` +
history replay `[src]`.

### 5.3 What the two examples show about the interface

P3 has no generic answer and is partial for Codex; P5 is generic for both
adapters and not for opencode; P7 differs in a way the ladder must know
(Codex's two rejects). Those are the three seams that justify a profile
object rather than a config table. Everything else is a default.

---

## 6. Invariants

1. The gateway never selects `allow_always` (§3.6).
2. Every ask is answered; a cancelled turn answers its asks `cancelled`
   before the prompt response is awaited (§3.5).
3. One prompt in flight per session; the backend serialises per session.
4. `cwd` on `session/new`, `load`, `resume` is the agent's
   `working_directory`, canonicalised the way `backend_identity` does.
5. The instructions file is named by the opaque `path_key` and lives under
   `RUNTIME_DIR/system-prompts/`, as for both existing backends.
6. A stored session id is never recreated silently on `-32002`.
7. The durable-instructions rung in use is logged once per session.

---

## 7. Decisions for the operator

- **D1** — SDK (`agent-client-protocol`, pydantic) or hand-rolled client
  (§3.3). The design is indifferent; the discriminators are a 0.x
  dependency versus owning schema drift.
- **D2** — `type: acp` + `profile:`, with `type: opencode` left as the 1.x
  HTTP backend (§3.1, R5). The alternative — swapping the implementation
  under `type: opencode` — is a silent behaviour change for 1.19.x users and
  is not recommended; the 1.x backend's retirement is a separate, later
  increment.
- **D3** — Per-agent process topology (§3.2), with `sessions_per_process`
  as the escape hatch. Per-watcher processes are a later option that the
  idle lifecycle would make affordable.
- **D4** — Whether to propose upstream to opencode an `_meta` carrying the
  permission `action` on `session/request_permission` (§4, the named gap)
  and/or adoption of the systemPrompt RFD. Both are proposals, not filed
  here.

---

## 8. Verification prerequisites

Read from source, not observed. Each is settled against a live opencode 2.2.5
(installable from the GitHub release; neither npm nor Homebrew carry 2.x)
before the opencode profile is called done:

1. `OPENCODE_CONFIG_CONTENT` on the `opencode acp` process reaches its
   private `--stdio` server, and a `shell` / `edit` / `external_directory`
   call under the §4 ruleset produces `session/request_permission`.
2. The wire shape of an unnamed ask (`external_directory` on a `/tmp`
   write): confirm `name` is absent and what `title`/`locations` carry.
3. `session/resume` of a session created by a previous `opencode acp`
   process, same data directory, same `cwd`, then a prompt turn on it.
4. `always` persists as a project allow rule in the shared store (the reason
   for invariant 1) — confirm, then never send it.
5. A `[live 1.18.34]` rehearsal of the handshake, `session/new`, a prompt
   turn and a permission round-trip is possible today (1.18.34's `opencode
   acp` already advertises `resume`, `fork`, `list`) and should precede
   item 1. Observed so far: `initialize` and `session/new` (Appendix A);
   not yet observed: a prompt turn and a permission round-trip.

For the Claude and Codex profiles, the `[unverified]` rows in §5 are the
checklist.

---

## 9. Delivery

1. `connection.py` + fake ACP agent + unit tests: handshake, request/response
   multiplexing, notifications, agent→client requests, stderr drain,
   shutdown, `$/cancel_request`-free cancel path.
2. `turn.py`: the fold and the final response, against recorded fixtures
   (opencode's `packages/cli/test/acp/wire-fixture.ts` is the shape
   reference).
3. The shared permission predicate extracted from the two existing brokers,
   their tests green; `broker.py` on top of it.
4. `AcpBackend` + `AgentProfile` + the opencode profile; `service.py`
   dispatch; config schema, `coop config`/TUI enumerations, `check_backend_signatures`.
5. The §8 checklist against 2.2.5; `docs/permission-reference.md`,
   `docs/architecture.md` ("Adding a New Agent Backend" → profiles),
   `docs/supported-features.md`, `config.example.yaml`.
6. Then, separately: Claude and Codex profiles when wanted; 1.x backend
   retirement; the per-watcher topology if measurement of rung (c)'s cost
   says so.

---

## 10. Testing

- A **fake ACP agent** as a Python stdio subprocess in `tests/helpers.py`
  (CLAUDE.md: shared by default): scripted responses, scripted asks,
  scripted updates, configurable capabilities. It is what every unit test
  of `connection.py`, `turn.py`, `broker.py` and the backend talks to, and
  it is how a profile's P3/P6/P7 differences are tested without the agent.
- Broker tests reuse `test_claude_broker.py`'s ladder cases verbatim through
  the shared predicate; the ACP-specific cases are: cancelled-on-cancel,
  `allow_always` never selected, unnamed-ask normalisation (§4), the Codex
  two-reject selection.
- Backend tests mirror `test_opencode_adapter.py`'s lifecycle cases
  (start/stop idempotence, self-heal, circuit breaker, orphan cleanup) and
  add re-attach after restart.
- A manual/e2e script under `tests/manual/` runs the §8 checklist and prints
  the frames, so the `[unverified]` items become `[live 2.2.5]` with
  evidence pasted into the PR.

---

## Appendix A — observed frames

`initialize` response, opencode 1.18.34 `[live 1.18.34]`:

```json
{"protocolVersion": 1,
 "agentCapabilities": {
   "loadSession": true,
   "mcpCapabilities": {"http": true, "sse": true},
   "promptCapabilities": {"embeddedContext": true, "image": true},
   "sessionCapabilities": {"close": {}, "fork": {}, "list": {}, "resume": {}}},
 "authMethods": [{"id": "opencode-login", "name": "Login with opencode",
                  "description": "Run `opencode auth login` in the terminal"}],
 "agentInfo": {"name": "OpenCode", "version": "1.18.34"}}
```

At 2.2.5 `[src]` `service.ts` `initialize`: `mcpCapabilities.sse` is
`false`, `sessionCapabilities` adds `additionalDirectories` and `delete`,
and `_meta["opencode/child-session-updates"]: true` is advertised.

`session/new` response, 1.18.34 `[live 1.18.34]`: `{sessionId: "ses_…",
configOptions: [{id: "model", category: "model", type: "select",
currentValue: "<provider>/<model>", options: […]}, …]}`.

`session/request_permission` options, 2.2.5 `[src]` `permission.ts`:

```json
[{"optionId": "once",   "kind": "allow_once",   "name": "Allow once"},
 {"optionId": "always", "kind": "allow_always", "name": "Always allow"},
 {"optionId": "reject", "kind": "reject_once",  "name": "Reject"}]
```

`session/prompt` response, 2.2.5 `[src]` `translate.ts` `response`:
`{stopReason: end_turn | max_tokens | refusal | cancelled, usage?: {…},
_meta: {"opencode/retry"?: …}}`.

## Appendix B — sources

- opencode `v2.2.5`: `packages/cli/src/acp/{agent,service,sessions,turn,permission,prompt,content,tool,translate,replay,config-option,child,error,capabilities}.ts`, `packages/cli/src/commands/handlers/acp.ts`, `packages/cli/src/services/standalone.ts`, `packages/server/src/handlers/{session,permission}.ts`, `packages/schema/src/{permission,session-inbox}.ts`, `packages/core/src/config/normalize.ts`, `CHANGELOG.md`; docs `cli/acp`, `permissions`, `config`, `migrate-v1`.
- ACP: `agentclientprotocol.com/protocol/v1/*`, `schema/v1/schema.json` (artifact `schema-v1.29.1`), RFDs `client-system-prompt` (Preview), `end-turn-token-usage` (Draft), `session-usage` (Completed); registry `cdn.agentclientprotocol.com/registry/v1/latest/registry.json`.
- Adapters: `agentclientprotocol/claude-agent-acp` @ `1d4418f` (v0.100.0), `agentclientprotocol/codex-acp` @ `519c267` (v2.5.0); Claude Agent SDK permissions docs; `openai/codex` `codex-rs/app-server`.
- Python SDK: `agentclientprotocol/python-sdk`, PyPI `agent-client-protocol` 0.12.1 / 1.0.0rc3.
