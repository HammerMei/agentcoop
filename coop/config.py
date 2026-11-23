"""The gateway's configuration file (design §4).

Four sections. ``servers`` are the chat servers and, under each, the ``bots``: the
accounts the gateway posts as, keyed by agent name, each with its own ``owners``
(the server's ``owners`` by default). A bot without a ``token_file`` is roster
only (another gateway's, or nobody's, §3.1): the gateway knows it is a bot and
never speaks for it. ``agents`` are the remote agents, each with the bearer token
the doorbell authenticates. ``rooms`` are patterns over ``<server>:<room>``, first
match wins, each naming the agents in that room and the room's parameters.
``gateway`` is the doorbell listener.

Who is a bot, whose instructions a bot takes and who moderates a room all come from
here, through :class:`~coop.room.roster.Roster`, never from the platform alone.

```yaml
servers:
  lab-mm:
    type: mattermost
    url: https://mm.example
    team: lab
    owners: [glin]
    guests: [pat]
    bots:
      laomei:  {username: laomei_bot, token_file: servers/lab-mm/laomei.token}
      wavebro: {username: wavebro_bot, token_file: servers/lab-mm/wavebro.token, owners: [wave]}
      muse:    {username: muse_bot}                      # roster only
agents:
  laomei:  {type: remote, token_file: agents/laomei/token}
  wavebro: {type: remote, token_file: agents/wavebro/token}
gateway:
  listen: 127.0.0.1:7474
  tls: {cert: gateway/cert.pem, key: gateway/key.pem}
classifier:
  command: [claude, -p, --output-format, text]
rooms:
  "lab-mm:team":
    agents: [laomei, wavebro, muse]
    enforce: false
```

Relative paths are relative to the directory the configuration file is in, which
in a normal install is the runtime directory (``coop/paths.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, get_type_hints

import yaml

from .paths import RUNTIME_DIR
from .room.classifier_cli import DEFAULT_COMMAND as DEFAULT_CLASSIFIER_COMMAND
from .room.model import Params
from .room.roster import Roster
from .room_pattern import InvalidRoomPattern, RoomPattern

DESIGN = "docs/design/remote-agent-gateway.md"
SERVER_TYPES = ("mattermost", "rocketchat")
# Fields of the frozen gateway's ``agents:`` entries that have no meaning here: the
# agent runs on its own host, under its own permissions (design §3.4, §4).
REJECTED_AGENT_FIELDS = ("permissions", "owner_allowed_tools", "guest_allowed_tools", "tool_presets",
                         "command", "working_directory")
_PARAM_FIELDS = {f.name for f in fields(Params)}
# What each room parameter must be: the annotation, with the Literal (classifier_fallback)
# read as str. A bool is not an int here, and "false" is not a bool.
_PARAM_TYPES = {name: (hint if hint in (bool, int, float, str) else str)
                for name, hint in get_type_hints(Params).items()}


class ConfigError(ValueError):
    """The file is not a configuration the daemon can run; the message says where."""


@dataclass(frozen=True)
class Bot:
    """One agent's account on one server. ``token_file`` ``None`` is a roster-only bot."""

    agent: str
    username: str
    token_file: Path | None
    owners: frozenset[str]

    @property
    def hosted(self) -> bool:
        return self.token_file is not None


@dataclass(frozen=True)
class Server:
    name: str
    type: str
    url: str
    team: str | None
    owners: frozenset[str]
    guests: frozenset[str]
    bots: dict[str, Bot]  # by agent name

    def bot_by_username(self, username: str) -> Bot | None:
        for bot in self.bots.values():
            if bot.username == username:
                return bot
        return None


@dataclass(frozen=True)
class Agent:
    name: str
    token_file: Path
    heartbeat: str = "gateway"
    bio_source: str = "pushed"


@dataclass(frozen=True)
class RoomRule:
    pattern: RoomPattern
    agents: tuple[str, ...]
    params: Params


@dataclass(frozen=True)
class Listen:
    address: str = "127.0.0.1:7474"
    tls: tuple[Path, Path] | None = None  # (cert, key)


@dataclass(frozen=True)
class ClassifierConfig:
    """The one-shot command that answers the classifier prompt on stdin
    (:mod:`coop.room.classifier_cli`); rooms with ``classifier_enabled: false``
    never run it."""

    command: tuple[str, ...] = DEFAULT_CLASSIFIER_COMMAND
    working_directory: Path | None = None


