"""A stored session id is only reused inside the backend that issued it (§2.4).

`backend_identity` shipped as a *field* with the state schema and nothing compared it,
so a record could be resumed against a backend whose type or working directory had
changed since — replaying the id into a different session store, which either loses the
conversation silently or lands on an unrelated session that happens to carry the same
id. These tests cover the comparison, not the field.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.core.state import WatcherState, backend_identity
from tests.helpers import start_watcher


class TestBackendIdentityValue(unittest.TestCase):
    def test_it_is_type_and_working_directory(self):
        self.assertEqual(backend_identity("claude", "/srv/work"), "claude:/srv/work")

    def test_either_half_changing_changes_the_identity(self):
        base = backend_identity("claude", "/srv/work")
        self.assertNotEqual(base, backend_identity("opencode", "/srv/work"))
        self.assertNotEqual(base, backend_identity("claude", "/srv/other"))

    def test_the_spelling_is_a_stored_value_not_a_display_choice(self):
        """Guards the separator against a tidy-up.

        The string is written into state files and compared against records already on
        disk, so changing how it is spelled does not "reformat" anything — it makes every
        stored identity mismatch, and every watcher silently starts a fresh session on the
        next boot. `tests/unit/test_state_schema.py` already persists `claude:/srv/work`.
        """
        self.assertEqual(backend_identity("claude", "/srv/work"), "claude:/srv/work")


class TestTheIdentityFollowsTheRealDirectory(unittest.TestCase):
    """A configured path and the directory a process actually runs in can differ."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / "v1").mkdir()
        (self.root / "v2").mkdir()
        self.link = self.root / "current"
        self.link.symlink_to(self.root / "v1")
        self.addCleanup(self._tmp.cleanup)

    def test_retargeting_a_symlink_changes_the_identity(self):
        """The case an uncanonicalized identity cannot see.

        `/srv/current -> /srv/v1` repointed to `/srv/v2` leaves config.yaml byte-identical
        while the backend's session store moves, because a child process launched with
        `cwd=<symlink>` reports the physical path. Comparing the configured string would
        call that a match and replay the old id into the new store.
        """
        before = backend_identity("claude", str(self.link))
        self.link.unlink()
        self.link.symlink_to(self.root / "v2")
        after = backend_identity("claude", str(self.link))

        self.assertNotEqual(before, after)
        self.assertIn("v1", before)
        self.assertIn("v2", after)

    def test_the_child_process_really_does_see_the_physical_path(self):
        """Pins the premise the test above rests on, rather than assuming POSIX.

        If `getcwd()` ever reported the symlink instead, canonicalizing would be the bug
        and this test says so at the point the assumption is made.
        """
        import subprocess
        out = subprocess.run(
            [sys.executable, "-c", "import os; print(os.getcwd())"],
            cwd=str(self.link), capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(Path(out).resolve(), (self.root / "v1").resolve())

    def test_an_empty_working_directory_is_not_resolved_to_the_process_cwd(self):
        """Config load requires the field, so empty only reaches here from tests —
        and resolving it would make an identity depend on where pytest was run."""
        self.assertEqual(backend_identity("claude", ""), "claude:")


class TestSessionReuseRequiresMatchingIdentity(unittest.IsolatedAsyncioTestCase):
    def _make_lifecycle(self, agent_type="claude", working_directory="/srv/work"):
        from gateway.core.config import (
            AgentConfig,
            CoreConfig,
            HistoryHandoffConfig,
            WatcherConfig,
        )
        from gateway.core.injected_context_builder import InjectedContextBuilder
        from gateway.core.session_maps import SessionMaps
        from gateway.core.watcher_lifecycle import WatcherLifecycle

        wc = WatcherConfig(
            name="w1",
            connector="rc",
            room="#nest",
            agent="claude",
            history_handoff=HistoryHandoffConfig(enabled=True, fetch_count=10),
        )
        config = CoreConfig()
        config.agents = {
            "claude": AgentConfig(
                name="claude", type=agent_type, working_directory=working_directory)
        }
        config.default_agent = "claude"

        connector = AsyncMock()
        connector.agent_username = "hammer-mei"
        connector.resolve_room = AsyncMock(
            return_value=MagicMock(id="r1", name="nest", type="channel"))
        connector.subscribe_room = AsyncMock()
        connector.fetch_room_history = AsyncMock(return_value=[])
        connector.get_last_processed_ts = MagicMock(return_value=None)
        connector.update_last_processed_ts = MagicMock()
        connector.attachment_cache_dir = MagicMock(return_value=None)

        agent = AsyncMock()
        agent.create_session = AsyncMock(return_value="fresh-session-id")
        agent.send = AsyncMock(return_value=MagicMock(is_error=False, text="ok"))
        agent.delete_session = AsyncMock(return_value=True)

        state_store = MagicMock()
        state_store.load = MagicMock(return_value={})
        state_store.save = MagicMock()

        lifecycle = WatcherLifecycle(
            connector=connector,
            agents={"claude": agent},
            config=config,
            state_store=state_store,
            # `holder()` on a bare MagicMock answers with a truthy mock, which now
            # reads as "another watcher already serves this room" (§4.1).
            dispatcher=MagicMock(holder=MagicMock(return_value=None)),
            injector=InjectedContextBuilder(config),
            permission_registry=None,
            maps=SessionMaps(),
        )
        lifecycle._attachment_workspace = MagicMock()
        lifecycle._attachment_workspace.setup = MagicMock(return_value="/tmp/fake")
        return lifecycle, connector, agent, wc

    async def _start(self, lifecycle, wc, state):
        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            await start_watcher(lifecycle, wc, state=state)

    def _stored(self, session_id, identity):
        return WatcherState(
            watcher_name="w1",
            session_id=session_id,
            room_id="r1",
            context_injected=True,
            backend_identity=identity,
        )

    async def test_a_matching_identity_reuses_the_session(self):
        lifecycle, _, agent, wc = self._make_lifecycle()
        state = self._stored("old-session-id", backend_identity("claude", "/srv/work"))

        await self._start(lifecycle, wc, state)

        agent.create_session.assert_not_called()
        self.assertEqual(lifecycle.get_watcher_state("w1").session_id, "old-session-id")

    async def test_a_changed_working_directory_forces_a_fresh_session(self):
        """The failure this exists to prevent: same agent name, different session store."""
        lifecycle, _, agent, wc = self._make_lifecycle(working_directory="/srv/moved")
        state = self._stored("old-session-id", backend_identity("claude", "/srv/work"))

        await self._start(lifecycle, wc, state)

        agent.create_session.assert_called_once()
        self.assertEqual(lifecycle.get_watcher_state("w1").session_id, "fresh-session-id")

    async def test_a_changed_backend_type_forces_a_fresh_session(self):
        lifecycle, _, agent, wc = self._make_lifecycle(agent_type="opencode")
        state = self._stored("old-session-id", backend_identity("claude", "/srv/work"))

        await self._start(lifecycle, wc, state)

        agent.create_session.assert_called_once()

    async def test_a_changed_room_forces_a_fresh_session(self):
        """The reachable half of "one session, one room" — an ordinary config edit.

        Changing `room:` on an existing watcher keeps the record, so reusing the session
        on an identity match alone rebinds it to the new room: that room then inherits
        the old room's transcript and identity header, and the watermark restore writes
        the *old* room's cursor onto it, silently discarding every message in the new
        room older than that timestamp. No error, no log, on a supported operation —
        while both refusal messages point the operator at file corruption instead.
        """
        lifecycle, _, agent, wc = self._make_lifecycle()
        state = self._stored("old-session-id", backend_identity("claude", "/srv/work"))
        state.room_id = "some-other-room"

        await self._start(lifecycle, wc, state)

        agent.create_session.assert_called_once()
        self.assertEqual(lifecycle.get_watcher_state("w1").session_id, "fresh-session-id")

    async def test_a_record_with_no_room_forces_a_fresh_session(self):
        """An empty `room_id` is a mismatch, not an exemption.

        The field has no dataclass default, so it is required and the reader fills an
        absent one with `""` — an empty value in a version-2 record therefore means the
        record was hand-edited or truncated, which is the case this refusal exists for.
        The first version of this test asserted the opposite, on the theory that such a
        record predated the field; no such record can be read (the format version refuses
        them), and the exemption contradicted the rule the identity comparison next to it
        already follows — unverifiable is not verified.
        """
        lifecycle, _, agent, wc = self._make_lifecycle()
        state = self._stored("old-session-id", backend_identity("claude", "/srv/work"))
        state.room_id = ""

        await self._start(lifecycle, wc, state)

        agent.create_session.assert_called_once()

    async def test_an_empty_stored_identity_forces_a_fresh_session(self):
        """Unverifiable is not verified — and this case outlives the migration window.

        `backend_identity` defaults to `""` and is not in `_REQUIRED_FIELDS`, so a state
        file that simply omits the key loads as empty for as long as the format lives.
        Treating that as "matches" would resume an id no one can attribute to a store.
        """
        lifecycle, _, agent, wc = self._make_lifecycle()
        state = self._stored("old-session-id", "")

        await self._start(lifecycle, wc, state)

        agent.create_session.assert_called_once()

    async def test_the_abandoned_session_is_not_deleted(self):
        """Deleting would run against the *current* backend, where the old id means
        nothing — or means someone else's session, which is the exact confusion the
        comparison exists to avoid. The old store keeps what it had."""
        lifecycle, _, agent, wc = self._make_lifecycle(working_directory="/srv/moved")
        state = self._stored("old-session-id", backend_identity("claude", "/srv/work"))

        await self._start(lifecycle, wc, state)

        for call in agent.delete_session.call_args_list:
            self.assertNotIn("old-session-id", call.args)

    async def test_a_forced_fresh_session_receives_the_history_handoff(self):
        """Deliberate, not incidental: an identity mismatch produces a genuinely new
        session, and a new session is exactly what the handoff is for. Recorded as a test
        so it reads as a decision rather than a side effect of `created_new_session`."""
        lifecycle, connector, _, wc = self._make_lifecycle(working_directory="/srv/moved")
        state = self._stored("old-session-id", backend_identity("claude", "/srv/work"))

        await self._start(lifecycle, wc, state)

        connector.fetch_room_history.assert_called_once()

    async def test_the_warning_carries_the_whole_abandoned_id(self):
        """The record is overwritten, so this log line is the only surviving copy.

        Session ids are logged truncated (`[:8]`) everywhere else, which is right for
        routine lines and wrong here: the user guide tells an operator to resume the
        abandoned conversation with the backend's own tooling, and half an id cannot be
        pasted into anything. A truncation added later "for consistency" would leave the
        documented recovery impossible with nothing failing.
        """
        lifecycle, _, _, wc = self._make_lifecycle(working_directory="/srv/moved")
        old_id = "01234567-89ab-cdef-0123-456789abcdef"
        state = self._stored(old_id, backend_identity("claude", "/srv/work"))

        with self.assertLogs("coop.core.watcher_lifecycle", "WARNING") as logs:
            await self._start(lifecycle, wc, state)

        self.assertTrue(
            any(old_id in line for line in logs.output),
            f"the full session id must appear in the warning; got {logs.output}")

    async def test_a_replacement_session_starts_with_context_undelivered(self):
        """The flag describes a *session*, and the replacement has received nothing.

        Both shipped backends ignore `already_delivered`, so this changes no behaviour
        today — it keeps the record true, and keeps the invariant `reset_watcher` already
        maintains (clearing a session id clears this flag) from having an exception that
        depends on which code path replaced the session.
        """
        lifecycle, _, _, wc = self._make_lifecycle(working_directory="/srv/moved")
        state = self._stored("old-session-id", backend_identity("claude", "/srv/work"))
        state.context_injected = True

        await self._start(lifecycle, wc, state)

        # `ensure()` sets the flag back to True on success, so asserting the flag after
        # startup would pass either way. The observable that matters is what the backend
        # was *told*: `already_delivered` is the value the contract lets it skip on.
        kwargs = lifecycle._agents["claude"].ensure_durable_instructions.call_args.kwargs
        self.assertFalse(
            kwargs["already_delivered"],
            "a replacement session must not be told its context was already delivered")

    async def test_a_reused_session_is_told_its_context_was_delivered(self):
        """The other half, so the assertion above pins a distinction rather than a
        constant: a genuinely resumed session keeps its history."""
        lifecycle, _, _, wc = self._make_lifecycle()
        state = self._stored("old-session-id", backend_identity("claude", "/srv/work"))
        state.context_injected = True

        await self._start(lifecycle, wc, state)

        kwargs = lifecycle._agents["claude"].ensure_durable_instructions.call_args.kwargs
        self.assertTrue(kwargs["already_delivered"])

    async def test_the_abandoned_session_takes_its_retry_bookkeeping_with_it(self):
        """Found by checking `reset_watcher`, not reported: it pairs "clear the session"
        with `injector.reset_session()`, and an identity mismatch replaces a session
        without passing through it. Left alone, a watcher that had reached
        failed_degraded would carry that verdict into a session never injected at all."""
        lifecycle, _, _, wc = self._make_lifecycle(working_directory="/srv/moved")
        state = self._stored("old-session-id", backend_identity("claude", "/srv/work"))
        lifecycle._injector.reset_session = MagicMock()

        await self._start(lifecycle, wc, state)

        lifecycle._injector.reset_session.assert_called_once_with("old-session-id")

    async def test_a_reused_session_keeps_its_bookkeeping(self):
        """The other half: reuse must not reset anything, or every restart would clear
        the retry counter it exists to accumulate."""
        lifecycle, _, _, wc = self._make_lifecycle()
        state = self._stored("old-session-id", backend_identity("claude", "/srv/work"))
        lifecycle._injector.reset_session = MagicMock()

        await self._start(lifecycle, wc, state)

        lifecycle._injector.reset_session.assert_not_called()

    async def test_what_is_stored_is_what_the_next_run_compares(self):
        """The round trip, which asserting a literal string would not catch.

        Compared and stored are one value threaded through `_start_watcher`; if they ever
        became two derivations, each could pass its own test while every restart forced a
        fresh session. Start once, feed the resulting record back in, and require reuse.
        """
        lifecycle, _, agent, wc = self._make_lifecycle()

        await self._start(lifecycle, wc, None)
        first = lifecycle.get_watcher_state("w1")
        self.assertEqual(first.session_id, "fresh-session-id")
        self.assertEqual(first.backend_identity, backend_identity("claude", "/srv/work"))

        lifecycle2, _, agent2, wc2 = self._make_lifecycle()
        await self._start(lifecycle2, wc2, first)

        agent2.create_session.assert_not_called()
        self.assertEqual(lifecycle2.get_watcher_state("w1").session_id, "fresh-session-id")


if __name__ == "__main__":
    unittest.main()
