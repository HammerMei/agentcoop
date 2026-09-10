"""A job's `room_id` is its identity: the wake resolves through it, and so does the reply.

A job used to name only a watcher HANDLE (`<connector>:<room label>`), which is a
pure function of (connector, room) and moves when the room is renamed. The design
doc already called it "cosmetic and free to change".

The first attempt at fixing this was reverted (8584029) for three defects, and
this file is built to fail on each of them:

* the reply was addressed by re-reading the record BY HANDLE after the wake, so
  a watcher resurrected under a new label produced `room.id == ""` — the agent
  ran a full turn and the reply went nowhere, while `enqueue` returning True made
  the fire count as a success and burn a finite job's `run_count`;
* the handle was consulted BEFORE the id, so a job holding a correct id could
  deliver into whichever room had taken its handle over;
* `room_id` never reached `jobs.json` at all (see test_job_store_roundtrip.py).

**Every test here uses `make_bare_session_manager`** rather than a hand-built
`SessionManager.__new__`. That is not tidiness: the hand-built stub in the
reverted attempt answered `get_watcher_state` from a list that ignored the name
argument, which asserted the post-wake re-read succeeds instead of testing
whether it can — and that is precisely the mechanism that hid the first defect.
The shared builder is guarded by its own drift test against a real instance.

Run with:
    uv run python -m pytest tests/unit/test_job_room_identity.py -v
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock

from gateway.core.connector import Connector
from gateway.core.state import WatcherState
from gateway.core.watcher_manager import RoomRef
from gateway.core.watcher_rule import RoomKind
from gateway.schedule_types import ScheduledJob
from tests.helpers import make_bare_session_manager


def _record(name="rc:general", room_id="room-1", **kw):
    """A real `WatcherState`, not a MagicMock.

    A MagicMock's `.room_id` is truthy whatever attribute production reads, which
    makes the `record is not None and record.room_id` branch un-falsifiable — the
    reverted attempt's tests had exactly that hole.
    """
    defaults = dict(
        watcher_name=name, session_id="", room_id=room_id, room_name="general",
        room_type="channel", room_kind="channel",
    )
    return WatcherState(**{**defaults, **kw})


def _manager(*, record=None, resolved=None, record_for_room=None,
             resident=None):
    """A manager wired for `inject_message` and `resolve_handle` only.

    **Every lookup honours its argument.** `get_watcher_state` is keyed by
    watcher name (it is reached only through `resolve_handle`), `record_for_room`
    by the given record's OWN `room_id`, and `resident` maps a ROOM ID to an
    already-running processor — so asking for the wrong handle or the wrong room
    misses the way it really would. A `return_value` mock that answers the same
    for any argument cannot fail the test it exists for: the whole defect class
    this file covers is production reading the right value from the wrong key.

    `inject_message` takes a room id and nothing else now, so the by-handle
    paths these tests used to exercise inside it have moved to `resolve_handle`
    and to the scheduler's `_resolve_target` (test_scheduler.py).
    """
    manager = make_bare_session_manager()
    processor = MagicMock()
    processor.enqueue = AsyncMock(return_value=True)
    manager._injected_processor = processor  # type: ignore[attr-defined]

    by_name = {record.watcher_name: record} if record is not None else {}
    manager._lifecycle.get_watcher_state = MagicMock(side_effect=by_name.get)
    by_room = ({record_for_room.room_id: record_for_room}
               if record_for_room is not None else {})
    manager._lifecycle.record_for_room = MagicMock(side_effect=by_room.get)
    manager._lifecycle.processor_for_room = MagicMock(
        side_effect=(resident or {}).get)
    manager._watcher_manager = MagicMock()
    manager._watcher_manager.get_or_create = AsyncMock(return_value=processor)
    manager._connector = MagicMock()
    manager._connector.room_ref_by_id = AsyncMock(return_value=resolved)
    return manager

def _enqueued_room(manager):
    processor = manager._injected_processor
    processor.enqueue.assert_awaited_once()
    return processor.enqueue.await_args.args[0].room


class TestTheContract(unittest.TestCase):
    def test_the_base_answers_none_rather_than_raising(self):
        import asyncio

        self.assertIsNone(
            asyncio.run(Connector.room_ref_by_id(object(), "r1"))  # type: ignore[arg-type]
        )

    def test_it_returns_a_room_ref_so_kind_and_participants_survive(self):
        import inspect

        annotation = str(inspect.signature(Connector.room_ref_by_id).return_annotation)
        self.assertIn("RoomRef", annotation)


class TestTheJobCarriesTheRoom(unittest.TestCase):
    def test_room_id_is_empty_on_a_job_that_predates_the_field(self):
        self.assertEqual(ScheduledJob(watcher="rc:general").room_id, "")

    def test_the_handle_is_kept_alongside_it(self):
        job = ScheduledJob(watcher="rc:general", connector="rc", room_id="room-1")
        self.assertEqual((job.watcher, job.room_id), ("rc:general", "room-1"))


class TestTheReplyIsAddressedFromTheSameResolution(unittest.IsolatedAsyncioTestCase):
    """Defect 1 of the reverted attempt, pinned at the point it was observable.

    The room that ends up on the enqueued message is what decides where the
    agent's answer goes. Asserting the wake happened is not enough — the reverted
    version's wake happened correctly and the reply still went nowhere.
    """

    RESOLVED = RoomRef(id="room-1", kind=RoomKind.CHANNEL, name="daily-standup")

    async def test_a_resurrected_room_addresses_the_reply_by_id(self):
        """The room was renamed, so the recreated watcher's handle differs from
        the job's. The reply must still be addressed to the room."""
        manager = _manager(record=None, resolved=self.RESOLVED)

        result = await manager.inject_message("room-1", "poke")

        self.assertTrue(result)
        room = _enqueued_room(manager)
        self.assertEqual(room.id, "room-1", "the reply had nowhere to go")
        self.assertEqual(room.name, "daily-standup", "the room's CURRENT name")

    async def test_the_room_id_is_never_empty_on_an_injected_message(self):
        """The specific observable the reverted version produced: `room.id == ""`
        with `enqueue` returning True, so the fire counted as a success."""
        manager = _manager(record=None, resolved=self.RESOLVED)

        await manager.inject_message("room-1", "poke")

        self.assertNotEqual(_enqueued_room(manager).id, "")

    async def test_a_resident_watcher_is_addressed_from_its_record(self):
        """The ordinary path: the watcher is RUNNING, so its record describes
        the room it is serving and no connector round trip happens. A record
        with no resident processor is a recreation and does resolve first
        (#145 — test_recreation_resolves_room_scope.py)."""
        record = _record()
        resident = MagicMock()
        resident.enqueue = AsyncMock(return_value=True)
        manager = _manager(record=record, record_for_room=record,
                           resident={"room-1": resident})

        await manager.inject_message("room-1", "poke")

        manager._connector.room_ref_by_id.assert_not_awaited()
        manager._watcher_manager.get_or_create.assert_not_awaited()
        resident.enqueue.assert_awaited_once()
        self.assertEqual(resident.enqueue.await_args.args[0].room.id, "room-1")

    async def test_an_empty_room_id_is_a_programming_error_not_a_lookup(self):
        """`inject_message` used to take a handle first and fall back to it when
        no id was supplied — the fallback IS the defect class this file exists
        for. There is no fallback now: a caller with only a handle resolves it
        through `resolve_handle` first, and an empty id here is refused loudly."""
        manager = _manager(record=None, resolved=None)

        with self.assertRaises(ValueError):
            await manager.inject_message("", "poke")
        manager._injected_processor.enqueue.assert_not_awaited()

    async def test_an_unresolvable_room_with_no_resident_is_refused(self):
        manager = _manager(record=None, resolved=None)

        result = await manager.inject_message("room-1", "poke")

        self.assertFalse(result)
        manager._injected_processor.enqueue.assert_not_awaited()


