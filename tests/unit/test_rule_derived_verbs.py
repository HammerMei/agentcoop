"""The operator verbs learn rule-derived records (§2.8, step 7a).

pause/resume/reset dispatch on the record: a name whose record carries a
materialized config takes the record path; a static name keeps the config
path byte-identical until the cutover deletes it. What these pin, in the
design's words: pause acts on a record and an unseen room has none (§4.4);
resume restarts the clock — the one deliberate exception to "a recreation
carries the clock" (§2.5); reset must not silently clear paused (§2.5); and
the expire verb is the forced-reclamation path with the operator watching.

The resume/reset/race tests run the wake suite's real harness — connector,
manager, lifecycle and dispatcher all real — because the defect this layer
can produce (a resume that wipes the frozen snapshots) is invisible to a
mocked seam (the A1 lesson).
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tests.helpers import (
    ENG_ROOM,
    evict_record,
    install_record,
    make_rule_derived_record,
    pop_processor,
    register_processor,
)
from tests.unit.test_wake_path import (
    _ACCESS,
    _doc,
)
from tests.unit.test_wake_path import (
    TestTheWakeResumesTheSameSession as _WakeSuite,
)

ROOM_ID = "wake-1"
LIFECYCLE_LOGGER = "coop.core.watcher_lifecycle"
NAME = "rc:eng-backend"


class TestVerbsOnRuleDerivedRecords(unittest.IsolatedAsyncioTestCase):

    _harness = _WakeSuite._harness
    _settle = _WakeSuite._settle

    async def _create(self, connector, lifecycle):
        """A watcher created through the real routing episode."""
        await connector._on_unrouted_message(_doc("m1", 1500), _ACCESS)
        record = lifecycle.get_watcher_state(NAME)
        self.assertIsNotNone(record)
        return record

    async def test_pause_learns_the_record(self):
        connector, lifecycle, dispatcher = await self._harness()
        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            MockProc.return_value.stop = AsyncMock()
            record = await self._create(connector, lifecycle)

            await lifecycle.pause_watcher(NAME)

        state = lifecycle.get_watcher_state(NAME)
        self.assertIs(state, record, "the record is mutated, never replaced")
        self.assertTrue(state.paused)
        self.assertIsNone(lifecycle.processor_named(NAME))
        # The frozen snapshots are untouched by a pause.
        self.assertEqual(state.rule_name, "eng")
        self.assertTrue(dict(state.config))

    async def test_pause_on_an_unseen_room_is_rejected_not_fabricated(self):
        """§4.4: pause acts on a record, and an unobserved room has none.
        Fabricating a blank record here is #118's defect 1 — the rejection
        must point at `rooms.except_for`, the durable alternative."""
        _, lifecycle, _ = await self._harness()

        with self.assertRaises(RuntimeError) as ctx:
            await lifecycle.pause_watcher("rc-never-seen")

        self.assertIn("except_for", str(ctx.exception))
        self.assertIsNone(lifecycle.get_watcher_state("rc-never-seen"),
                          "no blank record was fabricated")

    async def test_resume_restarts_the_clock_and_keeps_the_snapshots(self):
        """The owed test (§2.5): `last_activity_at` is stamped at the moment
        of resume — a watcher paused longer than its idle TTL must not be
        re-idled by the next sweep pass — while everything frozen at creation
        survives the fresh WatcherState the start constructs."""
        connector, lifecycle, dispatcher = await self._harness()
        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            MockProc.return_value.stop = AsyncMock()
            record = await self._create(connector, lifecycle)
            session_id = record.session_id
            created_at = record.created_at

            await lifecycle.pause_watcher(NAME)
            # A pause long enough that a carried clock would idle instantly.
            paused = lifecycle.get_watcher_state(NAME)
            paused.last_activity_at = "2020-01-01T00:00:00+00:00"
            paused.dropped_at = "2020-01-02T00:00:00+00:00"

            await lifecycle.resume_watcher(NAME)

        resumed = lifecycle.get_watcher_state(NAME)
        self.assertFalse(resumed.paused)
        self.assertEqual(resumed.session_id, session_id, "the same session resumed")
        self.assertEqual(resumed.rule_name, "eng", "the frozen rule survived")
        self.assertTrue(dict(resumed.config), "the frozen config survived")
        self.assertEqual(resumed.created_at, created_at)
        self.assertEqual(resumed.dropped_at, "", "resume returns it to active")
        self.assertNotEqual(resumed.last_activity_at, "2020-01-01T00:00:00+00:00",
                            "resume RESTARTS the clock (§2.5) — the deliberate "
                            "exception to 'a recreation carries the clock'")
        self.assertIsNotNone(lifecycle.processor_named(NAME))
        # Messages that arrived while paused were deliberately dropped, not
        # deferred (§4.4) — a resume must not replay the muted interval.
        connector.replay_room_since.assert_not_awaited()

    async def test_resume_seals_the_muted_interval(self):
        """Codex round 10: the immediate replay was already skipped (§4.4),
        but the record kept its pre-pause watermark — so the NEXT boot's
        replay probe delivered the muted interval after all. Resume from
        paused clears it; an idle wake must NOT (that replay is owed)."""
        connector, lifecycle, dispatcher = await self._harness()
        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            MockProc.return_value.stop = AsyncMock()
            await self._create(connector, lifecycle)
            lifecycle.get_watcher_state(NAME).last_processed_ts = "1400"

            await lifecycle.pause_watcher(NAME)
            await lifecycle.resume_watcher(NAME)

        resumed = lifecycle.get_watcher_state(NAME)
        self.assertEqual(resumed.last_processed_ts, "",
                         "the pre-pause watermark was sealed — the muted "
                         "interval is not owed to any future replay")
        connector.replay_room_since.assert_not_awaited()

    async def test_reset_refuses_a_paused_record(self):
        """§2.5: reset must not silently clear `paused` — the operator's one
        durable mute must not be erased as a side effect of session hygiene."""
        connector, lifecycle, _ = await self._harness()
        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            MockProc.return_value.stop = AsyncMock()
            await self._create(connector, lifecycle)
            await lifecycle.pause_watcher(NAME)

            with self.assertRaises(RuntimeError) as ctx:
                await lifecycle.reset_watcher(NAME)

        self.assertIn("resume", str(ctx.exception).lower())
        self.assertTrue(lifecycle.get_watcher_state(NAME).paused,
                        "the pause survived the refused reset")

    async def test_reset_on_a_removed_agent_fails_before_any_mutation(self):
        """Matrix sweep after Codex round 6: reset used to clear the session
        pointer BEFORE start's step-0 agent gate raised — the verb reported a
        pure refusal while its destructive half had already executed, and the
        abandoned session id was never logged anywhere. The named-but-missing
        agent is now refused by `_ensure_agent_available`, before the lock
        and before any write."""
        connector, lifecycle, _ = await self._harness()
        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            MockProc.return_value.stop = AsyncMock()
            record = await self._create(connector, lifecycle)
            session_id = record.session_id
            self.assertTrue(session_id)

            # The operator deletes the agent; the frozen binding remains.
            record.agent = "ghost"
            record.config = {**dict(record.config), "agent": "ghost"}

            with self.assertRaises(RuntimeError) as ctx:
                await lifecycle.reset_watcher(NAME)

        self.assertIn("ghost", str(ctx.exception))
        self.assertIn("expire", str(ctx.exception))
        state = lifecycle.get_watcher_state(NAME)
        self.assertEqual(state.session_id, session_id,
                         "the refusal ran before the destructive half — the "
                         "session pointer survived")
        self.assertTrue(state.context_injected,
                        "context_injected survived the refused reset")

    async def test_a_same_name_start_for_a_different_room_is_refused(self):
        """Matrix sweep after Codex round 6: a room deleted server-side and
        recreated under the same name derives the SAME watcher name for a
        different room_id — installing its record would silently clobber the
        old room's record (session, watermark, even a pause), with no
        backstop that ever notices. Refused loudly, before the session
        provision, so the refusal mints nothing."""
        from gateway.core.connector import Room
        from gateway.core.watcher_manager import config_from_record

        connector, lifecycle, _ = await self._harness()
        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            record = await self._create(connector, lifecycle)
            wc = config_from_record(record)
            agent = lifecycle._agents["default"]
            sessions_before = len(agent.created_sessions)

            with self.assertRaises(RuntimeError) as ctx:
                await lifecycle.start_watcher_in_room(
                    wc, None,
                    Room(id="recreated-room", name="eng-backend",
                         type="channel"))

        self.assertIn("expire", str(ctx.exception))
        self.assertIs(lifecycle.get_watcher_state(NAME), record,
                      "the old room's record was left untouched")
        self.assertEqual(len(agent.created_sessions), sessions_before,
                         "the refusal minted no session")

    async def test_resume_joins_the_shutdown_drain_barrier(self):
        """Codex round 9: the manager's drain covers message-triggered
        starts, but resume/reset call start_watcher_in_room directly off
        control handlers the ControlServer's stop does not await — a verb
        already inside session creation could install a processor after
        stop_all's snapshot. drain_verbs waits it out; a verb arriving after
        the drain refuses."""
        connector, lifecycle, _ = await self._harness()
        gate = asyncio.Event()
        agent = lifecycle._agents["default"]
        real_create = agent.create_session

        async def gated_create(*args, **kwargs):
            await gate.wait()
            return await real_create(*args, **kwargs)

        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            MockProc.return_value.stop = AsyncMock()
            await self._create(connector, lifecycle)
            await lifecycle.pause_watcher(NAME)
            # Force the resume to mint a session so it parks on the gate.
            lifecycle.get_watcher_state(NAME).session_id = ""
            agent.create_session = gated_create

            verb = asyncio.create_task(lifecycle.resume_watcher(NAME))
            for _ in range(10):
                await asyncio.sleep(0)

            drain = asyncio.create_task(lifecycle.drain_verbs())
            for _ in range(10):
                await asyncio.sleep(0)
            self.assertFalse(drain.done(),
                             "the drain must wait for the in-flight resume")

            gate.set()
            await drain
            await verb

            self.assertIsNotNone(
                lifecycle.processor_named(NAME),
                "the verb finished BEFORE the drain returned — its processor "
                "is inside the snapshot stop_all takes next")

            # And after the drain, new verbs refuse.
            with self.assertRaises(RuntimeError) as ctx:
                await lifecycle.reset_watcher(NAME)
            self.assertIn("shutting down", str(ctx.exception))

    async def test_pause_and_expire_refuse_after_the_drain(self):
        """Internal review of the barrier close: pause and expire were the
        two destructive writers outside flag+counter — protected only by the
        ControlServer's stop ordering, which is incidental and
        Python-version-dependent. They now share the verb barrier."""
        connector, lifecycle, _ = await self._harness()
        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            MockProc.return_value.stop = AsyncMock()
            await self._create(connector, lifecycle)

            await lifecycle.drain_verbs()

            with self.assertRaises(RuntimeError) as ctx:
                await lifecycle.pause_watcher(NAME)
            self.assertIn("shutting down", str(ctx.exception))
            self.assertFalse(lifecycle.get_watcher_state(NAME).paused,
                             "the refused pause wrote nothing")

    async def test_an_inflight_pause_holds_the_drain_open(self):
        """The wait-out half for pause: a pause already stopping its
        processor finishes before drain_verbs returns, so its state write
        lands before the final save."""
        connector, lifecycle, _ = await self._harness()
        gate = asyncio.Event()

        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()

            async def slow_stop():
                await gate.wait()

            MockProc.return_value.stop = AsyncMock(side_effect=slow_stop)
            await self._create(connector, lifecycle)

            verb = asyncio.create_task(lifecycle.pause_watcher(NAME))
            for _ in range(10):
                await asyncio.sleep(0)

            drain = asyncio.create_task(lifecycle.drain_verbs())
            for _ in range(10):
                await asyncio.sleep(0)
            self.assertFalse(drain.done(),
                             "the drain must wait for the in-flight pause")

            gate.set()
            await drain
            await verb

        self.assertTrue(lifecycle.get_watcher_state(NAME).paused,
                        "the pause completed BEFORE the drain returned")

    async def test_pause_refuses_a_record_reclaimed_while_it_waited(self):
        """TOCTOU sweep after Codex round 4: pause was the odd verb out —
        resume and reset both raise on a record reclaimed while they waited
        on the lock, but pause fell through, answered ok and logged 'paused'
        for a record that no longer existed. The room's next message would
        then create a fresh, UNPAUSED watcher: a silent contradiction of the
        operator's command."""
        connector, lifecycle, _ = await self._harness()
        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            MockProc.return_value.stop = AsyncMock()
            await self._create(connector, lifecycle)

            lock = lifecycle._get_watcher_lock(NAME)
            await lock.acquire()
            task = asyncio.create_task(lifecycle.pause_watcher(NAME))
            for _ in range(5):  # past the pre-check, parked on the lock
                await asyncio.sleep(0)
            # The reclaim, landing while the pause waits.
            evict_record(lifecycle, NAME)
            pop_processor(lifecycle, NAME)
            lock.release()

            with self.assertRaises(RuntimeError) as ctx:
                await task

        self.assertIn("reclaimed", str(ctx.exception))

    async def test_resume_and_reset_refuse_a_record_replaced_while_they_waited(self):
        """TOCTOU sweep after Codex round 4: both verbs built `wc` from the
        pre-lock record — acting on a REPLACEMENT would run the old record's
        config against a watcher the operator did not select. Same identity
        pin as the expire verb's `expected=`."""
        connector, lifecycle, _ = await self._harness()
        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            MockProc.return_value.stop = AsyncMock()
            await self._create(connector, lifecycle)

            for verb in (lifecycle.resume_watcher, lifecycle.reset_watcher):
                original = lifecycle.get_watcher_state(NAME)
                lock = lifecycle._get_watcher_lock(NAME)
                await lock.acquire()
                task = asyncio.create_task(verb(NAME))
                for _ in range(5):
                    await asyncio.sleep(0)
                # The reclaim-and-recreate cycle, completed while the verb
                # waited: a different object under the same name, WITH its
                # own resident processor.
                replacement = make_rule_derived_record(
                    name=NAME, room_id=original.room_id)
                install_record(lifecycle, replacement, as_name=NAME)
                replacement_proc = MagicMock()
                replacement_proc.stop = AsyncMock()
                register_processor(lifecycle, NAME, replacement_proc)
                lock.release()

                # The skip is logged as well as raised (owner, PR #150): the
                # CLI shows the operator the error, the daemon log shows
                # whoever reads it later why the verb did nothing.
                with self.assertRaises(RuntimeError) as ctx, \
                        self.assertLogs(LIFECYCLE_LOGGER, level="WARNING") as logs:
                    await task
                self.assertIn("replaced", str(ctx.exception))
                self.assertIn("skipped", str(ctx.exception))
                self.assertTrue(any("skipped" in line and "replaced" in line
                                    for line in logs.output), logs.output)
                self.assertIs(lifecycle.get_watcher_state(NAME), replacement,
                              "the replacement was left untouched")
                # Round 5: the gates run BEFORE the destructive stop — a
                # rejected reset must not leave the replacement non-resident.
                replacement_proc.stop.assert_not_awaited()
                self.assertIs(lifecycle.get_processor(NAME), replacement_proc,
                              "the replacement's processor survived the "
                              "rejected verb")
                pop_processor(lifecycle, NAME)

    async def test_resume_and_reset_say_so_when_the_record_was_reclaimed_while_they_waited(self):
        """Owner, PR #150: the no-op is correct — an expire or a membership
        removal landing inside the lock wait leaves nothing to act on — but an
        operator who typed the verb must be told WHY nothing happened, on the
        CLI (the raised message) and in the daemon log (the warning)."""
        connector, lifecycle, _ = await self._harness()
        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            MockProc.return_value.stop = AsyncMock()

            for verb, word in ((lifecycle.resume_watcher, "resume"),
                               (lifecycle.reset_watcher, "reset")):
                # A dormant rule-derived record, installed directly (the way
                # the replaced-while-waited test installs its replacement):
                # no processor, so the verb would recreate it.
                install_record(lifecycle, make_rule_derived_record(
                    name=NAME, room_id=ENG_ROOM.id), as_name=NAME)
                lock = lifecycle._get_watcher_lock(NAME)
                await lock.acquire()
                task = asyncio.create_task(verb(NAME))
                for _ in range(5):
                    await asyncio.sleep(0)
                # The reclamation, completed while the verb waited.
                evict_record(lifecycle, NAME)
                lock.release()

                with self.assertRaises(RuntimeError) as ctx, \
                        self.assertLogs(LIFECYCLE_LOGGER, level="WARNING") as logs:
                    await task
                message = str(ctx.exception)
                self.assertIn(f"{word.capitalize()} of watcher", message)
                self.assertIn("skipped", message)
                self.assertIn(f"reclaimed while the {word} waited", message)
                self.assertIn("expired, or the bot was removed", message)
                self.assertTrue(any("skipped" in line and "reclaimed" in line
                                    for line in logs.output), logs.output)
                self.assertIsNone(lifecycle.get_watcher_state(NAME),
                                  "nothing was recreated")

    async def test_reset_refuses_a_pause_that_landed_while_it_waited(self):
        """Codex round 3: the paused refusal runs before the lock, so a pause
        landing between that check and the lock's acquisition would be
        silently erased by the restart (which writes paused=False). The
        re-check under the lock is what closes the gap."""
        connector, lifecycle, _ = await self._harness()
        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            MockProc.return_value.stop = AsyncMock()
            await self._create(connector, lifecycle)

            # The pause, landing between reset's pre-lock check and its lock
            # acquisition — the in-place write the pause verb makes. (It
            # cannot land INSIDE the critical section: pause needs this same
            # lock, which is why gates-before-stop is complete — round 5.)
            lock = lifecycle._get_watcher_lock(NAME)
            await lock.acquire()
            task = asyncio.create_task(lifecycle.reset_watcher(NAME))
            for _ in range(5):  # past the pre-check, parked on the lock
                await asyncio.sleep(0)
            lifecycle.get_watcher_state(NAME).paused = True
            lock.release()

            with self.assertRaises(RuntimeError) as ctx:
                await task

        self.assertIn("paused", str(ctx.exception).lower())
        self.assertTrue(lifecycle.get_watcher_state(NAME).paused,
                        "the pause survived the refused reset")

    async def test_reset_mints_a_fresh_session_and_keeps_the_snapshots(self):
        connector, lifecycle, _ = await self._harness()
        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            MockProc.return_value.stop = AsyncMock()
            record = await self._create(connector, lifecycle)
            old_session = record.session_id

            await lifecycle.reset_watcher(NAME)

        reset = lifecycle.get_watcher_state(NAME)
        self.assertTrue(reset.session_id)
        self.assertNotEqual(reset.session_id, old_session, "a fresh session")
        self.assertEqual(reset.rule_name, "eng", "the frozen rule survived")
        self.assertTrue(dict(reset.config), "the frozen config survived")
        self.assertTrue(reset.last_activity_at, "reset stamps the clock too")

    async def test_pause_wins_the_race_against_a_wake(self):
        """The owed race test: a wake dispatched against a record whose pause
        is mid-teardown must wait on the watcher lock and then decline — the
        re-read under the lock is what makes §4.4 hold against a message
        arriving in the pause's own drain window."""
        connector, lifecycle, dispatcher = await self._harness()
        stop_gate = asyncio.Event()
        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()

            async def slow_stop():
                await stop_gate.wait()

            MockProc.return_value.stop = AsyncMock(side_effect=slow_stop)
            await self._create(connector, lifecycle)

            pause_task = asyncio.create_task(lifecycle.pause_watcher(NAME))
            # Let the pause reach the gated stop: the processor is already
            # popped, so a wake sees a non-resident record and dispatches
            # _recreate — which must block on the watcher lock the pause holds.
            for _ in range(50):
                await asyncio.sleep(0)
                if lifecycle.get_processor(NAME) is None:
                    break

            wake_task = asyncio.create_task(
                self.manager.get_or_create("rc", _wake_room()))
            await asyncio.sleep(0.01)
            self.assertFalse(wake_task.done(),
                             "the wake waits on the pause's watcher lock")

            stop_gate.set()
            await pause_task
            woken = await wake_task

        self.assertIsNone(woken, "the wake re-read the record under the lock "
                                 "and declined the now-paused room")
        self.assertTrue(lifecycle.get_watcher_state(NAME).paused)
        self.assertIsNone(lifecycle.processor_named(NAME))


