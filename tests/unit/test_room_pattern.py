"""Tests for the watcher room-pattern glob engine.

Two things get exhaustive treatment rather than examples, because both are
places where a subtle bug would be invisible in hand-written cases:

* **Matching** is differentially tested against an independent regex
  translation, over every pattern and every subject string up to a small length
  on a small alphabet.
* **Subsumption** is checked against brute force — enumerate the strings, find a
  witness the hard way, and require the automaton to agree.

The regex oracle deliberately covers only literals, `*` and `?`, where the
translation is trivially correct (`*` → `.*`, `?` → `.`, everything else
escaped). Character classes are covered by explicit cases instead, so that a
mistake in a hand-written oracle cannot masquerade as a bug in the engine.

Run with:
    uv run python -m pytest tests/unit/test_room_pattern.py -v
"""

from __future__ import annotations

import itertools
import re
import unittest

from gateway.core.room_pattern import (
    InvalidRoomPattern,
    RoomPattern,
    normalize_room_name,
    union_intersects,
    union_subsumes,
)


def _oracle_matches(pattern: str, name: str) -> bool:
    """Independent implementation, for literals/`*`/`?` only."""
    out = ["\\A"]
    for ch in pattern:
        if ch == "*":
            out.append(".*")
        elif ch == "?":
            out.append(".")
        else:
            out.append(re.escape(ch))
    out.append("\\Z")
    return re.match("".join(out), name, flags=re.DOTALL) is not None


class TestParsingRejectsInvalidPatterns(unittest.TestCase):
    def test_empty_pattern(self):
        with self.assertRaises(InvalidRoomPattern):
            RoomPattern("")

    def test_non_string(self):
        with self.assertRaises(InvalidRoomPattern):
            RoomPattern(None)  # type: ignore[arg-type]

    def test_unterminated_class(self):
        for p in ("[abc", "eng-[a", "[", "[!"):
            with self.subTest(p=p), self.assertRaises(InvalidRoomPattern):
                RoomPattern(p)

    def test_empty_class(self):
        with self.assertRaises(InvalidRoomPattern):
            RoomPattern("a[]b")

    def test_reversed_range(self):
        with self.assertRaises(InvalidRoomPattern):
            RoomPattern("[z-a]")

    def test_absurdly_wide_range_is_refused_not_expanded(self):
        """Guards against quietly building a 65k-member set."""
        with self.assertRaises(InvalidRoomPattern):
            RoomPattern("[\u0020-\uffff]")

    def test_a_valid_pattern_with_every_construct_parses(self):
        RoomPattern("eng-*-?[abc][!x]")


class TestMatchingBasics(unittest.TestCase):
    def test_literal_is_anchored_at_both_ends(self):
        p = RoomPattern("eng")
        self.assertTrue(p.matches("eng"))
        self.assertFalse(p.matches("eng-x"))
        self.assertFalse(p.matches("x-eng"))
        self.assertFalse(p.matches("engineering"))

    def test_star_matches_an_empty_run(self):
        self.assertTrue(RoomPattern("eng-*").matches("eng-"))
        self.assertTrue(RoomPattern("*").matches(""))

    def test_star_spans_separators(self):
        """Unlike a path glob, `*` has no special case for any character."""
        self.assertTrue(RoomPattern("a*b").matches("a-/.b"))

    def test_question_mark_is_exactly_one_character(self):
        p = RoomPattern("eng-?")
        self.assertTrue(p.matches("eng-a"))
        self.assertFalse(p.matches("eng-"))
        self.assertFalse(p.matches("eng-ab"))

    def test_character_class(self):
        p = RoomPattern("eng-[abc]")
        self.assertTrue(p.matches("eng-b"))
        self.assertFalse(p.matches("eng-d"))

    def test_character_range(self):
        p = RoomPattern("v[0-9]")
        self.assertTrue(p.matches("v7"))
        self.assertFalse(p.matches("va"))

    def test_negated_class_both_spellings(self):
        for spelling in ("[!abc]", "[^abc]"):
            with self.subTest(spelling=spelling):
                p = RoomPattern(f"eng-{spelling}")
                self.assertFalse(p.matches("eng-a"))
                self.assertTrue(p.matches("eng-z"))

    def test_closing_bracket_first_is_a_literal(self):
        p = RoomPattern("[]]")
        self.assertTrue(p.matches("]"))

    def test_a_literal_open_bracket_is_reachable_via_a_class(self):
        """There is no escape character, so this is the documented way to match
        a literal `[`."""
        p = RoomPattern("[[]")
        self.assertTrue(p.matches("["))
        self.assertFalse(p.matches("]"))

    def test_backslash_is_an_ordinary_literal_not_an_escape(self):
        """Documented consequence of the closed syntax: `\\*` matches a
        backslash followed by anything, not a literal asterisk."""
        self.assertTrue(RoomPattern("a\\b").matches("a\\b"))
        self.assertTrue(RoomPattern("a\\*").matches("a\\xyz"))
        self.assertFalse(RoomPattern("a\\*").matches("a*"))

    def test_repeated_stars_collapse_without_changing_the_language(self):
        self.assertEqual(RoomPattern("a**b"), RoomPattern("a*b"))
        self.assertTrue(RoomPattern("a***b").matches("axxb"))

    def test_matching_is_case_sensitive(self):
        """§2.1: case sensitive — both platforms' slugs are lowercase."""
        self.assertFalse(RoomPattern("eng-*").matches("ENG-x"))
        self.assertFalse(RoomPattern("ENG").matches("eng"))


