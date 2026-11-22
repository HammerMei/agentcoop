"""What every connector does to a post before the core sees it, in one place so the
platforms agree: the mention set, and the two-way mapping between a bot's platform
username and its agent name.

Mentions are read from the text, not from the platform: Rocket.Chat records them on
the message and Mattermost only on the websocket copy, never in history, and a post
replayed from history must normalise to the same ``Post`` as its live copy.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

from ..room.controls import is_control_line
from ..room.model import ALL

_MENTION = re.compile(r"@([A-Za-z0-9._-]+)")
_ROOM_WIDE = {"all", "channel", "here", "everyone"}


class Names:
    """The bots' two namespaces on one server: ``agent_of`` turns a platform username
    into the agent name the core works in, ``username_of`` the reverse. A name that
    is not a bot's passes through unchanged in both directions."""

    def __init__(self, usernames: Mapping[str, str]) -> None:  # agent -> username
        self._username = dict(usernames)
        self._agent = {u: a for a, u in usernames.items()}

    def agent_of(self, username: str) -> str:
        return self._agent.get(username, username)

    def collides(self, username: str) -> bool:
        """A platform username that is an agent's name without being that agent's
        username: the core would take the person for the bot."""
        return username in self._username and self._username[username] != username

    def username_of(self, agent: str) -> str:
        return self._username.get(agent, agent)

    def inbound(self, text: str) -> str:
        """``@username`` of a bot becomes ``@agent``: the agent reads the names it must
        use in ``--to``."""
        return _MENTION.sub(lambda m: "@" + self.agent_of(m.group(1)), text) if self._agent else text

    def outbound(self, text: str) -> str:
        """``@agent`` becomes ``@username`` so the platform notifies the bot."""
        return _MENTION.sub(lambda m: "@" + self.username_of(m.group(1)), text) if self._username else text


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
