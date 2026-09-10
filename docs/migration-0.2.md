# Migrating to the v0.2 config format

> **Later change:** the top-level `watchers:` block shown below was renamed to
> `watcher_rules:` in a later version, and its entries changed shape. The examples
> here are kept as they were at the time — see `migration-v1.md` for
> the current form.


> **Note:** v0.3 removes the `connector_defaults:`/`agent_defaults:`/
> `watcher_defaults:` blocks this doc introduces, entirely, in favor of named
> `*_templates:` + a per-entry `inherits:` field — see
> [docs/migration-0.3.md](migration-0.3.md). Everything else on this page
> (`tool_presets:`, `rooms:`, `description:`) is still current.

v0.2 adds a compact config format on top of what already worked: top-level
`connector_defaults:` / `agent_defaults:` / `watcher_defaults:` blocks that
deep-merge into every entry, named `tool_presets:` you can reference from any
agent's tool allow-lists, and `rooms:` to bind one connector+agent pair to
many rooms without repeating the whole watcher entry.

**None of this is required.** Every field from the old format still parses.
You can adopt the new format incrementally, field by field, or not at all.

## 1. The one behavior change — read this first

Watchers no longer post an "Agent online" / "Agent offline" message by
default. The default for `online_notification` / `offline_notification` is
now `null` (quiet) instead of `"✅ _Agent online_"` / `"❌ _Agent offline_"`.

**This does not raise a config error** — an existing config.yaml that never
set these fields explicitly will silently go quiet after upgrading. If you
want the old behavior back, set it once for every watcher via
`watcher_defaults:`:

```yaml
watcher_defaults:
  online_notification: "✅ _Agent online_"
  offline_notification: "❌ _Agent offline_"
```

