"""A watcher's handle follows its room's rename (owner, 2026-09-02; design §2.3).

The handle `<connector>:<room label>` is a function of the room's CURRENT name,
recomputed from the frame — never an identity. Identity is the room id. These
tests pin the lifecycle observer, the session manager seam every claimed frame
passes, and the `schedule list` column that must show the name as it is now.
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock

from tests.helpers import (
    install_record,
    make_bare_session_manager,
    make_lifecycle,
    make_rule_derived_record,
)
from tests.unit.test_lifecycle_keyed_by_room import _assert_consistent


def _lifecycle_with(name="mm:test-channel", room_id="R1", room_name="test-channel",
                    room_kind="channel", connector="mm"):
    lifecycle = make_lifecycle()
    record = make_rule_derived_record(name=name, room_id=room_id, connector=connector)
    record.room_name = room_name
    record.room_kind = room_kind
    record.room_type = "dm" if room_kind == "dm" else "channel"
    install_record(lifecycle, record)
    return lifecycle, record


class TestTheHandleFollowsTheRoom(unittest.IsolatedAsyncioTestCase):

    async def test_a_renamed_channel_gets_a_new_handle_and_the_old_one_stops_resolving(self):
        lifecycle, record = _lifecycle_with()

        with self.assertLogs("agent-chat-gateway.core.watcher_lifecycle", "WARNING") as cm:
            taken = await lifecycle.observe_room_name("R1", "test-channel-new")

        self.assertEqual(taken, "mm:test-channel-new")
        self.assertEqual(record.watcher_name, "mm:test-channel-new")
        self.assertEqual(record.room_name, "test-channel-new")
        self.assertIs(lifecycle.get_watcher_state("mm:test-channel-new"), record)
        self.assertIsNone(lifecycle.get_watcher_state("mm:test-channel"))
        self.assertIn("AUDIT", "\n".join(cm.output))
        _assert_consistent(self, lifecycle)

    async def test_the_old_name_is_pruned_from_the_file(self):
        """`StateStore.save` merges by name. Without the prune the file kept a
        frozen row under the old handle beside the live one, and the next boot
        hydrated that row first — old handle, old session, old watermark
        (internal review, P1)."""
        lifecycle, _ = _lifecycle_with()

        await lifecycle.observe_room_name("R1", "test-channel-new")

        lifecycle._state_store.save.assert_called_once()
        self.assertEqual(lifecycle._state_store.save.call_args.kwargs.get("prune"), {"mm:test-channel"})

    async def test_the_same_name_is_a_no_op(self):
        lifecycle, record = _lifecycle_with()

        self.assertIsNone(await lifecycle.observe_room_name("R1", "test-channel"))
        self.assertEqual(record.watcher_name, "mm:test-channel")
        lifecycle._state_store.save.assert_not_called()

    async def test_an_unknown_room_or_an_empty_name_changes_nothing(self):
        lifecycle, record = _lifecycle_with()

        self.assertIsNone(await lifecycle.observe_room_name("R-nope", "x"))
        self.assertIsNone(await lifecycle.observe_room_name("R1", ""))
        self.assertEqual(record.watcher_name, "mm:test-channel")

    async def test_the_lock_moves_with_the_name(self):
        """The same mutex object under the new name: a holder keeps holding it
        and the next taker waits on it, not on a twin."""
        lifecycle, _ = _lifecycle_with()
        lock = lifecycle._get_watcher_lock("mm:test-channel")

        await lifecycle.observe_room_name("R1", "renamed")

        self.assertIs(lifecycle._get_watcher_lock("mm:renamed"), lock)
        self.assertNotIn("mm:test-channel", lifecycle._watcher_locks)

    async def test_a_handle_held_by_another_room_is_not_taken(self):
        """Platforms keep names unique per team, so the holder is a stale record
        of a room renamed away and not yet heard from. The description is
        refreshed; the handle is kept; nothing is re-pointed."""
        lifecycle, record = _lifecycle_with()
        other = make_rule_derived_record(name="mm:renamed", room_id="R2", connector="mm")
        install_record(lifecycle, other)

        with self.assertLogs("agent-chat-gateway.core.watcher_lifecycle", "WARNING") as cm:
            taken = await lifecycle.observe_room_name("R1", "renamed")

        self.assertIsNone(taken)
        self.assertEqual(record.watcher_name, "mm:test-channel")
        self.assertEqual(record.room_name, "test-channel",
                         "nothing written — a refreshed description would stop the retry")
        self.assertIs(lifecycle.get_watcher_state("mm:renamed"), other)
        self.assertIn("still belongs to room R2", "\n".join(cm.output))
        lifecycle._state_store.save.assert_not_called()
        _assert_consistent(self, lifecycle)

    async def test_once_the_holder_is_gone_the_next_frame_takes_the_name(self):
        """The 'until' in the warning has to be able to fire: the first version
        refreshed `room_name` in the collision case, and the same-name
        short-circuit then made every later frame a no-op (internal review)."""
        lifecycle, record = _lifecycle_with()
        other = make_rule_derived_record(name="mm:renamed", room_id="R2", connector="mm")
        install_record(lifecycle, other)
        await lifecycle.observe_room_name("R1", "renamed")          # refused, holder present
        lifecycle._uninstall("mm:renamed")                    # the holder is reclaimed

        self.assertEqual(await lifecycle.observe_room_name("R1", "renamed"), "mm:renamed")
        self.assertIs(lifecycle.get_watcher_state("mm:renamed"), record)

    async def test_a_private_group_is_renamed_and_a_group_dm_is_not(self):
        lifecycle, record = _lifecycle_with(name="mm:ops", room_name="ops", room_kind="group")
        self.assertEqual(await lifecycle.observe_room_name("R1", "ops-new"), "mm:ops-new")

        lifecycle2, record2 = _lifecycle_with(name="mm:gdm:abc", room_name="a, b", room_kind="group_dm")
        self.assertIsNone(await lifecycle2.observe_room_name("R1", "a, b, c"))
        self.assertEqual(record2.watcher_name, "mm:gdm:abc")

    async def test_a_dm_is_not_renamed_by_a_frame(self):
        """A DM's label derives from the participants, which no frame carries."""
        lifecycle, record = _lifecycle_with(name="mm:dm:alice", room_name="alice", room_kind="dm")

        self.assertIsNone(await lifecycle.observe_room_name("R1", "alice-renamed"))
        self.assertEqual(record.watcher_name, "mm:dm:alice")


    async def test_the_resident_processor_takes_the_new_name(self):
        """The processor carries the handle into the ACG Session Identity header
        the agent reads every turn (Codex, PR #140): a stale one sends the
        agent's own `schedule create` at a name that no longer resolves."""
        from tests.helpers import register_processor

        lifecycle, _ = _lifecycle_with()
        processor = register_processor(lifecycle, "mm:test-channel", MagicMock(rename=AsyncMock()))

        await lifecycle.observe_room_name("R1", "test-channel-new")

        processor.rename.assert_awaited_once_with("mm:test-channel-new", room_name="test-channel-new")

    async def test_a_processor_that_cannot_take_the_name_does_not_fail_the_rename(self):
        from tests.helpers import register_processor

        lifecycle, record = _lifecycle_with()
        register_processor(lifecycle, "mm:test-channel",
                           MagicMock(rename=AsyncMock(side_effect=OSError("disk"))))

        with self.assertLogs("agent-chat-gateway.core.watcher_lifecycle", "WARNING") as cm:
            taken = await lifecycle.observe_room_name("R1", "test-channel-new")

        self.assertEqual(taken, "mm:test-channel-new")
        self.assertEqual(record.watcher_name, "mm:test-channel-new")
        self.assertIn("could not rewrite its identity header", "\n".join(cm.output))

