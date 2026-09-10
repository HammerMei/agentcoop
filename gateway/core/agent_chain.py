"""Agent-chain primitives shared between the core and connector layers.

Both ``gateway.core.agent_turn_runner`` and connector packages (e.g.
``gateway.connectors.rocketchat``, ``gateway.connectors.mattermost``) import
from here so that the token, prompt-suffix builder, and turn-budget tracker
are defined exactly once — no connector needs to import from another
connector to get platform-agnostic loop-protection logic.

``TurnStore`` was originally implemented inside the Rocket.Chat connector but
is keyed purely on ``(room_id, thread_id, sender)`` strings with no RC-specific
behavior, so it lives here now.  ``gateway.connectors.rocketchat.agent_chain``
re-exports it for backward compatibility.
"""

from __future__ import annotations

import collections
import logging
import time
from dataclasses import dataclass, field

# How long a generation tombstone outlives the context it replaced, as a multiple of
# the context TTL. It only has to cover a delivery still in flight when the context
# expired; past that, keeping it costs more than the one turn it would protect.
_TOMBSTONE_TTL_FACTOR = 2

# How many released tokens a context remembers, so a repeated release of one of them is
# refused rather than taking a turn twice. Only has to exceed the deliveries that can be
# in flight at once for one sender in one thread — a live room worker and a replay loop —
# so this is orders of magnitude of headroom, chosen so the bound never has to be reasoned
# about again rather than because two hundred and fifty-six means anything.
_RELEASED_TOKENS_REMEMBERED = 256

logger = logging.getLogger("coop.core.agent_chain")

# Sentinel the LLM outputs to self-terminate an agent chain turn.
# ACG detects this via exact match (response.text.strip() == TOKEN).
AGENT_CHAIN_TERMINATION_TOKEN = "<end-of-agent-chain>"


@dataclass
class AgentChainConfig:
    """Configuration for controlled agent-to-agent communication.

    Platform-agnostic — shared by every connector config that supports
    agent-chain (e.g. RocketChatConfig, MattermostConfig).
    """
    agent_usernames: list[str] = field(default_factory=list)
    max_turns: int = 5
    ttl_seconds: float = 3600.0


def build_agent_chain_context(turn: int, max_turns: int) -> str:
    """Build the toll-call prompt suffix injected when processing an agent-chain message.

    turn:      1-based current turn number (already incremented).
    max_turns: configured budget ceiling.
    """
    lines = [
        f"\n---\n[Agent chain: turn {turn}/{max_turns}]"
    ]
    if turn == max_turns - 1:
        lines.append(
            "\u26a0\ufe0f  Your next response will be your last turn in this agent chain."
        )
    elif turn >= max_turns:
        lines.append(
            "\u26a0\ufe0f  This is your final turn in this agent chain. "
            "Please wrap up gracefully.\n"
            "If the task is not yet complete, you may use the scheduler tool "
            "to schedule a follow-up message and continue with a fresh turn budget."
        )
    if turn < max_turns:
        lines.append(
            f"If this conversation is repeating without making progress (a loop), "
            f"or if you have nothing meaningful to add, respond with ONLY: "
            f"{AGENT_CHAIN_TERMINATION_TOKEN}"
        )
    return "\n".join(lines)


@dataclass
class _TurnContext:
    turns: int = 0
    last_updated: float = field(default_factory=time.monotonic)
    # Incremented by every reset. A turn number cannot identify the increment that
    # produced it, because a reset starts the next count at one again — so a delivery
    # still in flight would match a turn that belongs to someone else. See `release_turn`.
    generation: int = 0
    # Monotonic within a generation: the token handed to each increment. `turns` is the
    # *live* count and moves in both directions, so it cannot name a delivery — two
    # overlapping deliveries would be told "you took turn 2" if one released in between.
    issued: int = 0
    # Tokens already given back, so a repeated release is a no-op rather than a second
    # decrement, and an earlier delivery can still release after a later one has taken
    # its turn.
    #
    # **Bounded, and the bound is the point.** A message handed back repeatedly — a
    # processor that stays full through a reconnect loop — takes a fresh token per attempt
    # and gives every one of them back, while each attempt's *increment* refreshes
    # `last_updated`, so the TTL never reclaims the context. Remembering every token would
    # grow for as long as that lasts.
    #
    # A prefix counter was tried here first and does not work: a token that is *delivered*
    # is never released, so the prefix stops at the first one and every later hand-back
    # accumulates behind it. In any real chain that is token 1.
    #
    # Which direction to be wrong in is the whole design. Forgetting a **released** token
    # means a second release of it is honoured, and the count goes one below the truth —
    # the agent gets one extra turn. Forgetting an **outstanding** token means a genuine
    # release is refused, the budget stays spent on a message that was never sent, and the
    # filter then rejects it as complete: the message is lost. So the bound goes here and
    # not on the outstanding side, and it has to be larger than the number of deliveries
    # that can be in flight at once for one sender in one thread — a live worker and a
    # replay loop, which is two.
    released: collections.deque = field(
        default_factory=lambda: collections.deque(maxlen=_RELEASED_TOKENS_REMEMBERED))
    released_set: set = field(default_factory=set)

    def is_released(self, token: int) -> bool:
        return token in self.released_set

    def mark_released(self, token: int) -> None:
        if len(self.released) == self.released.maxlen:
            self.released_set.discard(self.released[0])
        self.released.append(token)
        self.released_set.add(token)

    def start_fresh_count(self) -> None:
        """Zero the count and invalidate every token of the previous one.

        Zeroed rather than dropped, so the generation survives: a delivery still in flight
        has to be able to tell that the count it took its turn from is gone.

        A method because the two resets — one sender, a whole room — did this as two
        copies of five lines, and the released-token bookkeeping would have been the sixth.
        Left behind, it says tokens of the *new* count have already been given back, and
        those releases are refused: the budget stays spent on messages never sent.
        """
        self.turns = 0
        self.generation += 1
        self.issued = 0
        self.released.clear()
        self.released_set.clear()
        self.last_updated = time.monotonic()


