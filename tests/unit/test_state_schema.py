"""The persisted state format: round-trip, and the refusal to read a legacy one.

Design §5.3 asks for both, and for a specific reason in each case.

**Round-trip.** This on-disk surface had no serialization test at all, while being
the only thing standing between a restart and a lost session. Every field has to be
written twice — once on the dataclass, once in the reader — so the coupling test below
walks the dataclass and requires each field to survive a save/load cycle. `config` and
`rule` are nested rather than scalar, so nesting and the empty case are covered
explicitly rather than by presence alone.

**Refusal.** A legacy record cannot be converted: it carries no agent, no materialized
config and no originating rule, so a converter would have to guess which rule now owns
the room — the silent re-binding the design exists to prevent. The alternative to
refusing is booting with an empty registry, which abandons every session *and looks
like a successful start*. That is why the check cannot live inside `load_state`'s
`except`: the old code caught every exception and logged "starting fresh", so a
refusal raised in the wrong place would wear exactly the costume it is meant to
replace.

`tests/helpers.py` patches `load_state`/`save_state` at module scope, so these tests
deliberately do not inherit from `IsolatedTestCase` — they drive the real functions
against a temporary `RUNTIME_DIR`.

Run with:
    uv run python -m pytest tests/unit/test_state_schema.py -v
"""

from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import patch

from gateway.core import state as state_mod
from gateway.core.state import (
    STATE_FORMAT_VERSION,
    FutureStateError,
    LegacyStateError,
    StateFormatError,
    WatcherState,
    check_state_formats,
    load_state,
    save_state,
    state_files,
)