class TestTheInjectedMessageIsRecognisableAsScheduled(unittest.IsolatedAsyncioTestCase):
    """The connectors render `to: me` and the turn runner warns on a silent
    reply by recognising the sender the injection stamps. One constant, checked
    here at the source so the three cannot drift apart."""

    async def test_the_sender_is_the_scheduler_constant(self):
        from gateway.core.connector import SCHEDULER_SENDER_ID, is_scheduled_message

        manager = _manager(record=None,
                           resolved=RoomRef(id="room-1", kind=RoomKind.CHANNEL, name="g"))

        await manager.inject_message("room-1", "poke")

        msg = manager._injected_processor.enqueue.await_args.args[0]
        self.assertEqual(msg.sender.id, SCHEDULER_SENDER_ID)
        self.assertTrue(is_scheduled_message(msg))


class TestTheIdOutranksTheHandle(unittest.IsolatedAsyncioTestCase):
    """Defect 2. A handle can come to mean a different room; an id cannot."""

    async def test_a_record_under_the_same_handle_but_another_room_is_not_used(self):
        """Room B took over the handle `rc:general` after room A expired. The job
        still targets room A by id, and must not be delivered into B."""
        room_b = _record(name="rc:general", room_id="room-B")
        resolved_a = RoomRef(id="room-A", kind=RoomKind.CHANNEL, name="eng")
        manager = _manager(
            record=room_b,            # findable by handle
            record_for_room=None,     # but not bound to room A
            resolved=resolved_a,
        )

        await manager.inject_message("room-A", "poke")

        room = _enqueued_room(manager)
        self.assertEqual(room.id, "room-A", "delivered into the wrong room")
        manager._lifecycle.record_for_room.assert_called_once_with("room-A")

    async def test_a_handle_resolves_to_whatever_room_currently_answers_to_it(self):
        """A job that predates the field has only its handle. `resolve_handle`
        is the ONE place it is turned into a room id — and the answer is the
        room CURRENTLY under that name, which is the best a handle can do."""
        manager = _manager(record=_record())

        self.assertEqual(manager.resolve_handle("rc:general"), "room-1")
        manager._lifecycle.get_watcher_state.assert_called_once_with("rc:general")

    async def test_a_handle_nobody_answers_to_resolves_to_nothing(self):
        manager = _manager(record=None)

        self.assertEqual(manager.resolve_handle("rc:general"), "")


