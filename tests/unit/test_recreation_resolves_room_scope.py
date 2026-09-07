"""#145: the operator-driven recreation sites resolve the room before rebuilding a watcher.

A record's room fields say what the room was when the record was written, not
whether this connector still serves it. Boot (#141) and the wake path already
ask the connector through `room_ref_by_id` before recreating a watcher from a
dormant record; `resume` and `inject_message`'s has-record branch were the two
sites left that trusted the stored fields — so a paused record for a room the
connector was reconfigured away from could be brought back to life by an
operator verb or a scheduled job, and the tracked-message path (no `_in_scope()`)
would then deliver the old room's messages to the agent.

Both sites keep the connector contract's two failure shapes apart, as the wake
path does: `None` is permanent (gone, another team, no longer a member) and the
verb is refused for good; a raise is transient and the verb is refused with a
retryable error. Neither reclaims the record — the operator is present and has
`expire` for that (see `SessionManager._require_room_served`).

Run with:
    uv run python -m pytest tests/unit/test_recreation_resolves_room_scope.py -v
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock

from gateway.core.watcher_manager import RoomRef
from gateway.core.watcher_rule import RoomKind
from tests.helpers import make_bare_session_manager, make_rule_derived_record

_LOGGER = "agent-chat-gateway.core.session_manager"


def _dormant_record(*, paused=False, name="mm:old-team-general", room_id="r-old",
                    session_id="sess-old-team-1234"):
    """A rule-derived record with no resident processor — paused when the test
    says so, idle otherwise. The shared builder, so the record is one the real
    lifecycle would accept (`config_from_record` reads `config.name`); a
    hand-built subset here would pass only because `_lifecycle` is a mock.
    Pause stops the processor, so paused+resident is not a state a real code
    path produces: the resident tests use the idle default."""
    return make_rule_derived_record(
        name, room_id=room_id, connector="mm", session_id=session_id,
        room_name="general", room_kind="channel", paused=paused,
    )


def _served(room_id="r-old"):
    return RoomRef(id=room_id, kind=RoomKind.CHANNEL, name="general")


def _resume_manager(record, *, resolved=None, resident=False, lookup=True):
    """A manager wired for the `resume` verb: the record is found BY NAME, the
    resident check BY ROOM ID, and the connector answers `resolved`
    (a `RoomRef`, `None`, or an exception instance to raise)."""
    mgr = make_bare_session_manager()
    by_name = {record.watcher_name: record} if record is not None else {}
    mgr._lifecycle.get_watcher_state = MagicMock(side_effect=by_name.get)
    by_room = ({record.room_id: "proc"} if (record is not None and resident) else {})
    mgr._lifecycle.processor_for_room = MagicMock(side_effect=by_room.get)
    mgr._connector.supports_room_lookup = MagicMock(return_value=lookup)
    if isinstance(resolved, BaseException):
        mgr._connector.room_ref_by_id = AsyncMock(side_effect=resolved)
    else:
        mgr._connector.room_ref_by_id = AsyncMock(return_value=resolved)
    return mgr


class TestResumeResolvesTheRoomFirst(unittest.IsolatedAsyncioTestCase):
    """At the control-command seam, as the issue asks: `dispatch_command`
    with a fake connector whose `room_ref_by_id` returns None / raises / answers."""

    async def test_a_room_the_connector_no_longer_serves_is_refused_and_left_paused(self):
        record = _dormant_record(paused=True)
        mgr = _resume_manager(record, resolved=None)

        with self.assertLogs(_LOGGER, level="WARNING") as logs:
            result = await mgr.dispatch_command(
                {"cmd": "resume", "watcher_name": "mm:old-team-general"})

        self.assertFalse(result["ok"])
        self.assertIn("not available to this connector", result["error"])
        self.assertIn("expire", result["error"],
                      "the refusal names the verb that reclaims the record")
        mgr._lifecycle.resume_watcher.assert_not_awaited()
        self.assertTrue(record.paused, "the record is left for the operator, not reclaimed")
        mgr._lifecycle.reclaim_room.assert_not_called()
        # The full session id goes in the log (issue #145).
        self.assertTrue(any("sess-old-team-1234" in line for line in logs.output),
                        logs.output)

    async def test_a_transient_lookup_failure_is_refused_as_retryable(self):
        record = _dormant_record(paused=True)
        mgr = _resume_manager(record, resolved=OSError("connection reset"))

        with self.assertLogs(_LOGGER, level="WARNING") as logs:
            result = await mgr.dispatch_command(
                {"cmd": "resume", "watcher_name": "mm:old-team-general"})

        self.assertFalse(result["ok"])
        self.assertIn("retry", result["error"].lower())
        self.assertNotIn("not available to this connector", result["error"],
                         "a blip must not read as a deleted room")
        mgr._lifecycle.resume_watcher.assert_not_awaited()
        self.assertTrue(record.paused)
        self.assertTrue(any("connection reset" in line for line in logs.output), logs.output)

    async def test_a_room_the_connector_still_serves_resumes_as_before(self):
        record = _dormant_record(paused=True)
        mgr = _resume_manager(record, resolved=_served())

        result = await mgr.dispatch_command(
            {"cmd": "resume", "watcher_name": "mm:old-team-general"})

        self.assertTrue(result["ok"], result)
        mgr._connector.room_ref_by_id.assert_awaited_once_with("r-old")
        mgr._lifecycle.resume_watcher.assert_awaited_once_with("mm:old-team-general")

    async def test_a_resident_watcher_is_not_resolved_again(self):
        """A resume on a running watcher only clears the paused flag — no
        recreation, so no round trip; the check is for the dormant record.
        (Not `paused=True`: pause stops the processor, so paused+resident is
        not a state a real code path produces.)"""
        record = _dormant_record()
        mgr = _resume_manager(record, resolved=None, resident=True)

        result = await mgr.dispatch_command(
            {"cmd": "resume", "watcher_name": "mm:old-team-general"})

        self.assertTrue(result["ok"], result)
        mgr._connector.room_ref_by_id.assert_not_awaited()
        mgr._lifecycle.resume_watcher.assert_awaited_once()

    async def test_a_connector_without_room_lookup_keeps_the_old_behaviour(self):
        """The base `room_ref_by_id` answers None for "cannot look rooms up";
        read as "gone" it would refuse every resume on such a connector. Same
        guard as boot's `_room_still_served`."""
        record = _dormant_record(paused=True)
        mgr = _resume_manager(record, resolved=None, lookup=False)

        result = await mgr.dispatch_command(
            {"cmd": "resume", "watcher_name": "mm:old-team-general"})

        self.assertTrue(result["ok"], result)
        mgr._connector.room_ref_by_id.assert_not_awaited()
        mgr._lifecycle.resume_watcher.assert_awaited_once()

    async def test_a_record_replaced_during_the_lookup_is_not_resumed(self):
        """The lookup yields; in that window the record is reclaimed and another
        room takes the handle over (Codex on #150). The lifecycle would read
        the name afresh and resume the replacement, whose room was never
        checked — so the name is re-read after the lookup and pinned to the
        record the check ran on, like `_resume_locked`'s own pin."""
        old = _dormant_record(paused=True)
        replacement = _dormant_record(paused=True, room_id="r-new",
                                      session_id="sess-new-9999")
        mgr = _resume_manager(old, resolved=_served())
        # First read (before the lookup) answers the old record; the re-read
        # after it answers the replacement.
        mgr._lifecycle.get_watcher_state = MagicMock(side_effect=[old, replacement])

        with self.assertLogs(_LOGGER, level="WARNING") as logs:
            result = await mgr.dispatch_command(
                {"cmd": "resume", "watcher_name": "mm:old-team-general"})

        self.assertFalse(result["ok"])
        self.assertIn("skipped", result["error"])
        self.assertIn("replaced while the resume waited", result["error"])
        self.assertTrue(any("skipped" in line for line in logs.output), logs.output)
        mgr._lifecycle.resume_watcher.assert_not_awaited()

    async def test_a_name_with_no_record_is_left_to_the_lifecycle_to_refuse(self):
        """No record, nothing to resolve: the lifecycle owns that error message."""
        mgr = _resume_manager(None, resolved=None)

        await mgr.dispatch_command({"cmd": "resume", "watcher_name": "mm:never-seen"})

        mgr._connector.room_ref_by_id.assert_not_awaited()
        mgr._lifecycle.resume_watcher.assert_awaited_once_with("mm:never-seen")


