"""The gateway over its rooms: one :class:`~coop.room.core.RoomCore` per room, and
what an agent holds across rooms (design §3.1): its bio and when it last polled.

The doorbell (a later increment) and the in-process client (``coop.client``) call
:meth:`poll`, :meth:`read`, :meth:`send`, :meth:`done` and :meth:`bio` here; a wake
id names its room, so ``read`` and ``done`` take the wake alone (design §3.2), and the
gateway records ``last_seen`` on a poll. A wake issued by a room is announced to the
agent's listeners, if any, so an in-process agent can poll at once; the doorbell's
long-poll registers one too.

Two namespaces meet here (design §2). An *agent* is the gateway's identity for a
token: the ``agents:`` entry, the bio, ``last_seen``. A room's core works in the
platform's namespace, where that agent is its bot account's *username*. The binding
is per room (:meth:`bind`): an agent may have one username on one server and another
on the next, and a username not bound to an agent in a room is simply not that
agent's there, whatever it is called.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace

from ..errors import NoLiveLease, RoomUnresolved
from .clock import Clock
from .core import RoomCore
from .model import Attachment, Post, Reply, Sent, Wake
from .trace import Trace

Listener = Callable[[Wake], Awaitable[None]]

# The version of the room-rules document (``coop/rules.md``) every wake carries
# (design §3.3): an agent refetches the document when a wake carries a newer one.
# Bumped with every change to the document; a test pins the pair.
RULES_VERSION = 3


@dataclass
class AgentState:
    bio: str | None = None
    bio_version: int = 0
    last_seen: float | None = None  # the agent's last wake poll (design §3.2, liveness)
    listeners: list[Listener] = field(default_factory=list)


class Gateway:
    def __init__(self, clock: Clock, trace: Trace | None = None, name: str = "g1",
                 rules_version: int = RULES_VERSION) -> None:
        self.clock = clock
        self.trace = trace if trace is not None else Trace()
        self.name = name
        # The version of the room-rules document every wake carries (design §3.3).
        self.rules_version = rules_version
        self.rooms: dict[str, RoomCore] = {}
        self.agents: dict[str, AgentState] = {}
        # Bios by agent; each room's core holds its copy by username (``_push_bio``).
        self.bios: dict[str, str] = {}
        # The per-room binding, both ways: (room, agent) -> username, (room, username) -> agent.
        self._username: dict[tuple[str, str], str] = {}
        self._agent: dict[tuple[str, str], str] = {}
        # Attachment handles (design §3.3): one per (room, platform file id), minted when a
        # read first delivers the post; a fetch names the handle, never the platform id.
        self._handles: dict[str, tuple[str, Attachment]] = {}
        self._handle_of: dict[tuple[str, str], str] = {}
        # Told after every bio push: the daemon saves the agents' document then.
        self.on_bio: Callable[[], None] | None = None

    # ── rooms and the binding ───────────────────────────────────────────────

    def add_room(self, core: RoomCore) -> RoomCore:
        """Register a room. A bot the core already hosts is bound to an agent of the
        same name: the simulated rooms and the in-process tests have one namespace. The
        daemon builds its cores with no hosted bots and binds each through :meth:`bind`."""
        if core.room in self.rooms:
            raise ValueError(f"room {core.room!r} is already on this gateway")
        core.on_wake = self._wake_issued
        core.rules_version = self.rules_version
        self.rooms[core.room] = core
        for bot in core.hosted:
            self.bind(core.room, bot, bot)
        return core

    def remove_room(self, room: str) -> RoomCore | None:
        core = self.rooms.pop(room, None)
        if core is not None:
            for key in [k for k in self._username if k[0] == room]:
                self._agent.pop((room, self._username.pop(key)), None)
        return core

    def bind(self, room: str, agent: str, username: str) -> None:
        """``agent`` is ``username`` in ``room`` from now on. The room's core must host
        ``username``; the bot's bio, if the agent pushed one, is in the core's copy."""
        core = self._room(room)
        if username not in core.hosted:
            raise ValueError(f"{username!r} is not hosted in room {room!r}")
        taken = self._agent.get((room, username))
        if taken is not None and taken != agent:
            raise ValueError(f"{username!r} in room {room!r} is {taken!r}'s bot, not {agent!r}'s")
        bound = self._username.get((room, agent))
        if bound is not None and bound != username:  # a rebind would leave the old username's side behind
            raise ValueError(f"{agent!r} is already {bound!r} in room {room!r}; unbind first")
        self._username[(room, agent)] = username
        self._agent[(room, username)] = agent
        self.agents.setdefault(agent, AgentState())
        bio = self.bios.get(agent)
        if bio is not None:
            core.bios[username] = bio

    def unbind(self, room: str, agent: str) -> None:
        username = self._username.pop((room, agent), None)
        if username is not None:
            self._agent.pop((room, username), None)
            core = self.rooms.get(room)
            if core is not None:
                core.bios.pop(username, None)

    def username_of(self, room: str, agent: str) -> str | None:
        """The agent's username in ``room``, ``None`` when it is not bound there."""
        return self._username.get((room, agent))

    def agent_of(self, room: str, username: str) -> str | None:
        return self._agent.get((room, username))

    def _bot(self, core: RoomCore, agent: str) -> str | None:
        return self._username.get((core.room, agent))

    def _cores_of(self, agent: str) -> list[tuple[RoomCore, str]]:
        """The rooms the agent is bound in, each with its username there."""
        found = []
        for core in self.rooms.values():
            bot = self._bot(core, agent)
            if bot is not None:
                found.append((core, bot))
        return found

    def start(self) -> None:
        for core in self.rooms.values():
            core.start()

    # ── wakes ───────────────────────────────────────────────────────────────

    def subscribe(self, agent: str, listener: Listener) -> None:
        """Register an in-process listener told of every wake issued for ``agent``. It
        runs as its own clock event after the issuing work finishes, and it polls like
        any client: the lease starts at its poll, not at the announcement."""
        self.agents.setdefault(agent, AgentState()).listeners.append(listener)

    async def _wake_issued(self, wake: Wake) -> None:
        agent = self._agent.get((wake.room, wake.bot))
        if agent is None:  # a bot no agent is bound to: nobody to tell
            return
        for listener in self.agents.setdefault(agent, AgentState()).listeners:
            self.clock.at(0, listener, wake)

    def poll(self, agent: str) -> Wake | None:
        """The agent's next pending wake across its rooms, its lease started; ``None``
        when nothing is pending. Every poll is a liveness signal."""
        self.agents.setdefault(agent, AgentState()).last_seen = self.clock.now()
        for core, bot in self._cores_of(agent):
            wake = core.poll(bot)
            if wake is not None:
                return wake
        return None

    # ── the turn ────────────────────────────────────────────────────────────

    def _room(self, room: str) -> RoomCore:
        try:
            return self.rooms[room]
        except KeyError:
            raise RoomUnresolved(f"no room {room!r}") from None

    def _room_of_wake(self, agent: str, wake_id: str) -> tuple[RoomCore, str]:
        """The room whose live lease for ``agent`` is ``wake_id``, and the agent's
        username there."""
        for core, bot in self._cores_of(agent):
            state = core.volatile.get(bot)
            run = state.turn if state is not None else None
            if run is not None and run.leased and run.wake.wake_id == wake_id:
                return core, bot
        raise NoLiveLease(f"{agent} holds no live lease on wake {wake_id!r}")

    # ── attachments ─────────────────────────────────────────────────────────

    def deliverable(self, agent: str, room: str, post: Post) -> Post:
        """The post as the doorbell delivers it to ``agent``: its author labelled as that
        agent sees them, its attachments named by handle."""
        core = self._room(room)
        bot = self._bot(core, agent)
        if bot is None:
            raise PermissionError(f"{agent} is not in the roster of room {room!r}")
        attachments = tuple(replace(a, id=self._handle(room, a)) for a in post.attachments)
        return replace(post, author_role=core.role_label(bot, post.author), attachments=attachments)

    def _handle(self, room: str, attachment: Attachment) -> str:
        key = (room, attachment.id)
        handle = self._handle_of.get(key)
        if handle is None:
            handle = self._handle_of[key] = f"a{len(self._handle_of) + 1}"
            self._handles[handle] = (room, attachment)
        return handle

    async def fetch_attachment(self, agent: str, handle: str) -> tuple[Attachment, bytes]:
        """``KeyError`` for a handle no read minted, ``PermissionError`` when the room's
        roster does not list the agent."""
        room, attachment = self._handles[handle]
        if not self.hosts(agent, room):
            raise PermissionError(f"{agent} is not in the roster of room {room!r}")
        return attachment, await self._room(room).platform.fetch_attachment(attachment.id)

    def starting_thread(self, agent: str, wake_id: str) -> str:
        """Where a wake's send goes when the agent names no thread (design §3.3)."""
        core, bot = self._room_of_wake(agent, wake_id)
        return core.volatile[bot].turn.wake.starting_thread

    def read(self, agent: str, wake_id: str) -> tuple[Wake, list[Post]]:
        core, bot = self._room_of_wake(agent, wake_id)
        return core.read(bot, wake_id)

    def read_room(self, agent: str, room: str, after: str | None = None) -> list[Post]:
        core = self._room(room)
        bot = self._bot(core, agent)
        if bot is None:
            raise PermissionError(f"{agent} is not in the roster of room {room!r}")
        return core.read_room(bot, after)

    async def send(self, agent: str, reply: Reply, room: str | None = None) -> Sent:
        core, bot = self.room_for(agent, reply, room)
        return await core.send(bot, reply)

    def room_for(self, agent: str, reply: Reply, room: str | None = None) -> tuple[RoomCore, str]:
        """The room a send lands in, and the agent's username there. A wake's send goes
        to the wake's room; an unsolicited send names its room. A wake id with no live
        lease falls back to ``room`` (the send is then unsolicited, design §3.5)."""
        if reply.wake_id is not None:
            try:
                core, bot = self._room_of_wake(agent, reply.wake_id)
                if room is not None and room != core.room:
                    # ``--room`` on a wake's send is the root of the wake's room (design §3.3)
                    raise RoomUnresolved(f"wake {reply.wake_id!r} is in room {core.room!r}, not {room!r}")
                return core, bot
            except NoLiveLease:
                # the lease is gone (expired, or the gateway restarted); the wake id still
                # names its room, so the late reply lands there unsolicited (design §3.4)
                if room is not None:
                    return self._bound(agent, room)
                found = next(((c, b) for c, b in self._cores_of(agent) if c.minted(reply.wake_id)), None)
                if found is None:
                    raise
                return found
        if room is not None:
            return self._bound(agent, room)
        # ``--thread T`` alone: the thread names its room, among the rooms this gateway
        # serves the agent in (design §3.3)
        found = [(c, b) for c, b in self._cores_of(agent) if c.platform.thread_posts(reply.thread)]
        if not found:
            raise RoomUnresolved(f"no room of {agent}'s has a thread {reply.thread!r}")
        if len(found) > 1:
            raise RoomUnresolved(f"thread {reply.thread!r} is in several of {agent}'s rooms "
                                 f"({', '.join(c.room for c, _ in found)}): name the room")
        return found[0]

    def _bound(self, agent: str, room: str) -> tuple[RoomCore, str]:
        core = self._room(room)
        bot = self._bot(core, agent)
        if bot is None:
            raise PermissionError(f"{agent} is not in the roster of room {room!r}")
        return core, bot

    async def done(self, agent: str, wake_id: str, answered: tuple[str, ...] = (),
                   failed: str | None = None) -> bool:
        """``False`` for a wake with no live lease anywhere: logged by the room that
        last held it is not knowable, so it is logged here and nothing moves."""
        try:
            core, bot = self._room_of_wake(agent, wake_id)
        except NoLiveLease:
            # no room owns the wake, so there is no username to name: the agent's name is what is known
            self.trace.add(self.clock.now(), "done_stale", gateway=self.name, bot=agent, wake=wake_id)
            return False
        return await core.done(bot, wake_id, answered, failed)

    def hosts(self, agent: str, room: str) -> bool:
        """Whether this gateway serves ``agent`` in ``room``: the roster check the
        doorbell makes before a read or an unsolicited send names a room (design §3.2,
        "the API answers a token only for rooms whose roster lists the agent")."""
        core = self.rooms.get(room)
        return core is not None and self._bot(core, agent) in core.hosted

    def holds_lease(self, agent: str) -> bool:
        return any(s.turn is not None and s.turn.leased
                   for core, bot in self._cores_of(agent) if (s := core.volatile.get(bot)) is not None)

    def online(self, agent: str, wait: float) -> bool:
        """Liveness from wake polls alone (design §3.2): an agent is offline when its last
        poll is older than two long-poll waits and it holds no live lease."""
        state = self.agents.get(agent)
        if state is None or state.last_seen is None:
            return False
        return self.clock.now() - state.last_seen <= 2 * wait or self.holds_lease(agent)

    def bio(self, agent: str, text: str) -> int:
        """Store the agent's bio; returns its new version (design §3.6 step 3)."""
        state = self.agents.setdefault(agent, AgentState())
        state.bio = text
        state.bio_version += 1
        self.set_bio(agent, text)
        if self.on_bio is not None:
            self.on_bio()
        return state.bio_version

    def set_bio(self, agent: str, text: str) -> None:
        """The agent's bio, into every room it is bound in; no version, no save."""
        self.bios[agent] = text
        for core, bot in self._cores_of(agent):
            core.bios[bot] = text

    def status(self) -> dict:
        return {
            "gateway": self.name,
            "agents": {a: {"bio_version": s.bio_version, "last_seen": s.last_seen} for a, s in self.agents.items()},
            "rooms": {r: c.status() for r, c in self.rooms.items()},
        }
