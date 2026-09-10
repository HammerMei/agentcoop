"""The mark that keeps a message the gateway could not take reachable later.

**This is AgentCoop's own bookkeeping, not a platform behaviour.** Both connectors apply
backpressure: when every processor queue is full, a message is refused and reported as
still owed. Refusing it is only honest if something remembers where to look for it, because
the per-message dedup id is forgotten precisely so a later replay can bring it back — and
the ordinary watermark cannot serve, since the next *accepted* message moves it past the
refused one for good.

So this module is shared for the reason AgentCoop shares anything: the question is AgentCoop's, and
asking it twice is how it ends up answered once. It is deliberately **not** an attempt to
make the connectors behave alike. Everything platform-specific stays with the platform:

* **Which branches are hand-backs** is control flow, and the two connectors' differ.
* **What else can invalidate a window.** Rocket.Chat closes one on a confirmed membership
  removal, because under subscribe-all the stream keeps delivering a channel the account
  has been removed from. Mattermost's *live* delivery tracks membership at the server
  (§6.2), so it needs no live gate — but that says nothing about its replay, which is a
  REST fetch the bot's token can still make against a public channel it has left.
  Mattermost therefore revalidates membership by id (`_resolved_channel`) at the top of
  `replay_room_since`, before a single replayed post is dispatched; a channel that is no
  longer the account's is skipped and reclaimed through the same removal hook a live
  `user_removed` event runs.
* **Why an outage window is captured at all.** Rocket.Chat resubscribes rooms one at a
  time, so a room that is live again while others are still confirming moves its watermark
  past the whole gap — that is what `_snapshot_replay_boundaries` exists for. Mattermost
  has no per-channel subscribe handshake; one connection resumes every channel at once, so
  that race does not exist and Mattermost does not get that call.

What is genuinely common is only this: a mark, and the rule for who may clear it.
"""

from __future__ import annotations

from .adapter_utils import ts_to_float


def just_before(ts: str) -> str:
    """The largest timestamp strictly below `ts`, as a replay lower bound.

    Shared because both platforms hand AgentCoop epoch milliseconds as a string — Rocket.Chat
    from `ts.$date`, Mattermost from `create_at` — so "one millisecond below" is an exact
    value on both rather than an epsilon.

    Strictly below, not equal. A replay hands the same mark to the filter as the watermark,
    and the filter rejects `msg_ts <= last_ts` as already processed, so a bound equal to the
    message fetches it and then throws it away. That is a fix that changes nothing, and it
    is what the first version of this did.

    A value that will not parse is returned unchanged: an unusable bound beats a fabricated
    one, and the caller's alternative is no bound at all.
    """
    try:
        return str(int(float(ts)) - 1)
    except (TypeError, ValueError):
        return ts


class ReplayWindow:
    """Claim/discharge bookkeeping for one room's replay mark.

    A mixin of methods only. The two fields are declared by the dataclasses that use it
    rather than here, because both have a non-default field (`room`) and inherited defaults
    would have to come first. Declaring them at the use site also keeps them visible next to
    the watermark they qualify, which is where a reader looks for them.

    Implementors must provide::

        replay_boundary: str | None = None
        boundary_claims: int = 0
        promised_ids: set = field(default_factory=set)

    `promised_ids` is the third piece of the same bookkeeping: the ids of frames a
    creation episode handed back through the queue while a live message may already
    have moved the watermark past them. A boundary is claimed for one of them only
    when the filter actually rejects it as already processed (see each connector's
    handler) — not pre-emptively at creation, which left a claim nothing discharged
    and had shutdown persist a boundary at the room's creation.
    """

    replay_boundary: str | None
    boundary_claims: int
    promised_ids: set

    def claim_boundary(self, *fallbacks: str | None) -> int:
        """Record that someone still needs this window read. Returns the claim count.

        The window is the *oldest* mark anyone owes a read of, so **the oldest candidate
        wins** — not the first one offered.

        Order mattered and was wrong. The hand-back sites offer the live watermark before
        the point just below the refused message, on the assumption that the watermark is
        the older of the two. It usually is; it is not always. Replay is not serialized
        against the per-room worker on either connector, so a newer message can be accepted
        while an older one is still in the handler — and then the watermark is *above* the
        message being refused. Taking the first offer put the mark past the very message it
        was opened for, which is the one thing it may never do.

        A candidate that will not parse sorts as the oldest, deliberately: a lower bound
        that is too low costs a re-fetch that dedup absorbs, and one that is too high loses
        a message. That is the same choice `just_before` makes for the same reason.

        **The count is what makes this a method rather than an `or` expression at each
        site.** Two claimants routinely want the same timestamp — a hand-back that lands
        while a replay is reading the very window it is claiming — and the value cannot tell
        them apart. A batch comparing the mark it snapshotted against the mark it finds
        therefore reads "unchanged" in exactly the case the comparison exists to catch,
        closes the window, and the next accepted message moves the watermark past the
        handed-back one for good. That defect survived four review rounds.

        A fully falsy claim writes and counts nothing: there is no window to owe a read of,
        and no replay will look at this room.
        """
        candidates = [c for c in (self.replay_boundary, *fallbacks) if c]
        if not candidates:
            return self.boundary_claims
        self.replay_boundary = min(
            candidates,
            key=lambda c: ts_to_float(c) if ts_to_float(c) is not None else float("-inf"),
        )
        self.boundary_claims += 1
        return self.boundary_claims

    def discharge_boundary(self, claims_at_entry: int) -> bool:
        """Close the window, unless it was claimed again since `claims_at_entry`.

        False means it is now owed to somebody else and has been left open. Every site that
        can decide a replay has read the window goes through here — including the ones a
        long way from the snapshot, since a REST round trip is ample room for a hand-back.
        """
        if self.boundary_claims != claims_at_entry:
            return False
        self.replay_boundary = None
        self.boundary_claims = 0
        # Promises are NOT settled here. Each is settled by the frame it names,
        # at the filter: accepted, or rejected and judged. A replay does not know
        # which promised frames its page covered — a promise made during the
        # replay names a frame newer than the page, and a promise made before it
        # can name a frame BELOW the watermark the page started from (the
        # creation path promises instead of claiming, so the window does not
        # reach down to it). Clearing on discharge lost both kinds: the queued
        # frame then arrived, read as already processed, and nobody kept it
        # reachable (Codex, PR #140 round 3). The one thing a stale promise can
        # cost is a redundant claim when a frame the replay DID dispatch arrives
        # again from the queue — one extra fetch at the next recovery, not a
        # lost message.
        return True

    def discard_boundary(self) -> None:
        """Drop the window and every claim on it, because nobody is entitled to it now.

        Distinct from `discharge_boundary`: that one reports a window as *read*, this one
        says it should never be read. Rocket.Chat's membership removal is the only caller —
        a window that spans a removal would replay the interval the account was not a member
        for. Mattermost has no caller: its removal check runs at the top of the replay
        itself and returns before the window is read, so there is no window to discard.
        """
        self.replay_boundary = None
        self.boundary_claims = 0
        self.promised_ids.clear()   # nobody is entitled to those either
