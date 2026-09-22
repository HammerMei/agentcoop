# coop-keeper

coop-keeper is the admin agent that ships with AgentCoop. An operator runs
their own `claude` or `opencode` CLI inside the keeper's directory and asks,
in plain language, for a bot to be created, changed or removed; the keeper
turns that into `config.yaml` entries, chat-server accounts and a daemon in
the requested state. It replaces the `coop onboard` wizard. This document
records the requirements and the design decisions; it is the definition the
implementing pull requests are measured against.

## 1. Motivation

Setting up an AgentCoop deployment today means creating a chat-server
account by hand, writing three related `config.yaml` entries in the right
relationship to each other, adding the account to the right rooms, and
restarting the daemon. The `coop onboard` wizard covered only the first
Rocket.Chat bot and nothing after it; every later bot, and every Mattermost
bot, was hand-written. The config TUI made the file easier to edit but still
required the operator to know which entries a bot needs.

The wizard's approach — a fixed sequence of questions — does not extend to
"add a second bot", "put Bob on Rocket.Chat as well" or "make Alice see only
`agent-room`". A conversational agent does, and the pieces it needs already
exist: `coop-provision` creates accounts and channel memberships,
`coop config validate`/`reload` have machine-readable output that was added
for exactly this purpose (config-reload design §1), and the two supported
agent CLIs both read an instruction file and a `SKILL.md` directory from the
current working directory.

### 1.1 Requirements

- One conversational flow covers first-time setup and every later change.
- Works identically under Claude Code and OpenCode, from one set of shipped
  files.
- Supports Mattermost and Rocket.Chat, and an agent present on both at once.
- Never asks for, accepts, or displays a credential in the conversation.
- Every change to a running system is shown as a plan and confirmed once
  before anything is executed.
- Works on a `config.yaml` the operator wrote by hand, not only on files the
  keeper produced.

## 2. Vocabulary

Three terms are added to `CONTEXT.md` and used throughout:

- **agent** — one `agents:` entry: a backend process (`claude` or
  `opencode`), a working directory and a persona. One agent can be present on
  several chat servers.
- **bot** — an agent's presence on one chat server: the connector (the
  server account), the agent, and the watcher rule that binds them. An agent
  has at most one bot per server.
- **profile** — one server's administrative credentials in
  `admin-profiles.yaml`, used by `coop-provision` to create accounts and
  memberships. Named after the server host (`mm-labpig`, `rc-labpig`) unless
  the operator renames it; `coop-provision init` accepts the same single
  path component as an agent name (§3.5), and a hand-written profile whose
  name falls outside it is usable but not for naming — the keeper asks for
  the connector name instead of deriving one.

An operator saying "create an agent named Bob on Mattermost" is asking for an
agent and one bot. The keeper's instructions state this translation.

## 3. Design

### 3.1 Installation and layout

`install.sh` installs files and nothing else: `uv`/Python, the `coop` and
`coop-provision` links, PATH, `contexts/`, `install_meta.json`, and the
keeper directory. Both entry modes stay — `curl | bash`, which clones to
`~/.agentcoop/repo`, and running the script from a checkout. The
`--no-onboard` flag, the wizard call and the `.env` instructions are removed.
The script ends by printing the command that starts the keeper:

```
cd ~/.agentcoop/agents/builtin/coop-keeper && claude     # or: opencode
```

Runtime layout:

```
~/.agentcoop/
  config.yaml                       # 0600
  admin-profiles.yaml               # 0600; the new default path for coop-provision
  agents/
    builtin/coop-keeper/            # shipped; overwritten on upgrade
      AGENTS.md                     # the keeper's instructions, in full
      CLAUDE.md                     # one line: @AGENTS.md
      .claude/skills/<name>/SKILL.md
      .claude/settings.json         # Claude Code permission rules (§3.9)
    user/<agent>/                   # one per operator-created agent
      CLAUDE.md                     # the persona
```