class TestEveryClaimedFramePassesTheObserver(unittest.IsolatedAsyncioTestCase):

    def _mgr(self, *, unsolicited):
        mgr = make_bare_session_manager()
        mgr._connector.supports_unsolicited_inbound = MagicMock(return_value=unsolicited)
        mgr._dispatcher.dispatch = AsyncMock(return_value=True)
        mgr._lifecycle.observe_room_name = AsyncMock(return_value=None)
        return mgr

    async def test_a_discovering_connector_observes_then_dispatches(self):
        mgr = self._mgr(unsolicited=True)
        msg = MagicMock()
        msg.room.id, msg.room.name = "R1", "renamed"

        self.assertTrue(await mgr._on_inbound(msg))

        mgr._lifecycle.observe_room_name.assert_awaited_once_with("R1", "renamed")
        mgr._dispatcher.dispatch.assert_awaited_once_with(msg)

    async def test_an_eager_connectors_room_name_is_config_not_a_platform_fact(self):
        """Script/voice room names are the configured literal; a frame must not
        rename the watcher after them."""
        mgr = self._mgr(unsolicited=False)
        msg = MagicMock()

        await mgr._on_inbound(msg)

        mgr._lifecycle.observe_room_name.assert_not_called()
        mgr._dispatcher.dispatch.assert_awaited_once_with(msg)

    async def test_the_connector_handler_is_the_observing_seam(self):
        """`connect_only` registers `_on_inbound`, not the dispatcher directly —
        otherwise the observer is bypassed for every live frame."""
        mgr = self._mgr(unsolicited=True)
        mgr._connector.connect = AsyncMock()
        mgr._connector.register_handler = MagicMock()
        mgr._connector.register_capacity_check = MagicMock()
        mgr._connector.register_router = MagicMock()
        mgr._connector.register_membership_hook = MagicMock()
        mgr._watcher_manager = None

        await mgr.connect_only()

        mgr._connector.register_handler.assert_called_once_with(mgr._on_inbound)


