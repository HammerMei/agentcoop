"""Bot turn runners. A :class:`BotRunner` turns a :class:`~tools.roomsim.model.TurnInput`
into raw text; the gateway parses and validates it. Three kinds:

- :class:`ScriptedBot` answers from a handler, so mechanism tests are deterministic;
- :class:`Recorder` / :class:`Replayer` fix a live run's outputs by turn hash and fail
  loudly when a replay's turn differs from the recorded one (section 8.1);
- :class:`BackendBot` is the bridge to the gateway's ``AgentBackend`` adapters: one
  persistent session per bot per room, permission broker off. A live turn's wall-clock
  duration becomes its simulated duration.
"""

from __future__ import annotations

import hashlib
import json
import time as _time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Protocol

from .model import BotOutput, Member, TurnInput
from .prompt import render_intro, render_prompt

if TYPE_CHECKING:
    from gateway.agents import AgentBackend


class BotRunner(Protocol):
    async def run(self, turn: TurnInput) -> BotOutput: ...


Reply = str | dict | BotOutput
Handler = Callable[[TurnInput], Reply]


def done_everywhere(turn: TurnInput) -> dict:
    """The compliant answer with nothing to say: ``done`` in every must-reply thread."""
    return {"replies": [{"thread": t, "done": True} for t in turn.hint.must_reply]}


def say(thread: str | None, to: list[str], text: str, **more) -> dict:
    item = {"to": to, "text": text, **more}
    if thread is not None:
        item["thread"] = thread
    return item


@dataclass
class ScriptedBot:
    """``handler`` receives the turn and returns raw text, a dict to serialise as the
    envelope, or a :class:`BotOutput` (to set duration, tokens, or a backend failure).
    The default handler answers ``done`` everywhere."""

    handler: Handler = done_everywhere
    duration: float = 1.0
    turns: list[TurnInput] = field(default_factory=list)

    async def run(self, turn: TurnInput) -> BotOutput:
        self.turns.append(turn)
        reply = self.handler(turn)
        if isinstance(reply, BotOutput):
            return reply
        text = reply if isinstance(reply, str) else json.dumps(reply)
        return BotOutput(text=text, duration_seconds=self.duration)


def fail(error: str = "backend failure", duration: float = 1.0) -> BotOutput:
    return BotOutput(text="", failed=True, error=error, duration_seconds=duration)


def turn_hash(turn: TurnInput) -> str:
    payload = {
        "bot": turn.bot,
        "source": turn.source,
        "thread": turn.starting_thread,
        "delivered": [(p.seq, p.thread, p.author, p.text) for p in turn.delivered],
        "hint": asdict(turn.hint),
        "job": turn.job_text,
        **({"error": turn.error} if turn.error else {}),  # a retry prompt carries the error (5.9)
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


@dataclass
class Recorder:
    inner: BotRunner
    records: list[dict] = field(default_factory=list)

    async def run(self, turn: TurnInput) -> BotOutput:
        output = await self.inner.run(turn)
        self.records.append({"hash": turn_hash(turn), "bot": turn.bot, "output": asdict(output)})
        return output


class ReplayMismatch(Exception):
    pass


@dataclass
class Replayer:
    """Hands back recorded outputs in order and refuses a turn whose hash differs from
    the recorded one: a replay that drifts must fail, not answer with the wrong turn."""

    records: list[dict]
    _next: int = 0

    def unused(self) -> int:
        return len(self.records) - self._next

    async def run(self, turn: TurnInput) -> BotOutput:
        if self._next >= len(self.records):
            raise ReplayMismatch(f"turn {self._next} for {turn.bot}: recording exhausted")
        record = self.records[self._next]
        self._next += 1
        if record["hash"] != turn_hash(turn):
            raise ReplayMismatch(
                f"turn {self._next - 1}: recorded {record['bot']} {record['hash']}, "
                f"got {turn.bot} {turn_hash(turn)}"
            )
        return BotOutput(**record["output"])


class BackendBot:
    """One ``AgentBackend`` session per bot per room. ``members`` and ``room`` feed the
    session's first message (who the bot is, who is in the room, the envelope contract)."""

    def __init__(
        self,
        backend: AgentBackend,
        working_directory: str,
        timeout: int,
        room: str,
        members: list[Member],
        clock: Callable[[], float] = _time.monotonic,
        env: dict[str, str] | None = None,
    ) -> None:
        self.backend = backend
        self.env = env
        self.working_directory = working_directory
        self.timeout = timeout
        self.room = room
        self.members = {m.username: m for m in members}
        self.sessions: dict[str, str] = {}
        self._clock = clock

    async def _session(self, bot: str) -> tuple[str, bool]:
        if bot in self.sessions:
            return self.sessions[bot], False
        session_id = await self.backend.create_session(
            self.working_directory, session_title=f"roomsim {self.room} {bot}"
        )
        self.sessions[bot] = session_id
        return session_id, True

    async def run(self, turn: TurnInput) -> BotOutput:
        started = self._clock()
        try:
            session_id, fresh = await self._session(turn.bot)
            prompt = render_prompt(turn)
            if fresh:
                intro = render_intro(self.members[turn.bot], self.room, list(self.members.values()))
                prompt = f"{intro}\n\n{prompt}"
            response = await self.backend.send(session_id, prompt, self.working_directory, self.timeout,
                                               env=self.env)
        except Exception as exc:  # the backend failed: a process error, timeout, credentials
            return BotOutput(text="", failed=True, error=f"{type(exc).__name__}: {exc}",
                             duration_seconds=max(self._clock() - started, 0.0))
        usage = response.usage
        return BotOutput(
            text=response.text,
            duration_seconds=max(self._clock() - started, 0.0),
            input_tokens=usage.input_tokens if usage else 0,
            output_tokens=usage.output_tokens if usage else 0,
            cache_read_tokens=usage.cache_read_tokens if usage else 0,
            cache_write_tokens=usage.cache_write_tokens if usage else 0,
            cost_usd=response.cost_usd or 0.0,
            failed=response.is_error,
            error=response.text if response.is_error else "",
        )