class TurnStore:
    """Thread-safe (asyncio single-threaded) per-sender turn budget tracker.

    Key: (room_id, thread_id, sender_username)
    - Each sender has an independent counter against the current bot.
    - On force-drop (budget exhausted): counter stays at max, sender locked until
      human message or TTL expiry.
    - On self-termination (LLM gracefully exits): counter stays, chain dies
      naturally (no reply posted → no trigger for the other agent to reply).
    - On human message: reset_all clears all counters for the room/thread.
    - TTL GC: entries older than ttl_seconds are purged lazily on each check,
      giving any sender a fresh full budget after a long idle period.

    Future consideration — two-TTL design:
        Force-drop and self-termination may warrant different TTLs.  A force-drop
        is like a dropped call: the other agent likely wants to keep talking and
        will retry quickly, so a shorter TTL is appropriate.  Self-termination is a
        natural hang-up: the room should stay quiet, so a longer TTL gives more
        cooldown before fresh budget is granted.  For now a single ttl_seconds is
        used for simplicity; split into force_drop_ttl / self_terminate_ttl if
        real-world tuning shows the single value is too coarse.
    """

    def __init__(self, ttl_seconds: float = 3600.0) -> None:
        self._ttl = ttl_seconds
        self._store: dict[tuple[str, str | None, str], _TurnContext] = {}
        # Generations outlive the contexts they belong to. A context is dropped by `_gc`
        # once it goes quiet, and recreating it at generation zero would make an old
        # delivery's token match a fresh context — the reset case again, arriving through
        # expiry instead. Kept here, so the number a key has reached is never reused.
        #
        # `(generation, recorded_at)`, and pruned, because the key includes a **thread
        # id**: a long-lived connector meets an unbounded number of threads, and one
        # tombstone per sender in each would outlive every context and defeat the very
        # reclamation the TTL exists for. An earlier comment here claimed these were
        # bounded by the rooms served, which was wrong — threads are not rooms.
        #
        # A tombstone only has to outlive a delivery that is still in flight, so it is
        # kept for `_TOMBSTONE_TTL_FACTOR` times the context TTL and then dropped. Beyond
        # that the release it would have refused is one whose delivery has been running
        # for hours; a wrongly-honoured release costs one turn, and an unbounded map costs
        # the process.
        self._generations: dict[tuple[str, str | None, str], tuple[int, float]] = {}

    # Key helpers
    @staticmethod
    def _key(room_id: str, thread_id: str | None, sender: str) -> tuple[str, str | None, str]:
        return (room_id, thread_id, sender)

    def generation(self, room_id: str, thread_id: str | None, sender: str) -> int:
        """How many times this context has been reset, and therefore which count is live.

        A turn number alone cannot identify the increment that produced it: a reset drops
        the counter and the next agent message starts again at one, so an in-flight
        delivery holding "I took turn 1" would match a turn 1 that belongs to somebody
        else. This distinguishes them, and it survives the reset because a reset zeroes
        the context rather than removing it.
        """
        key = self._key(room_id, thread_id, sender)
        ctx = self._store.get(key)
        if ctx is not None:
            return ctx.generation
        remembered = self._generations.get(key)
        return remembered[0] if remembered else 0

    def check_and_increment(
        self,
        room_id: str,
        thread_id: str | None,
        sender: str,
        max_turns: int,
    ) -> tuple[bool, int, int]:
        """Check turn budget and increment if allowed.

        Returns:
            (allowed, current_turn_after_increment, token)

            `allowed=False` means the message should be dropped, and the token is 0.
            The token names *this* increment and is what `release_turn` needs: the turn
            number cannot, because it is the live count and moves in both directions —
            two overlapping deliveries would both be told "turn 2" if one released in
            between.
        """
        self._gc()
        key = self._key(room_id, thread_id, sender)
        ctx = self._store.get(key)
        if ctx is None:
            remembered = self._generations.get(key)
            ctx = _TurnContext(generation=remembered[0] if remembered else 0)
            self._store[key] = ctx

        if ctx.turns >= max_turns:
            return False, ctx.turns, 0

        ctx.turns += 1
        ctx.issued += 1
        ctx.last_updated = time.monotonic()
        return True, ctx.turns, ctx.issued

    def release_turn(
        self,
        room_id: str,
        thread_id: str | None,
        sender: str,
        token: int,
        generation: int,
    ) -> int:
        """Give back a turn taken by `check_and_increment` for a message not delivered.

        The budget counts *turns an agent took*, and a message the gateway hands back for
        a later retry has not taken one — but the increment already happened, because the
        filter runs before the capacity preflight and before the handler. Every retry of
        the same document would otherwise spend another turn, and the budget would run out
        on a message that was never dispatched: the filter then rejects it as complete,
        the replay reports success, and the window closes over a message nobody saw.

        `token` is what `check_and_increment` handed *this* delivery. Deliveries overlap
        by design — replay and live traffic run concurrently — so the release has to name
        the increment it is undoing rather than the state it expects to find:

        * an unconditional decrement takes a turn from a delivery that succeeded, and the
          chain can then run past `max_turns`;
        * matching on the turn *number* fails the other way. A later delivery moves the
          count past it, the release is refused, and the budget stays spent on a message
          that was never sent — after which `check_and_increment` refuses the retry as
          exhausted, the filter reports it complete, and the replay closes its window over
          it. "Marginally stricter" is not the safe direction it looks like; it loses the
          message.

        The generation is still checked, because a reset makes every token of the previous
        count meaningless and a fresh count reissues the same numbers.
        """
        key = self._key(room_id, thread_id, sender)
        ctx = self._store.get(key)
        if ctx is None:
            # Garbage-collected since this delivery took its turn; nothing of it remains.
            return 0
        if ctx.generation != generation:
            # A reset started a fresh count. Nothing in it belongs to this delivery, and
            # the numbers cannot say so on their own: a fresh count begins at one again.
            return ctx.turns
        if token <= 0 or token > ctx.issued or ctx.is_released(token):
            # Never issued here, or already given back. Either is a caller bug, and
            # neither may take a turn from a delivery that is still using it.
            return ctx.turns
        ctx.mark_released(token)
        ctx.turns -= 1
        ctx.last_updated = time.monotonic()
        return ctx.turns

    def current_turns(self, room_id: str, thread_id: str | None, sender: str) -> int:
        """Return current turn count for a sender (0 if not tracked)."""
        key = self._key(room_id, thread_id, sender)
        ctx = self._store.get(key)
        return ctx.turns if ctx else 0

    def reset_sender(self, room_id: str, thread_id: str | None, sender: str) -> None:
        """Reset turn counter for a specific sender (call on any drop).

        Zeroed rather than dropped — see `_TurnContext.start_fresh_count`. `_gc` still
        reclaims the entry once it goes quiet, by which time no delivery can be holding a
        turn from it.
        """
        key = self._key(room_id, thread_id, sender)
        ctx = self._store.get(key)
        if ctx is not None:
            ctx.start_fresh_count()
            self._generations[key] = (ctx.generation, time.monotonic())
        logger.debug("Agent chain counter reset for sender=%s thread=%s", sender, thread_id)

    def reset_all(self, room_id: str, thread_id: str | None) -> None:
        """Reset all agent counters for a room/thread context (call on human message)."""
        keys_to_remove = [
            k for k in self._store if k[0] == room_id and k[1] == thread_id
        ]
        for k in keys_to_remove:
            ctx = self._store[k]
            ctx.start_fresh_count()
            self._generations[k] = (ctx.generation, time.monotonic())
        if keys_to_remove:
            logger.debug(
                "Agent chain counters reset for room=%s thread=%s (%d entries)",
                room_id, thread_id, len(keys_to_remove),
            )

    def _gc(self) -> None:
        """Remove entries older than TTL."""
        now = time.monotonic()
        stale_tombstones = [
            k for k, (_gen, at) in self._generations.items()
            if now - at > self._ttl * _TOMBSTONE_TTL_FACTOR
        ]
        for k in stale_tombstones:
            del self._generations[k]
        expired = [k for k, v in self._store.items() if now - v.last_updated > self._ttl]
        for k in expired:
            # The generation is carried forward rather than dropped with the context. A
            # delivery can outlive the TTL — normalization and a handler are not bounded
            # by it — and its token must not match the context that replaces this one.
            self._generations[k] = (self._store[k].generation + 1, now)
            del self._store[k]
