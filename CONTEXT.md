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