class TestTheWakeGoesThroughTheOneEntryPoint(unittest.IsolatedAsyncioTestCase):
    """Pause, the creation cap and the rule match are all decided inside
    `get_or_create`, so a job cannot reach a room a message could not."""

    async def test_creation_goes_through_get_or_create(self):
        resolved = RoomRef(id="room-1", kind=RoomKind.CHANNEL, name="general")
        manager = _manager(record=None, resolved=resolved)

        await manager.inject_message("room-1", "poke")

        manager._watcher_manager.get_or_create.assert_awaited_once()
        _, room = manager._watcher_manager.get_or_create.await_args.args
        self.assertEqual(room, resolved)

    async def test_a_refused_creation_injects_nothing(self):
        """`get_or_create` answers None for a paused record, a room no rule
        claims, and a creation over the cap. All three land here."""
        resolved = RoomRef(id="room-1", kind=RoomKind.CHANNEL, name="general")
        manager = _manager(record=None, resolved=resolved)
        manager._watcher_manager.get_or_create = AsyncMock(return_value=None)

        result = await manager.inject_message("room-1", "poke")

        self.assertFalse(result)
        manager._injected_processor.enqueue.assert_not_awaited()


class TestTheTwoFailureShapesStayApart(unittest.IsolatedAsyncioTestCase):
    """`None` is permanent, a raise is transient — the caller acts differently."""

    async def test_a_permanent_absence_says_it_is_final(self):
        import logging

        manager = _manager(record=None, resolved=None)

        with self.assertLogs("coop.core.session_manager",
                             level=logging.INFO) as logs:
            await manager.inject_message("room-1", "poke")

        self.assertTrue([m for m in logs.output if "final, not a retry" in m],
                        logs.output)

    async def test_a_transport_failure_says_it_will_be_retried(self):
        import logging

        manager = _manager(record=None, resolved=None)
        manager._connector.room_ref_by_id = AsyncMock(side_effect=OSError("net"))

        with self.assertLogs("coop.core.session_manager",
                             level=logging.WARNING) as logs:
            result = await manager.inject_message("room-1", "poke")

        self.assertFalse(result)
        self.assertTrue([m for m in logs.output if "retries at its next" in m],
                        logs.output)


