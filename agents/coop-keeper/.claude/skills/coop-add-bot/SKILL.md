---
name: coop-add-bot
description: Create a bot — a new agent with its persona and server account, or an existing agent on a further server. Plans the account, the rooms, the persona and one configuration write; generates the password into a file that is never read; maintains the shared agent-chain list. Ends in coop-apply-config.
---

# coop-add-bot

## Inputs

- **agent name** — one lower-case path component `^[a-z0-9][a-z0-9_-]{0,63}$`.
  Refuse anything else (a `/`, `..`, upper case) before creating anything.
- **profile** — the server. If no profile matches, offer `coop-bootstrap` first.
- Only when the agent does not exist yet: **backend** (choose from
  `coop config backends --json`; refuse one whose `found` is false) and
  **persona** (see below).

Read first: `coop config show --json` (resolved), `coop config show --raw --json`
(what the file holds — templates present? `file_digest`), `coop-provision
profiles --json`, `coop config backends --json`.

## Decide what this bot is

- **Agent exists** (`agents.<name>` in the resolved config): reuse its entry,
  working directory and persona unchanged; create only a connector and a rule.
  Say so in the plan; do not ask for a persona.
- **Agent already has a bot on this server**: a connector named by one of its
  rules whose `server.url` (canonicalised as AGENTS.md says) and, on Mattermost, `server.team`
  match the profile. Do not create a second one — offer to change the existing
  bot instead.
- **Account already exists on this installation**: a connector of the same URL
  (any team) whose `server.username` equals the chosen username. No account is
  created; the new connector takes its credentials with `--credentials-from`
  (below).
- **Name taken in config**: `coop config add`/`op: add` refuses it. Report the
  conflict and ask for another name.

## Defaults (each overridable by the operator's wording)

| Field | Default |
|---|---|
| server username | the agent name |
| email | `<username>@agentcoop.invalid` |
| `rooms` | `{include: ["*"], direct: true}` |
| `allowed_users.owners` | the operator's username on that server (from the server's other connectors when they agree on one owner; otherwise ask) |
| `allowed_users.guests` | `[]` |
| `inherits` | `default` on connector, agent and rule |
| `description` | `managed by coop-keeper` plus the operator's stated intent |
| connector name, rule name | `<agent>@<profile>` |
| agent `working_directory` | `~/.agentcoop/agents/user/<agent>` — write the **absolute** path |

## Persona

Written to `~/.agentcoop/agents/user/<agent>/AGENTS.md`, with `CLAUDE.md`
containing the single line `@AGENTS.md` beside it. Two more files pin the
bot's CLI to its built-in agent, so an operator whose CLI defaults to a
persona agent of their own does not have every bot answer as that persona:
`opencode.json` containing `{"default_agent": "build"}` and
`.claude/settings.json` containing `{"agent": ""}`. `CLAUDE.md`,
`opencode.json` and `.claude/settings.json` are created once and never
overwritten if present — the operator may edit them. On Claude Code, writing
into a `.claude/` directory asks the operator even though the path is
allowed — the CLI guards its own configuration directories — so that one
write prompts once; if it is refused, say so in the report and carry on, the
bot works without it.

- Text the operator supplied verbatim is written verbatim.
- An intent ("a professional lawyer, I have a car-insurance question") is
  drafted within that intent, adding nothing the operator did not ask for.
- No persona given → a one-line stub so the file exists to be edited later.

Show the text in the plan. Refuse the name when the directory already exists
and is not empty (`ls -A <dir>` prints something): creation would rewrite its
`AGENTS.md` and a later removal would delete it. Confirm the directory's
resolved path is under `~/.agentcoop/agents/user/` before writing.

## Rooms

Membership is separate from the rule: `include: ["*"]` serves any room the
account is in; joining is `coop-provision <profile> add-to-channel`. Add the
new account to every room a rule of another connector **of the same server**
names literally under `rooms.include` and does not veto under
`rooms.except_for`. A glob cannot be expanded — ask which rooms it stands for.
When nothing is found (first bot on the server), ask which rooms the bot
should join; "none" leaves it reachable by direct message only. The plan lists
every `add-to-channel`.

## Shared templates (first bot only)

If `show --raw --json` shows no `tool_presets.readonly-builtins` (a
hand-written file may have other presets), or no `default` entry in
`connector_templates`, `agent_templates` or `watcher_templates`, the fragment
creates the missing ones as its first edit. They carry only what is the same
on every server and backend:

```yaml
tool_presets:
  readonly-builtins:
    - {tool: Read}
    - {tool: Glob}
    - {tool: Grep}
    - {tool: WebSearch}
connector_templates:
  default:
    description: managed by coop-keeper — settings every bot's connector inherits
    filter_sender: false
    agent_chain:
      agent_usernames: []
agent_templates:
  default:
    description: managed by coop-keeper — settings every agent inherits
    permissions: {enabled: true, timeout: 300}
    owner_allowed_tools: [readonly-builtins]
watcher_templates:
  default:
    description: managed by coop-keeper — settings every rule inherits (session TTLs go here)
```

Never `type`, `command`, `working_directory`, `server`, `allowed_users` or
`rooms` in a template. A hand-written file with templates under other names is
left alone; keeper entries still inherit `default`, which you add.

## Agent chain

