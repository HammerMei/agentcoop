"""Tests for watcher rules: the rule shape, its parser, and shadow detection.

Covers `gateway.core.watcher_rule` and the rule half of `gateway.config`. The
static (`room:`/`rooms: [...]`) parser is deliberately untouched by this work and
is covered by `test_config_loading.py`; the two shapes coexist until the watcher
manager lands, so a few tests here assert that coexistence explicitly.

Run with:
    uv run python -m pytest tests/unit/test_watcher_rule.py -v
"""

from __future__ import annotations

import unittest
from pathlib import Path

from gateway.config import (
    _parse_one_watcher_rule,
    find_shadowed_rules,
)
from gateway.core.config import AgentConfig, ConnectorConfig
from gateway.core.room_pattern import RoomPattern
from gateway.core.watcher_rule import RoomKind, RoomMatcher, RuleMatch, WatcherRule

CONNECTORS = [
    ConnectorConfig(name="mm-home", type="mattermost", raw={}),
    ConnectorConfig(name="rc-home", type="rocketchat", raw={}),
]
CONNECTOR_NAMES = {c.name for c in CONNECTORS}
AGENTS = {"claude-eng": AgentConfig(name="claude-eng"), "claude-ops": AgentConfig(name="claude-ops")}


def parse(entry, *, index=0, templates=None, seen=None) -> WatcherRule:
    return _parse_one_watcher_rule(
        entry,
        index,
        connectors=CONNECTORS,
        connector_names=CONNECTOR_NAMES,
        agents=AGENTS,
        config_dir=Path("/tmp"),
        templates=templates or {},
        seen_rule_names=seen if seen is not None else set(),
    )


def rule(name="r", connector="mm-home", **rooms) -> WatcherRule:
    return WatcherRule(
        name=name,
        connector=connector,
        agent="claude-eng",
        rooms=RoomMatcher(
            include=tuple(RoomPattern(p) for p in rooms.get("include", ())),
            except_for=tuple(RoomPattern(p) for p in rooms.get("except_for", ())),
            direct=rooms.get("direct", False),
            group_direct=rooms.get("group_direct", False),
        ),
    )


MINIMAL = {"name": "eng", "connector": "mm-home", "agent": "claude-eng",
           "rooms": {"include": ["eng-*"]}}



# `TestShapeDiscrimination` lived here and tested `entry_is_watcher_rule`, which
# decided whether a `watchers:` entry was a rule or the removed static shape by
# looking for a `rooms:` MAPPING. Both are gone: entries live under
# `watcher_rules:` now, so the block they are in settles the question, and an old
# `watchers:` key is reported as an unknown top-level key. Removing that test is
# the point of the change rather than collateral — the sniffing is what stopped
# `rooms` being inheritable from a template.


class TestMatcherSemantics(unittest.TestCase):
    def test_include_claims(self):
        m = RoomMatcher(include=(RoomPattern("eng-*"),))
        self.assertIs(m.match("eng-backend", RoomKind.CHANNEL), RuleMatch.CLAIMED)

    def test_no_include_match_falls_through(self):
        m = RoomMatcher(include=(RoomPattern("eng-*"),))
        self.assertIs(m.match("ops-x", RoomKind.CHANNEL), RuleMatch.NO_MATCH)

    def test_except_for_declines_rather_than_falling_through(self):
        """§2.1's real decision: an excluded room does NOT reach a later rule.
        Fall-through would make except_for a routing operator and let two rules
        contend for the same room."""
        m = RoomMatcher(
            include=(RoomPattern("eng-*"),), except_for=(RoomPattern("eng-archive"),)
        )
        self.assertIs(m.match("eng-archive", RoomKind.CHANNEL), RuleMatch.DECLINED)
        self.assertIsNot(m.match("eng-archive", RoomKind.CHANNEL), RuleMatch.NO_MATCH)

    def test_groups_are_matched_by_name_like_channels(self):
        m = RoomMatcher(include=(RoomPattern("eng-*"),))
        self.assertIs(m.match("eng-secret", RoomKind.GROUP), RuleMatch.CLAIMED)

    def test_dms_need_the_opt_in_and_ignore_patterns(self):
        by_name = RoomMatcher(include=(RoomPattern("*"),))
        self.assertIs(by_name.match("anything", RoomKind.DM), RuleMatch.NO_MATCH)
        self.assertIs(RoomMatcher(direct=True).match("", RoomKind.DM), RuleMatch.CLAIMED)

    def test_group_dm_is_a_separate_opt_in_from_dm(self):
        """§6.4: require_mention is skipped for a 1:1 DM but must not be for a
        group DM, so `direct` must not imply `group_direct`."""
        self.assertIs(
            RoomMatcher(direct=True).match("", RoomKind.GROUP_DM), RuleMatch.NO_MATCH
        )
        self.assertIs(
            RoomMatcher(group_direct=True).match("", RoomKind.GROUP_DM),
            RuleMatch.CLAIMED,
        )
        self.assertIs(
            RoomMatcher(group_direct=True).match("", RoomKind.DM), RuleMatch.NO_MATCH
        )

    def test_except_for_is_not_consulted_for_dms(self):
        m = RoomMatcher(
            include=(RoomPattern("eng-*"),), except_for=(RoomPattern("*"),), direct=True
        )
        self.assertIs(m.match("", RoomKind.DM), RuleMatch.CLAIMED)

    def test_patterns_and_dm_opt_in_coexist_on_one_rule(self):
        m = RoomMatcher(include=(RoomPattern("eng-*"),), direct=True)
        self.assertIs(m.match("eng-x", RoomKind.CHANNEL), RuleMatch.CLAIMED)
        self.assertIs(m.match("", RoomKind.DM), RuleMatch.CLAIMED)
        self.assertIs(m.match("ops-x", RoomKind.CHANNEL), RuleMatch.NO_MATCH)

    def test_room_kind_is_direct_helper(self):
        self.assertTrue(RoomKind.DM.is_direct)
        self.assertTrue(RoomKind.GROUP_DM.is_direct)
        self.assertFalse(RoomKind.CHANNEL.is_direct)
        self.assertFalse(RoomKind.GROUP.is_direct)


