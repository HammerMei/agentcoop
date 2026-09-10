"""Tests for GatewayService startup/shutdown lifecycle hardening."""

from __future__ import annotations

import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.core.bot_identity import (
    BotIdentity,
    ConnectorIdentityError,
    DmClaim,
    DuplicateBotIdentityError,
    canonical_origin,
)
from gateway.core.session_manager import JOBS_CANCELLED_BOT_REMOVED as REMOVED
from gateway.service import GatewayService
from tests.helpers import make_bare_gateway_service


def _make_service() -> GatewayService:
    return make_bare_gateway_service()


def _accountless():
    """A connector declaring no shared bot account, which is what these tests are.

    `bot_identity()` returning None is the base class's answer for a connector with no
    account other connectors could also authenticate as — see gateway/core/connector.py.
    """
    connector = MagicMock()
    connector.bot_identity = MagicMock(return_value=None)
    return connector


class TestGatewayServiceRun(unittest.IsolatedAsyncioTestCase):
    async def test_startup_failure_writes_handshake_error_and_closes_fd(self):
        service = _make_service()
        service._runtime_manager.start_all = AsyncMock(return_value=[])
        service._runtime_manager.has_active_brokers = False
        service._runtime_manager.unavailable_agents = set()
        sm = MagicMock()
        sm.connect_only = AsyncMock()
        sm.settle_records = AsyncMock(return_value=[])
        sm.sync_only = AsyncMock(return_value=[])
        sm.shutdown = AsyncMock()
        service._entries = [
            SimpleNamespace(name="script", session_manager=sm, connector=_accountless())
        ]
        service._control.start = AsyncMock(side_effect=RuntimeError("control boom"))
        service._control.stop = AsyncMock()
        service._runtime_manager.stop_all = AsyncMock()

        rfd, wfd = os.pipe()
        try:
            with self.assertRaisesRegex(RuntimeError, "control boom"):
                await service.run(startup_fd=wfd)
            payload = os.read(rfd, 4096).decode()
        finally:
            os.close(rfd)

        self.assertIn("error:startup failed: control boom", payload)
        # Fatal startup failure must NOT emit "ok" — emitting it would cause
        # the parent to report "degraded startup" even though the daemon crashed.
        self.assertNotIn("ok", payload)
        service._runtime_manager.stop_all.assert_awaited_once()
        service._control.stop.assert_awaited_once()

    async def test_one_connector_failing_fast_does_not_orphan_a_slower_ones_state(self):
        """Repro for the user-reported bug: create a connector with a bad
        URL, restart — the daemon fails to start (as expected), but a
        PREVIOUSLY-WORKING connector's watchers also had their session
        state reset.

        Root cause: `run()`'s `asyncio.gather(*[sm.run_once() for e in
        entries])` has no `return_exceptions=True`. When one SessionManager's
        run_once() raises quickly (a bad connector failing to connect) while
        another's is still in-flight (a real RC/Mattermost login + DDP
        handshake, which takes longer), gather() propagates the first
        exception immediately WITHOUT cancelling the still-running sibling
        task — it keeps running as an orphan. run()'s `except` block then
        calls `shutdown()` on ALL entries, including the one whose run_once()
        hadn't finished populating its watcher states yet. That
        SessionManager's save_state() then unconditionally overwrites
        state.<connector>.json with an empty/partial dict, wiping out
        session IDs that were never touched by anything in this run.

        This test drives the REAL run()/shutdown() flow (only the two
        SessionManagers are mocked) so it fails against the real orphaned-
        task behavior and passes once run() is fixed to await/cancel
        siblings before shutting anything down.
        """
        service = _make_service()
        service._runtime_manager.start_all = AsyncMock(return_value=[])
        service._runtime_manager.has_active_brokers = False
        service._runtime_manager.unavailable_agents = set()
        service._runtime_manager.stop_all = AsyncMock()
        service._control.start = AsyncMock()
        service._control.stop = AsyncMock()

        good_run_once_finished = asyncio.Event()
        good_shutdown_called_before_finished = False

        async def slow_good_connect(**kwargs):
            nonlocal good_shutdown_called_before_finished
            # Simulate a real RC/Mattermost connect() taking longer than a
            # bad connector's near-instant DNS/connection-refused failure.
            # The slowness belongs to the CONNECT phase specifically: that is
            # where a login plus handshake actually is slow, and it is the
            # phase whose failure the identity barrier now sits behind.
            await asyncio.sleep(0.05)
            good_run_once_finished.set()
            return []

        async def good_shutdown():
            nonlocal good_shutdown_called_before_finished
            if not good_run_once_finished.is_set():
                good_shutdown_called_before_finished = True

        good_sm = MagicMock()
        good_sm.connect_only = AsyncMock(side_effect=slow_good_connect)
        good_sm.settle_records = AsyncMock(return_value=[])
        good_sm.sync_only = AsyncMock(return_value=[])
        good_sm.shutdown = AsyncMock(side_effect=good_shutdown)

        bad_sm = MagicMock()
        bad_sm.connect_only = AsyncMock(side_effect=ConnectionError("bad url: test"))
        bad_sm.settle_records = AsyncMock(return_value=[])
        bad_sm.sync_only = AsyncMock(return_value=[])
        bad_sm.shutdown = AsyncMock()

        service._entries = [
            SimpleNamespace(
                name="good-existing-connector",
                session_manager=good_sm,
                connector=_accountless(),
            ),
            SimpleNamespace(
                name="bad-new-connector",
                session_manager=bad_sm,
                connector=_accountless(),
            ),
        ]

        with self.assertRaises(ConnectionError):
            await service.run(startup_fd=-1)

        # Give the orphaned good_sm.run_once() task a chance to actually
        # finish in the background, so the assertion below reflects what
        # happened DURING shutdown, not a timing fluke of the test itself.
        await asyncio.sleep(0.1)

        self.assertFalse(
            good_shutdown_called_before_finished,
            "good-existing-connector's shutdown() (and therefore save_state()) "
            "ran before its run_once() had finished populating watcher "
            "state — this is the orphaned-task race that wipes out session "
            "IDs for a connector that was never actually part of the failure.",
        )