class TestScheduleListNamesTheWatcherAsItIsNow(unittest.TestCase):

    def test_the_watcher_column_comes_from_the_record_when_one_exists(self):
        from gateway.control import ControlServer
        from gateway.schedule_types import ScheduledJob

        job = ScheduledJob(id="acg-1", watcher="mm:test-channel", connector="mm", room_id="R1",
                           message="m", cron="* * * * *")
        store = MagicMock()
        store.list_jobs = MagicMock(return_value=[job])
        entry = MagicMock()
        entry.name = "mm"
        record = MagicMock()
        record.watcher_name = "mm:test-channel-new"
        entry.session_manager.record_for_room = MagicMock(return_value=record)

        server = ControlServer(entries=[entry], job_store=store)
        result = server._handle_schedule_list({"cmd": "schedule-list"})

        self.assertEqual(result["jobs"][0]["watcher"], "mm:test-channel-new")
        entry.session_manager.record_for_room.assert_called_once_with("R1")

    def test_without_a_record_the_stored_spelling_stands(self):
        from gateway.control import ControlServer
        from gateway.schedule_types import ScheduledJob

        job = ScheduledJob(id="acg-1", watcher="mm:gone", connector="mm", room_id="R1",
                           message="m", cron="* * * * *")
        store = MagicMock()
        store.list_jobs = MagicMock(return_value=[job])
        entry = MagicMock()
        entry.name = "mm"
        entry.session_manager.record_for_room = MagicMock(return_value=None)

        server = ControlServer(entries=[entry], job_store=store)
        result = server._handle_schedule_list({"cmd": "schedule-list"})

        self.assertEqual(result["jobs"][0]["watcher"], "mm:gone")

    def test_the_connector_filter_reaches_the_store_and_a_room_less_job_is_left_alone(self):
        from gateway.control import ControlServer
        from gateway.schedule_types import ScheduledJob

        job = ScheduledJob(id="acg-1", watcher="rc:legacy", connector="rc", room_id="",
                           message="m", cron="* * * * *")
        store = MagicMock()
        store.list_jobs = MagicMock(return_value=[job])
        entry = MagicMock()
        entry.name = "rc"

        server = ControlServer(entries=[entry], job_store=store)
        result = server._handle_schedule_list({"cmd": "schedule-list", "connector": "rc"})

        store.list_jobs.assert_called_once_with(connector="rc", include_completed=False)
        entry.session_manager.record_for_room.assert_not_called()
        self.assertEqual(result["jobs"][0]["watcher"], "rc:legacy")