In the repository the shipped files live at `agents/coop-keeper/`, leaving
room for further built-in agents beside it. `install.sh` copies the directory
into `builtin/`; `coop upgrade` replaces the whole `builtin/coop-keeper/`
tree with the shipped one, preserving only `.claude/settings.local.json` —
the one file a CLI writes into the directory (session state lives under
`~/.claude/projects/` and `~/.local/share/opencode/`, not here). Replacing
rather than overwriting means a skill or instruction file a release removes
is gone after the upgrade instead of loading beside its replacement. The `builtin/`
directory exists so that an operator can see which agents are the system's;
an operator who wants a customised keeper copies it under `user/`. Unlike
`contexts/`, there is no per-file "locally modified" check: protecting local
edits would contradict the directory's meaning.

The instruction-file pairing follows from how the two CLIs load files.
Claude Code reads only `CLAUDE.md` and expands `@file` imports; OpenCode reads
`AGENTS.md`, ignores `CLAUDE.md` when both exist, and does not expand
imports. `AGENTS.md` therefore carries the content and `CLAUDE.md` is a
one-line import. OpenCode also loads `.claude/skills/<name>/SKILL.md`, so one
skills directory serves both. Skill frontmatter uses only the fields both
CLIs recognise (`name`, `description`).

An agent's persona uses the same pairing in its working directory: the text
in `AGENTS.md`, and a `CLAUDE.md` holding `@AGENTS.md`. Relying on OpenCode's
`CLAUDE.md` fallback instead would break the moment the directory acquired an
`AGENTS.md` from anywhere else — OpenCode would then ignore the persona and
every later edit to it, silently. The keeper writes and rewrites `AGENTS.md`;
it creates `CLAUDE.md` once and never overwrites one that is already there.

### 3.2 Bootstrap

At the start of every session the keeper establishes the machine's state
with four commands: `coop-provision profiles --json` (the servers it can
administer, credentials masked), `coop config show --raw --json` (the file
as written, credentials masked — §3.10), `coop status`, and `coop config
backends --json`.
There is no aggregate "doctor" command; one is added only if these four
prove insufficient in use.

When `profiles --json` reports no profiles the keeper asks which platform, the
server URL, the team (Mattermost) and the operator's own username on that
server, then runs `coop-provision init <profile> --type … --server-url …
[--team …]`, which writes the profile with its credential fields empty, and
asks the operator to fill them in an editor. Once the operator says so, it
confirms the profile works with `coop-provision <profile> check`, a
read-only call that authenticates and resolves the team. The keeper never
opens the file itself — the same tool that reads the credentials is the one
that writes their skeleton — and never sees the credential.

The first plan that creates a bot also creates, if absent, the `tool_presets`
the agent template refers to and one `default` entry in each of
`connector_templates`, `agent_templates` and `watcher_templates`, as the first
edit of that plan. They are not written at session start: nothing is written
before a confirmed plan (§3.8), and on a hand-written file the atomic save
would strip comments the operator had not agreed to lose. A `config.yaml`
holding only these — no connector, agent or rule — is a valid *empty
deployment* (§3.10). These hold the settings the
operator wants applied to every bot; the keeper never overwrites them
afterwards, and an operator asking for "all bots" to change is asking for a
change to a template. The templates carry only fields that are the same on
every server and every backend:

| Template | Carries | Never carries |
|---|---|---|
| `connector_templates.default` | attachments, `reply_in_thread`, `filter_sender: false`, `agent_chain` (§3.7) | `type`, `server`, `allowed_users` |
| `agent_templates.default` | `permissions`, `timeout`, tool allow-lists (by preset name) | `type`, `command`, `working_directory` |
| `watcher_templates.default` | session TTLs | `rooms` — a `direct: true` in a template reaches only the first rule that inherits it |

A hand-written `config.yaml` that already has templates under other names is
left alone; keeper-created entries still inherit `default`, which the keeper
adds.

The operator's username is not stored anywhere of its own. It is reused from
the connectors of the same server (§3.4) only when they agree on exactly one
owner; when the server has no connector yet, or its connectors list no owner
or several, the keeper asks.

### 3.3 Credentials