def _wake_room():
    from gateway.core.watcher_manager import RoomRef
    from gateway.core.watcher_rule import RoomKind

    return RoomRef(id=ROOM_ID, kind=RoomKind.CHANNEL, name="eng-backend")


class TestStopSurvivesACursorReadFailure(unittest.IsolatedAsyncioTestCase):
    """#118 defect 3: `_stop_processor`'s watermark capture used to run with
    no try — a raising cursor read abandoned the unsubscribe and the drain,
    leaving the room subscribed and the queue undrained, to preserve a
    watermark it then did not capture anyway."""

    async def test_pause_completes_when_the_cursor_read_raises(self):
        from unittest.mock import AsyncMock, MagicMock

        from tests.helpers import (
            MockAgentBackend,
            make_core_config,
            make_lifecycle,
            make_rule_derived_record,
        )

        connector = MagicMock()
        connector.get_last_processed_ts = MagicMock(
            side_effect=RuntimeError("socket died"))
        connector.unsubscribe_room = AsyncMock()
        lifecycle = make_lifecycle(
            connector=connector,
            agents={"default": MockAgentBackend()},
            config=make_core_config(),
        )
        record = make_rule_derived_record(name="w1",
                                          last_processed_ts="persisted-mark")
        install_record(lifecycle, record, as_name="w1")
        proc = MagicMock()
        proc.stop = AsyncMock()
        register_processor(lifecycle, "w1", proc)

        await lifecycle.pause_watcher("w1")  # must not raise

        self.assertTrue(record.paused)
        self.assertEqual(record.last_processed_ts, "persisted-mark",
                         "the persisted mark stands when the live read fails")
        connector.unsubscribe_room.assert_awaited_once()
        proc.stop.assert_awaited_once()
        self.assertIsNone(lifecycle.get_processor("w1"))


