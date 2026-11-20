# AgentCoop

The product that puts AI agents into a team's chat rooms as teammates — of humans and of each other — and manages their sessions, permissions and lifecycle there. Formerly agent-chat-gateway (ACG); the name changed when the project outgrew "a gateway from chat to an agent".

## Language

### Naming

**AgentCoop**:
The product, as written in documentation, release notes and the README. One word, two capitals.
_Avoid_: agent-chat-gateway, ACG, Agent Chat Gateway, AgentCoOp, Agent Coop, Agentcoop

**Coop**:
The short, spoken form of AgentCoop. Also the CLI command (`coop`) and the environment-variable prefix (`COOP_`).
_Avoid_: acg, coop-ai

**gateway**:
The AgentCoop daemon process — the thing `coop start` starts, that holds watcher records and talks to connectors. A component of AgentCoop, not a synonym for it. The Python package of the current daemon is `gateway`, frozen until cutover; the remote-agent daemon's package is `coop` (`docs/design/remote-agent-gateway.md` §5).
_Avoid_: the server, the service (in prose), "the coop" (for the process)

**runtime directory**:
The one directory AgentCoop keeps everything in — `config.yaml`, `admin-profiles.yaml`, state, logs, the control socket, the attachment cache, coop-keeper and the agents it creates, the `repo/` clone — and the base every relative path in `config.yaml` resolves against (ADR 0003). `$COOP_HOME` when set, else `~/.agentcoop`; `gateway/paths.py` `RUNTIME_DIR` in code.
_Avoid_: config directory, install directory, home directory (that is the user's `~`), the config file's directory (as a base for anything)

**Coop Session Identity**:
The header block injected at the top of every agent session that tells the agent it is running under Coop and names its watcher and room. The name is deliberate: it lets an agent tell this environment apart from any other session or context it may hold.
_Avoid_: ACG Session Identity, Session Identity

### Agents and bots

**agent**:
One `agents:` entry in `config.yaml`. In the current daemon: a backend process (`claude` or `opencode`), a working directory and a persona. In the remote-agent daemon (`type: remote`): a token the gateway holds and a cursor per room, never a process; the agent runs on its own host. One agent can be present on several chat servers. When an operator says "agent" in conversation they usually mean an agent and its first bot.
_Avoid_: persona (for the entry — the persona is the agent's instruction file), watcher, bot (for the `agents:` entry)

**bot**:
An agent's presence on one chat server: the connector (the server account), the agent, and the watcher rule binding them. An agent has at most one bot per server; the same agent on Mattermost and Rocket.Chat is two bots.
_Avoid_: account (that is only the connector's half), agent (for the whole triple)

**role**:
The label on a delivered post's author: `owner`, `guest` or `agent`. `agent` is any bot in the room's roster; an author the configuration does not list is a `guest`. A label, never a permission: the gateway enforces nothing by it.
_Avoid_: anonymous (no longer a role), permission level

**wake**:
One doorbell event for one bot in one room, carrying metadata and never post text, with a lease. The agent reads the posts itself through `coop read`.
_Avoid_: trigger (that is the post that caused an addressed wake), notification, ping

**turn**:
What an agent does between a wake and its `done`: read, reply with `send`, finish with `done`. A turn the agent starts on its own (its scheduler, heartbeat or initiative) is an unsolicited `send`.
_Avoid_: session (that is the agent's own conversation state), invocation

**lease**:
The gateway's record that a wake is outstanding for a bot in a room, from the agent's poll picking it up until `done` or expiry. One outstanding wake per bot per room.
_Avoid_: lock, reservation

**bio**:
The agent's short self-description, pushed by the agent (`coop bio`), stored by the gateway per agent, read by the classifier. Identity, responsibilities, current topics, reply rules; no memory, no credentials.
_Avoid_: profile (reserved above for admin credentials), persona (the agent's own instruction file), description

**utility**:
The agent-side process for CLI agents (`coop agent run`): a doorbell client that runs one command per wake (`claude -p`, `codex exec`, `opencode run`) and sends `done` if the agent did not. Not a gateway component.
_Avoid_: runner, adapter (that is the per-CLI command table inside it), backend

**profile**:
One chat server's administrative credentials in `admin-profiles.yaml`, read by `coop-provision`. Named after the server host (`mm-labpig`) unless renamed. Distinct from a connector, which holds a bot's own credentials.
_Avoid_: admin config, server config

**coop-keeper**:
The built-in admin agent, installed at `~/.agentcoop/agents/builtin/coop-keeper/`, which an operator runs with their own `claude` or `opencode` CLI to create, change and remove bots (`docs/design/coop-keeper-design.md`). Replaces the removed `coop onboard` wizard.
_Avoid_: onboarding agent, the wizard, keeper (in documentation — fine in speech)

### Permissions

**permission broker**:
The gateway component that decides every tool call an agent makes — allow, deny, or ask a human. One per agent, always running; the backend's own permission engine is bypassed so the broker is the only gate (ADR-0002).
_Avoid_: the hook, the plugin (those are its transports), "permissions" as a synonym for the broker

**allow-list**:
`owner_allowed_tools` / `guest_allowed_tools`: the tool rules a role may run without being asked. The gateway's built-in rules for its own `coop …` commands are part of the owner allow-list. This is the whole policy; everything else is the remainder.
_Avoid_: whitelist, auto-approve list

**human approval**:
What `permissions.enabled` switches: whether an owner's tool call outside the allow-list is put to a person in chat (`true`) or denied at once (`false`). Not a switch for the permission broker or the allow-lists.
_Avoid_: "permissions on/off", "permission system disabled"
