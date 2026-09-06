# Config TUI

`config.yaml` is plain YAML and can always be hand-edited, but the config
TUI gives you a full-screen, keyboard-driven editor with validation,
provenance tracking (see at a glance whether a value is explicit, inherited
from a template, or a built-in default), and safe writes — every save is
validated against a temp file before it ever touches your real
`config.yaml`, with an automatic timestamped backup.

## Launching

```bash
agent-chat-gateway config
```

```bash
# Edit a config file other than the default (~/.agent-chat-gateway/config.yaml
# or $ACG_CONFIG)
agent-chat-gateway config --config /path/to/config.yaml

# Also flag values that just restate a built-in default, or duplicate a
# value already inherited from a template — useful for cleaning up a config
# that's grown noisy over time
agent-chat-gateway config --lint
```

Two related, non-interactive commands live under the same `config`
subcommand:

```bash
# Validate config.yaml without starting the daemon or opening the TUI
agent-chat-gateway config validate [--lint]

# One-time: fold .env secrets into config.yaml as literal values, then
# remove .env (also runs automatically the next time you start the daemon
# or open the TUI, if it detects a .env-backed config — this lets you do it
# as a manual step or a dry run instead)
agent-chat-gateway config migrate-env
```

## Layout

Five tabs across the top: **Connectors**, **Agents**, **Watcher Rules**,
**Templates**, **Tool Presets**. A banner above them shows the config's
current validation status (`✓ valid` or `✗ N error(s)`, plus warning/lint
counts) — press `v` when it says "press 'v' to view details" to see the
actual messages, not just a count.

Every list is sorted by name — except **Watcher Rules**, which always shows your
`watcher_rules:` rules in file order, because that order is meaningful: the
first rule that matches a room wins. A newly created entry on the other
tabs is always easy to find regardless of where it landed in the
underlying file.

## Keybindings

On the list (Overview) screen:

| Key | Action |
|---|---|
| `↑`/`↓` or click | Move the cursor in the current tab's list |
| `←`/`→` | Switch tabs |
| `Enter` | View the selected entry (read-only) |
| `e` | Edit the selected entry directly (skips the view-only step) |
| `d` | Delete the selected entry |
| `n` | Create a new entry on the current tab |
| `[` / `]` | *(Watcher Rules tab only)* Move the selected rule up / down — rule order decides which rule claims a room, see [Watcher Rules](#watcher-rules) below |
| `r` | Refresh from disk (picks up changes made outside the TUI) |
| `v` | View the full text of any validation errors/warnings/lint findings |
| `ctrl+e` | Open `$EDITOR` on the whole `config.yaml` file, then reload when you exit |
| `q` or `Escape` | Quit (prompts to discard if there are unsaved changes) |

Inside an edit/create form:

| Key | Action |
|---|---|
| `Tab` | Move to the next field |
| `ctrl+s` | Save |
| `ctrl+r` | Reset the focused field back to its inherited/default value |
| `ctrl+t` | Show/hide the focused field's value, if it's a masked secret |
| `Escape` | Go back (prompts to discard if there are unsaved changes) |

## Editing an entry

A field labeled with a trailing `*` (e.g. `Working directory *`) is
required — save will refuse to go through without it. Required fields are
always listed first in the form.

Every field shows its current *provenance* next to it: `(explicit)` if this
entry sets it directly, `(from '<template>')` if it comes from an
`inherits:` template, or `(default)` if nothing sets it and it's just the
built-in fallback. Clearing an explicit value back to blank (or `ctrl+r`)
reverts it to inherited/default — it does **not** write an explicit `null`.

**A nested field answers for itself.** `rooms:`, `server:`, `permissions:`
and `history_handoff:` are merged sub-key by sub-key, so a rule that sets
only `rooms.direct` over a template's `rooms.include` shows the flag as
`(explicit)` and the include as `(from '<template>')` — one block, different
labels. `ctrl+r` on one sub-key leaves its siblings alone.

Nothing is written to `config.yaml` until you press `ctrl+s`. Save
validates the whole file first; if your change would introduce a **new**
problem, it's rejected with the exact error message and nothing is written.
A problem that already existed elsewhere in the file before your edit never
blocks an unrelated save.

## Connectors

Four types: `rocketchat`, `mattermost`, `voice`, `script`. **`voice` and
`script` are experimental** — see
[docs/supported-features.md](supported-features.md#voice-gateway-experimental-)
for `voice`'s known limitations, and
[docs/scheduling.md](scheduling.md#headless-scheduling-no-chat-platform)
for `script`'s actual use case (a headless identity for scheduled jobs, not
a chat platform). A connector's type is fixed once created — to change it,
use `ctrl+e` to hand-edit the file.

Mattermost's authentication is dual-mode (a Personal Access Token, or a
username+password login) — pick which one via the "Auth method" dropdown;
the inactive group's fields are hidden so the two modes can't collide.

## Agents

Two types: `claude`, `opencode` — also fixed once created. `Owner allowed
tools`/`Guest allowed tools` each have their own list editor below the main
fields: **+ Add** to append a rule (either a reference to a named tool
preset, or a one-off inline rule), **- Remove** to delete the selected one,
and **Edit** to change a selected *inline* rule in place (grayed out until
you select an inline rule — a preset reference isn't editable here; remove
it and add a different preset reference instead).

## Watcher Rules

Each `watcher_rules:` entry is a *watcher rule*: a required unique name, a connector, an
agent, and a `rooms:` matcher (`include`/`except_for` glob patterns over
room names, plus `direct`/`group_direct` opt-ins for the two kinds of DMs).
Which rooms a rule actually claims is decided at runtime, per incoming
message — see [docs/user-guide.md](user-guide.md) for how matching works.

- The tab lists rules **in file order**, numbered — the first rule that
  matches a room wins, so the order you see is the order that routes.
  `[`/`]` move the selected rule up/down and save immediately.
- The edit form covers every rule field, including the two session
  lifecycle TTLs (`session_idle_days`/`session_expire_days`). A rule needs
  at least one `include` pattern or one of the DM opt-ins — a rule that
  can never match anything is refused.
- **Renaming** a rule is allowed (nothing in `config.yaml` references a
  rule by name), but to the daemon it's a delete-plus-create: existing
  sessions stay attributed to the old name and — because a session is
  sticky-bound to the settings it was created with — keep running with the
  *old* rule's settings; edits to the renamed rule never reach them. Idle
  ones age out through their (frozen) TTLs; to move a busy room onto the
  new rule now, `agent-chat-gateway expire` it — the next message in that room then
  rematches against the current rules and builds a fresh watcher.
  (`agent-chat-gateway reset` is *not* the lever here: it clears the session but rebuilds the
  watcher from the same persisted record, so the room stays on the old
  rule's frozen settings.)
- **Deleting** a rule warns you with what it strands: how many persisted
  session records on disk still belong to it, and how many scheduled jobs
  target those sessions. The counts are read from the daemon's own files.
  Idle stranded sessions are cleaned up by the daemon's lifecycle sweeps. The
  scheduled jobs are **not** deleted with the rule, and **deleting the rule does
  not stop them**. A room that still has a record is recreated from that record,
  not from the rules, and each fire counts as activity that keeps the record
  alive — so a job firing more often than `session_idle_days` (15) runs
  indefinitely, while still listing as active. Only a job whose interval exceeds
  that lets the room go idle and then expire, and stop. To stop one now, remove
  the job with `agent-chat-gateway schedule delete <job_id>`, or expire the watcher. See
  `docs/scheduling.md` for the full rules.
- This tab edits `config.yaml` only. To see or act on the *live* sessions a
  rule has created, use the CLI: `agent-chat-gateway list`, `agent-chat-gateway pause/resume/reset/expire`.
- Known limitation: while some rule in the file is broken (its row shows
  ERROR), moving or deleting any row *above* it is refused, quoting that
  broken rule's own error — a broken rule's error message is
  position-dependent, so shifting its position reads to the save safety-gate
  as a new problem. Two practical consequences: repair a file with several
  broken rows **bottom-up** (the lowest ERROR row first), and if that's
  awkward, use `ctrl+e` to fix them all in one `$EDITOR` pass. Everything
  else — editing rules in place, creating new ones, deleting a row with no
  ERROR row below it — is unaffected.

## Templates

`agent_templates`, `connector_templates`, and `watcher_templates` are named,
reusable field sets — any entry can opt in via its own `inherits: <name>`
field. The Templates tab lists all of them, across all three kinds, in one
flat list. The kind is shown — and offered when you press `n` — under its
display name, so `watcher_templates` appears as **watcher rule**.

A watcher-rule template's form includes the `rooms:` matcher fields, so a
policy every rule should carry can be written once. Sharing a matcher has a
few rules of its own — a rule's list replaces the template's rather than
extending it, and a shared `direct: true` leaves every rule but the first with
no DMs — see [Templates and `rooms` inheritance](user-guide.md#templates-and-rooms-inheritance).
On the Watcher Rules tab, the Rooms column shows each rule's **effective**
matcher, inherited parts included.

The same form also offers `connector` and `agent`, so a template can carry
them and a rule that inherits it needs neither on itself — the loader
resolves both from the template, and the rule's form shows them as
`(from '<template>')`. Leave either blank on the template to not set it.

Editing a field on a template shows a confirm dialog naming every entry
that would be affected (scoped to entries that actually inherit *this*
template and don't already override the field) before saving — so you know
the blast radius up front. Deleting a template still in use by some entry
is blocked with the same kind of message.

## Tool Presets

Named, reusable rule lists (`tool_presets:`) that an agent's
`owner_allowed_tools`/`guest_allowed_tools` can reference by name instead of
repeating the same rules on every agent. Deleting a preset still referenced
by an agent is blocked.

## Saving Does Not Reload

Saving writes `config.yaml` and validates it; the running daemon keeps the
configuration it loaded. Apply the change with `agent-chat-gateway config
reload` (`--dry-run` first to see what it will do), or `restart`. `status`
and `config show` tell you whether the file and the daemon agree.

## Escape Hatch

`ctrl+e` (from the list screen) opens `$EDITOR` on the raw `config.yaml`
file for anything the forms don't cover — an unusual connector `raw` key, a
rare field this TUI doesn't expose, or a bulk find-and-replace. The TUI
reloads and re-validates the file as soon as you exit the editor.
