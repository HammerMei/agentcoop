"""Data model of the simulator: the terms of section 3, the parameters of section 3.1,
and the structures the mechanisms exchange."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

TurnSource = Literal["addressed", "picked", "catch-up", "unfinished", "job"]
PostKind = Literal["human", "bot", "notice", "integration"]
Role = Literal["owner", "guest", "bot", "integration"]
Fallback = Literal["addressed-only", "everyone", "addressed-or-everyone"]

ALL = "all"
MAIN_THREAD = "main"
END_TOKEN = "<end-of-agent-chain>"

# Mechanism names used by rule counters and trace events (section 5 headings).
MUTE = "mute"
RATE_POSTS = "rate_posts"
RATE_WORDS = "rate_words"
COOLDOWN = "cooldown"
AUTHORITY = "authority"
MUST_REPLY = "must_reply"
RECIPIENTS = "recipients"
CLOSED_THREAD = "closed_thread"


@dataclass(frozen=True)
class Params:
    """Section 3.1. A count or duration of ``0`` disables that limit, except the two
    failure parameters, where ``0`` keeps the count for status and enforces nothing.

    Defaults are simulator choices; the real values are open decisions (section 9).
    """

    rate_limit_posts: int = 6
    rate_limit_words: int = 600
    rate_limit_seconds: float = 300
    cooldown_seconds: float = 0
    sweep_interval_seconds: float = 30
    catch_up_post_threshold: int = 3
    catch_up_age_seconds: float = 120
    control_ttl_days: float = 7
    classifier_posts: int = 20
    classifier_timeout_seconds: float = 10
    classifier_retries: int = 1
    classifier_fallback: Fallback = "addressed-only"
    classifier_decision_ttl_seconds: float = 600
    follow_up_retry_cap: int = 3
    failure_backoff_seconds: float = 30
    failure_backoff_max_seconds: float = 600
    consecutive_invalid_limit: int = 3
    # Simulator switches, not section 3.1 parameters.
    classifier_enabled: bool = True
    # ``False`` keeps every threshold and every HINT but holds no turn and rejects no
    # rule violation that an agent could have obeyed itself; the counters record what
    # the enforced mode would have blocked (plan amendment, 2026-11-10).
    enforce: bool = True

    @property
    def control_ttl_seconds(self) -> float:
        return self.control_ttl_days * 86400


@dataclass(frozen=True)
class Member:
    """A room member: a human with a role, a bot, or an integration account."""

    username: str
    role: Role
    display_name: str = ""
    nickname: str = ""
    profile: str = ""

    @property
    def is_bot(self) -> bool:
        return self.role == "bot"


@dataclass(frozen=True)
class Controls:
    """Control words parsed from one visible post (section 3, "control word").

    ``mute`` / ``unmute`` hold explicit usernames or :data:`ALL`. ``limit`` holds the
    keys that were written (``posts``, ``words``). ``orchestrator`` is a username,
    ``"none"``, or ``None`` when not written.
    """

    mute: tuple[str, ...] | str | None = None
    unmute: tuple[str, ...] | str | None = None
    limit: dict[str, int] | None = None
    orchestrator: str | None = None

    def is_empty(self) -> bool:
        return (
            self.mute is None
            and self.unmute is None
            and self.limit is None
            and self.orchestrator is None
        )


@dataclass(frozen=True)
class Post:
    """One platform message in server order. ``mentions`` are the mentions the platform
    recorded; :data:`ALL` stands for the room-wide mention."""

    seq: int
    time: float
    thread: str
    author: str
    text: str
    kind: PostKind
    mentions: frozenset[str] = frozenset()
    controls: Controls | None = None
    # Set on a bot's canonical post when the envelope said ``done`` with ``to`` and
    # nothing was posted: recorded, never a platform post. Kept for status only.
    words: int = 0

    @property
    def id(self) -> str:
        return f"p{self.seq}"

    def addresses(self, username: str) -> bool:
        return username in self.mentions or ALL in self.mentions


@dataclass
class ReplyItem:
    """One item of a reply envelope (section 5.9)."""

    thread: str | None = None
    to: list[str] | None = None
    text: str | None = None
    control: dict | None = None
    done: bool = False


@dataclass
class JobRequest:
    """Simulator stand-in for the scheduler tool a follow-up skill would call: the bot
    asks for a reminder in ``thread`` after ``in_seconds`` carrying ``text``."""

    in_seconds: float
    text: str
    thread: str | None = None


@dataclass
class Envelope:
    replies: list[ReplyItem] = field(default_factory=list)
    schedule: list[JobRequest] = field(default_factory=list)


@dataclass(frozen=True)
class ThreadGroup:
    """How HINT presents one delivered thread (section 5.8)."""

    thread: str
    group: Literal["must-reply", "optional", "closed"]
    # Why a thread is closed, by mechanism name; empty for open threads.
    closed_by: tuple[str, ...] = ()
    posts_left: int | None = None
    words_left: int | None = None


@dataclass(frozen=True)
class Hint:
    """Everything section 5.8 says a turn receives."""

    source: TurnSource
    starting_thread: str
    threads: tuple[ThreadGroup, ...]
    refused_last_turn: tuple[str, ...] = ()
    previous_failure: str | None = None
    backoff_until: float | None = None
    degraded: bool = False

    def group_of(self, thread: str) -> ThreadGroup | None:
        for group in self.threads:
            if group.thread == thread:
                return group
        return None

    @property
    def must_reply(self) -> tuple[str, ...]:
        return tuple(g.thread for g in self.threads if g.group == "must-reply")


@dataclass(frozen=True)
class TurnInput:
    """What one backend invocation receives: the posts after the cursor and the hint."""

    bot: str
    source: TurnSource
    starting_thread: str
    delivered: tuple[Post, ...]
    hint: Hint
    # Scheduled-job prompt text when ``source == "job"``.
    job_text: str | None = None
    # The schema error returned to the session once after an invalid envelope (5.9).
    error: str | None = None


@dataclass
class BotOutput:
    """What a :class:`~tools.roomsim.bots.BotRunner` returns for one turn."""

    text: str
    duration_seconds: float = 1.0
    input_tokens: int = 0
    output_tokens: int = 0
    failed: bool = False
    error: str = ""


@dataclass(frozen=True)
class Decision:
    """A classifier answer (section 5.1): usernames to add, usernames it would remove."""

    add: tuple[str, ...] = ()
    remove: tuple[str, ...] = ()
    reasons: dict[str, str] = field(default_factory=dict)
    fallback: str | None = None


@dataclass
class Job:
    """A scheduled reminder for one bot in one thread (section 5.11)."""

    id: int
    bot: str
    thread: str
    text: str
    due: float
    retries: int = 0
    # Set while a firing waits in backlog because the bot is muted or degraded.
    pending: bool = False