class TestAMissingFrozenAgentFailsClosed(unittest.IsolatedAsyncioTestCase):
    """Codex review of #121: `_resolve_agent_name` substituted the default for
    an unknown name — right for an empty field, wrong for a named one. A record
    frozen against a since-deleted agent must read `failed`, not silently
    restart under a different backend, working directory and tool policy.

    Both halves refuse now. The empty-field case was the one that still
    substituted, and there is no longer anything to substitute: `default_agent:`
    is gone, so a rule names its agent (or inherits it) and the record freezes
    that resolved name."""

    async def test_a_named_but_missing_agent_refuses_the_start(self):
        from unittest.mock import MagicMock

        from gateway.core.connector import Room
        from tests.helpers import (
            MockAgentBackend,
            make_core_config,
            make_lifecycle,
            make_watcher,
        )

        lifecycle = make_lifecycle(
            agents={"default": MockAgentBackend()},
            config=make_core_config(),
            dispatcher=MagicMock(holder=MagicMock(return_value=None)),
        )
        wc = make_watcher("room-1", name="w1", agent="deleted-agent")

        with self.assertRaises(RuntimeError) as ctx:
            await lifecycle.start_watcher_in_room(
                wc, None, Room(id="r1", name="room-1", type="channel"))

        self.assertIn("deleted-agent", str(ctx.exception))
        self.assertIn("no longer exists", str(ctx.exception))
        self.assertIsNone(lifecycle.get_watcher_state("w1"),
                          "nothing was written for a refused start")

    async def test_an_empty_agent_field_is_refused_rather_than_substituted(self):
        """The other half of the same rule, inverted by this change.

        An empty `agent` used to take the top-level `default_agent:` — the one
        case the sibling test above deliberately left substituting. That default
        no longer exists: a rule states its agent or inherits one, and a record's
        frozen `agent` is written from that resolved value. So an empty field
        means the record and the config disagree, and guessing is exactly what
        the named-but-missing case above already refuses to do.
        """
        from unittest.mock import MagicMock

        from gateway.core.connector import Room
        from tests.helpers import (
            MockAgentBackend,
            make_core_config,
            make_lifecycle,
            make_watcher,
        )

        lifecycle = make_lifecycle(
            agents={"default": MockAgentBackend()},
            config=make_core_config(),
            dispatcher=MagicMock(holder=MagicMock(return_value=None)),
        )
        wc = make_watcher("room-1", name="w1", agent="")

        with self.assertRaises(RuntimeError) as ctx:
            await lifecycle.start_watcher_in_room(
                wc, None, Room(id="r1", name="room-1", type="channel"))

        self.assertIn("no default to fall back to", str(ctx.exception))
        self.assertIsNone(lifecycle.get_watcher_state("w1"),
                          "nothing was written for a refused start")


