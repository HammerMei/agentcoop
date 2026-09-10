# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.0.0]

The first release under the new name. Everything below the *Renamed* section
is the dynamic-watcher cutover that had accumulated on `main` since v0.5.2.
Both changes are clean breaks: upgrading from any 0.x is a reinstall — see
`docs/migration-v1.md`.

### Renamed

- **agent-chat-gateway is now AgentCoop.** The project outgrew "a gateway from
  chat to an agent" and became a middleware where several agents collaborate
  with humans and with each other; the name follows. Short form: Coop.
  - The command is **`coop`** (`coop start`, `coop list`, …); the provisioning
    CLI is **`coop-provision`**. There is no alias. Running the old
    `agent-chat-gateway` command after an upgrade prints what happened and how
    to either reinstall or stay on v0.5.2 — it does nothing else.
  - The runtime directory is **`~/.agentcoop`**. `~/.agent-chat-gateway` is
    not read, moved or mentioned by v1; remove it by hand
    (`docs/migration-v1.md`).
  - Environment variables are **`COOP_ROLE`, `COOP_ALLOWED_TOOLS`,
    `COOP_APPROVAL_TOOLS`, `COOP_CONFIG`, `COOP_ADMIN_CONFIG`**. The `ACG_*`
    names are gone; hooks and scripts that read them need updating.
  - The agent-facing session header is `## Coop Session Identity`.
  - Logger names are `coop.*`; the Docker image is `ghcr.io/hammermei/agentcoop`
    (the old `agent-chat-gateway` image stays frozen at v0.5.2 and is not
    deleted); the repository is `HammerMei/agentcoop` (GitHub redirects the old
    URLs).
  - The Python package is still `gateway`, and "the gateway" is still the name
    of the daemon process — `docs/architecture.md` defines the two names.
- **Docker is for internal testing only.** The compose example and image are
  kept for AgentCoop's own E2E/CI runs and are no longer offered as an install
  option; the supported install is `install.sh` on the host.

### Removed

- **PyPI publishing.** AgentCoop is installed from git by `install.sh`; the
  PyPI package `agent-chat-gateway` stops at 0.5.2 and nothing is published
  under the new name.
- **The Homebrew branch of `upgrade`**, which no install could reach (there has
  never been a tap).

- **BREAKING: `online_notification` / `offline_notification` are removed**
  from watcher rules and templates (decided 2026-08-02 with the dynamic-watcher
  design; executed with the runtime cutover). A config still carrying them
  fails at load with an unknown-key error. IM platforms carry their own
  presence signal, and under the idle/expire lifecycle a watcher starts and
  stops many times over its life — announcing each transition was noise. The
  connector `notify_online`/`notify_offline` API is removed with it.

### Changed — the watcher model itself
- **BREAKING: `watchers:` entries are rules, and watchers are created per room
  at runtime** (`docs/design/dynamic-watcher-design.md`). A rule declares which
  rooms an agent serves — `rooms:` is a mapping with `include:` glob patterns,
  `except_for:`, and the DM opt-ins `direct:` / `group_direct:` — and the
  gateway creates each room's watcher on the room's first message (Rocket.Chat,
  Mattermost) or eagerly at startup for connectors with no inbound stream
  (voice, script, whose rules must name literal rooms). Rules match top-down;
  the first rule that claims a room wins, and `coop config validate` warns about
  rules an earlier rule shadows. Each created watcher is named
  `<connector>:<room>`, which is what `list` shows and the operator verbs act
  on. **The static shape — a `room:` key, or `rooms:` as a list — is a hard
  load error** naming the migration guide; see
  `docs/migration-dynamic-watchers.md`, and note the upgrade **resets every
  existing watcher session**: static-era state records are pruned at the first
  post-rewrite start (logged per record), and each room begins a fresh session
  on its first message.
- **Watchers now idle and expire** (design §2.5). A room quiet for
  `session_idle_days` (default 15) has its runtime dropped — the session is
  kept, the connector's room state survives, and the room's next message wakes
  it into the *same* session. A further `session_expire_days` (default 15)
  after the drop, everything is reclaimed: record, backend session, prompt
  file, attachment workspace. The TTLs are per rule, both default to 15, and
  there is **no ordering constraint between them** — they are sequential legs
  measured from different origins (last activity; the drop). An hourly sweep
  runs both legs, advancing a watcher at most one state per pass, so an outage
  of any length cannot cascade `active → expired`; **pause exempts from both**,
  and a pending scheduled job exempts a room from neither — each job records the
  room it targets, so its next run recreates the watcher a sweep reclaimed.
  Boot runs the same evaluation over was-active records, so a fleet reads
  honestly after a restart instead of `failed`.
- **A scheduled job records the room it targets**, so it survives a room rename
  and an `expire`: `jobs.json` gains `room_id` and moves to schema version 2. A
  fire resolves through that id, not through the watcher's name — a name is a
  pure function of `(connector, room)` and moves when the room does. Jobs written
  before this keep working by resolving their name; **run
  `coop schedule migrate` to record their room ids**, before
  renaming any rooms, since the migration finds each room through its job's
  watcher name. The daemon warns at startup while anything is unmigrated. The
  command is version-aware and safe to re-run, and it never guesses: a job whose
  room cannot be identified is reported and left alone.