The keeper's instructions state that it never asks for, accepts or repeats a
password or token. Administrative credentials are typed by the operator into
`admin-profiles.yaml`. Bot passwords are generated by the keeper into a temporary file created by
`mktemp` (mode `0600` regardless of umask; `openssl rand -out` would honour
the umask and produce `0644`) with `openssl rand -hex 24 > <file>`, and passed
to both CLIs by path —
`coop-provision … create-user --password-file` and `coop config add
connector --password-file` — and deleted once the plan has completed. It is
kept, still `0600`, when the plan fails between those two steps: the account
then exists with that password, and a retry that generated a fresh one would
be told by `create-user` that the user already exists and save a connector
that cannot log in (rotation is out of scope, §5). The failure report names
the file, and a resumed plan reuses it. There is no rollback of the created
account — the report says what exists, and the operator decides. The value
never appears in the conversation, and therefore never in the CLI's session
transcript or the shell history.

This is an instruction-level guarantee, backed by two mechanical measures:
the shipped `.claude/settings.json` denies the `Read` and `Edit` tools on
`config.yaml`, `admin-profiles.yaml` and `.config-backups/**`, and the
keeper reads configuration only through `coop config show --json` and
`--raw --json`, whose output is secret-masked (§3.10). The deny rules do not govern the shell: a `cat` of
either file is an unlisted command, which on Claude Code prompts the operator
and on OpenCode — whose defaults allow everything — runs. That residual risk
is documented rather than engineered away in this version.

### 3.4 Scoping a step to a server or to an agent

Several steps below act on "the other bots on this server" or "every bot of
this agent". Neither is read from names: a hand-written `config.yaml` may
name its connectors and rules however it likes. Both are derived from the
resolved configuration (`coop config show --json`):

- **The connectors of a server** are those whose `server.url` matches the
  profile's after canonicalisation — scheme and host lower-cased, trailing
  slash dropped, as the connector parsers already do with `rstrip("/")` — and,
  for Mattermost, whose `server.team` matches too. Room discovery and owner
  inference are scoped this way. A Mattermost **installation** — the URL
  alone — is the wider unit that accounts live in: a username is one account
  across every team of that URL, so anything about accounts (creating,
  deleting, the agent-chain list) is scoped to the installation, not the team.
- **The connectors of an agent** are those named by a rule whose `agent` is
  that agent. Persona resets and last-bot detection are scoped this way.

The `<agent>@<profile>` naming convention is for entries the keeper creates;
nothing relies on it when reading.

**Shared things are not changed behind the operator's back.** Before a plan
removes or rewrites anything, the keeper counts its other users in the
resolved configuration: rules naming a connector or an agent, agents sharing a
`working_directory`, connectors of one installation sharing a username. When
something is still used elsewhere, the plan is not silently narrowed — the
keeper stops, names what is shared and by whom, and offers the wider plan
that removes the dependents too. The operator either takes that plan, which
is then confirmed and executed like any other, or keeps the shared part. The
config CLI enforces the first two kinds itself (`remove` refuses an entry
that is still referenced, §3.10); the last two are the keeper's to check,
because neither the file nor `coop-provision` knows about them.

### 3.5 Creating a bot

Inputs: the agent name; the platform (a profile — the keeper offers to add a
server when none matches); and, only when the agent does not exist yet, the
backend — chosen from `coop config backends --json` every time, since one
deployment may mix backends — and the persona. Adding an existing agent to a
further server reuses its `agents:` entry, working directory and persona
unchanged, and creates only the connector and the rule; the keeper says so
in the plan rather than asking for a persona it will not use.

