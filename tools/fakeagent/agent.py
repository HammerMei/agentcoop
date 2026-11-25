"""The fake agent's turn loop and its script language.

A :class:`FakeAgent` is told of a wake (in process, by the gateway's listener hook;
over HTTP, by its own long-poll), polls it, reads, and after ``duration`` seconds
sends what its handler decided and calls ``done``. The handler sees a :class:`Turn`
(bot, source, starting thread, delivered posts, hint) and answers with a reply
envelope in the simulator's shape — ``{"replies": [{"thread", "to", "text",
"control", "done"}]}`` — so the simulator's scripts and scenarios carry over: a
``text``/``control`` item becomes one ``send --wake``, a ``done`` item becomes a thread
named in ``done --answered``. It can also answer with a :class:`Behaviour`, which adds
the misbehaviours the gateway must survive: a failed turn, a turn that never reads,
a turn that never calls ``done``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from coop.client import Doorbell
from coop.errors import InvalidReply, RefusedSend
from coop.room.clock import Clock
from coop.room.model import Hint, Post, Reply, TurnSource, Wake
from coop.room.platform import RefusedPost


@dataclass(frozen=True)
class Turn:
    """What the handler sees: the simulator's ``TurnInput`` read back through the doorbell."""

    bot: str
    source: TurnSource
    starting_thread: str
    delivered: tuple[Post, ...]
    hint: Hint
    wake: Wake


@dataclass
class Behaviour:
    """What the agent does with a turn. ``replies`` is the envelope's item list."""

    replies: list[dict] = field(default_factory=list)
    failed: str | None = None
    duration: float | None = None
    skip_done: bool = False


Reply_ = dict | Behaviour
Handler = Callable[[Turn], Reply_]


def done_everywhere(turn: Turn) -> dict:
    """The compliant answer with nothing to say: ``done`` in every must-reply thread."""
    return {"replies": [{"thread": t, "done": True} for t in turn.hint.must_reply]}


def fail(error: str = "backend failure", duration: float = 1.0) -> Behaviour:
    return Behaviour(failed=error, duration=duration)


@dataclass
class Rule:
    reply: object
    bot: str | None = None
    source: str | None = None
    thread: str | None = None
    when: Callable[[Turn], bool] | None = None
    once: bool = False
    used: int = 0

    def matches(self, turn: Turn) -> bool:
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
    """A handler: the first matching rule answers, else ``done`` in every must-reply
    thread. ``reply`` may be an envelope dict, a :class:`Behaviour`, or a callable
    taking the turn."""

    rules: list[Rule] = field(default_factory=list)
    fallback: Handler = done_everywhere

    def on(self, reply, **match) -> Script:
        self.rules.append(Rule(reply, **match))
        return self

    def __call__(self, turn: Turn) -> Reply_:
        for rule in self.rules:
            if rule.matches(turn):
                rule.used += 1
                return rule.reply(turn) if callable(rule.reply) else rule.reply
        return self.fallback(turn)


@dataclass
class Outcome:
    """What one turn did, for the tests: the posts the gateway accepted and the
    refusals it answered."""

    turn: Turn
    posted: list[Post] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)  # rules a non-enforcing room let the sends break
    refused: list[str] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)
    platform_refused: list[str] = field(default_factory=list)
    answered: list[str] = field(default_factory=list)
    done: bool | None = None


class FakeAgent:
    """One agent, possibly several bots (the handler sees ``turn.bot``), on one doorbell
    per bot. ``duration`` is how long a turn takes between its read and its sends."""

    def __init__(self, clock: Clock, handler: Handler = done_everywhere, duration: float = 1.0) -> None:
        self.clock = clock
        self.handler = handler
        self.duration = duration
        # A misbehaving client: decides from the wake alone whether to skip ``read``
        # (the handler runs after the read, so it cannot decide this itself).
        self.skip_read: Callable[[Wake], bool] | None = None
        self.doorbells: dict[str, Doorbell] = {}
        self.turns: list[Turn] = []
        self.outcomes: list[Outcome] = []

    def attach(self, bot: str, doorbell: Doorbell) -> None:
        self.doorbells[bot] = doorbell

    async def serve(self, bot: str, wait: float = 30.0) -> None:
        """Over HTTP there is no listener: long-poll the bot's doorbell until cancelled,
        taking each wake as it comes."""
        doorbell = self.doorbells[bot]
        while True:
            wake = await doorbell.poll(wait)
            if wake is not None:
                await self.take(doorbell, wake)

    async def on_wake(self, wake: Wake) -> None:
        """A wake was announced for one of this agent's bots: poll it like any client."""
        doorbell = self.doorbells[wake.bot]
        picked = await doorbell.poll()
        if picked is None:
            return
        await self.take(doorbell, picked)

    async def take(self, doorbell: Doorbell, wake: Wake) -> None:
        posts: list[Post] = []
        if self.skip_read is None or not self.skip_read(wake):
            _, posts = await doorbell.read(wake.wake_id)
        turn = Turn(wake.bot, wake.source, wake.starting_thread, tuple(posts), wake.hint, wake)
        self.turns.append(turn)
        answer = self.handler(turn)
        behaviour = answer if isinstance(answer, Behaviour) else Behaviour(replies=list(answer.get("replies", [])))
        duration = behaviour.duration if behaviour.duration is not None else self.duration
        self.clock.at(duration, self._finish, doorbell, turn, behaviour)

    async def _finish(self, doorbell: Doorbell, turn: Turn, behaviour: Behaviour) -> None:
        wake = turn.wake
        outcome = Outcome(turn)
        self.outcomes.append(outcome)
        if behaviour.failed is not None:
            outcome.done = await doorbell.done(wake.wake_id, failed=behaviour.failed)
            return
        for item in behaviour.replies:
            thread = item.get("thread") or wake.starting_thread
            if item.get("done"):
                outcome.answered.append(thread)
                continue
            # ``"unsolicited": True`` on an item: the agent forgot the wake id (design §3.5).
            reply = Reply(thread=thread, to=list(item["to"]) if item.get("to") is not None else None,
                          text=item.get("text"), control=item.get("control"),
                          wake_id=None if item.get("unsolicited") else wake.wake_id)
            try:
                sent = await doorbell.send(reply, room=wake.room)
            except RefusedSend as exc:
                outcome.refused.append(f"{thread}: {exc.reasons}")
            except InvalidReply as exc:
                outcome.invalid.append(f"{thread}: {exc}")
            except RefusedPost:
                outcome.platform_refused.append(thread)
            else:
                outcome.warnings.extend(sent.warnings)
                if sent.post is not None:
                    outcome.posted.append(sent.post)
        if behaviour.skip_done:
            return
        outcome.done = await doorbell.done(wake.wake_id, answered=tuple(outcome.answered))