class TestTheProcessorReissuesItsIdentityOnRename(unittest.IsolatedAsyncioTestCase):

    async def test_the_header_names_the_new_handle_under_the_same_room_keyed_file(self):
        from gateway.core.config import WatcherConfig
        from gateway.core.paths import watcher_prompt_key
        from tests.helpers import make_processor

        injector = MagicMock()
        injector.build = AsyncMock(side_effect=lambda agent, conn, wc, **kw: f"## ACG Session Identity\n- {wc.name} / {wc.room}")
        agent = MagicMock()
        agent.ensure_durable_instructions = AsyncMock(return_value="/runtime/system-prompts/k.md")
        wc = WatcherConfig(name="mm:test-channel", connector="mm", room="test-channel", agent="default")
        # `watcher_id` is the ROOM ID in production (`_start_watcher`), not the handle.
        processor = make_processor(agent=agent, watcher_id="room_1", connector_name="mm",
                                   context_injector=injector, watcher_config=wc)

        await processor.rename("mm:test-channel-new", room_name="test-channel-new")

        self.assertEqual(processor._watcher_config.name, "mm:test-channel-new")
        self.assertEqual(processor._watcher_config.room, "test-channel-new")
        self.assertEqual(processor.watcher_id, "room_1",
                         "the processor's identity is the room id and must not move")
        call = agent.ensure_durable_instructions.await_args
        self.assertIn("mm:test-channel-new / test-channel-new", call.args[3])
        self.assertEqual(call.kwargs["path_key"], watcher_prompt_key("mm", "room_1"),
                         "the same file the session was started with")
        self.assertEqual(processor._append_system_prompt_file, "/runtime/system-prompts/k.md")
        self.assertIs(call.kwargs["already_delivered"], False,
                      "a rename is a re-delivery; the keyword is required by both backends")

    async def test_the_rewrite_reaches_a_real_backend_signature(self):
        """The first version omitted the required `already_delivered` keyword; a
        bare AsyncMock accepted the call and the test passed while every real
        rename raised TypeError (Codex, PR #140). A stub with the base method's
        exact signature lets Python enforce it — `create_autospec` did not."""
        from gateway.core.config import WatcherConfig
        from tests.helpers import MockAgentBackend, make_processor

        seen = {}

        class _Backend(MockAgentBackend):
            async def ensure_durable_instructions(   # the base signature, verbatim
                self, session_id, working_directory, timeout, content, *,
                path_key, already_delivered,
            ):
                seen.update(content=content, path_key=path_key, already_delivered=already_delivered)
                return "/p.md"

        injector = MagicMock()
        injector.build = AsyncMock(return_value="header")
        wc = WatcherConfig(name="mm:a", connector="mm", room="a", agent="default")
        processor = make_processor(agent=_Backend(), watcher_id="mm:a", connector_name="mm",
                                   context_injector=injector, watcher_config=wc)

        await processor.rename("mm:b", room_name="b")   # a bad call raises TypeError here

        self.assertEqual(processor._append_system_prompt_file, "/p.md")
        self.assertIs(seen["already_delivered"], False)

    async def test_without_an_injector_only_the_room_description_moves(self):
        from tests.helpers import make_processor

        processor = make_processor(watcher_id="room_1")
        await processor.rename("mm:b", room_name="b")
        self.assertEqual(processor.watcher_id, "room_1")
        self.assertEqual(processor._room.name, "b")

    async def test_a_failed_rewrite_arms_the_next_messages_retry(self):
        """`_ensure_context_injected` returns at once while `context_injected`
        is set and the injector's status says done — so the lifecycle's promised
        "retry" never ran and the old header stood for the watcher's lifetime
        (Codex, PR #140). A failure now clears both."""
        from gateway.core.config import WatcherConfig
        from tests.helpers import make_processor, make_rule_derived_record

        injector = MagicMock()
        injector.build = AsyncMock(return_value="header")
        agent = MagicMock()
        agent.ensure_durable_instructions = AsyncMock(side_effect=OSError("disk full"))
        ws = make_rule_derived_record(name="mm:a", room_id="room_1")
        ws.context_injected = True
        wc = WatcherConfig(name="mm:a", connector="mm", room="a", agent="default")
        processor = make_processor(agent=agent, watcher_id="mm:a", connector_name="mm",
                                   context_injector=injector, watcher_config=wc, watcher_state=ws)

        with self.assertRaises(OSError):
            await processor.rename("mm:b", room_name="b")

        self.assertFalse(ws.context_injected, "the next message must re-run the injection")
        injector.reset_session.assert_called_once_with("ses_001")
        self.assertEqual(processor._watcher_config.name, "mm:b", "the name moved even though the rewrite did not")