class TestTheExpiryExemptionOracleIsGone(unittest.TestCase):
    """What was removed, and why nothing replaced it.

    `_has_pending_jobs` answered "does a pending scheduled job target this
    watcher", and the sweep used it to exempt that room from expiry. Codex round
    10 had made it honour the scheduler's own connector fallback, so the two
    agreed about which jobs claimed a watcher.

    Both are gone (owner, 2026-08-31). The exemption existed because "expiry
    deletes the record the recreation reads from, leaving the job pointing at
    nothing" — and a job now records the room it targets and resurrects it, so
    there is no record to protect. The cancel-side rule the oracle's fallback
    logic was shared with is still live and still tested (see
    `TestTheCancellationClaimRule` below).
    """

    def test_the_service_no_longer_answers_it(self):
        from gateway.service import GatewayService

        self.assertFalse(hasattr(GatewayService, "_has_pending_jobs"))


class TestTheCancellationClaimRule(unittest.TestCase):
    """`_cancel_jobs_for` — the cancel side, which is STILL LIVE.

    Restored after a review found it had lost its only test: the previous attempt
    deleted five tests when the exemption oracle went, and one of them covered
    this. `_cancel_jobs_for` is production code, wired to the membership-remove
    handler, and it carries Codex round 11's claim rule — a job with an empty or
    stale `connector` is DELIVERABLE to this watcher through the scheduler's
    fallback scan, so it must also be CANCELLABLE through it, or a reclaim leaves
    it orphaned: failing resolution forever if active, listed permanently if
    paused.
    """

    def _service_with_jobs(self, jobs):
        """`jobs` items are `(id, watcher, connector)` or `(id, watcher,
        connector, room_id)`. A job with no room id is a pre-schema-2 one, which
        is matched by handle."""
        from gateway.schedule_types import JobStatus, ScheduledJob

        service = _make_service()
        entry = MagicMock()
        entry.name = "rc"
        service._entries = [entry]
        service._job_store = MagicMock()
        service._job_store.list_jobs = MagicMock(return_value=[
            ScheduledJob(id=spec[0], watcher=spec[1], connector=spec[2],
                         room_id=(spec[3] if len(spec) > 3 else ""),
                         status=JobStatus.ACTIVE)
            for spec in jobs
        ])
        service._job_store.cancel = MagicMock(return_value=True)
        return service

    def test_a_job_on_this_connector_is_cancelled(self):
        service = self._service_with_jobs([("acg-1", "rc:general", "rc")])

        service._cancel_jobs_for("rc", "room-1", legacy_handle="rc:general",
                                 reason=REMOVED[0], advice=REMOVED[1])

        service._job_store.cancel.assert_called_once()

        self.assertEqual(service._job_store.cancel.call_args.args[0], "acg-1")
        self.assertEqual(service._job_store.cancel.call_args.kwargs["reason"],
                         "the bot was removed from the room, so the job could never deliver")

    def test_a_job_with_no_connector_is_cancelled_too(self):
        """The fallback claim: the scheduler would deliver it here, so a reclaim
        must be able to cancel it here."""
        service = self._service_with_jobs([("acg-1", "rc:general", "")])

        service._cancel_jobs_for("rc", "room-1", legacy_handle="rc:general",
                                 reason=REMOVED[0], advice=REMOVED[1])

        service._job_store.cancel.assert_called_once()

        self.assertEqual(service._job_store.cancel.call_args.args[0], "acg-1")

    def test_a_job_naming_a_connector_that_no_longer_exists_is_cancelled(self):
        service = self._service_with_jobs([("acg-1", "rc:general", "retired")])

        service._cancel_jobs_for("rc", "room-1", legacy_handle="rc:general",
                                 reason=REMOVED[0], advice=REMOVED[1])

        service._job_store.cancel.assert_called_once()

        self.assertEqual(service._job_store.cancel.call_args.args[0], "acg-1")

    def test_another_configured_connectors_job_is_left_alone(self):
        """The one case the fallback must NOT swallow: `mm` is configured, so its
        job is deliverable there and is not this reclaim's business."""
        service = self._service_with_jobs([("acg-1", "rc:general", "mm")])
        other = MagicMock()
        other.name = "mm"
        service._entries.append(other)

        service._cancel_jobs_for("rc", "room-1", legacy_handle="rc:general",
                                 reason=REMOVED[0], advice=REMOVED[1])

        service._job_store.cancel.assert_not_called()

    def test_another_watchers_job_is_left_alone(self):
        service = self._service_with_jobs([("acg-1", "rc:dev", "rc")])

        service._cancel_jobs_for("rc", "room-1", legacy_handle="rc:general",
                                 reason=REMOVED[0], advice=REMOVED[1])

        service._job_store.cancel.assert_not_called()
