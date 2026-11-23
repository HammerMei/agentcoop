"""What every connector does to a post before the core sees it, in one place so the
platforms agree: the mention set.

Mentions are read from the text, not from the platform: Rocket.Chat records them on
the message and Mattermost only on the websocket copy, never in history, and a post
replayed from history must normalise to the same ``Post`` as its live copy.

The room core works in the platform's own namespace: an author is its username, a bot
is its bot account's username, and the text is delivered as written. The agent name
(the gateway's identity for a token) is bound to a bot's username per room by the
gateway (design §2), never rewritten into the room's text.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from ..room.controls import is_control_line
from ..room.model import ALL

_MENTION = re.compile(r"@([A-Za-z0-9._-]+)")
_ROOM_WIDE = {"all", "channel", "here", "everyone"}


def mentions_in(text: str, known: Iterable[str]) -> frozenset[str]:
    """The names ``text`` addresses, among ``known`` (the room's members and bots, in
    the core's namespace), with :data:`ALL` for a room-wide mention. A name on a
    control line is a target, not an address: ``mute: @a`` does not address ``a``."""
    names = set(known)
    found = set()
    prose = "\n".join(line for line in text.splitlines() if not is_control_line(line))
    for name in _MENTION.findall(prose):
        if name.lower() in _ROOM_WIDE:
            found.add(ALL)
        elif name in names:
            found.add(name)
    return frozenset(found)
