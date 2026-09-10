"""`WatcherLifecycle` keys its records and processors by ROOM ID (§2.3).

For the whole first release of dynamic watchers they were keyed by the watcher
HANDLE, with `record_for_room` a linear scan — so the handle was the O(1)
lookup and the room id the awkward one. That gradient is what six separate
fixes across four review rounds walked down (§2.8, "the routing rule"): code
reached for the handle where a room id was available because the handle was the
easy key.

These tests pin the invariant the re-keying introduces, the two write points
that maintain it, and the refusals. The invariant is asserted by WALKING the
dicts, not by sampling one entry, so a write that bypasses `_install` shows up
here whichever record it touched.

Run with:
    uv run python -m pytest tests/unit/test_lifecycle_keyed_by_room.py -v
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from tests.helpers import (
    evict_record,
    install_record,
    make_lifecycle,
    make_rule_derived_record,
    pop_processor,
    register_processor,
)


def _record_without_a_room(name="rc:x"):
    """`make_rule_derived_record` defaults an EMPTY room id to `room-<name>` (it
    is `room_id or ...`), so the field has to be cleared after construction —
    the first version of these tests passed a `""` and tested nothing."""
    ws = make_rule_derived_record(name)
    ws.room_id = ""
    return ws


def _assert_consistent(case, lifecycle):
    """The invariant: every key is its record's room id, every name resolves to
    exactly that key, and every processor sits under a room that has a record."""
    for room_id, ws in lifecycle._states.items():
        case.assertEqual(room_id, ws.room_id, f"{ws.watcher_name} stored under the wrong key")
        case.assertEqual(lifecycle._room_of.get(ws.watcher_name), room_id,
                         f"{ws.watcher_name} does not resolve to its room")
    case.assertEqual(len(lifecycle._room_of), len(lifecycle._states),
                     "a name that points at no record, or a record with no name")
    for room_id in lifecycle._processors:
        case.assertIn(room_id, lifecycle._states, "a processor for a room with no record")


class TestTheStoreIsKeyedByRoom(unittest.TestCase):
    def setUp(self):
        self.lc = make_lifecycle()

    def test_records_sit_under_their_room_id_and_resolve_by_name(self):
        a = install_record(self.lc, make_rule_derived_record("rc:eng", room_id="R-eng"))
        b = install_record(self.lc, make_rule_derived_record("rc:ops", room_id="R-ops"))

        _assert_consistent(self, self.lc)
        self.assertIs(self.lc.record_for_room("R-eng"), a)
        self.assertIs(self.lc.get_watcher_state("rc:ops"), b)
        self.assertEqual(set(self.lc._states), {"R-eng", "R-ops"},
                         "the keys are room ids, not handles")

    def test_record_for_room_is_a_lookup_not_a_scan(self):
        """The reason for the change. Pinned structurally: the room id is a key
        of the dict, so the answer does not depend on iteration."""
        install_record(self.lc, make_rule_derived_record("rc:eng", room_id="R-eng"))

        self.assertIn("R-eng", self.lc._states)
        self.assertIsNone(self.lc.record_for_room("R-nope"))

    def test_states_is_a_name_keyed_view_and_a_copy(self):
        """Callers iterate `.values()`, sort `.keys()` for `list`, and hand it
        to `StateStore.save`/`merged_view`, all of which speak names. It must be
        a copy, or a caller could write around `_install`."""
        a = install_record(self.lc, make_rule_derived_record("rc:eng", room_id="R-eng"))

        view = self.lc.states()

        self.assertEqual(list(view), ["rc:eng"])
        self.assertIs(view["rc:eng"], a)
        view["rc:rogue"] = make_rule_derived_record("rc:rogue", room_id="R-rogue")
        self.assertIsNone(self.lc.get_watcher_state("rc:rogue"), "the view is not the store")

    def test_uninstall_removes_the_record_and_the_name(self):
        install_record(self.lc, make_rule_derived_record("rc:eng", room_id="R-eng"))

        self.lc._uninstall("rc:eng")

        _assert_consistent(self, self.lc)
        self.assertIsNone(self.lc.record_for_room("R-eng"))
        self.assertIsNone(self.lc.get_watcher_state("rc:eng"))
        self.assertIsNone(self.lc._uninstall("rc:eng"), "idempotent")

    def test_a_room_re_installed_under_a_new_name_drops_the_old_name(self):
        """A rename surfaced through recreation: the room keeps its record slot,
        the old handle must stop resolving — otherwise the operator's `pause
        rc:old` would still act on a room now called something else."""
        install_record(self.lc, make_rule_derived_record("rc:old", room_id="R-1"))

        new = install_record(self.lc, make_rule_derived_record("rc:new", room_id="R-1"))

        _assert_consistent(self, self.lc)
        self.assertIs(self.lc.record_for_room("R-1"), new)
        self.assertIs(self.lc.get_watcher_state("rc:new"), new)
        self.assertIsNone(self.lc.get_watcher_state("rc:old"))


class TestTheRefusals(unittest.TestCase):
    def setUp(self):
        self.lc = make_lifecycle()

    def test_a_record_with_no_room_id_cannot_be_installed(self):
        """It could never be recreated, and `""` as a key would make every such
        record collide on one slot."""
        with self.assertRaises(ValueError):
            self.lc._install(_record_without_a_room())
        _assert_consistent(self, self.lc)

    def test_a_name_is_not_re_pointed_at_a_different_room(self):
        """`start_watcher_in_room` refuses this at step 0.5; the store refuses
        it too so no other caller can silently swap an operator's name onto
        another room's session, watermark and pause flag."""
        install_record(self.lc, make_rule_derived_record("rc:eng", room_id="R-1"))

        with self.assertRaises(RuntimeError):
            self.lc._install(make_rule_derived_record("rc:eng", room_id="R-2"))
        self.assertIs(self.lc.get_watcher_state("rc:eng").room_id, "R-1")
        _assert_consistent(self, self.lc)

    def test_a_processor_needs_a_record_first(self):
        with self.assertRaises(RuntimeError):
            register_processor(self.lc, "rc:ghost", MagicMock())

    def test_the_fixture_helper_refuses_a_key_that_is_not_the_records_name(self):
        """Nine tests used to write `_states["w1"] = record`; a key disagreeing
        with the record's own name was a state nothing could find. The helper
        that replaced them makes that loud."""
        with self.assertRaises(AssertionError):
            install_record(self.lc, make_rule_derived_record("rc:eng", room_id="R-1"),
                           as_name="rc:other")


class TestProcessorsFollowTheRecord(unittest.TestCase):
    def setUp(self):
        self.lc = make_lifecycle()

    def test_registered_by_name_stored_by_room_found_by_name(self):
        install_record(self.lc, make_rule_derived_record("rc:eng", room_id="R-eng"))
        proc = register_processor(self.lc, "rc:eng", MagicMock())

        _assert_consistent(self, self.lc)
        self.assertIs(self.lc.get_processor("rc:eng"), proc)
        self.assertIs(self.lc._processors["R-eng"], proc, "stored under the room")

    def test_pop_by_name_and_evict_clears_both(self):
        install_record(self.lc, make_rule_derived_record("rc:eng", room_id="R-eng"),
                       processor=MagicMock())

        self.assertIsNotNone(pop_processor(self.lc, "rc:eng"))
        self.assertIsNone(self.lc.get_processor("rc:eng"))
        register_processor(self.lc, "rc:eng", MagicMock())
        evict_record(self.lc, "rc:eng")

        _assert_consistent(self, self.lc)
        self.assertEqual((self.lc._states, self.lc._processors, self.lc._room_of), ({}, {}, {}))


class TestHydrationAtStartup(unittest.TestCase):
    """`_hydrate` is `_install` for the replay: it skips and says why, never
    raises, and leaves the bad record on disk (the store merges disk with memory
    and prunes only what it is told to)."""

    def setUp(self):
        self.lc = make_lifecycle()

    def test_a_good_record_is_installed(self):
        ws = make_rule_derived_record("rc:eng", room_id="R-eng")
        self.assertTrue(self.lc._hydrate(ws))
        self.assertIs(self.lc.record_for_room("R-eng"), ws)

    def test_a_record_with_no_room_is_skipped_with_a_warning(self):
        with self.assertLogs("coop.core.watcher_lifecycle", "WARNING") as cm:
            ok = self.lc._hydrate(_record_without_a_room())

        self.assertFalse(ok)
        self.assertIn("no room id", "\n".join(cm.output))
        _assert_consistent(self, self.lc)

    def test_two_records_for_one_room_keep_the_first_and_log_an_error(self):
        """Sticky binding (§2.4) says one record per room. Two on disk is a
        corrupt file; the first loaded wins and the operator is told."""
        first = make_rule_derived_record("rc:a", room_id="R-1")
        second = make_rule_derived_record("rc:b", room_id="R-1")
        self.lc._hydrate(first)

        with self.assertLogs("coop.core.watcher_lifecycle", "ERROR") as cm:
            ok = self.lc._hydrate(second)

        self.assertFalse(ok)
        self.assertIs(self.lc.record_for_room("R-1"), first)
        self.assertIsNone(self.lc.get_watcher_state("rc:b"))
        self.assertIn("already belongs to watcher 'rc:a'", "\n".join(cm.output))
        _assert_consistent(self, self.lc)