class TestParserHappyPath(unittest.TestCase):
    def test_minimal_rule(self):
        r = parse(MINIMAL)
        self.assertEqual(r.name, "eng")
        self.assertEqual(r.connector, "mm-home")
        self.assertEqual(r.agent, "claude-eng")  # the entry's own
        self.assertIs(r.match("eng-x", RoomKind.CHANNEL), RuleMatch.CLAIMED)

    def test_name_is_stripped(self):
        self.assertEqual(parse({**MINIMAL, "name": "  eng  "}).name, "eng")

    def test_a_rule_must_name_its_connector(self):
        """Was `test_connector_defaults_to_the_first`. A rule with no connector
        used to take `connectors[0]` — the first in the file — which is the same
        silent, document-order binding the agent fallback made, and this
        module's own resolver docstring already called it worse than a crash."""
        entry = {"name": "eng", "agent": "claude-eng", "rooms": {"include": ["eng-*"]}}
        with self.assertRaises(ValueError) as cm:
            parse(entry)
        msg = str(cm.exception)
        self.assertIn("has no 'connector' set", msg)
        self.assertIn("mm-home", msg, "the choices are named")

    def test_explicit_agent(self):
        self.assertEqual(parse({**MINIMAL, "agent": "claude-ops"}).agent, "claude-ops")

    def test_dm_only_rule_needs_no_include(self):
        r = parse({"name": "dms", "connector": "mm-home", "agent": "claude-eng",
                   "rooms": {"direct": True}})
        self.assertIs(r.match("", RoomKind.DM), RuleMatch.CLAIMED)

    def test_ttls_are_read_and_live_on_the_rule(self):
        r = parse({**MINIMAL, "session_idle_days": 7, "session_expire_days": 30})
        self.assertEqual((r.session_idle_days, r.session_expire_days), (7, 30))

    def test_omitted_ttls_take_the_dataclass_defaults(self):
        """Inverted (Codex review of #121): this used to pin None — 'no
        lifecycle' — which was the defect, not the contract. §2.5's ruling is
        15/15 by default, and the parser passing its None explicitly was
        exactly how the defaults got silently overridden."""
        r = parse(MINIMAL)
        self.assertEqual(r.session_idle_days, 15)
        self.assertEqual(r.session_expire_days, 15)

    def test_history_handoff_is_read(self):
        r = parse({**MINIMAL, "history_handoff": {"fetch_count": 5}})
        self.assertEqual(r.history_handoff.fetch_count, 5)

    def test_the_retired_notification_fields_are_refused(self):
        """Removed 2026-08-02 by owner decision (platform presence is the
        signal; idle/wake cycles made per-transition announcements noise),
        executed with the runtime cutover. A config still carrying them must
        fail loudly, per the same breaking-change precedent as the static
        shape."""
        for field in ("online_notification", "offline_notification"):
            with self.subTest(field=field):
                with self.assertRaises(ValueError) as cm:
                    parse({**MINIMAL, field: "hi"})
                self.assertIn("unknown key", str(cm.exception))

    def test_inherits_supplies_shared_fields(self):
        """A template is how several rules share one agent — the only way, now
        that the implicit `default_agent:` fallback is gone. The entry omits
        `agent` entirely, which would be a hard error without the template."""
        entry = {k: v for k, v in MINIMAL.items() if k != "agent"}
        templates = {"base": {"agent": "claude-ops", "session_idle_days": 3}}
        r = parse({**entry, "inherits": "base"}, templates=templates)
        self.assertEqual(r.agent, "claude-ops")
        self.assertEqual(r.session_idle_days, 3)

    def test_the_entrys_own_fields_win_over_the_template(self):
        templates = {"base": {"agent": "claude-ops"}}
        r = parse({**MINIMAL, "inherits": "base", "agent": "claude-eng"}, templates=templates)
        self.assertEqual(r.agent, "claude-eng")


