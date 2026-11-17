# Direction: the gateway wakes remote agents instead of hosting them

Status: direction, discussed on 2026-11-16 and 2026-11-17. The first increment is
specified in `docs/design/remote-agent-gateway.md`. Product strategy
(naming, hosting, positioning) is a separate discussion.

Terminology. CONTEXT.md defines **gateway** as the AgentCoop daemon. This document
keeps that name. "Monitor" below names a *role* in the four-part model (§2), not a
new component: the gateway is the component that plays it.

## 1. Use case

Agents that each bring their own brain (LLM), memory, skills and harness, owned by
different people and not necessarily provisioned by AgentCoop, join a human chat
room as members. People and agents then work together in one open-ended
conversation: a meeting, a brainstorm, social talk, an ad-hoc task. The room decides
what the work is. Fixed task flows are not this use case, and AgentCoop is probably
not the best environment for multi-agent coding.

The messaging platform (Mattermost, Rocket.Chat) is the place. AgentCoop is the
integration layer that brings agents into it and keeps the conversation orderly.

## 2. The four parts

Every system surveyed (`docs/direction/prior-art.md`) decomposes into the same four
parts. They differ only in where each part lives.

| # | Part | Where it lives | Decision |
|---|---|---|---|
| 1 | **Monitor** the platform and decide when to wake which agent | Outside the agent: the gateway and its connectors, one long-running service connected to many agents | Agents run in containers that restart. One classifier call per post covers every agent. The room-level view (roster, cursors) is what makes "who speaks" possible. Every same-use-case product surveyed made this choice. |
| 2 | **Wake** the agent (the doorbell) | One-way, from the gateway to the agent, over whichever transport the agent can receive | The doorbell carries metadata, never content. It expects no response on the same channel. |
| 3 | **Read and send** | The agent fetches the posts itself, through a skill-wrapped CLI or plain REST following a skill document, and posts back the same way. The platform credentials stay in the gateway, which does the platform calls on the bot's behalf | The CLI injects the doorbell metadata into the messages it returns, so the agent sees one format. The CLI also reports what the platform cannot show: "done, nothing to say", and the agent's self-description. In enforce mode the CLI hands the reply to the gateway, which delivers it. |
| 4 | **Instructions** | The agent's own identity and skills, plus a room-rules skill supplied by AgentCoop | The rules: the owner is whoever the server-injected sender field says, answer only when addressed or relevant, one reply per turn, `done` when there is nothing to add, how to treat guests. |

Deployment: each agent runs isolated in its own container or host. The gateway
connects to many agents across hosts. The gateway no longer spawns agents on its own
host as the mainline. A small utility on the agent's host wraps CLI agents (Claude
Code, Codex, OpenCode) and runs one turn per wake.

## 3. Decisions

### 3.1 Permission broker: removed

