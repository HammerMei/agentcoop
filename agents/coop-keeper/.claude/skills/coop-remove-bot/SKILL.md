---
name: coop-remove-bot
description: Remove a bot in three ordered steps — detach the rule and reload, remove the connector/agent/agent-chain entry and reload, then delete the account and the agent's directory — each step subject to the shared-things rule. Confirmed once, executed in order, no rollback.
---

# coop-remove-bot

Removal is confirmed once and then done in **three ordered steps**. The order
exists because the gateway treats the two halves differently: a record whose
*rule* disappears at reload is fully reclaimed (subscription, backend session,
prompt file, attachments, record), while a *connector* that disappears keeps
its backend session by design. Removing both in one edit takes the second path.

## Identify the bot

From `coop config show --json`: the agent; the connector(s) named by its rules
whose `server.url` (canonicalised as AGENTS.md says) and, on Mattermost, `server.team` match the
profile; the rule(s) naming them. From `show --raw --json`: the file as
written, so you see `inherits:` and entry-level `agent_chain` lists.

## Shared things — check before planning

Count, in the resolved configuration:

- other rules naming the connector → the connector stays (`coop config remove`
  refuses it anyway); the plan says so, or offers the wider plan that removes
  those rules too;
- other rules naming the agent (its bots on other servers) → the agent, its
  directory and persona stay;
- other agents whose `working_directory` is the agent's directory, inside it,
  or a parent of it → the directory stays;
- other connectors of the same **installation** (same URL, any team) with the
  same `server.username`, compared case-insensitively → the account stays; on
  Mattermost, `delete-user` deactivates the account for every team at once.

When something is shared, do not silently narrow the plan: name what is shared
and by whom, and offer the wider plan. The operator takes it or keeps the
shared part.

## The plan

List the three steps with their exact effects, including: whether the gateway
will be started first, whether the deployment becomes empty (and the gateway is
stopped at the end), whether the account is deleted or kept and why, whether
the directory is deleted or kept and why. Ask once.

The plan lock (`coop-apply-config` step 4) is taken before step 1 and released
after step 3 — the account and directory steps are part of the plan.

## Step 1 — detach the runtime

If `coop status` says the gateway is not running, `coop start` (the file still
holds the bot, so it starts). Then one fragment through `coop-apply-config`
that removes **the rule and only the rule**:

```yaml
watcher_rules:
  - name: alice@rc-lab
    op: remove
```

`coop config reload` reclaims the room's records in full. With no rule
claiming the room, a message arriving now creates nothing.

## Step 2 — remove the configuration

One fragment through `coop-apply-config`. It is the second write of one
confirmed plan ("a plan with more than one write"): before the yes its dry
run reports only the reference to the rule step 1 removes; after step 1 it
takes a clean dry run whose `file_digest` equals the one step 1's write
returned, and writes with that.

```yaml
connectors:
  - name: alice@rc-lab
    op: remove                       # unless another rule still references it
agents:
  alice: null                        # only when no rule names the agent any more
connector_templates:
  default:
    agent_chain:
      agent_usernames: [bob]         # the complete list minus this bot's username only
```

If the removed rule carried `direct: true` and a surviving connector of the
same account (the agent's other Mattermost team) has a rule with
`direct: false`, this fragment also sets that rule `direct: true` — the
account's DMs would otherwise go unanswered — and the plan says so.

Agent chain is the mirror of creation: every connector whose resolved
`agent_chain.agent_usernames` still carries **this bot's** username is
patched to drop it — the shared template and any entry-level list alike —
unless a surviving connector of the installation still uses the username
(case-insensitively; then the account stays and so does the name). Nothing
else in a list is touched: a hand-written entry naming a bot of another
deployment is that operator's, and dropping it would silence that bot's loop
protection. Lists are replaced wholesale: write each complete new list.

If this leaves the deployment empty (presets and templates only), `reload`
accepts it and stops the last connector; then `coop stop`, and say the
deployment is empty — `coop start` would refuse it.

## Step 3 — remove what lives outside config.yaml

- **The account**: `coop-provision <profile> delete-user <username>`, only
  when no surviving connector of the same installation uses that username.
  Mattermost deactivates (reversible with `reactivate-user`); Rocket.Chat
  deletes permanently.
- **The directory**: `rm -r ~/.agentcoop/agents/user/<agent>`, only when the
  agent itself was removed in step 2 and no surviving agent's resolved
  `working_directory` is that directory, inside it, or a parent of it. Confirm
  the resolved path is under `~/.agentcoop/agents/user/` first. A directory
  anywhere else is never touched.

When a check fails the thing is kept and the report says which agent or
connector still uses it.

The account is last, not first: a connector whose account has just been
deleted enters an authentication-failure loop until it is stopped, and step
1's reclaim needs the connector alive to unsubscribe from the room.

## When a step fails

Stop. Say which of the three steps completed and which did not, quote the
error, undo nothing. A bot detached but not removed is a defined state: its
rule is gone, its connector and account remain; the repair is step 2 and 3 as
individually confirmed steps.