class TestUnicodeNormalisation(unittest.TestCase):
    """§2.1: compared NFC-normalised, so a decomposed pattern matches a
    composed name."""

    COMPOSED = "café"  # café with U+00E9
    DECOMPOSED = "café"  # café with e + combining acute

    def test_the_two_forms_are_genuinely_different_strings(self):
        self.assertNotEqual(self.COMPOSED, self.DECOMPOSED)

    def test_decomposed_pattern_matches_composed_name(self):
        self.assertTrue(RoomPattern(self.DECOMPOSED).matches(self.COMPOSED))

    def test_composed_pattern_matches_decomposed_name(self):
        self.assertTrue(RoomPattern(self.COMPOSED).matches(self.DECOMPOSED))

    def test_normalize_helper_is_idempotent(self):
        once = normalize_room_name(self.DECOMPOSED)
        self.assertEqual(once, normalize_room_name(once))

    def test_question_mark_counts_composed_characters(self):
        """After NFC the accented character is one code point, so `?` matches
        it — the decomposed form would have counted two."""
        self.assertTrue(RoomPattern("caf?").matches(self.DECOMPOSED))


class TestIsLiteral(unittest.TestCase):
    def test_plain_names_are_literal(self):
        self.assertTrue(RoomPattern("eng").is_literal)
        self.assertTrue(RoomPattern("eng-backend").is_literal)

    def test_metacharacters_are_not(self):
        for p in ("eng-*", "eng-?", "eng-[ab]"):
            with self.subTest(p=p):
                self.assertFalse(RoomPattern(p).is_literal)


class TestMatchingAgainstAnIndependentOracle(unittest.TestCase):
    """Exhaustive differential test over literals, `*` and `?`."""

    ALPHABET = "ab"
    PATTERN_CHARS = "ab*?"

    MAX_PATTERN = 5
    MAX_SUBJECT = 5

    def test_every_small_pattern_against_every_small_string(self):
        subjects = [
            "".join(s)
            for n in range(0, self.MAX_SUBJECT + 1)
            for s in itertools.product(self.ALPHABET, repeat=n)
        ]
        patterns = [
            "".join(t)
            for n in range(1, self.MAX_PATTERN + 1)
            for t in itertools.product(self.PATTERN_CHARS, repeat=n)
        ]
        checked = 0
        for pattern in patterns:
            compiled = RoomPattern(pattern)
            for subject in subjects:
                got = compiled.matches(subject)
                want = _oracle_matches(pattern, subject)
                if got != want:
                    self.fail(
                        f"pattern={pattern!r} subject={subject!r}: "
                        f"engine={got} oracle={want}"
                    )
                checked += 1

        # An exact count, so that a sweep which silently stops enumerating fails
        # here instead of passing vacuously.
        #   patterns: 4 + 4^2 + 4^3 + 4^4 + 4^5           = 1364
        #   subjects: 1 + 2 + 2^2 + 2^3 + 2^4 + 2^5       =   63
        expected_patterns = sum(len(self.PATTERN_CHARS) ** n for n in range(1, self.MAX_PATTERN + 1))
        expected_subjects = sum(len(self.ALPHABET) ** n for n in range(0, self.MAX_SUBJECT + 1))
        self.assertEqual(len(patterns), expected_patterns)
        self.assertEqual(len(subjects), expected_subjects)
        self.assertEqual(checked, expected_patterns * expected_subjects)