def _inject_manager(record, *, resolved=None, resident=False, lookup=True):
    """A manager wired for `inject_message` with a record for the room. Every
    lookup honours its argument (test_job_room_identity.py's rule)."""
    mgr = make_bare_session_manager()
    processor = MagicMock()
    processor.enqueue = AsyncMock(return_value=True)
    mgr._injected_processor = processor  # type: ignore[attr-defined]
    by_room = {record.room_id: record}
    mgr._lifecycle.record_for_room = MagicMock(side_effect=by_room.get)
    residents = {record.room_id: processor} if resident else {}
    mgr._lifecycle.processor_for_room = MagicMock(side_effect=residents.get)
    mgr._watcher_manager = MagicMock()
    mgr._watcher_manager.get_or_create = AsyncMock(return_value=processor)
    mgr._connector = MagicMock()
    mgr._connector.supports_room_lookup = MagicMock(return_value=lookup)
    if isinstance(resolved, BaseException):
        mgr._connector.room_ref_by_id = AsyncMock(side_effect=resolved)
    else:
        mgr._connector.room_ref_by_id = AsyncMock(return_value=resolved)
    return mgr


class TestInjectMessageResolvesADormantRecordsRoom(unittest.IsolatedAsyncioTestCase):
    """The scheduled-job path (issue comment): with a record and no resident
    processor, the room used to be rebuilt from the record and handed to
    `get_or_create` — the same recreation from stored fields as `resume`."""

    async def test_a_dormant_record_for_an_unserved_room_is_not_recreated(self):
        record = _dormant_record()  # idle: dormancy is the condition, not pause
        mgr = _inject_manager(record, resolved=None)

        with self.assertLogs(_LOGGER, level="INFO") as logs:
            result = await mgr.inject_message("r-old", "poke")

        self.assertFalse(result)
        mgr._watcher_manager.get_or_create.assert_not_awaited()
        mgr._injected_processor.enqueue.assert_not_awaited()
        joined = "\n".join(logs.output)
        # Final, not a retry — and said so (the transient test pins the other).
        self.assertIn("not available to this connector", joined)
        self.assertNotIn("retries at its next slot", joined)
        # The full session id goes in the log here too (issue #145) — the
        # wake path's own line names only the room.
        self.assertIn("sess-old-team-1234", joined)

    async def test_a_transient_lookup_failure_skips_the_fire_for_this_slot(self):
        record = _dormant_record()
        mgr = _inject_manager(record, resolved=OSError("net"))

        with self.assertLogs(_LOGGER, level="WARNING") as logs:
            result = await mgr.inject_message("r-old", "poke")

        self.assertFalse(result)
        mgr._watcher_manager.get_or_create.assert_not_awaited()
        self.assertTrue(any("retries" in line for line in logs.output), logs.output)

    async def test_a_dormant_record_for_a_served_room_is_recreated_from_the_connectors_answer(self):
        """One resolution feeds both the wake and the reply address — the
        connector's, which is current, rather than the record's snapshot."""
        record = _dormant_record()
        served = RoomRef(id="r-old", kind=RoomKind.CHANNEL, name="general-renamed")
        mgr = _inject_manager(record, resolved=served)

        result = await mgr.inject_message("r-old", "poke")

        self.assertTrue(result)
        mgr._connector.room_ref_by_id.assert_awaited_once_with("r-old")
        self.assertIs(mgr._watcher_manager.get_or_create.await_args.args[1], served)
        enqueued = mgr._injected_processor.enqueue.await_args.args[0]
        self.assertEqual(enqueued.room.id, "r-old")
        self.assertEqual(enqueued.room.name, "general-renamed")

    async def test_a_resident_watcher_is_addressed_from_its_record(self):
        """Nothing is recreated for a running watcher, so no round trip."""
        record = _dormant_record()
        mgr = _inject_manager(record, resolved=None, resident=True)

        result = await mgr.inject_message("r-old", "poke")

        self.assertTrue(result)
        mgr._connector.room_ref_by_id.assert_not_awaited()
        mgr._watcher_manager.get_or_create.assert_not_awaited()
        self.assertEqual(mgr._injected_processor.enqueue.await_args.args[0].room.id, "r-old")

    async def test_a_connector_without_room_lookup_recreates_from_the_record(self):
        record = _dormant_record()
        mgr = _inject_manager(record, resolved=None, lookup=False)

        result = await mgr.inject_message("r-old", "poke")

        self.assertTrue(result)
        mgr._connector.room_ref_by_id.assert_not_awaited()
        room = mgr._watcher_manager.get_or_create.await_args.args[1]
        self.assertEqual((room.id, room.name), ("r-old", "general"))


if __name__ == "__main__":
    unittest.main()
