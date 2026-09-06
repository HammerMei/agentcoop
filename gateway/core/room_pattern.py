"""Glob patterns for matching room names in watcher rules.

Implements the pattern language specified in `docs/design/dynamic-watcher-design.md`
§2.1. It is deliberately smaller than `fnmatch` and smaller than regex:

| | |
|---|---|
| syntax | `*` (any run, including empty), `?` (exactly one character), `[…]` character class. Nothing else — no alternation, no quantifiers, no anchors |
| matched against | the room's **full** platform name, implicitly anchored at both ends |
| case | sensitive; both platforms' slugs are lowercase by construction |
| unicode | compared NFC-normalised, so a decomposed pattern matches a composed name |

The constraint is not arbitrary: it is what makes *subsumption* decidable, which
is what lets config load warn exactly when one rule is fully shadowed by an
earlier one instead of guessing.

**Matching and subsumption share one automaton.** It would be faster to compile
to `re` for matching and keep the automaton only for subsumption, but then two
implementations of the same language would have to agree forever — and the first
time they disagreed, a rule would match a room at runtime that the load-time
shadowing check believed unreachable. One semantics, one bug surface.

Two consequences of the syntax being closed, both deliberate:

* **There is no escape character.** `\\` is an ordinary literal, so a pattern
  cannot match a room whose name really contains `*`, `?` or `[`. Both platforms
  build room names as slugs that exclude those characters, and adding an escape
  would widen the language that the subsumption check has to stay exact over.
  A literal `[` is still reachable as `[[]`.
* **`*` has no special case for any character.** Unlike a path glob it spans
  `-`, `.` and `/` freely, because a room name is one flat token rather than a
  path.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

__all__ = [
    "InvalidRoomPattern",
    "RoomPattern",
    "normalize_room_name",
    "union_intersects",
    "union_subsumes",
]


class InvalidRoomPattern(ValueError):
    """A pattern is not valid glob syntax.

    Raised at config load, never on the message-delivery path — an operator is
    present to fix it at load time and absent at delivery time.
    """


def normalize_room_name(name: str) -> str:
    """NFC-normalise a room name or pattern.

    Applied to both sides of every comparison so a decomposed pattern matches a
    composed name. Platform slugs are ASCII in practice, which is exactly why
    this is cheap to do unconditionally rather than conditionally.
    """
    return unicodedata.normalize("NFC", name)


class _Kind(Enum):
    LITERAL = "literal"
    ANY_ONE = "any_one"  # ?
    ANY_RUN = "any_run"  # *
    CLASS = "class"  # [...]


@dataclass(frozen=True)
class _Token:
    kind: _Kind
    char: str = ""
    members: frozenset[str] = frozenset()
    negated: bool = False

    def accepts(self, ch: str) -> bool:
        match self.kind:
            case _Kind.LITERAL:
                return ch == self.char
            case _Kind.ANY_ONE:
                return True
            case _Kind.CLASS:
                return (ch in self.members) != self.negated
            case _:  # ANY_RUN consumes via its own loop, not here
                return False


# A character class listing more than this many members is not expanded for
# subsumption purposes. Subsumption only drives a warning, so declining to
# decide is safe; silently building a 65k-symbol alphabet is not.
_MAX_CLASS_MEMBERS = 1024

# Ceiling on the product construction below. Reaching it means "not proven
# subsumed", never "subsumed".
_MAX_PRODUCT_STATES = 20_000

# Ceiling on the O(n^2) NFC pair check in _shared_alphabet. Above it, no alphabet
# is built and both callers fall back to reporting nothing.
_MAX_PAIR_CHECKS = 65_536


def _parse_class(pattern: str, i: int) -> tuple[_Token, int]:
    """Parse a `[...]` class starting at the `[`. Returns the token and the
    index just past the closing `]`."""
    j = i + 1
    negated = False
    if j < len(pattern) and pattern[j] in "!^":
        negated = True
        j += 1
    members: set[str] = set()
    # A `]` immediately after the opening bracket (or its negation) is a literal
    # `]`, matching POSIX and fnmatch. Otherwise it closes the class.
    first = True
    while j < len(pattern):
        if pattern[j] == "]" and not first:
            if not members:
                raise InvalidRoomPattern(
                    f"empty character class in pattern {pattern!r}"
                )
            return _Token(_Kind.CLASS, members=frozenset(members), negated=negated), j + 1
        # range a-z
        if (
            j + 2 < len(pattern)
            and pattern[j + 1] == "-"
            and pattern[j + 2] != "]"
        ):
            lo, hi = pattern[j], pattern[j + 2]
            if ord(lo) > ord(hi):
                raise InvalidRoomPattern(
                    f"reversed range {lo!r}-{hi!r} in pattern {pattern!r}"
                )
            if ord(hi) - ord(lo) + 1 > _MAX_CLASS_MEMBERS:
                raise InvalidRoomPattern(
                    f"character range {lo!r}-{hi!r} in pattern {pattern!r} spans "
                    f"more than {_MAX_CLASS_MEMBERS} characters"
                )
            members.update(chr(c) for c in range(ord(lo), ord(hi) + 1))
            j += 3
        else:
            members.add(pattern[j])
            j += 1
        first = False
    raise InvalidRoomPattern(f"unterminated '[' in pattern {pattern!r}")


def _parse(pattern: str) -> tuple[_Token, ...]:
    tokens: list[_Token] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            # Collapse runs of `*`: `**` is the same language as `*`, and
            # collapsing keeps the automaton and the subsumption search smaller.
            if not tokens or tokens[-1].kind is not _Kind.ANY_RUN:
                tokens.append(_Token(_Kind.ANY_RUN))
            i += 1
        elif ch == "?":
            tokens.append(_Token(_Kind.ANY_ONE))
            i += 1
        elif ch == "[":
            tok, i = _parse_class(pattern, i)
            tokens.append(tok)
        else:
            tokens.append(_Token(_Kind.LITERAL, char=ch))
            i += 1
    return tuple(tokens)


class RoomPattern:
    """A compiled room-name glob.

    Compilation happens at config load so an invalid pattern can never surface
    on the delivery path.
    """

    __slots__ = ("raw", "_tokens")

    def __init__(self, pattern: str) -> None:
        if not isinstance(pattern, str):
            raise InvalidRoomPattern(f"pattern must be a string, got {type(pattern).__name__}")
        if pattern == "":
            raise InvalidRoomPattern("pattern must not be empty")
        self.raw = pattern
        self._tokens = _parse(normalize_room_name(pattern))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"RoomPattern({self.raw!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, RoomPattern) and self._tokens == other._tokens

    def canonical(self) -> str:
        """The pattern re-spelled from its tokens — one spelling per language.

        `==` compares tokens, so `*` and `**`, or a name and its NFC form, are
        the same pattern; a fingerprint of a config must agree with that, or
        a rewrite to an equivalent spelling reads as "no changes" to the diff
        and "differs" to the digest, forever (#144). Deterministic across
        processes, unlike `hash()`; `raw` stays what an operator sees.
        """
        out = []
        for t in self._tokens:
            if t.kind is _Kind.LITERAL:
                out.append(t.char)
            elif t.kind is _Kind.ANY_ONE:
                out.append("?")
            elif t.kind is _Kind.ANY_RUN:
                out.append("*")
            else:
                # Members as comma-separated code points: the body holds only hex
                # digits and commas, so the first `]` closes the class and a
                # member `]`, `!` or `^` can never be misread (`[]=]` vs `[=]]`,
                # `[a!]` vs `[!a]`). A literal `[` cannot occur (it always opens a
                # class), so `[` unambiguously starts a class token. Not a valid
                # glob — this is a fingerprint, `raw` is for reading.
                body = ",".join(format(ord(c), "x") for c in sorted(t.members))
                out.append("[" + ("!" if t.negated else "=") + body + "]")
        return "".join(out)

    def __hash__(self) -> int:
        return hash(self._tokens)

    @property
    def is_literal(self) -> bool:
        """True when the pattern contains no metacharacters.

        Connectors that declare no unsolicited inbound require literal patterns,
        since they cannot discover rooms to match against (§2.6).
        """
        return all(t.kind is _Kind.LITERAL for t in self._tokens)

    def _closure(self, states: Iterable[int]) -> frozenset[int]:
        """Add states reachable without consuming input.

        `*` is the only source of epsilon moves: it may match the empty run and
        so lets position i advance to i+1 for free.
        """
        out: set[int] = set()
        stack = list(states)
        while stack:
            s = stack.pop()
            if s in out:
                continue
            out.add(s)
            if s < len(self._tokens) and self._tokens[s].kind is _Kind.ANY_RUN:
                stack.append(s + 1)
        return frozenset(out)

    def _start(self) -> frozenset[int]:
        return self._closure([0])

    def _step(self, states: frozenset[int], ch: str) -> frozenset[int]:
        nxt: set[int] = set()
        for s in states:
            if s >= len(self._tokens):
                continue
            tok = self._tokens[s]
            if tok.kind is _Kind.ANY_RUN:
                nxt.add(s)  # stay inside the run
            elif tok.accepts(ch):
                nxt.add(s + 1)
        return self._closure(nxt)

    def _accepts(self, states: frozenset[int]) -> bool:
        return len(self._tokens) in states

    def matches(self, name: str) -> bool:
        """Whether this pattern matches a room name, anchored at both ends."""
        states = self._start()
        for ch in normalize_room_name(name):
            states = self._step(states, ch)
            if not states:
                return False
        return self._accepts(states)

    def _alphabet_members(self) -> set[str]:
        """Characters this pattern can distinguish."""
        out: set[str] = set()
        for t in self._tokens:
            if t.kind is _Kind.LITERAL:
                out.add(t.char)
            elif t.kind is _Kind.CLASS:
                out.update(t.members)
        return out


def _sentinel(used: set[str]) -> str:
    """A character no pattern mentions, standing for "any other character".

    Sound because every automaton here treats all unmentioned characters
    identically — they can only be consumed by `?`, `*` or a negated class.
    """
    for cp in range(0xE000, 0xF900):  # private use area
        ch = chr(cp)
        if ch not in used:
            return ch
    for cp in range(0x10000, 0x10800):  # pragma: no cover - absurd input
        ch = chr(cp)
        if ch not in used:
            return ch
    raise InvalidRoomPattern("cannot allocate a sentinel character")  # pragma: no cover


def _shared_alphabet(patterns: Iterable[RoomPattern]) -> list[str] | None:
    """The finite alphabet a product search over these patterns can use.

    Returns `None` when no trustworthy alphabet exists, which both callers treat
    as "cannot decide, report nothing". Every character no pattern mentions
    behaves identically — it can only be consumed by `?`, `*` or a negated class —
    so one sentinel stands for all of them.

    Two refusals:

    * The set is too large to be worth expanding.
    * Some ordered pair of its characters **composes under NFC**. The product
      search concatenates alphabet characters into candidate witnesses, but
      `matches()` compares NFC-normalised names — so a witness NFC would rewrite
      is not a string any room name can be, and answers derived from it describe a
      different string. `"e" + U+0301` folds to `é`, and Hangul `U+1100 + U+1161`
      folds to `U+AC00`: two characters becoming one, where the automaton counted
      two. Refusing the alphabet keeps the remaining searches exact instead of
      occasionally wrong. It costs nothing in practice — both platforms build room
      names as slugs, so a pattern only reaches here with a composable pair if one
      was written deliberately.
    """
    used: set[str] = set()
    for p in patterns:
        used |= p._alphabet_members()
    if len(used) > _MAX_CLASS_MEMBERS:
        return None
    # NFC composition is a property of *adjacent pairs*, not of single characters,
    # so checking `unicodedata.combining()` per character was incomplete: Hangul
    # Jamo U+1100 and U+1161 both report a combining class of zero yet compose to
    # U+AC00 — one character where the automaton counted two. Canonical composition
    # only ever joins a starter with the character following it, so an alphabet
    # whose every ordered pair is NFC-stable cannot assemble a witness that NFC
    # would rewrite, and the search over it is exact.
    #
    # This must PREVENT bogus witnesses rather than detect them afterwards.
    # Detecting one and bailing out would be pointless: for both callers "found a
    # witness" and "cannot decide" happen to be the same return value, so a
    # post-hoc check could not change any answer while looking like it did.
    ordered = sorted(used)
    if len(ordered) * len(ordered) > _MAX_PAIR_CHECKS:
        return None
    for a in ordered:
        for b in ordered:
            if unicodedata.normalize("NFC", a + b) != a + b:
                return None
    return ordered + [_sentinel(used)]


def union_intersects(
    left: Iterable[RoomPattern], right: Iterable[RoomPattern]
) -> bool:
    """Is there any room name matched by some `left` pattern *and* some `right` one?

    Used to catch an `exclude` pattern that cannot overlap the rule's `include`
    union, which is dead config that reads like protection: excluding a room the
    include never matched does **not** keep a later rule from claiming it.

    The same product search as `union_subsumes` with the acceptance test flipped
    — look for a string both sides accept, rather than one only the inner side
    accepts. On hitting the bound this returns `True`, the opposite direction to
    `union_subsumes`, because both defaults mean "do not report anything": here a
    claimed intersection is the non-finding.

    The automaton walks raw code points while `matches()` compares NFC-normalised
    names, so the two disagree when a metacharacter can straddle a base character
    and a combining mark — the search would offer `"e" + U+0301` as a shared
    witness of `e?` and `?́`, while matching folds that to the single character
    `é`, which neither pattern accepts. `_shared_alphabet` refuses to build an
    alphabet in that case, so the answer here becomes the conservative one rather
    than a wrong one.
    """
    left = list(left)
    right = list(right)
    if not left or not right:
        return False

    alphabet = _shared_alphabet((*left, *right))
    if alphabet is None:
        return True  # cannot decide; do not report

    start = (tuple(p._start() for p in left), tuple(p._start() for p in right))
    seen = {start}
    queue = [start]
    while queue:
        if len(seen) > _MAX_PRODUCT_STATES:
            return True  # cannot decide; do not report
        l_states, r_states = queue.pop()
        if any(p._accepts(s) for p, s in zip(left, l_states)) and any(
            p._accepts(s) for p, s in zip(right, r_states)
        ):
            return True
        for ch in alphabet:
            nxt = (
                tuple(p._step(s, ch) for p, s in zip(left, l_states)),
                tuple(p._step(s, ch) for p, s in zip(right, r_states)),
            )
            # Either side dying means no suffix can produce a shared witness.
            if not any(nxt[0]) or not any(nxt[1]):
                continue
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return False


def union_subsumes(
    outer: Iterable[RoomPattern], inner: Iterable[RoomPattern]
) -> bool:
    """Does every name matched by *any* `inner` pattern also match some `outer`?

    This is the shadowing question: `outer` is an earlier rule's include list,
    `inner` a later rule's. Under first-match precedence, a later rule whose
    whole language is already claimed can never fire.

    Decided exactly, by searching the product of both sides' automata for a name
    the inner side accepts and the outer side rejects. The search is bounded; on
    hitting the bound this returns `False`, which understates shadowing rather
    than inventing it — a missed warning is a cosmetic loss, a false one sends an
    operator hunting a rule that works.
    """
    outer = list(outer)
    inner = list(inner)
    if not inner:
        return True  # an empty language is contained in anything
    if not outer:
        return False

    alphabet = _shared_alphabet((*outer, *inner))
    if alphabet is None:
        return False  # cannot decide; do not report

    start = (
        tuple(p._start() for p in outer),
        tuple(p._start() for p in inner),
    )
    seen = {start}
    queue = [start]
    while queue:
        if len(seen) > _MAX_PRODUCT_STATES:
            return False
        o_states, i_states = queue.pop()
        inner_accepts = any(p._accepts(s) for p, s in zip(inner, i_states))
        outer_accepts = any(p._accepts(s) for p, s in zip(outer, o_states))
        if inner_accepts and not outer_accepts:
            return False  # witness found
        for ch in alphabet:
            nxt = (
                tuple(p._step(s, ch) for p, s in zip(outer, o_states)),
                tuple(p._step(s, ch) for p, s in zip(inner, i_states)),
            )
            # Once the inner side is dead no suffix can produce a witness.
            if not any(nxt[1]):
                continue
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return True


def is_direct_room_name(room: str) -> bool:
    """Whether a statically configured room name addresses a direct message.

    `@someone` is the convention both connectors implement — Rocket.Chat's and
    Mattermost's `resolve_room()` each branch on the `@` prefix and open a direct
    channel — and `gateway/config.py` already relies on it when deriving a watcher name.
    Stated here so a caller that needs to *reason* about DMs (who owns an account's DM
    stream, §4.5) does not have to restate a convention it cannot see.

    Not a pattern-language concern, but this module is where room-name meaning lives,
    and the alternative was a fourth copy of `startswith("@")`.
    """
    return room.startswith("@")