class TestNfcComposablePairsAreRefusedRatherThanMisanswered(unittest.TestCase):
    """The automaton walks raw code points; `matches()` compares NFC-normalised
    names. They disagree when a metacharacter straddles a base character and a
    combining mark, so no alphabet is built and both functions fall back to their
    "report nothing" answer instead of a wrong one."""

    ACUTE = "\u0301"

    def test_intersection_declines_to_answer(self):
        # "e" + combining acute would be a shared witness on raw code points, but
        # NFC folds it to a single character that neither pattern accepts.
        got = union_intersects([RoomPattern("e?")], [RoomPattern("?" + self.ACUTE)])
        self.assertTrue(got, "must return the do-not-report value, not a wrong False")

    def test_subsumption_declines_to_answer(self):
        got = union_subsumes([RoomPattern("e?")], [RoomPattern("?" + self.ACUTE)])
        self.assertFalse(got, "must return the do-not-report value")

    def test_patterns_without_combining_marks_are_still_decided_exactly(self):
        self.assertFalse(union_intersects([RoomPattern("eng-*")], [RoomPattern("ops-*")]))
        self.assertTrue(union_intersects([RoomPattern("eng-*")], [RoomPattern("*-backend")]))

    def test_hangul_jamo_are_refused_too_though_their_combining_class_is_zero(self):
        """The first version of this guard checked unicodedata.combining() per
        character and missed these: both report zero yet U+1100 + U+1161 compose to
        U+AC00. Composition is a property of adjacent pairs, not of characters."""
        import unicodedata
        self.assertEqual(unicodedata.combining("\u1100"), 0)
        self.assertEqual(unicodedata.combining("\u1161"), 0)
        self.assertEqual(unicodedata.normalize("NFC", "\u1100\u1161"), "\uac00")
        self.assertTrue(
            union_intersects([RoomPattern("\u1100?")], [RoomPattern("?\u1161")]),
            "must return the do-not-report value, not a wrong False",
        )

    def test_a_composed_accent_is_fine_because_it_is_one_character(self):
        """Only a *standalone* combining mark trips the guard — a normal accented
        name composes to a single character at compile time."""
        self.assertTrue(union_intersects([RoomPattern("caf\u00e9")], [RoomPattern("caf?")]))


class TestSubsumptionKnownCases(unittest.TestCase):
    def _subsumes(self, outer: list[str], inner: list[str]) -> bool:
        return union_subsumes(
            [RoomPattern(p) for p in outer], [RoomPattern(p) for p in inner]
        )

    def test_star_subsumes_everything(self):
        self.assertTrue(self._subsumes(["*"], ["eng-*"]))
        self.assertTrue(self._subsumes(["*"], ["anything", "at-*", "all-?"]))

    def test_a_pattern_subsumes_itself(self):
        self.assertTrue(self._subsumes(["eng-*"], ["eng-*"]))

    def test_broader_prefix_subsumes_narrower(self):
        self.assertTrue(self._subsumes(["eng-*"], ["eng-backend-*"]))
        self.assertTrue(self._subsumes(["eng-*"], ["eng-backend"]))

    def test_narrower_does_not_subsume_broader(self):
        self.assertFalse(self._subsumes(["eng-backend-*"], ["eng-*"]))

    def test_disjoint_patterns_do_not_subsume(self):
        self.assertFalse(self._subsumes(["eng-*"], ["ops-*"]))

    def test_question_mark_is_subsumed_by_star_but_not_the_reverse(self):
        self.assertTrue(self._subsumes(["eng-*"], ["eng-?"]))
        self.assertFalse(self._subsumes(["eng-?"], ["eng-*"]))

    def test_a_union_can_subsume_what_no_single_member_does(self):
        """The exactness that matters: `a*` and `b*` together cover `?*`,
        though neither does alone."""
        self.assertTrue(self._subsumes(["a*", "b*"], ["[ab]*"]))
        self.assertFalse(self._subsumes(["a*"], ["[ab]*"]))

    def test_class_subsumes_its_members(self):
        self.assertTrue(self._subsumes(["v[0-9]"], ["v7"]))
        self.assertFalse(self._subsumes(["v[0-8]"], ["v9"]))

    def test_negated_class_subsumption(self):
        self.assertTrue(self._subsumes(["x[!a]"], ["xb"]))
        self.assertFalse(self._subsumes(["x[!a]"], ["xa"]))

    def test_empty_inner_is_vacuously_subsumed(self):
        self.assertTrue(self._subsumes(["eng-*"], []))

    def test_empty_outer_subsumes_nothing(self):
        self.assertFalse(self._subsumes([], ["eng-*"]))

    def test_witness_longer_than_the_shorter_pattern(self):
        """`a*` vs `a?` — the witness is "a", shorter; and `a?` vs `a*` needs
        the empty suffix. Both directions exercised."""
        self.assertFalse(self._subsumes(["a?"], ["a*"]))
        self.assertTrue(self._subsumes(["a*"], ["a?"]))


