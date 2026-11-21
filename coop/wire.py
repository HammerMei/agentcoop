"""The doorbell's wire format: the room core's dataclasses as JSON documents and back
(design §3.2). The gateway's API encodes with it and the agent-side client decodes
with it, so the two never disagree on a field name. It is the one module both sides
import; it knows nothing of HTTP.

A :data:`~coop.room.model.Position` travels as the JSON value it already is. A
platform whose position is a tuple gets a list back; nothing agent-side sends a
position to the gateway in this increment, so no client decodes one.
"""

from __future__ import annotations

from typing import Any

from .room.model import Attachment, Hint, Post, Reply, ThreadGroup, Trigger, Wake

Json = dict[str, Any]


# ── wakes ───────────────────────────────────────────────────────────────────


def encode_wake(wake: Wake) -> Json:
    return {
        "wake_id": wake.wake_id,
        "room": wake.room,
        "bot": wake.bot,
        "source": wake.source,
        "starting_thread": wake.starting_thread,
        "hwm": wake.hwm,
        "hint": encode_hint(wake.hint),
        "trigger": None if wake.trigger is None else encode_trigger(wake.trigger),
        "rules_version": wake.rules_version,
        "lease_expires_at": wake.lease_expires_at,
    }


def decode_wake(doc: Json) -> Wake:
    trigger = doc.get("trigger")
    return Wake(
        wake_id=doc["wake_id"],
        room=doc["room"],
        bot=doc["bot"],
        source=doc["source"],
        starting_thread=doc["starting_thread"],
        hwm=doc.get("hwm"),
        hint=decode_hint(doc["hint"]),
        trigger=None if trigger is None else decode_trigger(trigger),
        rules_version=doc.get("rules_version", 0),
        lease_expires_at=doc.get("lease_expires_at"),
    )


def encode_hint(hint: Hint) -> Json:
    return {
        "source": hint.source,
        "starting_thread": hint.starting_thread,
        "threads": [encode_group(g) for g in hint.threads],
        "refused_last_turn": list(hint.refused_last_turn),
        "previous_failure": hint.previous_failure,
        "backoff_until": hint.backoff_until,
        "degraded": hint.degraded,
    }


def decode_hint(doc: Json) -> Hint:
    return Hint(
        source=doc["source"],
        starting_thread=doc["starting_thread"],
        threads=tuple(decode_group(g) for g in doc["threads"]),
        refused_last_turn=tuple(doc.get("refused_last_turn", ())),
        previous_failure=doc.get("previous_failure"),
        backoff_until=doc.get("backoff_until"),
        degraded=doc.get("degraded", False),
    )


def encode_group(group: ThreadGroup) -> Json:
    return {
        "thread": group.thread,
        "group": group.group,
        "closed_by": list(group.closed_by),
        "posts_left": group.posts_left,
        "words_left": group.words_left,
    }


def decode_group(doc: Json) -> ThreadGroup:
    return ThreadGroup(
        thread=doc["thread"],
        group=doc["group"],
        closed_by=tuple(doc.get("closed_by", ())),
        posts_left=doc.get("posts_left"),
        words_left=doc.get("words_left"),
    )


def encode_trigger(trigger: Trigger) -> Json:
    return {
        "post_id": trigger.post_id,
        "author": trigger.author,
        "author_id": trigger.author_id,
        "author_role": trigger.author_role,
        "thread_root": trigger.thread_root,
        "relayed": trigger.relayed,
    }


def decode_trigger(doc: Json) -> Trigger:
    return Trigger(
        post_id=doc["post_id"],
        author=doc["author"],
        author_id=doc["author_id"],
        author_role=doc["author_role"],
        thread_root=doc["thread_root"],
        relayed=doc.get("relayed", False),
    )


# ── posts ───────────────────────────────────────────────────────────────────


def encode_post(post: Post) -> Json:
    """A post as ``read`` delivers it: the fields an agent reads, the author's role
    label and the attachments by handle (both filled by ``Gateway.deliverable``).
    ``controls`` stays on the gateway's side: the agent sees the control line in
    ``text`` like everyone in the room."""
    return {
        "id": post.id,
        "position": post.position,
        "time": post.time,
        "thread": post.thread,
        "author": post.author,
        "author_id": post.author_id,
        "author_role": post.author_role,
        "text": post.text,
        "kind": post.kind,
        "mentions": sorted(post.mentions),
        "words": post.words,
        "attachments": [encode_attachment(a) for a in post.attachments],
    }


def decode_post(doc: Json) -> Post:
    return Post(
        id=doc["id"],
        position=doc.get("position"),
        time=doc.get("time", 0.0),
        thread=doc["thread"],
        author=doc["author"],
        text=doc["text"],
        kind=doc["kind"],
        mentions=frozenset(doc.get("mentions", ())),
        words=doc.get("words", 0),
        author_id=doc.get("author_id", ""),
        author_role=doc.get("author_role", ""),
        attachments=tuple(decode_attachment(a) for a in doc.get("attachments", ())),
    )


def encode_attachment(attachment: Attachment) -> Json:
    return {"id": attachment.id, "name": attachment.name, "content_type": attachment.content_type,
            "size": attachment.size}


def decode_attachment(doc: Json) -> Attachment:
    return Attachment(id=doc["id"], name=doc["name"], content_type=doc.get("content_type", ""),
                      size=doc.get("size", 0))


# ── replies ─────────────────────────────────────────────────────────────────


def encode_reply(reply: Reply) -> Json:
    return {
        "thread": reply.thread,
        "to": reply.to,
        "text": reply.text,
        "control": reply.control,
        "wake_id": reply.wake_id,
    }


def decode_reply(doc: Json) -> Reply:
    return Reply(
        thread=doc["thread"],
        to=None if doc.get("to") is None else list(doc["to"]),
        text=doc.get("text"),
        control=doc.get("control"),
        wake_id=doc.get("wake_id"),
    )