class _RealStateFileTestCase(unittest.TestCase):
    """Drives the real load/save against a throwaway RUNTIME_DIR."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self._tmp.name)
        patcher = patch.object(state_mod, "RUNTIME_DIR", self.runtime)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def state_path(self, connector: str = "rc") -> Path:
        return self.runtime / f"state.{connector}.json"

    def write_raw(self, payload: dict, connector: str = "rc") -> Path:
        path = self.state_path(connector)
        path.write_text(json.dumps(payload))
        return path


class TestRoundTrip(_RealStateFileTestCase):
    def _full_record(self) -> WatcherState:
        """Every field set to something distinguishable from its default."""
        return WatcherState(
            watcher_name="rc-eng",
            session_id="ses_1",
            room_id="room_1",
            room_type="group",
            context_injected=True,
            paused=True,
            last_processed_ts="2026-08-13T10:00:00Z",
            room_name="eng-backend",
            room_kind="group_dm",
            participants=["alice", "bob"],
            connector="rc",
            agent="claude-eng",
            backend_identity="claude:/srv/work",
            created_at="2026-08-01T00:00:00Z",
            last_activity_at="2026-08-13T09:59:00Z",
            dropped_at="2026-08-13T09:00:00Z",
            config={"room": "eng-backend", "history_handoff": {"enabled": False}},
            rule_name="eng-rooms",
            rule={"name": "eng-rooms", "rooms": {"include": ["eng-*"]}},
            config_schema_version=1,
        )

    def test_every_field_survives_a_save_and_load(self):
        """The coupling test: walks the dataclass rather than a hand-listed set, so a
        field added without a reader entry fails here instead of silently loading as
        its default on every restart."""
        original = self._full_record()
        save_state("rc", [original])
        (restored,) = load_state("rc")
        for f in dataclasses.fields(WatcherState):
            with self.subTest(field=f.name):
                self.assertEqual(
                    getattr(restored, f.name),
                    getattr(original, f.name),
                    f"'{f.name}' did not survive the round trip — is it missing from "
                    "load_state's reader?",
                )

    def test_every_field_differs_from_its_default(self):
        """Guards the test above: a field whose "distinguishable" value happens to
        equal the default would pass the round trip even if the reader ignored it."""
        record = self._full_record()
        defaults = WatcherState(watcher_name="x", session_id="", room_id="")
        for f in dataclasses.fields(WatcherState):
            if f.name in ("watcher_name",):
                continue
            with self.subTest(field=f.name):
                self.assertNotEqual(
                    getattr(record, f.name),
                    getattr(defaults, f.name),
                    f"'{f.name}' is set to its own default in the fixture, so the "
                    "round-trip assertion for it proves nothing",
                )

    def test_a_minimal_record_round_trips_with_empty_nested_fields(self):
        """The empty case, which is what the static path actually writes today."""
        original = WatcherState(watcher_name="rc-general", session_id="", room_id="r1")
        save_state("rc", [original])
        (restored,) = load_state("rc")
        self.assertEqual(restored, original)
        self.assertEqual(restored.config, {})
        self.assertEqual(restored.rule, {})
        self.assertEqual(restored.participants, [])

    def test_nested_structures_survive_more_than_one_level(self):
        original = WatcherState(
            watcher_name="w", session_id="", room_id="r",
            config={"history_handoff": {"enabled": True, "fetch_count": 5},
                    "context_inject_files": ["/a.md", "/b.md"]},
            rule={"rooms": {"include": ["a-*"], "except_for": ["a-old"],
                            "direct": True}},
        )
        save_state("rc", [original])
        (restored,) = load_state("rc")
        self.assertEqual(restored.config["history_handoff"]["fetch_count"], 5)
        self.assertEqual(restored.config["context_inject_files"], ["/a.md", "/b.md"])
        self.assertEqual(restored.rule["rooms"]["except_for"], ["a-old"])
        self.assertIs(restored.rule["rooms"]["direct"], True)

    def test_the_reader_copies_nested_values_rather_than_aliasing_them(self):
        """Tested against `_record_from_dict` directly, not through `load_state`.

        Going through the file cannot observe this: `json.loads` builds fresh objects
        on every call, so two loads never share structure no matter what the reader
        does. A file-level version of this test passes even when the reader aliases —
        it was written that way first, and proved nothing. The property that is real,
        and is what `dict(...)`/`list(...)` in the reader buy, is that a record does
        not alias the payload it was built from.
        """
        from gateway.core.state import _record_from_dict

        payload = {
            "watcher_name": "w",
            "config": {"a": 1},
            "rule": {"rooms": {"include": ["a-*"]}},
            "participants": ["x"],
        }
        record = _record_from_dict(payload)
        record.config["a"] = 2
        record.participants.append("y")
        self.assertEqual(payload["config"], {"a": 1}, "config aliased the payload")
        self.assertEqual(payload["participants"], ["x"], "participants aliased")

    def test_several_records_round_trip_in_order(self):
        records = [
            WatcherState(watcher_name=f"w{i}", session_id=f"s{i}", room_id=f"r{i}")
            for i in range(3)
        ]
        save_state("rc", records)
        self.assertEqual(load_state("rc"), records)

    def test_saving_writes_the_version_marker(self):
        save_state("rc", [])
        payload = json.loads(self.state_path().read_text())
        self.assertEqual(payload["version"], STATE_FORMAT_VERSION)

    def test_a_missing_file_is_not_an_error(self):
        self.assertEqual(load_state("never-written"), [])


    def test_a_round_trip_through_save_is_accepted(self):
        """The obvious one, stated because it is what makes the refusal safe: files
        this build writes are readable by it."""
        save_state("rc", [WatcherState(watcher_name="w", session_id="s", room_id="r")])
        self.assertEqual(len(load_state("rc")), 1)

class TestLegacyRefusal(_RealStateFileTestCase):
    LEGACY_NEW_STYLE = {
        "watchers": [{
            "watcher_name": "rc-general",
            "session_id": "ses_old",
            "room_id": "room_1",
            "room_type": "channel",
            "context_injected": True,
            "paused": False,
            "last_processed_ts": "2026-01-01T00:00:00Z",
        }]
    }
    LEGACY_WATCHER_ID = {
        "watchers": [{
            "watcher_id": "abcdef123456",
            "room_name": "general",
            "session_id": "ses_older",
            "room_id": "room_1",
        }]
    }

    def test_an_unversioned_file_is_refused(self):
        """Both historical shapes are refused, not just the oldest one. §5.3's own
        account of what a legacy record carries — watcher_name, session_id, room_id,
        room_type, context_injected, paused, last_processed_ts — describes the
        *previous current* format, so the check is a file-level version marker rather
        than record-shape sniffing."""
        for label, payload in (
            ("previous current format", self.LEGACY_NEW_STYLE),
            ("watcher_id era", self.LEGACY_WATCHER_ID),
        ):
            with self.subTest(format=label):
                self.write_raw(payload)
                with self.assertRaises(LegacyStateError):
                    load_state("rc")

    def test_the_refusal_is_not_swallowed_into_starting_fresh(self):
        """The specific regression this guards: `load_state` used to wrap everything
        in `except Exception` and return `[]` with a "starting fresh" log. A refusal
        raised inside that would have been indistinguishable from a clean first boot,
        which is the outcome §5.3 calls out — every session abandoned, and the boot
        looks successful."""
        self.write_raw(self.LEGACY_NEW_STYLE)
        with self.assertRaises(LegacyStateError):
            load_state("rc")
        # And nothing was silently returned instead.
        try:
            result = load_state("rc")
        except LegacyStateError:
            result = "raised"
        self.assertEqual(result, "raised")

    def test_the_message_names_the_file_and_where_to_look(self):
        path = self.write_raw(self.LEGACY_NEW_STYLE)
        with self.assertRaises(LegacyStateError) as cm:
            load_state("rc")
        msg = str(cm.exception)
        self.assertIn(str(path), msg)
        self.assertIn("§5.3", msg)
        self.assertIn("no automatic conversion", msg)

    def test_a_future_version_is_refused_too(self):
        """Not only older files: a version this build does not know cannot be read
        either, and guessing would be the same mistake in the other direction."""
        self.write_raw({"version": STATE_FORMAT_VERSION + 1, "watchers": []})
        with self.assertRaises(FutureStateError):
            load_state("rc")

    def test_a_future_file_is_not_told_to_be_deleted(self):
        """The two directions need opposite advice, and one message cannot carry both.

        The legacy message says to delete the file. Following that on a *newer* file —
        during a rollback, say — destroys valid sessions and still leaves this build
        unable to read them. So the future case is its own exception with its own
        message, and this asserts the destructive instruction is absent from it.
        """
        self.write_raw({"version": STATE_FORMAT_VERSION + 1, "watchers": []})
        with self.assertRaises(FutureStateError) as cm:
            load_state("rc")
        msg = str(cm.exception)
        self.assertIn("Do not delete", msg)
        self.assertIn("newer version", msg)
        self.assertNotIn("move the state file(s) aside or delete", msg)

    def test_the_legacy_message_does_not_prescribe_the_cutover_config_rewrite(self):
        """§5.3's procedure rewrites concrete watchers into rules — and rules are not
        consumed yet on this branch, so an operator who followed it here would restart
        with **zero active watchers**. The message therefore says explicitly that the
        config rewrite belongs to the later cutover, and names the losses that do
        apply now."""
        self.write_raw({"watchers": [{"watcher_name": "w", "session_id": "s",
                                      "room_id": "r"}]})
        with self.assertRaises(LegacyStateError) as cm:
            load_state("rc")
        msg = str(cm.exception)
        self.assertIn("does NOT need rewriting", msg)
        self.assertIn("cutover", msg)
        self.assertIn("paused watcher comes back active", msg)

    def test_the_recovery_command_is_one_that_exists(self):
        """The installed entry points are `coop` and `coop-provision`;
        there is no `acg`. A recovery step that fails with command-not-found is worse
        than no step, since it arrives exactly when startup is already blocked."""
        self.write_raw({"watchers": []})
        with self.assertRaises(LegacyStateError) as cm:
            load_state("rc")
        msg = str(cm.exception)
        self.assertIn("coop list", msg)
        self.assertNotIn("'acg list'", msg)

    def test_both_refusals_share_a_catchable_base(self):
        """Callers decide once, not twice: `config validate` and the daemon preflight
        both catch the base rather than enumerating subclasses."""
        self.assertTrue(issubclass(LegacyStateError, StateFormatError))
        self.assertTrue(issubclass(FutureStateError, StateFormatError))

    def test_the_recovery_advice_is_followable_in_the_situation_it_describes(self):
        """Third round on this one message, and the sharpest of the three.

        It previously told the operator to run `coop list` to take an
        inventory. That command queries the running daemon — and the daemon is what
        just refused to start, so the instruction is impossible *by construction* in
        the only situation that produces this error. Not mis-ordered: unfollowable.

        The advice now points at the file itself, which is always available and in fact
        richer: it carries each watcher's name, session id, paused flag and watermark.
        """
        self.write_raw({"watchers": []})
        with self.assertRaises(LegacyStateError) as cm:
            load_state("rc")
        msg = str(cm.exception)
        self.assertIn("the file IS", msg)
        self.assertIn("paused flag", msg)
        # It may *mention* the command in order to warn against it, but must not
        # prescribe it as a step.
        self.assertNotIn("run 'coop list'", msg)

    def test_deeply_nested_json_is_corruption_not_a_refusal(self):
        """`json.loads` raises RecursionError, not ValueError, on ~100k nesting. With
        the except narrowed to (OSError, ValueError) it escaped — turning a corrupt
        file into an aborted startup and contradicting the contract two paragraphs up
        in load_state's own docstring."""
        self.state_path().write_text("[" * 100_000 + "]" * 100_000)
        self.assertEqual(load_state("rc"), [])


