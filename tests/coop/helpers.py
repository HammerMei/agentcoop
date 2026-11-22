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

from aiohttp.test_utils import TestServer

from coop.client import InProcessDoorbell
from coop.connectors.base import MemberInfo, Membership, Posted, RoomInfo
from coop.connectors.normalize import Names, mentions_in
from coop.doorbell.api import build_app
from coop.room.clock import Clock
from coop.room.controls import parse_controls
from coop.room.core import RoomCore
from coop.room.gateway import Gateway
from coop.room.model import MAIN_THREAD, Attachment, Member, Params, Post, PostKind
from coop.room.platform import RefusedPost
from coop.room.roster import Roster
from coop.room.trace import Trace
from coop.room.words import count_words
from tools.fakeagent.agent import FakeAgent, Script, Turn, done_everywhere
from tools.fakeagent.sim import FakePlatform, InjectedFailures, ScriptedClassifier, SimClock

__all__ = ["FAST", "Sandbox", "Doorbell", "Script", "Turn", "done_everywhere", "reply", "replies", "item",
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
        roster: Roster | None = None,
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
        self.gw = self.add_gateway("g1", hosted or list(bots), agents or list(bots), self.bot, self.classifier, params,
                                   roster=roster)
        self.gateway = self.gateways[0]

    def add_gateway(self, name, hosted, agents=None, agent: FakeAgent | None = None, classifier=None,
                    params=None, roster: Roster | None = None) -> RoomCore:
        gateway = Gateway(self.clock, self.trace, name=name)
        roster = roster or Roster.from_members(self.room.members().values(), agents or list(hosted))
        core = RoomCore(
            self.room.name, self.room, self.clock, self.trace, hosted, roster,
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

    def add_room(self, name: str, hosted: list[str], humans=(("H", "owner"),)) -> FakePlatform:
        """A second room on the first gateway, hosting ``hosted`` there."""
        room = FakePlatform(name, members(tuple(hosted), humans), self.clock, self.trace)
        roster = Roster.from_members(room.members().values(), hosted)
        core = RoomCore(name, room, self.clock, self.trace, hosted, roster, self.params, self.classifier,
                        name=self.gateway.name)
        self.gateway.add_room(core)
        room.cores.append(core)
        core.start()
        return room

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


# ── the doorbell over HTTP ──────────────────────────────────────────────────


class _KeepHandlers(TestServer):
    """aiohttp's test server cancels a handler when its client disconnects; a production
    runner does not by default (``handler_cancellation=False``). This server keeps
    the default, so the long-poll's behaviour with a client gone is the daemon's."""

    async def _make_runner(self, **kwargs):
        kwargs["handler_cancellation"] = False
        return await super()._make_runner(**kwargs)


class Doorbell:
    """One gateway behind the doorbell API on a real port, for the raw-HTTP tests and the
    fake agent's HTTP client. ``tokens`` maps a bearer token to its agent."""

    def __init__(self, gateway: Gateway, tokens: dict[str, str], rules_text: str | None = "rules",
                 tls: ssl.SSLContext | None = None, cancel_handlers: bool = True) -> None:
        self.tokens = tokens
        self.tls = tls
        server = TestServer if cancel_handlers else _KeepHandlers
        self.server = server(build_app(gateway, tokens, rules_text), scheme="https" if tls else "http")

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


# ── a fake connector ────────────────────────────────────────────────────────


class FakeConnector:
    """A chat server in memory, behind the :class:`~coop.connectors.base.Connector`
    protocol: rooms with members, posts with ``(time_ms, id)`` positions, files by
    id, and one event per bot connection in a room, as the real platforms deliver."""

    name = "fake"

    def __init__(self, on_event=None, names: dict[str, str] | None = None) -> None:
        self.on_event = on_event
        self.names = Names(names or {})
        self.rooms: dict[str, RoomInfo] = {}
        self.membership: dict[str, list[MemberInfo]] = {}
        self.posts: dict[str, list[Post]] = {}
        self.files: dict[str, bytes] = {}
        self.bots: dict[str, str] = {}  # agent -> token
        self.rosters: dict[str, Roster] = {}
        self.refusing: set[str] = set()
        self.typed: list[tuple[str, str, str | None]] = []
        self.started = False
        self._seq = 0
        self.now_ms = 1_000_000

    # test-side setup
    def add_room(self, room_id: str, name: str, members: list[MemberInfo], is_dm: bool = False) -> RoomInfo:
        self.rooms[room_id] = RoomInfo(room_id, name, is_dm)
        self.membership[room_id] = list(members)
        self.posts.setdefault(room_id, [])
        return self.rooms[room_id]

    def _hint(self, room_id: str, username: str) -> PostKind | None:
        for m in self.membership.get(room_id, []):
            if m.username == username:
                return "bot" if m.is_bot else None
        return None

    def make_post(self, room_id: str, username: str, text: str, thread: str | None = None,
                  files: tuple[tuple[str, str, bytes], ...] = ()) -> Post:
        """A post as the platform would store it, normalised with the room's roster."""
        self._seq += 1
        self.now_ms += 1
        post_id = f"m{self._seq}"
        attachments = []
        for name, content_type, data in files:
            file_id = f"f{len(self.files) + 1}"
            self.files[file_id] = data
            attachments.append(Attachment(file_id, name, content_type, len(data)))
        roster = self.rosters.get(room_id) or Roster(())
        author = self.names.agent_of(username)
        text = self.names.inbound(text)
        known = {self.names.agent_of(m.username) for m in self.membership.get(room_id, [])} | set(roster.bots)
        post = Post(
            id=post_id, position=(self.now_ms, post_id), time=self.now_ms / 1000, thread=thread or post_id,
            author=author, text=text, kind=roster.kind_of(author, self._hint(room_id, username)),
            mentions=mentions_in(text, known), controls=parse_controls(text), words=count_words(text),
            author_id=f"u_{username}", attachments=tuple(attachments),
        )
        self.posts[room_id].append(post)
        return post

    async def human(self, room_id: str, username: str, text: str, thread: str | None = None, **more) -> Post:
        """A person posts: every bot connection in the room delivers its copy."""
        post = self.make_post(room_id, username, text, thread, **more)
        await self._deliver(room_id, post)
        return post

    async def _deliver(self, room_id: str, post: Post) -> None:
        if self.on_event is None:
            return
        for agent in self.bots:
            if any(m.username == self.names.username_of(agent) for m in self.membership[room_id]):
                await self.on_event(Posted(room_id, post, seen_by=agent))

    async def join(self, room_id: str, agent: str) -> None:
        self.membership[room_id].append(MemberInfo(self.names.username_of(agent), is_bot=True))
        if self.on_event is not None and agent in self.bots:
            await self.on_event(Membership(room_id, agent, joined=True))

    async def leave(self, room_id: str, agent: str) -> None:
        username = self.names.username_of(agent)
        self.membership[room_id] = [m for m in self.membership[room_id] if m.username != username]
        if self.on_event is not None and agent in self.bots:
            await self.on_event(Membership(room_id, agent, joined=False))

    # Connector
    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def add_bot(self, agent: str, token: str) -> list[RoomInfo]:
        self.bots[agent] = token
        username = self.names.username_of(agent)
        return [info for rid, info in self.rooms.items() if any(m.username == username for m in self.membership[rid])]

    async def remove_bot(self, agent: str) -> None:
        self.bots.pop(agent, None)

    def watch(self, room_id: str, roster: Roster) -> None:
        self.rosters[room_id] = roster

    def unwatch(self, room_id: str) -> None:
        self.rosters.pop(room_id, None)

    async def room(self, room_id: str) -> RoomInfo:
        return self.rooms[room_id]

    async def members(self, room_id: str) -> list[MemberInfo]:
        return list(self.membership[room_id])

    async def history(self, room_id: str, after=None, before=None, limit: int = 60) -> list[Post]:
        posts = [p for p in self.posts[room_id]
                 if (after is None or p.position > after) and (before is None or p.position < before)]
        return posts[-limit:]

    async def post(self, agent: str, room_id: str, text: str, thread: str | None) -> Post:
        if agent not in self.bots:
            raise PermissionError(agent)
        if thread in self.refusing:
            raise RefusedPost(thread)
        post = self.make_post(room_id, self.names.username_of(agent), self.names.outbound(text), thread)
        await self._deliver(room_id, post)  # the echo, like the platform's
        return post

    async def fetch_file(self, agent: str, file_id: str) -> bytes:
        return self.files[file_id]

    async def typing(self, agent: str, room_id: str, thread: str | None) -> None:
        self.typed.append((agent, room_id, thread))
