"""The runtime half of the WatcherManager (§2.7, §2.8): the creation decision.

The lifecycle owns the start machinery and is mocked at the seam these tests pin
(`record_for_room` / `processor_named` / `start_watcher_in_room` / `get_watcher_state`
/ `save_state`); one end-to-end class runs the real lifecycle to catch the seam
itself drifting.

What the mocked half asserts is the *decision layer*: sticky binding beats rule
matching, a pause is a deliberate drop, single-flight covers check-plus-create,
the cap answers audibly, and the §5.3 record fields are frozen at creation.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from gateway.core.config import HistoryHandoffConfig
from gateway.core.room_pattern import RoomPattern
from gateway.core.state import CONFIG_SCHEMA_VERSION, WatcherState
from gateway.core.watcher_manager import (
    RoomRef,
    WatcherManager,
    config_from_record,
    materialize,
    rule_snapshot,
    watcher_label,
)
from gateway.core.watcher_rule import RoomKind, RoomMatcher, WatcherRule


def _rule(name="eng", connector="rc", agent="claude", include=("eng-*",),
          except_for=(), direct=False, group_direct=False, **kwargs):
    return WatcherRule(
        name=name,
        connector=connector,
        agent=agent,
        rooms=RoomMatcher(
            include=tuple(RoomPattern(p) for p in include),
            except_for=tuple(RoomPattern(p) for p in except_for),
            direct=direct,
            group_direct=group_direct,
        ),
        **kwargs,
    )


def _room(id="r1", kind=RoomKind.CHANNEL, name="eng-backend", participants=()):
    return RoomRef(id=id, kind=kind, name=name, participants=participants)


def _mock_lifecycle():
    """A lifecycle double whose seam behaves like the real one.

    `start_watcher_in_room` *applies* the provenance it is handed, exactly as
    the real seam does at construction, and registers the record. A double that
    swallowed provenance would let a manager that never sent any pass every
    test in this file — which is the shape of defect these tests exist for.
    """
    lifecycle = MagicMock()
    records: dict[str, WatcherState] = {}

    async def start(wc, state, room, history_before_ts=None, provenance=None):
        records[wc.name] = WatcherState(
            watcher_name=wc.name,
            session_id=state.session_id if state else "sess-new",
            room_id=room.id,
            room_type=room.type,
            room_name=room.name,
            last_processed_ts=state.last_processed_ts if state else "",
            **(provenance or {}),
        )

    lifecycle.records = records
    lifecycle.record_for_room = MagicMock(return_value=None)
    lifecycle.processor_named = MagicMock(return_value=None)
    lifecycle.resolve_agent_name = MagicMock(side_effect=lambda ref: ref or "default")
    lifecycle.start_watcher_in_room = AsyncMock(side_effect=start)
    lifecycle.get_watcher_state = MagicMock(side_effect=records.get)
    lifecycle.save_state = MagicMock()
    # Asked, not remembered: a bare MagicMock answers truthily, and the
    # manager's `_shutting_down` now reads the lifecycle's single transition
    # flag — a truthy mock would disarm every test's manager (the
    # bot_username lesson, again).
    lifecycle.transitions_disarmed = False
    # Same lesson for the reload flag (#144): a truthy mock would park every
    # offer in every test as "a reload is in progress".
    lifecycle.disarmed_for_reload = False
    lifecycle.disarm_reason = "the gateway is shutting down"

    def _disarm(reason="the gateway is shutting down"):
        lifecycle.transitions_disarmed = True
        lifecycle.disarm_reason = reason
        lifecycle.disarmed_for_reload = reason != "the gateway is shutting down"

    lifecycle.disarm_transitions = MagicMock(side_effect=_disarm)
    return lifecycle


def _manager(lifecycle=None, rules=None, connector=None, **kwargs):
    lifecycle = lifecycle if lifecycle is not None else _mock_lifecycle()
    connector = connector if connector is not None else MagicMock(
        send_text=AsyncMock())
    manager = WatcherManager(
        "rc", connector, lifecycle,
        rules if rules is not None else [_rule()], **kwargs,
    )
    return manager, lifecycle, connector


class TestACreationShowsSomeoneIsAnswering(unittest.IsolatedAsyncioTestCase):
    """Owner, 2026-09-03: a creation is the longest silence a sender sees after
    their message. A typing hint goes out once a rule has claimed the room —
    before the start — and a failed creation switches it off again (Rocket.Chat's
    indicator is a toggle; Mattermost clears its own)."""

    def _connector(self):
        return MagicMock(send_text=AsyncMock(), notify_typing=AsyncMock())

    async def test_the_hint_is_sent_after_the_match_and_before_the_start(self):
        order = []
        connector = self._connector()
        connector.notify_typing.side_effect = lambda rid, on: order.append(("typing", rid, on))
        manager, lifecycle, _ = _manager(connector=connector)

        async def record_start(wc, state, room, history_before_ts=None, provenance=None):
            order.append(("start", room.id))
        lifecycle.start_watcher_in_room = AsyncMock(side_effect=record_start)

        await manager.get_or_create("rc", _room())

        self.assertEqual(order[:2], [("typing", "r1", True), ("start", "r1")])

    async def test_a_room_no_rule_claims_gets_no_hint(self):
        connector = self._connector()
        manager, _, _ = _manager(connector=connector)

        self.assertIsNone(await manager.get_or_create("rc", _room(name="unrelated")))

        connector.notify_typing.assert_not_awaited()

    async def test_a_failed_start_switches_the_hint_off_and_still_raises(self):
        connector = self._connector()
        manager, lifecycle, _ = _manager(connector=connector)
        lifecycle.start_watcher_in_room = AsyncMock(side_effect=RuntimeError("backend down"))

        with self.assertRaises(RuntimeError):
            await manager.get_or_create("rc", _room())

        connector.notify_typing.assert_any_await("r1", False)

    async def test_a_connector_that_cannot_hint_does_not_block_the_creation(self):
        connector = self._connector()
        connector.notify_typing = AsyncMock(side_effect=OSError("socket closed"))
        manager, lifecycle, _ = _manager(connector=connector)
        lifecycle.start_watcher_in_room = AsyncMock()

        await manager.get_or_create("rc", _room())

        lifecycle.start_watcher_in_room.assert_awaited_once()


class TestANewRoomTakingARenamedAwayName(unittest.IsolatedAsyncioTestCase):
    """`#general` renamed to `#general-old` (nobody posts there again), then a
    fresh `#general` is created. The old record still holds `rc:general`, so the
    start refused the new room — and dropped its messages — until the old room
    spoke or was expired (final pre-merge review). The creation now asks the
    platform what the holder is called and moves its handle first."""

    def _setup(self, holder_now):
        connector = MagicMock(send_text=AsyncMock(), notify_typing=AsyncMock())
        connector.room_ref_by_id = AsyncMock(return_value=holder_now)
        manager, lifecycle, _ = _manager(connector=connector)
        lifecycle.room_holding = MagicMock(return_value="R-old")
        lifecycle.observe_room_name = AsyncMock(return_value="rc:eng-backend-old")
        lifecycle.start_watcher_in_room = AsyncMock()
        return manager, lifecycle, connector

    async def test_the_holders_handle_is_moved_before_the_start(self):
        manager, lifecycle, connector = self._setup(
            RoomRef(id="R-old", kind=RoomKind.CHANNEL, name="eng-backend-old"))

        await manager.get_or_create("rc", _room())

        connector.room_ref_by_id.assert_awaited_once_with("R-old")
        lifecycle.observe_room_name.assert_awaited_once_with("R-old", "eng-backend-old")
        lifecycle.start_watcher_in_room.assert_awaited_once()

    async def test_a_holder_that_really_has_the_same_name_is_left_to_the_refusal(self):
        """The observer is asked (it is a no-op for an unchanged name) and the
        start proceeds to its own refusal — nothing here re-points a handle."""
        manager, lifecycle, _ = self._setup(
            RoomRef(id="R-old", kind=RoomKind.CHANNEL, name="eng-backend"))
        lifecycle.observe_room_name = AsyncMock(return_value=None)

        await manager.get_or_create("rc", _room())

        lifecycle.observe_room_name.assert_awaited_once_with("R-old", "eng-backend")
        lifecycle.start_watcher_in_room.assert_awaited_once()

    async def test_a_holder_the_platform_cannot_describe_is_left_alone(self):
        manager, lifecycle, connector = self._setup(None)
        connector.room_ref_by_id = AsyncMock(side_effect=OSError("network"))

        await manager.get_or_create("rc", _room())

        lifecycle.observe_room_name.assert_not_awaited()

    async def test_no_holder_means_no_lookup(self):
        manager, lifecycle, connector = self._setup(None)
        lifecycle.room_holding = MagicMock(return_value=None)

        await manager.get_or_create("rc", _room())

        connector.room_ref_by_id.assert_not_awaited()


class TestAnOfferDuringAReload(unittest.IsolatedAsyncioTestCase):
    """#144: a reload's disarm parks an offer (retryable) where shutdown's
    declines it (final) — a declined offer remembers ids a kept connector
    would never forget."""

    async def test_a_reload_disarm_raises_a_retryable_error(self):
        from gateway.core.watcher_manager import ReloadInProgressError
        manager, lifecycle, _ = _manager()
        manager.disarm("a config reload is in progress — retry when it finishes")
        with self.assertRaises(ReloadInProgressError) as cm:
            await manager.get_or_create("rc", _room())
        self.assertIn("reload is in progress", str(cm.exception))
        lifecycle.start_watcher_in_room.assert_not_called()

    async def test_a_wake_parked_on_the_watcher_lock_parks_too(self):
        """The inner re-check under the watcher lock: a wake that waited out an
        idle-drop while a reload quiesced the manager must park like the outer
        checks, not decline and have its frames remembered."""
        from gateway.core.watcher_manager import ReloadInProgressError
        manager, lifecycle, _ = _manager()
        lifecycle.watcher_lock = lambda name: asyncio.Lock()
        record = WatcherState(watcher_name="rc:eng-backend", session_id="s1", room_id="r1",
                              rule_name="eng", config={"name": "rc:eng-backend"})
        manager.disarm("a config reload is in progress — retry when it finishes")
        with self.assertRaises(ReloadInProgressError):
            await manager._recreate(record, _room(), None)
        lifecycle.start_watcher_in_room.assert_not_called()

    async def test_a_shutdown_disarm_still_declines(self):
        manager, lifecycle, _ = _manager()
        manager.disarm()
        self.assertIsNone(await manager.get_or_create("rc", _room()))
        lifecycle.start_watcher_in_room.assert_not_called()


class TestCreation(unittest.IsolatedAsyncioTestCase):
    async def test_a_matching_rule_creates_a_watcher(self):
        manager, lifecycle, _ = _manager()
        started = {}

        async def record_start(wc, state, room, history_before_ts=None, provenance=None):
            started["wc"] = wc
            started["room"] = room
            started["history_before_ts"] = history_before_ts
            ws = WatcherState(watcher_name=wc.name, session_id="s1", room_id=room.id)
            lifecycle.get_watcher_state = MagicMock(return_value=ws)
            started["ws"] = ws
            lifecycle.processor_named = MagicMock(return_value="the-processor")

        lifecycle.start_watcher_in_room = AsyncMock(side_effect=record_start)

        result = await manager.get_or_create("rc", _room())

        self.assertEqual(result, "the-processor")
        self.assertEqual(started["wc"].name, "rc:eng-backend")
        self.assertIsNone(started["history_before_ts"])
        # The state passed for a first-ever room is None — there is no record.
        self.assertIsNone(lifecycle.start_watcher_in_room.call_args.args[1])

    async def test_the_snapshot_pin_declines_a_reclaimed_record(self):
        """Codex round 11: the boot loops walk a snapshot of hydrated
        records, and a live membership removal can reclaim one mid-walk —
        the unpinned re-read then found the room recordless and _create
        RESURRECTED a watcher for a room the bot just left. With the pin, a
        changed record declines instead."""
        manager, lifecycle, _ = _manager()
        snapshot_record = WatcherState(
            watcher_name="w1", session_id="s1", room_id="r1")
        # The reclaim, completed before the boot loop's call runs: the room
        # is recordless now.
        lifecycle.record_for_room = MagicMock(return_value=None)

        result = await manager.get_or_create(
            "rc", _room(), expected_record=snapshot_record)

        self.assertIsNone(result)
        lifecycle.start_watcher_in_room.assert_not_called()

    async def test_no_matching_rule_creates_nothing(self):
        manager, lifecycle, _ = _manager(rules=[_rule(include=("ops-*",))])
        result = await manager.get_or_create("rc", _room())
        self.assertIsNone(result)
        lifecycle.start_watcher_in_room.assert_not_called()

    async def test_the_trigger_timestamp_reaches_the_start(self):
        manager, lifecycle, _ = _manager()
        await manager.get_or_create(
            "rc", _room(), history_before_ts="2026-08-16T10:00:00+00:00")
        self.assertEqual(
            lifecycle.start_watcher_in_room.call_args.kwargs["history_before_ts"],
            "2026-08-16T10:00:00+00:00",
        )

    async def test_a_group_dm_room_is_typed_group_dm(self):
        """The platform Room the watcher subscribes with carries the classified
        kind, which is what puts a group DM on the mention-required side of the
        gate (§6.4) — RC's own resolver cannot tell the two DM kinds apart."""
        manager, lifecycle, _ = _manager(rules=[_rule(include=(), group_direct=True)])
        await manager.get_or_create(
            "rc", _room(id="g1", kind=RoomKind.GROUP_DM, name="",
                        participants=("alice", "bob")))
        platform_room = lifecycle.start_watcher_in_room.call_args.args[2]
        self.assertEqual(platform_room.type, "group_dm")
        self.assertEqual(platform_room.id, "g1")
        self.assertEqual(platform_room.name, "alice, bob")

    async def test_a_failed_creation_raises_rather_than_answering_none(self):
        """Inverted deliberately from 'a failed creation answers None' — a
        contract upgrade for the transaction (§2.2), not a reasoning reversal.
        None is a final answer (rule miss, pause); a creation that raised never
        carried out its decision, so the caller must be able to tell the two
        apart to retry one and not the other."""
        manager, lifecycle, _ = _manager()
        lifecycle.start_watcher_in_room = AsyncMock(side_effect=RuntimeError("boom"))

        with self.assertRaises(RuntimeError):
            await manager.get_or_create("rc", _room())
        lifecycle.save_state.assert_not_called()

        # The raise held no reservation: the lock and the cap slot released, so
        # the retry re-enters and succeeds.
        lifecycle.start_watcher_in_room = AsyncMock()
        await manager.get_or_create("rc", _room())
        lifecycle.start_watcher_in_room.assert_called_once()

    async def test_a_failed_recreation_raises_too(self):
        manager, lifecycle, _ = _manager()
        record = WatcherState(
            watcher_name="w", session_id="s1", room_id="r1",
            config={"name": "w", "connector": "rc", "room": "eng-backend",
                    "agent": "claude"})
        lifecycle.record_for_room = MagicMock(return_value=record)
        lifecycle.start_watcher_in_room = AsyncMock(side_effect=RuntimeError("boom"))

        with self.assertRaises(RuntimeError):
            await manager.get_or_create("rc", _room())
        # No activity stamp and no save for a recreation that never happened.
        self.assertEqual(record.last_activity_at, "")
        lifecycle.save_state.assert_not_called()

    async def test_a_recreation_against_a_reclaimed_record_raises_for_retry(self):
        """The expire-vs-wake race (§2.5): the record this wake dispatched
        against was reclaimed while it waited on the watcher lock. Raised, not
        None — None would remember the trigger as a decline, and the correct
        outcome for a reclaimed room is a retry that re-enters get_or_create
        and lands in _create, with this frame as the fresh watcher's trigger."""
        from gateway.core.watcher_manager import StaleRecordError

        manager, lifecycle, _ = _manager()
        record = WatcherState(
            watcher_name="w", session_id="s1", room_id="r1",
            config={"name": "w", "connector": "rc", "room": "eng-backend",
                    "agent": "claude"})
        # The dispatch read finds the record; the re-check under the lock
        # finds it reclaimed.
        lifecycle.record_for_room = MagicMock(side_effect=[record, None])

        with self.assertRaises(StaleRecordError):
            await manager.get_or_create("rc", _room())
        lifecycle.start_watcher_in_room.assert_not_called()

    async def test_a_disarmed_manager_declines_every_offer(self):
        """Shutdown disarms the manager before anything stops (§2.5): the wake
        arms stay reachable until the connector disconnects, and a creation
        mid-teardown outlives every stop that already ran. None-shaped — a
        final decline, so the drain drops and remembers, and the watermark
        stays put for the next boot."""
        manager, lifecycle, _ = _manager()

        manager.disarm()
        result = await manager.get_or_create("rc", _room())

        self.assertIsNone(result)
        lifecycle.start_watcher_in_room.assert_not_called()
        lifecycle.record_for_room.assert_not_called()

    async def test_a_wrong_connector_is_refused(self):
        manager, lifecycle, _ = _manager()
        result = await manager.get_or_create("mm", _room())
        self.assertIsNone(result)
        lifecycle.start_watcher_in_room.assert_not_called()


