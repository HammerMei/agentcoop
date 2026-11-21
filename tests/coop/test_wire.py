"""The wire format round-trips every structure the doorbell carries (design §3.2)."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from coop.room.model import Hint, Post, Reply, ThreadGroup, Trigger, Wake
from coop.wire import decode_post, decode_reply, decode_wake, encode_post, encode_reply, encode_wake

HINT = Hint(
    source="addressed",
    starting_thread="t1",
    threads=(
        ThreadGroup("t1", "must-reply", (), 3, 250),
        ThreadGroup("main", "optional", (), None, None),
        ThreadGroup("t0", "closed", ("mute", "rate_posts"), 0, 0),
    ),
    refused_last_turn=("rate_posts@t0: over",),
    previous_failure="lease expired",
    backoff_until=1234.5,
    degraded=True,
)
TRIGGER = Trigger(post_id="p7", author="H", author_id="u_H", author_role="owner", thread_root="t1")
WAKE = Wake(wake_id="w-team-3", room="team", bot="a", source="addressed", starting_thread="t1",
            hwm=7, hint=HINT, trigger=TRIGGER, rules_version=2, lease_expires_at=99.0)
POST = Post(id="p7", position=7, time=12.5, thread="t1", author="H", text="@a hello", kind="human",
            mentions=frozenset({"a"}), words=2, author_id="u_H")


def through_json(doc: dict) -> dict:
    return json.loads(json.dumps(doc))


@pytest.mark.parametrize("wake", [
    WAKE,
    replace(WAKE, trigger=None, source="catch-up", lease_expires_at=None, hwm=None),
    replace(WAKE, hint=replace(HINT, previous_failure=None, backoff_until=None, degraded=False,
                               refused_last_turn=())),
])
def test_a_wake_survives_json(wake):
    assert decode_wake(through_json(encode_wake(wake))) == wake


def test_a_post_survives_json_except_the_gateways_own_fields():
    # ``controls`` is the gateway's parse of the text, never sent; the agent sees the
    # control line in ``text`` like everyone in the room.
    doc = through_json(encode_post(POST, author_role="owner"))
    assert doc["author_role"] == "owner" and doc["mentions"] == ["a"]
    assert decode_post(doc) == replace(POST, author_role="owner")


def test_a_tuple_position_comes_back_as_a_list():
    doc = through_json(encode_post(replace(POST, position=(12.5, "p7"))))
    assert decode_post(doc).position == [12.5, "p7"]


@pytest.mark.parametrize("reply", [
    Reply(thread="t1", to=["H"], text="hi", control=None, wake_id="w-1"),
    Reply(thread="main", to=None, text=None, control={"mute": ["b"]}, wake_id=None),
])
def test_a_reply_survives_json(reply):
    assert decode_reply(through_json(encode_reply(reply))) == reply
