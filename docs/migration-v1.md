# Migrating to AgentCoop v1

v1.0.0 is a clean break from the v0 line (agent-chat-gateway 0.x) in two ways,
and both are handled by reinstalling rather than by migrating anything in place:

1. **The product, command, environment variables and runtime directory are
   renamed** — AgentCoop, `coop`, `COOP_*`, `~/.agentcoop`. Nothing in
   `~/.agent-chat-gateway` is read by v1.
2. **Watchers are rules, created per room at runtime** — `config.yaml` has to be
   rewritten, not renamed. See *Migrating to watcher rules* below.

There is no automatic migration for either. The v0 line stays installable:
`v0.5.2` is its last release, and its install script and image keep working
under the old names.

## Removing an agent-chat-gateway (v0) installation

Do this before installing v1, so the two do not both connect to your chat
platform with the same bot account.

```bash
# 1. Stop the old daemon. If `agent-chat-gateway stop` only prints the rename
#    notice (the install already pulled v1), stop it by its pid file instead:
agent-chat-gateway stop || kill "$(cat ~/.agent-chat-gateway/gateway.pid)"

# 2. Remove what the old installer created
rm -rf ~/.agent-chat-gateway                         # repo clone, config, state, logs, jobs
rm -f ~/.local/bin/agent-chat-gateway ~/.local/bin/acg-provision
# If you cloned the repo yourself and ran install.sh from it, the clone is
# wherever you put it (install_meta.json's repo_path) — remove or keep as you like.

# 3. OpenCode only: the role-enforcement plugin the v0 wizard copied to
#    ~/.opencode/plugins/ reads the OLD ACG_ROLE variable. With it in place,
#    v1's owner sessions would get no approval prompts for write tools — silently.
#    Remove it; the v1 wizard installs the new copy.
rm -f ~/.opencode/plugins/role-enforcement.ts

# 4. Install AgentCoop and run its setup wizard
curl -fsSL https://raw.githubusercontent.com/HammerMei/agentcoop/main/install.sh | bash
```

Before step 2, keep whatever you want to carry over by hand: your old
`config.yaml` (as a reference while rewriting it in the new shape), agent
working directories, and any context files under `~/.agent-chat-gateway/contexts/`
you had edited. Session state is not worth keeping — a v1 watcher starts a fresh
agent session regardless.

If a v0 install has already run `agent-chat-gateway upgrade` and pulled v1 code
into its repo, the old command prints this same guidance and exits. To stay on
v0 instead: `cd ~/.agent-chat-gateway/repo && git checkout v0.5.2 && uv sync`.

Anything that read the old environment variables — hooks, scripts, an
`opencode.json` — needs the new names: `COOP_ROLE`, `COOP_ALLOWED_TOOLS`,
`COOP_APPROVAL_TOOLS`, `COOP_CONFIG`, `COOP_ADMIN_CONFIG`. The old `ACG_*`
names are not set by v1.

## Migrating to watcher rules (dynamic watchers)

The static watcher shape — a `room:` key, or `rooms:` as a list — has been
removed. `watcher_rules:` entries are now **rules**: they declare which rooms an
agent serves, and the gateway creates each room's watcher on demand — on the
room's first message (Rocket.Chat, Mattermost), or eagerly at startup for
connectors with no inbound stream (voice, script).

**Two top-level keys changed.** The block is called `watcher_rules:` now, not
`watchers:`, and `default_agent:` is gone. A config still using either fails at
load, because an unrecognised top-level key is an error rather than something
quietly skipped:

```
config.yaml sets 'watchers', which this gateway does not use. Valid top-level
keys are: ... 'watcher_rules', 'watcher_templates'.
'watchers' — did you mean 'watcher_rules'?
```

Renaming the key is not the whole job, though — the entries under it changed
shape as well, and a leftover `room:` inside one is reported the same way
(`unknown key(s) 'room'`, with `rooms` in the list of valid keys). `coop config
validate` reports every entry that needs rewriting in one pass.

