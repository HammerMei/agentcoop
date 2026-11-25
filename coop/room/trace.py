"""The trace vocabulary. Every mechanism effect is one :class:`TraceEvent`; the
mechanism tests (spec section 8.3, ``tests/coop``) assert on these, never on core
internals, and the simulator's trace (``tools/roomsim/trace.py``) uses the same
kinds for the mechanisms both still have, which is what design §6.2's replay parity
compares.

Kinds and their fields:

- ``post``            — id, thread, author, post_kind, mentions
- ``control_applied`` — gateway, thread, by, control, value, post; ``control_ignored`` — gateway, thread, by, reason
- ``classifier_call`` — gateway, post; ``classifier_decision`` — gateway, post, add, remove, fallback, reasons
- ``classifier_retry`` — gateway, post, attempt, error; ``classifier_expired`` — gateway, post, bot
- ``turn_start``      — gateway, bot, source, thread, exempt, delivered, wake (the wake is issued)
- ``lease_started``   — gateway, bot, wake, expires_at (an agent's poll picked the wake up)
- ``typing``          — gateway, bot, thread (an addressed wake's pick-up sends the indicator)
- ``turn_held``       — gateway, bot, source, thread, gate
- ``turn_end``        — gateway, bot, wake, outcome (normal | backend_failure | invalid), posts,
                       attachments (handles the wake's read printed), fetched (of those, fetched before done)
- ``invalid``         — gateway, bot, wake, reason
- ``done_stale``      — gateway, bot, wake (a ``done`` for a wake with no live lease)
- ``read``            — gateway, bot, wake, delivered
- ``send``            — gateway, bot, wake (None = unsolicited), thread, post
- ``refused``         — gateway, bot, wake, thread, reason (the gateway refused a send, enforce mode)
- ``cursor_saved``    — gateway, bot, position
- ``unfinished_added`` / ``unfinished_removed`` — gateway, bot, thread
- ``sweep``           — gateway, bot, reason, candidates
- ``notice``          — gateway, bot, post, cause
- ``backoff``         — gateway, bot, until; ``degraded`` — gateway, bot, streak
- ``rule``            — gateway, bot, thread, mechanism, outcome (held | rejected | violated | complied | held_complied | held_violated)
- ``platform_refused`` — gateway, bot, thread (the platform refused an outbound post)
- ``platform_error``  — gateway, bot, thread, error (the platform did not answer a post, or a failure of another kind ended it; the message)
- ``slow_post``       — gateway, bot, thread, seconds (a platform post held the room past ``SLOW_POST_SECONDS``)
- ``attachment_refused`` — gateway, bot, thread, name (the platform or the gateway's limits refused a file a send carried; nothing posted)
- ``paused`` / ``resumed`` — gateway, bot
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TraceEvent:
    time: float
    n: int  # position in the trace
    kind: str
    fields: dict[str, Any] = field(default_factory=dict)

    def __getattr__(self, name: str) -> Any:
        try:
            return self.fields[name]
        except KeyError:
            raise AttributeError(name) from None

    def matches(self, **wanted: Any) -> bool:
        return all(self.fields.get(k) == v for k, v in wanted.items())

    def __str__(self) -> str:
        inner = " ".join(f"{k}={v!r}" for k, v in self.fields.items())
        return f"[{self.time:8.1f}] {self.kind} {inner}"


class Trace:
    """An append-only list of events with the queries the tests use."""

    def __init__(self) -> None:
        self.events: list[TraceEvent] = []
        self._next = 0

    def add(self, time: float, kind: str, **fields: Any) -> TraceEvent:
        event = TraceEvent(time, self._next, kind, fields)
        self._next += 1
        self.events.append(event)
        return event

    def of(self, kind: str, **wanted: Any) -> list[TraceEvent]:
        return [e for e in self.events if e.kind == kind and e.matches(**wanted)]

    def count(self, kind: str, **wanted: Any) -> int:
        return len(self.of(kind, **wanted))

    def last(self, kind: str, **wanted: Any) -> TraceEvent | None:
        found = self.of(kind, **wanted)
        return found[-1] if found else None

    def render(self) -> str:
        return "\n".join(str(e) for e in self.events)
