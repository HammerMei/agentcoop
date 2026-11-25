"""One ``send`` checked against the room rules (section 5.9 as design §3.5 restates
it): shape, recipients, control authority, closed threads, ``all`` resolution and the
canonical platform post.

``check_reply`` returns two lists. **Structural** problems are refused in every mode:
without a postable item there is nothing to post, and the CLI reports them as usage
errors. **Rule** problems are those an agent could have obeyed from its hint (a reply
in a closed thread, a control from a non-orchestrator, wrong recipients); enforce
mode refuses them, measure mode posts anyway and counts them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .controls import render_controls
from .model import ALL, AUTHORITY, END_TOKEN, RECIPIENTS, Reply

_CONTROL_KEYS = {"mute", "unmute"}  # limit is an owner control (5.3, 5.9)
_MENTION = re.compile(r"@([A-Za-z0-9._-]+)")
_ROOM_WIDE = {"all", "channel", "here"}


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
    # Why the reply's thread is closed to the bot right now, by mechanism (empty = open).
    closed_by: tuple[str, ...] = ()


def check_reply(reply: Reply, ctx: ValidationContext) -> tuple[list[Problem], list[Problem]]:
    """Return ``(structural, rules)``; see the module docstring for the split."""
    structural: list[Problem] = []
    rules: list[Problem] = []
    if not _shape_ok(reply, structural):
        return structural, rules
    if reply.text is not None or (reply.files and reply.to):
        _check_recipients(reply, ctx, rules)
    if reply.control is not None:
        _check_control(reply, ctx, structural, rules)
    for cause in ctx.closed_by:
        rules.append(Problem(cause, reply.thread, "reply in a closed thread"))
    return structural, rules


def _shape_ok(reply: Reply, structural: list[Problem]) -> bool:
    if reply.text is None and reply.control is None and not reply.files:
        structural.append(Problem("schema", reply.thread, "neither text, control nor a file"))
    elif reply.text is not None and not str(reply.text).strip():
        structural.append(Problem("schema", reply.thread, "empty text"))
    elif reply.text is not None and END_TOKEN in reply.text:
        structural.append(Problem("schema", reply.thread, f"{END_TOKEN} in text: call done instead"))
    else:
        return True
    return False


def _check_recipients(reply: Reply, ctx: ValidationContext, rules: list[Problem]) -> None:
    if not reply.to:
        rules.append(Problem(RECIPIENTS, reply.thread, "text without recipients"))
        return
    if ctx.bot in reply.to:
        rules.append(Problem(RECIPIENTS, reply.thread, "addresses the sender"))
    for name in reply.to:
        if name != ALL and name not in ctx.members:
            rules.append(Problem(RECIPIENTS, reply.thread, f"recipient {name!r} is not a member"))


def _check_control(reply: Reply, ctx: ValidationContext, structural: list[Problem], rules: list[Problem]) -> None:
    control = reply.control
    if not isinstance(control, dict) or not control:
        structural.append(Problem("schema", reply.thread, "control is not an object"))
        return
    unsupported = set(control) - _CONTROL_KEYS
    if unsupported:
        structural.append(Problem("schema", reply.thread, f"unsupported control {sorted(unsupported)}"))
        return
    if reply.thread is None:
        structural.append(Problem("schema", None, "a control names the thread it applies to"))
        return
    for key in ("mute", "unmute"):
        if key in control:
            _check_name_list(control[key], key, reply.thread, ctx, structural)
    if reply.thread not in ctx.orchestrator_in:
        rules.append(Problem(AUTHORITY, reply.thread, "control from a non-orchestrator"))


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


def canonical_text(reply: Reply) -> str:
    """Render one reply so its platform mentions are exactly its recipients: control
    lines first, missing recipient mentions prepended, stray ``@`` removed."""
    lines = render_controls(reply.control) if reply.control else []
    if reply.text is not None or (reply.files and reply.to):
        # a file alone with recipients is a caption of just their mentions
        recipients = list(reply.to or [])
        body = _MENTION.sub(lambda m: _keep_mention(m, recipients), reply.text or "")
        present = {m.group(1).lower() for m in _MENTION.finditer(body)}
        missing = [r for r in recipients
                   if not (present & (_ROOM_WIDE if r == ALL else {r.lower()}))]
        lead = " ".join(f"@{r}" for r in missing)
        lines.append(f"{lead} {body}".strip() if lead else body)
    return "\n".join(lines)


def _keep_mention(match: re.Match, recipients: list[str]) -> str:
    name = match.group(1)
    if name in recipients or (name.lower() in _ROOM_WIDE and ALL in recipients):
        return match.group(0)
    return name