class TestParserHardErrors(unittest.TestCase):
    def _err(self, entry, needle, **kw):
        with self.assertRaises(ValueError) as cm:
            parse(entry, **kw)
        self.assertIn(needle, str(cm.exception))
        return str(cm.exception)

    def test_missing_name_is_rejected_with_a_reason(self):
        msg = self._err({"connector": "mm-home", "rooms": {"include": ["a"]}}, "'name' is required")
        self.assertIn("no single room to derive a name from", msg)

    def test_blank_or_non_string_name(self):
        for bad in ("", "   ", 3, ["x"], None):
            with self.subTest(bad=bad):
                self._err({**MINIMAL, "name": bad}, "'name' is required")

    def test_duplicate_rule_name(self):
        seen = set()
        parse(MINIMAL, seen=seen)
        self._err(MINIMAL, "Duplicate watcher rule name 'eng'", seen=seen)

    def test_a_successful_parse_registers_the_name(self):
        seen: set[str] = set()
        parse(MINIMAL, seen=seen)
        self.assertEqual(seen, {"eng"})

    def test_a_leftover_room_key_is_an_unknown_key(self):
        """`room` had a dedicated message until it stopped being a key at all.
        That message also claimed the key "cannot be combined with a 'rooms:'
        block" even for an entry that had no such block."""
        msg = self._err({**MINIMAL, "room": "general"}, "unknown key(s)")
        self.assertIn("'room'", msg)

    def test_session_id_is_rejected_as_an_unknown_key(self):
        """No dedicated branch any more: the rule shape is a closed key set, so a
        removed field is reported the same way a typo is. See
        test_session_id_removed.py for why that replaced a hand-written message."""
        msg = self._err({**MINIMAL, "session_id": "abc"}, "unknown key(s)")
        self.assertIn("session_id", msg)

    def test_unknown_key_inside_rooms(self):
        msg = self._err({**MINIMAL, "rooms": {"include": ["a"], "directt": True}}, "unknown key")
        self.assertIn("directt", msg)

    def test_rooms_must_be_a_mapping_when_parsed_directly(self):
        self._err({"name": "x", "rooms": ["a"]}, "'rooms' must be a mapping")

    def test_empty_include_with_no_dm_opt_in_can_never_match(self):
        self._err({"name": "x", "rooms": {}}, "can never match any room")

    def test_include_must_be_a_list_not_a_bare_string(self):
        self._err({**MINIMAL, "rooms": {"include": "eng-*"}}, "must be a list")

    def test_include_entries_must_be_non_empty_strings(self):
        for bad in ([""], [3], [None]):
            with self.subTest(bad=bad):
                self._err({**MINIMAL, "rooms": {"include": bad}}, "non-empty strings")

    def test_duplicate_pattern(self):
        self._err({**MINIMAL, "rooms": {"include": ["a", "a"]}}, "duplicate pattern")

    def test_invalid_pattern_is_reported_at_load_with_the_pattern_quoted(self):
        msg = self._err({**MINIMAL, "rooms": {"include": ["eng-[a"]}}, "is not valid")
        self.assertIn("eng-[a", msg)

    def test_except_for_without_include_is_a_no_op_and_refused(self):
        self._err(
            {"name": "x", "rooms": {"except_for": ["a"], "direct": True}},
            "no effect without",
        )

    def test_an_except_for_that_cannot_overlap_the_include_is_refused(self):
        """The footgun: `except_for: [ops-secret]` next to `include: [eng-*]` reads
        like protection and does nothing at all, because a name the include
        misses is NO_MATCH and falls through to the next rule."""
        msg = self._err(
            {**MINIMAL, "rooms": {"include": ["eng-*"], "except_for": ["ops-secret"]}},
            "does nothing here",
        )
        # The message has to teach three things, or the operator fixes the
        # pattern and still does not get what they wanted: that except_for is
        # relative to this rule's include, that it does not protect a room from
        # later rules, and what does.
        # Pinned as MEANING, not as sentences: the wording was rewritten for
        # plain language and these assertions held it to the old phrasing —
        # including the `*later*` asterisks. What must survive a rewrite is that
        # all three points are still made.
        self.assertIn("this rule's own 'include'", msg)
        self.assertIn("later rule", msg)
        self.assertIn("BOTH 'include' and 'except_for'", msg)

    def test_a_partially_overlapping_except_for_is_fine(self):
        r = parse({**MINIMAL, "rooms": {"include": ["eng-*"], "except_for": ["eng-archive"]}})
        self.assertIs(r.match("eng-backend", RoomKind.CHANNEL), RuleMatch.CLAIMED)
        self.assertIs(r.match("eng-archive", RoomKind.CHANNEL), RuleMatch.DECLINED)

    def test_only_the_offending_pattern_is_named(self):
        msg = self._err(
            {
                **MINIMAL,
                "rooms": {"include": ["eng-*"], "except_for": ["eng-old", "ops-x"]},
            },
            "'ops-x'",
        )
        self.assertNotIn("'eng-old'", msg)

    def test_dm_flags_reject_the_object_form_for_now(self):
        msg = self._err({**MINIMAL, "rooms": {"direct": {"include": ["*"]}}}, "does not yet")
        self.assertIn("planned extension", msg)

    def test_dm_flags_must_be_booleans(self):
        self._err({**MINIMAL, "rooms": {"direct": "yes"}}, "must be true or false")

    def test_unknown_connector(self):
        self._err({**MINIMAL, "connector": "nope"}, "unknown connector")

    def test_non_string_connector(self):
        self._err({**MINIMAL, "connector": ["mm-home"]}, "must be a string")

    def test_unknown_agent(self):
        self._err({**MINIMAL, "agent": "nope"}, "unknown agent")

    def test_ttl_must_be_a_positive_int(self):
        for bad in (0, -1):
            with self.subTest(bad=bad):
                self._err({**MINIMAL, "session_idle_days": bad}, "positive integer")

    def test_ttl_rejects_bool_which_is_an_int_subclass(self):
        self._err({**MINIMAL, "session_idle_days": True}, "positive integer")

    def test_an_explicit_null_ttl_takes_the_default_by_contract(self):
        """Codex round 13, resolved as documentation: null is the documented
        template-inheritance SUPPRESS (`_deep_merge` keeps an explicit null
        as a value, so a child rule writes `session_expire_days: null` to
        undo a template's value and return to the 15-day default). It is not
        a disable switch — that reading was the F1 defect's side effect. This
        pin is the contract: null == omitted == the dataclass default."""
        rule = parse({**MINIMAL, "session_idle_days": None,
                      "session_expire_days": None})
        self.assertEqual(rule.session_idle_days, 15)
        self.assertEqual(rule.session_expire_days, 15)

    def test_the_two_legs_need_no_ordering_between_them(self):
        """Inverted deliberately: the old rule required idle < expire, on the
        assumption that both were measured from the moment the room went quiet.
        They are not (§2.5) — expiry is measured from the moment the watcher
        becomes *idle*, so they are two independent legs of a sequence and the
        default is 15/15, which the old rule would have rejected outright."""
        for idle, expire in ((15, 15), (30, 30), (31, 30)):
            with self.subTest(idle=idle, expire=expire):
                rule = parse({**MINIMAL, "session_idle_days": idle,
                              "session_expire_days": expire})
                self.assertEqual(rule.session_idle_days, idle)
                self.assertEqual(rule.session_expire_days, expire)

    def test_a_non_positive_lifetime_is_still_refused(self):
        """The ordering rule went; this one stays, and it is `_parse_rule_ttl`'s
        — checked here so removing the ordering rule cannot be read as removing
        every constraint on these fields."""
        for field in ("session_idle_days", "session_expire_days"):
            for bad in (0, -1):
                with self.subTest(field=field, value=bad):
                    self._err({**MINIMAL, field: bad}, "must be a positive integer")

    def test_entry_must_be_a_mapping(self):
        self._err(["not", "a", "mapping"], "must be a mapping")


