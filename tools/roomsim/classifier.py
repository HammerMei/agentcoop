"""The speaker classifier seam (section 5.1): what it is asked, what it answers, and
the fallback table applied when it fails.

A :class:`Classifier` sees only the request fields — never backlog, busy state,
cooldown or rate state. :class:`ScriptedClassifier` answers from a script and can
inject failures per post, so every run of section 8.1's three configurations is
deterministic. A model-backed classifier would implement the same protocol.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from .model import Decision, Fallback, Member, Post


class ClassifierError(Exception):
    """A timeout or error of one classifier call."""


@dataclass(frozen=True)
class ClassifierRequest:
    post: Post
    addressed: tuple[str, ...]
    history: tuple[Post, ...]  # recent posts of the thread, newest first
    omitted: int  # older posts not shown
    agents: tuple[Member, ...]


class Classifier(Protocol):
    async def classify(self, request: ClassifierRequest) -> Decision: ...


Script = Decision | Callable[[ClassifierRequest], Decision]


@dataclass
class ScriptedClassifier:
    """Answers by post seq (``decisions``), else ``default``. ``failures`` maps a post seq
    to the number of consecutive calls for that post that raise :class:`ClassifierError`.
    ``latency`` is the simulated seconds before a decision arrives."""

    decisions: dict[int, Script] = field(default_factory=dict)
    default: Script = Decision()
    failures: dict[int, int] = field(default_factory=dict)
    latency: float = 0.5
    calls: list[ClassifierRequest] = field(default_factory=list)

    async def classify(self, request: ClassifierRequest) -> Decision:
        self.calls.append(request)
        seq = request.post.seq
        if self.failures.get(seq, 0) > 0:
            self.failures[seq] -= 1
            raise ClassifierError(f"injected failure for post {seq}")
        script = self.decisions.get(seq, self.default)
        return script(request) if callable(script) else script


def fallback_decision(fallback: Fallback, post: Post, addressed: tuple[str, ...], bots: list[str]) -> Decision:
    """Section 5.1, failure policy. ``remove`` is empty: a fallback never removes."""
    everyone = fallback == "everyone" or (fallback == "addressed-or-everyone" and post.kind == "bot")
    add = tuple(b for b in bots if b not in addressed and b != post.author) if everyone else ()
    return Decision(add=add, fallback=fallback)