class TestPreflightCoversFilesNotConnectors(_RealStateFileTestCase):
    """The refusal has to be driven by what is on disk.

    Every path that would otherwise notice an unreadable file is per-connector — both
    `config validate`'s orphan check and `GatewayService`, which builds a session
    manager per *configured* connector. So a state file whose connector was renamed or
    removed in config.yaml is never opened, and the daemon starts successfully while
    abandoning every session in it: exactly the outcome the refusal exists to prevent,
    reached by a different route.
    """

    def test_state_files_lists_what_is_on_disk(self):
        save_state("rc", [])
        save_state("mm", [])
        self.assertEqual(
            [p.name for p in state_files()],
            ["state.mm.json", "state.rc.json"],
        )

    def test_a_connector_name_containing_dots_round_trips(self):
        """The name is recovered by stripping the fixed prefix and suffix, not by
        splitting on '.', so a dotted connector name survives."""
        save_state("rc.eu.prod", [WatcherState(
            watcher_name="w", session_id="s", room_id="r")])
        check_state_formats()  # must not raise
        self.assertEqual(len(load_state("rc.eu.prod")), 1)

    def test_the_preflight_refuses_a_file_for_an_unconfigured_connector(self):
        save_state("rc", [])                      # current format
        self.write_raw({"watchers": []}, "retired-connector")  # no version marker
        with self.assertRaises(LegacyStateError) as cm:
            check_state_formats()
        self.assertIn("state.retired-connector.json", str(cm.exception))

    def test_the_preflight_passes_when_every_file_is_current(self):
        save_state("rc", [WatcherState(watcher_name="w", session_id="s", room_id="r")])
        save_state("mm", [])
        check_state_formats()  # must not raise

    def test_the_preflight_ignores_a_corrupt_file(self):
        self.state_path().write_text("{ not json")
        check_state_formats()  # must not raise

    def test_no_files_at_all_is_fine(self):
        check_state_formats()

