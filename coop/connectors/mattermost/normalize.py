"""A Mattermost post as the room core sees it. Pure: the same function serves the
websocket copy and the history copy, so a replayed post equals its live one
(mentions come from the text, the platform's ``mentions`` field is not used).
"""

from __future__ import annotations

from collections.abc import Iterable

from ...room.controls import parse_controls
from ...room.model import Attachment, Post, PostKind
from ...room.roster import Roster
from ...room.words import count_words
from ..normalize import mentions_in


def normalize(post: dict, author: dict, roster: Roster, known: Iterable[str]) -> Post | None:
    """``author`` is the user object for ``post["user_id"]``. ``None`` for a system post
    (a join, a header change: ``type`` is set), which is not a message."""
    if post.get("type"):
        return None
    name = author.get("username", post.get("user_id", ""))
    hint: PostKind | None = None
    if (post.get("props") or {}).get("from_webhook") == "true":
        hint = "integration"
    elif author.get("is_bot"):
        hint = "bot"
    text = post.get("message", "")
    create_at = int(post.get("create_at", 0))
    files = (post.get("metadata") or {}).get("files") or []
    return Post(
        id=post["id"],
        position=(create_at, post["id"]),
        time=create_at / 1000,
        thread=post.get("root_id") or post["id"],
        author=name,
        text=text,
        kind=roster.kind_of(name, hint),
        mentions=mentions_in(text, known),
        controls=parse_controls(text),
        words=count_words(text),
        author_id=post.get("user_id", ""),
        attachments=tuple(Attachment(f["id"], f.get("name", f["id"]), f.get("mime_type", ""), int(f.get("size", 0)))
                          for f in files if isinstance(f, dict) and f.get("id")),
    )


def display_name(user: dict) -> str:
    full = " ".join(part for part in (user.get("first_name", ""), user.get("last_name", "")) if part)
    return full or user.get("username", "")
