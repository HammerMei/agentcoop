"""Builders for the room-core tests.

They live here and not in ``tests/helpers.py`` for the reason `CLAUDE.md` names as the
only one that carries weight: the core imports nothing from the frozen ``gateway``
package, and the gateway's shared helpers import half of it. One helper serving both
layers would fuse two things that are independent. The simulator's builders
(``tests/roomsim/helpers.py``) are the model for these; the simulator keeps its own
until design §6.2's replay parity has been run.

Every builder runs the real constructors (``RoomCore``, ``Gateway``, ``FakeAgent``).
The simulated environment (clock, room, scripted classifier) is the fake agent's own
(``tools/fakeagent/sim.py``), so the scenario runner and the tests build the same thing.
"""

from __future__ import annotations

import ssl
from collections.abc import Callable
from pathlib import Path

from aiohttp.test_utils import TestServer

from coop.client import InProcessDoorbell
from coop.doorbell.api import build_app
from coop.room.clock import Clock
from coop.room.core import RoomCore
from coop.room.gateway import Gateway
from coop.room.model import MAIN_THREAD, Member, Params, Post, PostKind
from coop.room.trace import Trace
from tools.fakeagent.agent import FakeAgent, Script, Turn, done_everywhere
from tools.fakeagent.sim import FakePlatform, InjectedFailures, ScriptedClassifier, SimClock

__all__ = ["FAST", "FIXTURES", "Sandbox", "Doorbell", "Script", "Turn", "done_everywhere", "reply", "replies", "item",
           "ScriptedClassifier", "InjectedFailures", "SimClock", "FakePlatform", "members"]

# Fast, eager defaults so a test runs in a few simulated seconds. Real values are open
# decisions (spec section 9); nothing here claims to be one. The lease outlives any
# scripted turn unless a test shortens it. Enforce mode is on here so the holds and
# refusals are visible; the product default is measure mode (design §1.1), and
# ``test_enforce`` runs both.
FAST = Params(
    rate_limit_posts=4,
    rate_limit_words=400,
    rate_limit_seconds=300,
    cooldown_seconds=0,
    sweep_interval_seconds=10,
    catch_up_idle_seconds=60,
    control_ttl_days=1,
    classifier_decision_ttl_seconds=100,
    failure_backoff_seconds=20,
    failure_backoff_max_seconds=60,
    consecutive_invalid_limit=2,
    lease_seconds=1000,
    enforce=True,
)


# ── the sandbox ─────────────────────────────────────────────────────────────


def members(bots=("a", "b", "c"), humans=(("H", "owner"),), extra=()) -> list[Member]:
    found = [Member(name, role) for name, role in humans]
    found += [Member(b, "bot", display_name=b.upper(), nickname=f"{b}-nick", profile=f"{b} profile") for b in bots]
    return found + list(extra)


class Sandbox:
    """One room, one clock, one gateway hosting every bot unless told otherwise, one
    fake agent answering every hosted bot's wakes from ``script``."""

    def __init__(
        self,
        bots=("a", "b", "c"),
        humans=(("H", "owner"),),
        params: Params = FAST,
        script: Callable[[Turn], object] | None = None,
        classifier: ScriptedClassifier | None = None,
        hosted: list[str] | None = None,
        agents: list[str] | None = None,
        extra_members: tuple[Member, ...] = (),
        is_dm: bool = False,
        start_time: float = 0.0,
        duration: float = 1.0,
        clock: Clock | None = None,
        attach_agent: bool = True,
    ) -> None:
        # ``clock`` is the simulated one unless a test runs on the event loop (the doorbell
        # tests); ``attach_agent=False`` leaves every wake for an HTTP client to poll.
        self.clock = clock if clock is not None else SimClock(start_time)
        self.attach_agent = attach_agent
        self.trace = Trace()
        self.room = FakePlatform("team", members(bots, humans, extra_members), self.clock, self.trace, is_dm=is_dm)
        self.params = params
        self.classifier = classifier if classifier is not None else ScriptedClassifier()
        self.gateways: list[Gateway] = []
        self.bot = FakeAgent(self.clock, script or Script(), duration)
        self.gw = self.add_gateway("g1", hosted or list(bots), agents or list(bots), self.bot, self.classifier, params)
        self.gateway = self.gateways[0]

    def add_gateway(self, name, hosted, agents=None, agent: FakeAgent | None = None, classifier=None,
                    params=None) -> RoomCore:
        gateway = Gateway(self.clock, self.trace, name=name)
        core = RoomCore(
            self.room.name, self.room, self.clock, self.trace, hosted, agents or list(hosted),
            params or self.params, classifier if classifier is not None else self.classifier, name=name,
        )
        gateway.add_room(core)
        agent = agent or self.bot
        for bot in hosted if self.attach_agent else ():
            agent.attach(bot, InProcessDoorbell(gateway, bot))
            gateway.subscribe(bot, agent.on_wake)
        self.room.cores.append(core)
        self.gateways.append(gateway)
        gateway.start()
        return core

    async def human(self, text: str, thread: str = MAIN_THREAD, author: str = "H",
                    attachments: tuple[tuple[str, str, bytes], ...] = ()) -> Post:
        return await self.room.post(author, text, thread, attachments=attachments)

    async def post(self, author: str, text: str, thread: str = MAIN_THREAD, kind: PostKind | None = None,
                   attachments: tuple[tuple[str, str, bytes], ...] = ()) -> Post:
        return await self.room.post(author, text, thread, kind, attachments)

    async def archive(self, thread: str) -> None:
        await self.room.archive(thread)

    async def run(self, seconds: float) -> None:
        await self.clock.run_for(seconds)

    def posts_by(self, bot: str, thread: str | None = None) -> list[Post]:
        return [p for p in self.room.posts if p.author == bot and p.kind == "bot"
                and (thread is None or p.thread == thread)]

    def turns(self, bot: str, source: str | None = None) -> list:
        events = self.trace.of("turn_start", bot=bot)
        return [e for e in events if source is None or e.source == source]


def reply(to, text, thread=None, **more) -> dict:
    found = {"to": list(to), "text": text, **more}
    if thread is not None:
        found["thread"] = thread
    return {"replies": [found]}


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


FIXTURES = Path(__file__).parent / "fixtures"


# ── the doorbell over HTTP ──────────────────────────────────────────────────


class Doorbell:
    """One gateway behind the doorbell API on a real port, for the raw-HTTP tests and the
    fake agent's HTTP client. ``tokens`` maps a bearer token to its agent."""

    def __init__(self, gateway: Gateway, tokens: dict[str, str], rules_text: str | None = "rules",
                 tls: ssl.SSLContext | None = None) -> None:
        self.tokens = tokens
        self.tls = tls
        self.server = TestServer(build_app(gateway, tokens, rules_text), scheme="https" if tls else "http")

    async def __aenter__(self) -> Doorbell:
        await self.server.start_server(ssl=self.tls)
        return self

    async def __aexit__(self, *exc) -> None:
        await self.server.close()

    @property
    def url(self) -> str:
        return str(self.server.make_url("")).rstrip("/")

    def headers(self, agent: str) -> dict[str, str]:
        token = next(t for t, a in self.tokens.items() if a == agent)
        return {"Authorization": f"Bearer {token}"}