class TestTheRecordIsFrozenAtCreation(unittest.IsolatedAsyncioTestCase):
    """§5.3's on-the-fly fields exist so the record is self-sufficient: recreation
    reads `config`, drift detection reads `rule`, and neither consults the live
    config. All of them are known only at the creation moment."""

    async def test_every_creation_only_field_is_populated(self):
        rule = _rule()
        manager, lifecycle, _ = _manager(rules=[rule])
        room = _room()

        await manager.get_or_create("rc", room)
        ws = lifecycle.get_watcher_state("rc:eng-backend")

        self.assertEqual(ws.room_kind, "channel")
        self.assertEqual(ws.connector, "rc")
        self.assertEqual(ws.agent, "claude")
        self.assertEqual(ws.rule_name, "eng")
        self.assertEqual(ws.rule, rule_snapshot(rule))
        self.assertEqual(ws.config_schema_version, CONFIG_SCHEMA_VERSION)
        self.assertTrue(ws.created_at)
        self.assertEqual(ws.created_at, ws.last_activity_at)
        # The frozen config round-trips into the exact WatcherConfig recreation needs.
        self.assertEqual(config_from_record(ws), materialize(rule, room))
        lifecycle.save_state.assert_called_once()

    async def test_participants_are_kept_for_a_group_dm(self):
        manager, lifecycle, _ = _manager(rules=[_rule(include=(), group_direct=True)])
        room = _room(id="g1", kind=RoomKind.GROUP_DM, name="",
                     participants=("alice", "bob"))

        await manager.get_or_create("rc", room)

        # Asked for by the label the product derives, not by a second copy of
        # the label rule spelled out here.
        ws = lifecycle.get_watcher_state(watcher_label("rc", room))
        self.assertEqual(ws.participants, ["alice", "bob"])
        self.assertEqual(ws.room_kind, "group_dm")


