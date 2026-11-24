"""One room's core: the mechanisms of spec section 5 applied to the bots this gateway
hosts, ported from ``tools/roomsim/gateway.py`` with the turn rewritten for remote
agents (design §3.1).

The end-to-end flow of section 4 is one method per step, named after the
specification's mechanism names, so a rule can be found from the text and back.
State is split into what is persisted (cursor, unfinished threads, thread settings,
the paused flag) and what is kept in memory (rate window, cooldown, backoff, invalid
streaks, decision cache, the outstanding wake), so ``restart`` resets exactly the
second kind.

A turn: ``_start_turn`` issues a wake (the hint fixed, the high-water mark fixed) and
holds it in the bot's one slot; the agent's :meth:`poll` picks it up and the lease
starts; :meth:`read` records that the agent fetched ``[cursor, hwm]``; every
:meth:`send` is one reply checked against the hint (a wake's send) or the room's
current rules (an unsolicited send) and posted on the bot's behalf; :meth:`done`
closes the turn, saves the cursor and clears the unfinished threads the turn
answered. A lease that expires, a ``done --failed``, or a ``done`` without a read is
a backend failure (section 5.10); a must-reply thread with neither a send on the
ledger nor ``--answered`` is an invalid turn.

The ``enforce`` switch of :class:`~coop.room.model.Params`: when off, a hold by mute,
rate or cooldown does not stop a wake and a rule problem in a send does not refuse
it; the hint still says what the rule is, and every such opportunity is counted as
``complied`` or ``violated`` (``held`` / ``rejected`` when enforced). Backoff,
degraded state, pause, one wake per bot, the cursor and the failure notice are not
switched: a bot cannot obey those by itself.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace

from ..errors import InvalidReply, NoLiveLease, RefusedSend
from .classifier import Classifier, ClassifierRequest, ClassifierResult, fallback_decision
from .clock import Clock, Handle
from .controls import is_control_only
from .model import (
    ALL,
    COOLDOWN,
    MUST_REPLY,
    MUTE,
    RATE_POSTS,
    RATE_WORDS,
    Decision,
    Hint,
    Params,
    Position,
    Post,
    Reply,
    ThreadGroup,
    Trigger,
    TurnSource,
    Wake,
    after,
)
from .platform import Platform, PlatformError, RefusedPost
from .replies import Problem, ValidationContext, canonical_text, check_reply, resolve_control
from .roster import Roster
from .trace import Trace
from .words import count_words

logger = logging.getLogger(__name__)

NOTICE_PREFIX = "SYSTEM ERROR: turn failed:"
HINTED_THREAD = "hinted_thread"  # a wake's send outside its hint: counted, never refused (design §3.1, 5.9 row)


# ── state ───────────────────────────────────────────────────────────────────


@dataclass
class ThreadSettings:
    """Persisted per thread (section 5.4): the latest value of each control kind with
    the position of its source post, and the newest post time observed."""

    orchestrator: str | None = None
    orchestrator_at: Position | None = None
    muted: dict[str, Position] = field(default_factory=dict)  # username -> source position
    unmuted_at: dict[str, Position] = field(default_factory=dict)
    limit_posts: int | None = None
    limit_words: int | None = None
    limit_at: Position | None = None
    last_activity: float = 0.0
    unknown_mutes: list[str] = field(default_factory=list)

    @property
    def is_default(self) -> bool:
        """No control has ever applied here: nothing but ``last_activity``, which a
        fresh entry gets again on its next lookup."""
        return (self.orchestrator is None and not self.muted and not self.unmuted_at and self.limit_posts is None
                and self.limit_words is None and not self.unknown_mutes)


@dataclass
class ThreadVolatile:
    recent: list[tuple[float, str, int]] = field(default_factory=list)  # (time, author, words)
    held_by_rate: set[str] = field(default_factory=set)

    @property
    def is_empty(self) -> bool:
        return not self.recent and not self.held_by_rate


@dataclass
class BotPersisted:
    cursor: Position | None = None
    unfinished: set[str] = field(default_factory=set)
    # ``coop pause``: held for every thread like a muted bot, no wake of any source,
    # the cursor stays, the backlog waits for ``resume`` (design §3.1, 5.5 row).
    paused: bool = False
    # ``coop reset``: the next wake tells the agent to start a fresh session.
    reset_pending: bool = False


@dataclass
class TurnRun:
    """One outstanding wake: issued, then leased from the agent's poll until ``done``
    or expiry. Both lists are ``[cursor, hwm]`` fixed at issue: ``delivered`` is the
    posts of others, what the hint and the trace are built from; ``payload`` adds the
    bot's own, and is what ``read`` returns (design 5.6 row: an own post is context,
    never cause)."""

    wake: Wake
    hwm: Position | None
    delivered: tuple[Post, ...]
    payload: tuple[Post, ...]
    leased: bool = False
    expires_at: float | None = None
    expiry: Handle | None = None
    # The agent fetched [cursor, hwm] through ``read``; a wake with nothing to read, its
    # own posts counted, records an empty complete read (design §3.3).
    read_complete: bool = False
    # Threads a ``send --wake`` landed in: what covers a must-reply thread at ``done``.
    ledger: set[str] = field(default_factory=set)
    problems: set[tuple[str, str | None]] = field(default_factory=set)
    # (thread, "control" | "text") items the bot sent: what it tried.
    items_seen: set[tuple[str, str]] = field(default_factory=set)
    posts: int = 0
    refused: list[str] = field(default_factory=list)
    # Reasons of the sends enforce mode refused: one such send makes the turn invalid
    # at ``done`` (design §3.1, the 5.10 row).
    rejected: list[str] = field(default_factory=list)


@dataclass
class BotVolatile:
    turn: TurnRun | None = None
    sweep_requested: bool = False
    # When the bot last ended a turn normally in this room, or the gateway (re)started:
    # the periodic catch-up timer counts from here.
    idle_since: float = 0.0
    last_post: dict[str, float] = field(default_factory=dict)
    retry_after: float = 0.0
    backoff_level: int = 0
    backoff_notice_sent: bool = False
    invalid_streak: int = 0
    degraded: bool = False
    invalid_notice_sent: bool = False
    previous_failure: str | None = None
    refused: tuple[str, ...] = ()
    expired_decisions: set[str] = field(default_factory=set)
    # Sends the agent made outside any wake, counted apart from turn replies (design §3.5).
    unsolicited_sends: int = 0
    # (trigger post id or thread, cause) holds already counted as held, so a hold the
    # periodic sweep finds again is one opportunity, not one per sweep.
    counted_holds: set[tuple[object, str]] = field(default_factory=set)


@dataclass
class CachedDecision:
    decision: Decision | None = None  # None while in flight
    time: float = 0.0
    position: Position | None = None  # the post's: evicted once every hosted cursor has passed it


@dataclass
class RuleCount:
    """Per mechanism. Two pairs compare across modes:

    - ``rejected`` (enforced: the bot sent a reply the gateway refused) against
      ``violated`` (not enforced: the gateway let it through); ``complied`` is the bot
      obeying on its own in a thread it was not started for;
    - ``held`` (enforced: the gateway never issued the wake) against
      ``held_complied`` / ``held_violated`` (not enforced: the turn ran for a thread the
      rule would have held, and the bot stayed silent there or did not)."""

    opportunities: int = 0
    complied: int = 0
    violated: int = 0
    rejected: int = 0
    held: int = 0
    held_complied: int = 0
    held_violated: int = 0


class _RoomLock:
    """One serialised event queue per room, re-entrant for the task that holds it: a
    platform that delivers a bot's own post inline (the test platform) re-enters
    ``on_post`` from inside ``send``; a connector delivers from its own task and waits."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task | None = None
        self._depth = 0

    async def __aenter__(self) -> None:
        task = asyncio.current_task()
        if self._owner is task:
            self._depth += 1
            return
        await self._lock.acquire()
        self._owner, self._depth = task, 1

    async def __aexit__(self, *exc) -> None:
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._lock.release()


