"""The reply envelope (section 5.9): parsing, validation, ``all`` resolution and the
canonical platform post.

Validation returns two lists. **Structural** problems are rejected in every mode:
without a parsable item there is nothing to post. **Rule** problems are those an agent
could have obeyed from its HINT (a reply in a closed thread, a control from a
non-orchestrator, a must-reply thread left without an item, wrong recipients); the
enforced mode rejects them, the measure-only mode posts anyway and counts them.

Encoding accepted here (the gateway's choice is an open decision, section 9): the
whole response as a JSON object, or the first fenced ```json block in it, or the
exact text ``<end-of-agent-chain>`` as one ``done``.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass

from .controls import render_controls
from .model import (
    ALL,
    AUTHORITY,
    END_TOKEN,
    MUST_REPLY,
    RECIPIENTS,
    Envelope,
    Hint,
    JobRequest,
    ReplyItem,
)

_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)
_CONTROL_KEYS = {"mute", "unmute"}  # limit is an owner control (5.3, 5.9)


class ParseError(Exception):
    pass


def parse_envelope(text: str) -> Envelope:
    stripped = text.strip()
    if stripped == END_TOKEN:
        return Envelope(replies=[ReplyItem(done=True)])
    if END_TOKEN in stripped:
        raise ParseError("end token with surrounding text")
    data = _load_json(stripped)
    if not isinstance(data, dict) or "replies" not in data:
        raise ParseError("replies absent")
    if not isinstance(data["replies"], list):
        raise ParseError("replies is not a list")
    replies = [_item(raw, i) for i, raw in enumerate(data["replies"])]
    raw_schedule = data.get("schedule")
    if raw_schedule is not None and not isinstance(raw_schedule, list):
        raise ParseError("schedule is not a list")
    schedule = [_job(raw) for raw in raw_schedule or []]
    return Envelope(replies=replies, schedule=schedule)


def _load_json(text: str):
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


def _item(raw, index: int) -> ReplyItem:
    if not isinstance(raw, dict):
        raise ParseError(f"reply {index} is not an object")
    to = raw.get("to")
    if isinstance(to, str):
        to = [to]
    if to is not None and not (isinstance(to, list) and all(isinstance(n, str) for n in to)):
        raise ParseError(f"reply {index}: to must be a list of usernames")
    thread, text, control = raw.get("thread"), raw.get("text"), raw.get("control")
    if thread is not None and not isinstance(thread, str):
        raise ParseError(f"reply {index}: thread must be a string")
    if text is not None and not isinstance(text, str):
        raise ParseError(f"reply {index}: text must be a string")
    if control is not None and not isinstance(control, dict):
        raise ParseError(f"reply {index}: control must be an object")
    done = raw.get("done", False)
    if not isinstance(done, bool):
        raise ParseError(f"reply {index}: done must be a boolean")
    return ReplyItem(thread=thread, to=list(to) if to is not None else None, text=text, control=control, done=done)


def _job(raw) -> JobRequest:
    if not isinstance(raw, dict) or not isinstance(raw.get("in_seconds"), (int, float)) \
            or not math.isfinite(raw["in_seconds"]):  # NaN would sit at the heap root and stall the run
        raise ParseError("schedule entry needs a finite numeric in_seconds")
    thread = raw.get("thread")
    if thread is not None and not isinstance(thread, str):
        raise ParseError("schedule entry: thread must be a string")
    return JobRequest(in_seconds=float(raw["in_seconds"]), text=str(raw.get("text", "")), thread=thread)


# ── validation ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Problem:
    mechanism: str  # a mechanism name for rule problems, "schema" for structural ones
    thread: str | None
    reason: str


@dataclass(frozen=True)
class ValidationContext:
    bot: str
    members: frozenset[str]
    agents: frozenset[str]
    orchestrator_in: frozenset[str]  # threads in which ``bot`` is the orchestrator


def validate(env: Envelope, hint: Hint, ctx: ValidationContext) -> tuple[list[Problem], list[Problem]]:
    """Return ``(structural, rules)``; see the module docstring for the split."""
    structural: list[Problem] = []
    rules: list[Problem] = []
    for item in env.replies:
        if item.thread is None:
            item.thread = hint.starting_thread
        _check_item(item, hint, ctx, structural, rules)
    covered = {i.thread for i in env.replies}
    for thread in hint.must_reply:
        if thread not in covered:
            rules.append(Problem(MUST_REPLY, thread, "must-reply thread has no item"))
    return structural, rules


def _check_item(item: ReplyItem, hint: Hint, ctx: ValidationContext, structural, rules) -> None:
    group = hint.group_of(item.thread)
    if group is None:
        structural.append(Problem("schema", item.thread, "thread not delivered nor listed"))
        return
    if not _shape_ok(item, structural):
        return
    if item.text is not None:
        _check_recipients(item, ctx, structural, rules)
    if item.control is not None:
        _check_control(item, ctx, structural, rules)
    if group.group == "closed" and (item.text is not None or item.control is not None):
        for cause in group.closed_by:
            rules.append(Problem(cause, item.thread, "reply in a closed thread"))


def _shape_ok(item: ReplyItem, structural: list[Problem]) -> bool:
    has_action = item.text is not None or item.control is not None or item.done
    if not has_action:
        structural.append(Problem("schema", item.thread, "item has no action"))
    elif item.done and (item.text is not None or item.control is not None):
        structural.append(Problem("schema", item.thread, "done with text or control"))
    elif item.text is not None and not str(item.text).strip():
        structural.append(Problem("schema", item.thread, "empty text"))
    else:
        return True
    return False


def _check_recipients(item: ReplyItem, ctx: ValidationContext, structural, rules) -> None:
    if not item.to:
        rules.append(Problem(RECIPIENTS, item.thread, "text without recipients"))
        return
    if ctx.bot in item.to:
        rules.append(Problem(RECIPIENTS, item.thread, "addresses the sender"))
    for name in item.to:
        if name != ALL and name not in ctx.members:
            rules.append(Problem(RECIPIENTS, item.thread, f"recipient {name!r} is not a member"))


def _check_control(item: ReplyItem, ctx: ValidationContext, structural, rules) -> None:
    control = item.control
    if not isinstance(control, dict) or not control:
        structural.append(Problem("schema", item.thread, "control is not an object"))
        return
    unsupported = set(control) - _CONTROL_KEYS
    if unsupported:
        structural.append(Problem("schema", item.thread, f"unsupported control {sorted(unsupported)}"))
        return
    for key in ("mute", "unmute"):
        if key in control:
            _check_name_list(control[key], key, item.thread, ctx, structural)
    if item.thread not in ctx.orchestrator_in:
        rules.append(Problem(AUTHORITY, item.thread, "control from a non-orchestrator"))


def _check_name_list(value, key: str, thread: str | None, ctx: ValidationContext, structural) -> None:
    if value == ALL:
        return
    if not isinstance(value, list) or not value:
        structural.append(Problem("schema", thread, f"{key} list is empty"))
        return
    for name in value:
        if name not in ctx.agents or name not in ctx.members:
            structural.append(Problem("schema", thread, f"{key} names {name!r}, not an agent in the room"))


# ── resolution and canonical form ───────────────────────────────────────────


def resolve_control(control: dict, bots_in_room: list[str], muted: set[str], orchestrator: str | None) -> dict:
    """Replace ``all`` by explicit names (section 5.2). ``all`` resolving to nobody is
    omitted; an explicit list is kept as written."""
    resolved = {}
    for key, value in control.items():
        if key == "mute" and value == ALL:
            names = [b for b in bots_in_room if b != orchestrator]
        elif key == "unmute" and value == ALL:
            names = [b for b in bots_in_room if b in muted]
        else:
            resolved[key] = value
            continue
        if names:
            resolved[key] = names
    return resolved


def canonical_text(item: ReplyItem) -> str:
    """Render one reply so its platform mentions are exactly its recipients: control
    lines first, missing recipient mentions prepended, stray ``@`` removed."""
    lines = render_controls(item.control) if item.control else []
    if item.text is not None:
        recipients = list(item.to or [])
        body = re.sub(r"@([A-Za-z0-9._-]+)", lambda m: _keep_mention(m, recipients), item.text)
        present = {m.group(1).lower() for m in re.finditer(r"@([A-Za-z0-9._-]+)", body)}
        missing = [r for r in recipients
                   if not (present & ({"all", "channel", "here"} if r == ALL else {r.lower()}))]
        lead = " ".join(f"@{r}" for r in missing)
        lines.append(f"{lead} {body}".strip() if lead else body)
    return "\n".join(lines)


def _keep_mention(match: re.Match, recipients: list[str]) -> str:
    name = match.group(1)
    if name in recipients or (name.lower() in {"all", "channel", "here"} and ALL in recipients):
        return match.group(0)
    return name


def is_empty_after_resolution(item: ReplyItem) -> bool:
    return item.text is None and not item.control and not item.done