class TestTheExpireVerb(unittest.IsolatedAsyncioTestCase):

    def _mgr(self, record, *, reclaimed="w1", cancelled=None):
        from tests.helpers import make_bare_session_manager

        mgr = make_bare_session_manager(
            # Two args now: the handle AND the room. Cancellation matches by
            # room when a job records one, because a handle can be taken over.
            _cancel_jobs=((lambda n, r: cancelled.append(n))
                          if cancelled is not None else None))
        mgr._lifecycle.get_watcher_state = MagicMock(return_value=record)
        mgr._lifecycle.reclaim_room = AsyncMock(return_value=reclaimed)
        return mgr

    async def test_expire_reclaims_and_leaves_scheduled_jobs_alone(self):
        """Inverted, owner 2026-08-31.

        Expire used to cancel the watcher's scheduled jobs, borrowed from the
        membership-removal path whose reason was "a job left in the store would
        fire forever at a room that cannot answer". After a membership removal
        that is true. After an operator expire it is not: the room is still
        there, the bot is still in it, and a watcher handle is a pure function of
        `(connector, room)` — so the room's next message brings the SAME name
        back and the job resumes. Cancelling destroyed something self-healing,
        silently, for an operator who never mentioned their schedules.
        """
        record = make_rule_derived_record(name="w1")
        cancelled = []
        mgr = self._mgr(record, cancelled=cancelled)

        await mgr.expire_watcher("w1")

        mgr._lifecycle.reclaim_room.assert_awaited_once()
        args = mgr._lifecycle.reclaim_room.call_args
        self.assertEqual(args.args[0], "room-w1")
        self.assertIn("expire", args.kwargs["reason"])
        # The identity pin, WITHOUT require_dormant (Codex round 3): expire
        # must not delete the selected record's replacement, but it acts on
        # active records too.
        self.assertIs(args.kwargs.get("expected"), record)
        self.assertFalse(args.kwargs.get("require_dormant", False))
        self.assertEqual(cancelled, [], "expire must not touch the schedules")

    async def test_expire_is_refused_on_a_connector_with_no_unsolicited_inbound(self):
        """Owner's decision (PR #140, Codex round 1). Expire promises "the room's
        next message recreates the watcher"; voice and script have no next
        message — nothing is pushed to them, so nothing routes an unclaimed
        room into creation — and the expired watcher stayed down until a
        restart. Refused, naming `reset`; nothing is reclaimed."""
        record = make_rule_derived_record(name="w1")
        mgr = self._mgr(record)
        mgr._connector.supports_unsolicited_inbound = MagicMock(return_value=False)
        mgr._connector.name = "voice-1"

        with self.assertRaises(RuntimeError) as ctx:
            await mgr.expire_watcher("w1")

        self.assertIn("reset w1", str(ctx.exception))
        self.assertIn("voice-1", str(ctx.exception))
        mgr._lifecycle.reclaim_room.assert_not_awaited()

    async def test_expire_raises_where_the_event_handlers_swallow(self):
        """An operator watching the command must see the failure."""
        mgr = self._mgr(None)
        with self.assertRaises(RuntimeError):
            await mgr.expire_watcher("ghost")

        static = make_rule_derived_record(name="s1", config={})
        mgr = self._mgr(static)
        with self.assertRaises(RuntimeError):
            await mgr.expire_watcher("s1")

        record = make_rule_derived_record(name="w1")
        mgr = self._mgr(record, reclaimed=None)
        with self.assertRaises(RuntimeError):
            await mgr.expire_watcher("w1")

    async def test_the_dispatch_arm_exists(self):
        record = make_rule_derived_record(name="w1")
        mgr = self._mgr(record)

        result = await mgr.dispatch_command(
            {"cmd": "expire", "watcher_name": "w1"})
        self.assertTrue(result["ok"])

        result = await mgr.dispatch_command({"cmd": "expire"})
        self.assertFalse(result["ok"])