class TestStickyBinding(unittest.IsolatedAsyncioTestCase):
    """A room with a record is recreated from its own persisted config (§2.4);
    the current rules are never consulted."""

    def _record(self, paused=False, config=None):
        return WatcherState(
            watcher_name="rc:eng-backend", session_id="s1", room_id="r1",
            paused=paused,
            config=config if config is not None
            else {"name": "rc:eng-backend", "connector": "rc",
                  "room": "eng-backend", "agent": "claude",
                  "context_inject_files": [],
                  "history_handoff": {"enabled": True, "fetch_count": 50,
                                      "verbatim_tail": 15, "max_fetch_count": 200}},
        )

    async def test_a_resident_watcher_is_returned_not_recreated(self):
        manager, lifecycle, _ = _manager()
        lifecycle.record_for_room = MagicMock(return_value=self._record())
        lifecycle.processor_named = MagicMock(return_value="resident")

        result = await manager.get_or_create("rc", _room())

        self.assertEqual(result, "resident")
        lifecycle.start_watcher_in_room.assert_not_called()

    async def test_an_idle_record_is_recreated_from_its_config_not_the_rules(self):
        # The rule list would DENY this room — sticky binding must win.
        manager, lifecycle, _ = _manager(rules=[])
        record = self._record()
        lifecycle.record_for_room = MagicMock(return_value=record)

        await manager.get_or_create("rc", _room())

        lifecycle.start_watcher_in_room.assert_called_once()
        wc = lifecycle.start_watcher_in_room.call_args.args[0]
        self.assertEqual(wc.name, "rc:eng-backend")
        self.assertEqual(wc.agent, "claude")
        # The record itself rides along, so the session is resumed, not re-minted.
        self.assertIs(lifecycle.start_watcher_in_room.call_args.args[1], record)

    async def test_a_paused_record_is_a_deliberate_drop(self):
        """§4.4: an explicit pause is never overridden by inference. A message
        for a paused room creates nothing and unpauses nothing."""
        manager, lifecycle, _ = _manager()
        lifecycle.record_for_room = MagicMock(return_value=self._record(paused=True))

        result = await manager.get_or_create("rc", _room())

        self.assertIsNone(result)
        lifecycle.start_watcher_in_room.assert_not_called()

    async def test_a_record_with_no_frozen_config_is_left_to_the_static_path(self):
        manager, lifecycle, _ = _manager()
        lifecycle.record_for_room = MagicMock(return_value=self._record(config={}))

        result = await manager.get_or_create("rc", _room())

        self.assertIsNone(result)
        lifecycle.start_watcher_in_room.assert_not_called()


