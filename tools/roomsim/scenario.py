"""Scenario files: a room, its gateways, scripted human events, and (for scripted runs)
the bots' answers, in YAML. ``load`` builds the :class:`~tools.roomsim.sim.Simulation`;
``run`` drives it to the horizon.

Shape::

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
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml

from .bots import BotRunner, Mixed, ScriptedBot, done_everywhere
from .classifier import ScriptedClassifier
from .gateway import SimGateway
from .model import Decision, Member, Params, TurnInput
from .room import Room
from .sim import Simulation


@dataclass
class ScriptRule:
    reply: object
    source: str | None = None
    thread: str | None = None
    when_from: str | None = None
    once: bool = False
    used: int = 0

    def matches(self, turn: TurnInput) -> bool:
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

    def __call__(self, turn: TurnInput):
        for rule in self.rules.get(turn.bot, []):
            if rule.matches(turn):
                rule.used += 1
                return rule.reply
        return done_everywhere(turn)


@dataclass
class Scenario:
    name: str
    sim: Simulation
    horizon: float
    script: ScenarioScript


def _params(raw: dict | None, enforce: bool | None) -> Params:
    known = {f.name for f in fields(Params)}
    values = {k: v for k, v in (raw or {}).items() if k in known}
    unknown = set(raw or {}) - known
    if unknown:
        raise ValueError(f"unknown params: {sorted(unknown)}")
    if enforce is not None:
        values["enforce"] = enforce
    return Params(**values)


def _classifier(raw: dict | None) -> ScriptedClassifier:
    raw = raw or {}
    decisions = {int(k): Decision(add=tuple(v.get("add", ())), remove=tuple(v.get("remove", ())))
                 for k, v in (raw.get("decisions") or {}).items()}
    return ScriptedClassifier(decisions=decisions, latency=float(raw.get("latency", 0.5)),
                              failures={int(k): int(v) for k, v in (raw.get("failures") or {}).items()})


def load(path: str | Path, enforce: bool | None = None, runner: BotRunner | None = None,
         scripted_bots: frozenset[str] = frozenset()) -> Scenario:
    data = yaml.safe_load(Path(path).read_text())
    members = [Member(m["username"], m["role"], m.get("display_name", ""), m.get("nickname", ""), m.get("profile", ""),
                      m.get("private_profile", ""))
               for m in data["members"]]
    room = Room(data.get("room", data["name"]), members, is_dm=bool(data.get("dm", False)))
    sim = Simulation(room)
    params = _params(data.get("params"), enforce)
    script = ScenarioScript({
        bot: [ScriptRule(**rule) for rule in rules] for bot, rules in (data.get("bots") or {}).items()
    })
    bot_runner = runner or ScriptedBot(script)
    if runner is not None and scripted_bots:
        bot_runner = Mixed(runner, ScriptedBot(script), scripted_bots)
    bots = room.bots()
    for raw in data.get("gateways") or [{"name": "g1", "hosted": bots}]:
        sim.add_gateway(SimGateway(sim, raw["name"], raw["hosted"], raw.get("agents", bots), params,
                                   bot_runner, _classifier(data.get("classifier"))))
    for event in data.get("events") or []:
        _schedule(sim, event)
    return Scenario(data["name"], sim, float(data.get("horizon", 600)), script)


def _schedule(sim: Simulation, event: dict) -> None:
    at = float(event.get("at", 0))
    if "human" in event:
        sim.human(at, event["human"], event["text"], event.get("thread", "main"))
    elif "archive" in event:
        sim.at(at, sim.archive, event["archive"])
    elif "post" in event:  # a post from an account no gateway runs, e.g. an integration
        sim.at(at, sim.post, event["post"], event["text"], event.get("thread", "main"))
    else:
        raise ValueError(f"unknown event: {event}")


async def run(scenario: Scenario) -> Simulation:
    await scenario.sim.run_until(scenario.horizon)
    return scenario.sim
