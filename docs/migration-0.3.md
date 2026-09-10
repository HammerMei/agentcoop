# Migrating to the v0.3 config format

> **Later change:** the top-level `watchers:` block shown below was renamed to
> `watcher_rules:` in a later version, and its entries changed shape. The examples
> here are kept as they were at the time — see `migration-v1.md` for
> the current form.


v0.3 removes the global `connector_defaults:` / `agent_defaults:` /
`watcher_defaults:` blocks introduced in v0.2 and replaces them with named,
reusable `connector_templates:` / `agent_templates:` / `watcher_templates:`
blocks — each a mapping of `template-name -> {fields...}` — referenced from
an individual entry via a new `inherits: <template-name>` field.

**This is a breaking change, not an additive one.** Unlike every other v0.2
addition, a leftover `agent_defaults:` (or `connector_defaults:`/
`watcher_defaults:`) key is a hard, immediate `ValueError` at load time — not
a silent no-op, and not a deprecation warning. See "Why a hard error, not a
quiet fallback" below for the reasoning.

## 1. Why this changed

The old mechanism merged a single shared block flatly and **unconditionally
into every entry of a kind, regardless of type**. That's fine for
type-agnostic fields (`timeout`, `permissions`), but genuinely dangerous for
type-specific ones: setting `command`/`type` in `agent_defaults` to give
claude agents a custom wrapper command silently broke any opencode agent
that didn't explicitly override it, since `OpenCodeBackend` execs
`agent_cfg.command` directly as the sidecar binary — a claude-shaped command
value there tries to spawn the wrong process entirely, with no error at load
time.

More broadly: a single global block per kind has no way to express "these 5
claude agents share one profile, these 5 opencode agents share a different
one" — every entry either takes the one global block or repeats every field
itself, which defeats having a sharing mechanism at all for exactly the case
(a fleet of agents split across a few distinct roles) it was meant to help.

Named templates fix both problems at once: a `claude-standard` template can
legitimately set `type: claude, command: claude-no-p`, and an
`opencode-standard` template can legitimately set
`type: opencode, command: opencode` — each only ever applies to the entries
that explicitly opt in via `inherits:`, so a template being type-specific is
no longer a footgun.

## 2. Why a hard error, not a quiet fallback

A leftover `agent_defaults:` (etc.) key raises immediately, with a message
naming the replacement key:

```
config.yaml 'agent_defaults:' is no longer supported (removed) — define
shared fields under 'agent_templates:' instead and add
'inherits: <template-name>' to each entry that should use them. See
docs/migration-0.3.md.
```

Silently ignoring the old key instead would be worse: whatever
permission/timeout/tool-allow-list settings you thought were still shared
would just silently stop applying, with zero signal that anything changed.
An explicit, actionable error at load time is far preferable to a fleet of
agents quietly reverting to built-in defaults.

There is no automated migration tool for this change (unlike the `.env` →
`config.yaml` migration in v0.2, which runs automatically on every
`agent-chat-gateway start`) — converting `*_defaults:` to `*_templates:` +
`inherits:` is a small, mechanical, one-time edit (see the recipes below),
and doing it by hand is also the moment to decide whether your fleet
actually needs more than one template per kind.

## 3. Before/after recipes

**A shared agent profile, one template, every agent opts in:**

```yaml
# Before (v0.2) — applies to EVERY agent, regardless of type
agent_defaults:
  type: claude
  command: claude-no-p
  timeout: 1800
  permissions: {enabled: true, timeout: 300}
agents:
  bot-a-agent: {}                     # inherited everything from agent_defaults
  bot-b-agent:
    timeout: 500                      # overrode just this one field

# After (v0.3) — a named template, referenced explicitly per agent
agent_templates:
  standard:
    type: claude
    command: claude-no-p
    timeout: 1800
    permissions: {enabled: true, timeout: 300}
agents:
  bot-a-agent:
    inherits: standard                # inherits everything from the template
  bot-b-agent:
    inherits: standard
    timeout: 500                      # still overrides just this one field
```

**Two agent types sharing one fleet — the case the old mechanism couldn't express:**

```yaml
agent_templates:
  claude-standard:
    type: claude
    command: claude-no-p
    timeout: 1800
  opencode-standard:
    type: opencode
    command: opencode
    timeout: 1800

agents:
  bot-a-agent: {inherits: claude-standard}
  bot-b-agent: {inherits: claude-standard}
  bot-c-agent: {inherits: opencode-standard}
  bot-d-agent: {inherits: opencode-standard}
```

**Connector and watcher templates convert the same way:**

```yaml
# Before
connector_defaults:
  type: mattermost
  agent_chain: {agent_usernames: [bot-a, bot-b], max_turns: 5, ttl_seconds: 60}
connectors:
  - name: mm-bot-a
    server: {url: "https://chat.example.com", team: home, username: "bot-a", password: "bot-a-password"}

watcher_defaults:
  online_notification: "✅ _Agent online_"
  offline_notification: "❌ _Agent offline_"
watchers:
  - {connector: mm-bot-a, agent: bot-a-agent, rooms: [nest, hammer]}

# After
connector_templates:
  standard:
    type: mattermost
    agent_chain: {agent_usernames: [bot-a, bot-b], max_turns: 5, ttl_seconds: 60}
connectors:
  - name: mm-bot-a
    inherits: standard
    server: {url: "https://chat.example.com", team: home, username: "bot-a", password: "bot-a-password"}

watcher_templates:
  standard:
    online_notification: "✅ _Agent online_"
    offline_notification: "❌ _Agent offline_"
watchers:
  - {connector: mm-bot-a, agent: bot-a-agent, rooms: [nest, hammer], inherits: standard}
    # -> mm-bot-a-nest, mm-bot-a-hammer, both inheriting the template
```

## 4. What didn't change

- `inherits:` is a **single template name**, not a list — an entry inherits
  from at most one template. This is deliberate: multi-template composition
  is more powerful but opens a whole additional dimension of merge-order
  complexity that isn't worth it yet; some repeated fields across templates
  are an acceptable tradeoff for keeping the mental model simple.
- **No nested templates** — a template setting `inherits:` itself is a
  `ValueError` (mirrors `tool_presets:`'s existing "no preset-of-presets"
  rule). This keeps resolution flat: an entry's effective value is always
  either explicit-on-the-entry, from-its-one-template, or the built-in
  code-level default — never a longer chain.
- Templates are validated for structure only (must be a mapping, must not
  set an identity field like `name`/`room`/`rooms`/`session_id`, must not
  set `inherits`) — never for "is every field a real agent/connector/watcher
  needs actually present." A template is meant to be a *partial* field set
  (e.g. a template need not set `working_directory`, since that's inherently
  per-agent); "is everything required actually present" is still checked
  only once, on the fully-resolved (template ∪ entry) value.
- `tool_presets:`, `rooms:`, and everything else from v0.2 is unchanged.
- **The config TUI** (`agent-chat-gateway config`) fully understands
  `*_templates:`/`inherits:` — a "Templates" tab lists named templates
  across all three kinds with full create/edit/delete, and every agent/
  connector entry has an Inherits picker to set/change/clear which template
  it opts into. (This was a real, temporary gap right after the engine
  change above shipped — see `docs/design/config-tool.md` for how it was
  closed.)
