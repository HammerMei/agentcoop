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
The AgentCoop daemon process — the thing `coop start` starts, that holds watcher records and talks to connectors. A component of AgentCoop, not a synonym for it. Also the Python package name.
_Avoid_: the server, the service (in prose), "the coop" (for the process)

**Coop Session Identity**:
The header block injected at the top of every agent session that tells the agent it is running under Coop and names its watcher and room. The name is deliberate: it lets an agent tell this environment apart from any other session or context it may hold.
_Avoid_: ACG Session Identity, Session Identity

### Agents and bots

**agent**:
One `agents:` entry in `config.yaml`: a backend process (`claude` or `opencode`), a working directory and a persona. One agent can be present on several chat servers. When an operator says "agent" in conversation they usually mean an agent and its first bot.
_Avoid_: persona (for the entry — the persona is the agent's instruction file), watcher, bot (for the `agents:` entry)

**bot**:
An agent's presence on one chat server: the connector (the server account), the agent, and the watcher rule binding them. An agent has at most one bot per server; the same agent on Mattermost and Rocket.Chat is two bots.
_Avoid_: account (that is only the connector's half), agent (for the whole triple)

**profile**:
One chat server's administrative credentials in `admin-profiles.yaml`, read by `coop-provision`. Named after the server host (`mm-labpig`) unless renamed. Distinct from a connector, which holds a bot's own credentials.
_Avoid_: admin config, server config

**coop-keeper**:
The built-in admin agent, installed at `~/.agentcoop/agents/builtin/coop-keeper/` once it ships, which an operator runs with their own `claude` or `opencode` CLI to create, change and remove bots (`docs/design/coop-keeper-design.md`). Replaces the removed `coop onboard` wizard.
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
