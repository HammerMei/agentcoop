"""A send with no thread is a new top-level post, which opens a thread of its own
(design §3.3): no mute reaches it, it covers no thread of a wake's ledger, and a
control cannot name it."""

import pytest

from coop.errors import InvalidReply
from coop.room.model import Reply

from .helpers import Sandbox


async def test_a_new_top_level_post_is_its_own_thread_and_no_mute_reaches_it():
    sb = Sandbox(bots=("a", "b"))
    await sb.human("mute: @a")  # in main, the simulator's one top level
    assert sb.gw.status()["threads"]["main"]["muted"] == ["a"]
    post = await sb.gateway.send("a", Reply(thread=None, to=["H"], text="a fresh topic", wake_id=None), room="team")
    assert post is not None and post.thread == post.id
    assert sb.trace.last("send", bot="a").thread is None
    assert sb.gw.status()["threads"][post.id]["muted"] == []


async def test_a_control_cannot_name_a_thread_that_does_not_exist_yet():
    sb = Sandbox(bots=("a", "b"))
    with pytest.raises(InvalidReply, match="a control names the thread"):
        await sb.gateway.send("a", Reply(thread=None, control={"mute": ["b"]}, wake_id=None), room="team")
    assert sb.posts_by("a") == []


async def test_a_new_thread_counts_toward_the_bots_cooldown_in_that_thread():
    sb = Sandbox(bots=("a", "b"))
    post = await sb.gateway.send("a", Reply(thread=None, to=["H"], text="x", wake_id=None), room="team")
    assert sb.gw.volatile["a"].last_post[post.id] == sb.clock.now()
    assert "main" not in sb.gw.volatile["a"].last_post
