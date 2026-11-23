"""The per-room log behind the core's Platform calls: one entry per post however many
copies arrive, ordered by position, bounded reads, members by username with the
roster's kinds, posting and fetching through the connector."""

import pytest

from coop.connectors.base import MemberInfo, RoomInfo
from coop.connectors.roomlog import RoomLog
from coop.room.platform import RefusedPost
from coop.room.roster import Roster

from .helpers import FakeConnector

ROSTER = Roster(("a_bot", "b_bot", "x"), {"a_bot": frozenset({"H"}), "b_bot": frozenset({"H", "K"}), "x": frozenset()},
                frozenset({"G"}))
PEOPLE = [MemberInfo("H"), MemberInfo("G"), MemberInfo("K"), MemberInfo("a_bot", is_bot=True),
          MemberInfo("b_bot", is_bot=True), MemberInfo("x", is_bot=True), MemberInfo("hook", is_bot=True)]


def make_log(fc: FakeConnector | None = None, hosted=("a_bot", "b_bot")) -> tuple[FakeConnector, RoomLog]:
    fc = fc or FakeConnector()
    info = fc.add_room("c1", "team", PEOPLE)
    fc.watch("c1", ROSTER)
    log = RoomLog(fc, info, ROSTER, list(hosted))
    return fc, log


async def test_copies_of_one_post_make_one_entry_in_position_order():
    fc, log = make_log()
    p1 = fc.make_post("c1", "H", "first")
    p2 = fc.make_post("c1", "H", "second")
    assert log.ingest(p2) and log.ingest(p1) and not log.ingest(p1) and not log.ingest(p2)
    assert [p.id for p in log.posts] == [p1.id, p2.id]
    assert log.last_position() == p2.position and log.by_id(p1.id) is p1 and log.by_id("nope") is None
    assert log.backfill([p1, p2, fc.make_post("c1", "H", "third")]) == 1


async def test_reads_are_bounded_by_position_exclusive_below_inclusive_above():
    fc, log = make_log()
    posts = [fc.make_post("c1", "H", f"n{i}") for i in range(5)]
    log.backfill(posts)
    assert log.posts_after(None) == posts
    assert log.posts_after(posts[1].position) == posts[2:]
    assert log.posts_after(posts[1].position, posts[3].position) == posts[2:4]
    assert log.posts_after(posts[4].position) == []
    reply = fc.make_post("c1", "H", "in thread", thread=posts[0].id)
    log.ingest(reply)
    assert log.thread_posts(posts[0].id) == [posts[0], reply]
    assert log.thread_posts(posts[0].id, newest_first=True) == [reply, posts[0]]


async def test_members_are_by_username_with_the_rosters_kinds():
    fc, log = make_log()
    log.set_members(await fc.members("c1"))
    members = log.members()
    assert set(members) == {"H", "G", "K", "a_bot", "b_bot", "x", "hook"}
    assert members["a_bot"].is_bot and members["x"].is_bot and members["hook"].is_bot  # hook: the platform's hint
    assert members["H"].role == "owner" and members["K"].role == "owner" and members["G"].role == "guest"


async def test_a_post_goes_through_the_connector_as_the_bot_and_lands_in_the_log_once():
    fc, log = make_log()
    await fc.add_bot("a_bot", "tok")
    post = await log.post("a_bot", "@H done, @b_bot next", None, kind="bot")
    assert post.author == "a_bot" and post.thread == post.id and post.text == "@H done, @b_bot next"
    assert fc.posts["c1"][-1].text == "@H done, @b_bot next"  # delivered as written
    assert log.posts == [post]
    assert log.ingest(post) is False  # the echo from the connection is a duplicate


async def test_a_refused_or_archived_thread_raises_refused_post():
    fc, log = make_log()
    await fc.add_bot("a_bot", "tok")
    fc.refusing.add("t1")
    with pytest.raises(RefusedPost):
        await log.post("a_bot", "x", "t1")
    log.archived.add("t2")
    with pytest.raises(RefusedPost):
        await log.post("a_bot", "x", "t2")
    assert log.is_archived("t2") and not log.is_archived("t1")


async def test_a_file_is_fetched_through_the_first_hosted_bot():
    fc, log = make_log()
    post = fc.make_post("c1", "H", "see file", files=(("a.txt", "text/plain", b"hello"),))
    log.ingest(post)
    assert await log.fetch_attachment(post.attachments[0].id) == b"hello"
    _, bare = make_log(hosted=())
    with pytest.raises(KeyError):
        await bare.fetch_attachment(post.attachments[0].id)


async def test_the_fake_connector_delivers_one_copy_per_bot_connection_in_the_room():
    seen = []

    async def on_event(event):
        seen.append(event)

    fc = FakeConnector(on_event)
    fc.add_room("c1", "team", PEOPLE)
    fc.add_room("c2", "other", [MemberInfo("H"), MemberInfo("a_bot", is_bot=True)])
    assert [r.id for r in await fc.add_bot("a_bot", "t")] == ["c1", "c2"]
    assert [r.id for r in await fc.add_bot("b_bot", "t")] == ["c1"]
    await fc.human("c1", "H", "@a_bot hi")
    await fc.human("c2", "H", "only a")
    assert [(e.room_id, e.seen_by) for e in seen] == [("c1", "a_bot"), ("c1", "b_bot"), ("c2", "a_bot")]
    assert seen[0].post.mentions == {"a_bot"} and seen[0].post.text == "@a_bot hi"
    assert (await fc.room("c2")).name == "other" and isinstance(await fc.room("c1"), RoomInfo)


async def test_a_person_named_like_an_agent_is_a_person_the_bot_is_its_username():
    fc, log = make_log()
    log.set_members([MemberInfo("a"), MemberInfo("a_bot", is_bot=True)])  # a person named like agent a
    members = log.members()
    assert not members["a"].is_bot and members["a"].role == "guest" and members["a_bot"].is_bot
    post = fc.make_post("c1", "a", "@a_bot hi")
    assert post.kind == "human" and post.author == "a" and post.mentions == {"a_bot"}