Tool-level access control is the wrong layer. An intent-level rule ("do not post
blogs", "do not leak customer data") cannot be enforced by gating tools. The web
tool cannot be disabled. The HTTP pattern cannot be predicted. The tool the agent
will use is unknown. In production the human-approval flow was never turned on,
because the stream of approval requests made the agent unusable.

The layers that work:

1. **Credential and resource scope** (hard): what the agent can reach. The owner
   configures this on the agent. Not AgentCoop's.
2. **Instructions** (soft): what the agent should and should not do. Part 4.
3. **Observation and attribution** (after the fact): what the agent did, attributed
   to the agent. The gateway sees every post.

The operator of one hosted personal agent reported the same split for that
product: system prompt first, hard control only at specific connectors such as
payment, never at the tool level.

AgentCoop's contract: **it guarantees identity and role, not execution.** The role
label on every delivered message is server-injected and cannot be forged by post
content. The only hard control AgentCoop legitimately owns is on its own connector,
the chat platform: posting (rate, closed threads, mute), applied in enforce mode.

Roles are labels, not permissions. There are three: `owner`, `guest` and `agent`.
A sender the configuration does not list is a `guest`. The `anonymous` role of the
current gateway, which drops an unlisted sender before delivery, goes away: the
gateway delivers and labels, and the agent decides. Finer roles are later work, for
example a delegate of an owner, or an agent's own owner as distinct from a room's
owner.

Consequence: agents run in their harness's permission-bypass mode by default
(`--dangerously-skip-permissions`, `--auto`, `--dangerously-bypass-approvals-and-sandbox`).
The hard ceiling is the harness's own deny configuration, set by the owner.

Later, not first version: the gateway's classifier can pre-scan conversation against
global rules ("payment not allowed") and tag a warning onto the delivered message, or
block delivery. The tag rides the doorbell metadata. Default to warn, not block.

### 3.2 Scheduler: removed

Each agent uses its own scheduler. Hermes, OpenClaw and Muse have one. Claude Code
agents use OS cron with the CLI, or Claude Code scheduled tasks. Jobs that live on
`coop schedule` today move to the agent that owns them before cutover.

### 3.3 Agent chain: replaced by rate limit and cooldown

The agent-chain budget drops messages, which splits the room's context between
agents (issue #190). The rate limit and cooldown of
`docs/design/multi-agent-enhancement.md` §5.3 hold a turn instead of dropping a
message, and the held post stays in the backlog for a later turn. The
`<end-of-agent-chain>` token survives as the `done` signal.

### 3.4 Heartbeat: exactly one

Periodic catch-up exists because hosted coding agents have no heartbeat. Registration
records who owns the heartbeat for each agent, agent-side or gateway-side, and
exactly one is active. The gateway-side heartbeat is a catch-up scan of posts the
agent has not seen, not a plain timer (spec §5.7).

### 3.5 Self-description: a skill, pushed by the agent

The agent pushes a short self-description to the gateway, or writes it into the
platform's own profile field. The gateway reads it when classifying and logs the
version it used. Nothing is asked over the doorbell. Sources, in order: an A2A Agent
Card if the agent publishes one, the platform's description field (Mattermost bot
`description`, Rocket.Chat `bio`, Matrix custom profile field), a one-time
self-introduction through the CLI. Descriptions stay short: identity,
responsibilities, current topics, reply rules. No memory, no credentials. Evidence
from a live room: relevance scores on an implicit question went from 0.38 without
the description to 0.97 with it.

### 3.6 Classifier and the other gateway mechanics: kept, in shadow first

The classifier (one call per post, one question per agent, fail-open on error),
periodic catch-up, cursor, mute, orchestrator, rate and cooldown, the enforce
switch: spec §5 lands in the gateway. Run in shadow mode on a live room and compare
scores with what agents actually did before setting thresholds.

### 3.7 ACP backend: second phase

`docs/design/acp-agent-backend.md` designed an in-gateway ACP client with the
permission broker as its centrepiece (R4, §3.6, P3). With the broker gone and agents
in bypass mode, a one-shot command per wake covers Claude Code, Codex and OpenCode
without ACP. ACP returns in the second phase, when streaming or forwarding asks to an owner is wanted.

### 3.8 Voice gateway: removed

The voice gateway was experimental. It runs `claude -p` per request and answers
synchronously, so it depends on the hosted backends this direction removes. It is
out of the pivot. If a use case appears it comes back as a
separate layer above the gateway, with its own speech handling and a synchronous
reply path, not as a connector inside the gateway.

## 4. What is removed, kept, moved

Dispositions of every feature in `docs/supported-features.md` as of 2026-11-17.

| Feature | Disposition |
|---|---|
| Rocket.Chat and Mattermost connectors: routing, reconnect, watermark dedup, multi-connector, auth modes | Kept. They are the monitor's eyes. |
| Message triggering: DM, `@mention`, room-wide mentions | Kept as the `addressed` turn source. The classifier adds `picked`. |
| Attachments: inbound download, size limits, metadata in prompt | Changed to lazy fetch. `coop read` lists attachments with ids; the agent fetches one through the gateway only when it needs it. The size limit applies at that fetch. |
| Typing and status indicators | Kept. The gateway sends typing on wake. |
| Voice gateway (experimental) | Removed (§3.8). If revived, it is a layer above the gateway, not a connector. |
| Claude CLI backend, OpenCode backend, backend behaviour | Moved into the agent-side utility as one-shot command adapters. Not a gateway component. |
| Session persistence (`state.json`), session ids across restarts, reset | Session ids move to the utility. The gateway persists the cursor and the agent roster. `reset` stays as a gateway command that tells the utility to start a new session. |
| Per-room serial queue, queue depth limits | Replaced by one outstanding wake per agent per room (spec §5.5 as a lease). |
| Watcher pause and resume | Kept. |
| Dynamic watchers: `rooms:` include and except patterns, DM opt-in, room discovery from the stream, idle and expiry reclaim | Kept. It is gateway-side. The agent configures nothing per room: a wake names the room, and the gateway creates the cursor when it discovers the room. |
| `AgentSession`, `ScriptConnector`, `pipe_to()` | Retired with the hosted-agent model. |
| RBAC roles, role from trusted connector context | Kept as labels: `owner`, `guest`, `agent` (§3.1). An unlisted sender is a `guest`. The role rides every delivered message and nothing is enforced by it. |
| Tool allow-lists, parameter matching, tree-sitter command parsing, guest tool rejection | Removed (§3.1). |
| Human-in-the-loop approval: approval ids, timeouts, queue pause, hook and SSE transports | Removed (§3.1). |
| Context injection: connector, agent and watcher context files | Moved. Identity is the agent's own. Room rules are a skill the gateway publishes. |
| Lazy instruction loading (`coop instructions`) | Kept for the rules skill. |
| CLI: `start`, `stop`, `restart`, `status`, `config reload`, `config show` | Kept. |
| CLI: `list`, `pause`, `resume`, `reset` | Kept. |
| CLI: `send` (text, file, stdin) | Kept. It is the agent's send path. |
| CLI: `fetch-history` | Kept. It is the agent's read path, extended to inject wake metadata. |
| CLI: `schedule create/list/delete/pause/resume` | Removed (§3.2). |
| coop-keeper, `coop config add/remove/patch` | Kept. The agent entry changes to a remote agent. |
| Configuration: YAML, templates, `tool_presets`, `rooms:` expansion, schema, validation | Kept, minus the permission fields. |
| Agent chain: token, per-sender budgets, TTL | Replaced (§3.3). The agent list configuration survives: the classifier and the rate limit need it. |

## 5. The doorbell

No surveyed vendor's doorbell returns the agent's answer in the HTTP response.
Answers always travel through a channel the agent writes to. The doorbell is one-way
by design and the CLI is the return path.

| Transport | Reaches | Agent host must run | Status |
|---|---|---|---|
| Outbound long-poll or WebSocket from the agent host to the gateway | Any agent that can run a process: hosted personal-agent VMs (proven on one), CLI agents through the utility, open agents with their own gateway | A small always-on client with auto-restart. The agent itself can be a one-shot turn. | First version |
| MCP Events (SEP-3415, webhook mode) | ChatGPT Work chats and dots, OpenClaw, OpenCode (experimental) | Nothing. The gateway must be a public HTTPS endpoint. | Later. The SEP is accepted but not final. |
| A2A `SendMessage` | OpenClaw, Hermes | An HTTP server that is up | Later. Wake and ask in one call, with the Agent Card as the description. |
| Vendor trigger | Grok Bot (webhook with Bearer, email, Slack mention) | Nothing | Later |
| The agent's existing channel (Slack DM, email) | dots, Duet | Nothing. The gateway acts as the owner on that channel. | Later |
| Polling store (ntfy-style topic with `poll=1&since=`) | cron or heartbeat agents | A periodic `curl` | Later |

Wake payload: the server-authenticated author id, the author's role, a `relayed`
flag when the instruction in a post belongs to someone else (a guest quoting the
owner), the thread root, the turn source and the hint of spec §5.8.

Busy and dead: the agent acknowledges a wake with `done`. The gateway holds a lease
per wake with a timeout, tells busy from dead, and coalesces wakes into one turn per
batch.

## 6. Agent experience: kits, not a menu

Ship one adoption kit per agent family. Each kit pre-chooses the transport, the
heartbeat owner, the description source and the rules text:

- Claude Code, Codex, OpenCode: the utility with a one-shot command adapter,
  gateway-side catch-up, OS cron for schedules. First version.
- Open agents with their own channel plugins (Hermes, OpenClaw): the gateway
  subscription for relevance tags and metadata, A2A card as the description,
  agent-side heartbeat.
- Hosted personal agents (Muse, dots, Grok Bot): per-vendor transport from §5.

Each kit is a skill document plus a configuration. The CLI is the reference
implementation. The floor is the skill document plus REST.

## 7. Stated assumptions

- Room members are trusted and the LLM honours the role label. Measured for chat
  rules in live runs (PR #202), not for tool use. A hostile guest can attempt prompt
  injection. Mitigations exist (`relayed`, the pre-scan, enforce mode for posting)
  and are later work.
- A hosted personal-agent VM allows a user-started long-running process. Proven on
  one vendor. Not stated by the others.
- MCP Events will finalise close to the current draft.

## 8. Supersedes

This direction overrides, without editing them yet:

- ADR-0002 (the gateway as the only permission gate).
- `docs/design/acp-agent-backend.md` R4, §3.6 and P3. The transport and turn parts
  stay valid for a later ACP transport.
- `docs/design/multi-agent-enhancement.md` §2.2 ("a bot cannot bypass them": true
  only in enforce mode), §5.11 (`FOLLOW_UP`, the scheduler is gone), §8.2 (the
  implementation sequence), §10 (remote agents are the mainline, not an extension).
- `docs/agent-chain.md`.
- `docs/supported-features.md`, the voice gateway section (removed, §3.8).
- The permission entries of CONTEXT.md.

## 9. Evidence

- Five products share the use case (Ando, Agent Room, Aurmada Rooms, Conduyt,
  Buzz). All keep the monitor outside the agent. None has a per-agent tool
  permission gate. None has a who-speaks mechanism beyond caps and a pause line,
  except Ando's participation judge for its own agents. Details and quotes:
  `docs/direction/prior-art.md`.
- A Muse-based agent runs a long-lived WebSocket client on its vendor VM with
  owner-built auto-restart, uses relevance tagging bound to its description, and
  coalesces wakes behind a lock with a lease. Its owner's draft (2026-11-15)
  contributed the `relayed` flag, the exactly-one heartbeat rule, the ack and lease,
  and logging the description version.
- The simulator (`tools/roomsim/`) implements the §5 mechanics from the
  specification alone. The gateway's core starts from it.
