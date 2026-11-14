"""The speaker classifier seam (section 5.1): what it is asked, what it answers, and
the fallback table applied when it fails.

A :class:`Classifier` sees only the request fields — never backlog, busy state,
cooldown or rate state — and answers with a :class:`ClassifierResult`: a decision or
an error, with the latency the gateway schedules it at. :class:`ScriptedClassifier`
answers from a script and can inject failures per post, so every run of section
8.1's three configurations is deterministic. :class:`BackendClassifier` asks a model
through the gateway's ``AgentBackend`` adapters, one fresh session per call, so each
call sees only its request; its wall-clock duration becomes its latency.
"""

from __future__ import annotations

import time as _time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from .envelope import ParseError, load_json
from .model import Decision, Fallback, Member, Post
from .prompt import render_classifier_prompt

if TYPE_CHECKING:
    from gateway.agents import AgentBackend


@dataclass(frozen=True)
class ClassifierRequest:
    post: Post
    addressed: tuple[str, ...]
    history: tuple[Post, ...]  # recent posts of the thread, newest first
    omitted: int  # older posts not shown
    agents: tuple[Member, ...]


@dataclass(frozen=True)
class ClassifierResult:
    """One call's outcome. ``decision`` is None when the call failed (a timeout, a backend
    error, an unparseable answer); ``error`` says why. ``latency`` is in seconds."""

    decision: Decision | None = None
    error: str = ""
    latency: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def failed(self) -> bool:
        return self.decision is None


class Classifier(Protocol):
    async def classify(self, request: ClassifierRequest) -> ClassifierResult: ...


Script = Decision | Callable[[ClassifierRequest], Decision]


@dataclass
class ScriptedClassifier:
    """Answers by post seq (``decisions``), else ``default``. ``failures`` maps a post seq
    to the number of consecutive calls for that post that fail. ``latency`` is the
    simulated seconds before a result arrives."""

    decisions: dict[int, Script] = field(default_factory=dict)
    default: Script = Decision()
    failures: dict[int, int] = field(default_factory=dict)
    latency: float = 0.5
    calls: list[ClassifierRequest] = field(default_factory=list)

    async def classify(self, request: ClassifierRequest) -> ClassifierResult:
        self.calls.append(request)
        seq = request.post.seq
        if self.failures.get(seq, 0) > 0:
            self.failures[seq] -= 1
            return ClassifierResult(error=f"injected failure for post {seq}", latency=self.latency)
        script = self.decisions.get(seq, self.default)
        return ClassifierResult(script(request) if callable(script) else script, latency=self.latency)


@dataclass
class InjectedFailures:
    """Fails ``failures[seq]`` consecutive calls for a post without asking ``inner``, so
    section 8.1's injected-failure configuration also runs against a live classifier."""

    inner: Classifier
    failures: dict[int, int] = field(default_factory=dict)
    latency: float = 0.5

    async def classify(self, request: ClassifierRequest) -> ClassifierResult:
        seq = request.post.seq
        if self.failures.get(seq, 0) > 0:
            self.failures[seq] -= 1
            return ClassifierResult(error=f"injected failure for post {seq}", latency=self.latency)
        return await self.inner.classify(request)


class BackendClassifier:
    """A model-backed classifier: one fresh ``AgentBackend`` session per call, permission
    broker off. A fresh session is what keeps 5.1's "receives only" true; a persistent
    one would carry every earlier request."""

    def __init__(
        self,
        backend: AgentBackend,
        working_directory: str,
        timeout: int,
        room: str,
        clock: Callable[[], float] = _time.monotonic,
        env: dict[str, str] | None = None,
    ) -> None:
        self.backend = backend
        self.working_directory = working_directory
        self.timeout = timeout
        self.room = room
        self.env = env
        self._clock = clock
        self.prompts: list[str] = []

    async def classify(self, request: ClassifierRequest) -> ClassifierResult:
        started = self._clock()
        prompt = render_classifier_prompt(request, self.room)
        self.prompts.append(prompt)
        try:
            session_id = await self.backend.create_session(
                self.working_directory, session_title=f"roomsim {self.room} classifier #{request.post.seq}"
            )
            response = await self.backend.send(session_id, prompt, self.working_directory, self.timeout, env=self.env)
        except Exception as exc:  # the backend failed: a process error, timeout, credentials
            return ClassifierResult(error=f"{type(exc).__name__}: {exc}", latency=self._elapsed(started))
        usage = response.usage
        cost = dict(
            latency=self._elapsed(started),
            input_tokens=usage.input_tokens if usage else 0,
            output_tokens=usage.output_tokens if usage else 0,
            cache_read_tokens=usage.cache_read_tokens if usage else 0,
            cache_write_tokens=usage.cache_write_tokens if usage else 0,
            cost_usd=response.cost_usd or 0.0,
        )
        if response.is_error:
            return ClassifierResult(error=response.text, **cost)
        try:
            return ClassifierResult(parse_decision(response.text, request), **cost)
        except ParseError as exc:
            return ClassifierResult(error=f"unparseable answer: {exc}", **cost)

    def _elapsed(self, started: float) -> float:
        return max(self._clock() - started, 0.0)


def parse_decision(text: str, request: ClassifierRequest) -> Decision:
    """The answer of :func:`render_classifier_prompt`: ``add`` and ``remove`` lists of
    ``{"username", "reason"}``. Names outside the offered agents are dropped, as is an
    ``add`` of a bot already addressed and a ``remove`` of one that is not (5.1)."""
    data = load_json(text.strip())
    if not isinstance(data, dict) or ("add" not in data and "remove" not in data):
        raise ParseError("add and remove absent")
    offered = {m.username for m in request.agents} - {request.post.author}
    reasons: dict[str, str] = {}
    add = _names(data.get("add"), "add", reasons)
    remove = _names(data.get("remove"), "remove", reasons)
    add = tuple(n for n in add if n in offered and n not in request.addressed)
    remove = tuple(n for n in remove if n in offered and n in request.addressed)
    return Decision(add=add, remove=remove, reasons={n: r for n, r in reasons.items() if n in add + remove})


def _names(raw, key: str, reasons: dict[str, str]) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ParseError(f"{key} is not a list")
    names: list[str] = []
    for entry in raw:
        if isinstance(entry, str):
            name = entry
        elif isinstance(entry, dict) and isinstance(entry.get("username"), str):
            name = entry["username"]
            if isinstance(entry.get("reason"), str):
                reasons[name] = entry["reason"]
        else:
            raise ParseError(f"{key} entry is not a username")
        names.append(name.lstrip("@"))
    return names


def fallback_decision(fallback: Fallback, post: Post, addressed: tuple[str, ...], bots: list[str]) -> Decision:
    """Section 5.1, failure policy. ``remove`` is empty: a fallback never removes."""
    everyone = fallback == "everyone" or (fallback == "addressed-or-everyone" and post.kind == "bot")
    add = tuple(b for b in bots if b not in addressed and b != post.author) if everyone else ()
    return Decision(add=add, fallback=fallback)
