"""Render a turn as the prompt a live bot receives: the HINT of section 5.8, the
delivered posts labelled by thread, and the envelope contract of section 5.9.

Kept apart from the mechanisms so a prompt wording change cannot alter a trace.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .model import END_TOKEN, Hint, Member, TurnInput

if TYPE_CHECKING:
    from .classifier import ClassifierRequest

CONTRACT = f"""Answer with one JSON object and nothing else:
{{"replies": [{{"thread": "<thread id>", "to": ["<username>", ...], "text": "..."}},
              {{"thread": "<thread id>", "control": {{"mute": ["<username>"] | "all",
                                                   "unmute": [...] | "all"}}}},
              {{"thread": "<thread id>", "done": true}}],
 "schedule": [{{"in_seconds": N, "thread": "<thread id>", "text": "<what to check>"}}]}}
Each reply holds exactly one of: text (with non-empty "to"), control, text plus control,
or done. "thread" defaults to the starting thread. "to" lists recipients, never yourself;
use "all" only with a reason. Put control words in "control", never in text. Only the
thread's orchestrator may send a control. The thread's rate limit is the owner's; to keep
an exchange short, ask for it in text ("one post each, 60 words or fewer"). "schedule" is optional: use it when you are
waiting on someone and want to check back later. The exact text {END_TOKEN} on its own
means one done in the starting thread."""

INSTRUCTIONS = """Rules for this turn:
- Reply once per must-reply thread (text, control, or done). Do not restate what is
  already in the thread; say done when you have nothing to add.
- Optional threads: reply only if it saves a later turn. Closed threads: text and
  control are not allowed there; done means you have nothing to say, silence means you
  will come back to it.
- Treat "to" as the people your reply is for, and include everyone whose request it
  answers. Address whoever your reply unblocks. Respect the posts and words left in a
  thread.
- If you are waiting on someone, schedule a follow-up instead of posting a reminder."""


def render_intro(bot: Member, room: str, members: list[Member]) -> str:
    """The first message of a bot's session: who it is and who is in the room."""
    others = "\n".join(
        f"- {m.username} ({m.role}{', ' + m.profile if m.profile else ''})"
        for m in members
        if m.username != bot.username
    )
    return (
        f"You are {bot.username} ({bot.display_name or bot.username}), a bot in chat room "
        f"#{room}. Your role: {bot.profile or 'a helpful teammate'}."
        f"{' ' + bot.private_profile if bot.private_profile else ''}\nRoom members:\n{others}\n\n"
        f"{CONTRACT}"
    )


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
    return "\n".join(lines)


CLASSIFIER_CONTRACT = """Answer with one JSON object and nothing else:
{"add": [{"username": "<unaddressed bot that should speak>", "reason": "..."}],
 "remove": [{"username": "<addressed bot that seems unnecessary>", "reason": "..."}]}
Use empty lists when nobody should be added or removed. Name only bots from the list
above. Choose the smallest useful set: add a bot when the post needs it, not because it
could answer. An addressed bot answers anyway; "remove" only records your doubt."""


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
    history = "\n".join(f"[{p.thread}] #{p.seq} {p.author} ({p.kind}): {p.text}" for p in request.history) or "(none)"
    omitted = f" ({request.omitted} older posts omitted)" if request.omitted else ""
    addressed = ", ".join(request.addressed) or "none"
    return (
        f"You choose which bots should answer a new post in chat room #{room}.\n"
        f"Bots in the room:\n{agents}\n\n"
        f"New post:\n[{post.thread}] #{post.seq} {post.author} ({post.kind}): {post.text}\n"
        f"Addressed bots: {addressed}\n\n"
        f"Recent posts in this thread, newest first{omitted}:\n{history}"
    )


def render_classifier_prompt(request: ClassifierRequest, room: str) -> str:
    """The state plus the answer contract, for a classifier that writes text."""
    return f"{render_classifier_state(request, room)}\n\n{CLASSIFIER_CONTRACT}"


def render_prompt(turn: TurnInput) -> str:
    posts = "\n".join(
        f"[{p.thread}] #{p.seq} {p.author}: {p.text}" for p in turn.delivered
    ) or "(no new posts)"
    job = f"\nScheduled reminder fired: {turn.job_text}\n" if turn.job_text else ""
    error = f"\nYour previous reply was invalid: {turn.error}. Answer again.\n" if turn.error else ""
    return f"{render_hint(turn.hint)}\n{job}{error}\nNew posts since your last turn:\n{posts}\n\n{INSTRUCTIONS}"
