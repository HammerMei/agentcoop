"""Who is who in one room, from configuration (design §2, §3.1).

The gateway, not the platform, decides whether an author is a bot, whose instructions
a bot takes, and who may moderate the room. A platform's own flag (Mattermost's
``is_bot``) is the last hint, not the first: not every platform has one, and a bot
account the platform restricts may not be the account an agent uses.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from .model import Member, PostKind

Label = str  # ``agent`` | ``owner`` | ``guest``: the role label on a delivered post


@dataclass(frozen=True)
class Roster:
    """``bots`` is every bot in the room's roster, in order: the ones this gateway
    hosts and the roster-only ones another gateway drives. ``owners`` maps a bot to
    the people whose instructions it takes; ``guests`` are the people listed as
    guests of any bot. Names are agent names, the namespace the room core works in."""

    bots: tuple[str, ...]
    owners: Mapping[str, frozenset[str]] = field(default_factory=dict)
    guests: frozenset[str] = frozenset()

    @classmethod
    def from_members(cls, members: Iterable[Member], bots: Iterable[str]) -> Roster:
        """The roster a simulated room implies: every human member with the ``owner``
        role owns every bot, every ``guest`` member is a guest."""
        people = [m for m in members if not m.is_bot and m.role != "integration"]
        owners = frozenset(m.username for m in people if m.role == "owner")
        bots = tuple(bots)
        return cls(bots, {b: owners for b in bots}, frozenset(m.username for m in people if m.role == "guest"))

    def kind_of(self, author: str, platform_hint: PostKind | None = None) -> PostKind:
        """What kind of post ``author`` writes, configuration first, the platform's hint
        last: a bot in the roster is a bot, a listed owner or guest is a human, and
        otherwise the platform's word stands, an unknown author being human."""
        if author in self.bots:
            return "bot"
        if self.is_person(author):
            return "human"
        return platform_hint or "human"

    def is_person(self, author: str) -> bool:
        return author in self.guests or any(author in people for people in self.owners.values())

    def is_moderator(self, author: str) -> bool:
        """Whether ``author`` may set the room's controls: today, the owner of any bot
        in the roster. A room-moderator role, when it comes, changes this one place."""
        return any(author in self.owners.get(bot, ()) for bot in self.bots)

    def label(self, bot: str, author: str) -> Label:
        """The role label ``bot`` sees on ``author``'s posts (design §2)."""
        if author in self.bots:
            return "agent"
        return "owner" if author in self.owners.get(bot, ()) else "guest"