class TestIdentityBarrier(unittest.IsolatedAsyncioTestCase):
    """The barrier's value is its *position*, so these assert ordering, not just refusal.

    A check that rejects duplicates after one connector has subscribed prevents nothing:
    that connector is already receiving and answering. `find_identity_conflicts` is unit
    tested elsewhere; what cannot be tested there is that startup runs it between the
    two phases, because a SessionManager owns one connector and can never see the pair.
    """

    def _service_with(self, *identities, dms=None):
        service = _make_service()
        service._runtime_manager.start_all = AsyncMock(return_value=[])
        service._runtime_manager.has_active_brokers = False
        service._runtime_manager.unavailable_agents = set()
        service._runtime_manager.stop_all = AsyncMock()
        service._control.start = AsyncMock()
        service._control.stop = AsyncMock()
        service._dm_claims = dict(dms or {})

        entries = []
        for i, identity in enumerate(identities):
            sm = MagicMock()
            sm.connect_only = AsyncMock()
            sm.settle_records = AsyncMock(return_value=[])
            sm.sync_only = AsyncMock(return_value=[])
            sm.shutdown = AsyncMock()
            connector = MagicMock()
            connector.bot_identity = MagicMock(return_value=identity)
            entries.append(
                SimpleNamespace(name=f"c{i}", session_manager=sm, connector=connector))
        service._entries = entries
        return service

    async def test_a_shared_account_is_refused_before_anything_subscribes(self):
        same = BotIdentity("rocketchat", "https://chat.example.com", "user-abc")
        service = self._service_with(same, same)

        with self.assertRaises(DuplicateBotIdentityError):
            await service.run(startup_fd=-1)

        for entry in service._entries:
            entry.session_manager.connect_only.assert_awaited_once()
            entry.session_manager.sync_only.assert_not_awaited()

    async def test_distinct_accounts_reach_the_second_phase(self):
        """Otherwise the test above would pass against a barrier that refuses always."""
        service = self._service_with(
            BotIdentity("rocketchat", "https://chat.example.com", "user-a"),
            BotIdentity("rocketchat", "https://chat.example.com", "user-b"),
        )
        service._control.start = AsyncMock(side_effect=RuntimeError("stop here"))

        with self.assertRaisesRegex(RuntimeError, "stop here"):
            await service.run(startup_fd=-1)

        for entry in service._entries:
            entry.session_manager.sync_only.assert_awaited_once()

    async def test_the_url_spelling_does_not_decide_it(self):
        """Two operators writing one server differently is a duplicate, not two."""
        service = self._service_with(
            BotIdentity("rocketchat", canonical_origin("https://chat.example.com/"), "user-abc"),
            BotIdentity("rocketchat", canonical_origin("https://chat.example.com:443"), "user-abc"),
        )

        with self.assertRaises(DuplicateBotIdentityError):
            await service.run(startup_fd=-1)

    async def test_a_connector_that_cannot_identify_itself_stops_startup(self):
        """Fail-closed: unanswerable cannot be compared, so it does not start."""
        service = self._service_with(BotIdentity("rocketchat", "https://s", "u1"))
        service._entries[0].connector.bot_identity = MagicMock(
            side_effect=ConnectorIdentityError("whoami failed"))

        with self.assertRaises(ConnectorIdentityError):
            await service.run(startup_fd=-1)

        service._entries[0].session_manager.sync_only.assert_not_awaited()

    async def test_two_overlapping_dm_claims_across_teams_are_refused(self):
        """The exception's condition, wired: the claims come from the service's own map,
        derived from both watcher shapes at construction."""
        service = self._service_with(
            BotIdentity("mattermost", "https://mm.example.com", "user-abc", scope="team-1"),
            BotIdentity("mattermost", "https://mm.example.com", "user-abc", scope="team-2"),
            dms={"c0": DmClaim(direct=True), "c1": DmClaim(direct=True)},
        )

        with self.assertRaises(DuplicateBotIdentityError) as cm:
            await service.run(startup_fd=-1)
        self.assertIn("direct message", str(cm.exception).lower())

    async def test_different_teams_without_dm_overlap_start_normally(self):
        service = self._service_with(
            BotIdentity("mattermost", "https://mm.example.com", "user-abc", scope="team-1"),
            BotIdentity("mattermost", "https://mm.example.com", "user-abc", scope="team-2"),
            dms={"c0": DmClaim(direct=True)},
        )
        service._control.start = AsyncMock(side_effect=RuntimeError("stop here"))

        with self.assertRaisesRegex(RuntimeError, "stop here"):
            await service.run(startup_fd=-1)

        for entry in service._entries:
            entry.session_manager.sync_only.assert_awaited_once()