**This is not a 1:1 rename.** Read *What changes underneath* before editing —
some of the old shape's guarantees are deliberately not carried over.

## The rewrite

Old — note the block name, which changed too:

```yaml
watchers:
  - name: support
    connector: rc-home
    room: general
  - connector: rc-home
    rooms: [dev, ops]
  - connector: rc-home
    room: "@alice"
```

New:

```yaml
watcher_rules:
  - name: my-rooms            # required — rules are not auto-named
    agent: my-agent           # required — no default_agent: any more
    connector: rc-home        # required — no "first connector" default either
    rooms:
      include: [general, dev, ops]
      direct: true            # replaces the "@alice" entry — see below
```

Field notes:

- **`name:` is required** and names the *rule*, not a watcher. Each created
  watcher gets a derived name like `rc-home:general`, which is what
  `list`/`pause`/`resume`/`reset`/`expire` act on.
- **`rooms:` can come from a template.** A `watcher_templates:` entry may set
  it, so a policy every rule should carry — an exclusion, say — is written once:

  ```yaml
  watcher_templates:
    channels:
      connector: rc-home
      rooms: {except_for: ['*-secret']}   # no inheriting rule serves these
  watcher_rules:
    - {name: eng, inherits: channels, rooms: {include: ['eng-*']}}
    - {name: ops, inherits: channels, rooms: {include: ['ops-*']}}
    - {name: dms, connector: rc-home, agent: my-agent, rooms: {direct: true}}
  ```

  All four `rooms` subkeys inherit, and a rule's own `rooms:` merges over the
  template's key by key. **There are several ways to get this wrong that this
  example is arranged to avoid** — a shared `direct: true` leaves every rule
  but the first with no DMs, a rule's list replaces the template's rather than
  adding to it, and an inherited exclusion that cannot match anything the rule
  includes is a hard error. Each is spelled out with its message in
  [Templates and `rooms` inheritance](user-guide.md#templates-and-rooms-inheritance).
- **`rooms:` is a mapping**: `include:` takes glob patterns over room names
  (`eng-*`, `general`, `*`), `except_for:` subtracts from this rule's own
  include, and `direct:` / `group_direct:` opt into the two DM kinds.
- **A DM entry cannot be named.** `room: "@alice"` worked because the static
  path resolved the name at startup; a rule matches rooms as they arrive, and
  a DM has no room name for a pattern to match. `direct: true` claims 1:1
  DMs (the connector's `owners`/`guests` lists still gate who can talk);
  `group_direct: true` claims multi-party DMs, where mentions are required.
- Rules match top-down; the first rule that claims a room wins. `coop config
  validate` warns about rules an earlier rule shadows completely.
- `session_id:` was removed separately and stays removed. To carry context
  into a new session, have the agent summarise the session to a file and list
  it in `context_inject_files` — a handoff survives the backend expiring a
  session, which pinning never did (docs/user-guide.md, Use Case 3).
- Connectors with no inbound stream (**voice, script**) require literal
  `rooms.include` entries — a pattern has nothing to match against there —
  and their rooms start eagerly at boot, as the static shape's did.

## What changes underneath — the accepted losses

1. **Every room gets its own watcher and its own session.** A multi-room
   static entry shared nothing; a rule creates one watcher per room. Existing
   static-era sessions are **not** carried over: the first boot after the
   upgrade prunes every static-era state record (logged per record), and each
   room's first message starts a fresh session. If a session's content
   matters, have the agent summarise it to a file *before* upgrading and
   inject the file into the new session.
2. **Watermarks go with the records.** Messages that arrived while the
   gateway was down across the upgrade boundary are not replayed.
3. **Scheduled jobs name watchers.** Jobs targeting old static watcher names
   point at nothing after the prune — recreate them against the new derived
   names (`coop list` shows them once the rooms have spoken).
4. **A paused room becomes active** unless re-expressed. Pause acts on a
   record, and the static record is pruned. To keep the bot out of a room
   durably, put the room in the rule's `rooms.except_for:` — declarative, and
   effective before the first message. (Pausing the new watcher after it is
   created also works, but only once it exists.)
5. **Watchers now idle and expire.** A room quiet for `session_idle_days`
   (default 15) releases its runtime (session kept; the next message resumes
   it); a further `session_expire_days` (default 15) reclaims the session and
   record entirely, and the room's next message starts fresh. Pause a watcher
   to exempt it from both timers.

## Checklist

1. Rename the block from `watchers:` to `watcher_rules:`.
2. Delete `default_agent:`, and make sure every rule states both an `agent:`
   and a `connector:` — or takes them from its `inherits:` template. Neither
   has an implicit default any more. The old ones resolved to whichever agent
   and connector came first in the file, which is a binding nobody wrote down,
   and reordering those blocks silently re-pointed rules that relied on it.
3. Rewrite each entry under it as a rule (above).
4. `coop config validate` — fix every error; read the shadowing warnings.
5. If any old session's content matters, export it via a summary file first.
6. Restart the gateway. Expect one `Pruning static-era watcher record`
   log line per old record — that is the clean break, not a fault.
7. Delete every scheduled job that targeted an old static watcher name
   (`schedule list`, then `schedule delete <id>` for each) — the prune
   removes the records, NOT the jobs, and an orphaned job re-fires forever
   against a watcher that no longer exists. Then recreate the jobs against
   the new watcher names.
8. `coop schedule migrate` — records the room each remaining job
   targets, so a later room rename cannot orphan it. **Before you rename any
   rooms**: the migration finds a job's room through its watcher name. Step 7's
   recreated jobs already have it; this is for anything that survived.

## Changing a connector's server (or type)

A persisted watcher record binds to a **platform room id**, and room ids are
meaningful only on the server that minted them. If you point an existing
connector name at a different server (a new `server.url`, or a different
connector `type`), its old records will try to resume against rooms that do
not exist there — each watcher reads `failed`, loudly, until it expires.

When you migrate servers, do one of:

- **rename the connector** — the old `state.<name>.json` is then reported by
  `coop config validate` as belonging to no configured connector, and you can
  delete it deliberately; or
- **delete the state file** for that connector before the first start against
  the new server.

Session content does not survive a server move either way; export anything
that matters first (checklist step 5).

## Mattermost DM watchers created before the `@` strip

Mattermost sends a DM's counterpart `@`-prefixed on the websocket event, and the
connector used to carry that through. Since `@` is not label-safe it was
percent-encoded, so the same person had two handles depending on the platform:

```
rc-e2e:dm:test_user        ← Rocket.Chat
mm-e2e:dm:%40test_user     ← Mattermost, before the strip
```

The connector now drops the prefix, and **there is nothing to migrate.**
Records resolve by room id, not by name, so an existing DM watcher keeps
serving its room under the old name with its session and watermark intact — no
duplicate watcher, no orphan, nothing in `failed`.

What does break is anything that **types** the new handle. `schedule create
mm-e2e:dm:test_user` is rejected with "Watcher … not found in any connector"
while the record still holds the encoded name, because every operator verb is
name-addressed with no room-id fallback.

If you want the clean name, expire the old record and let the room's next
message recreate it:

```bash
coop expire 'mm-e2e:dm:%40test_user'   # quoted for legibility, not necessity
```

Accepted losses, the same ones the rest of this document takes: the session is
fresh (history handoff refetches recent messages) and the watermark resets
once. The durable-instructions prompt file is **not** left behind — `expire`
reclaims the room, and reclamation deletes that file (it is keyed by the room,
so no rename can orphan it).

Leaving the encoded name in place is also a legitimate choice. It is ugly in
`list` and awkward to type, and that is the whole of the cost.

## `online_notification` / `offline_notification` are removed

The fields are gone from rules and templates — a config still carrying them
fails at load with an unknown-key error. The platform's own presence
indicators are the replacement (they were always the better signal), and the
idle/expire lifecycle means a watcher now starts and stops many times over
its life; announcing each transition was noise, not status.