Every bot username goes into
`connector_templates.default.agent_chain.agent_usernames`, so that bots do not
answer each other without loop protection. Merge-patch replaces a list
wholesale: write the complete list (existing entries plus the new username).
Then check every connector **of this installation** (same server URL, any
team — accounts and the chain list live at that level) in the resolved
config: one whose resolved `agent_chain.agent_usernames` lacks the new
username has an entry-level list of its own — patch that entry's list too, in
the same fragment, and say so in the plan.

## Password file

```
mktemp ~/.agentcoop/agents/.bot-password.XXXXXX
```

creates a `0600` file under `agents/` — the directory your file tools and
shell are allowed to work in on both CLIs, and one that survives a reboot if
the file has to be kept — and prints its path. Then, with that literal path:

```
openssl rand -hex 24 > <path>
```

The value is never printed, read or repeated. Delete the file with `rm <path>`
only after the whole plan has completed. If the plan fails between the account
step and the configuration write, **keep the file**, name its path in the
report, and reuse it when the plan is resumed — a fresh password would leave an
account whose password nobody has. There is no rollback of a created account.

## The dry run before the yes

Run the fragment through `coop config patch --file … --dry-run --json` to get
the plan's configuration section (see `coop-apply-config`). For a **new**
agent this dry run reports exactly one expected error — the agent's
`working_directory` does not exist yet, and the rule that names the agent
cannot resolve — because the directory is created only after the yes. Show
the plan with that noted; any other finding is a real problem in the
fragment. After step 1 below has created the directory, run the dry run
again: it must now be clean, and its `file_digest` must equal the one the
first dry run reported — nothing of this plan has written `config.yaml` yet,
so a different digest means someone else did (stop, re-plan). The write
uses that digest.

## Order of execution

After the yes — and after taking the plan lock (`coop-apply-config` step 4;
it is released after step 6):

1. **Directory and persona**: `mkdir -p ~/.agentcoop/agents/user/<agent>`,
   write `AGENTS.md`; write `CLAUDE.md`, `opencode.json` and
   `.claude/settings.json` if absent (`mkdir -p` the `.claude` directory).
   First, because the configuration write validates that `working_directory`
   exists.
2. **Account** — one of:
   - `coop-provision <profile> create-user <username> <username>@agentcoop.invalid --password-file <path>`
   - it reports the account exists and is deactivated (Mattermost) →
     `coop-provision <profile> reactivate-user <username> --password-file <path>`;
     the plan says the old account is revived with a new password
   - it reports `already exists … — skipping` (the account is **active** and
     no connector of this installation uses it) → **stop before the
     configuration write**: the account's password is not the generated one,
     so a connector written now cannot log in. Report it, keep the password
     file, and offer two confirmed ways on: another username; or taking the
     account over — on Mattermost `delete-user` (deactivates) then
     `reactivate-user --password-file <path>`, on Rocket.Chat `delete-user`
     (permanent) then `create-user` — saying that whoever used the account
     loses access to it. Resume only when the operator has chosen.
   - an existing connector of this installation already uses the username →
     no account; the connector step uses `--credentials-from <that connector>`.
3. **Rooms**: `coop-provision <profile> add-to-channel <username> <room>` for
   each room in the plan.
4. **Configuration** — **one** fragment through `coop-apply-config`, carrying:
   the templates if absent; the connector (`op: add`); the agent, if new; the
   rule (`op: add`); the agent-chain list. Credentials only as
   `{from_file: <path>}`:

   ```yaml
   connectors:
     - name: bob@mm-lab
       op: add
       type: mattermost
       inherits: default
       description: managed by coop-keeper — <intent>
       server:
         url: https://mm.example
         team: lab
         username: bob
         password: {from_file: /Users/alice/.agentcoop/agents/.bot-password.XXXXXX}
       allowed_users: {owners: [alice], guests: []}
   agents:
     bob:
       type: claude
       command: claude
       working_directory: /Users/alice/.agentcoop/agents/user/bob
       inherits: default
       description: managed by coop-keeper — <intent>
   watcher_rules:
     - name: bob@mm-lab
       op: add
       connector: bob@mm-lab
       agent: bob
       inherits: default
       rooms: {include: ["*"], direct: true}
       description: managed by coop-keeper — <intent>
   connector_templates:
     default:
       agent_chain:
         agent_usernames: [alice-bot, bob]
   ```

   **Second team on one installation** is the one case that takes two writes
   (`coop-apply-config`, "a plan with more than one write"):
   a fragment cannot copy another connector's credentials, so the connector is
   added with `coop config add connector <name> --type … --server-url … --team …
   --credentials-from <existing> --owner <operator> --inherits default` (dry
   run, digest, write — same discipline), and the rule and agent-chain change go
   in one fragment after it. The plan says both. Two more things are true of
   this case, and the plan states them up front: the rule gets
   `direct: false` — validation allows `direct: true` on only one connector
   of an account, a DM having no team, and the first team keeps it; and the
   account must be a **member of the new team** before its connector can
   connect, which only the room step gives it (`add-to-channel` through the
   new team's profile adds the membership first; there is no add-to-team
   command) — so here "none" is not an answer to the room question: the plan
   joins at least one room of the new team (every Mattermost team has
   `town-square`), and the room step runs **before** the configuration write,
   or the reload leaves the new connector degraded until it does.

5. **Apply**: `reload`, or `start` when the gateway is stopped — the
   `coop-apply-config` steps.
6. `rm <password path>`.

## Report

Name the account, the rooms joined, the persona file, the configuration
entries, and what the gateway did. If a step failed, which steps completed, the
error, and the kept password file's path.