The persona is written to `~/.agentcoop/agents/user/<agent>/CLAUDE.md`. Its
content is determined by what the operator said: text supplied verbatim is
written verbatim; an intent ("a professional lawyer, I have a car-insurance
question") is drafted by the keeper within that intent, adding nothing the
operator did not ask for. Either way the text is shown in the plan before it
is written. An operator who declines to give one gets a one-line stub so the
file exists to be edited later. The persona belongs to the agent, not the
bot: an agent on two servers has one persona.

Defaults, each overridable by the operator's wording:

| Field | Default |
|---|---|
| server username | the agent name; when a connector of the same installation already uses it (an agent's second Mattermost team), no account is created — the new connector takes its credentials from that one with `--credentials-from`, and the keeper says so in the plan |
| email | `<username>@agentcoop.invalid` (a reserved TLD; both platforms check syntax only) |
| `rooms` | `{include: ["*"], direct: true}` |
| `filter_sender` | `false` — anyone in a room the bot is in may talk to it; roles still apply |
| `allowed_users.owners` | the operator's username on that server |
| `allowed_users.guests` | `[]` |
| `inherits` | `default`, on all three entries |
| `description` | `managed by coop-keeper` plus the operator's stated intent, on all three entries |

Room membership is separate from the rule: a rule with `include: ["*"]`
serves any room the account is in, and joining is done with `coop-provision
add-to-channel`. The keeper adds the new account to every room a rule of another connector
**of the same server** (§3.4) names literally; rules of other servers say
nothing about this one, even when a channel name happens to repeat. A glob in
an existing rule cannot be
expanded (there is no `list-channels`, and glob expansion is deliberately not
added to `coop-provision`), so the keeper asks which rooms it stands for.
When that discovery yields nothing — the first bot on a server has no
earlier rules to learn from — the keeper asks the operator which rooms the
bot should join, and accepts "none" as an answer that leaves the bot reachable
by direct message only. The plan lists every `add-to-channel` it will run.

Ordering: the agent's directory and persona file first, because
`working_directory` must exist when the file is validated
(`gateway/config.py`, `_parse_one_agent`); then connector, agent and rule,
each through `coop config add`, which validates the whole file after every
write. The ordering is sufficient from the very first bot because an empty
deployment is valid (§3.10): a file with one connector and nothing else passes,
so no step needs to skip validation or bundle several entries.

An agent name becomes a directory name under `agents/user/`, so `coop
config add agent` and the keeper both accept only a single path component:
`^[a-z0-9][a-z0-9_-]{0,63}$`. Anything else — a `/`, `..`, an absolute path,
upper case — is refused before any file is created, and the keeper checks
that the resolved directory is under `agents/user/` before writing to it. A
name that already exists is refused by `coop config add`; the keeper
reports the conflict and asks for another name. Connector and rule names are
generated by convention — `<agent>@<profile>`, so `bob@mm-labpig` — and the
operator is not expected to know them. `@` is permitted in connector names;
`:` and glob characters are not, and the convention avoids both.

### 3.6 Changing and removing a bot

A room change is a `coop config patch` on the rule, plus
`coop-provision add-to-channel` for every literal room the change adds that
the account is not yet in, followed by the apply step (§3.8). Membership in a
room the change drops is left as it is: the rule no longer serves it, and
leaving a room is a visible act in chat that the operator can do deliberately. A persona change rewrites the agent's `CLAUDE.md` and changes
nothing in `config.yaml`, so `reload` has nothing to apply. On Claude Code
the gateway launches a fresh `claude` process for every turn with
`--system-prompt-snapshot off` (`gateway/agents/claude/adapter.py`), so the
rewritten file is read on the bot's next turn without further action. Whether
an OpenCode session re-reads its instruction file mid-session is not
established; the plan for a persona change therefore offers
`coop reset '<connector>:*'` for each connector of the agent (§3.4) — all of
them share the one persona file, and a fresh session does read it — as a
confirmed runtime step, and states that on Claude Code it is optional.
For an agent whose working directory is outside `agents/user/`, the keeper
reports that it does not manage that agent's persona file and leaves the
operator to edit it. A working directory shared by more than one agent is a
shared thing (§3.4): the plan names every agent the edit would reach, and the
operator decides whether that is what they meant.

Removing a bot is confirmed once and then done in full, with every step
subject to the shared-things rule (§3.4): the account's username is removed
from the agent-chain list (§3.7) unless another surviving bot still uses it —
the default of naming accounts after the agent makes that the ordinary case
for an agent on two servers; the rule is removed, and the connector with it
unless another rule still references that connector; the agent is removed
when no rule names it any more; all in one edit. The daemon is reloaded once,
so the surviving connectors pick up the shortened chain list in the same
apply and reconciliation expires the records no rule covers. The server
account is deleted with `coop-provision delete-user` only when no surviving
connector of the same installation uses that username — on Mattermost a
delete deactivates the account for every team at once. Removing the
deployment's last bot leaves a valid empty deployment — presets and templates
only — which `reload` accepts (it stops every connector and expires every
record, so no state file or session outlives the bot) and `start` refuses
(§3.10); the plan therefore reloads, then stops the daemon, and says the
deployment is now empty. Only when the agent itself was removed — its last bot — is its
directory under `agents/user/` deleted, and only after checking that no
surviving agent's resolved `working_directory` is that directory, inside it,
or a parent of it (a hand-written configuration may share directories; the
keeper does not assume otherwise). When the check fails the directory is
kept and the report says which agent still uses it. An agent that still has
a bot on another server keeps its working directory and persona, and a
directory anywhere outside `agents/user/` is never touched.

### 3.7 Agent-to-agent chains

With the defaults above, the second bot on a server would see the first
bot's messages as ordinary user messages and answer them, and vice versa,
with no loop protection — `agent_chain` disables that protection when
omitted. The keeper therefore maintains a single list of every bot username
in `connector_templates.default.agent_chain.agent_usernames`, appending on
creation and removing on deletion. Own messages are dropped by each
connector before this list is consulted, so a bot's own name in the shared
list is harmless. `max_turns` and `ttl_seconds` keep their defaults.

The list is shared across servers. A human on one server whose username
equals a bot's username on another would be treated as an agent there
(bypassing the sender allow-list, subject to the turn budget). This is
accepted for this version and documented. Whether a connector actually
receives the shared list is decided from its resolved `agent_usernames` in
`coop config show --json`, not from its `inherits:` value — an entry-level
list replaces the template's wholesale, so a hand-written connector that
inherits `default` and also sets its own list is as unaffected as one that
inherits nothing. Every connector of the server whose resolved list lacks the
new username is patched individually, and the plan says so. Removal is the
mirror image: every connector whose resolved list still carries a username no
surviving bot uses is patched to drop it — the shared template and any
entry-level override alike — so a deleted account cannot keep bypassing the
sender allow-list through a list the keeper once added it to.

### 3.8 The plan is the unit of confirmation

The keeper expands each request into one plan — accounts to create or
delete, rooms to join, the persona text, the configuration entries, and the
runtime effect (`reload`, `start`) — prints it, and asks once. **Nothing is
written before the yes**: the plan is built from the resolved configuration
and from `coop config add|remove|patch --dry-run --json`, which validate the
merged result and report it without touching the file, so a declined plan
leaves no edit on disk waiting for a later `start` to apply it. On yes the
keeper executes the whole plan without further questions; on a failure it
stops and reports what was completed. Steps that only read state need no
confirmation. A persona write does: on Claude Code the bot reads the file on
its next turn (§3.6), so a persona-only request is its own plan.

The apply step, after the yes, is the write → `coop config reload --dry-run`
→ `coop config reload` (or `coop start` when the daemon is not running). The
dry run here is a guard, not the preview: it is the daemon's own account of
what the confirmed edit will do, and if it names anything the plan did not —
a connector restart the operator was not told about — the keeper stops and
shows it before reloading.

The keeper assumes it is the only session editing `config.yaml` at a time.
Two keeper sessions, or a keeper and the config TUI, writing concurrently
can corrupt the file; the user guide says so.

### 3.9 Permissions for the keeper itself

OpenCode allows every operation by default; nothing is shipped for it.
Claude Code's `auto` permission mode cannot be selected from a project-local
settings file, so the shipped `.claude/settings.json` uses explicit rules
instead: allow `Bash(coop *)`, `Bash(coop-provision *)`, the handful of
shell commands the skills run — `mkdir` for an agent's directory, `mktemp`,
`openssl rand` and `rm` for the password file, `which` for the
session-start checks — and the `Read`/`Write`/`Edit` tools under `~/.agentcoop/agents/`;
deny the credential files named in §3.3. Everything else prompts. The allow
list is derived from what the four skills actually execute, so a skill that
gains a new command adds it here in the same change. The permission file is the one place where a provider-specific file
is shipped; `AGENTS.md` refers to no provider-specific feature.

### 3.10 Command surface

Added to `coop config`, each with `--json`:

```
config add connector <name> --type … --server-url … [--team …] --username … --password-file … --owner … [--inherits …]
config add agent     <name> --type … --command … --working-directory … [--inherits …]
config add rule      <name> --connector … --agent … [--include …]… [--direct] [--inherits …]
config remove connector|agent|rule <name>
config patch [--set <path>=<value>]… [--unset <path>]… [--file <fragment.yaml>]
config show --raw
config backends
```

**Empty deployment.** `GatewayConfig` currently requires at least one
connector and one agent. That requirement moves from the file to the daemon:
a `config.yaml` with zero connectors, agents and rules is valid — it is the
state between installing and the first bot, and after removing the last —
and `coop start` refuses to run a deployment with no watcher rules, with a
message that says so. `coop config reload` accepts one: reloading to an empty
deployment stops every connector and expires every record, which is how the
last bot's state is cleaned up before the daemon is stopped (§3.6).
Validation of references (a rule naming an absent connector) is unchanged.

**Masked, not missing.** `config show --raw --json` returns the file as
written — templates, `inherits:`, `description`, key order — with every value
under a password, token or secret key replaced by the sentinel `***`
(`gateway/config_diff.py`, `REDACTED`), so a masked field is visibly present
and distinguishable from one that was never set. The resolved view (`config
show --json`) already uses the same sentinel. Because a masked document must
never be written back, `config add` and `config patch` refuse any value equal
to the sentinel, whether it arrives through `--set` or inside `--file`; the
keeper edits by path and never round-trips a whole document.

`add`, `remove` and `patch` take `--dry-run`: the merged result is validated
and returned (with `--json`, the resulting entries and any findings) and the
file is not touched — the keeper builds its plan from this. Every JSON these
commands emit, dry run or not, passes through the same redaction as `config
show`: a connector result carries `***` where its password is, never the
value read from `--password-file`. `add connector` also takes
`--credentials-from <connector>`, which copies `server.username` and the
password or token from an existing connector inside the command, for a second
Mattermost team on an installation where the agent already has an account. Without it, `add`
writes one entry and validates the whole file; an invalid result is
not written and the findings are returned in the same format as
`config validate --json`. An existing name is refused rather than replaced.
`add agent` also checks that `command` resolves on PATH and refuses when it
does not — a machine-specific check that belongs at the moment of adding,
not in `config validate`, which runs in CI and containers where the backend
is legitimately absent. `remove` refuses while other entries still refer to
the name; removal order is the operator's (the keeper's) responsibility.
`patch` follows JSON merge-patch semantics: `null` deletes, `--set` values
are parsed as YAML so `500` is an integer and `"500"` a string, and list
entries are addressed by name (`connectors[name=bob@mm-labpig].reply_in_thread`).
`backends` reports each supported backend type with its command and whether
it was found, using the same lookup `add agent` uses.

All writes go through the config TUI's existing atomic save, which takes a
timestamped backup under `.config-backups/` first. Comments in `config.yaml`
are not preserved by any of these — the TUI has never preserved them, and
the `description` field is the preservable comment; the user guide and
`config.example.yaml` now say so.

`coop-provision` gains four things: `--password-file` on `create-user`;
`init <profile> --type … --server-url … [--team …]`, which writes a profile
with empty credential fields and refuses to touch a profile that already
exists; `<profile> check`, which runs the existing `connect()` — an
authenticated `get_me` plus team resolution — and exits non-zero on failure;
and `profiles --json`, which lists every profile's name, type, server URL and
team with credential fields present but masked with the same `***`
sentinel, so the keeper can tell an unfilled skeleton (field empty) from a
filled profile (field masked). The default location
of `admin-profiles.yaml` moves from the current directory to
`~/.agentcoop/admin-profiles.yaml`; `--config` and `COOP_ADMIN_CONFIG` still
override it. The default `--log-file` moves the same way, to
`~/.agentcoop/coop-provision.log`: the keeper runs the command from
`builtin/coop-keeper/`, which an upgrade replaces wholesale (§3.1), and the
full API error log is exactly what an operator needs after a failed plan.

### 3.11 Skills

Four skills, for the flows that have order and consequences: `bootstrap`,
`add-bot`, `remove-bot`, `apply-config`. Single-step operations — a persona
edit, a room change — are described in `AGENTS.md` and end in `apply-config`.
Putting an existing agent on a second server is `add-bot` with an existing
agent, not a separate skill. `AGENTS.md` itself is kept short in this version
— the keeper's role, the vocabulary translation, the credential rule, the
session-start checks and the plan rule — and grows from what testing shows it
needs.

### 3.12 Removed

`gateway/onboard.py`, the `coop onboard` command, `make onboard`,
`tests/unit/test_onboard.py` and `detect_agent_backends()` are deleted; the
backend lookup moves into `coop config backends`/`add agent`. The `.env`
migration (`coop config migrate-env` and the automatic migration on `coop
start`) is deleted; the v0 → v1 upgrade is a reinstall and the population
still carrying a `.env` is effectively zero. `docs/install-agent.md`, which
walked an AI agent through the same setup by hand, is deleted in favour of
the keeper. No tombstone or alias is left for `coop onboard`.

## 4. What it does and does not guarantee

- Every `config.yaml` written by the keeper or its commands passes
  `coop config validate` at the moment it is written.
- Nothing running changes without a plan having been shown and confirmed.
- Credentials never pass through the keeper by instruction. Their files are
  denied to its `Read`/`Edit` tools on Claude Code and a shell read prompts
  the operator; on OpenCode the instruction is the only barrier.
- Everything the keeper reads is masked with `***`, never omitted: a
  credential field is always visible as present, filled or not. No `coop` or
  `coop-provision` output — read or write, text or JSON — carries a secret.
- Nothing shared with another bot, agent or rule is removed or rewritten
  without the operator having seen what else it reaches.
- Comments in `config.yaml` are not preserved.
- Concurrent edits to `config.yaml` are not detected.
- Room membership is only as complete as the literal room names in existing
  rules; globs require the operator's answer.

## 5. Out of scope for this version

Rotating a bot's password or token; platform-specific account settings
beyond what `create-user` sets; Mattermost bot accounts (token
authentication) — accounts are regular users with a password; changes to the
config TUI; comment-preserving YAML; a `coop doctor` command; Claude Code
`auto` mode; cascading `remove`; glob expansion in `coop-provision`; any
keeper-specific state file; automated testing of the keeper's conversational
behaviour.

## 6. Delivery

This document and the `CONTEXT.md` glossary entries land first, on their
own. Then two pull requests, in order:

1. **Command surface** — §3.10 in full: `coop config add/remove/patch/backends`
   and `show --raw`, `--dry-run` and redacted JSON on the write commands,
   `--credentials-from`, the empty-deployment validation change, the `***`
   write-back refusal, `coop-provision init`/`check`/`profiles`,
   `--password-file`, and the `admin-profiles.yaml` and log default paths. Independently
   testable and useful without the keeper.
2. **The keeper** — `agents/coop-keeper/` with `AGENTS.md`, `CLAUDE.md`,
   the four skills and `.claude/settings.json`; `install.sh` and
   `coop upgrade` changes; the removals in §3.12; the documentation
   changes.

## 7. Testing

The command surface is unit-tested like the rest of `gateway/cli.py`. The
keeper's behaviour is verified by running the following against the lab
Mattermost and Rocket.Chat servers and recording the outcome in the second
pull request:

1. Bootstrap on a machine with no `admin-profiles.yaml` and no `config.yaml`.
2. Create `bob` on Mattermost with the defaults.
3. Create `alice` on Rocket.Chat restricted to `agent-room`.
4. Change `bob`'s persona; confirm the next turn reflects it on Claude Code,
   and that the plan offered `coop reset` and a reset session reflects it.
5. Add `bob` to Rocket.Chat (second server; new profile sub-flow).
6. Remove `alice`: account, directory and configuration.
7. Add a bot to a hand-written `config.yaml` that uses `inherits`.
8. Refusals: a duplicate name, an agent name with a path separator, a backend
   that is not installed, a credential pasted into the conversation, a patch
   carrying the `***` sentinel, and — after the second bot on a server —
   `agent_usernames` containing both.
9. Decline a plan after it is shown: `config.yaml`, `admin-profiles.yaml`
   and the server are unchanged. Fail a plan between `create-user` and
   `config add connector`: the report names the kept password file, and a
   resumed plan completes with the account able to log in.
10. Add an agent to a second Mattermost team on the same installation: no
    account is created, the second connector logs in with the first's
    credentials, and removing one of the two bots leaves the account and the
    other connector working.
11. Remove a bot whose connector another rule still uses: the plan stops,
    names the rule, and completes only as the wider plan the operator chose.
12. Remove the last remaining bot: the daemon is stopped, `config.yaml` keeps
   its presets and templates and still validates, and `coop start` refuses
   with the empty-deployment message.