class TestTheSchedulerPassesTheId(unittest.IsolatedAsyncioTestCase):
    async def test_the_fire_hands_over_the_jobs_room_id(self):
        from gateway.core.scheduler import JobScheduler

        scheduler = JobScheduler.__new__(JobScheduler)
        manager = MagicMock()
        manager.inject_message = AsyncMock(return_value=True)
        scheduler._session_managers = {"rc": manager}

        job = ScheduledJob(
            watcher="rc:general", connector="rc", room_id="room-1", message="poke")
        await scheduler._inject(job, scheduler._resolve_target(job))

        manager.inject_message.assert_awaited_once_with("room-1", "poke")
        manager.resolve_handle.assert_not_called()


class TestEveryConnectorTypeCanBeResurrectedOrSaysItCannot(unittest.TestCase):
    """The net for the connector half of the resurrection promise.

    `docs/scheduling.md` says a job brings its watcher back. That is true only of
    a connector that overrides `Connector.room_ref_by_id`; the base answers
    `None`, and a job on such a connector fails at every slot after an `expire`
    while the log names three causes that are all false. Voice and script
    inherited the base for a whole release and nothing noticed.

    Walks `SUPPORTED_CONNECTOR_TYPES` — the canonical list — so a NEW connector
    type cannot be forgotten by the person who forgot the override: it fails here
    with the doc obligation in the message. A type that genuinely cannot look a
    room up by id is declared in `CANNOT_RESURRECT`, and the qualifier goes into
    `docs/scheduling.md` in the same commit.
    """

    # Declared, not inferred. Empty today — every shipped connector can answer
    # by id — and a type added here must also be named in docs/scheduling.md.
    CANNOT_RESURRECT: frozenset[str] = frozenset()

    def _class_for(self, connector_type: str):
        from gateway.connectors.mattermost.connector import MattermostConnector
        from gateway.connectors.rocketchat.connector import RocketChatConnector
        from gateway.connectors.script.connector import ScriptConnector
        from gateway.connectors.voice.connector import VoiceConnector
        classes = {
            "rocketchat": RocketChatConnector,
            "mattermost": MattermostConnector,
            "voice": VoiceConnector,
            "script": ScriptConnector,
        }
        self.assertIn(connector_type, classes,
                      f"{connector_type!r} is in SUPPORTED_CONNECTOR_TYPES but "
                      f"this test does not know its class — add it here")
        return classes[connector_type]

    def test_every_supported_type_overrides_room_ref_by_id_or_is_declared(self):
        from gateway.core.connector import SUPPORTED_CONNECTOR_TYPES, Connector

        for connector_type in SUPPORTED_CONNECTOR_TYPES:
            with self.subTest(connector=connector_type):
                cls = self._class_for(connector_type)
                overrides = cls.room_ref_by_id is not Connector.room_ref_by_id
                self.assertTrue(
                    overrides or connector_type in self.CANNOT_RESURRECT,
                    f"{connector_type} inherits the base room_ref_by_id, which "
                    f"answers None — so a scheduled job can never bring one of "
                    f"its watchers back after an expire. Override it, or add "
                    f"{connector_type!r} to CANNOT_RESURRECT and carry the "
                    f"qualifier into docs/scheduling.md.",
                )

    def test_every_supported_type_declares_room_lookup_or_is_declared(self):
        """The capability flag boot's destructive scope check asks first (#141):
        a shipped connector that overrides `room_ref_by_id` but leaves the base
        `supports_room_lookup` (False) would silently keep the pre-lookup
        behaviour — its records would never be reclaimed for a room it no longer
        serves. The two must move together."""
        from gateway.core.connector import SUPPORTED_CONNECTOR_TYPES

        for connector_type in SUPPORTED_CONNECTOR_TYPES:
            with self.subTest(connector=connector_type):
                cls = self._class_for(connector_type)
                declares = cls.supports_room_lookup(cls.__new__(cls))
                self.assertEqual(
                    declares, connector_type not in self.CANNOT_RESURRECT,
                    f"{connector_type}.supports_room_lookup() must agree with "
                    f"whether it overrides room_ref_by_id",
                )

    def test_a_declared_exception_is_not_secretly_overriding(self):
        """The other direction: a type listed as unable that DOES override is a
        stale declaration, and the docs would understate the connector."""
        from gateway.core.connector import Connector

        for connector_type in self.CANNOT_RESURRECT:
            with self.subTest(connector=connector_type):
                cls = self._class_for(connector_type)
                self.assertIs(cls.room_ref_by_id, Connector.room_ref_by_id,
                              f"{connector_type} overrides room_ref_by_id — "
                              f"remove it from CANNOT_RESURRECT")