# ── the core ────────────────────────────────────────────────────────────────


class RoomCore:
    def __init__(
        self,
        room: str,
        platform: Platform,
        clock: Clock,
        trace: Trace,
        hosted: list[str],
        roster: Roster,
        params: Params,
        classifier: Classifier | None = None,
        name: str = "g1",
        rules_version: int = 0,
        wake_seed: int = 0,
    ) -> None:
        self.room = room
        self.platform = platform
        self.clock = clock
        self.trace = trace
        self.name = name
        self.hosted = list(hosted)
        self.roster = roster
        self.agents = list(roster.bots)
        self.params = params
        self.classifier = classifier
        self.rules_version = rules_version
        # Bios the agents pushed (design §3.6 step 3), by the bot's username in this
        # room, kept in step by the gateway; the classifier reads a member's bio in
        # place of the platform profile.
        self.bios: dict[str, str] = {}
        self.online = True
        self.started = False
        self.settings: dict[str, ThreadSettings] = {}
        self.thread_state: dict[str, ThreadVolatile] = {}
        self.persisted: dict[str, BotPersisted] = {b: BotPersisted() for b in hosted}
        self.volatile: dict[str, BotVolatile] = {b: BotVolatile() for b in hosted}
        self.decisions: dict[str, CachedDecision] = {}
        self.replaying = False
        self.rules: dict[str, RuleCount] = {}
        self.on_wake: Callable[[Wake], Awaitable[None]] | None = None
        # Told after every change to the persisted state (a commit, a control, pause,
        # resume, reset): the daemon saves the room then.
        self.on_change: Callable[[], None] | None = None
        # Wake ids count up from the seed, so a daemon seeding it from its start time
        # never reissues an id a previous run gave out.
        self._wake_seq = wake_seed
        # Every entry runs under this lock (design: one serialised event queue per room);
        # the classifier call itself runs outside it.
        self._lock = _RoomLock()

    # ── lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> None:
        self.started = True
        for bot in self.hosted:
            self.volatile[bot].idle_since = self.now()
            self._schedule_periodic_sweep(bot)

    def _schedule_periodic_sweep(self, bot: str) -> None:
        if self.params.sweep_interval_seconds > 0:
            self.clock.at(self.params.sweep_interval_seconds, self._periodic_sweep, bot)

    async def _periodic_sweep(self, bot: str) -> None:
        if bot not in self.hosted:
            return  # removed in place: its timer ends here
        try:
            await self.sweep(bot, "periodic")
        finally:
            self._schedule_periodic_sweep(bot)  # a sweep that raised must not end the timer

    # ── changes in place (a reload, a bot joining or leaving) ───────────────

    def add_hosted(self, bot: str, cursor: Position | None) -> None:
        """Host ``bot`` here from now on, its cursor at ``cursor`` (the room's latest
        post for a bot new to the room, or ``None`` for a room created around it,
        design §3.6 step 4). Everything about the other bots stays as it is."""
        if bot in self.hosted:
            return
        if bot not in self.agents:
            raise ValueError(f"{bot} is not in the roster of room {self.room!r}")
        self.hosted.append(bot)
        self.persisted[bot] = BotPersisted(cursor=cursor)
        self.volatile[bot] = BotVolatile(idle_since=self.now())
        if self.started:  # before ``start`` the room's own start schedules it, once
            self._schedule_periodic_sweep(bot)
            # what is undelivered above its cursor is its to catch up on now, not at the
            # next periodic sweep (which an operator may have turned off): the posts of
            # a room created around it, or those since a restored cursor
            self._request_sweep(bot, "hosted")
        self.trace.add(self.now(), "hosted_added", gateway=self.name, bot=bot)

    async def remove_hosted(self, bot: str) -> None:
        """Stop hosting ``bot`` here. An outstanding wake ends as a failure; its state is
        dropped, the file the daemon keeps included (the caller's). Other bots' leases,
        backoff and rate windows are untouched."""
        async with self._lock:
            if bot not in self.hosted:
                return
            run = self.volatile[bot].turn
            if run is not None:
                if run.expiry is not None:
                    run.expiry.cancel()
                self._finish(run, "removed")
            self.hosted.remove(bot)
            self.persisted.pop(bot, None)
            self.volatile.pop(bot, None)
            self.trace.add(self.now(), "hosted_removed", gateway=self.name, bot=bot)

    def update(self, roster: Roster, params: Params, classifier: Classifier | None = None) -> None:
        """A reload's new roster, parameters and classifier, applied in place. A sweep
        interval that was off starts its timers now."""
        was_off = self.params.sweep_interval_seconds <= 0
        self.roster = roster
        self.agents = list(roster.bots)
        self.params = params
        self.classifier = classifier
        if was_off and params.sweep_interval_seconds > 0:
            for bot in self.hosted:
                self._schedule_periodic_sweep(bot)

    def _changed(self) -> None:
        if self.on_change is not None:
            self.on_change()

    @property
    def wake_seq(self) -> int:
        """The last wake number minted: what a core opened later on this handle counts from."""
        return self._wake_seq

    def set_offline(self) -> None:
        self.online = False

    async def set_online(self, replay_after: Position | None) -> None:
        """Reconnect: posts after ``replay_after`` are replayed as new triggers; earlier
        ones were missed (section 5.6)."""
        self.online = True
        await self._replay(replay_after)

    async def _replay(self, replay_after: Position | None) -> None:
        """Replayed posts are new triggers for what is still undelivered and seed the rate
        window, own posts included, since none of them was counted at post time (5.3)."""
        self.replaying = True
        try:
            for post in self.platform.posts_after(replay_after):
                await self.on_post(post)
        finally:
            self.replaying = False

    async def restart(self, replay_after: Position | None) -> None:
        """Keep persisted state, reset in-memory state, replay recent posts to seed the
        rate window (section 5.3) and as new triggers (section 5.6). An outstanding wake
        is forgotten: a ``done`` for it finds no live lease and moves nothing."""
        self.thread_state = {}
        self.volatile = {b: BotVolatile(idle_since=self.now()) for b in self.hosted}
        self.decisions = {}
        self.online = True
        await self._replay(replay_after)

    # ── helpers over state ──────────────────────────────────────────────────

    def now(self) -> float:
        return self.clock.now()

    def _thread(self, thread: str) -> ThreadSettings:
        settings = self.settings.setdefault(thread, ThreadSettings(last_activity=self.now()))
        if self.params.control_ttl_seconds and self.now() - settings.last_activity > self.params.control_ttl_seconds:
            fresh = ThreadSettings(last_activity=settings.last_activity)
            self.settings[thread] = fresh
            return fresh
        return settings

    def _tstate(self, thread: str) -> ThreadVolatile:
        return self.thread_state.setdefault(thread, ThreadVolatile())

    def _bots_in_room(self) -> list[str]:
        members = self.platform.members()
        return [a for a in self.agents if a in members]

    def _cursor(self, bot: str) -> Position | None:
        return self.persisted[bot].cursor

    def _addresses(self, post: Post, bot: str) -> bool:
        """Whether ``post`` addresses ``bot``: a mention, or any post in a 1:1 DM, which
        addresses its bot by definition (spec section 3); a control-only post addresses
        nobody. One predicate for arrival, the sweep and the hint."""
        if post.addresses(bot):
            return True
        return self.platform.is_dm and not (post.controls is not None and is_control_only(post.text))

    def is_muted(self, bot: str, thread: str) -> bool:
        return bot in self._thread(thread).muted

    def is_orchestrator(self, bot: str, thread: str) -> bool:
        return self._thread(thread).orchestrator == bot

    def _request_sweep(self, bot: str, reason: str) -> None:
        if bot in self.volatile and not self.volatile[bot].sweep_requested:
            self.volatile[bot].sweep_requested = True
            self.clock.at(0, self._requested_sweep, bot, reason)

    async def _requested_sweep(self, bot: str, reason: str) -> None:
        if bot not in self.volatile:
            return  # removed in place before the sweep ran
        self.volatile[bot].sweep_requested = False
        await self.sweep(bot, reason)

    def on_thread_removed(self, thread: str) -> None:
        """Archive or deletion (section 5.4): settings and unfinished state go."""
        self.settings.pop(thread, None)
        for state in self.persisted.values():
            state.unfinished.discard(thread)

    def pause(self, bot: str) -> None:
        """No wake of any source until ``resume`` (design §3.1, the 5.5 row): a wake
        issued but not yet polled is withdrawn; a leased one runs to its ``done``."""
        self.persisted[bot].paused = True
        run = self.volatile[bot].turn
        if run is not None and not run.leased:
            self.volatile[bot].turn = None
            self.trace.add(self.now(), "turn_end", gateway=self.name, bot=bot, wake=run.wake.wake_id,
                           outcome="withdrawn", posts=0)
        self.trace.add(self.now(), "paused", gateway=self.name, bot=bot)
        self._changed()

    def resume(self, bot: str) -> None:
        self.persisted[bot].paused = False
        self.trace.add(self.now(), "resumed", gateway=self.name, bot=bot)
        self._changed()
        self._request_sweep(bot, "resume")

    def reset(self, bot: str) -> None:
        """The next wake for ``bot`` carries ``reset``: the agent starts a fresh session
        for this room (design §3.4)."""
        self.persisted[bot].reset_pending = True
        self.trace.add(self.now(), "reset_requested", gateway=self.name, bot=bot)
        self._changed()

    # ── section 4: a post arrives ───────────────────────────────────────────

    async def on_post(self, post: Post) -> None:
        """One post from the room, in server order. Copies of one post that several
        bot accounts stream are deduplicated before this point (the room log)."""
        async with self._lock:
            await self._on_post(post)

    async def _on_post(self, post: Post) -> None:
        if not self.online:
            return
        copies = [b for b in self.hosted if b != post.author]
        self._thread(post.thread).last_activity = max(self._thread(post.thread).last_activity, post.time)
        if post.kind == "notice":
            return  # recognised before eligibility: no turn, no classifier, not in the window
        self._count_for_rate(post)  # a human post clears the window even when control-only (5.3)
        if not copies:
            return  # the gateway's own post: counted (on replay) but no trigger
        self._warn_if_late(post, copies)
        if self._apply_controls(post):
            return
        addressed = tuple(b for b in copies if self._addresses(post, b) and after(post.position, self._cursor(b)))
        for bot in addressed:
            await self._try_start(bot, "addressed", post.thread, trigger=post)
        if any(after(post.position, self._cursor(b)) for b in copies):
            self._classify(post, addressed)

    # step 1 — AUTHORITY / SETTINGS
    def _apply_controls(self, post: Post) -> bool:
        controls = post.controls
        if controls is None:
            return False
        settings = self._thread(post.thread)
        moderator = self.roster.is_moderator(post.author)
        if not moderator and post.author != settings.orchestrator:
            self.trace.add(self.now(), "control_ignored", gateway=self.name, thread=post.thread,
                           by=post.author, reason="not authorised")
            return False
        if controls.orchestrator is not None and moderator and after(post.position, settings.orchestrator_at):
            name = None if controls.orchestrator == "none" else controls.orchestrator
            if name is None or name in self._bots_in_room():  # the orchestrator is a bot in the room (5.12)
                settings.orchestrator = name
                settings.orchestrator_at = post.position
                self._trace_control(post, "orchestrator", settings.orchestrator)
            else:
                self.trace.add(self.now(), "control_ignored", gateway=self.name, thread=post.thread,
                               by=post.author, reason=f"orchestrator {name!r} is not a bot in the room")
        if controls.mute is not None:
            self._apply_mute(post, settings, controls.mute)
        if controls.unmute is not None:
            self._apply_unmute(post, settings, controls.unmute)
        if controls.limit is not None and moderator and after(post.position, settings.limit_at):
            settings.limit_posts = controls.limit.get("posts", settings.limit_posts)
            settings.limit_words = controls.limit.get("words", settings.limit_words)
            settings.limit_at = post.position
            self._trace_control(post, "limit", dict(controls.limit))
        return is_control_only(post.text)

    def _trace_control(self, post: Post, control: str, value) -> None:
        self.trace.add(self.now(), "control_applied", gateway=self.name, thread=post.thread,
                       by=post.author, control=control, value=value, post=post.id,
                       age=self.now() - post.time)  # > control TTL only in a replay (5.4)
        self._changed()

    def _resolve_names(self, names, settings: ThreadSettings, muted_only: bool) -> list[str]:
        bots = self._bots_in_room()
        if names == ALL:
            if muted_only:
                return [b for b in bots if b in settings.muted]
            return [b for b in bots if b != settings.orchestrator]
        known = [n for n in names if n in bots]
        settings.unknown_mutes.extend(n for n in names if n not in bots)
        return known

    def _apply_mute(self, post: Post, settings: ThreadSettings, names) -> None:
        targets = self._resolve_names(names, settings, muted_only=False)
        applied = []
        for bot in targets:
            if after(post.position, settings.unmuted_at.get(bot)) and after(post.position, settings.muted.get(bot)):
                settings.muted[bot] = post.position
                applied.append(bot)
        if applied:
            self._trace_control(post, "mute", applied)

    def _apply_unmute(self, post: Post, settings: ThreadSettings, names) -> None:
        targets = self._resolve_names(names, settings, muted_only=True)
        applied = []
        for bot in targets:
            if after(post.position, settings.muted.get(bot)):
                settings.muted.pop(bot, None)
                settings.unmuted_at[bot] = post.position
                applied.append(bot)
                self._request_sweep(bot, "unmute")
        if applied:
            self._trace_control(post, "unmute", applied)

    # RATE_LIMIT bookkeeping
    def _count_for_rate(self, post: Post) -> None:
        state = self._tstate(post.thread)
        if post.kind == "human":
            state.recent.clear()
            for bot in list(state.held_by_rate):
                self._request_sweep(bot, "human post")
            state.held_by_rate.clear()
        elif post.kind == "bot" and post.author in self.agents and (post.author not in self.hosted or self.replaying):
            if not self.is_orchestrator(post.author, post.thread):
                state.recent.append((post.time, post.author, post.words))

    def _count_own_post(self, bot: str, thread: str, time: float, words: int) -> tuple[float, str, int] | None:
        """Count a post the gateway is about to make on the bot's behalf, before any
        gateway, this one included, receives it from the platform (5.3): the echo of a
        hosted bot's post is not counted again in ``_count_for_rate``."""
        self.volatile[bot].last_post[thread] = time
        if self.is_orchestrator(bot, thread):
            return None
        entry = (time, bot, words)
        self._tstate(thread).recent.append(entry)
        return entry

    def _uncount_own_post(self, bot: str, thread: str, entry: tuple[float, str, int] | None,
                          previous_last: float | None) -> None:
        """The platform refused the post: what was counted for it is taken back."""
        if entry is not None:
            recent = self._tstate(thread).recent
            if entry in recent:
                recent.remove(entry)
        if previous_last is None:
            self.volatile[bot].last_post.pop(thread, None)
        else:
            self.volatile[bot].last_post[thread] = previous_last

    def _recent(self, thread: str) -> list[tuple[float, str, int]]:
        state = self._tstate(thread)
        if self.params.rate_limit_seconds > 0:
            cutoff = self.now() - self.params.rate_limit_seconds
            state.recent = [r for r in state.recent if r[0] > cutoff]
        return state.recent

    def _limits(self, thread: str) -> tuple[int, int]:
        settings = self._thread(thread)
        posts = settings.limit_posts if settings.limit_posts is not None else self.params.rate_limit_posts
        words = settings.limit_words if settings.limit_words is not None else self.params.rate_limit_words
        return posts, words

    def _rate_causes(self, bot: str, thread: str) -> list[str]:
        """Which rate gates currently close ``thread`` to ``bot`` (none for its orchestrator)."""
        if self.is_orchestrator(bot, thread):
            return []
        posts, words = self._limits(thread)
        recent = self._recent(thread)
        causes = []
        if posts and len(recent) >= posts:
            causes.append(RATE_POSTS)
        if words and sum(r[2] for r in recent) >= words:
            causes.append(RATE_WORDS)
        return causes

    def _in_cooldown(self, bot: str, thread: str) -> bool:
        if self.is_orchestrator(bot, thread):
            return False  # always exempt from rate and cooldown gates (4.1)
        last = self.volatile[bot].last_post.get(thread)
        return bool(self.params.cooldown_seconds) and last is not None \
            and self.now() - last < self.params.cooldown_seconds

    # step 2/3/5/6 — TURN_ELIGIBILITY, MUTE, RATE_LIMIT, ONE_TURN
    def _exempt(self, source: TurnSource, thread: str, trigger: Post | None) -> bool:
        if source != "addressed" or trigger is None:
            return False
        return trigger.kind == "human" or trigger.author == self._thread(thread).orchestrator

    def _hard_hold(self, bot: str, source: TurnSource, thread: str, exempt: bool,
                   from_sweep: bool) -> str | None:
        """Holds that are not switched off by ``enforce``: pause, the muted column for a
        degraded bot, backoff, and one wake per bot. Only a newly arriving human or
        orchestrator mention bypasses backoff and the degraded state; the sweeper's
        retry of a failed turn waits like any other sweep-started turn."""
        state = self.volatile[bot]
        if self.persisted[bot].paused:
            return "paused"
        if state.turn is not None:
            return "one_turn"
        waits = from_sweep or not exempt
        if state.degraded and waits:
            return "degraded"
        if waits and self.now() < state.retry_after:
            return "backoff"
        return None

    def _rule_holds(self, bot: str, source: TurnSource, thread: str, exempt: bool) -> list[str]:
        causes = [MUTE] if self.is_muted(bot, thread) else []
        if exempt:
            return []
        causes += self._rate_causes(bot, thread)
        if self._in_cooldown(bot, thread):
            causes.append(COOLDOWN)
        return causes

    async def _try_start(
        self,
        bot: str,
        source: TurnSource,
        thread: str,
        trigger: Post | None = None,
        hwm: Position | None = None,
        from_sweep: bool = False,
    ) -> bool:
        exempt = self._exempt(source, thread, trigger)
        hard = self._hard_hold(bot, source, thread, exempt, from_sweep)
        if hard:
            self._held(bot, source, thread, hard)
            return False
        holds = self._rule_holds(bot, source, thread, exempt)
        key = trigger.id if trigger is not None else thread
        if holds and self.params.enforce:
            for cause in holds:
                if (key, cause) not in self.volatile[bot].counted_holds:
                    self.volatile[bot].counted_holds.add((key, cause))
                    self._count_rule(bot, thread, cause, "held")
            self._held(bot, source, thread, holds[0])
            return False
        self.volatile[bot].counted_holds = {k for k in self.volatile[bot].counted_holds if k[0] != key}
        await self._start_turn(bot, source, thread, trigger, hwm, held_by=tuple(holds))
        return True

    def _held(self, bot: str, source: TurnSource, thread: str, gate: str) -> None:
        self.trace.add(self.now(), "turn_held", gateway=self.name, bot=bot, source=source,
                       thread=thread, gate=gate)
        if gate in (RATE_POSTS, RATE_WORDS):
            self._tstate(thread).held_by_rate.add(bot)

    # step 4 — WHO_SPEAKS
    def _classify(self, post: Post, addressed: tuple[str, ...]) -> None:
        if not self._classifier_applies(post):
            return
        self.decisions[post.id] = CachedDecision(position=post.position)
        self.trace.add(self.now(), "classifier_call", gateway=self.name, post=post.id)
        request = self._request(post, self._all_addressed(post))  # what the classifier sees is fixed now
        self.clock.at(0, self._classifier_attempt, request, 1)

    def _classifier_applies(self, post: Post) -> bool:
        if self.classifier is None or not self.params.classifier_enabled or post.id in self.decisions:
            return False
        if self.platform.is_dm or (post.controls and is_control_only(post.text)):
            return False
        bots = [b for b in self._bots_in_room() if b != post.author]
        return not all(self._not_offered(b, post.thread) for b in bots)

    def _not_offered(self, bot: str, thread: str) -> bool:
        """Muted and degraded bots are not offered to the classifier (5.2, 5.10). This is
        gateway work the bot cannot influence, so it is the same in both enforce modes."""
        degraded = bot in self.volatile and self.volatile[bot].degraded
        return self.is_muted(bot, thread) or degraded

    def _all_addressed(self, post: Post) -> tuple[str, ...]:
        return tuple(b for b in self._bots_in_room() if post.addresses(b) and b != post.author)

    def _request(self, post: Post, addressed: tuple[str, ...]) -> ClassifierRequest:
        history = self.platform.thread_posts(post.thread, newest_first=True)
        history = [p for p in history if p.position < post.position]  # on replay the room already holds newer posts
        shown = tuple(history[: self.params.classifier_posts or None])  # 0 disables the cap (3.1)
        members = self.platform.members()
        offered = [self._with_bio(members[b]) for b in self._bots_in_room() if not self._not_offered(b, post.thread)]
        return ClassifierRequest(post, addressed, shown, len(history) - len(shown), tuple(offered))

    def _with_bio(self, member):
        # A roster-only bot (hosted elsewhere, design §3.1) has no bio here, whatever the
        # gateway's dict holds under its name (a restored document, a push by that name).
        bio = self.bios.get(member.username) if member.username in self.hosted else None
        return replace(member, profile=bio) if bio is not None else member

    async def _classifier_attempt(self, request: ClassifierRequest, attempt: int) -> None:
        """One call, bounded by the timeout, outside the room lock; the result comes
        back under it after the latency the classifier reports (zero for a live one,
        whose wall-clock time has already passed; a scripted one states simulated
        seconds). An answer slower than the timeout is a timeout, whatever it said."""
        timeout = self.params.classifier_timeout_seconds
        try:
            result = await asyncio.wait_for(self.classifier.classify(request), timeout or None)
        except asyncio.TimeoutError:
            result = ClassifierResult(error=f"timeout after {timeout}s")  # the wait itself took the timeout
        except Exception as exc:  # the classifier is a foreign component: its failure is a fallback, not a crash
            result = ClassifierResult(error=f"{type(exc).__name__}: {exc}")
        if timeout and result.latency > timeout:
            result = ClassifierResult(error=f"timeout after {timeout}s", latency=timeout)
        self.clock.at(result.latency, self._classifier_arrived, request, attempt, result)

    async def _classifier_arrived(self, request: ClassifierRequest, attempt: int, result: ClassifierResult) -> None:
        async with self._lock:
            await self._classifier_result(request, attempt, result)

    async def _classifier_result(self, request: ClassifierRequest, attempt: int, result: ClassifierResult) -> None:
        post, addressed = request.post, request.addressed
        if result.failed:
            if attempt <= self.params.classifier_retries:
                self.trace.add(self.now(), "classifier_retry", gateway=self.name, post=post.id,
                               attempt=attempt, error=result.error)
                self.clock.at(0, self._classifier_attempt, request, attempt + 1)
                return
            decision = fallback_decision(self.params.classifier_fallback, post, addressed, self._bots_in_room())
        else:
            decision = result.decision
        await self._decided(post, addressed, decision)

    async def _decided(self, post: Post, addressed: tuple[str, ...], decision: Decision) -> None:
        self.decisions[post.id] = CachedDecision(decision, self.now(), post.position)
        self.trace.add(self.now(), "classifier_decision", gateway=self.name, post=post.id,
                       add=list(decision.add), remove=list(decision.remove), fallback=decision.fallback,
                       reasons=dict(decision.reasons))
        for bot in self.hosted:
            if bot in decision.add and bot not in addressed and bot != post.author \
                    and after(post.position, self._cursor(bot)):
                await self._try_start(bot, "picked", post.thread, trigger=post)

    def _decision_for(self, bot: str, post: Post) -> bool:
        """Whether a still-valid cached decision selects ``bot`` for ``post``."""
        cached = self.decisions.get(post.id)
        if cached is None or cached.decision is None or bot not in cached.decision.add:
            return False
        ttl = self.params.classifier_decision_ttl_seconds
        if ttl and self.now() - cached.time > ttl:
            if post.id not in self.volatile[bot].expired_decisions:
                self.volatile[bot].expired_decisions.add(post.id)
                self.trace.add(self.now(), "classifier_expired", gateway=self.name, post=post.id, bot=bot)
            return False
        return True

    # ── CATCH_UP: the sweeper ───────────────────────────────────────────────

    def _backlog(self, bot: str, through: Position | None = None) -> list[Post]:
        """The undelivered posts of others: every decision (trigger, candidate, due
        thread, hint) is made from these; the bot's own posts are read, never acted on."""
        return [p for p in self.platform.posts_after(self._cursor(bot), through) if p.author != bot]

    async def sweep(self, bot: str, reason: str) -> None:
        async with self._lock:
            await self._sweep(bot, reason)

    async def _sweep(self, bot: str, reason: str) -> None:
        if not self.online:
            return
        candidates = self._candidates(bot)
        self.trace.add(self.now(), "sweep", gateway=self.name, bot=bot, reason=reason,
                       candidates=[c[0] + ":" + str(c[1]) for c in candidates])
        hwm = self.platform.last_position()
        for kind, key in candidates:
            if await self._start_candidate(bot, kind, key, hwm):
                return

    def _candidates(self, bot: str) -> list[tuple[str, object]]:
        """Section 5.7's order: backlog posts that address or select the bot, oldest
        first (every one, so a held older post cannot mask a newer human mention);
        unfinished threads; threads due for catch-up. Each candidate is rechecked by
        ``_try_start``, which holds or starts it."""
        found: list[tuple[str, object]] = []
        for post in self._backlog(bot):
            if post.kind != "notice" and (self._addresses(post, bot) or self._decision_for(bot, post)):
                found.append(("post", post.id))
        # An unfinished thread is a candidate once it "has become open" (5.7), and a
        # thread is due for catch-up only when open and out of cooldown: a closed one
        # is not held, it is not a candidate, in either enforce mode.
        found += [("unfinished", t) for t in sorted(self.persisted[bot].unfinished) if not self._closed_by(bot, t)]
        found += [("catch-up", t) for t in self._due_threads(bot)]
        return found

    def _due_threads(self, bot: str) -> list[str]:
        """Periodic catch-up (5.7): once the bot has ended no turn for
        ``catch_up_idle_seconds`` (``0`` disables it), every thread holding undelivered
        posts is due, newest first; a closed or cooling thread is not. Nothing is due while
        any post behind the cursor is still being classified: the turn would deliver the
        whole backlog and swallow the pick. A failure notice alone makes nothing due; it
        rides along with the next turn."""
        idle = self.params.catch_up_idle_seconds
        if not idle or self.now() - self.volatile[bot].idle_since < idle:
            return []
        backlog = self._backlog(bot)
        if any(self._classifying(p) for p in backlog):
            return []
        by_thread: dict[str, list[Post]] = {}
        for post in backlog:
            if post.kind != "notice":
                by_thread.setdefault(post.thread, []).append(post)
        due = [(posts[-1].position, thread) for thread, posts in by_thread.items()
               if not self._closed_by(bot, thread) and not self._in_cooldown(bot, thread)]
        return [t for _, t in sorted(due, reverse=True)]

    def _classifying(self, post: Post) -> bool:
        cached = self.decisions.get(post.id)
        return cached is not None and cached.decision is None

    async def _start_candidate(self, bot: str, kind: str, key, hwm: Position | None) -> bool:
        if kind == "post":
            post = next((p for p in self._backlog(bot) if p.id == key), None)
            if post is None:
                return False
            source: TurnSource = "addressed" if self._addresses(post, bot) else "picked"
            if source == "picked" and not self._decision_for(bot, post):
                return False
            # the trigger decides eligibility; delivery runs through the sweep's mark (5.6)
            return await self._try_start(bot, source, post.thread, trigger=post, hwm=hwm, from_sweep=True)
        if kind == "unfinished":
            if key not in self.persisted[bot].unfinished:
                return False
            return await self._try_start(bot, "unfinished", key, hwm=hwm, from_sweep=True)
        return await self._try_start(bot, "catch-up", key, hwm=hwm, from_sweep=True)

    # ── step 7: HINT and the wake ───────────────────────────────────────────

    def _closed_by(self, bot: str, thread: str) -> tuple[str, ...]:
        causes = [MUTE] if self.is_muted(bot, thread) else []
        return tuple(causes + self._rate_causes(bot, thread))

    def _build_hint(self, bot: str, source: TurnSource, starting: str, delivered: list[Post],
                    held_by: tuple[str, ...]) -> Hint:
        state = self.volatile[bot]
        threads = {starting} | {p.thread for p in delivered} | set(self.persisted[bot].unfinished)
        groups = tuple(self._group(bot, t, starting, delivered, held_by) for t in sorted(threads))
        return Hint(
            source=source,
            starting_thread=starting,
            threads=groups,
            refused_last_turn=state.refused,
            previous_failure=state.previous_failure,
            backoff_until=state.retry_after if self.now() < state.retry_after else None,
            degraded=state.degraded,
        )

    def _group(self, bot: str, thread: str, starting: str, delivered: list[Post],
               held_by: tuple[str, ...]) -> ThreadGroup:
        closed_by = held_by if thread == starting else self._closed_by(bot, thread)
        opened_by_authority = any(
            p.thread == thread and self._addresses(p, bot) and self._exempt("addressed", thread, p)
            for p in delivered
        )
        if closed_by and not opened_by_authority:
            return ThreadGroup(thread, "closed", closed_by)
        addressed_here = any(p.thread == thread and self._addresses(p, bot) for p in delivered)
        unfinished = thread in self.persisted[bot].unfinished
        group = "must-reply" if (thread == starting or addressed_here or unfinished) else "optional"
        posts_left, words_left = self._allowance(bot, thread)
        return ThreadGroup(thread, group, (), posts_left, words_left)

    def _allowance(self, bot: str, thread: str) -> tuple[int | None, int | None]:
        if self.is_orchestrator(bot, thread):
            return None, None
        posts, words = self._limits(thread)
        recent = self._recent(thread)
        return (max(posts - len(recent), 0) if posts else None,
                max(words - sum(r[2] for r in recent), 0) if words else None)

    def _warn_if_late(self, post: Post, copies: list[str]) -> None:
        """A post that arrives below a bot's cursor, or below the mark of a wake already
        issued, is never in a read: the cursor passes it. The paths that produce one (a
        resync interleaving with live frames, the bot's own post admitted ahead of a
        human's in flight) are rare and unbounded in the code, so they are made visible
        here and fixed in the core when the rate says so."""
        for bot in copies:
            run = self.volatile[bot].turn
            if not after(post.position, self._cursor(bot)):
                logger.warning("%s: post %s arrived below %s's cursor; it will not be in a read", self.room, post.id, bot)
            elif run is not None and run.hwm is not None and not after(post.position, run.hwm):
                logger.warning("%s: post %s arrived below the mark of %s's wake %s; it will not be in that read",
                               self.room, post.id, bot, run.wake.wake_id)

    def _trigger(self, bot: str, post: Post | None) -> Trigger | None:
        if post is None:
            return None
        return Trigger(post_id=post.id, author=post.author, author_id=post.author_id,
                       author_role=self.role_label(bot, post.author), thread_root=post.thread)

    def role_label(self, bot: str, author: str) -> str:
        """The three labels of design §2, on a wake's trigger and on every post ``read``
        delivers to ``bot``: ``agent`` is a bot in the room's roster, ``owner`` is a
        person the configuration lists as this bot's owner, and any other author is a
        ``guest``, whatever the platform calls them."""
        return self.roster.label(bot, author)

    async def _start_turn(self, bot: str, source: TurnSource, thread: str, trigger: Post | None,
                          hwm: Position | None, held_by: tuple[str, ...]) -> None:
        """Issue the wake: the high-water mark and the hint are fixed now; the lease
        starts when the agent's poll picks the wake up (design §3.2)."""
        through = hwm if hwm is not None else (trigger.position if trigger is not None else None)
        delivered = self._backlog(bot, through)
        hint = self._build_hint(bot, source, thread, delivered, held_by)
        self._wake_seq += 1
        wake = Wake(f"w-{self.room}-{self._wake_seq}", self.room, bot, source, thread, through, hint,
                    trigger=self._trigger(bot, trigger), rules_version=self.rules_version,
                    reset=self.persisted[bot].reset_pending)
        payload = tuple(self.platform.posts_after(self._cursor(bot), through))
        if wake.reset:
            # a fresh session holds none of its unfinished threads: their posts come again (design §3.4)
            again = {p.id: p for t in self.persisted[bot].unfinished for p in self.platform.thread_posts(t)}
            again.update({p.id: p for p in payload})
            payload = tuple(sorted(again.values(), key=lambda p: p.position))
        run = TurnRun(wake, through, tuple(delivered), payload, read_complete=not payload)
        self.volatile[bot].turn = run
        if bot not in self.platform.members():  # configured here, not in the room: loud, until the roster owns it
            logger.warning("%s: %s is hosted but not a member of the room; wake %s issued", self.room, bot, wake.wake_id)
        self.trace.add(self.now(), "turn_start", gateway=self.name, bot=bot, source=source,
                       thread=thread, exempt=self._exempt(source, thread, trigger),
                       delivered=[p.id for p in delivered], wake=wake.wake_id)
        if self.on_wake is not None:
            await self.on_wake(wake)

    # ── the agent's side of a turn: poll, read, send, done ──────────────────

    def minted(self, wake_id: str) -> bool:
        """Whether this room issued ``wake_id`` (``w-<room>-<n>``), live or not: a late send
        on an expired wake still names its room (design §3.4)."""
        prefix = f"w-{self.room}-"
        return wake_id.startswith(prefix) and wake_id[len(prefix):].isdigit()

    def poll(self, bot: str) -> Wake | None:
        """Hand the bot its pending wake, if any, and start its lease. One outstanding
        wake per bot per room: a leased wake is handed out once."""
        state = self.volatile.get(bot)
        run = state.turn if state is not None else None
        if run is None or run.leased or self.persisted[bot].paused:
            return None
        run.leased = True
        if run.wake.reset:
            # the agent has the flag now; a wake lost to a restart before this carried it again
            self.persisted[bot].reset_pending = False
            self._changed()
        if self.params.lease_seconds:  # 0: a lease never expires (3.1: zero disables a limit)
            run.expires_at = self.now() + self.params.lease_seconds
            run.wake = replace(run.wake, lease_expires_at=run.expires_at)
            run.expiry = self.clock.at(self.params.lease_seconds, self._lease_expired, bot, run)
        self.trace.add(self.now(), "lease_started", gateway=self.name, bot=bot, wake=run.wake.wake_id,
                       expires_at=run.expires_at)
        return run.wake

    def _live(self, bot: str, wake_id: str) -> TurnRun:
        state = self.volatile.get(bot)
        run = state.turn if state is not None else None
        if run is None or not run.leased or run.wake.wake_id != wake_id:
            raise NoLiveLease(f"{bot} holds no live lease on wake {wake_id!r} in {self.room}")
        return run

    def read(self, bot: str, wake_id: str) -> tuple[Wake, list[Post]]:
        """The wake's hint and the posts after the cursor through its mark, the bot's
        own included; records on the lease that the agent fetched them (design §3.3)."""
        run = self._live(bot, wake_id)
        run.read_complete = True
        self.trace.add(self.now(), "read", gateway=self.name, bot=bot, wake=wake_id,
                       delivered=[p.id for p in run.delivered],
                       own=[p.id for p in run.payload if p.author == bot])
        return run.wake, list(run.payload)

    def read_room(self, bot: str, after: str | None = None) -> list[Post]:
        """A read outside a wake: the posts after the cursor, or after the post ``after``
        names, through the latest; how an agent checks whether its own reply landed, and
        what an agent's own heartbeat reads. Records nothing. ``KeyError`` for a post id
        the room does not hold."""
        if after is None:
            start = self._cursor(bot)
        else:
            start = self.platform.position_of(after)
            if start is None:
                raise KeyError(after)
        return list(self.platform.posts_after(start))

    async def send(self, bot: str, reply: Reply) -> Post | None:
        """One reply posted on the bot's behalf (design §3.5). A wake's send is checked
        against that wake's hint; an unsolicited send against the thread's current
        mute (rate and cooldown exempt). Enforce mode refuses with :class:`RefusedSend`;
        measure mode posts and counts. Returns ``None`` when a control resolved to
        nothing and there was no text."""
        async with self._lock:
            return await self._send(bot, reply)

    async def _send(self, bot: str, reply: Reply) -> Post | None:
        run = self._attributed(bot, reply)
        ctx = self._validation_context(bot, reply.thread, run)
        structural, rules = check_reply(reply, ctx)
        if structural:
            raise InvalidReply("; ".join(f"{p.thread}: {p.reason}" for p in structural))
        if run is None and self.persisted[bot].paused:
            # pause is the operator's hold on the bot, not a room rule an agent could obey:
            # it refuses in both modes (design §3.1, the 5.5 row)
            self.trace.add(self.now(), "refused", gateway=self.name, bot=bot, wake=None,
                           thread=reply.thread, reason="paused")
            raise RefusedSend("paused: the bot is paused in this room")
        self._note_hinted(bot, reply, run)
        if run is not None:
            run.problems.update((p.mechanism, p.thread) for p in rules)
            run.items_seen.update(self._items(reply))
        if rules and self.params.enforce:
            reasons = "; ".join(f"{p.mechanism}@{p.thread}: {p.reason}" for p in rules)
            self.trace.add(self.now(), "refused", gateway=self.name, bot=bot, wake=reply.wake_id,
                           thread=reply.thread, reason=reasons)
            if run is None:
                self._count_reply_rules(bot, reply, rules)
            else:
                run.rejected.append(reasons)
            raise RefusedSend(reasons)
        if run is None:
            self._count_reply_rules(bot, reply, rules)
            self.volatile[bot].unsolicited_sends += 1
        try:
            post = await self._post_reply(bot, reply, run)
        finally:
            if run is not None and reply.thread is not None:
                run.ledger.add(reply.thread)  # a reply the platform refused was still given (5.6, 5.9)
        self.trace.add(self.now(), "send", gateway=self.name, bot=bot, wake=reply.wake_id,
                       thread=reply.thread, post=post.id if post else None)
        return post

    def _attributed(self, bot: str, reply: Reply) -> TurnRun | None:
        """The live turn a send belongs to. A wake id with no live lease (a late send
        after expiry) makes the send unsolicited: attribution is lost, nothing is
        refused for it (design §3.4, §3.5)."""
        if reply.wake_id is None:
            return None
        try:
            return self._live(bot, reply.wake_id)
        except NoLiveLease:
            logger.info("%s: send names wake %s with no live lease; treated as unsolicited", bot, reply.wake_id)
            reply.wake_id = None
            return None

    def _validation_context(self, bot: str, thread: str | None, run: TurnRun | None) -> ValidationContext:
        if thread is None:
            closed_by = ()  # a new top-level post opens a thread nobody has closed yet
        elif run is not None and (group := run.wake.hint.group_of(thread)) is not None:
            closed_by = group.closed_by  # fixed at turn start (5.3, 5.9)
        else:
            # unsolicited, or a wake's reply outside its hint: the thread's mute now; rate and
            # cooldown never refuse a reply (design §3.5)
            closed_by = (MUTE,) if self.is_muted(bot, thread) else ()
        return ValidationContext(
            bot=bot,
            members=frozenset(self.platform.members()),
            agents=frozenset(self.agents),
            orchestrator_in=frozenset(t for t, s in self.settings.items() if s.orchestrator == bot),
            closed_by=closed_by,
        )

    @staticmethod
    def _items(reply: Reply) -> set[tuple[str, str]]:
        found = set()
        if reply.control is not None:
            found.add((reply.thread, "control"))
        if reply.text is not None:
            found.add((reply.thread, "text"))
        return found

    def _note_hinted(self, bot: str, reply: Reply, run: TurnRun | None) -> None:
        """Spec 5.9's "reply only in a delivered or hinted thread" is an instruction and a
        counter here, never a refusal (design §3.1, 5.9 row)."""
        if run is None or reply.thread is None:  # a new thread is neither delivered nor hinted
            return
        inside = run.wake.hint.group_of(reply.thread) is not None
        self._count_rule(bot, reply.thread, HINTED_THREAD, "complied" if inside else "violated")

    def _count_reply_rules(self, bot: str, reply: Reply, rules: list[Problem]) -> None:
        """An unsolicited send is counted on its own, as it is posted (or refused)."""
        broke = {(p.mechanism, p.thread) for p in rules}
        outcome = "rejected" if self.params.enforce else "violated"
        for thread, kind in sorted(self._items(reply)):
            mechanism = "authority" if kind == "control" else "recipients"
            self._count_rule(bot, thread, mechanism, outcome if (mechanism, thread) in broke else "complied")
        for cause in ((MUTE,) if self.is_muted(bot, reply.thread) else ()):
            self._count_rule(bot, reply.thread, cause, outcome if (cause, reply.thread) in broke else "complied")

    async def _post_reply(self, bot: str, reply: Reply, run: TurnRun | None) -> Post | None:
        if reply.control:
            settings = self._thread(reply.thread)
            reply.control = resolve_control(reply.control, self._bots_in_room(), set(settings.muted), settings.orchestrator)
            if not reply.control and reply.text is None:
                return None  # ``all`` resolved to nobody and nothing else remains: post nothing (8.3)
        text = canonical_text(reply)
        words = count_words(text)
        # a new top-level post has no thread to count in until the platform names it;
        # the count after is still ahead of the echo, which never counts a hosted bot
        new_thread = reply.thread is None
        previous_last = None if new_thread else self.volatile[bot].last_post.get(reply.thread)
        entry = None if new_thread else self._count_own_post(bot, reply.thread, self.now(), words)
        try:
            post = await self.platform.post(bot, text, reply.thread, kind="bot")
        except RefusedPost:
            if not new_thread:
                self._uncount_own_post(bot, reply.thread, entry, previous_last)
            self.trace.add(self.now(), "platform_refused", gateway=self.name, bot=bot, thread=reply.thread)
            if run is not None and reply.thread is not None:
                run.refused.append(reply.thread)
            raise
        except PlatformError:
            # whether it landed is unknown; counted as not, the agent reads the room (design §3.3)
            if not new_thread:
                self._uncount_own_post(bot, reply.thread, entry, previous_last)
            self.trace.add(self.now(), "platform_error", gateway=self.name, bot=bot, thread=reply.thread)
            raise
        if new_thread:
            self._count_own_post(bot, post.thread, self.now(), words)
        if run is not None:
            run.posts += 1
        return post

    async def done(self, bot: str, wake_id: str, answered: tuple[str, ...] = (), failed: str | None = None) -> bool:
        """Close the turn. Returns ``False`` for a wake with no live lease: logged, and
        nothing moves (the gateway has already applied the failure backoff)."""
        async with self._lock:
            return await self._done(bot, wake_id, answered, failed)

    async def _done(self, bot: str, wake_id: str, answered: tuple[str, ...], failed: str | None) -> bool:
        state = self.volatile.get(bot)
        run = state.turn if state is not None else None
        if run is None or not run.leased or run.wake.wake_id != wake_id:
            self.trace.add(self.now(), "done_stale", gateway=self.name, bot=bot, wake=wake_id)
            logger.info("%s: done for wake %s with no live lease in %s", bot, wake_id, self.room)
            return False
        if run.expiry is not None:
            run.expiry.cancel()
        if failed is not None:
            await self._backend_failure(run, failed)
            return True
        if not run.read_complete:
            await self._backend_failure(run, "no read")
            return True
        covered = run.ledger | set(answered)
        missing = [t for t in run.wake.hint.must_reply
                   if t not in covered and not self.platform.is_archived(t)]
        if missing:
            run.problems.update((MUST_REPLY, t) for t in missing)
        reasons = run.rejected + [f"must_reply@{t}: no send or --answered" for t in missing]
        if reasons and self.params.enforce:
            await self._invalid_failure(run, "; ".join(reasons))
            return True
        self._commit(run, covered)
        self._count_turn_rules(run)
        self._finish(run, "normal", answered=sorted(covered))
        return True

    async def _lease_expired(self, bot: str, run: TurnRun) -> None:
        async with self._lock:
            if self.volatile.get(bot) is not None and self.volatile[bot].turn is run and run.leased:
                await self._backend_failure(run, "lease expired")

    def prune_threads(self) -> None:
        """Drop the per-thread entries that hold nothing: on a platform every top-level
        post is a thread, and a lookup creates an entry, so the two dicts would otherwise
        grow with the room. A dropped entry comes back on demand, the same."""
        for thread in [t for t, s in self.settings.items() if s.is_default]:
            del self.settings[thread]
        for thread in [t for t, s in self.thread_state.items() if s.is_empty]:
            del self.thread_state[thread]

    def _commit(self, run: TurnRun, covered: set[str]) -> None:
        self.prune_threads()
        bot = run.wake.bot
        state = self.persisted[bot]
        if run.hwm is not None and after(run.hwm, state.cursor):
            state.cursor = run.hwm
            self.trace.add(self.now(), "cursor_saved", gateway=self.name, bot=bot, position=run.hwm)
        for group in run.wake.hint.threads:
            if self.platform.is_archived(group.thread):
                continue  # removed while the turn ran: it can no longer be answered (5.4)
            if group.group == "closed" and group.thread not in covered and group.thread not in state.unfinished:
                state.unfinished.add(group.thread)
                self.trace.add(self.now(), "unfinished_added", gateway=self.name, bot=bot, thread=group.thread)
        for thread in covered & state.unfinished:
            state.unfinished.discard(thread)
            self.trace.add(self.now(), "unfinished_removed", gateway=self.name, bot=bot, thread=thread)
        self._evict_decisions()
        self._changed()

    def _evict_decisions(self) -> None:
        """Decided entries every hosted cursor has passed: no backlog holds their post, so
        no sweep or start reads them again (a restart drops the whole cache). A bot whose
        cursor is null, paused or away keeps them, as it keeps the backlog itself."""
        cursors = [self.persisted[b].cursor for b in self.hosted]
        if not cursors or any(c is None for c in cursors):
            return
        floor = min(cursors)
        gone = [pid for pid, c in self.decisions.items()
                if c.decision is not None and c.position is not None and not after(c.position, floor)]
        for pid in gone:
            del self.decisions[pid]
            for state in self.volatile.values():
                state.expired_decisions.discard(pid)

    def _finish(self, run: TurnRun, outcome: str, **more) -> None:
        bot = run.wake.bot
        state = self.volatile[bot]
        state.turn = None
        state.refused = tuple(run.refused)
        if outcome == "normal":
            state.retry_after, state.backoff_level, state.backoff_notice_sent = 0.0, 0, False
            state.invalid_streak, state.degraded, state.invalid_notice_sent = 0, False, False
            state.previous_failure = None
            state.idle_since = self.now()
        self.trace.add(self.now(), "turn_end", gateway=self.name, bot=bot, wake=run.wake.wake_id,
                       outcome=outcome, posts=run.posts, **more)
        self._request_sweep(bot, "turn end")

    # ── FAILURE_NOTICE ──────────────────────────────────────────────────────

    async def _backend_failure(self, run: TurnRun, error: str) -> None:
        bot = run.wake.bot
        state = self.volatile[bot]
        state.previous_failure = f"backend failure ({error})"
        if self.params.failure_backoff_seconds:
            wait = min(self.params.failure_backoff_seconds * 2 ** state.backoff_level,
                       self.params.failure_backoff_max_seconds or float("inf"))
            state.retry_after = self.now() + wait
            state.backoff_level += 1
            self.trace.add(self.now(), "backoff", gateway=self.name, bot=bot, until=state.retry_after)
            self.clock.at(wait, self._requested_sweep, bot, "backoff ended")
        if not state.backoff_notice_sent:
            state.backoff_notice_sent = True
            await self._notice(bot, run.wake.starting_thread, "backend failure")
        if run.items_seen:
            self._count_turn_rules(run)  # the sends before the failure happened (5.10, design §3.4)
        self._finish(run, "backend_failure", error=error)

    async def _invalid_failure(self, run: TurnRun, reasons: str) -> None:
        bot = run.wake.bot
        state = self.volatile[bot]
        state.invalid_streak += 1
        state.previous_failure = f"invalid turn ({reasons})"
        self.trace.add(self.now(), "invalid", gateway=self.name, bot=bot, wake=run.wake.wake_id, reason=reasons)
        limit = self.params.consecutive_invalid_limit
        if not state.invalid_notice_sent:
            state.invalid_notice_sent = True
            await self._notice(bot, run.wake.starting_thread, "invalid reply")
        if limit and state.invalid_streak >= limit and not state.degraded:
            state.degraded = True
            self.trace.add(self.now(), "degraded", gateway=self.name, bot=bot, streak=state.invalid_streak)
            await self._notice(bot, run.wake.starting_thread, "degraded")
        self._count_turn_rules(run)  # a turn that failed on a rule is the worst offender
        self._finish(run, "invalid")

    async def _notice(self, bot: str, thread: str, cause: str) -> None:
        try:
            post = await self.platform.post(bot, f"{NOTICE_PREFIX} {cause}", thread, kind="notice")
        except RefusedPost:
            return
        except PlatformError as exc:  # the notice is best effort; the turn's bookkeeping goes on
            logger.warning("%s: notice not posted: %s", self.room, exc)
            return
        self.trace.add(self.now(), "notice", gateway=self.name, bot=bot, post=post.id, cause=cause)

    # ── rule counting (the enforce toggle's output) ─────────────────────────

    def _count_rule(self, bot: str, thread: str | None, mechanism: str, outcome: str) -> None:
        count = self.rules.setdefault(mechanism, RuleCount())
        count.opportunities += 1
        setattr(count, outcome, getattr(count, outcome) + 1)
        self.trace.add(self.now(), "rule", gateway=self.name, bot=bot, thread=thread,
                       mechanism=mechanism, outcome=outcome)

    def _count_turn_rules(self, run: TurnRun) -> None:
        """One count per opportunity the turn had, from the problems and items seen across
        its sends: ``rejected`` when enforced, ``violated`` when not, else ``complied``.
        The starting thread closed by a rule exists only with enforcement off (the turn
        ran where the rule would have held it) and is counted apart, as ``held_*``."""
        bot = run.wake.bot
        starting = run.wake.starting_thread
        broke = "rejected" if self.params.enforce else "violated"
        for group in run.wake.hint.threads:
            if group.group == "closed":
                for cause in group.closed_by:
                    problem = (cause, group.thread) in run.problems
                    if group.thread == starting:
                        outcome = "held_violated" if problem else "held_complied"
                    else:
                        outcome = broke if problem else "complied"
                    self._count_rule(bot, group.thread, cause, outcome)
            elif group.group == "must-reply":
                self._count_rule(bot, group.thread, "must_reply",
                                 broke if ("must_reply", group.thread) in run.problems else "complied")
        for thread, kind in sorted(run.items_seen):
            mechanism = "authority" if kind == "control" else "recipients"
            self._count_rule(bot, thread, mechanism, broke if (mechanism, thread) in run.problems else "complied")
        hinted = {g.thread for g in run.wake.hint.threads}
        for mechanism, thread in sorted(p for p in run.problems if p[1] not in hinted and p[0] == MUTE):
            self._count_rule(bot, thread, mechanism, broke)  # a reply in a muted thread outside the hint

    # ── STATUS ──────────────────────────────────────────────────────────────

    def _thread_status(self, thread: str) -> dict:
        s = self._thread(thread)
        recent = self._recent(thread)
        return {"orchestrator": s.orchestrator, "muted": sorted(s.muted), "limits": self._limits(thread),
                "recent_posts": len(recent), "recent_words": sum(r[2] for r in recent),
                "unknown_mutes": list(s.unknown_mutes)}

    def _bot_status(self, bot: str) -> dict:
        v, p = self.volatile[bot], self.persisted[bot]
        run = v.turn
        return {"cursor": p.cursor, "unfinished": sorted(p.unfinished), "paused": p.paused,
                "backlog": len(self._backlog(bot)),
                "wake": None if run is None else {
                    "id": run.wake.wake_id, "source": run.wake.source, "leased": run.leased,
                    "expires_at": run.expires_at, "read": run.read_complete},
                "retry_after": v.retry_after, "degraded": v.degraded, "invalid_streak": v.invalid_streak,
                "expired_decisions": len(v.expired_decisions), "unsolicited_sends": v.unsolicited_sends}

    def status(self) -> dict:
        members = self.platform.members()
        return {
            "gateway": self.name,
            "room": self.room,
            "threads": {t: self._thread_status(t) for t in list(dict.fromkeys([*self.settings, *self.thread_state]))},
            "bots": {b: self._bot_status(b) for b in self.hosted},
            "agents": list(self.agents),
            "bot_like_members_missing": [m.username for m in members.values() if m.is_bot and m.username not in self.agents],
            "hosted_not_members": [b for b in self.hosted if b not in members],
            "rules": {m: vars(c) for m, c in self.rules.items()},
            "decisions": len(self.decisions),
        }