class TestGatewayServiceShutdown(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_continues_after_session_manager_failure(self):
        service = _make_service()
        bad_sm = MagicMock()
        bad_sm.shutdown = AsyncMock(side_effect=RuntimeError("sm failed"))
        good_sm = MagicMock()
        good_sm.shutdown = AsyncMock()
        service._entries = [
            SimpleNamespace(name="bad", session_manager=bad_sm),
            SimpleNamespace(name="good", session_manager=good_sm),
        ]
        service._runtime_manager.stop_all = AsyncMock()
        service._control.stop = AsyncMock()

        expiry_task = AsyncMock()
        expiry_task.cancel = MagicMock()
        service._expiry_task = expiry_task

        await service.shutdown()

        bad_sm.shutdown.assert_awaited_once()
        good_sm.shutdown.assert_awaited_once()
        service._runtime_manager.stop_all.assert_awaited_once()
        expiry_task.cancel.assert_called_once()
        service._control.stop.assert_awaited_once()

    async def test_shutdown_continues_after_runtime_manager_failure(self):
        service = _make_service()
        sm = MagicMock()
        sm.shutdown = AsyncMock()
        service._entries = [SimpleNamespace(name="only", session_manager=sm)]
        service._runtime_manager.stop_all = AsyncMock(
            side_effect=RuntimeError("rt failed")
        )
        service._control.stop = AsyncMock()

        await service.shutdown()

        sm.shutdown.assert_awaited_once()
        service._control.stop.assert_awaited_once()


class TestWriteStartupSignal(unittest.TestCase):
    """_write_startup_signal() — protocol correctness for success vs. fatal paths.

    Bug fixed: previously the function always appended "ok\\n", so a fatal
    startup failure would still cause the parent to report "degraded startup"
    even though the daemon had already crashed.
    """

    def _read_pipe(self, wfd: int, rfd: int) -> str:
        """Write nothing more, close write-end, read everything from read-end."""
        try:
            os.close(wfd)
        except OSError:
            pass
        data = b""
        try:
            while chunk := os.read(rfd, 4096):
                data += chunk
        except OSError:
            pass
        finally:
            try:
                os.close(rfd)
            except OSError:
                pass
        return data.decode()

    # ── Success path ──────────────────────────────────────────────────────────

    def test_success_no_warnings_emits_ok(self):
        """Clean startup (no errors) must emit exactly 'ok\\n'."""
        from gateway.service import _write_startup_signal

        rfd, wfd = os.pipe()
        _write_startup_signal(wfd, [])
        payload = self._read_pipe(-1, rfd)  # wfd already closed by function
        self.assertEqual(payload.strip(), "ok")
        self.assertNotIn("error:", payload)

    def test_success_with_warnings_emits_errors_and_ok(self):
        """Degraded startup (non-fatal warnings) must emit error lines AND ok."""
        from gateway.service import _write_startup_signal

        rfd, wfd = os.pipe()
        _write_startup_signal(wfd, ["agent foo unavailable", "watcher bar skipped"])
        payload = self._read_pipe(-1, rfd)
        self.assertIn("error:agent foo unavailable", payload)
        self.assertIn("error:watcher bar skipped", payload)
        self.assertIn("ok", payload)
        # ok must appear AFTER error lines (last line)
        lines = [line for line in payload.splitlines() if line.strip()]
        self.assertEqual(lines[-1], "ok")

    # ── Fatal path ────────────────────────────────────────────────────────────

    def test_fatal_no_ok_emitted(self):
        """Fatal failure must NOT emit 'ok' — parent must see failure."""
        from gateway.service import _write_startup_signal

        rfd, wfd = os.pipe()
        _write_startup_signal(wfd, ["startup failed: connection refused"], fatal=True)
        payload = self._read_pipe(-1, rfd)
        self.assertIn("error:startup failed: connection refused", payload)
        self.assertNotIn("ok", payload)

    def test_fatal_empty_errors_no_ok(self):
        """fatal=True with no error messages still must not emit 'ok'."""
        from gateway.service import _write_startup_signal

        rfd, wfd = os.pipe()
        _write_startup_signal(wfd, [], fatal=True)
        payload = self._read_pipe(-1, rfd)
        self.assertNotIn("ok", payload)

    def test_fatal_multiple_errors_no_ok(self):
        """Multiple error lines on fatal path — none of them must be 'ok'."""
        from gateway.service import _write_startup_signal

        rfd, wfd = os.pipe()
        _write_startup_signal(wfd, ["err1", "err2", "err3"], fatal=True)
        payload = self._read_pipe(-1, rfd)
        for line in payload.splitlines():
            self.assertNotEqual(line.strip(), "ok", f"unexpected 'ok' line: {line!r}")

    # ── Newline sanitization ───────────────────────────────────────────────────

    def test_newlines_in_errors_sanitized(self):
        """Embedded newlines in error messages must not split the protocol lines."""
        from gateway.service import _write_startup_signal

        rfd, wfd = os.pipe()
        _write_startup_signal(wfd, ["line one\nline two"], fatal=True)
        payload = self._read_pipe(-1, rfd)
        # Must be a single error: line with the newline replaced by space
        error_lines = [line for line in payload.splitlines() if line.startswith("error:")]
        self.assertEqual(len(error_lines), 1)
        self.assertIn("line one line two", error_lines[0])

    # ── OSError on write is logged, not raised (E1) ──────────────────────────

    def test_oserror_on_write_is_logged_not_raised(self):
        """E1: OSError when writing the startup signal must be logged (not raised).

        If the write fails (e.g. closed fd, EPIPE), the function must still
        close the fd so the parent receives EOF and can unblock.  The error
        must appear in the log rather than being silently swallowed.
        """
        from gateway.service import _write_startup_signal

        # Create a pipe, then close the write end before passing it to the
        # function — any write attempt will raise EBADF (bad file descriptor).
        rfd, wfd = os.pipe()
        os.close(wfd)  # pre-close to force OSError on write

        with self.assertLogs("coop.service", level="WARNING") as log_ctx:
            # Must NOT raise — the OSError must be caught and logged.
            _write_startup_signal(wfd, [])

        # The warning must mention the fd and the OSError
        combined = " ".join(log_ctx.output)
        self.assertIn("startup signal", combined.lower())

        # Clean up the read end
        try:
            os.close(rfd)
        except OSError:
            pass

    def test_oserror_on_write_still_closes_fd(self):
        """After an OSError on write, the fd must be closed so the parent unblocks.

        We verify this by attempting a second close, which must raise EBADF
        (i.e. the fd was already closed by the function's finally block).
        """
        from gateway.service import _write_startup_signal

        rfd, wfd = os.pipe()
        os.close(wfd)  # force OSError on write

        import logging
        # suppress the expected warning so it doesn't pollute test output
        logger = logging.getLogger("coop.service")
        logger.disabled = True
        try:
            _write_startup_signal(wfd, [])
        finally:
            logger.disabled = False

        # The fd should already be closed; a second close must raise OSError/EBADF
        with self.assertRaises(OSError):
            os.close(wfd)

        try:
            os.close(rfd)
        except OSError:
            pass

    # ── Parent-side interpretation ────────────────────────────────────────────

    def test_parent_sees_failure_when_no_ok(self):
        """_wait_for_startup_signal must exit(1) when no 'ok' line is received.

        This simulates the daemon writing only error lines (fatal=True path)
        and verifies the parent correctly interprets absence of 'ok' as failure.
        """
        from gateway.daemon import _wait_for_startup_signal

        rfd, wfd = os.pipe()
        # Write error-only payload (no ok) then close write end
        os.write(wfd, b"error:startup failed: kaboom\n")
        os.close(wfd)

        with self.assertRaises(SystemExit) as cm:
            _wait_for_startup_signal(rfd)
        self.assertEqual(cm.exception.code, 1)

    def test_parent_sees_degraded_when_errors_and_ok(self):
        """_wait_for_startup_signal must exit(0) with warnings on degraded startup."""
        from gateway.daemon import _wait_for_startup_signal

        rfd, wfd = os.pipe()
        os.write(wfd, b"error:watcher foo missing\nok\n")
        os.close(wfd)

        import io
        from contextlib import redirect_stderr, redirect_stdout

        out = io.StringIO()
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm:
            with redirect_stdout(out), redirect_stderr(err):
                _wait_for_startup_signal(rfd)
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("watcher foo missing", err.getvalue())

    def test_parent_sees_success_on_clean_ok(self):
        """_wait_for_startup_signal must exit(0) on clean 'ok\\n' response."""
        from gateway.daemon import _wait_for_startup_signal

        rfd, wfd = os.pipe()
        os.write(wfd, b"ok\n")
        os.close(wfd)

        import io
        from contextlib import redirect_stdout

        out = io.StringIO()
        with self.assertRaises(SystemExit) as cm:
            with redirect_stdout(out):
                _wait_for_startup_signal(rfd)
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("successfully", out.getvalue())


class TestSanitizePipeMessage(unittest.TestCase):
    """sanitize_pipe_message() — shared by _write_startup_signal() above AND
    gateway/daemon.py's own pipe writes (lock-acquire/config-migration/
    config-load/service-crash failures). Code-review finding: daemon.py
    used to keep its own local, byte-for-byte duplicate of this exact
    logic, applied at only 2 of its 5 write sites — consolidated here so
    there's one definition, imported and applied everywhere."""

    def test_strips_embedded_newlines(self):
        from gateway.service import sanitize_pipe_message

        result = sanitize_pipe_message("line one\nline two\r\nline three")
        self.assertNotIn("\n", result)
        self.assertNotIn("\r", result)

    def test_leaves_a_single_line_message_unchanged(self):
        from gateway.service import sanitize_pipe_message

        self.assertEqual(sanitize_pipe_message("all good"), "all good")

    def test_daemon_module_imports_the_same_function_not_a_local_copy(self):
        import gateway.daemon
        import gateway.service

        self.assertIs(gateway.daemon.sanitize_pipe_message, gateway.service.sanitize_pipe_message)


class TestServiceRunFatalHandshake(unittest.IsolatedAsyncioTestCase):
    """GatewayService.run() fatal paths must not emit 'ok' to the handshake pipe."""

    def _make_svc(self):
        return make_bare_gateway_service()

    async def test_exception_during_startup_no_ok_in_pipe(self):
        """RuntimeError during startup must not produce 'ok' in the pipe."""
        svc = self._make_svc()
        svc._control.start = AsyncMock(side_effect=RuntimeError("boom"))

        rfd, wfd = os.pipe()
        try:
            with self.assertRaises(RuntimeError):
                await svc.run(startup_fd=wfd)
            payload = os.read(rfd, 4096).decode()
        finally:
            try:
                os.close(rfd)
            except OSError:
                pass

        self.assertIn("startup failed: boom", payload)
        self.assertNotIn("ok", payload)

    async def test_cancelled_during_startup_no_ok_in_pipe(self):
        """CancelledError during startup (e.g. SIGTERM) must not produce 'ok'."""
        import asyncio

        svc = self._make_svc()

        async def _cancel_on_start():
            raise asyncio.CancelledError()

        svc._control.start = _cancel_on_start

        rfd, wfd = os.pipe()
        try:
            try:
                await svc.run(startup_fd=wfd)
            except (asyncio.CancelledError, Exception):
                pass
            payload = os.read(rfd, 4096).decode()
        finally:
            try:
                os.close(rfd)
            except OSError:
                pass

        self.assertIn("startup cancelled", payload)
        self.assertNotIn("ok", payload)

    async def test_successful_startup_emits_ok(self):
        """Successful startup must still emit 'ok' so the parent exits 0."""
        import asyncio

        svc = self._make_svc()

        # Control.start() succeeds; the run loop is cancelled immediately after
        start_called = asyncio.Event()

        async def _start_ok():
            start_called.set()

        svc._control.start = _start_ok

        rfd, wfd = os.pipe()
        try:
            task = asyncio.create_task(svc.run(startup_fd=wfd))
            await start_called.wait()
            # Give run() time to write the signal
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            payload = os.read(rfd, 4096).decode()
        finally:
            try:
                os.close(rfd)
            except OSError:
                pass

        self.assertIn("ok", payload)


# ── Appended from test_round7_fixes.py ────────────────────────────────────────


class TestStartupFdOnCancel(unittest.IsolatedAsyncioTestCase):
    """startup_fd must be closed even if CancelledError is raised during startup."""

    async def test_startup_fd_written_on_cancelled_error(self):
        """_write_startup_signal must be called in finally even after CancelledError."""
        import asyncio


        # The cancellation arrives mid-startup — at the control socket's start.
        # (The hand-built fixture this test used to carry crashed on a missing
        # attribute first, so the CancelledError path was never actually taken.)
        svc = make_bare_gateway_service()
        svc._control.start = AsyncMock(side_effect=asyncio.CancelledError())

        write_signal_calls: list = []

        def fake_write_signal(fd, errors, *, fatal=False):
            write_signal_calls.append((fd, errors))

        with (
            patch("gateway.service._write_startup_signal", side_effect=fake_write_signal),
            patch("gateway.service.ConnectorPermissionNotifier"),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await svc.run(startup_fd=5)

        fds_written = [fd for fd, _ in write_signal_calls]
        self.assertIn(5, fds_written, "startup_fd must be written/closed in finally on CancelledError")

class TestCancellationRequiresOwnershipNotJustTheRoom(unittest.TestCase):
    """Cancelling by ROOM alone let one connector delete another's job.

    `_claims_this_room` matched a job by `room_id`, and the surrounding filter
    admitted it when `j.connector not in configured` — a clause added so a job
    with a stale connector, deliverable through the scheduler's fallback scan,
    stayed cancellable too. But a room id does not name an owner: ids are
    per-server, and the canonical multi-agent setup is one account per agent in
    the same rooms. So a job belonging to a connector that had been renamed away
    was deleted by a DIFFERENT connector's membership event, under an audit line
    saying the bot had been removed from the room. It had not been removed from
    that agent's account.

    Cancellation is destructive and unappealable, so ambiguity must not resolve
    to "delete". The job is left instead: it fails loudly at its next fire, which
    an operator can still repair.
    """

    def _service_with_jobs(self, jobs):
        from gateway.schedule_types import JobStatus, ScheduledJob

        service = _make_service()
        entry = MagicMock()
        entry.name = "rc"
        service._entries = [entry]
        service._job_store = MagicMock()
        service._job_store.list_jobs = MagicMock(return_value=[
            ScheduledJob(id=spec[0], watcher=spec[1], connector=spec[2],
                         room_id=(spec[3] if len(spec) > 3 else ""),
                         status=JobStatus.ACTIVE)
            for spec in jobs
        ])
        service._job_store.cancel = MagicMock(return_value=True)
        return service

    def _removed(self, service):
        return [c.args[0] for c in service._job_store.cancel.call_args_list]

    def test_another_connectors_job_in_the_same_room_is_left_alone(self):
        service = self._service_with_jobs([
            ("acg-mine", "rc:general", "rc", "room-1"),
            ("acg-theirs", "alice:general", "alice", "room-1"),
        ])

        service._cancel_jobs_for("rc", "room-1", legacy_handle="rc:general",
                                 reason=REMOVED[0], advice=REMOVED[1])

        self.assertEqual(self._removed(service), ["acg-mine"])

    def test_a_retired_connectors_job_is_left_alone_too(self):
        """The exact case the removed escape clause admitted: `alice` is not in
        `configured`, so the old filter treated its job as fair game."""
        service = self._service_with_jobs([
            ("acg-theirs", "alice:general", "alice", "room-1"),
        ])

        service._cancel_jobs_for("rc", "room-1", legacy_handle="rc:general",
                                 reason=REMOVED[0], advice=REMOVED[1])

        self.assertEqual(self._removed(service), [])

    def test_a_pre_schema_2_job_is_still_cancelled_by_its_handle(self):
        """The case the escape clause existed for, kept: the job's `connector`
        field is empty, so ownership comes from the handle's prefix — which is
        this connector. Note the handle alone is NOT enough when the connector
        field names a configured connector; see
        `TestTheCancellationClaimRule::test_another_configured_connectors_job_is_left_alone`,
        because delivery reads that field first."""
        service = self._service_with_jobs([
            ("acg-old", "rc:general", ""),
        ])

        service._cancel_jobs_for("rc", "room-1", legacy_handle="rc:general",
                                 reason=REMOVED[0], advice=REMOVED[1])

        self.assertEqual(self._removed(service), ["acg-old"])

    def test_a_pre_schema_2_job_under_another_connectors_handle_is_left_alone(self):
        service = self._service_with_jobs([
            ("acg-old", "alice:general", ""),
        ])

        service._cancel_jobs_for("rc", "room-1", legacy_handle="rc:general",
                                 reason=REMOVED[0], advice=REMOVED[1])

        self.assertEqual(self._removed(service), [])


class TestCancellationMatchesByRoomNotByHandle(unittest.TestCase):
    """Found by the sweep, after the same defect class appeared three times.

    A handle can be taken over by another room once the original's record is
    reclaimed — which this branch made routine by removing the expiry exemption
    for job-bearing rooms. Matching jobs by handle was then wrong in BOTH
    directions, and both were silent:

    * a live room's job was deleted under the audit line "the bot was removed
      from the room", which is false for that room;
    * this room's own job, if its handle had since moved, was left firing at a
      room the bot had left.
    """

    def _service(self, jobs):
        from gateway.schedule_types import JobStatus, ScheduledJob

        service = _make_service()
        entry = MagicMock()
        entry.name = "rc"
        service._entries = [entry]
        service._job_store = MagicMock()
        service._job_store.list_jobs = MagicMock(return_value=[
            ScheduledJob(id=jid, watcher=w, connector="rc", room_id=r,
                         status=JobStatus.ACTIVE)
            for jid, w, r in jobs
        ])
        service._job_store.cancel = MagicMock(return_value=True)
        return service

    def test_another_rooms_job_under_the_same_handle_survives(self):
        """Room B holds the handle and is alive; the bot was removed from A."""
        service = self._service([("acg-b", "rc:general", "room-B")])

        service._cancel_jobs_for("rc", "room-A", legacy_handle="rc:general", reason=REMOVED[0], advice=REMOVED[1])

        service._job_store.cancel.assert_not_called()

    def test_this_rooms_job_is_cancelled_even_under_a_moved_handle(self):
        """A's job was created when A held `rc:general`; A has since been
        renamed, so its record's handle differs. The room id still matches."""
        service = self._service([("acg-a", "rc:general", "room-A")])

        service._cancel_jobs_for("rc", "room-A", legacy_handle="rc:daily-standup", reason=REMOVED[0], advice=REMOVED[1])

        service._job_store.cancel.assert_called_once()

        self.assertEqual(service._job_store.cancel.call_args.args[0], "acg-a")

    def test_a_pre_schema_2_job_still_matches_by_handle(self):
        """It has no id, so the handle is the only key it has."""
        service = self._service([("acg-old", "rc:general", "")])

        service._cancel_jobs_for("rc", "room-A", legacy_handle="rc:general", reason=REMOVED[0], advice=REMOVED[1])

        service._job_store.cancel.assert_called_once()

        self.assertEqual(service._job_store.cancel.call_args.args[0], "acg-old")


class TestRecordsSettleBeforeTheIdentityBarrier(unittest.IsolatedAsyncioTestCase):
    """#143: the identity barrier folds persisted DM records into each connector's
    claim, so the fleet must be reconciled before it runs — a record a deleted
    rule left behind would otherwise refuse a legitimate two-team setup."""

    async def test_settle_runs_before_connect_and_the_identity_check(self):
        service = _make_service()
        service._runtime_manager.start_all = AsyncMock(return_value=[])
        service._runtime_manager.has_active_brokers = False
        service._runtime_manager.unavailable_agents = set()
        service._runtime_manager.stop_all = AsyncMock()
        order = []
        sm = MagicMock()
        sm.settle_records = AsyncMock(side_effect=lambda **kw: order.append("settle") or [])
        sm.connect_only = AsyncMock(side_effect=lambda: order.append("connect"))
        sm.sync_only = AsyncMock(side_effect=lambda **kw: order.append("sync") or [])
        sm.shutdown = AsyncMock()
        service._entries = [
            SimpleNamespace(name="script", session_manager=sm, connector=_accountless())
        ]
        service._check_bot_identities = MagicMock(side_effect=lambda: order.append("identity"))
        service._control.start = AsyncMock(side_effect=RuntimeError("stop here"))
        service._control.stop = AsyncMock()

        with self.assertRaises(RuntimeError):
            await service.run()

        self.assertEqual(order, ["settle", "connect", "identity", "sync"])


if __name__ == "__main__":
    unittest.main()