class TestSingleFlight(unittest.IsolatedAsyncioTestCase):
    async def test_two_concurrent_offers_for_one_room_create_once(self):
        """§2.7 step 4: the lock covers the existence check and the creation
        together. The loser of the race must observe the winner's watcher, not
        start a second creation."""
        manager, lifecycle, _ = _manager()
        release = asyncio.Event()
        starts = []

        async def slow_start(wc, state, room, history_before_ts=None, provenance=None):
            starts.append(wc.name)
            await release.wait()
            ws = WatcherState(watcher_name=wc.name, session_id="s", room_id=room.id)
            lifecycle.get_watcher_state = MagicMock(return_value=ws)
            # What a finished creation looks like to the second caller:
            lifecycle.record_for_room = MagicMock(return_value=ws)
            lifecycle.processor_named = MagicMock(return_value="the-processor")

        lifecycle.start_watcher_in_room = AsyncMock(side_effect=slow_start)

        first = asyncio.create_task(manager.get_or_create("rc", _room()))
        second = asyncio.create_task(manager.get_or_create("rc", _room()))
        await asyncio.sleep(0)  # both tasks reach the lock
        release.set()
        results = await asyncio.gather(first, second)

        self.assertEqual(starts, ["rc:eng-backend"], "exactly one creation ran")
        self.assertEqual(results[1], "the-processor")

    async def test_two_different_rooms_create_concurrently(self):
        """The lock is per room — one room's slow creation must not serialize
        every other room behind it."""
        manager, lifecycle, _ = _manager(
            rules=[_rule(include=("eng-*",))])
        gate_a = asyncio.Event()
        in_start = asyncio.Event()

        async def start(wc, state, room, history_before_ts=None, provenance=None):
            if room.id == "ra":
                in_start.set()
                await gate_a.wait()

        lifecycle.start_watcher_in_room = AsyncMock(side_effect=start)

        task_a = asyncio.create_task(
            manager.get_or_create("rc", _room(id="ra", name="eng-a")))
        await in_start.wait()
        # Room B completes while room A is still inside its creation.
        await asyncio.wait_for(
            manager.get_or_create("rc", _room(id="rb", name="eng-b")), timeout=1)
        gate_a.set()
        await task_a