@dataclass(frozen=True)
class Config:
    servers: dict[str, Server]
    agents: dict[str, Agent]
    rooms: tuple[RoomRule, ...]
    gateway: Listen
    classifier: ClassifierConfig = ClassifierConfig()

    def rule_for(self, handle: str) -> RoomRule | None:
        """The first ``rooms`` entry matching ``<server>:<room>``, or none."""
        for rule in self.rooms:
            if rule.pattern.matches(handle):
                return rule
        return None

    def roster_for(self, server: str, rule: RoomRule) -> Roster:
        """The room's roster on ``server``, in the platform's namespace: the usernames
        of the rule's agents that have a bot there, in the rule's order, each with its
        own owners; the server's guests."""
        found = self.servers[server]
        present = [found.bots[a] for a in rule.agents if a in found.bots]
        return Roster(tuple(b.username for b in present), {b.username: b.owners for b in present}, found.guests)

    def missing_bots(self, server: str, rule: RoomRule) -> tuple[str, ...]:
        """The rule's agents with no bot on ``server``: listed for a room they cannot be in."""
        return tuple(a for a in rule.agents if a not in self.servers[server].bots)


# ── loading ─────────────────────────────────────────────────────────────────


def load(path: Path, base: Path | None = None) -> Config:
    base = path.parent if base is None else base
    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: not YAML: {exc}") from None
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: the document is not a mapping")
    return parse(raw, base)


def parse(raw: dict, base: Path = RUNTIME_DIR) -> Config:
    _only_keys(raw, ("servers", "agents", "rooms", "gateway", "classifier"), "the document")
    servers = {name: _server(name, entry, base) for name, entry in _mapping(raw, "servers").items()}
    agents = {name: _agent(name, entry, base) for name, entry in _mapping(raw, "agents").items()}
    rooms = tuple(_room(pattern, entry) for pattern, entry in _mapping(raw, "rooms").items())
    config = Config(servers, agents, rooms, _listen(_mapping(raw, "gateway"), base),
                    _classifier(_mapping(raw, "classifier"), base))
    _cross_check(config)
    return config


def _server(name: str, entry: Any, base: Path) -> Server:
    where = f"servers.{name}"
    entry = _is_mapping(entry, where)
    _only_keys(entry, ("type", "url", "team", "owners", "guests", "bots"), where)
    kind = _string(entry, "type", where)
    if kind not in SERVER_TYPES:
        raise ConfigError(f"{where}.type: {kind!r} is not one of {SERVER_TYPES}")
    team = entry.get("team")
    if kind == "mattermost" and not isinstance(team, str):
        raise ConfigError(f"{where}.team: a mattermost server names its team")
    owners = _names(entry.get("owners", []), f"{where}.owners")
    bots = {agent: _bot(agent, raw, owners, base, f"{where}.bots.{agent}")
            for agent, raw in _mapping(entry, "bots", where).items()}
    seen: dict[str, str] = {}
    for bot in bots.values():
        if bot.username in seen:
            raise ConfigError(f"{where}.bots: {seen[bot.username]!r} and {bot.agent!r} share username {bot.username!r}")
        seen[bot.username] = bot.agent
    return Server(name, kind, _string(entry, "url", where), team, owners,
                  _names(entry.get("guests", []), f"{where}.guests"), bots)


def _bot(agent: str, entry: Any, default_owners: frozenset[str], base: Path, where: str) -> Bot:
    entry = _is_mapping(entry, where)
    _only_keys(entry, ("username", "token_file", "owners"), where)
    token = entry.get("token_file")
    if token is not None and not isinstance(token, str):
        raise ConfigError(f"{where}.token_file: not a string")
    owners = _names(entry["owners"], f"{where}.owners") if "owners" in entry else default_owners
    return Bot(agent, _string(entry, "username", where), None if token is None else _path(token, base), owners)


def _agent(name: str, entry: Any, base: Path) -> Agent:
    where = f"agents.{name}"
    entry = _is_mapping(entry, where)
    rejected = [k for k in REJECTED_AGENT_FIELDS if k in entry]
    if rejected:
        raise ConfigError(f"{where}: {rejected} are not gateway settings any more; the agent runs on its own "
                          f"host under its own permissions ({DESIGN} §3.4)")
    _only_keys(entry, ("type", "token_file", "heartbeat", "bio_source"), where)
    kind = _string(entry, "type", where)
    if kind != "remote":
        raise ConfigError(f"{where}.type: {kind!r}; every agent is `remote` now, reached through the doorbell "
                          f"({DESIGN} §4)")
    heartbeat = entry.get("heartbeat", "gateway")
    if heartbeat != "gateway":
        raise ConfigError(f"{where}.heartbeat: {heartbeat!r}; only `gateway` is defined ({DESIGN} §5.7)")
    bio_source = entry.get("bio_source", "pushed")
    if bio_source != "pushed":
        raise ConfigError(f"{where}.bio_source: {bio_source!r}; only `pushed` is defined ({DESIGN} §4)")
    return Agent(name, _path(_string(entry, "token_file", where), base), heartbeat, bio_source)


