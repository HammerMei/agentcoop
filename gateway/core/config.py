"""Core configuration types — platform-agnostic dataclasses.

These types are the canonical definitions used throughout the core layer.
``gateway.config`` re-exports them for backward compatibility and adds
gateway-level concerns (``GatewayConfig``, YAML parsing, env-var expansion).

All platform-specific settings (server URL, credentials, user allow-lists)
belong in the connector's own config (e.g. RocketChatConfig).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .connector import UserRole

# Built-in context files shipped inside the gateway package.
# Resolved relative to this file: gateway/core/config.py → gateway/core/ → gateway/ → gateway/contexts/
_BUILTIN_CONTEXTS_DIR = Path(__file__).parent.parent / "contexts"

# Built-in tool rules automatically prepended to every agent's owner/guest allowed tools.
# These are gateway-specific Bash commands that agents should always be able to call
# without triggering a 🔐 human-approval prompt — they are the gateway's own management
# interface, not arbitrary shell commands.
#
# Owner rules: send, schedule, fetch-history, instructions, date
#   (owners get all commands — write, mutation, and read-only)
# Guest rules:  fetch-history and instructions only (read-only; send/schedule are owner-only)
_BUILTIN_OWNER_TOOL_RULES: "list[ToolRule]" = []  # populated after ToolRule is defined
_BUILTIN_GUEST_TOOL_RULES: "list[ToolRule]" = []  # populated after ToolRule is defined

# ── Shared config types ──────────────────────────────────────────────────────

@dataclass
class PermissionConfig:
    """Per-agent permission approval configuration."""
    enabled: bool = False
    timeout: int = 300                              # seconds before auto-deny
    skip_owner_approval: bool = False               # when True, all owner tool calls are auto-approved without RC notification


@dataclass
class ToolRule:
    """A single entry in an owner or guest tool allow list.

    tool:   Regex matched against the tool name (case-insensitive, fullmatch).
            Supports wildcards, e.g. "mcp__rocketchat__get_.*".
    params: Optional regex matched against the tool's primary parameter
            (case-insensitive, fullmatch).  If omitted, only the tool name
            is checked.  The field used for matching depends on the tool:
              Bash / bash   → command string
              WebFetch      → url string
              Read/Edit/
              Write         → file_path string
              unknown / MCP → full tool_input serialized as JSON
    """
    tool: str
    params: str | None = None

    @staticmethod
    def from_config(raw) -> "ToolRule":
        """Parse a config entry — must be a dict with a 'tool' key.

        Validates and pre-compiles both regex patterns at config-load time so
        that misconfigured rules raise a clear ValueError immediately rather
        than producing a cryptic re.error during a live tool-call match.
        """
        import re as _re
        if isinstance(raw, dict):
            tool_pattern = raw.get("tool", "")
            params_pattern = raw.get("params") or None
            # Validate tool regex at config-load time
            try:
                _re.compile(tool_pattern, _re.IGNORECASE)
            except _re.error as e:
                raise ValueError(
                    f"Invalid regex for 'tool' field {tool_pattern!r}: {e}"
                ) from e
            # Validate params regex at config-load time (if provided)
            if params_pattern is not None:
                try:
                    _re.compile(params_pattern, _re.IGNORECASE | _re.DOTALL)
                except _re.error as e:
                    raise ValueError(
                        f"Invalid regex for 'params' field {params_pattern!r}: {e}"
                    ) from e
            return ToolRule(
                tool=tool_pattern,
                params=params_pattern,
            )
        raise ValueError(
            f"Invalid tool rule: {raw!r}. Expected a dict with a 'tool' key, "
            "e.g. {{tool: Bash, params: 'git .*'}}"
        )


# Populate the built-in owner tool rules now that ToolRule is defined.
# These allow agents to call gateway management commands (send / schedule)
# without triggering a 🔐 human-approval prompt in Rocket.Chat.
_BUILTIN_OWNER_TOOL_RULES = [
    ToolRule(tool="Bash", params="coop\\s+send\\s+.*"),
    ToolRule(tool="Bash", params="coop\\s+schedule\\s+.*"),
    ToolRule(tool="Bash", params="coop\\s+fetch-history\\s+.*"),
    ToolRule(tool="Bash", params="coop\\s+instructions\\s+(scheduling|fetch-history)\\s*"),
    # date is a read-only command used by agents to compute timestamps.
    # It is safe to auto-approve for owners so that compound bash commands
    # containing $(date ...) sub-expressions do not trigger approval prompts.
    ToolRule(tool="Bash", params="date(\\s+.*)?"),
]

_BUILTIN_GUEST_TOOL_RULES = [
    # fetch-history is read-only — guests can safely query channel history
    # for context without being able to send messages or create schedules.
    ToolRule(tool="Bash", params="coop\\s+fetch-history\\s+.*"),
    ToolRule(tool="Bash", params="coop\\s+instructions\\s+(scheduling|fetch-history)\\s*"),
]


@dataclass
class AgentConfig:
    name: str = "default"
    type: str = "claude"
    command: str = "claude"
    new_session_args: list[str] = field(default_factory=list)
    working_directory: str = ""  # cwd for sidecar process (opencode agents); "" = inherit gateway cwd
    session_prefix: str = "agent-chat"  # prefix for session names/titles
    lazy_instruction_loading: bool = True  # inject short tool index; load full docs on demand
    context_inject_files: list[str] = field(default_factory=list)  # paths injected once per session
    owner_allowed_tools: list[ToolRule] = field(default_factory=list)  # auto-approved for owners
    guest_allowed_tools: list[ToolRule] = field(default_factory=list)  # auto-approved for guests
    timeout: int = 360           # seconds to wait for the agent to respond; must be > permissions.timeout
    permissions: PermissionConfig = field(default_factory=PermissionConfig)
    # No session_idle_days / session_expire_days here: the on-the-fly watcher
    # lifecycle TTLs live on the WatcherRule instead (design §5.4), so two rules
    # sharing one agent can have different lifecycles. Setting either on an agent
    # is a load error naming the new home — see `_MOVED_TO_RULE_KEYS` in
    # gateway/config.py.

    def effective_owner_allowed_tools(self) -> "list[ToolRule]":
        """Return owner_allowed_tools with built-in gateway rules prepended.

        The built-in rules (``coop send``, ``schedule``,
        ``fetch-history``, and ``instructions``) are always included so that
        agents can call gateway management commands without triggering a 🔐
        human-approval prompt — no user config required.  User-defined rules
        follow the built-in ones so that custom patterns can further extend
        the allow list.
        """
        return list(_BUILTIN_OWNER_TOOL_RULES) + list(self.owner_allowed_tools)

    def effective_guest_allowed_tools(self) -> "list[ToolRule]":
        """Return guest_allowed_tools with built-in read-only gateway rules prepended.

        The built-in guest rules allow ``coop fetch-history``
        and ``coop instructions`` — read-only operations safe
        for guest-role agents.  Write operations (``send``, ``schedule``)
        remain owner-only.
        """
        return list(_BUILTIN_GUEST_TOOL_RULES) + list(self.guest_allowed_tools)


@dataclass
class ConnectorConfig:
    """Configuration for a single connector instance.

    'name' is the unique identifier used for state file namespacing and CLI routing.
    'type' determines which Connector implementation is instantiated.
    'raw' holds the connector-specific config dict, passed directly to the connector factory.
    'context_inject_files' holds connector-level context paths injected into every session
    on this connector (layer 1 of 3; agent and watcher layers are added on top).
    """

    name: str
    type: str       # "rocketchat" | "script" | "voice" | "mattermost"
    raw: dict       # type-specific config, passed to connector factory
    context_inject_files: list[str] = field(default_factory=list)


@dataclass
class HistoryHandoffConfig:
    """Configuration for on-startup channel history injection.

    When enabled, AgentCoop fetches recent channel history and injects it as Layer 0
    context whenever a new agent session is created (reset, upgrade, or first
    join).  This restores conversational continuity without requiring the
    previous agent session to be alive.

    fetch_count : Total number of messages to fetch from the channel.
    verbatim_tail : Last N messages are injected verbatim; older messages are
        condensed to a single line each to reduce context window usage.
    """

    enabled: bool = True
    fetch_count: int = 50
    verbatim_tail: int = 15
    max_fetch_count: int = 200   # hard cap for on-demand fetch-history; prevents context window overload


@dataclass
class WatcherConfig:
    """Static definition of a watcher (connector + room + agent binding).

    Defined in config.yaml under 'watchers:'. The gateway starts all configured
    watchers on startup — no runtime add-watcher commands are needed.

    There is no `session_id` here: sessions are not pinned from config. The gateway
    creates one on first start, persists the id it was given in state.json, reuses it
    across restarts, and clears it on 'reset'. `watchers[].session_id` is refused at
    load, naming the handoff replacement — see gateway/config.py. The runtime-assigned
    `WatcherState.session_id` is a different thing and is unaffected.
    """

    name: str                                        # unique watcher name (used in CLI commands)
    connector: str                                   # must match a ConnectorConfig.name
    room: str                                        # room name or @username for DM
    agent: str                                       # must match an AgentConfig.name
    exclude_rooms: list[str] = field(default_factory=list)  # only meaningful once room == "*" is
    # supported (docs/design/dynamic-watcher-design.md) — the config loader currently rejects
    # room == "*" with a clear "not implemented yet" error, so this is always empty today.
    context_inject_files: list[str] = field(default_factory=list)  # watcher-level context (layer 3)
    history_handoff: HistoryHandoffConfig = field(default_factory=HistoryHandoffConfig)  # session context recovery


# ── CoreConfig ───────────────────────────────────────────────────────────────

@dataclass
class CoreConfig:
    """Platform-agnostic gateway configuration consumed by SessionManager and MessageProcessor."""

    agents: dict[str, AgentConfig] = field(default_factory=dict)
    connector_configs: dict[str, ConnectorConfig] = field(default_factory=dict)
    max_queue_depth: int = 100  # max pending messages per room; 0 = unbounded (not recommended)

    def agent_config(self, name: str) -> AgentConfig:
        """The AgentConfig for `name`, or an empty one if there is no such agent.

        No longer falls back to a `default_agent` or to "whichever agent came
        first": a rule must state its agent, so every caller passes a name that
        was resolved against this same dict. An empty `AgentConfig()` is kept as
        the answer for an unknown name rather than a raise, because the callers
        here read single fields off it (`timeout`, `working_directory`) on paths
        where the loud refusal already happened upstream — `WatcherLifecycle`
        raises before it gets this far.
        """
        if name and name in self.agents:
            return self.agents[name]
        return AgentConfig()

    def env_for_role(self, role: UserRole, agent_name: str = "") -> dict[str, str]:
        """Return the subprocess environment variables for the given user role.

        Passed as ``env`` to AgentBackend.send() so the permission broker can
        identify which role is making a request.

        COOP_ROLE is hardcoded ("owner" / "guest") and never user-configurable.
        Tool allow-list enforcement is handled entirely by the permission broker
        using the structured ToolRule lists from config.
        """
        if role == UserRole.OWNER:
            return {"COOP_ROLE": "owner"}
        if role == UserRole.ANONYMOUS:
            raise ValueError(
                "ANONYMOUS users are not permitted to interact with agent sessions"
            )
        return {"COOP_ROLE": "guest"}

    def context_inject_files_for(
        self,
        connector_name: str,
        agent_name: str,
        watcher_ctx: list[str],
    ) -> list[str]:
        """Return the ordered list of context files to inject for a watcher session.

        Concatenates four layers in order:
          0. Built-in system files (auto-injected; no user config needed):
               - rc-gateway-context.md  — injected for every Rocket.Chat connector
               - mm-gateway-context.md  — injected for every Mattermost connector
               - tool-index-context.md  — injected when the agent uses lazy instruction loading
               - scheduling/fetch-history docs — injected when lazy loading is disabled
          1. Connector-level files (from ConnectorConfig.context_inject_files)
          2. Agent-level files    (from AgentConfig.context_inject_files)
          3. Watcher-level files  (passed in directly as watcher_ctx)

        Built-in injection only fires when ConnectorConfig exists for the given
        connector name.  This keeps unit-test CoreConfigs (which typically have no
        connector entries) unaffected by auto-injection.

        Empty lists at any level are silently skipped.
        """
        result: list[str] = []
        connector_cfg = self.connector_configs.get(connector_name)
        agent_cfg = self.agent_config(agent_name)

        # Layer 0: built-in system context files (shipped inside the package)
        if connector_cfg is not None:
            if connector_cfg.type == "rocketchat":
                result.append(str(_BUILTIN_CONTEXTS_DIR / "rc-gateway-context.md"))
            elif connector_cfg.type == "mattermost":
                result.append(str(_BUILTIN_CONTEXTS_DIR / "mm-gateway-context.md"))
            if agent_cfg.lazy_instruction_loading:
                result.append(str(_BUILTIN_CONTEXTS_DIR / "tool-index-context.md"))
            else:
                result.append(str(_BUILTIN_CONTEXTS_DIR / "scheduling-context.md"))
                result.append(str(_BUILTIN_CONTEXTS_DIR / "fetch-history-context.md"))
            # Layer 1: connector-level user files
            result.extend(connector_cfg.context_inject_files)

        # Layer 2: agent-level user files
        result.extend(agent_cfg.context_inject_files)

        # Layer 3: watcher-level user files
        result.extend(watcher_ctx)
        return result

    def timeout_for(self, agent_name: str) -> int:
        """Return the configured response timeout (seconds) for the given agent."""
        return self.agent_config(agent_name).timeout

    @classmethod
    def from_gateway_config(cls, cfg) -> "CoreConfig":
        """Derive a CoreConfig from a GatewayConfig (transition helper)."""
        return cls(
            agents=cfg.agents,
            connector_configs={c.name: c for c in cfg.connectors},
            max_queue_depth=cfg.max_queue_depth,
        )
