"""A state file for a connector that is no longer configured is reclaimed at
boot: each record's session id is logged, then the file is removed (#143).

Seam: a real `GatewayService` built from a config file; the sweep runs in its
constructor, between the two state preflights.
"""

import dataclasses
import json
import unittest
from unittest.mock import patch

from gateway.core.state import STATE_FORMAT_VERSION, save_state
from gateway.core.watcher_manager import RoomRef
from gateway.core.watcher_rule import RoomKind
from tests.helpers import (
    isolate_runtime_dir,
    make_record_from_rule,
    make_rule,
    write_gateway_config,
)


# Local on purpose: the removed connector's records are specific to this suite;
# the shared `make_record_from_rule` does the construction.
def _ghost_record(session_id, room_id="r-old"):
    """A rule-derived record as the removed connector 'ghost' would have written it."""
    rule = make_rule(room="old-room", name="eng", connector="ghost", agent="default")
    room = RoomRef(id=room_id, kind=RoomKind.CHANNEL, name="old-room")
    return make_record_from_rule(rule, room, session_id=session_id)


class TestOrphanedStateFiles(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.tmp, self.runtime = isolate_runtime_dir(self)

    def _write_raw(self, connector, payload):
        """Bypasses the real writer on purpose: these tests need shapes the
        writer cannot produce (a malformed record, a non-list `watchers`)."""
        (self.runtime / f"state.{connector}.json").write_text(json.dumps(payload))

    async def test_records_of_an_unconfigured_connector_are_logged_and_the_file_removed(self):
        from gateway.service import GatewayService

        save_state("ghost", [_ghost_record("sess-ghost-7777")])
        save_state("script", [])

        with self.assertLogs("coop", level="INFO") as logs:
            GatewayService(write_gateway_config(self.tmp))

        self.assertFalse((self.runtime / "state.ghost.json").exists(),
                         "nothing will ever open it again")
        self.assertTrue((self.runtime / "state.script.json").exists(),
                        "the configured connector's file is not touched")
        audit = [line for line in logs.output
                 if "AUDIT: session released" in line and "sess-ghost-7777" in line]
        self.assertEqual(len(audit), 1, logs.output)
        self.assertIn("connector-removed", audit[0])

    async def test_a_file_that_cannot_be_removed_is_not_reported_released(self):
        from gateway.service import GatewayService

        save_state("ghost", [_ghost_record("sess-ghost-8888")])

        with patch("pathlib.Path.unlink", side_effect=OSError("read-only")), \
                self.assertLogs("coop", level="WARNING") as logs:
            GatewayService(write_gateway_config(self.tmp))

        self.assertTrue((self.runtime / "state.ghost.json").exists())
        self.assertFalse(any("AUDIT: session released" in line for line in logs.output),
                         "a release that did not happen is not announced")
        self.assertTrue(any("remain until the next start" in line for line in logs.output))

    async def test_a_file_with_a_record_that_does_not_parse_is_kept(self):
        from gateway.service import GatewayService

        good = dataclasses.asdict(_ghost_record("sess-ghost-ok", room_id="r-ok"))
        bad = dict(dataclasses.asdict(_ghost_record("sess-ghost-bad", room_id="r-bad")),
                   paused="yes please")  # not a bool: load_state skips it
        self._write_raw("ghost", {"version": STATE_FORMAT_VERSION, "watchers": [good, bad]})

        with self.assertLogs("coop", level="WARNING") as logs:
            GatewayService(write_gateway_config(self.tmp))

        self.assertTrue((self.runtime / "state.ghost.json").exists(),
                        "deleting it would lose sess-ghost-bad without a trace")
        self.assertFalse(any("AUDIT: session released" in line for line in logs.output))
        self.assertTrue(any("could not be parsed" in line for line in logs.output), logs.output)

    async def test_an_orphan_sharing_session_ids_with_a_live_file_does_not_block_the_boot(self):
        """Renaming a connector by copying its state file leaves the old file behind
        with the SAME session ids. The uniqueness preflight must see the fleet after
        the sweep, or the boot is refused for records about to be released."""
        from gateway.service import GatewayService

        shared = _ghost_record("sess-shared-1", room_id="r1")
        save_state("old-name", [shared])
        save_state("script", [dataclasses.replace(
            shared, connector="script", watcher_name="script:room")])

        GatewayService(write_gateway_config(self.tmp))  # no DuplicateSessionError

        self.assertFalse((self.runtime / "state.old-name.json").exists())
        self.assertTrue((self.runtime / "state.script.json").exists())

    async def test_a_file_whose_watchers_field_is_not_a_list_is_kept_and_does_not_block_the_boot(self):
        """`load_state` reads a null/scalar `watchers` as corrupt-but-loadable (empty);
        the sweep must not crash the constructor on it — one stale file must never
        keep every configured connector from starting."""
        from gateway.service import GatewayService

        self._write_raw("ghost", {"version": STATE_FORMAT_VERSION, "watchers": None})

        with self.assertLogs("coop", level="WARNING") as logs:
            GatewayService(write_gateway_config(self.tmp))  # no TypeError

        self.assertTrue((self.runtime / "state.ghost.json").exists(), "kept for manual repair")
        self.assertTrue(any("could not be read at all" in line for line in logs.output), logs.output)