class TestParserScalarValidation(unittest.TestCase):
    """Malformed scalars must be load errors, not silent defaults.

    Both of these are copies of checks in the static parser, fixed separately
    there because that copy is reachable in a released version and this one is
    not yet."""

    def _err(self, entry, needle):
        with self.assertRaises(ValueError) as cm:
            parse(entry)
        self.assertIn(needle, str(cm.exception))
        return str(cm.exception)

    def test_a_falsy_non_string_connector_is_rejected_not_defaulted(self):
        """These skipped a truthiness-guarded type check and then bound the rule
        to connectors[0] — silently, to the wrong account."""
        for bad in (False, 0, [], {}):
            with self.subTest(bad=bad):
                self._err({**MINIMAL, "connector": bad}, "'connector' must be a string")

    def test_null_connector_is_refused_like_an_absent_one(self):
        """`connector:` is legal in a watcher_templates entry, so explicit null is
        how a rule declines an inherited one. Declining the only source now leaves
        the rule with no connector at all, and there is nothing to fall back to —
        so it is the same error as omitting the key, not a way to reach a
        default. (This test used to assert the opposite.)"""
        with self.assertRaises(ValueError) as cm:
            parse({**MINIMAL, "connector": None})
        self.assertIn("has no 'connector' set", str(cm.exception))

    def test_a_non_mapping_history_handoff_is_rejected(self):
        for bad in (True, [1], "yes"):
            with self.subTest(bad=bad):
                msg = self._err({**MINIMAL, "history_handoff": bad},
                                "'history_handoff' must be a mapping")
                self.assertIn("enabled: false", msg)

    def test_history_handoff_false_does_not_silently_enable_it(self):
        """`false` used to be replaced by {}, which means the defaults — and
        enabled defaults to True, so it turned the feature on."""
        self._err({**MINIMAL, "history_handoff": False}, "must be a mapping")

    def test_null_history_handoff_uses_defaults(self):
        self.assertTrue(parse({**MINIMAL, "history_handoff": None}).history_handoff.enabled)


class TestUnknownRuleKeysAreRejected(unittest.TestCase):
    """The schema sets additionalProperties: false, but `coop config validate` runs
    collect_config() rather than the schema — so without this check a typo is
    silently ignored and the rule quietly lacks whatever it was meant to set."""

    def test_a_typo_in_a_ttl_key_is_rejected(self):
        with self.assertRaises(ValueError) as cm:
            parse({**MINIMAL, "session_expire_day": 30})  # missing the 's'
        self.assertIn("session_expire_day", str(cm.exception))

    def test_the_message_lists_the_valid_keys(self):
        with self.assertRaises(ValueError) as cm:
            parse({**MINIMAL, "bogus": 1})
        self.assertIn("session_expire_days", str(cm.exception))

    def test_every_documented_key_is_accepted(self):
        entry = {
            "name": "eng", "connector": "mm-home", "agent": "claude-eng",
            "description": "x", "rooms": {"include": ["eng-*"]},
            "session_idle_days": 7, "session_expire_days": 30,
            "context_inject_files": [], "history_handoff": {"enabled": False},
        }
        self.assertEqual(parse(entry).name, "eng")

    def test_the_key_set_matches_the_json_schema(self):
        """Pins the promise made in WATCHER_RULE_KEYS' comment: the loader and the
        schema must accept the same keys, since only one of them runs in each
        path."""
        import json

        from gateway.config import WATCHER_RULE_KEYS
        schema = json.loads(
            (Path(__file__).parents[2] / "gateway/schema/config.schema.json").read_text()
        )
        self.assertEqual(
            set(schema["$defs"]["watcherRule"]["properties"]), set(WATCHER_RULE_KEYS)
        )