class TestAReclaimSurvivesARenameInItsAwaitWindow(unittest.IsolatedAsyncioTestCase):
    """`_reclaim_record_locked` awaited the session delete, the prompt reclaim
    and a `to_thread`, then uninstalled by the NAME captured at entry. A frame
    renaming the room inside that window re-pointed `_room_of` first, so the
    uninstall found nothing: the record stayed installed under its new handle,
    active-shaped, pointing at a session just deleted, and the reclaim reported
    success (internal review, P2). It now uninstalls by the record's current
    name and prunes both."""

    _harness = _WakeSuite._harness
    _settle = _WakeSuite._settle

    async def test_the_record_is_gone_under_both_names(self):
        connector, lifecycle, _ = await self._harness()
        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            MockProc.return_value.stop = AsyncMock()
            await connector._on_unrouted_message(_doc("m1", 1500), _ACCESS)
            record = lifecycle.get_watcher_state(NAME)
            self.assertIsNotNone(record)
            agent = lifecycle._agents["default"]

            async def rename_mid_reclaim(*args, **kwargs):
                self.assertEqual(await lifecycle.observe_room_name(ROOM_ID, "eng-renamed"),
                                 "rc:eng-renamed")
            agent.reclaim_durable_instructions = rename_mid_reclaim
            saves = []
            real_save = lifecycle._state_store.save
            lifecycle._state_store.save = lambda states, **kw: (saves.append(kw), real_save(states, **kw))[1]

            reclaimed = await lifecycle.reclaim_room(ROOM_ID, reason="test")

        self.assertIsNotNone(reclaimed)
        self.assertIsNone(lifecycle.record_for_room(ROOM_ID), "the record survived its own reclaim")
        self.assertIsNone(lifecycle.get_watcher_state("rc:eng-renamed"))
        self.assertIsNone(lifecycle.get_watcher_state(NAME))
        # Both names pruned from the file: the old one is the row on disk, the
        # new one is what a save between the rename and this point would have
        # written. Pruning only the captured name left the renamed row behind
        # (internal review found this half untested).
        self.assertTrue(any(kw.get("prune") == {NAME, "rc:eng-renamed"} for kw in saves), saves)


class TestControlResolvesRecordOnlyNames(unittest.TestCase):

    def test_a_record_only_name_finds_its_entry(self):
        """The control layer resolved verbs through config alone — a
        rule-derived name never reached the lifecycle at all. The record
        answers second, so the static path keeps winning while both exist."""
        from gateway.control import ControlServer

        entry = MagicMock()
        entry.session_manager.get_watcher_config = MagicMock(return_value=None)
        entry.session_manager.get_watcher_state = MagicMock(
            return_value=make_rule_derived_record(name="rc-eng"))
        server = ControlServer.__new__(ControlServer)
        server._entries = [entry]

        self.assertIs(server._find_entry_for_watcher("rc-eng"), entry)

        entry.session_manager.get_watcher_state = MagicMock(return_value=None)
        result = server._find_entry_for_watcher("ghost")
        self.assertIsInstance(result, dict)
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
