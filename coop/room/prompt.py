"""Render what an agent and the classifier read: the HINT of section 5.8 (what ``coop
read`` prints ahead of the posts) and the classifier's state (section 5.1).

Kept apart from the mechanisms so a wording change cannot alter a trace.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .model import Hint

if TYPE_CHECKING:
    from .classifier import ClassifierRequest


def render_hint(hint: Hint) -> str:
    lines = [f"Turn source: {hint.source}. Starting thread: {hint.starting_thread}."]
    for group in hint.threads:
        extra = ""
        if group.group == "closed":
            extra = f" (closed by {', '.join(group.closed_by)})"
        elif group.posts_left is not None or group.words_left is not None:
            extra = f" ({group.posts_left} posts, {group.words_left} words left)"
        lines.append(f"- {group.thread}: {group.group}{extra}")
    if hint.refused_last_turn:
        lines.append(f"The platform refused your last replies in: {', '.join(hint.refused_last_turn)}.")
    if hint.previous_failure:
        lines.append(f"Your previous turn in this room failed: {hint.previous_failure}.")
    if hint.backoff_until is not None:
        lines.append(f"You are in backoff until t={hint.backoff_until:.0f}.")
    if hint.degraded:
        lines.append("You are degraded in this room after repeated invalid replies.")
    if hint.source == "catch-up":
        lines.append(f"Catch-up in {hint.starting_thread}: you were not addressed. Reply only if you have "
                     "something to add or revise that the thread does not already have; otherwise reply done.")
    return "\n".join(lines)


def one_line(text: str) -> str:
    """Newlines collapsed to one space, so no post can forge a header line (design §3.3)."""
    return " ".join(text.split())



CLASSIFIER_CONTRACT = """Answer with one JSON object and nothing else:
{"add": [{"username": "<unaddressed bot with something to add or revise>", "reason": "..."}],
 "remove": [{"username": "<addressed bot that seems unnecessary>", "reason": "..."}]}
Use empty lists when nobody should be added or removed. Name only bots from the list
above. Add a bot when its role or knowledge gives the thread something it does not
already have: a correction, an addition, or a question or opinion that adds value; not
because it could answer. A greeting or an acknowledgement needs nobody. An addressed
bot answers anyway; "remove" only records your doubt."""


def render_classifier_state(request: ClassifierRequest, room: str) -> str:
    """What the speaker classifier sees (section 5.1): the post with its addressed bots,
    recent thread history newest first, and the agent list with profiles. Nothing else."""
    agents = "\n".join(
        f"- {m.username}" + (f" (display name {m.display_name!r}" + (f", nickname {m.nickname!r}" if m.nickname else "")
                             + ")" if m.display_name else (f" (nickname {m.nickname!r})" if m.nickname else ""))
        + f": {m.profile or 'no profile'}"
        for m in request.agents
    ) or "(none)"
    post = request.post
    history = "\n".join(f"[{p.thread}] #{p.id} {p.author} ({p.kind}): {one_line(p.text)}"
                        for p in request.history) or "(none)"
    omitted = f" ({request.omitted} older posts omitted)" if request.omitted else ""
    addressed = ", ".join(request.addressed) or "none"
    return (
        f"You choose which bots should answer a new post in chat room #{room}.\n"
        f"Bots in the room:\n{agents}\n\n"
        f"New post:\n[{post.thread}] #{post.id} {post.author} ({post.kind}): {one_line(post.text)}\n"
        f"Addressed bots: {addressed}\n\n"
        f"Recent posts in this thread, newest first{omitted}:\n{history}"
    )


def render_classifier_prompt(request: ClassifierRequest, room: str) -> str:
    """The state plus the answer contract, for a classifier that writes text."""
    return f"{render_classifier_state(request, room)}\n\n{CLASSIFIER_CONTRACT}"
