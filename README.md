# AgentCoop

[![CI](https://github.com/HammerMei/agentcoop/actions/workflows/ci.yml/badge.svg)](https://github.com/HammerMei/agentcoop/actions/workflows/ci.yml)
[![Docker](https://ghcr-badge.egpl.dev/hammermei/agentcoop/latest_tag?trim=major&label=docker&color=blue)](https://github.com/HammerMei/agentcoop/pkgs/container/agentcoop)
![Python](https://img.shields.io/badge/python-%3E%3D3.12-blue)
![License](https://img.shields.io/badge/license-MIT-green)
[![Stars](https://img.shields.io/github/stars/HammerMei/agentcoop?style=flat&color=yellow)](https://github.com/HammerMei/agentcoop/stargazers)

**Turn your AI agent into a team-shared chatbot — in minutes.**

Already running Claude Code or OpenCode? **AgentCoop** (previously known as agent-chat-gateway, or ACG) connects it to your team's chat system (Rocket.Chat, Mattermost, and more) so everyone can talk to it directly from chat — no terminal required, no code changes to your agent.

Inspired by [OpenClaw](https://github.com/openclaw/openclaw)'s vision of making AI agents accessible from any messaging app — built for the team layer.

> **How it compares to Claude Code Channels:** Claude Code's native [Channels](https://code.claude.com/docs/en/channels) feature connects a single session to Telegram, Discord, or iMessage — great for personal use. AgentCoop is built for teams: multiple agents, multiple chat systems, per-user roles, human oversight for sensitive operations, and shared sessions across your whole workspace.

---

## Features

- 💬 **[Works with Rocket.Chat and Mattermost](docs/supported-features.md#chat-platform-connectors)** — and extensible to Slack, Discord, or any other chat system you use
- 🤖 **[Bring your own agent](docs/supported-features.md#agent-backends)** — Claude Code and OpenCode work out of the box; plug in any other agent too
- 👥 **[User-aware in chat](docs/user-guide.md#user-aware-responses)** — the agent knows who sent each message and can personalize tone, language, and style per person using room profiles
- 🔒 **[Owner & Guest roles](docs/permission-reference.md#roles)** — control who can do what, with different permissions per role
- 🛡️ **[Human oversight for sensitive actions](docs/permission-reference.md#approval-workflow)** — the agent pauses and asks for your approval before executing risky operations like file writes or shell commands
- 🔗 **[Continue your session remotely](docs/user-guide.md#use-case-3--carry-an-existing-agent-sessions-context-into-chat)** — hand a local agent session's context to a chat room and pick up right where you left off, from anywhere
- 📎 **[File attachments](docs/user-guide.md#attachment-handling)** — send files in chat and the agent can read and work with them
- 🧠 **[Context injection](docs/user-guide.md#context-files)** — pre-load domain knowledge, system prompts, or project context into the agent at startup
- ⚡ **[Multiple chat systems at once](docs/user-guide.md#multi-connector-setup)** — connect to several chat platforms simultaneously
- ⏰ **[Built-in task scheduler](docs/scheduling.md)** — let the agent schedule recurring or one-shot tasks directly from chat ("remind me in 5 minutes", "run daily standup at 09:00") without any infrastructure setup
- 🤝 **[Agent-to-agent collaboration](docs/agent-chain.md)** — let multiple AI agents collaborate in a shared room; built-in loop protection keeps conversations bounded and human-observable
- 🛠️ **[Interactive config TUI](docs/config-tool.md)** — `coop config` gives you a full-screen editor with validation, provenance tracking, and safe writes, instead of hand-editing YAML
- 🧪 **[Voice gateway (experimental)](docs/supported-features.md#voice-gateway-experimental-)** — connect your agent to Siri via iOS Shortcuts; any phone becomes a zero-hardware voice interface with no custom wake word infrastructure

---

## What's Supported

| | Supported today | Can be extended |
|--|--|--|
| **Chat platforms** | Rocket.Chat, Mattermost | Slack, Discord, and others |
| **Agent backends** | Claude Code, OpenCode | Any agent with a CLI interface |
| **Voice** | iOS Shortcuts *(experimental)* | Android via Tasker, dedicated hardware |

---

## Quick Start

### Install (AI-guided, recommended)

The easiest way to install is to ask your AI agent to do it for you — it handles dependencies, configuration, and any troubleshooting automatically.

In Claude Code or OpenCode, run this prompt:

```
Please install AgentCoop by following the instructions at https://raw.githubusercontent.com/HammerMei/agentcoop/main/docs/install-agent.md
```

> Prefer a native install? See [INSTALL.md](INSTALL.md) for step-by-step instructions.

---

## Running the Gateway

```bash
# Start the gateway
coop start

# Check status
coop status

# Stop the gateway
coop stop
```

See [docs/user-guide.md](docs/user-guide.md) for the full CLI reference, configuration options, and usage examples.

---

## Documentation

| Document | Description |
|---|---|
| [INSTALL.md](INSTALL.md) | Manual installation guide |
| [docs/user-guide.md](docs/user-guide.md) | Configuration reference, CLI usage, and operational guide |
| [docs/architecture.md](docs/architecture.md) | System architecture and module breakdown |
| [docs/permission-reference.md](docs/permission-reference.md) | Roles, permissions, and human oversight deep dive |
| [docs/supported-features.md](docs/supported-features.md) | Supported features, known limitations, and roadmap |
| [docs/requirements.md](docs/requirements.md) | Functional specification and behavioral requirements |
| [docs/scheduling.md](docs/scheduling.md) | Built-in task scheduler — recurring and one-shot jobs from chat |
| [docs/agent-chain.md](docs/agent-chain.md) | Agent-to-agent collaboration — enabling multiple heterogeneous AI agents to coordinate via chat |
| [docs/config-tool.md](docs/config-tool.md) | Interactive config TUI — `coop config`, keybindings, and a guide to every entity type |

---

## Why "AgentCoop"?

The project started as *agent-chat-gateway*: a gateway that gave agents without a built-in chat channel a way onto a messaging app. It has since grown into something else — a middleware where several agents collaborate with humans, and with each other, like regular teammates in a room. A gateway is a door; this had become the place behind the door. So: a **coop** — a shared home for agents — and a **co-op**, because they work together in it. Every agent needs a coop to come home to. And yes, they all taste like chicken.

The daemon that `coop start` starts is still called *the gateway*: it is the component of AgentCoop that stands between the chat platforms and the agents (see [docs/architecture.md](docs/architecture.md)). Upgrading from an agent-chat-gateway install is a reinstall — see [docs/migration-v1.md](docs/migration-v1.md).