class TestTheCreationCap(unittest.IsolatedAsyncioTestCase):
    async def test_over_cap_answers_with_a_visible_notice_not_a_silent_drop(self):
        manager, lifecycle, connector = _manager(creation_cap=1)
        release = asyncio.Event()

        async def slow_start(wc, state, room, history_before_ts=None, provenance=None):
            await release.wait()

        lifecycle.start_watcher_in_room = AsyncMock(side_effect=slow_start)

        first = asyncio.create_task(
            manager.get_or_create("rc", _room(id="ra", name="eng-a")))
        await asyncio.sleep(0)

        result = await manager.get_or_create("rc", _room(id="rb", name="eng-b"))

        self.assertIsNone(result)
        connector.send_text.assert_awaited_once()
        self.assertEqual(connector.send_text.await_args.args[0], "rb")

        release.set()
        await first

    async def test_a_failed_notice_does_not_raise_out_of_routing(self):
        manager, lifecycle, connector = _manager(creation_cap=0)
        connector.send_text = AsyncMock(side_effect=RuntimeError("rest down"))

        result = await manager.get_or_create("rc", _room())
        self.assertIsNone(result)

    async def test_the_cap_releases_when_a_creation_finishes(self):
        manager, lifecycle, _ = _manager(creation_cap=1)
        await manager.get_or_create("rc", _room(id="ra", name="eng-a"))
        await manager.get_or_create("rc", _room(id="rb", name="eng-b"))
        self.assertEqual(lifecycle.start_watcher_in_room.await_count, 2)


