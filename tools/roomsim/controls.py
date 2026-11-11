"""Parse control words from visible post text (section 3, "control word"; 5.2; 5.3; 5.12).

Recognised, each at the start of a line, case-insensitive::

    mute: @a @b         mute: all          unmute: @a         unmute: all
    limit: 20 posts     limit: 2000 words  limit: 20 posts 2000 words
    orchestrator: @chair                   orchestrator: none
"""

from __future__ import annotations

import re

from .model import ALL, Controls

_LINE = re.compile(r"^\s*(mute|unmute|limit|orchestrator)\s*:\s*(.*?)\s*$", re.I | re.M)
_LIMIT = re.compile(r"(\d+)\s*(posts?|words?)", re.I)
_NAME = re.compile(r"@?([A-Za-z0-9._-]+)")


def _names(arg: str) -> tuple[str, ...] | str | None:
    if arg.strip().lower() in {ALL, "@all"}:
        return ALL
    names = tuple(m.group(1) for m in _NAME.finditer(arg))
    return names or None


def parse_controls(text: str) -> Controls | None:
    """Return the controls written in ``text``, or ``None`` when there are none."""
    mute = unmute = orchestrator = None
    limit: dict[str, int] | None = None
    for word, arg in _LINE.findall(text):
        word = word.lower()
        if word == "mute":
            mute = _names(arg)
        elif word == "unmute":
            unmute = _names(arg)
        elif word == "orchestrator":
            orchestrator = _parse_orchestrator(arg)
        else:
            limit = _parse_limit(arg) or limit
    found = Controls(mute=mute, unmute=unmute, limit=limit, orchestrator=orchestrator)
    return None if found.is_empty() else found


def _parse_orchestrator(arg: str) -> str | None:
    if arg.strip().lower() == "none":
        return "none"
    match = _NAME.search(arg)
    return match.group(1) if match else None


def _parse_limit(arg: str) -> dict[str, int] | None:
    found = {}
    for number, unit in _LIMIT.findall(arg):
        found["posts" if unit.lower().startswith("post") else "words"] = int(number)
    return found or None


def is_control_line(line: str) -> bool:
    return bool(_LINE.match(line))


def is_control_only(text: str) -> bool:
    """True when every non-empty line of ``text`` is a control word (section 4 step 1)."""
    lines = [line for line in text.splitlines() if line.strip()]
    return bool(lines) and all(_LINE.match(line) for line in lines)


def render_controls(control: dict) -> list[str]:
    """Render an envelope ``control`` as the visible lines a canonical post starts with."""
    lines = []
    for key in ("mute", "unmute"):
        if key in control:
            value = control[key]
            lines.append(f"{key}: {ALL if value == ALL else ' '.join('@' + n for n in value)}")
    if "limit" in control:
        parts = [f"{control['limit'][k]} {k}" for k in ("posts", "words") if k in control["limit"]]
        lines.append("limit: " + " ".join(parts))
    return lines
