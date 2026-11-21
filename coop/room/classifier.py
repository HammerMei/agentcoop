"""The speaker classifier seam (section 5.1): what it is asked, what it answers, and
the fallback table applied when it fails.

A :class:`Classifier` sees only the request fields — never backlog, busy state,
cooldown or rate state — and answers with a :class:`ClassifierResult`: a decision or
an error. The core bounds a call with ``classifier_timeout_seconds`` and retries
``classifier_retries`` times before the fallback. The model-backed classifiers
(a one-shot CLI, Jev) arrive with the daemon; the scripted one lives with the tests.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Protocol

from .model import Decision, Fallback, Member, Post

_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


class ParseError(Exception):
    pass


def load_json(text: str):
    """A JSON object: the whole text, or one fenced block in it."""
    try:
        return json.loads(text)
    except ValueError:
        pass
    match = _FENCE.search(text)
    if not match:
        raise ParseError("no JSON object found")
    try:
        return json.loads(match.group(1))
    except ValueError as exc:
        raise ParseError(f"malformed JSON block: {exc}") from None


@dataclass(frozen=True)
class ClassifierRequest:
    post: Post
    addressed: tuple[str, ...]
    history: tuple[Post, ...]  # recent posts of the thread, newest first
    omitted: int  # older posts not shown
    agents: tuple[Member, ...]


@dataclass(frozen=True)
class ClassifierResult:
    """One call's outcome. ``decision`` is None when the call failed (a backend error,
    an unparseable answer); ``error`` says why."""

    decision: Decision | None = None
    error: str = ""
    # Seconds before the core acts on the result: zero for a live classifier (its
    # wall-clock time has passed), simulated seconds for a scripted one.
    latency: float = 0.0

    @property
    def failed(self) -> bool:
        return self.decision is None


class Classifier(Protocol):
    async def classify(self, request: ClassifierRequest) -> ClassifierResult: ...


def parse_decision(text: str, request: ClassifierRequest) -> Decision:
    """The answer of ``render_classifier_prompt``: ``add`` and ``remove`` lists of
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
            name = entry["username"].lstrip("@")
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