class TestTheReaderChecksTypes(_RealStateFileTestCase):
    """A value read without checking its type is the ninth instance of that shape here.

    This one escaped the file layer entirely: `{"watcher_name": []}` was accepted, and
    `StateStore.load()` then built `{ws.watcher_name: ws}` and raised
    `TypeError: unhashable type: 'list'` — aborting startup from a *caller*, so nothing
    inside `load_state` could have caught it, and contradicting the graceful-corruption
    contract this module documents.

    So the reader validates every field against the dataclass's own annotations rather
    than the one field that was reported.
    """

    def _one(self, record: dict):
        self.write_raw({"version": STATE_FORMAT_VERSION, "watchers": [record]})
        return load_state("rc")

    def test_a_non_string_watcher_name_is_skipped_not_returned(self):
        for bad in ([], {}, 7, None, True, ""):
            with self.subTest(name=bad):
                self.assertEqual(
                    self._one({"watcher_name": bad, "session_id": "s", "room_id": "r"}),
                    [],
                )

    def test_a_skipped_record_cannot_crash_statestore(self):
        """The actual failure, at the layer it happened: StateStore keys on the name."""
        from gateway.core.state_store import StateStore

        self.write_raw({"version": STATE_FORMAT_VERSION, "watchers": [
            {"watcher_name": [], "session_id": "s", "room_id": "r"},
            {"watcher_name": "good", "session_id": "s2", "room_id": "r2"},
        ]})
        connector = unittest.mock.MagicMock()
        connector.get_last_processed_ts.return_value = None
        loaded = StateStore("rc", connector).load()
        self.assertEqual(list(loaded), ["good"])

    def test_every_declared_field_rejects_a_wrong_type(self):
        """Enumerated from `_FIELD_TYPES`, so a field added to the dataclass is covered
        without anyone adding a case here."""
        from gateway.core.state import _FIELD_TYPES

        wrong: dict[type, object] = {
            str: [], bool: "yes", int: "seven", list: {"a": 1}, dict: ["a"],
        }
        for field_name, want in _FIELD_TYPES.items():
            with self.subTest(field=field_name, want=want.__name__):
                self.assertEqual(
                    self._one({
                        "watcher_name": "w", "session_id": "s", "room_id": "r",
                        field_name: wrong[want],
                    }),
                    [],
                    f"'{field_name}' accepted a {type(wrong[want]).__name__}",
                )

    def test_bool_and_int_are_not_interchangeable(self):
        """bool subclasses int, so each direction needs its own check."""
        self.assertEqual(
            self._one({"watcher_name": "w", "session_id": "s", "room_id": "r",
                       "paused": 1}),
            [],
            "'paused: 1' was accepted as a boolean",
        )

    def test_a_valid_record_beside_a_broken_one_still_loads(self):
        """One bad record is skipped, not the whole file: the others are real sessions,
        and discarding them would abandon more than the corruption did."""
        self.write_raw({"version": STATE_FORMAT_VERSION, "watchers": [
            {"watcher_name": "broken", "paused": "yes"},
            {"watcher_name": "fine", "session_id": "s", "room_id": "r"},
        ]})
        loaded = load_state("rc")
        self.assertEqual([ws.watcher_name for ws in loaded], ["fine"])

    def test_a_required_field_omitted_reads_as_empty(self):
        """What the previous reader did via per-field defaults, preserved: the
        dataclass keeps `session_id`/`room_id` required so no construction site can
        forget them, and the reader supplies the empty value instead."""
        (record,) = self._one({"watcher_name": "w"})
        self.assertEqual((record.session_id, record.room_id), ("", ""))

    def test_the_preflight_does_not_refuse_a_file_over_one_bad_record(self):
        self.write_raw({"version": STATE_FORMAT_VERSION,
                        "watchers": [{"watcher_name": []}]})
        check_state_formats()  # must not raise


