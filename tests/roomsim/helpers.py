"""Builders for the roomsim tests.

They live here and not in ``tests/helpers.py`` for the reason `CLAUDE.md` names as the
only one that carries weight: the simulator imports nothing from the gateway except
the backend adapters, and the gateway's shared helpers import half the gateway. One
helper serving both layers would fuse two things that are independent. The one
double both layers share, ``MockAgentBackend``, is imported from ``tests/helpers.py``.

Every builder runs the real constructors (``Room``, ``Simulation``, ``SimGateway``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from tools.roomsim.bots import ScriptedBot, done_everywhere
from tools.roomsim.classifier import ScriptedClassifier
from tools.roomsim.gateway import SimGateway
from tools.roomsim.model import MAIN_THREAD, Member, Params, TurnInput
from tools.roomsim.room import Room
from tools.roomsim.sim import Simulation
from tools.roomsim.trace import Trace

# Fast, eager defaults so a test runs in a few simulated seconds. Real values are open
# decisions (spec section 9); nothing here claims to be one.
FAST = Params(
    rate_limit_posts=4,
    rate_limit_words=400,
    rate_limit_seconds=300,
    cooldown_seconds=0,
    sweep_interval_seconds=10,
    catch_up_post_threshold=3,
    catch_up_age_seconds=60,
    control_ttl_days=1,
    classifier_decision_ttl_seconds=100,
    failure_backoff_seconds=20,
    failure_backoff_max_seconds=60,
    consecutive_invalid_limit=2,
)


@dataclass
class Rule:
    reply: object
    bot: str | None = None
    source: str | None = None
    thread: str | None = None
    when: Callable[[TurnInput], bool] | None = None
    once: bool = False
    used: int = 0

    def matches(self, turn: TurnInput) -> bool:
        if self.once and self.used:
            return False
        return (
            (self.bot is None or self.bot == turn.bot)
            and (self.source is None or self.source == turn.source)
            and (self.thread is None or self.thread == turn.starting_thread)
            and (self.when is None or self.when(turn))
        )


@dataclass
class Script:
    """A handler for :class:`ScriptedBot`: the first matching rule answers, else
    ``done`` in every must-reply thread. ``reply`` may be a dict, raw text, a
    :class:`BotOutput`, or a callable taking the turn."""

    rules: list[Rule] = field(default_factory=list)
    fallback: Callable[[TurnInput], object] = done_everywhere

    def on(self, reply, **match) -> Script:
        self.rules.append(Rule(reply, **match))
        return self

    def __call__(self, turn: TurnInput):
        for rule in self.rules:
            if rule.matches(turn):
                rule.used += 1
                return rule.reply(turn) if callable(rule.reply) else rule.reply
        return self.fallback(turn)


def members(bots=("a", "b", "c"), humans=(("H", "owner"),), extra=()) -> list[Member]:
    found = [Member(name, role) for name, role in humans]
    found += [Member(b, "bot", display_name=b.upper(), nickname=f"{b}-nick", profile=f"{b} profile") for b in bots]
    return found + list(extra)


class Sandbox:
    """One room, one simulation, one gateway hosting every bot unless told otherwise."""

    def __init__(
        self,
        bots=("a", "b", "c"),
        humans=(("H", "owner"),),
        params: Params = FAST,
        script: Callable[[TurnInput], object] | None = None,
        classifier: ScriptedClassifier | None = None,
        hosted: list[str] | None = None,
        agents: list[str] | None = None,
        extra_members: tuple[Member, ...] = (),
        is_dm: bool = False,
        start_time: float = 0.0,
    ) -> None:
        self.room = Room("team", members(bots, humans, extra_members), is_dm=is_dm)
        self.sim = Simulation(self.room, start_time)
        self.params = params
        self.bot = ScriptedBot(script or Script())
        self.classifier = classifier if classifier is not None else ScriptedClassifier()
        self.gw = self.add_gateway("g1", hosted or list(bots), agents or list(bots), self.bot, self.classifier, params)

    def add_gateway(self, name, hosted, agents=None, runner=None, classifier=None, params=None) -> SimGateway:
        gw = SimGateway(
            self.sim, name, hosted, agents or list(hosted), params or self.params,
            runner or self.bot, classifier if classifier is not None else self.classifier,
        )
        self.sim.add_gateway(gw)
        return gw

    @property
    def trace(self) -> Trace:
        return self.sim.trace

    async def human(self, text: str, thread: str = MAIN_THREAD, author: str = "H"):
        return await self.sim.post(author, text, thread)

    async def run(self, seconds: float) -> None:
        await self.sim.run_for(seconds)

    def posts_by(self, bot: str, thread: str | None = None) -> list:
        return [p for p in self.room.posts if p.author == bot and p.kind == "bot"
                and (thread is None or p.thread == thread)]

    def turns(self, bot: str, source: str | None = None) -> list:
        events = self.trace.of("turn_start", bot=bot)
        return [e for e in events if source is None or e.source == source]


def reply(to, text, thread=None, **more) -> dict:
    item = {"to": list(to), "text": text, **more}
    if thread is not None:
        item["thread"] = thread
    return {"replies": [item]}


def replies(*items) -> dict:
    return {"replies": list(items)}


def item(thread=None, to=None, text=None, control=None, done=False) -> dict:
    found = {}
    if thread is not None:
        found["thread"] = thread
    if to is not None:
        found["to"] = list(to)
    if text is not None:
        found["text"] = text
    if control is not None:
        found["control"] = control
    if done:
        found["done"] = True
    return found