class TestContextInjectFilesValidation(unittest.TestCase):
    def test_a_bare_string_is_rejected_rather_than_split_per_character(self):
        """Unvalidated, `context_inject_files: foo` became three paths ending /f,
        /o, /o — because _resolve_paths iterates whatever it is given."""
        with self.assertRaises(ValueError) as cm:
            parse({**MINIMAL, "context_inject_files": "foo"})
        self.assertIn("one character at a time", str(cm.exception))

    def test_a_non_string_element_is_a_value_error_not_a_type_error(self):
        """A TypeError would escape collect_config()'s except ValueError and abort
        the whole validation pass."""
        with self.assertRaises(ValueError):
            parse({**MINIMAL, "context_inject_files": ["ok", 3]})

    def test_an_empty_element_is_skipped_not_rejected(self):
        """Documents the shared implementation's behaviour rather than the stricter
        one this test originally asserted.

        `_resolve_paths` skips empty entries, which is how the static path has
        always behaved and is therefore released behaviour. Tightening it to an
        error would change that path too, so it belongs in its own change rather
        than riding along here — noted because an empty entry is most likely a
        typo, and silently dropping it is the same quiet family as the bugs this
        branch has been fixing."""
        r = parse({**MINIMAL, "context_inject_files": ["", "a.md"]})
        self.assertEqual(len(r.context_inject_files), 1)

    def test_a_proper_list_is_resolved(self):
        r = parse({**MINIMAL, "context_inject_files": ["a.md"]})
        self.assertTrue(r.context_inject_files[0].endswith("a.md"))