class TestGarbledValuesNeverCrashConsumers(_RealStateFileTestCase):
    """The VALUE layer of the type-checking suite above (structural close
    after Codex rounds 8/9 hit it twice): a field can be the right TYPE and
    still carry garbage — `room_kind: "channel_typo"`, `session_expire_days:
    true` — and the crash then fires in a CONSUMER, one field past the
    loader's graceful-corruption contract. This walk feeds every field
    correctly-typed junk and drives the consumers the rounds actually broke:
    load, lifecycle classification, TTL arithmetic, config rebuild, kind
    conversion, and the room-description path."""

    _VALUE_GARBAGE = {
        "session_id": "\x00 not a session ::: id",
        "room_id": ":::",
        "room_type": "no-such-type",
        "room_name": "\x00\x01",
        "room_kind": "channel_typo",
        "participants": [1, None, {}, "alice"],
        "connector": ":::bad",
        "agent": "no-such-agent",
        "rule_name": "\x00",
        "rule": {"session_idle_days": True, "session_expire_days": "soon",
                 "name": [], "rooms": "not-a-dict"},
        "config": {"name": 123, "connector": [], "room": {},
                   "history_handoff": {"enabled": "yes"},
                   "context_inject_files": "not-a-list"},
        "backend_identity": ":::",
        "created_at": "not-a-date",
        "last_activity_at": "also-not-a-date",
        "dropped_at": "garbage",
        "last_processed_ts": "\x00",
        "config_schema_version": -99,
    }

    def test_every_field_survives_value_garbage(self):
        from datetime import datetime

        from gateway.core.state import (
            lifecycle_state,
            past_expire_ttl,
            past_idle_ttl,
            room_kind_or_channel,
        )
        from gateway.core.watcher_manager import (
            RoomRef,
            config_from_record,
            room_description,
            room_label,
        )

        now = datetime.now().astimezone()
        for field_name, garbage in self._VALUE_GARBAGE.items():
            with self.subTest(field=field_name):
                record = {
                    "watcher_name": "w1", "session_id": "s1", "room_id": "r1",
                    "room_kind": "dm", "participants": ["alice"],
                    "rule_name": "eng",
                    "rule": {"session_idle_days": 15, "session_expire_days": 15},
                    "config": {"name": "w1", "connector": "rc", "room": "x",
                               "agent": "default"},
                    field_name: garbage,
                }
                self.write_raw(
                    {"version": STATE_FORMAT_VERSION, "watchers": [record]})
                states = load_state("rc")  # must not raise
                for ws in states:
                    lifecycle_state(ws, resident=False)
                    past_idle_ttl(ws, now)
                    past_expire_ttl(ws, now)
                    config_from_record(ws)
                    kind = room_kind_or_channel(ws)
                    ref = RoomRef(id=ws.room_id, kind=kind,
                                  name=ws.room_name,
                                  participants=tuple(ws.participants))
                    room_label(ref)        # the wake's naming path
                    room_description(ref)  # the identity header's path


class TestCorruptionStaysGraceful(_RealStateFileTestCase):
    def test_a_corrupt_file_still_degrades_gracefully(self):
        """Deliberately *not* a refusal. A corrupt file holds no recoverable state
        either way, so refusing to boot over it would trade a graceful degradation for
        an outage. Only a *readable* file in an unreadable format is worth refusing."""
        self.state_path().write_text("{not json at all")
        self.assertEqual(load_state("rc"), [])

    def test_a_non_object_payload_degrades_gracefully(self):
        self.state_path().write_text("[]")
        self.assertEqual(load_state("rc"), [])

    def test_a_versioned_file_with_a_junk_record_degrades_rather_than_raising(self):
        self.write_raw({"version": STATE_FORMAT_VERSION, "watchers": ["nonsense", 7]})
        self.assertEqual(load_state("rc"), [])


if __name__ == "__main__":
    unittest.main()