class TestConfigFromRecord(unittest.TestCase):
    def test_round_trips_a_materialized_config(self):
        rule = _rule()
        wc = materialize(rule, _room())
        from gateway.core.watcher_manager import _jsonable

        record = WatcherState(
            watcher_name=wc.name, session_id="s", room_id="r1", config=_jsonable(wc))
        self.assertEqual(config_from_record(record), wc)

    def test_an_empty_config_is_not_recreatable(self):
        record = WatcherState(watcher_name="w", session_id="s", room_id="r1")
        self.assertIsNone(config_from_record(record))

    def test_a_nameless_config_is_not_recreatable(self):
        record = WatcherState(
            watcher_name="w", session_id="s", room_id="r1",
            config={"connector": "rc"})
        self.assertIsNone(config_from_record(record))

    def test_identity_fields_rebuild_from_the_records_frozen_columns(self):
        """Codex rounds 14+15 (P1s): the nested config's values are
        untypechecked, and a corrupted `config.agent`/`config.name` —
        missing, wrongly typed, or a VALID-BUT-DIFFERENT value — must never
        win over the record's top-level frozen columns. A different nested
        name forked a second record for one room (expiring the original then
        deleted the live session); a different nested agent ran the room
        under another backend and tool policy, silently."""
        for garbled_agent in (None,                      # missing
                              ["not", "a", "string"],    # wrong type
                              "another-valid-agent"):    # conflicting value
            with self.subTest(agent=garbled_agent):
                config = {"name": "a-conflicting-name", "connector": "rc",
                          "room": "x"}
                if garbled_agent is not None:
                    config["agent"] = garbled_agent
                record = WatcherState(
                    watcher_name="the-frozen-name", session_id="s",
                    room_id="r1", agent="the-frozen-agent", config=config)
                wc = config_from_record(record)
                self.assertEqual(wc.agent, "the-frozen-agent",
                                 "the frozen agent column wins, always")
                self.assertEqual(wc.name, "the-frozen-name",
                                 "the frozen name column wins, always")

    def test_wrongly_typed_handoff_values_fall_back_to_defaults(self):
        record = WatcherState(
            watcher_name="w", session_id="s", room_id="r1",
            config={"name": "w", "connector": "rc", "room": "x", "agent": "a",
                    "history_handoff": {"enabled": "yes", "fetch_count": "many"}})
        wc = config_from_record(record)
        self.assertEqual(wc.history_handoff, HistoryHandoffConfig())


class TestEveryStateFieldIsClassified(unittest.TestCase):
    """A start rebuilds a `WatcherState` from scratch, so every field is either
    rebuilt by that start or carried into it. A field in neither set is one a
    recreation silently drops — which is how the frozen rule snapshot was wiped,
    leaving the next boot to prune the record as an orphan.

    Enumerated rather than listed, so the *next* §5.3 field cannot be added
    without deciding which side it is on."""

    def test_no_field_is_unclassified_or_double_classified(self):
        from dataclasses import fields as dataclass_fields

        from gateway.core.state import (
            FROZEN_AT_CREATION_FIELDS,
            LIFECYCLE_CLOCK_FIELDS,
            SESSION_SCOPED_FIELDS,
        )

        declared = {f.name for f in dataclass_fields(WatcherState)}
        sets = (SESSION_SCOPED_FIELDS, FROZEN_AT_CREATION_FIELDS,
                LIFECYCLE_CLOCK_FIELDS)
        classified = set().union(*sets)

        self.assertEqual(
            declared - classified, set(),
            "a WatcherState field is classified neither rebuilt nor carried — "
            "a recreation would silently drop it",
        )
        self.assertEqual(classified - declared, set(), "a classified field no longer exists")
        for i, first in enumerate(sets):
            for second in sets[i + 1:]:
                self.assertEqual(first & second, set(), "a field is in two sets")

    def test_carried_fields_covers_everything_a_start_does_not_rebuild(self):
        from gateway.core.state import carried_fields

        record = WatcherState(watcher_name="w", session_id="s", room_id="r1",
                              rule_name="eng", created_at="then")
        carried = carried_fields(record)
        self.assertEqual(carried["rule_name"], "eng")
        self.assertEqual(carried["created_at"], "then")
        self.assertNotIn("session_id", carried, "a start rebuilds this")

    def test_a_missing_record_carries_nothing(self):
        from gateway.core.state import carried_fields

        self.assertEqual(carried_fields(None), {})


