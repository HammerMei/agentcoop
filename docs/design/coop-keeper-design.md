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
cd ~/.agentcoop/agents/builtin/coop-keeper && opencode     # or: claude
```

Runtime layout:

```
~/.agentcoop/
  config.yaml                       # 0600
  admin-profiles.yaml               # 0600; the new default path for coop-provision
  agents/
    builtin/coop-keeper/            # shipped; refreshed by manifest on upgrade
      AGENTS.md                     # the keeper's instructions, in full
      CLAUDE.md                     # one line: @AGENTS.md
      manifest.yaml                 # the paths AgentCoop owns here (below)
      .claude/skills/coop-<name>/SKILL.md
      .claude/settings.json         # Claude Code permission rules (§3.9)
      opencode.json                 # OpenCode permission rules (§3.9)
    user/<agent>/                   # one per operator-created agent
      AGENTS.md                     # the persona
      CLAUDE.md                     # one line: @AGENTS.md
      opencode.json                 # {"default_agent": "build"}   (created once)
      .claude/settings.json         # {"agent": ""}                (created once)
```

In the repository the shipped files live at `agents/coop-keeper/`, leaving
room for further built-in agents beside it. `install.sh` and `coop upgrade`
both bring `builtin/coop-keeper/` up to the shipped tree through one function
(`gateway/upgrade.py`, `sync_keeper_dir`), **by manifest**: `manifest.yaml`, shipped in the
directory, is one forever-growing list of the paths AgentCoop owns there,
each marked `in-use` or `obsolete`. An `in-use` file is overwritten with the
shipped one and an `in-use` directory is replaced as a unit, so a file a
release dropped from inside a skill does not linger; an `obsolete` path is
removed if present; **anything not listed is never touched**. Adding a
shipped file adds an `in-use` line; removing one flips its line to
`obsolete`, and the line stays, so an upgrade from any earlier release still
removes what that release shipped. Skill directories carry a `coop-` prefix
so an operator's own skill cannot collide with an owned path.

Replacing the whole tree while preserving a named list of files was
rejected: it requires knowing every file a CLI might write into the
directory, and it destroys what the keeper or the operator saved there; a
leftover file is the smaller harm. The `builtin/` directory exists so that an operator can see
which agents are the system's; an operator who wants a customised keeper
copies it under `user/`. Unlike `contexts/`, there is no per-file "locally
modified" check on the owned paths: protecting local edits to them would
contradict the directory's meaning. Symlinks are followed, as everywhere
else in the runtime directory: an operator may link the keeper directory or a
file in it wherever they like, and an owned path that is a link to a file of
their own is overwritten on upgrade — that is what the link asked for, not a
case the sync guards against.

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
server, shows the profile it is about to create as a plan of its own (§3.8),
and on yes runs `coop-provision init <profile> --type … --server-url …
[--team …]`, which writes the profile with its credential fields empty. It
then asks the operator to fill them in an editor and, once the operator says
so, confirms the profile works with `coop-provision <profile> check`, a
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
operator wants applied to every bot; the keeper does not rewrite them
afterwards — with one reserved exception, `connector_templates.default.
agent_chain.agent_usernames`, which the keeper maintains on every bot
creation and removal (§3.7) — and an operator asking for "all bots" to change
is asking for a change to a template. The templates carry only fields that are the same on
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
`mktemp` under `~/.agentcoop/agents/` (mode `0600` regardless of umask;
`openssl rand -out` would honour the umask and produce `0644`; under
`agents/` rather than `$TMPDIR` because that is the directory both CLIs'
permission files open to the keeper — deleting a file in `$TMPDIR` prompted
on OpenCode in the lab — and because a kept file should survive a reboot)
with `openssl rand -hex 24 > <file>`, and passed
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

This is an instruction-level guarantee, backed by mechanical measures that
are guardrails against accidental exposure, not a sandbox: the shipped
`.claude/settings.json` denies `Read` on `config.yaml`, `admin-profiles.yaml`, the generated `agents/.bot-password.*` files
and `.config-backups/**` (Claude Code applies a `Read` deny to its `Edit`,
`Write`, `Grep` and `Glob` tools and to the shell readers it recognises —
`cat`, `head`, `tail`, `sed`, redirections — when they name the file); the
shipped `opencode.json` denies the same three paths to OpenCode's `read` and
`edit` tools (as `*/.agentcoop/config.yaml` — those rules see a path relative
to the working directory); and the keeper reads configuration only through
`coop config show --json` and `--raw --json`, whose output is secret-masked
(§3.10). The residuals are known and documented rather than engineered away:
a shell command may reach the file. On Claude Code the Read tool, `cat`,
`head` and `cp` of `config.yaml` are all denied outright with the shipped
file; a `coop` subcommand the keeper never needs, `coop send --attach`,
would have uploaded it to a room under an allow-all `coop` rule, which is why
the allow list names the subcommands the skills run rather than `coop *`. On OpenCode a `cat` of a path outside the working
directory falls to `external_directory`'s default `ask` and prompts, while
`head`, `sed` or a variable expansion, which OpenCode infers no path from,
run and return the file — and on OpenCode v1 an "always allow" answer to any
read prompt overrides configured denies for that session. Closing these would
mean chasing a perfect rule set for an agent that runs on the operator's own
machine, with the operator's own access to the files; the rule set here is
deliberately the reasonable one.

### 3.4 Scoping a step to a server or to an agent

Several steps below act on "the other bots on this server" or "every bot of
this agent". Neither is read from names: a hand-written `config.yaml` may
name its connectors and rules however it likes. Both are derived from the
resolved configuration (`coop config show --json`):

- **The connectors of a server** are those whose `server.url` matches the
  profile's after canonicalisation — the runtime's `canonical_origin`
  (`gateway/core/bot_identity.py`): scheme and host lower-cased, default port
  and trailing slash and root dot dropped, IP literals canonical, path kept;
  the keeper's prose and `coop config add connector --credentials-from` both
  apply that rule — and,
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
in the plan rather than asking for a persona it will not use. An agent that
already has a bot on the target server (§3.4) is not given a second one —
that would be two accounts answering the same rooms — and the keeper offers
to change the existing bot instead.

The persona is written to `~/.agentcoop/agents/user/<agent>/AGENTS.md`,
with the one-line `CLAUDE.md` beside it (§3.1), and two files that pin the
bot's CLI to its built-in agent — `opencode.json` with `default_agent:
build`, `.claude/settings.json` with `agent: ""` — created once and never
overwritten. Without them a bot inherits whatever default agent the
operator's own CLI configuration names, and answers as that persona. Not a
common setup, but the safer default; the operator may edit the files. On
Claude Code the write of `.claude/settings.json` into the agent's directory
prompts even under the allow rule — the CLI guards `.claude/` directories
themselves — so it is the one step of a plan that asks the operator; a
refusal is reported and the plan goes on. A directory that already
exists at that path and is not empty is not adopted: the name is refused
with the reason, because creation would rewrite its `AGENTS.md` and a later
removal would delete it. Its content is determined by what the operator said: text supplied verbatim is
written verbatim; an intent ("a professional lawyer, I have a car-insurance
question") is drafted by the keeper within that intent, adding nothing the
operator did not ask for. Either way the text is shown in the plan before it
is written. An operator who declines to give one gets a one-line stub so the
file exists to be edited later. The persona belongs to the agent, not the
bot: an agent on two servers has one persona.

Defaults, each overridable by the operator's wording:

| Field | Default |
|---|---|
| server username | the agent name; when a connector of the same installation already uses it (compared case-insensitively — an agent's second Mattermost team), no account is created — the new connector takes its credentials from that one with `--credentials-from`, and the keeper says so in the plan. A deactivated account of that name (a Mattermost bot removed earlier) is revived with `coop-provision reactivate-user --password-file` and a new password: when the keeper knows of it while planning, the plan says so; when `create-user` is what reveals it, the plan stops and the revival is confirmed on its own, since it is not what the operator approved |
| email | `<username>@agentcoop.invalid` (a reserved TLD; both platforms check syntax only) |
| `rooms` | `{include: ["*"], direct: true}`; `{include: [], direct: true}` when the operator answers "none" to the rooms question |
| `filter_sender` | `false` — anyone in a room the bot is in may talk to it; roles still apply |
| `allowed_users.owners` | the operator's username on that server |
| `allowed_users.guests` | `[]` |
| `inherits` | `default`, on all three entries |
| `description` | `managed by coop-keeper` plus the operator's stated intent, on all three entries |

Room membership is separate from the rule: a rule with `include: ["*"]`
serves any room the account is in, and joining is done with `coop-provision
add-to-channel`. The keeper adds the new account to every room a rule of another connector
**of the same server** (§3.4) names literally *and actually serves* — a room
the rule lists under `include` but vetoes under `except_for` is not one it
serves, and the new account is not added to it; rules of other servers say
nothing about this one, even when a channel name happens to repeat. A glob in
an existing rule cannot be
expanded (there is no `list-channels`, and glob expansion is deliberately not
added to `coop-provision`), so the keeper asks which rooms it stands for.
When that discovery yields nothing — the first bot on a server has no
earlier rules to learn from — the keeper asks the operator which rooms the
bot should join, and accepts "none" as an answer that leaves the bot reachable
by direct message only — with `rooms: {include: [], direct: true}`, since
both servers place a new account in default rooms and `["*"]` would serve
them. The plan lists every `add-to-channel` it will run.

Ordering: the agent's directory and persona file first, because
`working_directory` must exist when the file is validated
(`gateway/config.py`, `_parse_one_agent`); then the account (or its
credentials); then **one** `coop config patch --file` carrying every
configuration entry the plan touches — presets and templates if absent, the
connector, the agent, the rule, the agent-chain list — validated and written
as a whole (§3.8). One write, one validation, one backup: there is no state
between "connector added" and "rule added" for a failure to leave behind or
for a preview to miss. The one exception is the second team on a Mattermost
installation: a fragment cannot copy another connector's credentials, so
that connector is added with `coop config add connector --credentials-from`
(one write) and the rule and agent-chain change follow in one fragment (a
second write), both under the same dry-run/digest discipline, and the plan
says so. Two facts of this case surfaced in the lab (scenario 10): the
second connector's rule takes `direct: false`, because validation allows
`direct: true` on only one connector of an account (a DM has no team) and the
first team keeps it; and the account has to be a member of the second team
before that connector can connect — `add-to-channel` through the second
team's profile adds the membership — so the ordinary order, rooms before the
configuration write, matters here more than elsewhere: written first, the
connector degrades at reload until the rooms step has run.

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
leaving a room is a visible act in chat that the operator can do deliberately.
A persona change rewrites the agent's `AGENTS.md` and changes nothing in
`config.yaml`, so `reload` has nothing to apply. On Claude Code
the gateway launches a fresh `claude` process for every turn with
`--system-prompt-snapshot off` (`gateway/agents/claude/adapter.py`), so the
rewritten file is read on the bot's next turn without further action; an
OpenCode session rebuilds its system prompt from the instruction file on
every model step (opencode 1.18, `session/instruction.ts`), so the same holds
there. The plan for a persona change still offers `coop reset
'<connector>:*'` for each connector of the agent (§3.4) — all of them share
the one persona file — as an optional, confirmed runtime step: it gives the
bot a clean session rather than a mid-conversation change of voice.
For an agent whose working directory is outside `agents/user/`, the keeper
reports that it does not manage that agent's persona file and leaves the
operator to edit it. A working directory shared by more than one agent is a
shared thing (§3.4): the plan names every agent the edit would reach, and the
operator decides whether that is what they meant.

When the removed bot's rule held the account's `direct: true` (§3.5: one
connector of an account answers DMs), the surviving connector's rule takes it
over in the same step-2 write, or the account's DMs would go unanswered.

Removing a bot is confirmed once and then done in **three ordered steps**,
every one subject to the shared-things rule (§3.4). The order exists because
the daemon treats the two halves of a removal differently: a record whose
*rule* disappears at reload is fully reclaimed — subscription, backend
session, prompt file, attachments, record (`session_manager.py`,
`_apply_expire`) — while a *connector* that disappears at reload keeps its
backend session by design (`keep_backend_session`, #144/#146). "Backend
session" here means what the backend can delete: OpenCode sessions are
deleted; Claude sessions never are — `ClaudeBackend` does not implement
`delete_session`, so every path leaves the transcript under
`~/.claude/projects/` and logs the id (the open decision in #146). For a
Claude bot the ordering therefore protects the prompt file, attachments and
record; for an OpenCode bot it also protects against a leaked session.
Removing both in one edit would take the second path; reclaiming with
`coop expire` first would leave a window in which the still-present rule
recreates the watcher on the next message.

1. **Detach the runtime.** If the daemon is not running, `coop start` (the
   configuration still holds the bot, so it starts). Then one `patch` removes
   the rule — and only the rule — and `reload` applies it: the daemon reclaims
   the room's record in full, and with no rule claiming the room, a message
   arriving now creates nothing.
2. **Remove the configuration.** One `patch` removes the connector, unless
   another rule still references it; the agent, when no rule names it any
   more; and this bot's username — that entry only, never another name in
   the list — from the agent-chain list, unless another surviving bot still
   uses it (the default of naming accounts after the agent makes that the
   ordinary case for an agent on two servers). `reload`
   applies it. If this leaves the deployment empty — presets and templates
   only — `reload` accepts that (it stops the last connector) and `start`
   would refuse it (§3.10), so the plan then stops the daemon and says the
   deployment is empty.
3. **Remove what lives outside `config.yaml`.** The server account, with
   `coop-provision delete-user`, only when no surviving connector of the same
   installation uses that username — compared case-insensitively, since
   Rocket.Chat treats `ProbeBot` and `probebot` as one login — and knowing
   that on Mattermost this deactivates the account for every team at once,
   which `reactivate-user` can undo. The agent's directory under
   `agents/user/`, only when the agent itself was removed and no surviving
   agent's resolved `working_directory` is that directory, inside it, or a
   parent of it (a hand-written configuration may share directories; the
   keeper does not assume otherwise). When a check fails the thing is kept
   and the report says which agent or connector still uses it. A directory
   anywhere outside `agents/user/` is never touched.

The account is last, not first: a connector whose account has just been
deleted enters an authentication-failure loop until it is stopped, and step
1's reclaim needs the connector alive to unsubscribe from the room.

### 3.7 Agent-to-agent chains

With the defaults above, the second bot on a server would see the first
bot's messages as ordinary user messages and answer them, and vice versa,
with no loop protection — `agent_chain` disables that protection when
omitted. The keeper therefore maintains a single list of every bot username
in `connector_templates.default.agent_chain.agent_usernames`, appending on
creation and removing on deletion. Own messages are dropped by each
connector before this list is consulted, so a bot's own name in the shared
list is harmless. `max_turns` and `ttl_seconds` keep their defaults. The
connectors this touches are those of the **installation** (§3.4): the
account, and so the name in the list, is one across every team of a
Mattermost URL.

The list is shared across servers. A human on one server whose username
equals a bot's username on another would be treated as an agent there
(bypassing the sender allow-list, subject to the turn budget). This is
accepted for this version and documented. Whether a connector actually
receives the shared list is decided from its resolved `agent_usernames` in
`coop config show --json`, not from its `inherits:` value — an entry-level
list replaces the template's wholesale, so a hand-written connector that
inherits `default` and also sets its own list is as unaffected as one that
inherits nothing. Every connector of the installation whose resolved list lacks the
new username is patched individually, and the plan says so. Removal is the
mirror image: when no surviving bot on any server uses the username (the
list is shared, so a name protects the agent's other bots too), every
connector whose resolved list still carries it is patched to drop that name — the shared template and any
entry-level override alike, and only that name; a hand-written entry naming a
bot of another deployment stays — so a deleted account cannot keep bypassing
the sender allow-list through a list the keeper once added it to.

### 3.8 The plan is the unit of confirmation

The keeper expands each request into one plan — accounts to create or
delete, rooms to join, the persona text, the configuration change, and the
runtime effect (`reload`, `start`, `stop`) — prints it, and asks once. On yes
it executes the whole plan without further questions. Steps that only read
state need no confirmation. A persona write does: on Claude Code the bot
reads the file on its next turn (§3.6), so a persona-only request is its own
plan.

**One configuration write per step.** Everything a plan changes in
`config.yaml` is composed into one fragment and applied with one
`coop config patch --file` (§3.10): merge-patch semantics, the whole file
validated, one backup, one atomic replace. A removal has two such steps for
the reason §3.6 gives; everything else has one. Credentials never enter the
fragment — a field is written as `{from_file: <path>}` and the command reads
the file itself.

**Nothing is written before the yes.** The plan is built from the resolved
configuration and from `patch --file --dry-run --json`, which returns the
whole merged, masked result without touching the file — so the preview is
the exact final state, and a declined plan leaves no edit on disk waiting for
a later `start` to apply it. One dry run is allowed to fail before the yes:
a plan that creates an agent's directory as its first step (§3.5) is
dry-run before that directory exists, so "working directory does not exist"
and the rule that follows from it are expected there; the dry run is
repeated, and must be clean, once the directory exists and before the write.
The plan's own scratch files — the fragment under the keeper's `.plans/` and
the generated password file — do exist before the yes and are removed on a
no.

**A write applies only to the file it was planned against.** The dry run
reports the digest of the file it read; the write passes it back as
`--if-digest`, and if anything — the config TUI, a hand edit, another keeper
— changed the file in between, the write is refused, nothing is written, and
the keeper re-plans and asks again. This is the correctness guarantee against
concurrent editors, and it needs no lock.

**One plan at a time.** Before executing — before the plan's first step, a
directory or an account included, and until after its last — the keeper takes
`~/.agentcoop/agents/plan.lock` — created with `mkdir`, which is atomic and
fails if the directory exists — holding a description of the plan and an
expiry a few minutes out; it removes the lock when the plan ends. A keeper that finds the
lock held and unexpired tells the operator who holds it and for how long, and
does not start. The lock is cooperative — only keepers honour it; the TUI and
hand edits are caught by the digest — and it expires on its own, so a keeper
that dies mid-plan leaves nothing to clean up by hand. It lives under
`~/.agentcoop/agents/`, not in the keeper's directory, because it guards the
deployment, not one copy of the keeper — and under `agents/` rather than
`~/.agentcoop/` itself because that is the one directory both CLIs'
permission files already open to the keeper (§3.9) — a lock directly under
`~/.agentcoop/` prompts on OpenCode on every plan. No agent can be named
`plan.lock`: the agent-name pattern has no `.`.

After the write, `coop config reload --dry-run` runs as a guard, not a
preview: it is the daemon's own account of what the confirmed edit will do,
and if it names anything the plan did not — a connector restart the operator
was not told about — the keeper stops and shows it before `reload` (or
`start` when the daemon is not running).

**Best effort against everything else that moves.** While a plan runs, the
configuration, the chat server and the daemon can all be changed by someone
else — an operator in the TUI, an admin on the server, a message that
creates a watcher. The keeper does not try to accommodate that. Where a step
can detect it — the digest; a `create-user` that finds the account already
there, which the CLI reports as a skip with exit 0 and the keeper treats as a
stop, since the account's password is not the generated one — it stops with
what it met; otherwise the plan proceeds and the outcome is whatever the
interleaving produced. A plan with two writes (§3.5's second team, §3.6's
removal) is confirmed once on both fragments; the second is dry-run again
after the first write and written against the digest that write returned.

**No rollback.** When a step fails, the plan stops there, says exactly which
steps completed and which did not, and does nothing to undo them. A failure
part-way through a plan that has touched a file, a chat server and a running
daemon is a state that a mechanical rollback can easily make worse. What
follows is not plan mode: the operator and the keeper repair the state one
step at a time, each step visible and confirmed like any other, using the
same commands and the documentation the keeper carries for exactly this
(§3.11) — or the operator repairs it by hand. A kept password file (§3.3) is
one input to that repair.

### 3.9 Permissions for the keeper itself

Both CLIs get the same shape of rule set, and the same restraint: allow the
paths and commands the keeper must use, deny the credential files, and leave
everything else at the CLI's own default. It is a reasonable guardrail, not a
perfect one (§3.3).

Claude Code's `auto` permission mode cannot be selected from a project-local
settings file, so the shipped `.claude/settings.json` uses explicit rules:
allow the `coop` subcommands the skills run — `config`, `status`, `start`,
`stop`, `reset`, `list` — never `coop *` (that would auto-approve `coop send
--attach <file>`), `Bash(coop-provision *)`, the handful of shell commands
the skills run — `mkdir` for an agent's directory and the plan lock,
`mktemp` and `openssl rand` for the password file, `rm` for the password
file, the lock, a fragment and a removed agent's directory, `ls` for the
empty-directory check, `date` for the lock's expiry — the `Read`/`Edit`
tools under `~/.agentcoop/agents/` (the plan lock lives there too, §3.8), and
`Read` on `install_meta.json` for the repository path (§3.11);
deny the credential files named in §3.3. Everything else prompts, which is
Claude Code's default. The rules take effect only after the operator has
accepted Claude Code's workspace-trust dialog for the directory — until
then every `coop` command prompts, and a headless session is refused even the
session-start reads; the install and user documentation say to accept it.
The allow list is derived from what the four skills actually execute
(`tests/unit/test_coop_keeper_shipped.py` walks the skills in both directions:
every command has a rule, every rule has a command), so a skill that gains a
new command adds it here in the same change, and a rule nothing uses fails.

OpenCode's defaults allow everything except `external_directory` — any path
outside the working directory — which asks. The keeper's directory is not a
git repository, so `~/.agentcoop/config.yaml` and `~/.agentcoop/agents/user/…`
are both "external" to it, and without a config every persona write would
prompt. The shipped `opencode.json` therefore carries one `permission` block
(the v1 object form, which OpenCode v2 migrates and v1 requires): allow
`external_directory` on `~/.agentcoop/agents/*` (that check sees `<dir>/*`,
so it can only speak about directories, and it is why the plan lock lives
under `agents/`), and deny
`read` and `edit` on the three credential paths as `*/.agentcoop/…` (those
checks see a path relative to the working directory, so a `~/…` pattern would
never match). Nothing else — no bash patterns, no change to the bash default;
a `cat` of a credential file falls to `external_directory`'s default `ask`.
Both files also pin the CLI's default agent to its built-in one
(`"agent": ""` for Claude Code, `"default_agent": "build"` for OpenCode): an
operator whose CLI defaults to a persona agent of their own would otherwise
run the keeper *as* that persona, and `AGENTS.md` would compete with it. `AGENTS.md` and the skills refer to no
provider-specific feature.

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
and distinguishable from one that was never set. The redactor there matches
on key names, so the raw view exempts the positions where a key is an
entity's *name* — a template, preset, agent or rule called `token-bots` is
shown, not masked — and masks only values under the schema's credential
fields. The resolved view (`config
show --json`) already uses the same sentinel. Because a masked document must
never be written back, `config add` and `config patch` refuse any value equal
to the sentinel, whether it arrives through `--set` or inside `--file`; the
keeper edits by path and never round-trips a whole document. Unlike `config
show`, `show --raw` prints the file even when it does not validate — with the
findings, and exit 1 — because the keeper reads a hand-written file with it
before it can fix the file; a `config.yaml` that does not exist yet is
reported as the empty deployment (`exists: false`, an empty document,
`ok: true`) rather than as an error, since that is the state bootstrap starts
from (§3.2). A YAML error is reported by kind and position only: the
offending line may be a credential.

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
are parsed as YAML so `500` is an integer and `"500"` a string, and a list
entry is addressed with `--entry connector:<name>` (likewise `rule:`) rather
than inside the path, so a name is never parsed for delimiters; `--entry`
addresses an entry that exists and refuses an absent name. Merge-patch
replaces a list wholesale, and `connectors:` and `watcher_rules:` are lists
in the file, so a fragment's entries there are merged **by `name`**: a new
name is appended, an existing one merged into, and an explicit `op` field on
the entry names the other two operations — `op: add` refuses an existing name
(the same rule as `config add`) and `op: remove` deletes the entry and only
it. `op` is the fragment's word and never reaches the file. The mapping
blocks (`agents:`, the templates, `tool_presets:`) are keyed by name in the
file itself, so plain merge-patch addresses one entry there and `null`
removes it. A fragment
may write a credential as `{from_file: <path>}`; the command reads the file
and stores the value, and the fragment itself never holds it — and reads it
on a dry run too, so the keeper writes a password file before it plans.
`--dry-run
--json` returns the whole merged, masked document and the digest of the file
it was computed against (`file_digest`, over the file's bytes — not the
`digest` `config show` reports over the resolved configuration, which does
not see a `description` edit); the write accepts that digest as
`--if-digest` and refuses, writing nothing, when the file has changed since
(§3.8).
`backends` reports each supported backend type with its command and whether
it was found, using the same lookup `add agent` uses.

All writes go through the config TUI's existing atomic save, which takes a
timestamped backup under `.config-backups/` first. The one exception is the
first write to a `config.yaml` that does not exist yet (bootstrap, §3.2): it
is planned against the empty deployment — the digest of no bytes — and
created `0600` with the same temp-beside-then-replace and no backup, there
being nothing to back up. Comments in `config.yaml`
are not preserved by any of these — the TUI has never preserved them, and
the `description` field is the preservable comment; the user guide and
`config.example.yaml` now say so.

`coop-provision` gains five things: `--password-file` on `create-user`;
`reactivate-user <username> --password-file`, which on Mattermost re-enables
a deactivated account and sets the new password (the admin API needs no old
one) — an account that is *active* is refused, since that would be a password
rotation (§5) — and on Rocket.Chat reports that there is nothing to
reactivate, deletion there being permanent;
`init <profile> --type … --server-url … [--team …]`, which writes a profile
with empty credential fields and refuses to touch a profile that already
exists; `<profile> check`, which runs the existing `connect()` — an
authenticated `get_me` plus team resolution — and exits non-zero on failure;
and `profiles --json`, which lists every profile's name, type, server URL and
team with credential fields present but masked with the same `***`
sentinel, so the keeper can tell an unfilled skeleton (field empty) from a
filled profile (field masked); a profiles file that does not exist yet lists
no profiles rather than failing. `init` and `profiles` act on the file rather
than through a profile, so they take the place of the `<profile>` word and a
profile literally named `init` or `profiles` cannot be addressed; `init`
re-serializes the file (comments in it are not kept) and leaves it `0600`.
The CLI validates only the profile it was asked for, so an unfilled skeleton
beside a working profile does not stop the working one. The default location
of `admin-profiles.yaml` moves from the current directory to
`~/.agentcoop/admin-profiles.yaml`; `--config` and `COOP_ADMIN_CONFIG` still
override it. The default `--log-file` moves the same way, to
`~/.agentcoop/coop-provision.log`: the keeper runs the command from
`builtin/coop-keeper/`, whose owned files an upgrade refreshes (§3.1), and the
full API error log is exactly what an operator needs after a failed plan.

### 3.11 Skills

Four skills, for the flows that have order and consequences: `coop-bootstrap`,
`coop-add-bot`, `coop-remove-bot`, `coop-apply-config` (the `coop-` prefix
keeps the owned skill directories from colliding with an operator's, §3.1).
Single-step operations — a persona edit, a room change — are described in
`AGENTS.md` and end in `coop-apply-config`. Putting an existing agent on a
second server is `coop-add-bot` with an existing agent, not a separate skill.
Skill frontmatter is `name` (equal to the directory name — OpenCode v1 keys a
skill by `name`, v2 by the directory) and `description`, the two fields every
CLI reads; the same `.claude/skills/` directory serves Claude Code and both
OpenCode lines. No `.opencode/` directory is shipped: OpenCode v1 writes a
`package.json` and `node_modules/` into one that exists. `AGENTS.md` itself is kept short in this version
— the keeper's role, the vocabulary translation, the credential rule, the
session-start checks, the plan rule — and grows from what testing shows it
needs. One thing it does carry in full: **where the authoritative
documentation is**, so that when a plan has failed part-way (§3.8) the keeper
can reason from the real schema and the real command surface rather than
from memory. The repository is at the `repo_path` in
`~/.agentcoop/install_meta.json`; from there `AGENTS.md` names
`gateway/schema/config.schema.json` and `config.example.yaml` for the
configuration, `docs/user-guide.md` and `docs/permission-reference.md` for
behaviour, `docs/design/config-reload-design.md` for what `reload` does and
does not do, and `coop --help` / `coop-provision --help` for the commands as
installed. Repair is done with the same read-only checks and the same
confirmed steps as any plan, one at a time.

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
- A configuration write applies only to the file it was planned against
  (`--if-digest`); a concurrent edit makes the write fail, never merge.
- **Best effort against concurrent change.** Changes made to the
  configuration, the chat server or the daemon while a plan is running are
  not accommodated. Where a step can detect the interference it fails with
  the error it met; where it cannot, the result is whatever the interleaving
  produced.
- **No rollback.** A plan that fails part-way stops, reports what completed
  and what did not, and undoes nothing; repair is a separate, step-by-step,
  confirmed activity, not part of plan mode.
- Comments in `config.yaml` are not preserved.
- Room membership is only as complete as the literal room names in existing
  rules; globs require the operator's answer.

## 5. Out of scope for this version

Rotating a bot's password or token on request (a Mattermost account that is
reactivated does receive a new password, but only then); a lock the CLI
enforces — `plan.lock` is honoured by keepers only; platform-specific account settings
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
   and `show --raw`; on the write commands `--dry-run`, redacted JSON,
   `--if-digest`, `--entry`, `{from_file:}` and `--credentials-from`; the
   empty-deployment validation change; the `***` write-back refusal;
   `coop-provision init`/`check`/`profiles`/`reactivate-user` and
   `--password-file`; and the `admin-profiles.yaml` and log default paths. Independently
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
4. Change `bob`'s persona (`AGENTS.md`); confirm the next turn reflects it on
   Claude Code, and that the plan offered `coop reset` and a reset session
   reflects it.
5. Add `bob` to Rocket.Chat (second server; new profile sub-flow).
6. Remove `alice` in the three steps of §3.6: after step 1 a message in her
   room creates no watcher and her session is gone; after step 3 the
   account and directory are gone.
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
    other connector working. Then remove the last one and create `bob` on
    Mattermost again: the plan uses `reactivate-user` and the bot logs in.
11. Remove a bot whose connector another rule still uses: the plan stops,
    names the rule, and completes only as the wider plan the operator chose.
12. Remove the last remaining bot with the daemon already stopped: the plan
    starts it, detaches, removes, stops it; `config.yaml` keeps its presets
    and templates and still validates; no `state.*.json`, prompt file or
    backend session for the bot remains; `coop start` refuses with the
    empty-deployment message.
13. Edit `config.yaml` in the TUI between a plan's yes and its write: the
    write is refused on the digest, nothing is written, the keeper re-plans.
14. Start a plan in a second keeper session while the first holds
    `plan.lock`: the second reports the holder and does not start; after the
    lock expires it may.
15. Make a step fail mid-plan (revoke the admin token after step 1 of a
    removal): the keeper stops, reports steps 1 done and 2–3 not, undoes
    nothing, and the repair proceeds as individual confirmed steps.
16. Remove an agent's bot on one server while its bot on another survives:
    the account is gone from the first server and the username is still in
    the shared agent-chain list.
