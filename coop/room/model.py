"""Data model of the room core: the terms of spec section 3, the parameters of
section 3.1, and the structures the mechanisms exchange with the doorbell."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, get_args

TurnSource = Literal["addressed", "picked", "catch-up", "unfinished"]
PostKind = Literal["human", "bot", "notice", "integration"]
Role = Literal["owner", "guest", "bot", "integration"]
Fallback = Literal["addressed-only", "everyone", "addressed-or-everyone"]
ALL = "all"
MAIN_THREAD = "main"
# The hosted-agent chain's end marker: in a reply body it is an error, the agent calls
# ``done`` instead (design §3.1, the 5.9 row).
END_TOKEN = "<end-of-agent-chain>"

# Where a post sits in the room's server order. The core only compares positions
# (``<``, ``==``) and stores them, so a platform may use any orderable, JSON-safe
# value: the test platform uses an ``int`` seq; a chat platform uses a
# ``(timestamp, post id)`` pair (design §3.1, the 5.6 row). ``None`` as a cursor or
# bound means "before every post".
Position = Any

# Mechanism names used by rule counters and trace events (section 5 headings).
MUTE = "mute"
RATE_POSTS = "rate_posts"
RATE_WORDS = "rate_words"
COOLDOWN = "cooldown"
AUTHORITY = "authority"
MUST_REPLY = "must_reply"
RECIPIENTS = "recipients"


def after(position: Position, bound: Position | None) -> bool:
    """Whether ``position`` comes after ``bound`` (``None`` = before everything)."""
    return bound is None or position > bound


@dataclass(frozen=True)
class Params:
    """Section 3.1. A count or duration of ``0`` disables that limit, except the two
    failure parameters, where ``0`` keeps the count for status and enforces nothing.

    Defaults are the simulator's choices; the real values are open decisions (section 9).
    """

    rate_limit_posts: int = 6
    rate_limit_words: int = 600
    rate_limit_seconds: float = 300
    cooldown_seconds: float = 0
    sweep_interval_seconds: float = 30
    catch_up_idle_seconds: float = 1800
    control_ttl_days: float = 7
    classifier_posts: int = 20
    classifier_timeout_seconds: float = 10
    classifier_retries: int = 1
    classifier_fallback: Fallback = "addressed-only"
    classifier_decision_ttl_seconds: float = 600
    failure_backoff_seconds: float = 30
    failure_backoff_max_seconds: float = 600
    consecutive_invalid_limit: int = 3
    # How long a wake's lease lives once an agent's poll has picked it up (design §3.2):
    # the agent's timeout for the room. Expiry is a backend failure.
    lease_seconds: float = 300
    # ``False`` leaves the classifier out: only addressed posts and the catch-up wake a bot.
    classifier_enabled: bool = True
    # Measure mode (design §1.1, §3.5), the default: ``False`` keeps every threshold and
    # every hint but holds no wake for a rule an agent could obey itself and refuses no
    # send; the counters record what enforce mode would have held or refused.
    enforce: bool = False

    def __post_init__(self) -> None:
        if self.classifier_fallback not in get_args(Fallback):
            raise ValueError(f"classifier_fallback: {self.classifier_fallback!r} is not one of {get_args(Fallback)}")

    @property
    def control_ttl_seconds(self) -> float:
        return self.control_ttl_days * 86400


@dataclass(frozen=True)
class Member:
    """A room member: a human with a role, a bot, or an integration account. ``profile``
    is the bot's bio as the classifier sees it (design §2: pushed by the agent)."""

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
    recorded; :data:`ALL` stands for the room-wide mention. ``author`` is the username
    the core keys everything by; ``author_id`` is the platform's server-authenticated id,
    carried into the wake payload."""

    id: str
    position: Position
    time: float
    thread: str
    author: str
    text: str
    kind: PostKind
    mentions: frozenset[str] = frozenset()
    controls: Controls | None = None
    words: int = 0
    author_id: str = ""

    def addresses(self, username: str) -> bool:
        return username in self.mentions or ALL in self.mentions


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
class Trigger:
    """The post that caused an addressed or picked wake (design §3.2); ``None`` on a
    catch-up or unfinished wake."""

    post_id: str
    author: str
    author_id: str
    author_role: str
    thread_root: str
    # Carried, not computed, in this increment (design §1.2).
    relayed: bool = False


@dataclass(frozen=True)
class Wake:
    """One doorbell event for one bot in one room (design §3.2). It carries metadata
    and never post text: the agent reads ``[cursor, hwm]`` itself through ``read``.
    ``lease_expires_at`` is ``None`` until an agent's poll picks the wake up."""

    wake_id: str
    room: str
    bot: str
    source: TurnSource
    starting_thread: str
    hwm: Position | None
    hint: Hint
    trigger: Trigger | None = None
    rules_version: int = 0
    lease_expires_at: float | None = None


@dataclass
class Reply:
    """One ``send``: a reply item of section 5.9 as the CLI states it. ``wake_id`` is
    attribution only (design §3.5): ``None`` is an unsolicited send."""

    thread: str
    to: list[str] | None = None
    text: str | None = None
    control: dict | None = None
    wake_id: str | None = None


@dataclass(frozen=True)
class Decision:
    """A classifier answer (section 5.1): usernames to add, usernames it would remove."""

    add: tuple[str, ...] = ()
    remove: tuple[str, ...] = ()
    reasons: dict[str, str] = field(default_factory=dict)
    fallback: str | None = None