class TestIntersection(unittest.TestCase):
    def _hits(self, left: list[str], right: list[str]) -> bool:
        return union_intersects(
            [RoomPattern(p) for p in left], [RoomPattern(p) for p in right]
        )

    def test_identical_patterns_intersect(self):
        self.assertTrue(self._hits(["eng"], ["eng"]))

    def test_disjoint_literals_do_not(self):
        self.assertFalse(self._hits(["eng"], ["ops"]))

    def test_a_star_intersects_anything(self):
        self.assertTrue(self._hits(["*"], ["eng-backend"]))

    def test_overlapping_prefixes(self):
        self.assertTrue(self._hits(["eng-*"], ["*-backend"]))

    def test_disjoint_prefixes(self):
        self.assertFalse(self._hits(["eng-*"], ["ops-*"]))

    def test_length_mismatch_makes_them_disjoint(self):
        self.assertFalse(self._hits(["??"], ["???"]))

    def test_classes(self):
        self.assertTrue(self._hits(["v[0-9]"], ["v7"]))
        self.assertFalse(self._hits(["v[0-8]"], ["v9"]))

    def test_empty_side_never_intersects(self):
        self.assertFalse(self._hits([], ["eng"]))
        self.assertFalse(self._hits(["eng"], []))

    def test_union_on_either_side(self):
        self.assertTrue(self._hits(["a*", "b*"], ["b-x"]))
        self.assertFalse(self._hits(["a*", "b*"], ["c-x"]))

    def test_it_is_symmetric(self):
        for left, right in ((["eng-*"], ["*-backend"]), (["a"], ["b"]), (["?"], ["ab"])):
            with self.subTest(left=left, right=right):
                self.assertEqual(self._hits(left, right), self._hits(right, left))


class TestIntersectionAgainstBruteForce(unittest.TestCase):
    """The one algorithmic addition gets the same treatment as the first: find
    the shared witness the hard way and require agreement.

    Both directions are asserted here, unlike subsumption. A witness found means
    the engine must say True; and for patterns this short, no witness up to
    length 6 means there is none, so the engine must say False."""

    ALPHABET = "ab"
    MAX_SUBJECT = 6

    def test_all_pattern_pairs_up_to_length_three(self):
        chars = "ab*?"
        patterns = [
            "".join(t) for n in range(1, 4) for t in itertools.product(chars, repeat=n)
        ]
        subjects = [
            "".join(s)
            for n in range(0, self.MAX_SUBJECT + 1)
            for s in itertools.product(self.ALPHABET, repeat=n)
        ]
        compiled = {p: RoomPattern(p) for p in patterns}
        pairs = 0
        for a, b in itertools.product(patterns, repeat=2):
            pa, pb = compiled[a], compiled[b]
            witness = next(
                (s for s in subjects if pa.matches(s) and pb.matches(s)), None
            )
            claimed = union_intersects([pa], [pb])
            if (witness is not None) != claimed:
                self.fail(
                    f"left={a!r} right={b!r}: engine={claimed} but brute force "
                    f"{'found ' + repr(witness) if witness else 'found nothing'}"
                )
            pairs += 1
        self.assertEqual(pairs, len(patterns) ** 2)


