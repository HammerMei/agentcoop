"""The roster decides who is a bot, who moderates, and what label each bot sees on an
author (design §2, §3.1): configuration first, the platform's hint last."""

from coop.room.model import Member
from coop.room.roster import Roster

ROSTER = Roster(bots=("a", "b", "x"),                       # x: roster-only, another gateway's
                owners={"a": frozenset({"ann"}), "b": frozenset({"bob", "ann"})},
                guests=frozenset({"gus"}))


def test_a_bot_in_the_roster_is_a_bot_whatever_the_platform_says():
    assert ROSTER.kind_of("x", platform_hint="human") == "bot"
    assert ROSTER.kind_of("a") == "bot"


def test_a_listed_owner_or_guest_is_human_whatever_the_platform_says():
    assert ROSTER.kind_of("ann", platform_hint="bot") == "human"
    assert ROSTER.kind_of("gus", platform_hint="integration") == "human"


def test_an_unlisted_author_takes_the_platform_hint_and_is_human_without_one():
    assert ROSTER.kind_of("stranger", platform_hint="bot") == "bot"
    assert ROSTER.kind_of("stranger", platform_hint="integration") == "integration"
    assert ROSTER.kind_of("stranger") == "human"


def test_the_owner_of_any_bot_in_the_roster_moderates_and_nobody_else_does():
    assert ROSTER.is_moderator("ann")
    assert ROSTER.is_moderator("bob")
    assert not ROSTER.is_moderator("gus")
    assert not ROSTER.is_moderator("a")
    assert not ROSTER.is_moderator("stranger")


def test_the_label_is_per_bot_an_owner_of_one_bot_is_a_guest_to_another():
    assert ROSTER.label("a", "ann") == "owner"
    assert ROSTER.label("b", "ann") == "owner"
    assert ROSTER.label("a", "bob") == "guest"
    assert ROSTER.label("b", "bob") == "owner"
    assert ROSTER.label("a", "gus") == "guest"
    assert ROSTER.label("a", "stranger") == "guest"
    assert ROSTER.label("a", "x") == "agent"
    assert ROSTER.label("a", "b") == "agent"


def test_from_members_gives_every_bot_every_owner_member():
    members = [Member("H", "owner"), Member("G", "guest"), Member("a", "bot"), Member("hook", "integration")]
    roster = Roster.from_members(members, ["a", "b"])
    assert roster.bots == ("a", "b")
    assert roster.owners == {"a": frozenset({"H"}), "b": frozenset({"H"})}
    assert roster.guests == frozenset({"G"})
    assert roster.kind_of("hook", platform_hint="integration") == "integration"
