---
name: coop-bootstrap
description: First contact with a machine — no profile in admin-profiles.yaml, a profile whose credentials are still empty, or no config.yaml yet. Creates the profile skeleton with coop-provision init, has the operator fill it in, and verifies it with check. Never asks for a credential.
---

# coop-bootstrap

Runs when the session-start checks show one of:

- `coop-provision profiles --json` → `"profiles": []`, or the operator names a
  server no profile covers;
- a profile whose `username`/`password`/`token` fields are `""` (empty means
  unfilled; `***` means filled);
- `coop config show --raw --json` → `"exists": false`.

## A server with no profile

1. Ask for: the platform (`rocketchat` or `mattermost`), the server URL, the
   team name (Mattermost only), and **the operator's own username on that
   server** — it becomes the owner of every bot created there.
2. Propose a profile name: `mm-<host>` or `rc-<host>` from the URL's first host
   label (`https://mm.lab.example` → `mm-mm`, so prefer something the operator
   recognises: `mm-lab`). The name is one lower-case path component,
   `^[a-z0-9][a-z0-9_-]{0,63}$`.
3. Show the profile you are about to create as a plan and ask once:

   ```text
   Profile to create: mm-lab
     type:        mattermost
     server_url:  https://mm.example
     team:        lab
     credentials: left empty for you to fill in
   ```

4. On yes:

   ```
   coop-provision init mm-lab --type mattermost --server-url https://mm.example --team lab
   ```

   `init` refuses a profile that already exists and re-serialises the file
   (comments in it are not kept). It leaves the file `0600`.

5. Tell the operator to open `~/.agentcoop/admin-profiles.yaml` in an editor
   and fill in either `token` or `username` + `password` for that profile,
   then say so. Do not open the file yourself, and do not ask what they typed.

6. When they say it is filled in:

   ```
   coop-provision mm-lab check
   ```

   Exit 0 means the credentials authenticate and (Mattermost) the team
   resolves. Non-zero: quote the message and ask them to correct the file;
   the full API error is in `~/.agentcoop/coop-provision.log`.

## A profile with empty credentials

Steps 5–6 only.

## No config.yaml

Nothing to do now. Say that the first bot's plan will create the file, along
with the shared `tool_presets` and the `default` entries of
`connector_templates`, `agent_templates` and `watcher_templates` — the settings
every bot inherits. Nothing is written at session start (see
`coop-apply-config`: nothing before a yes).

## What the operator's username is for

It is not stored anywhere of its own. When a server already has connectors,
reuse their `allowed_users.owners` only when they agree on exactly one owner;
otherwise, or on a server with no connector yet, ask.
