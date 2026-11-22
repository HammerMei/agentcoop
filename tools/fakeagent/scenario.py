"""The simulator's scenario files (``tools/roomsim/scenario.py``) run through the room
core with the fake agent: the same YAML, the same room, the gateway core in place of
``SimGateway`` and the fake agent's ``read``/``send``/``done`` in place of the envelope.
This is what design §6.2's replay parity compares against the simulator.

Shape (unchanged)::

    name: handoff
    room: team
    members:
      - {username: H, role: owner}
      - {username: a, role: bot, profile: runs migrations}
    params: {rate_limit_posts: 6, enforce: true}
    gateways:
      - {name: g1, hosted: [a, b, c]}          # agents default to every bot member
    classifier: {latency: 0.5, decisions: {2: {add: [b]}}, failures: {1: 1}}
    events:
      - {at: 0, human: H, text: "@a run the migration; @b deploy after"}
      - {at: 120, archive: t2}
    bots:                                      # scripted answers, matched in order
      a:
        - {source: addressed, once: true, reply: {replies: [{to: [H, b], text: done}]}}
      b:
        - {when_from: a, reply: {replies: [{to: [H, a], text: deployed}]}}
    horizon: 600

A ``schedule`` in a reply is the simulator's scheduler stand-in and is ignored here
(FOLLOW_UP is out, design §1.2); ``follow_up_retry_cap`` in ``params`` likewise.
Classifier scripts are keyed by post seq in the file and by post id (``p<seq>``) here.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml

from coop.client import InProcessDoorbell
from coop.room.core import RoomCore
from coop.room.gateway import Gateway
from coop.room.model import Decision, Member, Params
from coop.room.roster import Roster
from coop.room.trace import Trace

from .agent import FakeAgent, Turn, done_everywhere
from .sim import FakePlatform, ScriptedClassifier, SimClock

SIMULATOR_ONLY_PARAMS = {"follow_up_retry_cap"}


@dataclass
class ScriptRule:
    reply: object
    source: str | None = None
    thread: str | None = None
    when_from: str | None = None
    once: bool = False
    used: int = 0

    def matches(self, turn: Turn) -> bool:
        if self.once and self.used:
            return False
        if self.source is not None and self.source != turn.source:
            return False
        if self.thread is not None and self.thread != turn.starting_thread:
            return False
        return self.when_from is None or any(p.author == self.when_from for p in turn.delivered)


@dataclass
class ScenarioScript:
    rules: dict[str, list[ScriptRule]] = field(default_factory=dict)

    def __call__(self, turn: Turn):
        for rule in self.rules.get(turn.bot, []):
            if rule.matches(turn):
                rule.used += 1
                return rule.reply
        return done_everywhere(turn)


@dataclass
class Scenario:
    name: str
    clock: SimClock
    trace: Trace
    room: FakePlatform
    gateways: list[Gateway]
    agents: list[FakeAgent]
    horizon: float

    @property
    def cores(self) -> list[RoomCore]:
        return [core for gw in self.gateways for core in gw.rooms.values()]


def _params(raw: dict | None, enforce: bool | None, overrides: dict[str, str] | None = None) -> Params:
    """The scenario's params, then ``overrides`` (``--param name=value``) parsed by the
    field's default type."""
    known = {f.name: f.default for f in fields(Params)}
    values = {k: v for k, v in (raw or {}).items() if k in known}
    for name, text in (overrides or {}).items():
        if name not in known:
            raise ValueError(f"unknown param: {name}")
        values[name] = _parse_param(text, known[name])
    unknown = set(raw or {}) - set(known) - SIMULATOR_ONLY_PARAMS
    if unknown:
        raise ValueError(f"unknown params: {sorted(unknown)}")
    if enforce is not None:
        values["enforce"] = enforce
    return Params(**values)


def _parse_param(text: str, default) -> object:
    if isinstance(default, bool):
        if text.lower() not in ("true", "false"):
            raise ValueError(f"a boolean param takes true or false, not {text!r}")
        return text.lower() == "true"
    if isinstance(default, int):
        return int(text)
    if isinstance(default, float):
        return float(text)
    return text


def _classifier(raw: dict | None) -> ScriptedClassifier:
    raw = raw or {}
    decisions = {f"p{k}": Decision(add=tuple(v.get("add", ())), remove=tuple(v.get("remove", ())))
                 for k, v in (raw.get("decisions") or {}).items()}
    return ScriptedClassifier(decisions=decisions, latency=float(raw.get("latency", 0.5)),
                              failures={f"p{k}": int(v) for k, v in (raw.get("failures") or {}).items()})


def load(path: str | Path, enforce: bool | None = None, params: dict[str, str] | None = None) -> Scenario:
    data = yaml.safe_load(Path(path).read_text())
    members = [Member(m["username"], m["role"], m.get("display_name", ""), m.get("nickname", ""), m.get("profile", ""))
               for m in data["members"]]
    clock, trace = SimClock(), Trace()
    room = FakePlatform(data.get("room", data["name"]), members, clock, trace, is_dm=bool(data.get("dm", False)))
    params = _params(data.get("params"), enforce, params)
    script = ScenarioScript({
        bot: [ScriptRule(**rule) for rule in rules] for bot, rules in (data.get("bots") or {}).items()
    })
    bots = room.bots()
    gateways, agents = [], []
    for raw in data.get("gateways") or [{"name": "g1", "hosted": bots}]:
        gateway = Gateway(clock, trace, name=raw["name"])
        roster = Roster.from_members(room.members().values(), raw.get("agents", bots))
        core = RoomCore(room.name, room, clock, trace, raw["hosted"], roster, params,
                        _classifier(data.get("classifier")), name=raw["name"])
        gateway.add_room(core)
        agent = FakeAgent(clock, script)
        for bot in raw["hosted"]:
            agent.attach(bot, InProcessDoorbell(gateway, bot))
            gateway.subscribe(bot, agent.on_wake)
        room.cores.append(core)
        gateway.start()
        gateways.append(gateway)
        agents.append(agent)
    for event in data.get("events") or []:
        _schedule(clock, room, event)
    return Scenario(data["name"], clock, trace, room, gateways, agents, float(data.get("horizon", 600)))


def _schedule(clock: SimClock, room: FakePlatform, event: dict) -> None:
    at = float(event.get("at", 0))
    if "human" in event:
        clock.at(at, room.post, event["human"], event["text"], event.get("thread", "main"))
    elif "archive" in event:
        clock.at(at, room.archive, event["archive"])
    elif "post" in event:  # a post from an account no gateway runs, e.g. an integration
        clock.at(at, room.post, event["post"], event["text"], event.get("thread", "main"))
    else:
        raise ValueError(f"unknown event: {event}")


async def run(scenario: Scenario) -> Scenario:
    await scenario.clock.run_until(scenario.horizon)
    return scenario