class TestEveryRuleFieldIsTypeChecked(unittest.TestCase):
    """The systematic guard, added because the review kept finding fields one at a
    time.

    Four separate rounds each found another field the parser read and copied
    without checking its type -- connector, history_handoff, context_inject_files,
    then the notifications. Fixing them individually left no reason to believe the
    next one was covered. This enumerates every key in WATCHER_RULE_KEYS and
    requires each to reject a value of the wrong type, so a field added later
    without validation fails here rather than in a fifth review round."""

    # A wrong-typed value per field, and the fields where any type is acceptable.
    WRONG_VALUE = {
        "name": [],
        "connector": False,
        "agent": 3,
        "rooms": ["eng-*"],
        "session_idle_days": "seven",
        "session_expire_days": [30],
        "context_inject_files": "notes.md",
        "history_handoff": True,
    }
    # `description` is annotation only and never read; `inherits` is resolved
    # before this parser sees the entry, and has its own tests.
    NOT_READ_HERE = {"description", "inherits"}

    def test_the_table_covers_every_declared_key(self):
        from gateway.config import WATCHER_RULE_KEYS

        self.assertEqual(
            set(self.WRONG_VALUE) | self.NOT_READ_HERE,
            set(WATCHER_RULE_KEYS),
            "a rule field was added without a wrong-type case here",
        )

    def test_each_field_rejects_a_wrong_type(self):
        for field, bad in self.WRONG_VALUE.items():
            with self.subTest(field=field):
                with self.assertRaises(ValueError, msg=f"{field} accepted {bad!r}"):
                    parse({**MINIMAL, field: bad})

    def test_history_handoff_inner_fields_are_checked_too(self):
        """The outer mapping being valid is not enough: `enable: false` -- one
        letter short -- was silently ignored, leaving handoff ENABLED, which is the
        same inversion `history_handoff: false` used to produce."""
        for bad in ({"enable": False}, {"enabled": "false"}, {"fetch_count": -1},
                    {"fetch_count": True}, {"verbatim_tail": "10"}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse({**MINIMAL, "history_handoff": bad})

    def test_valid_history_handoff_still_works(self):
        r = parse({**MINIMAL, "history_handoff": {"enabled": False, "fetch_count": 5}})
        self.assertFalse(r.history_handoff.enabled)
        self.assertEqual(r.history_handoff.fetch_count, 5)

    def test_a_non_string_yaml_key_does_not_crash_the_error_path(self):
        """`1: value` made the unknown-key formatter raise TypeError out of the
        error path, escaping collect_config()'s `except ValueError`."""
        with self.assertRaises(ValueError):
            parse({**MINIMAL, 1: "value"})
        with self.assertRaises(ValueError):
            parse({**MINIMAL, "rooms": {"include": ["a"], 2: "x"}})

    def test_a_failed_rule_does_not_reserve_its_name(self):
        """The static parser stages names for exactly this reason. With the name
        registered first, a rule that then failed left it taken, and a later valid
        rule was rejected as a duplicate of something that was never returned."""
        seen: set[str] = set()
        with self.assertRaises(ValueError):
            parse({**MINIMAL, "context_inject_files": "oops"}, seen=seen)
        self.assertEqual(seen, set(), "the failed rule's name was left registered")
        # The same name must still be usable afterwards.
        self.assertEqual(parse(MINIMAL, seen=seen).name, "eng")


class TestTheDenyIdiom(unittest.TestCase):
    """Including a room and excluding it is how you blackhole it.

    It is the design's only way to express "no rule may claim this room": the
    rule claims the room, declines it, and DECLINED does not fall through. Worth
    pinning as intended behaviour rather than leaving it to look like a
    contradiction someone should "fix"."""

    def test_the_same_pattern_in_both_lists_is_accepted(self):
        r = parse({"name": "block", "agent": "claude-eng", "connector": "mm-home",
                   "rooms": {"include": ["eng-old"], "except_for": ["eng-old"]}})
        self.assertIs(r.match("eng-old", RoomKind.CHANNEL), RuleMatch.DECLINED)

    def test_it_blocks_later_rules_which_a_plain_omission_would_not(self):
        blocker = parse(
            {"name": "block", "agent": "claude-eng", "connector": "mm-home",
             "rooms": {"include": ["eng-old"], "except_for": ["eng-old"]}},
            seen=set(),
        )
        catchall = parse({"name": "all", "agent": "claude-eng", "connector": "mm-home",
                          "rooms": {"include": ["eng-*"]}}, seen=set())

        def route(room: str) -> str:
            for rule_ in (blocker, catchall):
                outcome = rule_.match(room, RoomKind.CHANNEL)
                if outcome is not RuleMatch.NO_MATCH:
                    return f"{rule_.name}:{outcome.name}"
            return "unrouted"

        self.assertEqual(route("eng-old"), "block:DECLINED")
        self.assertEqual(route("eng-new"), "all:CLAIMED")

    def test_a_broader_deny_pattern_works_the_same_way(self):
        r = parse({"name": "block", "agent": "claude-eng", "connector": "mm-home",
                   "rooms": {"include": ["tmp-*"], "except_for": ["tmp-*"]}})
        self.assertIs(r.match("tmp-anything", RoomKind.CHANNEL), RuleMatch.DECLINED)


class TestShadowDetection(unittest.TestCase):
    def _found(self, rules):
        return [(f.rule.name, f.shadowed_by.name, f.scope) for f in find_shadowed_rules(rules)]

    def test_a_later_narrower_rule_is_shadowed(self):
        a, b = rule("broad", include=["eng-*"]), rule("narrow", include=["eng-backend"])
        self.assertEqual(self._found([a, b]), [("narrow", "broad", "rule")])

    def test_order_matters(self):
        a, b = rule("broad", include=["eng-*"]), rule("narrow", include=["eng-backend"])
        self.assertEqual(self._found([b, a]), [])

    def test_disjoint_rules_are_fine(self):
        self.assertEqual(
            self._found([rule("a", include=["eng-*"]), rule("b", include=["ops-*"])]), []
        )

    def test_different_connectors_never_shadow(self):
        a = rule("a", connector="mm-home", include=["*"])
        b = rule("b", connector="rc-home", include=["eng-*"])
        self.assertEqual(self._found([a, b]), [])

    def test_star_shadows_everything_after_it(self):
        rules = [rule("a", include=["*"]), rule("b", include=["eng-*"]), rule("c", include=["ops-?"])]
        self.assertEqual(self._found(rules), [("b", "a", "rule"), ("c", "a", "rule")])

    def test_no_rules_no_findings(self):
        self.assertEqual(find_shadowed_rules([]), [])

    def test_a_shadow_formed_only_by_a_union_is_not_reported(self):
        """A documented, deliberate gap. `[ab]*` really is covered by `a*` with
        `b*`, and `union_subsumes` can prove it, but comparing one earlier rule at
        a time keeps each warning pointed at a single rule someone can go and
        read."""
        rules = [rule("a", include=["a*"]), rule("b", include=["b*"]), rule("c", include=["[ab]*"])]
        self.assertEqual(self._found(rules), [])

    def test_but_a_single_earlier_rule_that_covers_it_is_reported(self):
        a, c = rule("a", include=["[ab]*"]), rule("c", include=["a*"])
        self.assertEqual(self._found([a, c]), [("c", "a", "rule")])


class TestAnEarlierRulesBlockingLanguageIsItsInclude(unittest.TestCase):
    """The subtle one, and the earlier implementation had it backwards.

    `except_for` yields DECLINED, which halts routing rather than falling through
    — so a room an earlier rule declines never reaches a later rule either. An
    earlier rule therefore blocks everything its `include` matches, whether it
    goes on to claim or decline it, and its own `except_for` has no bearing on
    what it shadows."""

    def _found(self, rules):
        return [(f.rule.name, f.shadowed_by.name, f.scope) for f in find_shadowed_rules(rules)]

    def test_a_declined_room_still_shadows_a_later_rule_for_it(self):
        deny = rule("deny", include=["eng-*"], except_for=["eng-archive"])
        later = rule("later", include=["eng-archive"])
        self.assertEqual(self._found([deny, later]), [("later", "deny", "rule")])

    def test_the_deny_idiom_shadows_completely(self):
        """Its whole purpose is to stop later rules, so saying so is correct."""
        blocker = rule("block", include=["tmp-*"], except_for=["tmp-*"])
        catchall = rule("all", include=["tmp-x"])
        self.assertEqual(self._found([blocker, catchall]), [("all", "block", "rule")])

    def test_an_except_for_on_the_earlier_rule_does_not_narrow_what_it_shadows(self):
        """Same blocker name both times, so the only difference under test is
        whether the earlier rule carries an except_for."""
        later = rule("b", include=["eng-backend"])
        with_exc = self._found(
            [rule("a", include=["eng-*"], except_for=["eng-archive"]), later]
        )
        without_exc = self._found([rule("a", include=["eng-*"]), later])
        self.assertEqual(with_exc, without_exc)
        self.assertEqual(with_exc, [("b", "a", "rule")])

    def test_a_rule_shadowed_despite_its_own_except_for(self):
        a = rule("a", include=["eng-*"])
        b = rule("b", include=["eng-*"], except_for=["eng-archive"])
        self.assertEqual(self._found([a, b]), [("b", "a", "rule")])


class TestBlockerAttributionIsNotCollapsedWhenBlockersDiffer(unittest.TestCase):
    """A whole-rule finding names one earlier rule as responsible, so it may only
    be emitted when one earlier rule really does take every reach.

    Collapsing when *different* rules block different reaches would attribute the
    finding to a rule that does not claim the others — the warning would be true
    but its suggested remedy wrong."""

    def _found(self, rules):
        return sorted(
            (f.rule.name, f.shadowed_by.name, f.scope) for f in find_shadowed_rules(rules)
        )

    def test_separate_blockers_stay_separate(self):
        named_only = rule("named", include=["eng-*"])
        dm_only = rule("dms", direct=True)
        hybrid = rule("hybrid", include=["eng-x"], direct=True)
        self.assertEqual(
            self._found([named_only, dm_only, hybrid]),
            [("hybrid", "dms", "direct"), ("hybrid", "named", "named")],
        )

    def test_one_blocker_taking_everything_still_collapses(self):
        both = rule("both", include=["*"], direct=True)
        hybrid = rule("hybrid", include=["eng-x"], direct=True)
        self.assertEqual(self._found([both, hybrid]), [("hybrid", "both", "rule")])


class TestDmReachIsReportedIndependently(unittest.TestCase):
    """§2.1 asks for a warning when an earlier rule already claimed a DM class.

    A hybrid rule can lose its DM reach while staying perfectly alive for named
    rooms, so whole-rule shadowing alone would miss it — the rule looks healthy."""

    def _found(self, rules):
        return [(f.rule.name, f.shadowed_by.name, f.scope) for f in find_shadowed_rules(rules)]

    def test_a_dead_dm_opt_in_on_an_otherwise_live_rule(self):
        a = rule("a", include=["eng-*"], direct=True)
        b = rule("b", include=["ops-*"], direct=True)  # named reach is disjoint
        self.assertEqual(self._found([a, b]), [("b", "a", "direct")])

    def test_group_direct_is_tracked_separately_from_direct(self):
        a = rule("a", direct=True)
        b = rule("b", direct=True, group_direct=True)
        self.assertEqual(self._found([a, b]), [("b", "a", "direct")])

    def test_a_named_reach_can_die_while_dms_stay_live(self):
        a = rule("a", include=["eng-*"])
        b = rule("b", include=["eng-backend"], direct=True)
        self.assertEqual(self._found([a, b]), [("b", "a", "named")])

    def test_all_reaches_dead_collapses_into_one_rule_finding(self):
        a = rule("a", include=["*"], direct=True, group_direct=True)
        b = rule("b", include=["eng-*"], direct=True, group_direct=True)
        self.assertEqual(self._found([a, b]), [("b", "a", "rule")])

    def test_a_dm_only_rule_is_not_shadowed_by_a_named_only_rule(self):
        broad = rule("broad", include=["*"])
        dms = rule("dms", direct=True)
        self.assertEqual(self._found([broad, dms]), [])

    def test_a_dm_only_rule_shadowed_by_an_earlier_dm_opt_in(self):
        self.assertEqual(
            self._found([rule("earlier", direct=True), rule("dms", direct=True)]),
            [("dms", "earlier", "rule")],
        )


class TestSharedHelpersAttributeToTheRuleParser(unittest.TestCase):
    """The messages must name the *rule* parser, not whatever last defined a helper.

    This exists because of a merge, not a code change. The static and rule parsers
    each grew their own copy of `_parse_history_handoff` / `_validated_notification`
    / `_key_list`, in different parts of the file — so merging the branches was
    textually clean and left two same-named module-level `def`s, where **the later
    one silently wins**. `ruff` did not flag it (verified: `--select F811` reports
    nothing on that file) and every existing assertion still passed, because they
    all match substrings of the message body rather than its attribution. The only
    visible symptom was a doubled prefix:

        Watcher rule at index Watcher entry at index 0: unknown key(s) ...

    So each parser now asserts its own prefix on a check that goes through a shared
    helper. A future merge that reintroduces a shadowing copy fails here instead of
    corrupting error messages silently. See the static counterpart in
    tests/unit/test_config_loading.py.

    The prefix carries the rule's NAME as well as its index (user-requested: an
    index alone makes an operator count entries in their own file to find the one
    being complained about). Asserting both keeps this file's original subject —
    which parser produced the message — and pins the naming too.
    """

    def _msg(self, entry) -> str:
        with self.assertRaises(ValueError) as cm:
            parse(entry, index=3)
        return str(cm.exception)

    def test_history_handoff_error_names_the_rule_and_index(self):
        msg = self._msg({**MINIMAL, "history_handoff": {"enable": False}})
        self.assertTrue(
            msg.startswith("Watcher rule at index 3 ('eng')"),
            f"expected a rule-parser prefix naming the rule, got: {msg}",
        )

    def test_connector_error_names_the_rule_and_index(self):
        msg = self._msg({**MINIMAL, "connector": False})
        self.assertTrue(
            msg.startswith("Watcher rule at index 3 ('eng')"),
            f"expected a rule-parser prefix naming the rule, got: {msg}",
        )

    def test_agent_error_names_the_rule_and_index(self):
        msg = self._msg({**MINIMAL, "agent": 3})
        self.assertTrue(
            msg.startswith("Watcher rule at index 3 ('eng')"),
            f"expected a rule-parser prefix naming the rule, got: {msg}",
        )


if __name__ == "__main__":
    unittest.main()


class TestOmittedTtlsGetTheDefaults(unittest.TestCase):
    """Codex review of #121, P1 — and real: the parser passed its None
    explicitly, overriding the dataclass defaults, so every config that did
    not spell the TTLs out had the whole idle/expiry lifecycle silently off.
    Pinned at the loader seam, because tests that construct WatcherRule
    directly get the defaults and can never see this."""

    def _load(self, watcher_block):
        import tempfile
        import textwrap

        from gateway.config import GatewayConfig

        cfg = (
            "connectors:\n"
            "  - name: rc\n"
            "    type: rocketchat\n"
            "    server: {url: https://x.com, username: b, password: s}\n"
            "agents:\n"
            "  default: {type: claude, working_directory: /tmp}\n"
            "watcher_rules:\n"
            + textwrap.indent(textwrap.dedent(watcher_block), "  ")
        )
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(cfg)
        return GatewayConfig.from_file(f.name).watcher_rules[0]

    def test_omitted_ttls_are_15_15(self):
        rule = self._load("- name: w1\n  agent: default\n  connector: rc\n  rooms: {include: [general]}\n")
        self.assertEqual(rule.session_idle_days, 15)
        self.assertEqual(rule.session_expire_days, 15)
        # And the frozen snapshot — what the runtime actually judges against —
        # carries the defaults too.
        from gateway.core.watcher_manager import rule_snapshot

        snap = rule_snapshot(rule)
        self.assertEqual(snap["session_idle_days"], 15)
        self.assertEqual(snap["session_expire_days"], 15)

    def test_explicit_ttls_still_win(self):
        rule = self._load(
            "- name: w1\n  agent: default\n  connector: rc\n  rooms: {include: [general]}\n"
            "  session_idle_days: 3\n  session_expire_days: 40\n")
        self.assertEqual(rule.session_idle_days, 3)
        self.assertEqual(rule.session_expire_days, 40)


class TestEveryRuleErrorNamesTheRule(unittest.TestCase):
    """A message about a rule says WHICH rule, not just its position.

    User-requested, and it was half-true: some checks built their prefix from
    the shared `where` (which gained the name) and others hand-wrote
    `f"Watcher rule at index {index}"`, so an operator got the name for a bad
    `except_for` and a bare index for a bad `rooms.direct` — in the same file, on
    the same rule. Counting entries in your own YAML to find the one being
    complained about is exactly the work an error message exists to save.

    This walks the checks rather than sampling one, because the split was
    invisible per-message: each looked fine alone. The `_parse_dm_flag` case
    below is why — unifying the prefix left it reading a `where` that was not in
    its scope, so a non-boolean `direct:` raised NameError instead of the
    message, and only an input that reached that one branch showed it.
    """

    NAME = "eng"

    BAD_ENTRIES = {
        "rooms is not a mapping": {"rooms": ["general"]},
        "unknown key in rooms": {"rooms": {"include": ["a"], "nonsense": True}},
        "include is not a list": {"rooms": {"include": 5}},
        "include holds a non-string": {"rooms": {"include": [5]}},
        "an invalid glob": {"rooms": {"include": ["eng-[a"]}},
        "direct is not a bool": {"rooms": {"include": ["a"], "direct": 3}},
        "direct in the object form": {"rooms": {"include": ["a"], "direct": {"x": 1}}},
        "group_direct is not a bool": {"rooms": {"include": ["a"], "group_direct": "yes"}},
        "a negative ttl": {"rooms": {"include": ["a"]}, "session_idle_days": -1},
        "a non-list context_inject_files": {
            "rooms": {"include": ["a"]}, "context_inject_files": "notes.md"},
        "an unknown rule key": {"rooms": {"include": ["a"]}, "typo": 1},
        "an inert except_for": {"rooms": {"include": ["eng-a"], "except_for": ["ops-b"]}},
        "a matcher that claims nothing": {"rooms": {"except_for": ["a"]}},
        "a missing connector": {"rooms": {"include": ["a"]}, "connector": None},
        "a missing agent": {"rooms": {"include": ["a"]}, "agent": None},
        "a bad history_handoff key": {
            "rooms": {"include": ["a"]}, "history_handoff": {"enable": False}},
    }

    def test_each_one_names_the_rule(self):
        for label, overrides in self.BAD_ENTRIES.items():
            with self.subTest(case=label):
                entry = {**MINIMAL, "name": self.NAME, **overrides}
                entry = {k: v for k, v in entry.items() if v is not None or k == "rooms"}
                if overrides.get("connector", "unset") is None:
                    entry.pop("connector", None)
                if overrides.get("agent", "unset") is None:
                    entry.pop("agent", None)
                with self.assertRaises(ValueError) as cm:
                    parse(entry, index=7)
                message = str(cm.exception)
                self.assertIn(
                    f"('{self.NAME}')", message,
                    f"{label}: the rule is not named — {message}",
                )
                self.assertIn("index 7", message, f"{label}: the index is gone")

    def test_a_check_running_before_the_name_exists_still_uses_the_index(self):
        """The deliberate exception: `name` itself. There is nothing to name a
        rule by yet, so those messages stay index-only rather than inventing a
        placeholder."""
        with self.assertRaises(ValueError) as cm:
            parse({"connector": "mm-home", "agent": "claude-eng",
                   "rooms": {"include": ["a"]}}, index=7)
        self.assertIn("Watcher rule at index 7:", str(cm.exception))
        self.assertIn("'name' is required", str(cm.exception))