class TestTheConnectorsResolveByIdThroughTheirOwnClassifier(
        unittest.IsolatedAsyncioTestCase):
    """One classifier per connector. On Rocket.Chat the letter `d` covers both DM
    kinds and the difference decides whether the mention gate applies (§6.4), so a
    by-id-only classifier would be a second place to get that wrong."""

    async def test_rocketchat_classifies_a_channel_from_its_subscription(self):
        from gateway.connectors.rocketchat.connector import RocketChatConnector

        connector = RocketChatConnector.__new__(RocketChatConnector)
        connector._rest = MagicMock()
        connector._rest.get_subscription = AsyncMock(
            return_value={"t": "c", "name": "general"})

        self.assertEqual(
            await connector.room_ref_by_id("room-1"),
            RoomRef(id="room-1", kind=RoomKind.CHANNEL, name="general"),
        )

    async def test_rocketchat_asks_who_is_in_a_direct_room(self):
        from gateway.connectors.rocketchat.connector import RocketChatConnector

        connector = RocketChatConnector.__new__(RocketChatConnector)
        connector._rest = MagicMock()
        connector._rest.get_subscription = AsyncMock(return_value={"t": "d"})
        connector._direct_room_identity = AsyncMock(
            return_value=(RoomKind.DM, ("alice",)))

        room = await connector.room_ref_by_id("room-1")

        self.assertEqual((room.kind, room.participants), (RoomKind.DM, ("alice",)))

    async def test_rocketchat_answers_none_when_it_has_no_subscription(self):
        """Which is also how membership is answered: Rocket.Chat drops the
        subscription when the account leaves or is removed."""
        from gateway.connectors.rocketchat.connector import RocketChatConnector

        connector = RocketChatConnector.__new__(RocketChatConnector)
        connector._rest = MagicMock()
        connector._rest.get_subscription = AsyncMock(return_value=None)

        self.assertIsNone(await connector.room_ref_by_id("room-1"))

    def _mm(self, channel, members=(), member_of=None):
        from gateway.connectors.mattermost.connector import MattermostConnector

        connector = MattermostConnector.__new__(MattermostConnector)
        connector._rest = MagicMock()
        connector._rest.team_id = channel.get("team_id", "")
        connector._rest.bot_user_id = "bot"
        connector._rest.get_channel = AsyncMock(return_value=channel)
        connector._rest.channel_member_usernames = AsyncMock(
            return_value=list(members))
        ids = {channel["id"]} if member_of is None else member_of
        connector._rest.get_member_channel_ids = AsyncMock(return_value=ids)
        return connector

    async def test_mattermost_keeps_the_members_as_participants(self):
        connector = self._mm(
            {"id": "d1", "display_name": "", "type": "dm"}, members=["alice"])

        room = await connector.room_ref_by_id("d1")

        self.assertEqual((room.kind, room.participants), (RoomKind.DM, ("alice",)))

    async def test_mattermost_asks_for_members_once_per_resolution(self):
        """The reverted version delegated to `resolve_room_by_id` and then read
        the members again — two identical round trips, and a window in which the
        two answers could disagree."""
        connector = self._mm(
            {"id": "d1", "display_name": "", "type": "dm"}, members=["alice"])

        await connector.room_ref_by_id("d1")

        self.assertEqual(connector._rest.channel_member_usernames.await_count, 1)

    async def test_mattermost_answers_none_for_a_channel_it_has_left(self):
        """Mattermost, unlike Rocket.Chat, still resolves a channel the bot was
        removed from — so it has to ask. Without this a resurrection could put
        the agent back into a room it was kicked out of (§2.7)."""
        connector = self._mm(
            {"id": "c1", "name": "eng", "display_name": "", "type": "channel"},
            member_of=set(),
        )

        self.assertIsNone(await connector.room_ref_by_id("c1"))

    async def test_mattermost_answers_none_for_a_deleted_channel(self):
        """A permanent absence, per the contract — not a raise the caller would
        log as a retryable blip forever."""
        import httpx

        connector = self._mm({"id": "c1", "name": "eng", "type": "channel"})
        response = httpx.Response(404, request=httpx.Request("GET", "http://x"))
        connector._rest.get_channel = AsyncMock(
            side_effect=httpx.HTTPStatusError("gone", request=response.request,
                                             response=response))

        self.assertIsNone(await connector.room_ref_by_id("c1"))

    async def test_mattermost_still_raises_on_a_transport_failure(self):
        """The one case a retry can change."""
        connector = self._mm({"id": "c1", "name": "eng", "type": "channel"})
        connector._rest.get_channel = AsyncMock(side_effect=OSError("network"))

        with self.assertRaises(OSError):
            await connector.room_ref_by_id("c1")

    async def test_voice_answers_by_echoing_the_id_as_a_channel(self):
        """A voice room's id IS its name — `resolve_room` builds it that way —
        so the inverse is the same identity. Before this override the base's
        `None` meant a job could not resurrect a voice watcher after an expire,
        and the failure log blamed the room for being gone."""
        from gateway.connectors.voice.connector import VoiceConnector

        connector = VoiceConnector.__new__(VoiceConnector)

        self.assertEqual(
            await connector.room_ref_by_id("kitchen"),
            RoomRef(id="kitchen", kind=RoomKind.CHANNEL, name="kitchen"),
        )

    async def test_script_answers_the_same_way(self):
        from gateway.connectors.script.connector import ScriptConnector

        connector = ScriptConnector.__new__(ScriptConnector)

        self.assertEqual(
            await connector.room_ref_by_id("nightly"),
            RoomRef(id="nightly", kind=RoomKind.CHANNEL, name="nightly"),
        )

    async def test_voice_and_script_kind_matches_what_inbound_assigns(self):
        """`kind_for.get(room.type, RoomKind.CHANNEL)` on the inbound path: voice
        rooms are type 'channel' and script rooms are type 'script', which is not
        a RoomKind, so both land on CHANNEL. A resurrected watcher must get the
        same kind or `require_mention` and the label form would differ from the
        original."""
        from gateway.connectors.script.connector import ScriptConnector
        from gateway.connectors.voice.connector import VoiceConnector
        from gateway.core.watcher_rule import RoomKind as RK

        kind_for = {k.value: k for k in RK}
        for cls in (VoiceConnector, ScriptConnector):
            with self.subTest(connector=cls.__name__):
                c = cls.__new__(cls)
                room = await c.resolve_room("r")
                inbound_kind = kind_for.get(room.type, RK.CHANNEL)
                ref = await c.room_ref_by_id("r")
                self.assertEqual(ref.kind, inbound_kind)


