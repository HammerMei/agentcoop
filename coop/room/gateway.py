"""The gateway over its rooms: one :class:`~coop.room.core.RoomCore` per room, and
what an agent holds across rooms (design §3.1): its bio and when it last polled.

The doorbell (a later increment) and the in-process client (``coop.client``) call
:meth:`poll`, :meth:`read`, :meth:`send`, :meth:`done` and :meth:`bio` here; a wake
id names its room, so ``read`` and ``done`` take the wake alone (design §3.2), and the
gateway records ``last_seen`` on a poll. A wake issued by a room is announced to the
agent's listeners, if any, so an in-process agent can poll at once; the doorbell's
long-poll registers one too.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from ..errors import NoLiveLease
from .clock import Clock
from .core import RoomCore
from .model import Post, Reply, Wake
from .trace import Trace

Listener = Callable[[Wake], Awaitable[None]]


@dataclass
class AgentState:
    bio: str | None = None
    bio_version: int = 0
    last_seen: float | None = None  # the agent's last wake poll (design §3.2, liveness)
    listeners: list[Listener] = field(default_factory=list)


class Gateway:
    def __init__(self, clock: Clock, trace: Trace | None = None, name: str = "g1", rules_version: int = 1) -> None:
        self.clock = clock
        self.trace = trace if trace is not None else Trace()
        self.name = name
        # The version of the room-rules document every wake carries (design §3.3).
        self.rules_version = rules_version
        self.rooms: dict[str, RoomCore] = {}
        self.agents: dict[str, AgentState] = {}
        # Bios by agent, shared with every room's core (the classifier reads them).
        self.bios: dict[str, str] = {}

    def add_room(self, core: RoomCore) -> RoomCore:
        core.on_wake = self._wake_issued
        core.bios = self.bios
        core.rules_version = self.rules_version
        self.rooms[core.room] = core
        for bot in core.hosted:
            self.agents.setdefault(bot, AgentState())
        return core

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
        for listener in self.agents.setdefault(wake.bot, AgentState()).listeners:
            self.clock.at(0, listener, wake)

    def poll(self, agent: str) -> Wake | None:
        """The agent's next pending wake across its rooms, its lease started; ``None``
        when nothing is pending. Every poll is a liveness signal."""
        self.agents.setdefault(agent, AgentState()).last_seen = self.clock.now()
        for core in self.rooms.values():
            wake = core.poll(agent)
            if wake is not None:
                return wake
        return None

    # ── the turn ────────────────────────────────────────────────────────────

    def _room(self, room: str) -> RoomCore:
        try:
            return self.rooms[room]
        except KeyError:
            raise KeyError(f"no room {room!r}") from None

    def _room_of_wake(self, agent: str, wake_id: str) -> RoomCore:
        """The room whose live lease for ``agent`` is ``wake_id``."""
        for core in self.rooms.values():
            state = core.volatile.get(agent)
            run = state.turn if state is not None else None
            if run is not None and run.leased and run.wake.wake_id == wake_id:
                return core
        raise NoLiveLease(f"{agent} holds no live lease on wake {wake_id!r}")

    def read(self, agent: str, wake_id: str) -> tuple[Wake, list[Post]]:
        return self._room_of_wake(agent, wake_id).read(agent, wake_id)

    def read_room(self, agent: str, room: str) -> list[Post]:
        return self._room(room).read_room(agent)

    async def send(self, agent: str, reply: Reply, room: str | None = None) -> Post | None:
        """A wake's send goes to the wake's room; an unsolicited send names its room.
        A wake id with no live lease falls back to ``room`` (the send is then
        unsolicited, design §3.5)."""
        if reply.wake_id is not None:
            try:
                core = self._room_of_wake(agent, reply.wake_id)
            except NoLiveLease:
                if room is None:
                    raise
                core = self._room(room)
        elif room is None:
            raise ValueError("an unsolicited send names its room")
        else:
            core = self._room(room)
        return await core.send(agent, reply)

    async def done(self, agent: str, wake_id: str, answered: tuple[str, ...] = (),
                   failed: str | None = None) -> bool:
        """``False`` for a wake with no live lease anywhere: logged by the room that
        last held it is not knowable, so it is logged here and nothing moves."""
        try:
            core = self._room_of_wake(agent, wake_id)
        except NoLiveLease:
            self.trace.add(self.clock.now(), "done_stale", gateway=self.name, bot=agent, wake=wake_id)
            return False
        return await core.done(agent, wake_id, answered, failed)

    def bio(self, agent: str, text: str) -> int:
        """Store the agent's bio; returns its new version (design §3.6 step 3)."""
        state = self.agents.setdefault(agent, AgentState())
        state.bio = text
        state.bio_version += 1
        self.bios[agent] = text
        return state.bio_version

    def status(self) -> dict:
        return {
            "gateway": self.name,
            "agents": {a: {"bio_version": s.bio_version, "last_seen": s.last_seen} for a, s in self.agents.items()},
            "rooms": {r: c.status() for r, c in self.rooms.items()},
        }