class TestEndToEndThroughTheRealLifecycle(unittest.IsolatedAsyncioTestCase):
    """One pass with nothing mocked between the manager and the lifecycle, so a
    drift in the seam's signature or in what `_start_watcher` builds fails here
    rather than in production. The connector and agent are still doubles."""

    def _real_lifecycle(self):
        from unittest.mock import patch as _patch

        from gateway.core.dispatch import MessageDispatcher
        from gateway.core.injected_context_builder import InjectedContextBuilder
        from gateway.core.session_maps import SessionMaps
        from tests.helpers import MockAgentBackend, make_core_config, make_lifecycle

        connector = MagicMock()
        connector.agent_username = "hammer-mei"
        connector.subscribe_room = AsyncMock()
        connector.unsubscribe_room = AsyncMock()
        connector.fetch_room_history = AsyncMock(return_value=[])
        connector.get_last_processed_ts = MagicMock(return_value=None)
        connector.update_last_processed_ts = MagicMock()
        connector.attachment_cache_dir = MagicMock(return_value=None)
        connector.send_to_room = AsyncMock()

        config = make_core_config()
        lifecycle = make_lifecycle(
            connector=connector,
            agents={"default": MockAgentBackend()},
            config=config,
            dispatcher=MessageDispatcher(connector),
            injector=InjectedContextBuilder(config),
            maps=SessionMaps(),
            state_store=MagicMock(load=MagicMock(return_value={}),
                                  save=MagicMock()),
        )
        lifecycle._attachment_workspace = MagicMock(
            setup=MagicMock(return_value="/tmp/fake"))
        return lifecycle, connector, _patch

    async def test_a_creation_runs_the_real_start_and_freezes_a_real_record(self):
        lifecycle, connector, _patch = self._real_lifecycle()
        rule = _rule(agent="default")
        manager = WatcherManager("rc", connector, lifecycle, [rule])
        room = _room()

        with _patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            result = await manager.get_or_create(
                "rc", room, history_before_ts="2026-08-16T10:00:00+00:00")

        self.assertIsNotNone(result)
        ws = lifecycle.get_watcher_state("rc:eng-backend")
        self.assertEqual(ws.room_id, "r1")
        self.assertEqual(ws.rule_name, "eng")
        self.assertEqual(config_from_record(ws), materialize(rule, room))
        self.assertTrue(ws.session_id, "a session was provisioned")
        # The trigger bound reached the real history fetch.
        self.assertEqual(
            connector.fetch_room_history.call_args.kwargs.get("before_ts"),
            "2026-08-16T10:00:00+00:00",
        )
        # And the record is queryable through the sticky-binding lookup.
        self.assertIs(lifecycle.record_for_room("r1"), ws)

    async def test_a_recreation_keeps_the_frozen_record_intact(self):
        """The test the mocked seam could not give: a start builds a *fresh*
        `WatcherState`, so a recreation that does not carry the frozen fields
        wipes the snapshot recreation reads — and the next boot prunes the
        emptied record as an orphan, discarding the session with it.

        Two restarts and a room's continuity was gone, silently. Nothing in the
        mocked-seam suite could see it, because there the seam is a mock."""
        lifecycle, connector, _patch = self._real_lifecycle()
        rule = _rule(agent="default")
        manager = WatcherManager("rc", connector, lifecycle, [rule])
        room = _room()

        with _patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            await manager.get_or_create("rc", room)
            created = lifecycle.get_watcher_state("rc:eng-backend")
            frozen = {
                "rule_name": created.rule_name,
                "rule": dict(created.rule),
                "config": dict(created.config),
                "room_kind": created.room_kind,
                "connector": created.connector,
                "agent": created.agent,
                "created_at": created.created_at,
                "config_schema_version": created.config_schema_version,
            }
            session_id = created.session_id
            # What an idle drop leaves behind: the record, no processor.
            await lifecycle.stop_all()

            recreated_proc = await manager.get_or_create("rc", room)

        self.assertIsNotNone(recreated_proc)
        ws = lifecycle.get_watcher_state("rc:eng-backend")
        for name, value in frozen.items():
            self.assertEqual(getattr(ws, name), value,
                             f"recreation dropped the frozen field {name!r}")
        self.assertEqual(ws.session_id, session_id, "the session was resumed, not re-minted")
        # And the record still reads as rule-derived, so the next boot keeps it.
        self.assertTrue(ws.rule_name)
        self.assertEqual(config_from_record(ws), materialize(rule, room))

    async def test_a_recreation_replays_the_interval_its_room_owes(self):
        """What makes an abort recoverable for a room with a record (§2.2).

        The routing episode that parked, or the buffer that overflowed, left
        its frames below the record's watermark — and nothing else would ever
        return to them: the reconnect replay iterates *tracked* rooms and a
        parked room is untracked, so the next live message's commit would seal
        the interval by advancing the watermark past it.

        Bounded by the record's own mark, and named explicitly so the room's
        replay boundary is not spent by a window it did not set.
        """
        lifecycle, connector, _patch = self._real_lifecycle()
        connector.replay_room_since = AsyncMock()
        manager = WatcherManager("rc", connector, lifecycle, [_rule(agent="default")])

        with _patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            await manager.get_or_create("rc", _room())
            ws = lifecycle.get_watcher_state("rc:eng-backend")
            ws.last_processed_ts = "1786874400000"   # what the room had reached
            await lifecycle.stop_all()

            await manager.get_or_create("rc", _room())

        connector.replay_room_since.assert_awaited_once_with(
            "r1", after_ts="1786874400000")

    async def test_a_first_ever_creation_replays_nothing(self):
        """There is no window behind it — the accepted residual, not a bug."""
        lifecycle, connector, _patch = self._real_lifecycle()
        connector.replay_room_since = AsyncMock()
        manager = WatcherManager("rc", connector, lifecycle, [_rule(agent="default")])

        with _patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            await manager.get_or_create("rc", _room())

        connector.replay_room_since.assert_not_awaited()

    async def test_a_failed_replay_does_not_undo_a_successful_recreation(self):
        lifecycle, connector, _patch = self._real_lifecycle()
        connector.replay_room_since = AsyncMock(side_effect=RuntimeError("rest down"))
        manager = WatcherManager("rc", connector, lifecycle, [_rule(agent="default")])

        with _patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            await manager.get_or_create("rc", _room())
            lifecycle.get_watcher_state("rc:eng-backend").last_processed_ts = "1000"
            await lifecycle.stop_all()

            with self.assertLogs(
                "agent-chat-gateway.core.watcher_manager", "WARNING"
            ):
                proc = await manager.get_or_create("rc", _room())

        self.assertIsNotNone(proc, "the room is up either way")

    async def test_a_resumed_session_bounds_its_handoff_at_the_records_watermark(self):
        """A13. If the backend expired the session during the downtime — which
        is precisely the long-downtime case a startup replay exists for — the
        recreation mints a fresh one and the handoff fetches history. Unbounded,
        that fetch pulls in the very interval the replay is about to deliver,
        and the agent sees it twice: once inside a discarded history turn, once
        as live prompts."""
        lifecycle, connector, _patch = self._real_lifecycle()
        connector.replay_room_since = AsyncMock()
        manager = WatcherManager("rc", connector, lifecycle, [_rule(agent="default")])

        with _patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            await manager.get_or_create("rc", _room())
            ws = lifecycle.get_watcher_state("rc:eng-backend")
            ws.last_processed_ts = "1786874400000"
            # The backend forgot the session while the room was idle.
            ws.session_id = ""
            await lifecycle.stop_all()
            connector.fetch_room_history.reset_mock()

            await manager.get_or_create("rc", _room())

        self.assertEqual(
            connector.fetch_room_history.call_args.kwargs.get("before_ts"),
            "1786874400000",
        )

    async def test_the_tighter_of_the_two_handoff_bounds_wins(self):
        """The case that distinguishes `_earlier` from `or`, and which the test
        above cannot see because it passes no trigger bound at all.

        A trigger is by construction a message *above* the watermark, and
        `before_ts` is an exclusive upper bound — so the trigger's bound fetches
        **more** history, not less. Preferring it, as `or` does whenever a
        trigger exists (i.e. on every message-path recreation), re-admits the
        whole interval the replay is about to deliver: the double delivery the
        bound exists to prevent.
        """
        lifecycle, connector, _patch = self._real_lifecycle()
        connector.replay_room_since = AsyncMock()
        manager = WatcherManager("rc", connector, lifecycle, [_rule(agent="default")])

        with _patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            await manager.get_or_create("rc", _room())
            ws = lifecycle.get_watcher_state("rc:eng-backend")
            ws.last_processed_ts = "1786874400000"   # the room's own boundary
            ws.session_id = ""                        # backend forgot the session
            await lifecycle.stop_all()
            connector.fetch_room_history.reset_mock()

            # A trigger, above the watermark, as every real one is.
            await manager.get_or_create(
                "rc", _room(), history_before_ts="1786888888888")

        self.assertEqual(
            connector.fetch_room_history.call_args.kwargs.get("before_ts"),
            "1786874400000",
            "the record's watermark is the tighter bound and must win over the "
            "trigger's, which sits above it",
        )

    async def test_a_recreation_carries_the_idle_clock_and_clears_dropped_at(self):
        """Deliberately inverted (the sixth in this branch): this test used to
        assert recreation *advances* `last_activity_at`, and internal review
        showed that to be §2.5's condemned boot-time mutation — the boot
        evaluation recreates every was-active record at every start, so the
        stamp meant a deployment restarted more often than its idle TTL never
        idled anything, silently. A recreation is residency, not activity;
        when it *is* activity (a wake), the triggering message's enqueue
        stamps the clock moments later at its one advancing write site.
        """
        lifecycle, connector, _patch = self._real_lifecycle()
        manager = WatcherManager("rc", connector, lifecycle, [_rule(agent="default")])

        with _patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            await manager.get_or_create("rc", _room())
            ws = lifecycle.get_watcher_state("rc:eng-backend")
            ws.last_activity_at = "2020-01-01T00:00:00+00:00"
            ws.dropped_at = "2020-01-02T00:00:00+00:00"
            await lifecycle.stop_all()

            await manager.get_or_create("rc", _room())

        ws = lifecycle.get_watcher_state("rc:eng-backend")
        self.assertEqual(
            ws.last_activity_at, "2020-01-01T00:00:00+00:00",
            "a restart is not activity — the idle clock survives recreation",
        )
        self.assertEqual(ws.dropped_at, "", "a resident room is not dropped")

    async def test_a_second_message_reuses_the_watcher_it_created(self):
        lifecycle, connector, _patch = self._real_lifecycle()
        manager = WatcherManager("rc", connector, lifecycle, [_rule(agent="default")])

        with _patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            first = await manager.get_or_create("rc", _room())
            second = await manager.get_or_create("rc", _room())

        self.assertIs(first, second, "one watcher, not two")
        connector.subscribe_room.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