class TestTheHandleNeverPicksTheSession(unittest.IsolatedAsyncioTestCase):
    """Found by review, after the first fix: the reverted defect had MOVED.

    Addressing the reply by id was fixed, but the PROCESSOR was still looked up
    by the caller's handle whenever no record was bound to the room — and that
    handle is the one thing known to be untrustworthy. A resident watcher for
    another room answered to it, so the job ran in THAT room's agent session
    (its history, its working directory, its tool policy) while the reply went
    to the room the id names. `enqueue` returned True, so the fire counted as a
    success and a finite job burned a run.

    It also bypassed `get_or_create` entirely, which is where pause, the
    creation cap and the rule match are decided — falsifying the claim, in the
    code's own comment, that a job cannot reach a room a message could not.
    """

    RESOLVED_A = RoomRef(id="room-A", kind=RoomKind.CHANNEL, name="eng")

    async def test_another_rooms_resident_processor_is_not_used(self):
        """Room B took over the handle and is resident; room A's record is gone."""
        room_b_processor = MagicMock()
        room_b_processor.enqueue = AsyncMock(return_value=True)
        manager = _manager(
            record=None,                       # nothing bound to room A
            record_for_room=None,
            resolved=self.RESOLVED_A,
            resident={"room-B": room_b_processor},   # resident for B, not A
        )
        manager._watcher_manager.get_or_create = AsyncMock(return_value=None)

        result = await manager.inject_message("room-A", "poke")

        self.assertFalse(result)
        room_b_processor.enqueue.assert_not_awaited()

    async def test_the_room_is_asked_for_through_get_or_create_instead(self):
        """Which is what makes pause, the cap and the rule match apply."""
        room_b_processor = MagicMock()
        room_b_processor.enqueue = AsyncMock(return_value=True)
        manager = _manager(
            record=None, record_for_room=None, resolved=self.RESOLVED_A,
            resident={"room-B": room_b_processor},
        )

        await manager.inject_message("room-A", "poke")

        manager._watcher_manager.get_or_create.assert_awaited_once()
        _, room = manager._watcher_manager.get_or_create.await_args.args
        self.assertEqual(room.id, "room-A")
        # And the message went to the processor get_or_create returned, not B's.
        room_b_processor.enqueue.assert_not_awaited()
        _enqueued_room(manager)

    async def test_a_record_bound_to_the_room_may_still_name_its_processor(self):
        """The legitimate reuse: the record IS the room's, so its name is safe."""
        record = _record(name="rc:daily-standup", room_id="room-A")
        resident = MagicMock()
        resident.enqueue = AsyncMock(return_value=True)
        manager = _manager(
            record=record, record_for_room=record,
            resident={"room-A": resident},
        )

        result = await manager.inject_message("room-A", "poke")

        self.assertTrue(result)
        resident.enqueue.assert_awaited_once()
        manager._watcher_manager.get_or_create.assert_not_awaited()

    async def test_a_resolved_handle_then_reaches_its_rooms_resident(self):
        """The pre-schema-2 job end to end: handle → room id → that room's
        processor. Two calls, and the second cannot be given the handle."""
        resident = MagicMock()
        resident.enqueue = AsyncMock(return_value=True)
        record = _record()
        manager = _manager(record=record, record_for_room=record,
                           resident={"room-1": resident})

        room_id = manager.resolve_handle("rc:general")
        result = await manager.inject_message(room_id, "poke")

        self.assertTrue(result)
        resident.enqueue.assert_awaited_once()


class TestTheRoomGuardIsReachedOnItsOwn(unittest.IsolatedAsyncioTestCase):
    """Review: `test_nothing_is_injected_when_no_room_can_be_resolved` passed
    with the room guard DELETED, because the processor guard above it returned
    first. A resident processor is needed to reach the room guard at all."""

    async def test_a_resident_watcher_with_no_resolvable_room_injects_nothing(self):
        resident = MagicMock()
        resident.enqueue = AsyncMock(return_value=True)
        manager = _manager(
            record=None, record_for_room=None, resolved=None,
            resident={"room-1": resident},
        )

        result = await manager.inject_message("room-1", "poke")

        self.assertFalse(result, "an unaddressable message must not be injected")
        resident.enqueue.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