Run `agent-chat-gateway config validate` after upgrading — it does not detect
this particular change (it's a valid, quieter default, not an error), but it's
good practice to re-validate after any version bump.

## 2. Everything else is additive

`room:` (singular) still works exactly as before — it's now formally an alias
for `rooms: [<room>]`. Explicit watcher `name:`, full `attachments:` /
`agent_chain:` / `permissions:` blocks, inline tool rules — all unchanged. You
do not need to touch a working config.yaml at all to stay on v0.2.

## 3. Before adopting `rooms:` — watcher names are persistent identifiers

A watcher's `name` (explicit or auto-generated) is a filesystem- and
state-persistence key:

- `~/.agent-chat-gateway/state.<connector>.json` — keyed by watcher name;
  session ID, sticky/paused flags
- `<working_directory>/.acg-attachments/<name>/` — attachment cache
- `<RUNTIME_DIR>/system-prompts/<name>.md` — injected system prompt
- `agent-chat-gateway pause|resume|reset <name>` — CLI muscle memory

If you rewrite a `name: my-watcher` / `room: general` entry as
`rooms: [general]` (dropping the explicit name), the watcher's name changes
from `my-watcher` to the auto-generated `<connector>-general`. That is a
**different identity** as far as the gateway is concerned — the running
session under the old name is orphaned:

- Options:
  1. **Keep the explicit `name:`** on any watcher whose session you care
     about, and only use bare `rooms:` for new or disposable bindings. This
     is the simplest and safest choice — do this for anything already in
     production.
  2. **Migrate state.json by hand**, if you want to keep both the session
     and the new auto-generated name: stop the daemon
     (`agent-chat-gateway stop`), rename the top-level watcher-name key in
     `~/.agent-chat-gateway/state.<connector>.json` from the old name to the
     new one, then start again. Attachment and system-prompt files under the
     old name are left on disk (harmless orphans — safe to delete once
     you've confirmed the new name is working).

Run `agent-chat-gateway config validate` after any watcher-naming change — it
warns when a connector's `state.<connector>.json` contains a watcher name
that no longer exists in the (expanded) config, which is exactly this
situation.

## 4. Before/after recipes

**Connector `agent_chain:` copied into every bot's connector:**

```yaml
# Before — repeated verbatim per connector
connectors:
  - name: mm-bot-a
    type: mattermost
    agent_chain: {agent_usernames: [bot-a, bot-b], max_turns: 5, ttl_seconds: 60}
    server: {url: "https://chat.example.com", team: home, username: "bot-a", password: "bot-a-password"}
  - name: mm-bot-b
    type: mattermost
    agent_chain: {agent_usernames: [bot-a, bot-b], max_turns: 5, ttl_seconds: 60}
    server: {url: "https://chat.example.com", team: home, username: "bot-b", password: "bot-b-password"}

# After — one shared block, per-connector fields only where they differ
connector_defaults:
  type: mattermost
  agent_chain: {agent_usernames: [bot-a, bot-b], max_turns: 5, ttl_seconds: 60}

connectors:
  - name: mm-bot-a
    server: {url: "https://chat.example.com", team: home, username: "bot-a", password: "bot-a-password"}
  - name: mm-bot-b
    server: {url: "https://chat.example.com", team: home, username: "bot-b", password: "bot-b-password"}
```

**Tool allow-lists copied into every agent:**

```yaml
# Before
agents:
  bot-a-agent: {owner_allowed_tools: [{tool: Read}, {tool: Glob}, {tool: Grep}]}
  bot-b-agent: {owner_allowed_tools: [{tool: Read}, {tool: Glob}, {tool: Grep}]}

# After
tool_presets:
  readonly: [{tool: Read}, {tool: Glob}, {tool: Grep}]
agents:
  bot-a-agent: {owner_allowed_tools: [readonly]}
  bot-b-agent: {owner_allowed_tools: [readonly]}   # presets and inline rules mix freely
```

**Watcher cartesian product (one connector+agent, many rooms):**

```yaml
# Before — one full entry per room, name/session_id/notifications repeated
watchers:
  - {name: bot-a-nest, connector: mm-bot-a, room: nest, agent: bot-a-agent,
     session_id: null, context_inject_files: [], online_notification: null, offline_notification: null}
  - {name: bot-a-hammer, connector: mm-bot-a, room: hammer, agent: bot-a-agent,
     session_id: null, context_inject_files: [], online_notification: null, offline_notification: null}

# After
watchers:
  - {connector: mm-bot-a, agent: bot-a-agent, rooms: [nest, hammer]}
    # -> mm-bot-a-nest, mm-bot-a-hammer
```

## 5. New tooling

- `agent-chat-gateway config validate [--config PATH] [--lint]` — validates
  config.yaml without starting the daemon; also instantiates each
  connector's own config (catching empty `server:` credentials that
  `from_file` alone doesn't check) and warns about state.json orphans. Add
  `--lint` to flag values that just restate a built-in default or duplicate
  a `*_defaults` entry.
- `gateway/schema/config.schema.json` — a JSON Schema for editor
  autocomplete and typo-checking. `config.example.yaml` references it via a
  `# yaml-language-server: $schema=...` comment; most YAML-aware editors
  (VS Code + the YAML extension, Neovim + yaml-language-server, etc.) pick
  it up automatically.

See `config.example.yaml` for a fully annotated reference using the new
format, including a commented multi-connector example showing
`connector_defaults` / `agent_defaults` / `watcher_defaults` together.

## 6. New: `description:` fields

Every connector, agent, and watcher entry, plus each `*_defaults` block, may
carry an optional `description: <text>` — a free-text note that is purely
informational: never validated, never read at runtime, safe to omit. It
exists so tooling (starting with the config TUI, `agent-chat-gateway config`)
has a structured place to show a human-readable note about an entry, instead
of relying on a YAML comment that a tool can't reliably attribute to the
right entry (or preserve across a rewrite).

**If you're planning to use the config TUI once it can save changes:** a
tool-driven save rewrites the whole file and does not preserve hand-written
YAML comments (a timestamped backup is made before every such save, so
nothing is unrecoverable). Moving any notes you care about from comments
into `description:` fields ahead of time means they survive future TUI saves
unchanged.