class TestSubsumptionAgainstBruteForce(unittest.TestCase):
    """Exhaustive: enumerate strings, find witnesses the hard way.

    A brute-force sweep bounded at length M can only *find* witnesses, never
    prove their absence, so the assertions are directional:

    * a witness found ⇒ `union_subsumes` must be False
    * `union_subsumes` True ⇒ no witness may exist within the bound

    Those are the same implication, and it is the one that catches a wrongly
    optimistic answer — the dangerous direction, since it would suppress a
    warning about a rule that really is reachable.
    """

    ALPHABET = "ab"
    MAX_SUBJECT = 6

    def _subjects(self) -> list[str]:
        return [
            "".join(s)
            for n in range(0, self.MAX_SUBJECT + 1)
            for s in itertools.product(self.ALPHABET, repeat=n)
        ]

    def test_all_pattern_pairs_up_to_length_three(self):
        chars = "ab*?"
        patterns = [
            "".join(t)
            for n in range(1, 4)
            for t in itertools.product(chars, repeat=n)
        ]
        subjects = self._subjects()
        compiled = {p: RoomPattern(p) for p in patterns}
        pairs = 0
        for a, b in itertools.product(patterns, repeat=2):
            pa, pb = compiled[a], compiled[b]
            witness = next(
                (s for s in subjects if pb.matches(s) and not pa.matches(s)), None
            )
            claimed = union_subsumes([pa], [pb])
            if witness is not None and claimed:
                self.fail(
                    f"outer={a!r} inner={b!r}: claimed subsumed, but {witness!r} "
                    f"matches the inner pattern and not the outer one"
                )
            pairs += 1
        self.assertGreater(pairs, 3_000, "the pair sweep did not run")

    def test_union_pairs_against_brute_force(self):
        """Two-pattern outer unions, where exactness is harder to get right."""
        singles = ["a*", "b*", "?", "a?", "[ab]", "[ab]*", "aa", "b", "*"]
        subjects = self._subjects()
        compiled = {p: RoomPattern(p) for p in singles}
        checked = 0
        for o1, o2, inner in itertools.product(singles, repeat=3):
            outer = [compiled[o1], compiled[o2]]
            pi = compiled[inner]
            witness = next(
                (
                    s
                    for s in subjects
                    if pi.matches(s) and not any(p.matches(s) for p in outer)
                ),
                None,
            )
            claimed = union_subsumes(outer, [pi])
            if witness is not None and claimed:
                self.fail(
                    f"outer={[o1, o2]!r} inner={inner!r}: claimed subsumed, but "
                    f"{witness!r} escapes the union"
                )
            checked += 1
        self.assertGreater(checked, 500, "the union sweep did not run")

    def test_no_witness_within_bound_implies_the_engine_agrees(self):
        """The other direction, on patterns short enough that any witness must
        be short: if brute force finds nothing, the engine must say subsumed."""
        chars = "ab*"
        patterns = [
            "".join(t) for n in range(1, 4) for t in itertools.product(chars, repeat=n)
        ]
        subjects = self._subjects()
        compiled = {p: RoomPattern(p) for p in patterns}
        for a, b in itertools.product(patterns, repeat=2):
            pa, pb = compiled[a], compiled[b]
            witness = next(
                (s for s in subjects if pb.matches(s) and not pa.matches(s)), None
            )
            if witness is None:
                self.assertTrue(
                    union_subsumes([pa], [pb]),
                    f"outer={a!r} inner={b!r}: no witness up to length "
                    f"{self.MAX_SUBJECT}, but the engine denied subsumption",
                )


if __name__ == "__main__":
    unittest.main()


class TestCanonicalSpelling(unittest.TestCase):
    """`canonical()` is the spelling `==` compares by, deterministic across
    processes — what a config fingerprint must hash (#144)."""

    def test_equal_patterns_spell_the_same(self):
        self.assertEqual(RoomPattern("eng-**").canonical(), RoomPattern("eng-*").canonical())
        self.assertEqual(RoomPattern("eng-*"), RoomPattern("eng-**"))

    def test_class_members_are_ordered(self):
        self.assertEqual(RoomPattern("x[cba]").canonical(), RoomPattern("x[abc]").canonical())
        self.assertEqual(RoomPattern("x[!ba]").canonical(), "x[!61,62]")

    def test_a_member_bang_is_not_read_as_negation(self):
        self.assertNotEqual(RoomPattern("[a!]"), RoomPattern("[!a]"))
        self.assertNotEqual(RoomPattern("[a!]").canonical(), RoomPattern("[!a]").canonical())

    def test_a_member_bracket_does_not_collide_with_a_literal_one(self):
        self.assertNotEqual(RoomPattern("[]=]"), RoomPattern("[=]]"))
        self.assertNotEqual(RoomPattern("[]=]").canonical(), RoomPattern("[=]]").canonical())

    def test_raw_is_kept_for_display(self):
        self.assertEqual(RoomPattern("eng-**").raw, "eng-**")