class TestAStaleHandleIsRederivedEvenWhenTheRoomNameMatches(unittest.IsolatedAsyncioTestCase):
    """A reset that captured its config before a rename and reinstalled the
    record afterwards left the OLD handle beside the NEW room name. A
    short-circuit on `room_name` then never re-derived it (Codex, PR #140);
    the comparison is on the derived handle now."""

    async def test_the_handle_catches_up_on_the_next_frame(self):
        lifecycle, record = _lifecycle_with(name="mm:test-channel", room_name="test-channel-new")

        taken = await lifecycle.observe_room_name("R1", "test-channel-new")

        self.assertEqual(taken, "mm:test-channel-new")
        self.assertEqual(record.watcher_name, "mm:test-channel-new")
        self.assertIsNone(lifecycle.get_watcher_state("mm:test-channel"))

    async def test_a_frame_that_changes_nothing_still_writes_nothing(self):
        lifecycle, _ = _lifecycle_with()
        self.assertIsNone(await lifecycle.observe_room_name("R1", "test-channel"))
        lifecycle._state_store.save.assert_not_called()


def _serve_room(mgr, room_id):
    """The connector answers for `room_id`: with the bare manager's MagicMock
    connector the #145 lookup would raise TypeError on `await`, be swallowed as
    a transient blip, and `get_or_create` would never be reached."""
    from gateway.core.watcher_manager import RoomRef
    from gateway.core.watcher_rule import RoomKind

    mgr._connector.supports_room_lookup = MagicMock(return_value=True)
    mgr._connector.room_ref_by_id = AsyncMock(
        return_value=RoomRef(id=room_id, kind=RoomKind.CHANNEL, name="eng"))


class TestAScheduledWakeRetriesPastAStaleRecord(unittest.IsolatedAsyncioTestCase):
    """`get_or_create` raises `StaleRecordError` when the record it read was
    reclaimed while it waited on the watcher lock; the contract is "the caller
    retries". Connector routing does; `inject_message` took it as a failed
    delivery and advanced `next_run` — for a date-anchored one-shot, to next
    year (Codex, PR #140). One re-read: the room now has no record, so the
    retry takes `_create`."""

    async def test_the_second_attempt_delivers(self):
        from gateway.core.watcher_manager import StaleRecordError

        mgr = make_bare_session_manager()
        mgr._connector_name = "rc"
        record = make_rule_derived_record(name="rc:eng", room_id="R1")
        processor = MagicMock()
        processor.enqueue = AsyncMock(return_value=True)
        mgr._lifecycle.record_for_room = MagicMock(return_value=record)
        mgr._lifecycle.processor_for_room = MagicMock(return_value=None)
        _serve_room(mgr, "R1")  # a dormant record resolves the room first (#145)
        mgr._watcher_manager = MagicMock()
        mgr._watcher_manager.get_or_create = AsyncMock(
            side_effect=[StaleRecordError("reclaimed while waiting"), processor])

        self.assertTrue(await mgr.inject_message("R1", "poke"))

        self.assertEqual(mgr._watcher_manager.get_or_create.await_count, 2)
        processor.enqueue.assert_awaited_once()

    async def test_a_second_stale_record_is_not_retried_forever(self):
        from gateway.core.watcher_manager import StaleRecordError

        mgr = make_bare_session_manager()
        mgr._connector_name = "rc"
        mgr._lifecycle.record_for_room = MagicMock(return_value=make_rule_derived_record(name="rc:eng", room_id="R1"))
        mgr._lifecycle.processor_for_room = MagicMock(return_value=None)
        _serve_room(mgr, "R1")  # a dormant record resolves the room first (#145)
        mgr._watcher_manager = MagicMock()
        mgr._watcher_manager.get_or_create = AsyncMock(side_effect=StaleRecordError("again"))

        with self.assertRaises(StaleRecordError):
            await mgr.inject_message("R1", "poke")
        self.assertEqual(mgr._watcher_manager.get_or_create.await_count, 2)

