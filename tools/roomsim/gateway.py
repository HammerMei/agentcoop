"""One simulated gateway: the mechanisms of section 5 applied to the bots it hosts.

The end-to-end flow of section 4 is one method per step, named after the
specification's mechanism names, so a rule can be found from the text and back.
State is split into what the real gateway persists (cursor, unfinished threads,
thread settings) and what it keeps in memory (rate window, cooldown, backoff,
invalid streaks, decision cache), so ``restart`` can reset exactly the second kind.

The ``enforce`` switch of :class:`~tools.roomsim.model.Params`: when off, a hold by
mute, rate or cooldown does not stop a turn and a rule problem in an envelope does
not reject it; the HINT still says what the rule is, and every such opportunity is
counted as ``complied`` or ``violated`` (``blocked`` when enforced). Backoff,
degraded state, one turn per bot, the cursor and the failure notice are not
switched: a bot cannot obey those by itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .classifier import Classifier, ClassifierError, ClassifierRequest, fallback_decision
from .controls import is_control_only
from .envelope import (
    ParseError,
    Problem,
    ValidationContext,
    canonical_text,
    parse_envelope,
    resolve_control,
    validate,
)
from .model import (
    ALL,
    COOLDOWN,
    MUTE,
    RATE_POSTS,
    RATE_WORDS,
    BotOutput,
    Decision,
    Envelope,
    Hint,
    Job,
    Params,
    Post,
    ThreadGroup,
    TurnInput,
    TurnSource,
)
from .room import RefusedPost
from .sim import Simulation

NOTICE_PREFIX = "[gateway] turn failed:"
RULE_GATES = (MUTE, RATE_POSTS, RATE_WORDS, COOLDOWN)


# ── state ───────────────────────────────────────────────────────────────────


@dataclass
class ThreadSettings:
    """Persisted per thread (section 5.4): the latest value of each control kind with
    the seq of its source post, and the newest post time observed."""

    orchestrator: str | None = None
    orchestrator_seq: int = 0
    muted: dict[str, int] = field(default_factory=dict)  # username -> source seq
    unmuted_seq: dict[str, int] = field(default_factory=dict)
    limit_posts: int | None = None
    limit_words: int | None = None
    limit_seq: int = 0
    last_activity: float = 0.0
    unknown_mutes: list[str] = field(default_factory=list)


@dataclass
class ThreadVolatile:
    recent: list[tuple[float, str, int]] = field(default_factory=list)  # (time, author, words)
    held_by_rate: set[str] = field(default_factory=set)


@dataclass
class BotPersisted:
    cursor: int = 0
    unfinished: set[str] = field(default_factory=set)
    jobs: dict[int, Job] = field(default_factory=dict)


@dataclass
class BotVolatile:
    running: bool = False
    sweep_requested: bool = False
    last_post: dict[str, float] = field(default_factory=dict)
    retry_after: float = 0.0
    backoff_level: int = 0
    backoff_notice_sent: bool = False
    invalid_streak: int = 0
    degraded: bool = False
    invalid_notice_sent: bool = False
    previous_failure: str | None = None
    refused: tuple[str, ...] = ()
    expired_decisions: set[int] = field(default_factory=set)
    # Bumped by restart; a turn that ends under an older generation belonged to the
    # process that died, and its output is dropped (the replayed turn answers instead).
    generation: int = 0
    # (trigger seq or thread, cause) holds already counted as blocked, so a hold the
    # periodic sweep finds again is one opportunity, not one per sweep.
    counted_holds: set[tuple[object, str]] = field(default_factory=set)


@dataclass
class CachedDecision:
    decision: Decision | None = None  # None while in flight
    time: float = 0.0


@dataclass
class RuleCount:
    """Per mechanism. Two pairs compare across modes:

    - ``rejected`` (enforced: the bot wrote a reply the gateway refused) against
      ``violated`` (not enforced: the gateway let it through); ``complied`` is the bot
      obeying on its own in a thread it was not started for;
    - ``held`` (enforced: the gateway never started the turn) against
      ``held_complied`` / ``held_violated`` (not enforced: the turn ran for a thread the
      rule would have held, and the bot stayed silent there or did not)."""

    opportunities: int = 0
    complied: int = 0
    violated: int = 0
    rejected: int = 0
    held: int = 0
    held_complied: int = 0
    held_violated: int = 0


@dataclass
class TurnRun:
    turn: TurnInput
    job: Job | None
    hwm: int
    attempt: int = 1
    problems: set[tuple[str, str | None]] = field(default_factory=set)
    posts: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost_usd: float = 0.0
    generation: int = 0
    # (thread, "control" | "text") items the bot wrote on any attempt: what it tried.
    items_seen: set[tuple[str, str]] = field(default_factory=set)


# ── the gateway ─────────────────────────────────────────────────────────────


class SimGateway:
    def __init__(
        self,
        sim: Simulation,
        name: str,
        hosted: list[str],
        agents: list[str],
        params: Params,
        runner,
        classifier: Classifier | None = None,
    ) -> None:
        self.sim = sim
        self.name = name
        self.hosted = list(hosted)
        self.agents = list(agents)
        self.params = params
        self.runner = runner
        self.classifier = classifier
        self.online = True
        self.settings: dict[str, ThreadSettings] = {}
        self.thread_state: dict[str, ThreadVolatile] = {}
        self.persisted: dict[str, BotPersisted] = {b: BotPersisted() for b in hosted}
        self.volatile: dict[str, BotVolatile] = {b: BotVolatile() for b in hosted}
        self.decisions: dict[int, CachedDecision] = {}
        self.replaying = False
        self.rules: dict[str, RuleCount] = {}
        self.totals: dict[str, dict[str, int]] = {b: self._zero_totals() for b in hosted}
        self._job_seq = 0

    @staticmethod
    def _zero_totals() -> dict[str, int]:
        return {"turns": 0, "posts": 0, "words": 0, "tokens_in": 0, "tokens_out": 0, "cache_read": 0,
                "cache_write": 0, "cost_usd": 0.0, "sweeps": 0, "failures": 0, "invalid": 0}

    # ── lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> None:
        for bot in self.hosted:
            self._schedule_periodic_sweep(bot)

    def _schedule_periodic_sweep(self, bot: str) -> None:
        if self.params.sweep_interval_seconds > 0:
            self.sim.at(self.params.sweep_interval_seconds, self._periodic_sweep, bot)

    async def _periodic_sweep(self, bot: str) -> None:
        await self.sweep(bot, "periodic")
        self._schedule_periodic_sweep(bot)

    def set_offline(self) -> None:
        self.online = False

    async def set_online(self, replay_after: int) -> None:
        """Reconnect: posts after ``replay_after`` are replayed as new triggers; earlier
        ones were missed (section 5.6)."""
        self.online = True
        await self._replay(replay_after)

    async def _replay(self, replay_after: int) -> None:
        """Replayed posts are new triggers for what is still undelivered and seed the rate
        window, own posts included, since none of them was counted at post time (5.3)."""
        self.replaying = True
        try:
            for post in self.sim.room.after(replay_after):
                await self.on_post(post)
        finally:
            self.replaying = False

    async def restart(self, replay_after: int) -> None:
        """Keep persisted state, reset in-memory state, replay recent posts to seed the
        rate window (section 5.3) and as new triggers (section 5.6)."""
        self.thread_state = {}
        self.volatile = {b: BotVolatile(generation=v.generation + 1) for b, v in self.volatile.items()}
        self.decisions = {}
        self.online = True
        await self._replay(replay_after)

    # ── helpers over state ──────────────────────────────────────────────────

    def now(self) -> float:
        return self.sim.now()

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
        return [a for a in self.agents if self.sim.room.is_member(a)]

    def is_muted(self, bot: str, thread: str) -> bool:
        return bot in self._thread(thread).muted

    def is_orchestrator(self, bot: str, thread: str) -> bool:
        return self._thread(thread).orchestrator == bot

    def _request_sweep(self, bot: str, reason: str) -> None:
        if bot in self.volatile and not self.volatile[bot].sweep_requested:
            self.volatile[bot].sweep_requested = True
            self.sim.at(0, self._requested_sweep, bot, reason)

    async def _requested_sweep(self, bot: str, reason: str) -> None:
        self.volatile[bot].sweep_requested = False
        await self.sweep(bot, reason)

    def on_thread_removed(self, thread: str) -> None:
        """Archive or deletion (section 5.4): settings and unfinished state go."""
        self.settings.pop(thread, None)
        for state in self.persisted.values():
            state.unfinished.discard(thread)

    # ── section 4: a post arrives ───────────────────────────────────────────

    async def on_post(self, post: Post) -> None:
        if not self.online:
            return
        copies = [b for b in self.hosted if b != post.author]
        self._thread(post.thread).last_activity = max(self._thread(post.thread).last_activity, post.time)
        if post.kind == "notice":
            return  # recognised before eligibility: no turn, no classifier, not in the window
        self._count_for_rate(post)  # a human post clears the window even when control-only (5.3)
        if not copies:
            return  # the gateway's own post: counted (on replay) but no trigger
        if self._apply_controls(post):
            return
        dm = self.sim.room.is_dm  # a 1:1 DM addresses its bot by definition (section 3)
        addressed = tuple(b for b in copies if (dm or post.addresses(b)) and post.seq > self.persisted[b].cursor)
        for bot in addressed:
            await self._try_start(bot, "addressed", post.thread, trigger=post)
        if any(post.seq > self.persisted[b].cursor for b in copies):
            await self._classify(post, addressed)

    # step 1 — AUTHORITY / SETTINGS
    def _apply_controls(self, post: Post) -> bool:
        controls = post.controls
        if controls is None:
            return False
        settings = self._thread(post.thread)
        role = self.sim.room.role_of(post.author)
        if role != "owner" and post.author != settings.orchestrator:
            self.sim.trace.add(self.now(), "control_ignored", gateway=self.name, thread=post.thread,
                               by=post.author, reason="not authorised")
            return False
        if controls.orchestrator is not None and role == "owner" and post.seq > settings.orchestrator_seq:
            settings.orchestrator = None if controls.orchestrator == "none" else controls.orchestrator
            settings.orchestrator_seq = post.seq
            self._trace_control(post, "orchestrator", settings.orchestrator)
        if controls.mute is not None:
            self._apply_mute(post, settings, controls.mute)
        if controls.unmute is not None:
            self._apply_unmute(post, settings, controls.unmute)
        if controls.limit is not None and role == "owner" and post.seq > settings.limit_seq:
            settings.limit_posts = controls.limit.get("posts", settings.limit_posts)
            settings.limit_words = controls.limit.get("words", settings.limit_words)
            settings.limit_seq = post.seq
            self._trace_control(post, "limit", dict(controls.limit))
        return is_control_only(post.text)

    def _trace_control(self, post: Post, control: str, value) -> None:
        self.sim.trace.add(self.now(), "control_applied", gateway=self.name, thread=post.thread,
                           by=post.author, control=control, value=value, seq=post.seq,
                           age=self.now() - post.time)  # > control TTL only in a replay (5.4)

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
            if post.seq > settings.unmuted_seq.get(bot, 0) and post.seq > settings.muted.get(bot, 0):
                settings.muted[bot] = post.seq
                applied.append(bot)
        if applied:
            self._trace_control(post, "mute", applied)

    def _apply_unmute(self, post: Post, settings: ThreadSettings, names) -> None:
        targets = self._resolve_names(names, settings, muted_only=True)
        applied = []
        for bot in targets:
            if post.seq > settings.muted.get(bot, 0):
                settings.muted.pop(bot, None)
                settings.unmuted_seq[bot] = post.seq
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

    def _count_own_post(self, bot: str, post: Post) -> None:
        if not self.is_orchestrator(bot, post.thread):
            self._tstate(post.thread).recent.append((post.time, bot, post.words))
        self.volatile[bot].last_post[post.thread] = post.time

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
        if source == "job":
            return True
        if source != "addressed" or trigger is None:
            return False
        return trigger.kind == "human" or trigger.author == self._thread(thread).orchestrator

    def _hard_hold(self, bot: str, source: TurnSource, thread: str, exempt: bool,
                   from_sweep: bool) -> str | None:
        """Holds that are not switched off by ``enforce``: the muted column for a
        degraded bot, backoff, and one turn per bot. Only a newly arriving human or
        orchestrator mention bypasses backoff and the degraded state; the sweeper's
        retry of a failed turn waits like any other sweep-started turn."""
        state = self.volatile[bot]
        if state.running:
            return "one_turn"
        waits = from_sweep or not exempt or source == "job"
        if state.degraded and waits:
            return "degraded"
        if waits and self.now() < state.retry_after:
            return "backoff"
        return None

    def _rule_holds(self, bot: str, source: TurnSource, thread: str, exempt: bool) -> list[str]:
        causes = [MUTE] if self.is_muted(bot, thread) else []
        if source == "job":
            return causes  # exempt from rate and cooldown; a mute still holds it (4.1)
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
        job: Job | None = None,
        hwm: int | None = None,
        from_sweep: bool = False,
    ) -> bool:
        exempt = self._exempt(source, thread, trigger)
        hard = self._hard_hold(bot, source, thread, exempt, from_sweep)
        if hard:
            self._held(bot, source, thread, hard, job)
            return False
        holds = self._rule_holds(bot, source, thread, exempt)
        if holds and self.params.enforce:
            key = trigger.seq if trigger is not None else thread
            for cause in holds:
                if (key, cause) not in self.volatile[bot].counted_holds:
                    self.volatile[bot].counted_holds.add((key, cause))
                    self._count_rule(bot, thread, cause, "held")
            self._held(bot, source, thread, holds[0], job)
            return False
        key = trigger.seq if trigger is not None else thread
        self.volatile[bot].counted_holds = {k for k in self.volatile[bot].counted_holds if k[0] != key}
        await self._start_turn(bot, source, thread, trigger, job, hwm, held_by=tuple(holds))
        return True

    def _held(self, bot: str, source: TurnSource, thread: str, gate: str, job: Job | None) -> None:
        self.sim.trace.add(self.now(), "turn_held", gateway=self.name, bot=bot, source=source,
                           thread=thread, gate=gate)
        if gate in (RATE_POSTS, RATE_WORDS):
            self._tstate(thread).held_by_rate.add(bot)
        if job is not None:
            job.pending = True
            self.sim.trace.add(self.now(), "job_waiting", gateway=self.name, bot=bot, job=job.id,
                               thread=thread, gate=gate)

    # step 4 — WHO_SPEAKS
    async def _classify(self, post: Post, addressed: tuple[str, ...]) -> None:
        if not self._classifier_applies(post):
            return
        self.decisions[post.seq] = CachedDecision()
        self.sim.trace.add(self.now(), "classifier_call", gateway=self.name, post=post.seq)
        request = self._request(post, self._all_addressed(post))  # what the classifier sees is fixed now
        await self._classifier_attempt(request, 1)

    def _classifier_applies(self, post: Post) -> bool:
        if self.classifier is None or not self.params.classifier_enabled or post.seq in self.decisions:
            return False
        if self.sim.room.is_dm or (post.controls and is_control_only(post.text)):
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
        history = self.sim.room.thread_posts(post.thread, newest_first=True)
        history = [p for p in history if p.seq < post.seq]  # on replay the room already holds newer posts
        shown = tuple(history[: self.params.classifier_posts or None])  # 0 disables the cap (3.1)
        offered = [self.sim.room.members[b] for b in self._bots_in_room() if not self._not_offered(b, post.thread)]
        return ClassifierRequest(post, addressed, shown, len(history) - len(shown), tuple(offered))

    async def _classifier_attempt(self, request: ClassifierRequest, attempt: int) -> None:
        latency = getattr(self.classifier, "latency", 0.0)
        timeout = self.params.classifier_timeout_seconds
        timed_out = bool(timeout) and latency > timeout
        self.sim.at(timeout if timed_out else latency, self._classifier_result, request, attempt, timed_out)

    async def _classifier_result(self, request: ClassifierRequest, attempt: int, timed_out: bool = False) -> None:
        post, addressed = request.post, request.addressed
        try:
            if timed_out:
                raise ClassifierError(f"timeout after {self.params.classifier_timeout_seconds}s")
            decision = await self.classifier.classify(request)
        except ClassifierError as exc:
            if attempt <= self.params.classifier_retries:
                self.sim.trace.add(self.now(), "classifier_retry", gateway=self.name, post=post.seq,
                                   attempt=attempt, error=str(exc))
                await self._classifier_attempt(request, attempt + 1)
                return
            decision = fallback_decision(self.params.classifier_fallback, post, addressed, self._bots_in_room())
        await self._decided(post, addressed, decision)

    async def _decided(self, post: Post, addressed: tuple[str, ...], decision: Decision) -> None:
        self.decisions[post.seq] = CachedDecision(decision, self.now())
        self.sim.trace.add(self.now(), "classifier_decision", gateway=self.name, post=post.seq,
                           add=list(decision.add), remove=list(decision.remove), fallback=decision.fallback,
                           reasons=dict(decision.reasons))
        for bot in self.hosted:
            if bot in decision.add and bot not in addressed and bot != post.author \
                    and post.seq > self.persisted[bot].cursor:
                await self._try_start(bot, "picked", post.thread, trigger=post)

    def _decision_for(self, bot: str, post: Post) -> bool:
        """Whether a still-valid cached decision selects ``bot`` for ``post``."""
        cached = self.decisions.get(post.seq)
        if cached is None or cached.decision is None or bot not in cached.decision.add:
            return False
        ttl = self.params.classifier_decision_ttl_seconds
        if ttl and self.now() - cached.time > ttl:
            if post.seq not in self.volatile[bot].expired_decisions:
                self.volatile[bot].expired_decisions.add(post.seq)
                self.sim.trace.add(self.now(), "classifier_expired", gateway=self.name, post=post.seq, bot=bot)
            return False
        return True

    # ── CATCH_UP: the sweeper ───────────────────────────────────────────────

    def _backlog(self, bot: str, through: int | None = None) -> list[Post]:
        return [p for p in self.sim.room.after(self.persisted[bot].cursor, through) if p.author != bot]

    async def sweep(self, bot: str, reason: str) -> None:
        if not self.online:
            return
        self.totals[bot]["sweeps"] += 1
        candidates = self._candidates(bot)
        self.sim.trace.add(self.now(), "sweep", gateway=self.name, bot=bot, reason=reason,
                           candidates=[c[0] + ":" + str(c[1]) for c in candidates])
        hwm = self.sim.room.last_seq()
        for kind, key in candidates:
            if await self._start_candidate(bot, kind, key, hwm):
                return

    def _candidates(self, bot: str) -> list[tuple[str, object]]:
        """Section 5.7's order: backlog posts that address or select the bot, oldest
        first (every one, so a held older post cannot mask a newer human mention);
        unfinished threads; job firings held in backlog; threads due for catch-up.
        Each candidate is rechecked by ``_try_start``, which holds or starts it."""
        found: list[tuple[str, object]] = []
        for post in self._backlog(bot):
            if post.kind != "notice" and (post.addresses(bot) or self._decision_for(bot, post)):
                found.append(("post", post.seq))
        # An unfinished thread is a candidate once it "has become open" (5.7), and a
        # thread is due for catch-up only when open and out of cooldown: a closed one
        # is not held, it is not a candidate, in either enforce mode.
        found += [("unfinished", t) for t in sorted(self.persisted[bot].unfinished) if not self._closed_by(bot, t)]
        found += [("job", j.id) for j in self.persisted[bot].jobs.values() if j.pending]
        found += [("catch-up", t) for t in self._due_threads(bot)]
        return found

    def _due_threads(self, bot: str) -> list[str]:
        by_thread: dict[str, list[Post]] = {}
        for post in self._backlog(bot):
            by_thread.setdefault(post.thread, []).append(post)
        due = []
        for thread, posts in by_thread.items():
            if self._closed_by(bot, thread) or self._in_cooldown(bot, thread):
                continue
            count_hit = self.params.catch_up_post_threshold and len(posts) >= self.params.catch_up_post_threshold
            age_hit = self.params.catch_up_age_seconds and \
                self.now() - posts[0].time >= self.params.catch_up_age_seconds
            if count_hit or age_hit:
                due.append((max(p.seq for p in posts), thread))
        return [t for _, t in sorted(due, reverse=True)]

    async def _start_candidate(self, bot: str, kind: str, key, hwm: int) -> bool:
        if kind == "job":
            job = self.persisted[bot].jobs.get(key)
            return job is not None and job.pending and \
                await self._try_start(bot, "job", job.thread, job=job, hwm=hwm, from_sweep=True)
        if kind == "post":
            post = self.sim.room.by_seq(key)
            if post.seq <= self.persisted[bot].cursor:
                return False
            source: TurnSource = "addressed" if post.addresses(bot) else "picked"
            if source == "picked" and not self._decision_for(bot, post):
                return False
            # the trigger decides eligibility; delivery runs through the sweep's mark (5.6)
            return await self._try_start(bot, source, post.thread, trigger=post, hwm=hwm, from_sweep=True)
        if kind == "unfinished":
            if key not in self.persisted[bot].unfinished:
                return False
            return await self._try_start(bot, "unfinished", key, hwm=hwm, from_sweep=True)
        return await self._try_start(bot, "catch-up", key, hwm=hwm, from_sweep=True)

    # ── FOLLOW_UP: jobs ─────────────────────────────────────────────────────

    def schedule_job(self, bot: str, thread: str, text: str, in_seconds: float) -> Job:
        self._job_seq += 1
        job = Job(self._job_seq, bot, thread, text, due=self.now() + in_seconds)
        self.persisted[bot].jobs[job.id] = job
        self.sim.at(in_seconds, self._fire_job, bot, job.id)
        self.sim.trace.add(self.now(), "job_scheduled", gateway=self.name, bot=bot, job=job.id,
                           thread=thread, due=job.due)
        return job

    async def _fire_job(self, bot: str, job_id: int) -> None:
        job = self.persisted[bot].jobs.get(job_id)
        if job is None:
            return
        self.sim.trace.add(self.now(), "job_fired", gateway=self.name, bot=bot, job=job.id, thread=job.thread)
        job.pending = True
        await self._try_start(bot, "job", job.thread, job=job, hwm=self.sim.room.last_seq())

    async def _job_failed(self, bot: str, job: Job) -> None:
        """Retry under the backoff; after ``follow_up_retry_cap`` failed retries post a
        notice and stop (5.11). ``0`` disables the cap (3.1)."""
        job.retries += 1
        cap = self.params.follow_up_retry_cap
        if cap and job.retries > cap:
            self.persisted[bot].jobs.pop(job.id, None)
            self.sim.trace.add(self.now(), "job_capped", gateway=self.name, bot=bot, job=job.id, thread=job.thread)
            await self._notice(bot, job.thread, "scheduled job gave up")
            return
        job.pending = True
        self.sim.trace.add(self.now(), "job_retry", gateway=self.name, bot=bot, job=job.id,
                           thread=job.thread, retries=job.retries)

    # ── step 7: HINT and delivery ───────────────────────────────────────────

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
            p.thread == thread and p.addresses(bot) and self._exempt("addressed", thread, p)
            for p in delivered
        )
        if closed_by and not opened_by_authority:
            return ThreadGroup(thread, "closed", closed_by)
        addressed_here = any(p.thread == thread and p.addresses(bot) for p in delivered)
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

    async def _start_turn(self, bot: str, source: TurnSource, thread: str, trigger: Post | None,
                          job: Job | None, hwm: int | None, held_by: tuple[str, ...]) -> None:
        through = hwm if hwm is not None else trigger.seq
        delivered = self._backlog(bot, through)
        hint = self._build_hint(bot, source, thread, delivered, held_by)
        turn = TurnInput(bot, source, thread, tuple(delivered), hint, job.text if job else None)
        state = self.volatile[bot]
        state.running = True
        self.totals[bot]["turns"] += 1
        self.sim.trace.add(self.now(), "turn_start", gateway=self.name, bot=bot, source=source,
                           thread=thread, exempt=self._exempt(source, thread, trigger),
                           delivered=[p.seq for p in delivered])
        if job is not None:
            job.pending = False
        await self._invoke(TurnRun(turn, job, through if through is not None else self.persisted[bot].cursor,
                                   generation=state.generation))

    async def _invoke(self, run: TurnRun) -> None:
        output = await self.runner.run(run.turn)
        self._account(run, output)
        self.sim.at(output.duration_seconds, self._end_turn, run, output)

    @staticmethod
    def _account(run: TurnRun, output: BotOutput) -> None:
        run.tokens_in += output.input_tokens
        run.tokens_out += output.output_tokens
        run.cache_read += output.cache_read_tokens
        run.cache_write += output.cache_write_tokens
        run.cost_usd += output.cost_usd

    # ── step 8/9: output, posting, commit ───────────────────────────────────

    async def _end_turn(self, run: TurnRun, output: BotOutput) -> None:
        bot = run.turn.bot
        if run.generation != self.volatile[bot].generation:
            self._drop_stale(run)
            return
        if output.failed:
            await self._backend_failure(run, output.error)
            return
        try:
            env = parse_envelope(output.text)
            structural, rules = validate(env, run.turn.hint, self._validation_context(bot))
        except ParseError as exc:
            structural, rules, env = [Problem("schema", None, str(exc))], [], Envelope()
        run.problems.update((p.mechanism, p.thread) for p in rules)
        run.items_seen.update((i.thread, "control") for i in env.replies if i.control is not None)
        run.items_seen.update((i.thread, "text") for i in env.replies if i.text is not None)
        rejected = structural + (rules if self.params.enforce else [])
        if rejected:
            await self._invalid(run, rejected, output)
            return
        await self._accept(run, env)

    def _drop_stale(self, run: TurnRun) -> None:
        """The process that ran this turn died (``restart``); its output is not posted and
        nothing is committed. A job it was firing goes back to pending so a sweep finds it."""
        if run.job is not None:
            run.job.pending = True
            self._request_sweep(run.turn.bot, "stale job turn")
        self.sim.trace.add(self.now(), "turn_dropped", gateway=self.name, bot=run.turn.bot,
                           source=run.turn.source, thread=run.turn.starting_thread)

    def _validation_context(self, bot: str) -> ValidationContext:
        room = self.sim.room
        return ValidationContext(
            bot=bot,
            members=frozenset(room.members),
            agents=frozenset(self.agents),
            orchestrator_in=frozenset(t for t, s in self.settings.items() if s.orchestrator == bot),
        )

    async def _invalid(self, run: TurnRun, problems: list[Problem], output: BotOutput) -> None:
        bot = run.turn.bot
        reasons = "; ".join(f"{p.mechanism}@{p.thread}: {p.reason}" for p in problems)
        self.sim.trace.add(self.now(), "invalid", gateway=self.name, bot=bot, attempt=run.attempt, reason=reasons)
        if run.attempt == 1:
            run.attempt = 2
            retry = TurnInput(bot, run.turn.source, run.turn.starting_thread, run.turn.delivered,
                              run.turn.hint, run.turn.job_text)
            run.turn = retry
            await self._invoke_retry(run, reasons)
            return
        await self._invalid_failure(run, reasons)

    async def _invoke_retry(self, run: TurnRun, error: str) -> None:
        """Return the schema error to the same session once (section 5.9)."""
        output = await self.runner.run(_with_error(run.turn, error))
        self._account(run, output)
        self.sim.at(output.duration_seconds, self._end_turn, run, output)

    async def _accept(self, run: TurnRun, env: Envelope) -> None:
        bot = run.turn.bot
        posted_threads = await self._post_replies(run, env)
        self._commit(run, env, posted_threads)
        for request in env.schedule:
            self.schedule_job(bot, request.thread or run.turn.starting_thread, request.text, request.in_seconds)
        self._count_turn_rules(run)
        self._finish(run, "normal")

    async def _post_replies(self, run: TurnRun, env: Envelope) -> set[str]:
        bot = run.turn.bot
        refused: list[str] = []
        posted: set[str] = set()
        for item in env.replies:
            if item.done:
                self.sim.trace.add(self.now(), "done", gateway=self.name, bot=bot, thread=item.thread,
                                   to=list(item.to or []))  # 8.3: record done.to without posting
                continue
            if item.control:
                settings = self._thread(item.thread)
                item.control = resolve_control(item.control, self._bots_in_room(), set(settings.muted), settings.orchestrator)
                if not item.control and item.text is None:
                    item.done = True
                    continue
            try:
                post = self.sim.append(bot, canonical_text(item), item.thread, kind="bot")
            except RefusedPost:
                refused.append(item.thread)
                self.sim.trace.add(self.now(), "refused", gateway=self.name, bot=bot, thread=item.thread)
                continue
            self._count_own_post(bot, post)  # before any gateway, this one included, receives it
            await self.sim.deliver(post)
            posted.add(item.thread)
            run.posts += 1
            self.totals[bot]["posts"] += 1
            self.totals[bot]["words"] += post.words
        self.volatile[bot].refused = tuple(refused)
        return posted

    def _commit(self, run: TurnRun, env: Envelope, posted: set[str]) -> None:
        bot = run.turn.bot
        state = self.persisted[bot]
        if run.hwm > state.cursor:
            state.cursor = run.hwm
            self.sim.trace.add(self.now(), "cursor_saved", gateway=self.name, bot=bot, seq=run.hwm)
        answered = {i.thread for i in env.replies}
        for group in run.turn.hint.threads:
            if group.thread in self.sim.room.archived:
                continue  # removed while the turn ran: it can no longer be answered (5.4)
            if group.group == "closed" and group.thread not in answered and group.thread not in state.unfinished:
                state.unfinished.add(group.thread)
                self.sim.trace.add(self.now(), "unfinished_added", gateway=self.name, bot=bot, thread=group.thread)
        for thread in answered & state.unfinished:
            state.unfinished.discard(thread)
            self.sim.trace.add(self.now(), "unfinished_removed", gateway=self.name, bot=bot, thread=thread)
        if run.job is not None:
            state.jobs.pop(run.job.id, None)

    def _finish(self, run: TurnRun, outcome: str) -> None:
        bot = run.turn.bot
        state = self.volatile[bot]
        state.running = False
        self.totals[bot]["tokens_in"] += run.tokens_in
        self.totals[bot]["tokens_out"] += run.tokens_out
        self.totals[bot]["cache_read"] += run.cache_read
        self.totals[bot]["cache_write"] += run.cache_write
        self.totals[bot]["cost_usd"] += run.cost_usd
        if outcome == "normal":
            state.retry_after, state.backoff_level, state.backoff_notice_sent = 0.0, 0, False
            state.invalid_streak, state.degraded, state.invalid_notice_sent = 0, False, False
            state.previous_failure = None
        self.sim.trace.add(self.now(), "turn_end", gateway=self.name, bot=bot, outcome=outcome, posts=run.posts)
        self._request_sweep(bot, "turn end")

    # ── FAILURE_NOTICE ──────────────────────────────────────────────────────

    async def _backend_failure(self, run: TurnRun, error: str) -> None:
        bot = run.turn.bot
        state = self.volatile[bot]
        self.totals[bot]["failures"] += 1
        state.previous_failure = f"backend failure ({error})"
        if run.attempt > 1:
            self._count_turn_rules(run)  # the first attempt's rejections happened
        if run.job is not None:
            await self._job_failed(bot, run.job)
        if self.params.failure_backoff_seconds:
            wait = min(self.params.failure_backoff_seconds * 2 ** state.backoff_level,
                       self.params.failure_backoff_max_seconds or float("inf"))
            state.retry_after = self.now() + wait
            state.backoff_level += 1
            self.sim.trace.add(self.now(), "backoff", gateway=self.name, bot=bot, until=state.retry_after)
            self.sim.at(wait, self._requested_sweep, bot, "backoff ended")
        if not state.backoff_notice_sent:
            state.backoff_notice_sent = True
            await self._notice(bot, run.turn.starting_thread, "backend failure")
        self._finish(run, "backend_failure")

    async def _invalid_failure(self, run: TurnRun, reasons: str) -> None:
        bot = run.turn.bot
        state = self.volatile[bot]
        self.totals[bot]["invalid"] += 1
        state.invalid_streak += 1
        state.previous_failure = f"invalid envelope ({reasons})"
        if run.job is not None:
            await self._job_failed(bot, run.job)
        limit = self.params.consecutive_invalid_limit
        if not state.invalid_notice_sent:
            state.invalid_notice_sent = True
            await self._notice(bot, run.turn.starting_thread, "invalid reply")
        if limit and state.invalid_streak >= limit and not state.degraded:
            state.degraded = True
            self.sim.trace.add(self.now(), "degraded", gateway=self.name, bot=bot, streak=state.invalid_streak)
            await self._notice(bot, run.turn.starting_thread, "degraded")
        self._count_turn_rules(run)  # a turn that failed on a rule is the worst offender
        self._finish(run, "invalid")

    async def _notice(self, bot: str, thread: str, cause: str) -> None:
        try:
            post = await self.sim.post(bot, f"{NOTICE_PREFIX} {cause}", thread, kind="notice")
        except RefusedPost:
            return
        self.sim.trace.add(self.now(), "notice", gateway=self.name, bot=bot, seq=post.seq, cause=cause)

    # ── rule counting (the enforce toggle's output) ─────────────────────────

    def _count_rule(self, bot: str, thread: str | None, mechanism: str, outcome: str) -> None:
        count = self.rules.setdefault(mechanism, RuleCount())
        count.opportunities += 1
        setattr(count, outcome, getattr(count, outcome) + 1)
        self.sim.trace.add(self.now(), "rule", gateway=self.name, bot=bot, thread=thread,
                           mechanism=mechanism, outcome=outcome)

    def _count_turn_rules(self, run: TurnRun) -> None:
        """One count per opportunity the turn had, from the problems and items seen across
        its attempts: ``rejected`` when enforced, ``violated`` when not, else ``complied``.
        The starting thread closed by a rule exists only with enforcement off (the turn
        ran where the rule would have held it) and is counted apart, as ``held_*``."""
        bot = run.turn.bot
        starting = run.turn.starting_thread
        broke = "rejected" if self.params.enforce else "violated"
        for group in run.turn.hint.threads:
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

    # ── STATUS ──────────────────────────────────────────────────────────────

    def _thread_status(self, thread: str) -> dict:
        s = self._thread(thread)
        recent = self._recent(thread)
        return {"orchestrator": s.orchestrator, "muted": sorted(s.muted), "limits": self._limits(thread),
                "recent_posts": len(recent), "recent_words": sum(r[2] for r in recent),
                "unknown_mutes": list(s.unknown_mutes)}

    def status(self) -> dict:
        return {
            "gateway": self.name,
            "threads": {t: self._thread_status(t) for t in list(self.settings)},
            "bots": {
                b: {"cursor": self.persisted[b].cursor, "unfinished": sorted(self.persisted[b].unfinished),
                    "backlog": len(self._backlog(b)), "running": self.volatile[b].running,
                    "retry_after": self.volatile[b].retry_after, "degraded": self.volatile[b].degraded,
                    "invalid_streak": self.volatile[b].invalid_streak,
                    "expired_decisions": len(self.volatile[b].expired_decisions),
                    "jobs": len(self.persisted[b].jobs)}
                for b in self.hosted
            },
            "agents": list(self.agents),
            "bot_like_members_missing": [b for b in self.sim.room.bots() if b not in self.agents],
            "rules": {m: vars(c) for m, c in self.rules.items()},
            "totals": self.totals,
        }


def _with_error(turn: TurnInput, error: str) -> TurnInput:
    return TurnInput(turn.bot, turn.source, turn.starting_thread, turn.delivered, turn.hint,
                     job_text=turn.job_text, error=error)