def _room(pattern: str, entry: Any) -> RoomRule:
    where = f"rooms.{pattern!r}"
    try:
        compiled = RoomPattern(pattern)
    except InvalidRoomPattern as exc:
        raise ConfigError(f"{where}: {exc}") from None
    entry = _is_mapping(entry, where)
    unknown = sorted(set(entry) - _PARAM_FIELDS - {"agents"})
    if unknown:
        raise ConfigError(f"{where}: unknown settings {unknown}")
    agents = entry.get("agents")
    if not isinstance(agents, list) or not agents or not all(isinstance(a, str) for a in agents):
        raise ConfigError(f"{where}.agents: a non-empty list of agent names")
    if len(set(agents)) != len(agents):
        raise ConfigError(f"{where}.agents: names repeat")
    values = {k: v for k, v in entry.items() if k != "agents"}
    for key, value in values.items():
        expected = _PARAM_TYPES[key]
        ok = (isinstance(value, bool) if expected is bool
              else isinstance(value, str) if expected is str
              else isinstance(value, (int, float)) and not isinstance(value, bool)
              and (expected is float or isinstance(value, int)))
        if not ok:
            raise ConfigError(f"{where}.{key}: {value!r} is not {expected.__name__}")
    try:
        params = Params(**values)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{where}: {exc}") from None
    return RoomRule(compiled, tuple(agents), params)


def _listen(entry: Any, base: Path) -> Listen:
    where = "gateway"
    entry = _is_mapping(entry, where)
    _only_keys(entry, ("listen", "tls"), where)
    address = entry.get("listen", Listen.address)
    if not isinstance(address, str) or ":" not in address:
        raise ConfigError(f"{where}.listen: a `host:port` address")
    port = address.rpartition(":")[2]
    if not port.isdigit() or int(port) > 65535:  # 0 is valid: the kernel picks (tests, a proxy in front)
        raise ConfigError(f"{where}.listen: {port!r} is not a port (0-65535)")
    tls = entry.get("tls")
    if tls is None:
        return Listen(address, None)
    tls = _is_mapping(tls, f"{where}.tls")
    _only_keys(tls, ("cert", "key"), f"{where}.tls")
    return Listen(address, (_path(_string(tls, "cert", f"{where}.tls"), base),
                            _path(_string(tls, "key", f"{where}.tls"), base)))


def _classifier(entry: Any, base: Path) -> ClassifierConfig:
    where = "classifier"
    entry = _is_mapping(entry, where)
    _only_keys(entry, ("command", "working_directory"), where)
    command = entry.get("command", list(DEFAULT_CLASSIFIER_COMMAND))
    if not isinstance(command, list) or not command or not all(isinstance(c, str) and c for c in command):
        raise ConfigError(f"{where}.command: a non-empty list of strings")
    cwd = entry.get("working_directory")
    if cwd is not None and not isinstance(cwd, str):
        raise ConfigError(f"{where}.working_directory: not a string")
    return ClassifierConfig(tuple(command), None if cwd is None else _path(cwd, base))


def _cross_check(config: Config) -> None:
    for server in config.servers.values():
        for bot in server.bots.values():
            if bot.hosted and bot.agent not in config.agents:
                raise ConfigError(f"servers.{server.name}.bots.{bot.agent}: has a token_file but no agents.{bot.agent} "
                                  "entry; a bot the gateway posts as belongs to an agent it wakes")
            if not bot.hosted and bot.agent in config.agents:
                raise ConfigError(f"servers.{server.name}.bots.{bot.agent}: no token_file, yet agents.{bot.agent} exists; "
                                  "the gateway cannot post as a bot it has no token for")
    known = set(config.agents) | {a for s in config.servers.values() for a in s.bots}
    for rule in config.rooms:
        unknown = [a for a in rule.agents if a not in known]
        if unknown:
            raise ConfigError(f"rooms.{rule.pattern.raw!r}.agents: {unknown} are neither agents nor bots on any server")


# ── helpers ─────────────────────────────────────────────────────────────────


def _is_mapping(value: Any, where: str) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: not a mapping")
    return value


def _mapping(parent: dict, key: str, where: str = "the document") -> dict:
    """The section, ``{}`` when absent or null; any other value must be a mapping, so a
    ``rooms: []`` is refused rather than read as no rooms."""
    value = parent.get(key)
    if value is None:
        value = {}
    return _is_mapping(value, f"{where}.{key}" if where != "the document" else key)


def _only_keys(entry: dict, allowed: tuple[str, ...], where: str) -> None:
    unknown = sorted(set(entry) - set(allowed))
    if unknown:
        raise ConfigError(f"{where}: unknown keys {unknown}")


def _string(entry: dict, key: str, where: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{where}.{key}: a non-empty string is required")
    return value


def _names(value: Any, where: str) -> frozenset[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ConfigError(f"{where}: a list of names")
    return frozenset(value)


def _path(text: str, base: Path) -> Path:
    path = Path(text).expanduser()
    return path if path.is_absolute() else base / path
