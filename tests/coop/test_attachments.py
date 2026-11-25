"""Attachments (design §3.3): the limits, the bounded transfer, the handle table and a
send that carries files, against the in-process gateway."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace

import pytest

from coop.errors import RefusedSend
from coop.room.attachments import AttachmentRefused, Limits, TransferTimeout, bounded
from coop.room.model import Reply, Upload
from coop.room.platform import PlatformError, RefusedAttachment

from .helpers import FAST, Sandbox


async def chunks_of(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


async def collect(chunks: AsyncIterator[bytes]) -> bytes:
    return b"".join([c async for c in chunks])


# ── limits and the bounded transfer ─────────────────────────────────────────


def test_zero_leaves_the_platforms_limits_in_force_and_a_value_is_named_in_the_refusal():
    assert Limits().over_size("a1", 10**9) is None and Limits().over_count(50) is None
    assert Limits(max_size_mb=10).over_size("a1", 12_900_000) == (
        "attachment a1 is 12.3 MB, over the 10 MB limit (gateway.attachments.max_size_mb)")
    assert Limits(max_size_mb=10).over_size("a1", 10 * 1024 * 1024) is None
    assert Limits(max_per_post=2).over_count(3) == (
        "3 attachments on one post, over the limit of 2 (gateway.attachments.max_per_post)")


async def test_a_bounded_transfer_stops_at_the_size_limit_before_yielding_the_crossing_chunk():
    limits = Limits(max_size_mb=10 / (1024 * 1024))  # ten bytes
    seen = []
    with pytest.raises(AttachmentRefused, match="over the"):
        async for chunk in bounded(chunks_of(b"1234", b"5678", b"9abc"), "a1", limits):
            seen.append(chunk)
    assert seen == [b"1234", b"5678"]  # the third chunk crosses: refused, not yielded
    assert await collect(bounded(chunks_of(b"1234", b"5678"), "a1", limits)) == b"12345678"


async def test_a_bounded_transfer_refuses_more_bytes_than_the_sender_declared():
    with pytest.raises(AttachmentRefused, match="longer than the 5 bytes declared"):
        await collect(bounded(chunks_of(b"123", b"456"), "x.bin", Limits(), expected=5))


async def test_a_bounded_transfer_times_out_as_a_whole_not_per_chunk():
    now = [0.0]

    async def slow() -> AsyncIterator[bytes]:
        for part in (b"a", b"b", b"c"):
            now[0] += 50
            yield part

    limits = Limits(transfer_timeout_seconds=120)
    with pytest.raises(TransferTimeout, match="ran past 120 s"):
        await collect(bounded(slow(), "x.bin", limits, clock=lambda: now[0]))
    now[0] = 0
    assert await collect(bounded(slow(), "x.bin", Limits(transfer_timeout_seconds=200), clock=lambda: now[0])) == b"abc"


# ── handles ─────────────────────────────────────────────────────────────────


async def read_and_done(sb: Sandbox, agent: str):
    """Every pending wake of ``agent`` taken through read and a valid done (the cursor
    moves); the last wake and everything the reads delivered."""
    delivered, last = [], None
    await sb.run(1)  # a wake the sweep schedules is issued on the clock
    while (wake := sb.gateway.poll(agent)) is not None:
        _, posts = sb.gateway.read(agent, wake.wake_id)
        delivered += [sb.gateway.deliverable(agent, "team", p, wake.wake_id) for p in posts]
        await sb.gateway.done(agent, wake.wake_id, answered=(wake.starting_thread,))
        await sb.run(1)  # the sweep at turn end issues the next wake on the clock
        last = wake
    return last, delivered


async def test_a_handle_lives_until_every_cursor_passed_its_post_and_an_hour_went_by():
    sb = Sandbox(bots=("a", "b"), attach_agent=False)
    post = await sb.human("@a see", "t1", attachments=(("a.txt", "text/plain", b"hello"),))
    _, delivered = await read_and_done(sb, "a")
    [handle] = [a.id for p in delivered if p.id == post.id for a in p.attachments]
    assert handle == "a1" and sb.trace.last("turn_end", bot="a").attachments == 1
    await sb.human("@b and you", "t1")  # b's turn commits its cursor past the post too
    await read_and_done(sb, "b")
    await sb.run(3580)
    await sb.human("@a more", "t1")  # a read mints and evicts: the hour is not up
    await read_and_done(sb, "a")
    found, chunks = sb.gateway.fetch_attachment("a", handle)
    assert found.name == "a.txt" and await collect(chunks) == b"hello"
    await sb.run(30)
    await sb.human("@a later", "t1", attachments=(("b.txt", "text/plain", b"later"),))
    _, delivered = await read_and_done(sb, "a")
    assert [a.id for p in delivered for a in p.attachments] == ["a2"]  # the counter never reuses a number
    with pytest.raises(KeyError):
        sb.gateway.fetch_attachment("a", handle)


async def test_a_room_read_printing_an_old_handle_keeps_it_for_the_hour():
    sb = Sandbox(bots=("a",), attach_agent=False)
    post = await sb.human("@a see", "t1", attachments=(("a.txt", "text/plain", b"hello"),))
    await read_and_done(sb, "a")
    await sb.run(3000)
    older = sb.gateway.deliverable("a", "team", post)  # ``read --room --after``: printed again now
    await sb.run(601)  # past the first printing's hour, not the second's
    await sb.human("@a more", "t1")
    await read_and_done(sb, "a")
    assert sb.gateway.fetch_attachment("a", older.attachments[0].id)[0].name == "a.txt"


async def test_the_turn_counts_the_handles_its_read_printed_and_those_fetched_before_done():
    sb = Sandbox(bots=("a",), attach_agent=False)
    await sb.human("@a see", "t1", attachments=(("a.txt", "text/plain", b"x"), ("b.txt", "text/plain", b"y")))
    wake = sb.gateway.poll("a")
    _, posts = sb.gateway.read("a", wake.wake_id)
    [delivered] = [sb.gateway.deliverable("a", "team", p, wake.wake_id) for p in posts]
    await collect(sb.gateway.fetch_attachment("a", delivered.attachments[0].id)[1])
    await sb.gateway.done("a", wake.wake_id)
    end = sb.trace.last("turn_end", bot="a")
    assert (end.attachments, end.fetched) == (2, 1)


async def test_a_fetch_over_the_size_limit_is_refused_before_the_platform_is_asked():
    sb = Sandbox(bots=("a",), attach_agent=False, params=FAST)
    sb.gateway.limits = Limits(max_size_mb=1)
    await sb.human("@a see", "t1", attachments=(("big.bin", "application/octet-stream", b"x" * 2_000_000),))
    _, delivered = await read_and_done(sb, "a")
    with pytest.raises(AttachmentRefused, match="a1 is 1.9 MB"):
        sb.gateway.fetch_attachment("a", delivered[0].attachments[0].id)


# ── a send that carries files ───────────────────────────────────────────────


async def test_a_send_posts_its_files_with_the_text_as_their_caption():
    sb = Sandbox(bots=("a",), attach_agent=False)
    await sb.human("@a the graph?", "t1")
    wake = sb.gateway.poll("a")
    sent = await sb.gateway.send("a", Reply("t1", ["H"], "here", wake_id=wake.wake_id,
                                            files=(Upload.of("graph.png", b"PNG...", "image/png"),)))
    [found] = sent.post.attachments
    assert (found.name, found.content_type, found.size) == ("graph.png", "image/png", 6)
    assert sb.room.files[found.id] == b"PNG..." and sent.post.text == "@H here"
    only_file = await sb.gateway.send("a", Reply("t1", wake_id=wake.wake_id, files=(Upload.of("n.txt", b"n"),)))
    assert only_file.post.text == "" and only_file.post.attachments[0].name == "n.txt"


async def test_a_refused_send_reads_no_byte_of_its_files():
    sb = Sandbox(bots=("a",), attach_agent=False, params=replace(FAST, enforce=True))
    await sb.human("@a hello", "t1")
    wake = sb.gateway.poll("a")
    read = []

    async def watched() -> AsyncIterator[bytes]:
        read.append(True)
        yield b"x"

    with pytest.raises(RefusedSend):  # text for nobody: the room refuses, the file is never read
        await sb.gateway.send("a", Reply("t1", None, "hi", wake_id=wake.wake_id,
                                         files=(Upload("x.txt", "text/plain", 1, watched()),)))
    assert read == [] and sb.room.files == {}


async def test_the_limits_refuse_a_send_before_a_byte_reaches_the_platform():
    sb = Sandbox(bots=("a",), attach_agent=False)
    sb.gateway.limits = Limits(max_size_mb=1, max_per_post=1)
    await sb.human("@a hello", "t1")
    wake = sb.gateway.poll("a")
    with pytest.raises(AttachmentRefused, match="2 attachments on one post"):
        await sb.gateway.send("a", Reply("t1", ["H"], "hi", wake_id=wake.wake_id,
                                         files=(Upload.of("a", b"a"), Upload.of("b", b"b"))))
    with pytest.raises(AttachmentRefused, match="big.bin is 1.9 MB"):
        await sb.gateway.send("a", Reply("t1", ["H"], "hi", wake_id=wake.wake_id,
                                         files=(Upload("big.bin", "", 2_000_000, chunks_of(b"x")),)))
    with pytest.raises(AttachmentRefused, match="longer than the 1 bytes declared"):
        await sb.gateway.send("a", Reply("t1", ["H"], "hi", wake_id=wake.wake_id,
                                         files=(Upload("lie.bin", "", 1, chunks_of(b"xx")),)))
    assert sb.room.files == {}


async def test_a_file_the_platform_refuses_posts_nothing_and_leaves_the_thread_open(monkeypatch):
    sb = Sandbox(bots=("a",), attach_agent=False)
    await sb.human("@a hello", "t1")
    wake = sb.gateway.poll("a")

    async def refuse(*a, **kw):
        raise RefusedAttachment("huge.iso", "413: too large")

    monkeypatch.setattr(sb.room, "post", refuse)
    with pytest.raises(RefusedAttachment, match="huge.iso"):
        await sb.gateway.send("a", Reply("t1", ["H"], "hi", wake_id=wake.wake_id, files=(Upload.of("huge.iso", b"x"),)))
    assert sb.trace.last("attachment_refused", bot="a").name == "huge.iso"
    assert sb.gw.volatile["a"].turn.refused == []  # not the reply's refusal: the thread is still open
    assert sb.gw.status()["threads"]["t1"]["recent_posts"] == 0


# ── the internal round's findings ───────────────────────────────────────────


async def test_a_file_alone_with_recipients_mentions_them_and_checks_them():
    sb = Sandbox(bots=("a",), attach_agent=False)
    await sb.human("@a the graph?", "t1")
    wake = sb.gateway.poll("a")
    sent = await sb.gateway.send("a", Reply("t1", ["H"], wake_id=wake.wake_id, files=(Upload.of("g.png", b"x"),)))
    assert sent.post.text == "@H" and sent.post.attachments[0].name == "g.png"
    sb.gateway.limits = Limits()
    with pytest.raises(RefusedSend, match="recipient 'nobody' is not a member"):  # the recipients rule applies to a caption
        await sb.gateway.send("a", Reply("t1", ["nobody"], wake_id=wake.wake_id, files=(Upload.of("h.png", b"x"),)))


async def test_a_file_refused_mid_send_leaves_the_thread_unanswered(monkeypatch):
    sb = Sandbox(bots=("a",), attach_agent=False, params=replace(FAST, enforce=True))
    await sb.human("@a hello", "t1")
    wake = sb.gateway.poll("a")
    sb.gateway.read("a", wake.wake_id)

    async def refuse(*a, **kw):
        raise RefusedAttachment("huge.iso", "413: too large")

    monkeypatch.setattr(sb.room, "post", refuse)
    with pytest.raises(RefusedAttachment):
        await sb.gateway.send("a", Reply("t1", ["H"], "hi", wake_id=wake.wake_id, files=(Upload.of("huge.iso", b"x"),)))
    assert sb.gw.volatile["a"].turn.ledger == set()  # nothing was given: the must-reply thread is still open
    monkeypatch.undo()
    await sb.gateway.done("a", wake.wake_id)
    assert sb.trace.last("turn_end", bot="a").outcome == "invalid"


async def test_a_failure_of_another_kind_mid_upload_leaves_no_phantom_post():
    sb = Sandbox(bots=("a",), attach_agent=False)
    await sb.human("@a hello", "t1")
    wake = sb.gateway.poll("a")

    async def dropped() -> AsyncIterator[bytes]:
        yield b"x"
        raise ConnectionResetError("Connection lost")  # the agent's connection went mid-upload

    with pytest.raises(ConnectionResetError):
        await sb.gateway.send("a", Reply("t1", ["H"], "hi", wake_id=wake.wake_id,
                                         files=(Upload("x.bin", "", 2, dropped()),)))
    assert sb.gw.status()["threads"]["t1"]["recent_posts"] == 0 and sb.gw.volatile["a"].turn.ledger == set()
    assert sb.trace.last("platform_error", bot="a").error.startswith("ConnectionResetError")


async def test_a_fetch_counts_as_fetched_only_when_the_whole_file_reached_the_agent(monkeypatch):
    sb = Sandbox(bots=("a",), attach_agent=False)
    await sb.human("@a see", "t1", attachments=(("a.txt", "text/plain", b"0123456789"),))
    wake = sb.gateway.poll("a")
    _, posts = sb.gateway.read("a", wake.wake_id)
    [delivered] = [sb.gateway.deliverable("a", "team", p, wake.wake_id) for p in posts]
    handle = delivered.attachments[0].id

    async def breaks(file_id):
        yield b"0123"
        raise PlatformError("mm: dropped")

    monkeypatch.setattr(sb.room, "fetch_attachment", breaks)
    with pytest.raises(PlatformError):
        await collect(sb.gateway.fetch_attachment("a", handle)[1])
    assert sb.gw.volatile["a"].turn.fetched == set()
    monkeypatch.undo()
    await collect(sb.gateway.fetch_attachment("a", handle)[1])
    assert sb.gw.volatile["a"].turn.fetched == {handle}


async def test_a_platform_stream_shorter_than_the_posts_size_is_refused_not_served(monkeypatch):
    sb = Sandbox(bots=("a",), attach_agent=False)
    await sb.human("@a see", "t1", attachments=(("a.txt", "text/plain", b"0123456789"),))
    _, delivered = await read_and_done(sb, "a")

    async def short(file_id):
        yield b"0123"

    monkeypatch.setattr(sb.room, "fetch_attachment", short)
    with pytest.raises(AttachmentRefused, match="shorter than the 10 bytes declared"):
        await collect(sb.gateway.fetch_attachment("a", delivered[0].attachments[0].id)[1])


async def test_a_closed_rooms_handles_are_dropped():
    sb = Sandbox(bots=("a",), attach_agent=False)
    await sb.human("@a see", "t1", attachments=(("a.txt", "text/plain", b"x"),))
    _, delivered = await read_and_done(sb, "a")
    handle = delivered[0].attachments[0].id
    sb.gateway.remove_room("team")
    with pytest.raises(KeyError):
        sb.gateway.fetch_attachment("a", handle)
    assert sb.gateway._handles == {}


async def test_an_up_front_limit_refusal_is_traced():
    sb = Sandbox(bots=("a",), attach_agent=False)
    sb.gateway.limits = Limits(max_per_post=1)
    await sb.human("@a hello", "t1")
    wake = sb.gateway.poll("a")
    with pytest.raises(AttachmentRefused):
        await sb.gateway.send("a", Reply("t1", ["H"], "hi", wake_id=wake.wake_id,
                                         files=(Upload.of("a", b"a"), Upload.of("b", b"b"))))
    assert sb.trace.last("attachment_refused", bot="a").thread == "t1"


async def test_a_handle_a_live_turn_lists_outlives_the_hour():
    sb = Sandbox(bots=("a",), attach_agent=False, params=replace(FAST, lease_seconds=0))  # a lease that never expires
    post = await sb.human("@a see", "t1", attachments=(("a.txt", "text/plain", b"x"),))
    wake = sb.gateway.poll("a")
    _, posts = sb.gateway.read("a", wake.wake_id)
    [delivered] = [sb.gateway.deliverable("a", "team", p, wake.wake_id) for p in posts]
    handle = delivered.attachments[0].id
    sb.gw.persisted["a"].cursor = post.position  # as a reset wake leaves it: the post below the cursor, the turn live
    await sb.run(3601)
    sb.gateway.deliverable("a", "team", await sb.human("later", "t1"))  # runs the eviction
    assert sb.gateway.fetch_attachment("a", handle)[0].name == "a.txt"
    await sb.gateway.done("a", wake.wake_id, answered=("t1",))
    sb.gateway.deliverable("a", "team", await sb.human("later still", "t1"))
    with pytest.raises(KeyError):
        sb.gateway.fetch_attachment("a", handle)


async def test_a_handle_past_its_hour_survives_while_a_hosted_cursor_is_behind_its_post():
    sb = Sandbox(bots=("a", "b"), attach_agent=False)
    await sb.human("@a see", "t1", attachments=(("a.txt", "text/plain", b"x"),))
    _, delivered = await read_and_done(sb, "a")
    handle = delivered[0].attachments[0].id
    await sb.run(3601)
    sb.gateway.deliverable("a", "team", await sb.human("later", "t1"))
    assert sb.gw.persisted["b"].cursor is None and sb.gateway.fetch_attachment("a", handle)[0].name == "a.txt"
    await sb.human("@b you too", "t1")
    await read_and_done(sb, "b")  # b's cursor passes the post (and b's read printed the handle again)
    await sb.run(3601)
    sb.gateway.deliverable("a", "team", await sb.human("later still", "t1"))
    with pytest.raises(KeyError):
        sb.gateway.fetch_attachment("a", handle)


async def test_a_post_that_held_the_room_past_thirty_seconds_is_said(monkeypatch):
    sb = Sandbox(bots=("a",), attach_agent=False)
    await sb.human("@a hello", "t1")
    wake = sb.gateway.poll("a")
    real = sb.room.post

    async def slow(*a, **kw):
        sb.clock.clock += 31  # the platform took half a minute
        return await real(*a, **kw)

    monkeypatch.setattr(sb.room, "post", slow)
    await sb.gateway.send("a", Reply("t1", ["H"], "hi", wake_id=wake.wake_id))
    assert sb.trace.last("slow_post", bot="a").seconds == 31


async def test_a_platform_error_during_the_upload_is_said_with_its_message(monkeypatch, caplog):
    sb = Sandbox(bots=("a",), attach_agent=False)
    await sb.human("@a hello", "t1")
    wake = sb.gateway.poll("a")

    async def down(*a, **kw):
        raise PlatformError("fake: upload x.bin: 503 Service Unavailable")

    monkeypatch.setattr(sb.room, "post", down)
    with caplog.at_level("WARNING"), pytest.raises(PlatformError):
        await sb.gateway.send("a", Reply("t1", ["H"], "hi", wake_id=wake.wake_id, files=(Upload.of("x.bin", b"x"),)))
    assert sb.trace.last("platform_error", bot="a").error == "fake: upload x.bin: 503 Service Unavailable"
    assert any("failed at the platform (fake: upload x.bin: 503" in r.message and "counts as answered" in r.message
               for r in caplog.records)
