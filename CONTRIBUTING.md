# Contributing to AgentCoop

Thanks for your interest in contributing! This guide covers everything you need to get started.

---

## Table of Contents

- [Dev Setup](#dev-setup)
- [Running Tests](#running-tests)
- [Code Style](#code-style)
- [Project Structure](#project-structure)
- [Adding a New Connector](#adding-a-new-connector)
- [Submitting a PR](#submitting-a-pr)

---

## Dev Setup

**Prerequisites:** Python 3.12+, [uv](https://docs.astral.sh/uv/getting-started/installation/)

```bash
git clone https://github.com/HammerMei/agentcoop.git
cd agentcoop

# Install all dependencies (including dev extras)
uv sync

# Set up config (interactive wizard — safe to skip for code-only changes)
make setup
```

That's it. No virtualenv activation needed — prefix commands with `uv run` or use `make`.

---

## Running Tests

```bash
# Run full test suite
make test

# Run a specific test file
uv run pytest tests/test_onboard.py -v

# Run tests matching a keyword
uv run pytest tests/ -k "test_detect_backends" -v

# Run with coverage report
uv run pytest tests/ --cov=gateway --cov-report=term-missing
```

The test suite uses only the in-process `ScriptConnector` — no running Rocket.Chat or agent
instance is needed.  All network and subprocess calls are mocked.

**Add tests for every change.** PRs without tests for new logic will not be merged.

---

## Code Style

- **Type hints throughout** — all public functions and methods must be fully annotated
- **No hardcoded paths** — use `Path.home()`, `Path(__file__)`, or constants; never `"/home/user/..."`
- **No debug `print()`** — use `logging.getLogger(__name__)` inside library code; `console.print()` (Rich) is fine in CLI / wizard code
- **Docstrings** — public functions and classes need a one-line summary; complex ones should document args and raises
- **Imports** — stdlib first, third-party second, local last; no star imports

Run lint (ruff, if installed):

```bash
make lint
```

Ruff is configured in `pyproject.toml` with `line-length = 100`, `target-version = "py312"`, and `select = ["E", "F", "W", "I"]`.

---

## Project Structure

```
gateway/
├── cli.py              # argparse entry point — add new subcommands here
├── config.py           # YAML loader → GatewayConfig dataclasses
├── daemon.py           # daemonization, PID file, signal handling
├── service.py          # top-level orchestrator
├── onboard.py          # interactive setup wizard (Rich)
├── upgrade.py          # upgrade command logic
│
├── core/
│   ├── connector.py    # Connector ABC + normalized types (Room, IncomingMessage, …)
│   ├── session_manager.py
│   └── message_processor.py
│
├── connectors/
│   ├── __init__.py     # connector_factory() — register new connectors here
│   ├── rocketchat/     # reference connector implementation (DDP WebSocket + REST)
│   └── mattermost/     # second reference implementation (REST v4 + WebSocket, no per-channel subscribe)
│
└── agents/
    ├── claude/         # ClaudeBackend
    └── opencode/       # OpenCodeBackend + role-enforcement plugin
```

Key design principle: **`core/` never imports from `connectors/`**.  All platform knowledge stays
inside the connector package; the core layer only sees the abstract `Connector` interface and
normalized `IncomingMessage` objects.

---

## Adding a New Connector

Connectors are the main extension point.  Here is the minimum required to add one:

### 1. Create the package

```
gateway/connectors/myplatform/
├── __init__.py
├── config.py       # MyPlatformConfig dataclass
└── connector.py    # MyPlatformConnector(Connector)
```

### 2. Implement the `Connector` ABC

```python
from gateway.core.connector import Connector, IncomingMessage, Room

class MyPlatformConnector(Connector):
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    def register_handler(self, handler) -> None: ...
    def register_capacity_check(self, check) -> None: ...
    async def send_text(self, room_id, response, thread_id=None) -> None: ...
    async def resolve_room(self, room_name) -> Room: ...
```

See `gateway/core/connector.py` for the full interface and docstrings, and
`gateway/connectors/rocketchat/` or `gateway/connectors/mattermost/` as reference
implementations — the two differ structurally (RC uses DDP WebSocket subscriptions
per room; Mattermost's WebSocket streams every channel the bot belongs to with no
per-channel subscribe), which is useful to compare when deciding how your own
platform's transport model should map onto the `Connector` ABC.

### 3. Register in the factory

```python
# gateway/connectors/__init__.py
from .myplatform.connector import MyPlatformConnector

def connector_factory(cc: ConnectorConfig) -> Connector:
    ...
    if cc.type == "myplatform":
        return MyPlatformConnector(MyPlatformConfig.from_connector_config(cc))
```

### 4. Add to the onboard wizard

```python
# gateway/onboard.py — _step_select_connector()
connectors = [
    ("rocketchat", "Rocket.Chat"),
    ("myplatform", "My Platform"),   # add here
]
```

### 5. Write tests

Model your tests on `tests/test_connector.py`.  Use `unittest.mock` to stub the
platform's transport layer — no live connection required.

---

## Submitting a PR

1. **Fork** the repo and create a feature branch off `main`
2. **Write tests** — new logic must have coverage
3. **Run the full suite** locally: `make test`
4. **Keep PRs focused** — one feature or fix per PR; avoid unrelated cleanup
5. **PR title** — short imperative summary, e.g. `feat(connector): add Slack connector`
6. **PR description** — explain *why*, not just *what*; link related issues

CI runs `pytest` on Ubuntu and macOS across Python 3.12 and 3.13.  All checks must pass
before a PR can be merged.

---

## Questions?

Open a [GitHub Discussion](https://github.com/HammerMei/agentcoop/discussions) or
file an issue — we are happy to help.
