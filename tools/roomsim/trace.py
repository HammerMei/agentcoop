"""The trace vocabulary. Every mechanism effect is one :class:`TraceEvent`; the
required tests (section 8.3) assert on these, never on simulator internals, so the
gateway can later produce the same trace and reuse the tests (section 8.1).

Kinds and their fields:

- ``post``            — seq, thread, author, post_kind, mentions
- ``control_applied`` — gateway, thread, by, control; ``control_ignored`` — gateway, thread, by, reason
- ``classifier_call`` — gateway, post; ``classifier_decision`` — gateway, post, add, remove, fallback
- ``classifier_expired`` — gateway, post, bot
- ``turn_start``      — gateway, bot, source, thread, exempt
- ``turn_held``       — gateway, bot, source, thread, gate
- ``turn_end``        — gateway, bot, outcome (normal | backend_failure | invalid), posts
- ``invalid``         — gateway, bot, reason, attempt
- ``cursor_saved``    — gateway, bot, seq
- ``unfinished_added`` / ``unfinished_removed`` — gateway, bot, thread
- ``sweep``           — gateway, bot, reason, candidates
- ``notice``          — gateway, bot, seq, cause
- ``backoff``         — gateway, bot, until; ``degraded`` — gateway, bot, streak
- ``job_scheduled`` / ``job_fired`` / ``job_waiting`` / ``job_retry`` / ``job_capped`` — gateway, bot, job, thread
- ``rule``            — gateway, bot, thread, mechanism, outcome (blocked | complied | violated)
- ``refused``         — gateway, bot, thread (platform refused an outbound post)
- ``done``            — gateway, bot, thread, to (a done reply; nothing is posted)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TraceEvent:
    time: float
    n: int  # position in the trace; ``seq`` is left to the post-id fields
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