- **Operator verbs act on records, and there is a new one**:
  `coop expire <watcher>` clears a room's session and reclaims its record and
  files now (it overrides pause, audibly — the audit line names the room). It
  does NOT cancel the room's scheduled jobs: expire does not stop a rule
  watching a room, so the job records the room's id and brings the watcher back
  on its next run. Being removed from a room does cancel them, because there
  the room can no longer answer. `pause` on a room the gateway has never seen is rejected
  with a pointer at `rooms.except_for` (the old path fabricated a blank record
  that then *overwrote the real one on disk* — #118). `reset` refuses a paused
  watcher instead of silently clearing the pause; `resume` restarts the idle
  clock at the moment of resume, so a long-paused watcher is not re-idled by
  the next sweep pass.
- **`list` reports state records rather than config entries** (design §2.8),
  with a state filter: `--active`, `--idle`, `--paused`, `--failed`, `--all`,
  defaulting to everything except idle. Rows carry room id and participants; a
  watcher that is supposed to be running and is not reads **`failed`** —
  derived from record-vs-residency, never stored, so the next successful start
  clears it. A failed record past its idle TTL converts to idle at the next
  boot rather than being retried forever.

### Added
- **Membership events** (design §2.7): both discovering connectors register a
  room the moment the bot is added to it — as an idle record, nothing started,
  so a bot invited to fifty rooms is listable immediately at zero session
  cost — and reclaim the room's record, session and files the moment the bot
  is removed, overriding pause (the platform saying the room is gone outranks
  an inactivity guard) and cancelling the room's scheduled jobs. Rocket.Chat
  listens on `subscriptions-changed`, Mattermost on `user_added`/
  `user_removed`. A **periodic membership reconciliation** (daily) backstops a
  missed removal event for paused and idle records, which nothing else ever
  touches; an unanswerable membership probe keeps everything (fail = keep).
- **`docs/migration-dynamic-watchers.md`** — the rewrite procedure, the field
  notes (a DM entry cannot be named; `direct: true` replaces `room: "@user"`),
  and the accepted losses, stated as such.
- **Two connectors may no longer run as one bot account.** Each connector
  reports the identity its platform assigned it, and startup refuses before
  any connector subscribes if two of them are the same account on the same
  server. Two exceptions, per design §4.5: Mattermost connectors scoped to
  **different teams** may share an account, and no two of them may claim
  **overlapping** direct messages — a rule opting in with `direct:`/
  `group_direct:` claims that whole DM class, so two connectors both opting in
  conflict. A connector that cannot establish its own identity stops startup.
  Mattermost opens its socket while connecting but starts reading only after
  watchers are restored, so startup messages are buffered rather than
  discarded.
- **A persisted session is only resumed against the backend that issued it.**
  Watcher state records the resolved `backend_identity` (agent backend type
  plus the canonicalized working directory) alongside the session id, and the
  two are compared before the id is reused. Changing an agent's `type` or
  `working_directory` starts a fresh session, logged with the before/after
  identity, instead of replaying the stored id into a different session store.
- `AgentBackend.typical_session_retention_days() -> int | None` — a backend
  declares its session-retention limit (`ClaudeBackend`: 30; `OpenCodeBackend`:
  none).

### Changed
- **Per-room files are named by a digest instead of the watcher name**
  (design §2.3): the attachment workspace and the system-prompt file both key
  on `(connector, room id)`. A rename orphans neither, and two rooms can no
  longer collide onto one path. *On upgrade:* one prompt file and one symlink per existing room are
  orphaned once; they are internal artifacts and harmless to delete — expiry
  reclaims the new ones automatically.
- **A room with no watcher is no longer reported as backpressure.** The
  capacity preflight reports `AVAILABLE` / `FULL` / `UNROUTED`, and only
  `FULL` produces the "server busy" notice.
- **One room is served by one watcher, and one session belongs to one room**
  (design §4.1). The dispatch index is a single slot — a second claim at
  runtime raises — and `bind_session` fails closed instead of silently
  re-pointing a session's identity header and permission routing at a second
  room. Under rules the collision cannot arise from config: first-match
  precedence decides, and validate warns about the shadowed rule. Two agents
  in one room remains supported the intended way — one bot account each.

### Removed
- **BREAKING: the static watcher shape** (`room:`, `rooms:` as a list, the
  auto-derived `<connector>-<room>` naming of static entries, `exclude_room`,
  and the `room: "*"` placeholder rejection). All replaced by rules, above.
- **BREAKING: `watchers[].session_id` (sticky sessions).** Setting it is a
  config error naming the replacement rather than being silently ignored. This
  was documented in `v0.5.1`, so a config carrying it fails at startup after
  upgrading. *Why gone rather than moved:* a pinned id names a session the
  backend is free to expire, and with a watcher created per discovered room,
  one id in config cannot say which room it belongs to. *Migration:* delete
  the field; to carry context into a session, have the agent summarise it to a
  file and list that file in `context_inject_files` (user guide, Use Case 3).
  Runtime session continuity across restarts is unaffected.

### Added

- **A typing indicator while a watcher is being created.** The first message in
  a room used to be followed by the longest silence a sender ever sees — session
  provisioning and the history handoff — with no sign anything was listening.
  Once a rule has claimed the room, the connector shows the bot typing before
  the start; a failed creation switches it off again. Rooms no rule claims get
  no hint.

### Fixed
- **`send` without `--connector` works on a multi-connector gateway for any
  room the gateway serves** (#136). The connector is found from the room —
  by id, name or watcher handle — through the lifecycle records, in memory;
  `--connector` is required only for a room no watcher has, or one served by
  more than one connector. Agents no longer have to recover from the refusal
  by reading their identity header.
- **The durable-instructions file is keyed by the room, and a rename reaches
  the running watcher.** `watcher_prompt_key` no longer includes the handle
  (a static-era deviation from §2.3), so a rename keeps the file the session
  was started with; the resident processor takes the new handle and rewrites
  the "ACG Session Identity" header the agent reads every turn, and connector
  subscriptions are identified by room id so pause/expire after a rename
  release them. Files written by earlier dynamic-watcher builds under the old
  handle-keyed name are not migrated (one orphan per watcher, under
  `system-prompts/`; delete them by hand).
- **A watcher's name follows its room's rename.** The handle
  `<connector>:<room>` was derived at creation and kept: after a channel rename,
  `list` showed the old name until the watcher happened to be recreated, and the
  old handle could be typed against the wrong watcher once the platform reused
  it. It is now re-derived from the room's current name on the first message
  after a rename (Mattermost `channel_name`, Rocket.Chat `roomName` — the URL
  name, not the display name), with an `AUDIT` log line; `schedule list` shows
  each job's watcher as it is named now.
  A new room that takes a renamed-away room's URL name moves the old
  handle out of the way at creation, after asking the platform what the
  holder is called now.
  DM labels still carry the counterpart's name as of creation.
- **A job the gateway cancels is kept, not deleted.** Cancellation (the bot
  removed from the room; the job's connector gone from the config) now marks
  the job `cancelled` with `cancelled_at` and `cancel_reason` instead of
  removing it, so an accidental cancellation is visible in `schedule list
  --all` and in `jobs.json`, and `schedule resume` restores it. Cancelled jobs
  age out after `completed_job_ttl_days` like completed ones; only `schedule
  delete` removes a record.
- **`expire` is refused on connectors without unsolicited inbound** (voice,
  script). Its contract is "reclaimed now, recreated by the room's next
  message", and those connectors have no next message: an expired watcher
  stayed down, silently, until a restart. The error names `reset` as the verb
  that has the effect expire can honestly have there.
- **A scheduled job whose connector left the config is cancelled, not
  re-homed.** The fire used to hand such a job to whichever remaining connector
  held its room — under one account per agent, a different agent. It now
  cancels the job at its next slot with an audit line, the same way a room
  removal does (marked `cancelled` and kept — see the entry above).
- **Mattermost replay revalidates membership first.** A removal while the
  WebSocket was down produced no `user_removed` event, and the bot's token can
  still read a public channel it has left, so a reconnect replay delivered a
  kicked channel's backlog to the old watcher. The replay now checks membership
  by channel id before dispatching anything, and a confirmed removal is
  reclaimed through the same hook a live `user_removed` runs; a lookup failure is treated as
  unknown and the replay proceeds, so a network blip cannot be mistaken for a
  removal.
- **OpenCode watchers reclaim their durable instructions on expiry.** The
  adapter had no `reclaim_durable_instructions`, so every expired OpenCode
  watcher left its `system-prompts/<key>.md` behind. The file is now removed,
  as `ClaudeBackend` already did.
- **`coop schedule migrate` no longer hides work done at an unchanged version.**
  A version-2 jobs file with a live job lacking a room id re-runs the 1→2 step;
  the CLI keyed "nothing to do" on the versions matching and hid the steps,
  outcomes and jobs needing attention. It now says "nothing to do" only when
  the run recorded nothing.
- **`config validate --lint` on a non-list `watcher_rules:`** raised a
  TypeError in place of the collected structural error.
- **The config TUI's delete-rule warning counts jobs by room, not only by
  handle**, so a job whose watcher was renamed is still counted.
- **A restart no longer re-delivers messages the agent already answered** in
  rooms whose watcher was created since the previous start. The creation path
  claimed a replay boundary below the frames it handed back — a promise to
  replay them if the watermark had already moved past them — and nothing
  discharged that claim when the frames were simply accepted, which is the
  ordinary outcome. Shutdown then persisted a boundary at the room's creation,
  and the next start replayed everything since it, on both connectors. The
  frames are now *promised* rather than claimed: the boundary is claimed only
  at the moment the filter actually rejects one as already processed.
- **The bot's Rocket.Chat identity is the account the server knows, not the
  configured spelling** (#112). Login is not spelling-exact — a lowercase or
  email login resolves to the account's canonical username, which is what
  every message frame carries — so the mention gate, the history handoff's
  own-turn labels, the own-message fallback, the `to:` field and the typing
  payload all compared against the wrong string and failed silently. They now
  use the canonical spelling captured at login.
- **A delivery in flight across a watcher restart no longer commits to a
  detached state object** (#115), on either connector: the watermark, dedup id
  and hand-back boundary follow the *room*, so an accepted message is not
  re-delivered by the next replay and a handed-back one stays recoverable.
  Also from #115: Mattermost's `unregister_channel` releases the channel's
  queue and worker instead of leaking them; the Rocket.Chat routing path
  records a `roomParticipant: False` removal for a tracked room instead of
  dropping the news; the sender allow-list has one implementation again; and a
  stale return annotation is corrected.
- **`pause`/`resume`/`reset` no longer corrupt a record that exists on disk
  but was not loaded this run** (#118): the blank-record fabrication is gone
  with the path that produced it, a raising watermark read no longer aborts a
  teardown's unsubscribe and drain, and the stale-`room_id` cursor hazard is
  unreachable under records bound to `(connector, room_id)`.
- **Permission brokers are enforced on every start path.** The fail-closed
  guard for an unavailable agent lived only in `resume`/`reset`, so a watcher
  created on the message path (or eagerly at boot) could start without its
  permission broker and process messages with no tool-call enforcement,
  silently.
## [0.5.2] - 2026-08-02

The last stable release of the v0 line, and the version production runs.
Two fixes over 0.5.1; no new features, no breaking changes. Roll back here if
the dynamic-watcher release misbehaves.

### Fixed
- **File attachment uploads restored on Rocket.Chat 8.0+.** RC 8.0 removed the
  one-step `rooms.upload/{rid}` endpoint (closes #56); the client now detects
  the server's major version and dispatches to the two-step
  `rooms.media` + `rooms.mediaConfirm` flow, falling back for pre-8.0 servers.
- **OpenCode agents no longer lose their identity/addressing header after
  context compaction.** Durable content is written per watcher and forwarded
  fresh on every send via OpenCode's per-request `system` field.

## [0.5.1] - 2026-07-30

### Fixed
- **`history_handoff` now actually defaults to enabled.** Commit `31f966d`
  (2026-05-10) intended to make `history_handoff` opt-out instead of opt-in,
  flipping `HistoryHandoffConfig`'s dataclass default to `enabled=True` — but
  missed the watcher config loader (`gateway/config.py`), which kept its own
  hardcoded `enabled=False` fallback whenever a watcher's config omitted the
  `history_handoff:` block entirely. The loader now falls back to
  `HistoryHandoffConfig`'s own field defaults instead of separately hardcoded
  literals, so the two can't drift apart again; the config TUI's watcher
  template preview (`gateway/configtool/screens/watcher_detail.py`) does the
  same. No config changes are needed to pick this up — a watcher with no
  `history_handoff:` block now gets history handoff on by default; add
  `history_handoff: {enabled: false}` to opt back out.

## [0.5.0] - 2026-07-30

### Added
- **Config TUI (`agent-chat-gateway config`)** — a new full-screen,
  keyboard-driven editor for `config.yaml`, replacing hand-editing YAML for
  day-to-day changes. Full create/edit/delete for Connectors, Agents,
  Watchers, Templates, and Tool Presets, with per-field provenance labels
  (explicit / inherited from a template / built-in default), required-field
  markers, blast-radius confirms before a template edit affects other
  entries, and validate-before-write saves (every save is checked against a
  temp file first, with an automatic timestamped backup) — a bad edit never
  reaches your real config.yaml. `ctrl+e` opens `$EDITOR` on the raw file
  for anything the forms don't cover. See `docs/config-tool.md`.
- **Config TUI: full watcher CRUD**, including the `rooms:` group
  merge/split rules: creating or editing a room whose connector, agent, and
  other settings match an existing entry merges it into that entry's
  `rooms:` list instead of creating a near-duplicate; editing a per-room
  setting (room, name, session ID, notifications, connector, agent,
  inherits, ...) on one room in a group splits it out into its own entry
  without touching its siblings — only `description` (a free-text
  annotation) edits the whole group in place. A "Clone for rooms" action
  bulk-adds several rooms sharing a watcher's settings in one step,
  reachable both from inside a watcher and directly from the Watchers list.
- **Config TUI: Tool Presets tab** — create, edit, and delete named
  `tool_presets:` entries (add/edit/remove individual rules, each either an
  inline rule or a reference to another preset by name) directly in the
  TUI; deleting a preset still referenced by an agent is blocked with a
  clear reason.
- **Fault-tolerant config validation with per-entity save gate and status**
  (#73). `GatewayConfig.from_file()`'s per-entity parsing is now shared with
  a new `collect_config()` that keeps going past independent per-entity
  failures, rather than failing the whole file on the first bad entry. This
  lets the config TUI save or delete an entity as long as the edit doesn't
  introduce a *new* problem, instead of being blocked by an unrelated,
  pre-existing error elsewhere in the file, and shows real per-row ERROR
  status for agents and watchers (not just connectors), including when
  multiple entities are independently broken.
- **Compact config.yaml format (v0.2)**: top-level `connector_defaults:` /
  `agent_defaults:` / `watcher_defaults:` blocks deep-merge into every entry
  of the matching kind; named `tool_presets:` can be referenced by name from
  any agent's `owner_allowed_tools`/`guest_allowed_tools`; and watcher
  `rooms: [a, b, ...]` binds one connector+agent pair to many rooms at once
  (auto-deriving each expanded watcher's name as `<connector>-<room>`). None
  of this is required — existing config.yaml files keep working unchanged.
  See `docs/migration-0.2.md`.
- **`agent-chat-gateway config validate [--config PATH] [--lint]`** — checks
  config.yaml without starting the daemon: full structural validation, plus
  per-connector-type checks (e.g. empty Rocket.Chat/Mattermost credentials)
  that were previously only caught lazily at daemon start, plus a warning
  when a connector's persisted `state.<connector>.json` references a watcher
  name no longer in the config (session about to be dropped). `--lint` flags
  config values that just restate a built-in default or duplicate an
  inherited `*_defaults` value.
- **JSON Schema for config.yaml** (`gateway/schema/config.schema.json`) —
  `config.example.yaml` references it via a `# yaml-language-server: $schema=`
  comment for editor autocomplete and inline typo-checking.
- **Mattermost connector** — a second full chat platform connector (REST v4 +
  WebSocket), alongside Rocket.Chat, with dual auth (Bot Token or
  username/password), RBAC role/mention filtering, threaded replies,
  attachments, reconnect history replay, and shared multi-agent agent-chain
  loop protection (#59). Onboarding CLI wizard support and an E2E docker test
  harness are deferred to a follow-up.
- **`day:` field in the Rocket.Chat message header** — the gateway now
  precomputes the weekday (e.g. `day: Sun`) alongside `ts:` so agents don't
  have to infer it from a bare date, which was unreliable and could cause
  scheduled weekday tasks to be silently skipped (#53).
- **Config TUI: `agent_chain.*` fields for Rocket.Chat/Mattermost connectors
  and their templates.** `agent_usernames`/`max_turns`/`ttl_seconds`
  (`docs/agent-chain.md`) previously had to be hand-edited into config.yaml —
  a plain gap, since both connectors already supported it. Also a Select-
  driven "Auth method" picker for Mattermost connectors (`token` /
  `username + password`) replacing the old plain warning text: only the
  relevant credential fields show, and switching modes clears the other
  group so the two can no longer collide by accident.
- **Config TUI: agent templates can now edit `owner_allowed_tools`/
  `guest_allowed_tools`.** Both fields were already legal on an
  `agent_templates:` entry (nothing in `gateway/config.py` forbids them
  there) but the TUI's template screen never had an editor for them —
  user-reported gap. The existing per-agent tool-list editor (`ListView` +
  "+ Add"/"- Remove" buttons, referencing a `tool_presets:` entry or
  writing an inline rule) was extracted into a shared
  `ToolListEditorMixin` and is now available on agent templates too, with
  its own blast-radius-scoped confirm before saving a change that affects
  other entries.
- **Config TUI: `'v'` shows the actual validation error/warning text.**
  The Overview banner previously showed only a bare count (`✗ 1
  error(s)`) — the real message (e.g. "Agent 'x': working_directory is
  required", surfaced after hand-editing a required field away and
  refreshing) was computed by `validate_config()` but never displayed
  anywhere, leaving no way to find out what to fix short of running
  `agent-chat-gateway config validate` in a separate terminal —
  user-reported. The banner now grows a `(press 'v' to view details)`
  hint whenever there's something to show.

### Changed
- **BREAKING: `online_notification`/`offline_notification` default to quiet
  (`null`) instead of `"✅ _Agent online_"` / `"❌ _Agent offline_"`.** No
  config error results — watchers that never set these fields explicitly
  simply stop announcing online/offline after upgrading. Restore the old
  behavior globally with `watcher_defaults: {online_notification: "✅ _Agent
  online_", offline_notification: "❌ _Agent offline_"}`. See
  `docs/migration-0.2.md`.
- **BREAKING (v0.3 config format): `connector_defaults:`/`agent_defaults:`/
  `watcher_defaults:` removed entirely**, replaced by named
  `connector_templates:`/`agent_templates:`/`watcher_templates:` blocks + a
  per-entry `inherits: <name>` field. The old blocks merged flatly and
  unconditionally into *every* entry of a kind regardless of type — setting
  `command`/`type` in `agent_defaults` to give claude agents a custom
  wrapper silently broke any opencode agent that didn't override it
  (`OpenCodeBackend` execs the configured `command` directly as the sidecar
  binary). Named templates only apply to entries that explicitly opt in, so
  type-specific fields are finally safe to share, and different groups of
  agents/connectors/watchers can each have their own template instead of
  fighting over one global block. A leftover `*_defaults:` key is a hard,
  immediate load-time error (not a silent no-op) naming the replacement key.
  No automated migration — see `docs/migration-0.3.md` for the reasoning and
  before/after recipes. The config TUI (`agent-chat-gateway config`) fully
  understands the new mechanism: a "Templates" tab lists/creates/edits/
  deletes named templates across all three kinds, and every agent/connector
  entry has an Inherits picker.

### Fixed
- **Identity/addressing header now delivered durably via the system
  prompt, surviving Claude Code's auto-compact (closes #52).** Previously,
  the watcher's identity + multi-agent addressing rules — including
  RBAC/injection-defense rules from `context_inject_files` (e.g.
  `contexts/rc-gateway-context.md`) — were sent as a one-time user message.
  Claude Code's auto-compact summarizes conversation history but never
  touches the system prompt, so this content was permanently lost after
  the first compact: the agent would stop honoring the multi-agent
  addressing guideline, causing massive message fan-out in a multi-agent
  room. A second latent bug: this content was also gated behind having
  `context_inject_files`/`history_context` configured, so a watcher with
  neither never received it at all. Fix (Claude only — OpenCode's own fix
  is deferred to a follow-up): `ClaudeBackend` now writes this content to a
  durable per-watcher system-prompt file
  (`~/.agent-chat-gateway/system-prompts/<watcher>.md`) and re-appends it
  via `--append-system-prompt-file` on every turn, so it survives
  compaction and session resume regardless of how long the conversation
  runs (#58).
- **Scheduled-task messages now carry a usable `ts:`/`day:` timestamp.**
  Previously, messages injected by the scheduler used an ISO-formatted
  timestamp that the Rocket.Chat header formatter couldn't parse, so
  `ts:`/`day:` were silently omitted from every scheduled-task prompt —
  exactly the case (e.g. scheduled stock reports) that motivated #53.
- **Config TUI: picking an Inherits template no longer clears an agent's/
  connector's Name or Description.** User-reported: typing a Name +
  Description while creating a new agent, then picking a template via the
  Inherits button, silently cleared both. Neither field is ever set by a
  template, but the Inherits-switch's full-form recompute rebuilt the
  Description Input from the (stale, not-yet-saved) entry and the Name
  Input from a permanently blank default — now tracked separately from
  that recompute so they survive any number of template switches.

---

## [0.4.0] - 2026-05-14

### Added
- **Lazy instruction loading for bundled gateway docs**: agents now receive a
  compact tool index by default and can load full scheduling/history guidance on
  demand with `agent-chat-gateway instructions scheduling` or
  `agent-chat-gateway instructions fetch-history`. Agents can opt out with
  `lazy_instruction_loading: false` to inject the full docs at session start.
- **Rocket.Chat `@all` fan-out routing**: room-wide `@all` mentions are treated
  as explicit permission for broader multi-agent fan-out, while specific agent
  mentions in the same message remain priority responders.

### Changed
- **Multi-agent reply guidance** now emphasizes directed `@mention` replies,
  conservative broadcast behavior, priority responders, and clean silence via
  `<end-of-agent-chain>`.
- **Documentation** now covers built-in context auto-injection, lazy instruction
  loading, and `@all` routing semantics.

---

## [0.3.3] - 2026-05-06

### Fixed
- **`pause` / `resume` CLI no longer require `--connector`**: watcher names are
  globally unique across all connectors, so the server now auto-resolves the
  target connector from the watcher name (same behaviour as `reset`). The
  `--connector` option has been removed from both commands.
- **`<end-of-agent-chain>` token always stripped from agent responses**: previously
  the token was only intercepted during agent-chain turns; a user-to-agent turn
  could still leak the raw token into the chat room. The token is now stripped
  unconditionally. Content on **either side** of the token is preserved and
  delivered — fixing a silent data-loss bug where text placed *after* the token
  (e.g. `<end-of-agent-chain>\nBye now`) was discarded entirely.

---

## [0.3.2] - 2026-05-04

### Fixed
- **`opencode serve` orphan process on `acg stop`**: sequential processor drain
  could take up to 30 s × N watchers before backends were even signalled, causing
  the daemon's SIGKILL grace window to expire before `opencode serve` had a chance
  to exit cleanly. Two changes combined to fix this:
  - `stop_all()` now drains all processors **concurrently** via `asyncio.gather`
    instead of a sequential `for` loop (worst-case drain drops from 30 s × N to
    ~30 s regardless of watcher count).
  - `stop_daemon()` grace window extended from **30 s → 90 s** to absorb the
    worst-case 30 s drain + 20 s backend stop (SIGTERM→SIGKILL ladder).
- **`acg reset` CLI timeout**: reset command now waits up to **300 s** (previously
  60 s) for the socket response, preventing spurious `asyncio.TimeoutError` while
  OpenCode reinitialises its serve process and the context injector replays the
  full session history.

---

## [0.3.1] - 2026-05-04

### Added
- **Multi-agent `to:` field in RC prompt prefix**: each message header now
  includes a compact `to:` routing field so agents know whether they are being
  addressed directly or as bystanders:
  - `to: me` — explicitly @-mentioned or DM → respond normally
  - `to: @wavebro` — another agent @-mentioned, not this bot → stay silent unless essential
  - `to: me+@wavebro` — both this bot and another agent → respond normally
  - `to: *` — no explicit agent @-mention (broadcast) → use judgment
  Only usernames listed in `agent_chain.agent_usernames` appear in `to:`; regular
  user @-mentions remain in the message body unchanged. Closes #17.
- **Agent identity in session context**: the gateway context header injected at
  session start now includes the bot's own `@username` and multi-agent behavior
  guidelines when `agent_chain` is configured, so agents can reason about their
  own identity from the first message.
- **`mentions` field on `IncomingMessage`**: the normalized message dataclass
  now carries the list of @-mentioned usernames from the platform's metadata
  (e.g. RC's `mentions[]` array), available for connector-level routing logic.

---

## [0.3.0] - 2026-05-03

### Added
- **Message timestamp in agent prompt header**: the trusted RC identity header
  now includes a `ts` field with the original message timestamp formatted in
  the connector's local timezone (ISO 8601 with UTC offset), e.g.
  `[Rocket.Chat #general | from: alice | role: owner | ts: 2026-05-03T09:30:00-07:00]`.
  Agents can now reason about message timing, detect stale messages after
  reconnect, and enforce time-based rules (game timeouts, SLA reminders).
  Closes #18.
- **Per-connector `timezone` setting**: each connector now accepts an optional
  `timezone` field (IANA name, e.g. `"America/Los_Angeles"`) that controls
  both message timestamp formatting and the default timezone for scheduled
  tasks created against that connector's watchers. Falls back to the ACG
  server's local timezone when omitted.

### Fixed
- **Config env var false-positive**: passwords or tokens whose resolved value
  contains a `$WORD` pattern (e.g. `myPass$HM`) were incorrectly flagged as
  unresolved environment variables at startup. The scanner now checks the
  original placeholder string rather than the expanded value, so only genuinely
  unresolved variables raise an error. Closes #11.

### Changed
- **Scheduler timezone now per-connector** ⚠️ _migration required_: the
  `scheduler.default_timezone` config field has been removed. Set `timezone`
  on each connector instead — the scheduler automatically uses the watcher's
  connector timezone as its fallback when `--tz` is not supplied to
  `acg schedule create`. Users who had `scheduler.default_timezone` set should
  move that value to the relevant connector's `timezone` field.

---

## [0.2.8] - 2026-04-12

### Fixed
- **`LimitOverrunError` on large tool results**: the two streaming
  `create_subprocess_exec` calls in `ClaudeBackend` now set
  `limit=16 * 1024 * 1024` (16 MB) on the underlying `asyncio.StreamReader`,
  raising it from the 64 KB default. Large single-line outputs — base64-encoded
  images or audio attachments — previously caused
  `asyncio.exceptions.LimitOverrunError: Separator is not found, and chunk
  exceed the limit`, crashing the turn. The non-streaming path that uses
  `communicate()` is unaffected and unchanged.

---

## [0.2.7] - 2026-04-12

### Fixed
- **`acg reset` AttributeError**: removing `--connector` from the reset subparser
  left a stale `args.connector` reference in the reset handler, causing
  `AttributeError: 'Namespace' object has no attribute 'connector'` on every
  `acg reset` invocation. The dead code has been removed.

---

## [0.2.6] - 2026-04-11

### Fixed
- **Permission cross-room bypass**: `approve`/`deny` commands are now scoped to
  the originating room — an owner in room B can no longer resolve a permission
  request that was raised in room A. The registry entry is left pending (not
  removed) on a mismatch so the correct room can still resolve it, and the
  response is identical to "no pending request" to avoid leaking request
  existence across rooms.
- **Permission cross-thread confusion**: when both the pending request and the
  incoming command carry a `thread_id`, they must match. Approvals sent from a
  different thread within the same room are rejected. Room-level approvals
  (`from_thread_id=None`) are intentionally still allowed — no major chat
  platform (RC, Slack, Discord, Teams) enforces thread-level permissions
  separate from the room, so blocking room-level approval would hurt UX with
  no security benefit.
- **RC connector `_handle_send_busy` TypeError**: the `thread_id` kwarg was
  incorrectly named `thread_id=` instead of `tmid=` when calling
  `post_message()`, causing a `TypeError` on every busy-notification attempt.
  The error was silently swallowed by the caller, so busy users never received
  the retry message. Fixed to use the correct `tmid=` kwarg.
- **Scheduler `run_count` consumed on injection failure**: finite jobs
  (`times > 0`) no longer lose a run when the target watcher is unavailable.
  On failure, `next_run` is advanced (to avoid a retry flood) but `run_count`
  and `last_run` are left unchanged so the remaining budget is preserved.
  Infinite jobs (`times = 0`) still advance `run_count` on failure, consistent
  with prior behaviour (the count is non-binding for completion).
- **Scheduler catch-up replay of previously-failed fires**: added
  `last_attempted_at` field to `ScheduledJob` (set on every `_fire_once` call,
  including failures). On daemon restart, the catch-up anchor now uses
  `last_attempted_at` instead of `last_run`, preventing replay of fire slots
  where injection already failed. Backward-compatible: old `jobs.json` files
  without the field fall back to `last_run` as before.

### Changed
- **`acg reset` no longer requires `--connector`**: watcher names are globally
  unique across all connectors, so the control server now resolves the owning
  connector automatically by searching all entries. The `--connector` flag has
  been removed from the CLI to eliminate a redundant and confusing argument.
- **Scheduler injection failure notifications**: when a scheduled job cannot be
  delivered (watcher not running), a best-effort notification is sent directly
  to the watcher's room via the connector — bypassing the watcher queue so it
  arrives even when the watcher is paused or its queue is full. Paused watchers
  log at INFO level (expected state) rather than WARNING and receive no
  notification; only unexpected failures (non-paused watcher unavailable) get
  notified.

---

## [0.2.5] - 2026-04-11

### Added
- **`Skill` tool allow-list support**: added `"skill"` field mapping to
  `_CLAUDE_PARAM_FIELD` so the `Skill` tool extracts the skill name directly
  (e.g. `"daily-briefing"`) instead of falling back to the full JSON blob.
  Config rule `params: "daily-briefing"` now correctly auto-approves Skill
  tool calls without triggering a permission prompt.
- **Daily-briefing runner script** (`gateway/agents/opencode/skills/daily-briefing/runner.py`):
  stdlib-only Python script that fetches all briefing data sources in parallel
  (stocks via Yahoo Finance/stooq, TechCrunch RSS, Hacker News, GitHub
  Trending, world news RSS, entertainment RSS) and prints a single JSON object
  to stdout. Reduces daily briefing run time from ~4–5 min to ~1–1.5 min by
  collapsing multiple LLM/HTTP round-trips into one parallel fetch + one LLM
  pass for summary and HTML generation.

---

## [0.2.4] - 2026-04-11

### Fixed
- **Heredoc command matching in allow-list**: bash commands using heredoc
  redirects (`python3 << 'EOF' ... EOF`) were incorrectly reduced to just
  the interpreter name (`python3`) by the tree-sitter AST walker. The full
  `redirected_statement` text (including the heredoc body) is now extracted,
  so allow-list patterns can inspect the heredoc content — e.g.
  `python3.*github\.com/trending.*` now correctly matches a Python heredoc
  that fetches GitHub trending. Compound commands with heredoc (e.g.
  `python3 << 'EOF'...EOF && rm -rf /`) are still split correctly — the
  dangerous sub-command is extracted separately and must also satisfy the
  allow list.

---

## [0.2.3] - 2026-04-11

### Fixed
- **Watcher validation at schedule create time**: unknown watcher names are
  now rejected immediately with an actionable error listing all available
  watcher names — the agent can self-correct without waiting for fire time.

### Changed
- **`--connector` removed from `acg schedule create`**: the watcher name
  uniquely identifies the connector; specifying `--connector` was redundant
  and was the root cause of the watcher validation bypass bug.
- **`scheduling-context.md`**: added explicit warning to always use the exact
  watcher name from `acg list` — do not guess or invent names.

---

## [0.2.2] - 2026-04-11

### Changed
- **`JobStore.save()` cleanup**: removed the EBUSY fallback added in 0.2.1 —
  superseded by the `data/` directory mount which allows atomic `rename(2)`
  natively. Drops the unused `errno` import.

---

## [0.2.1] - 2026-04-11

### Fixed
- **Docker EBUSY error**: `JobStore.save()` now falls back to in-place write
  when `rename()` returns `EBUSY` (caused by Docker single-file bind-mounts
  pinning the file inode).
- **`jobs.json` moved to `data/` subdirectory** (`~/.agent-chat-gateway/data/jobs.json`):
  use a directory bind-mount (`./data:/root/.agent-chat-gateway/data`) instead
  of a single-file mount to avoid the EBUSY issue entirely. The `data/`
  directory is pre-created in `Dockerfile.acg` and in `docker-compose.example/`.
  Future persistent runtime files can be added to `data/` without changing
  the Docker volume configuration.

### Migration (Docker users upgrading from 0.2.0)
If you had `./jobs.json` mounted as a single-file volume:
1. `mkdir data && mv jobs.json data/`
2. Update `docker-compose.yml`: replace `- ./jobs.json:/root/.agent-chat-gateway/jobs.json`
   with `- ./data:/root/.agent-chat-gateway/data`
3. `docker compose up -d`

---

## [0.2.0] - 2026-04-10

### Added
- **In-process job scheduler** (`acg schedule`): schedule recurring or one-shot
  agent tasks without leaving the chat. Jobs persist across restarts in
  `~/.agent-chat-gateway/jobs.json` with atomic writes.
- **`acg schedule create`**: create recurring jobs (`--every 1h`, `--every 1d`,
  `--every 1w`) or one-shot reminders (`--in 30m`, `--in 2h`), with optional
  `--times N` run limit and `--tz` timezone support.
- **`acg schedule list`**: display active/paused jobs in a formatted table;
  `--all` includes recently completed jobs.
- **`acg schedule delete / pause / resume`**: full lifecycle management.
- **Direct message injection**: scheduled jobs bypass the Rocket.Chat self-message
  filter entirely — messages are injected directly into the watcher's message
  processor queue as `OWNER`-role messages.
- **Catch-up on restart**: all missed fires are replayed immediately on daemon
  startup, with correct run-count tracking.
- **`scheduling-context.md`**: built-in context file auto-injected into every
  agent session, teaching the agent the `acg schedule` CLI commands.
- **Thread-safe `JobStore`**: `threading.Lock` + copy-on-write pattern ensures
  concurrent reads/writes from `asyncio.to_thread()` workers are safe.
- **TTL-based completed job purge**: completed jobs are automatically removed
  after `scheduler.completed_job_ttl_days` (default 7 days).
- **`gateway/core/tz_utils.py`**: cross-platform IANA timezone detection utility.
- **New dependency**: `croniter>=2.0.0` for cron expression parsing.

### Changed
- Built-in context files (`rc-gateway-context.md`, `scheduling-context.md`)
  moved from `contexts/` to `gateway/contexts/` (shipped inside the Python
  package) so they are always available regardless of install method.
- `config.py`: built-in context files are now auto-injected at Layer 0 for all
  connectors; no manual `context_inject_files` entry needed for the defaults.

---

## [0.1.9] - 2026-04-06

### Changed
- Re-release of 0.1.8 to fix PyPI publish after history rewrite removed
  `docker_env/` (which contained sensitive data) from all prior commits.
  No functional code changes from 0.1.8.

---

## [0.1.8] - 2026-04-06

### Added
- **Docker support**: `Dockerfile.acg`, `docker/entrypoint.acg.sh`, and
  `docker/docker-compose.example/` for deploying ACG via Docker. The image
  is published to `ghcr.io/hammermei/agent-chat-gateway` on every release.
- **GitHub Container Registry**: `.github/workflows/docker.yml` builds and
  pushes `linux/amd64` + `linux/arm64` images on every `v*` tag.

---

## [0.1.7] - 2026-04-06

### Fixed
- **OpenCode bash permission bypass**: OpenCode's default permission ruleset
  uses `"*": "allow"`, which caused all bash commands to run without emitting
  a `permission.asked` SSE event, completely bypassing ACG's permission broker.
  ACG now injects `bash["*"] = "ask"` via `OPENCODE_CONFIG_CONTENT` at sidecar
  startup so that bash tool calls are properly intercepted and enforced by
  `owner_allowed_tools` / `guest_allowed_tools`. A set of read-only git commands
  and `agent-chat-gateway send` are pre-approved as safe defaults. Users who
  explicitly set a `"*"` catch-all in their own `OPENCODE_CONFIG_CONTENT` are
  unaffected.

---

## [0.1.6] - 2026-04-04

### Added
- **OpenCode SSE streaming** (`stream()` method on `OpenCodeBackend`): intermediate
  agent events — tool calls, text deltas, step completions — are now surfaced in
  real time via the `GET /event` SSE endpoint instead of waiting for the full turn
  to complete. Events are consumed via an `asyncio.Queue` background task and yielded
  as `AgentEvent` objects with deduplication and deadline enforcement.
- `_post_message_async()`: new fire-and-forget POST to `/session/{id}/prompt_async`
  (HTTP 202) using a dedicated `_PROMPT_ASYNC_POST_TIMEOUT` internal constant.
- RC typing-indicator is now refreshed periodically during long SSE streaming turns.

### Changed
- `_SSE_QUEUE_POLL_INTERVAL` renamed to `_SSE_QUEUE_MAX_WAIT` to more accurately
  describe its role as an upper bound on `queue.get()` blocking time.
- `has_usage` token sentinel now includes cache token buckets so that cache-only
  turns correctly produce a `TokenUsage` object instead of returning `None`.
- `duration_ms` in `AgentResponse` is coerced to `int` (or `None`) from the HTTP
  response; the SSE path explicitly does not populate this field.
- All error messages across the OpenCode adapter are now sanitized: no raw exception
  strings, response bodies, or internal host:port values appear in user-facing errors.

### Fixed
- `base_url` is captured before spawning the SSE background task to eliminate a
  race with concurrent `stop()` calls that could null out `self._base_url`.
- `assert` statement replaced with `if/raise` for the SSE handshake invariant check
  (bare asserts are stripped under Python `-O` optimization flag).
- Token accumulator fields (`input_tokens`, `output_tokens`, etc.) now apply
  `int()` coercion in both SSE and HTTP parse paths, preventing silent float
  violations of `TokenUsage`'s `int` type contract.
- `create_session` no longer includes the raw API response dict in `RuntimeError`
  messages; raw body is logged at `DEBUG` level instead.

---

## [0.1.5] - 2026-04-01

### Added
- Context files (`contexts/`) are now copied to `~/.agent-chat-gateway/contexts/`
  on install so that `config.yaml` path references resolve correctly without
  pointing into the git repo.
- `upgrade` now syncs context files after `git pull` with smart merge:
  unchanged user copies are overwritten; user-modified copies are saved as
  `<name>.default` with a warning to merge manually.

### Changed
- `config.example.yaml`: moved `contexts/rc-gateway-context.md` from
  agent-level to connector-level `context_inject_files` so it is shared
  across all agents using that connector.
- `onboard.py`: generated config now sets connector-level
  `context_inject_files` (was incorrectly empty before).

### Fixed
- `install.sh`: `RUNTIME_DIR` is now defined before the context copy block
  (was referenced before assignment in the previous release).

---

## [0.1.4] - 2026-04-01

### Fixed
- `install.sh`: always write `install_meta.json` so that `upgrade` works even
  when `--no-onboard` skips the interactive wizard.
- `upgrade`: resolve `uv` via common fallback paths (`~/.local/bin/uv`,
  `~/.cargo/bin/uv`) when it is absent from PATH — common in SSH sessions.

---

## [0.1.3] - 2026-04-01

### Added
- Agent can now send files or attachments to Rocket.Chat by running
  `agent-chat-gateway send <room> --attach /path/to/file` directly.
  Added Bash allow-rule (`agent-chat-gateway\s+send\s+.*`) to
  `config.example.yaml` for both owner and guest tool lists (Claude and
  OpenCode sections), and documented the pattern in
  `contexts/rc-gateway-context.md`.
- Added `--no-onboard` flag to `install.sh` for agent-driven installs that
  skip the interactive onboarding wizard.
- Installer now informs the user of the executable location and the shell
  source command needed to activate it post-install.

### Fixed
- OpenCode adapter: gracefully handle empty body and non-JSON API responses
  instead of crashing with an unhandled exception.
- Installer: install `uv` first, then use it to install Python 3.12 when the
  system Python is too old.

### Changed
- `install.sh` now clones the repo into `~/.agent-chat-gateway/repo` instead
  of directly into `~/`.

### Docs
- Clarify `working_directory` as the project folder; default to current
  directory when omitted.
- Add PATH setup step for pip-installed packages.

---

## [0.1.2] - 2026-03-30

### Fixed
- `upgrade` command: detect pip-installed packages and run
  `pip install --upgrade agent-chat-gateway` automatically.

### Docs
- Promote AI-guided install path, add git prerequisite note, make
  `install-agent.md` more agent-friendly.

---

## [0.1.1] - 2026-03-30

### Fixed
- Move dependencies into `[project]` section in `pyproject.toml`.
- Resolve all ruff lint violations and pre-existing `PID_FILE` import error.

---

## [0.1.0] - Initial release

First public release of agent-chat-gateway.
