"""Per-bot owners in a room (design §2): who may set controls, and what label each bot
sees on an author."""

from coop.room.roster import Roster

from .helpers import Sandbox

# ann owns a, bob owns b; gus is a guest of both
ROSTER = Roster(bots=("a", "b"), owners={"a": frozenset({"ann"}), "b": frozenset({"bob"})}, guests=frozenset({"gus"}))
HUMANS = (("ann", "owner"), ("bob", "owner"), ("gus", "guest"))


async def test_the_owner_of_any_bot_in_the_room_sets_controls_a_guest_does_not():
    sb = Sandbox(bots=("a", "b"), humans=HUMANS, roster=ROSTER)
    await sb.human("mute: @b", author="ann")  # ann owns a, not b, and still moderates the room
    assert sb.gw.status()["threads"]["main"]["muted"] == ["b"]
    await sb.human("mute: @a", author="gus")
    assert sb.gw.status()["threads"]["main"]["muted"] == ["b"]
    assert sb.trace.last("control_ignored").by == "gus"


async def test_each_bot_reads_an_author_with_the_label_its_own_owners_give():
    sb = Sandbox(bots=("a", "b"), humans=HUMANS, roster=ROSTER)
    by_ann = await sb.human("hello", author="ann")
    by_bob = await sb.human("hello", author="bob")
    by_gus = await sb.human("hello", author="gus")
    by_b = await sb.post("b", "hello")
    seen_by_a = {p.author: sb.gateway.deliverable("a", "team", p).author_role for p in (by_ann, by_bob, by_gus, by_b)}
    seen_by_b = {p.author: sb.gateway.deliverable("b", "team", p).author_role for p in (by_ann, by_bob, by_gus, by_b)}
    assert seen_by_a == {"ann": "owner", "bob": "guest", "gus": "guest", "b": "agent"}
    assert seen_by_b == {"ann": "guest", "bob": "owner", "gus": "guest", "b": "agent"}
