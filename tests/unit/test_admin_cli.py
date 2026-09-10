"""Unit tests for gateway.admin.cli: argument parsing and the _run/_dispatch
exit-code and idempotency-notice behavior.

admin_factory/load_profiles/get_profile are patched so these tests exercise
only the CLI's own dispatch/error-handling logic, not real network calls.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from gateway.admin.base import (
    AdminChannel,
    AdminUser,
    ChannelAlreadyExistsError,
    ChannelArchivedError,
    UserAlreadyExistsError,
    UserDeactivatedError,
    UserNotFoundError,
)
from gateway.admin.cli import _configure_error_log, _run, build_parser, main
from gateway.admin.config import AdminConfigError


def _http_status_error(status_code: int, json_body: dict) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://mm.example/api/v4/users")
    response = httpx.Response(status_code, request=request, json=json_body)
    return httpx.HTTPStatusError("error", request=request, response=response)


def _detach_admin_log_handlers(abs_path: str) -> None:
    """Remove handlers a test attached for `abs_path`, so a temp file that is
    about to be deleted is not left wired to a module-level logger."""
    for lg in (
        logging.getLogger("coop.admin.errors"),
        logging.getLogger("coop"),
    ):
        for h in [
            h for h in lg.handlers
            if isinstance(h, logging.FileHandler) and h.baseFilename == abs_path
        ]:
            lg.removeHandler(h)
            h.close()


def _args(argv: list[str]):
    return build_parser().parse_args(argv)


class TestBuildParser(unittest.TestCase):
    def test_create_user_parses_positional_and_optional_args(self):
        args = _args(["mm-lab", "create-user", "alice", "a@x.com", "pw", "--full-name", "Alice A"])
        self.assertEqual(args.profile, "mm-lab")
        self.assertEqual(args.command, "create-user")
        self.assertEqual(args.username, "alice")
        self.assertEqual(args.email, "a@x.com")
        self.assertEqual(args.password, "pw")
        self.assertEqual(args.full_name, "Alice A")

    def test_create_user_rejects_removed_verified_flag(self):
        # --verified was deliberately removed (see PlatformAdmin.create_user):
        # argparse must reject it rather than silently ignoring it.
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            _args(["mm-lab", "create-user", "alice", "a@x.com", "pw", "--verified"])

    def test_create_channel_private_flag(self):
        args = _args(["mm-lab", "create-channel", "secret", "--private"])
        self.assertTrue(args.private)

    def test_create_channel_defaults_to_public(self):
        args = _args(["mm-lab", "create-channel", "eng"])
        self.assertFalse(args.private)

    def test_config_flag_is_optional(self):
        args = _args(["--config", "/tmp/x.yaml", "mm-lab", "delete-user", "alice"])
        self.assertEqual(args.config, "/tmp/x.yaml")

    def test_log_file_defaults_to_msg_admin_log(self):
        args = _args(["mm-lab", "delete-user", "alice"])
        self.assertEqual(args.log_file, "coop-provision.log")

    def test_log_file_flag_overrides_default(self):
        args = _args(["--log-file", "/tmp/custom.log", "mm-lab", "delete-user", "alice"])
        self.assertEqual(args.log_file, "/tmp/custom.log")


class TestRunDispatch(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # _run() always calls _configure_error_log(args.log_file), which
        # defaults to a real, cwd-relative "coop-provision.log" — none of these
        # tests care about that concern (it's covered by TestConfigureErrorLog
        # and TestHttpStatusErrorHandling below), so stub it out to avoid
        # every test in this class writing a stray file to the repo.
        patcher = patch("gateway.admin.cli._configure_error_log")
        self.addCleanup(patcher.stop)
        patcher.start()

    async def test_unknown_profile_returns_1(self):
        with patch("gateway.admin.cli.load_profiles", return_value={}), \
             patch("gateway.admin.cli.get_profile", side_effect=AdminConfigError("nope")):
            args = _args(["ghost", "delete-user", "alice"])
            self.assertEqual(await _run(args), 1)

    async def test_create_user_success_returns_0(self):
        mock_admin = AsyncMock()
        mock_admin.create_user = AsyncMock(
            return_value=AdminUser(id="u1", username="alice", email="a@x.com")
        )
        with patch("gateway.admin.cli.load_profiles", return_value={"p": object()}), \
             patch("gateway.admin.cli.get_profile", return_value=object()), \
             patch("gateway.admin.cli.admin_factory", return_value=mock_admin):
            args = _args(["p", "create-user", "alice", "a@x.com", "pw"])
            code = await _run(args)

        self.assertEqual(code, 0)
        mock_admin.connect.assert_awaited_once()
        mock_admin.close.assert_awaited_once()
        mock_admin.create_user.assert_awaited_once_with(
            "alice", "a@x.com", "pw", full_name=None
        )

    async def test_create_user_already_exists_is_not_an_error(self):
        mock_admin = AsyncMock()
        existing = AdminUser(id="u1", username="alice", email="a@x.com")
        mock_admin.create_user = AsyncMock(side_effect=UserAlreadyExistsError("alice", existing=existing))
        with patch("gateway.admin.cli.load_profiles", return_value={}), \
             patch("gateway.admin.cli.get_profile", return_value=object()), \
             patch("gateway.admin.cli.admin_factory", return_value=mock_admin):
            args = _args(["p", "create-user", "alice", "a@x.com", "pw"])
            code = await _run(args)

        # Idempotent-by-default: already-exists is a note, not a failure.
        self.assertEqual(code, 0)
        mock_admin.close.assert_awaited_once()

    async def test_create_user_identity_mismatch_is_an_error(self):
        # Existing account has a DIFFERENT email than requested — must not
        # be silently treated as a safe retry (see identity_matches).
        mock_admin = AsyncMock()
        existing = AdminUser(id="u1", username="alice", email="someone-else@x.com")
        mock_admin.create_user = AsyncMock(
            side_effect=UserAlreadyExistsError("alice", existing=existing, identity_matches=False)
        )
        stderr = io.StringIO()
        with patch("gateway.admin.cli.load_profiles", return_value={}), \
             patch("gateway.admin.cli.get_profile", return_value=object()), \
             patch("gateway.admin.cli.admin_factory", return_value=mock_admin), \
             contextlib.redirect_stderr(stderr):
            args = _args(["p", "create-user", "alice", "a@x.com", "pw"])
            code = await _run(args)

        self.assertEqual(code, 1)
        self.assertIn("already exists but with a different email", stderr.getvalue())
        mock_admin.close.assert_awaited_once()

    async def test_create_user_deactivated_account_is_an_error(self):
        # End-to-end proof of the property UserDeactivatedError's docstring
        # depends on: because it is NOT a UserAlreadyExistsError subclass, it
        # falls through to _run()'s broad handler and exits 1 rather than
        # being reported as an idempotent skip.
        mock_admin = AsyncMock()
        existing = AdminUser(id="u1", username="alice", email="a@x.com", deactivated=True)
        mock_admin.create_user = AsyncMock(
            side_effect=UserDeactivatedError("alice", existing=existing)
        )
        stderr = io.StringIO()
        with patch("gateway.admin.cli.load_profiles", return_value={}), \
             patch("gateway.admin.cli.get_profile", return_value=object()), \
             patch("gateway.admin.cli.admin_factory", return_value=mock_admin), \
             contextlib.redirect_stderr(stderr):
            args = _args(["p", "create-user", "alice", "a@x.com", "pw"])
            code = await _run(args)

        self.assertEqual(code, 1)
        output = stderr.getvalue()
        self.assertIn("deactivated", output)
        self.assertNotIn("skipping", output)
        mock_admin.close.assert_awaited_once()

    async def test_create_channel_already_exists_is_not_an_error(self):
        mock_admin = AsyncMock()
        existing = AdminChannel(id="c1", name="eng", is_private=False)
        mock_admin.create_channel = AsyncMock(
            side_effect=ChannelAlreadyExistsError("eng", existing=existing)
        )
        with patch("gateway.admin.cli.load_profiles", return_value={}), \
             patch("gateway.admin.cli.get_profile", return_value=object()), \
             patch("gateway.admin.cli.admin_factory", return_value=mock_admin):
            args = _args(["p", "create-channel", "eng"])
            code = await _run(args)

        self.assertEqual(code, 0)

    async def test_create_channel_privacy_mismatch_is_an_error(self):
        # Existing channel is PUBLIC, but --private was requested — the
        # requested state was never achieved, so this must NOT be a silent
        # idempotent no-op.
        mock_admin = AsyncMock()
        existing = AdminChannel(id="c1", name="secret", is_private=False)
        mock_admin.create_channel = AsyncMock(
            side_effect=ChannelAlreadyExistsError("secret", existing=existing)
        )
        stderr = io.StringIO()
        with patch("gateway.admin.cli.load_profiles", return_value={}), \
             patch("gateway.admin.cli.get_profile", return_value=object()), \
             patch("gateway.admin.cli.admin_factory", return_value=mock_admin), \
             contextlib.redirect_stderr(stderr):
            args = _args(["p", "create-channel", "secret", "--private"])
            code = await _run(args)

        self.assertEqual(code, 1)
        self.assertIn("already exists but is public, not private", stderr.getvalue())

    async def test_create_channel_privacy_match_is_still_a_noop(self):
        # Sanity check the fix didn't break the existing idempotent-no-op
        # path when the privacy actually does match.
        mock_admin = AsyncMock()
        existing = AdminChannel(id="c1", name="secret", is_private=True)
        mock_admin.create_channel = AsyncMock(
            side_effect=ChannelAlreadyExistsError("secret", existing=existing)
        )
        with patch("gateway.admin.cli.load_profiles", return_value={}), \
             patch("gateway.admin.cli.get_profile", return_value=object()), \
             patch("gateway.admin.cli.admin_factory", return_value=mock_admin):
            args = _args(["p", "create-channel", "secret", "--private"])
            code = await _run(args)

        self.assertEqual(code, 0)

    async def test_create_channel_archived_is_an_error_not_a_skip(self):
        # End-to-end proof of the property ChannelArchivedError's docstring
        # relies on: because it is NOT a ChannelAlreadyExistsError subclass, it
        # falls through to _run()'s broad handler and exits 1 rather than being
        # reported as an idempotent skip over an unusable channel.
        mock_admin = AsyncMock()
        existing = AdminChannel(id="c1", name="eng", is_private=False, archived=True)
        mock_admin.create_channel = AsyncMock(
            side_effect=ChannelArchivedError("eng", existing=existing)
        )
        stderr = io.StringIO()
        with patch("gateway.admin.cli.load_profiles", return_value={}), \
             patch("gateway.admin.cli.get_profile", return_value=object()), \
             patch("gateway.admin.cli.admin_factory", return_value=mock_admin), \
             contextlib.redirect_stderr(stderr):
            args = _args(["p", "create-channel", "eng"])
            code = await _run(args)

        self.assertEqual(code, 1)
        output = stderr.getvalue()
        self.assertIn("archived", output)
        self.assertNotIn("skipping", output)
        mock_admin.close.assert_awaited_once()

    async def test_not_found_error_returns_1_and_still_closes(self):
        mock_admin = AsyncMock()
        mock_admin.delete_user = AsyncMock(side_effect=UserNotFoundError("no such user"))
        with patch("gateway.admin.cli.load_profiles", return_value={}), \
             patch("gateway.admin.cli.get_profile", return_value=object()), \
             patch("gateway.admin.cli.admin_factory", return_value=mock_admin):
            args = _args(["p", "delete-user", "ghost"])
            code = await _run(args)

        self.assertEqual(code, 1)
        mock_admin.close.assert_awaited_once()

    async def test_create_channel_success_returns_0(self):
        mock_admin = AsyncMock()
        mock_admin.create_channel = AsyncMock(
            return_value=AdminChannel(id="c1", name="eng", is_private=False)
        )
        with patch("gateway.admin.cli.load_profiles", return_value={}), \
             patch("gateway.admin.cli.get_profile", return_value=object()), \
             patch("gateway.admin.cli.admin_factory", return_value=mock_admin):
            args = _args(["p", "create-channel", "eng"])
            code = await _run(args)

        self.assertEqual(code, 0)
        mock_admin.create_channel.assert_awaited_once_with("eng", is_private=False)

    async def test_add_to_channel_success_returns_0(self):
        mock_admin = AsyncMock()
        with patch("gateway.admin.cli.load_profiles", return_value={}), \
             patch("gateway.admin.cli.get_profile", return_value=object()), \
             patch("gateway.admin.cli.admin_factory", return_value=mock_admin):
            args = _args(["p", "add-to-channel", "alice", "eng"])
            code = await _run(args)

        self.assertEqual(code, 0)
        mock_admin.add_user_to_channel.assert_awaited_once_with("alice", "eng")

    async def test_delete_channel_success_returns_0(self):
        mock_admin = AsyncMock()
        with patch("gateway.admin.cli.load_profiles", return_value={}), \
             patch("gateway.admin.cli.get_profile", return_value=object()), \
             patch("gateway.admin.cli.admin_factory", return_value=mock_admin):
            args = _args(["p", "delete-channel", "eng"])
            code = await _run(args)

        self.assertEqual(code, 0)
        mock_admin.delete_channel.assert_awaited_once_with("eng")

    async def test_delete_user_success_returns_0(self):
        mock_admin = AsyncMock()
        with patch("gateway.admin.cli.load_profiles", return_value={}), \
             patch("gateway.admin.cli.get_profile", return_value=object()), \
             patch("gateway.admin.cli.admin_factory", return_value=mock_admin):
            args = _args(["p", "delete-user", "alice"])
            code = await _run(args)

        self.assertEqual(code, 0)
        mock_admin.delete_user.assert_awaited_once_with("alice")

    async def test_admin_factory_error_returns_1_without_connecting(self):
        with patch("gateway.admin.cli.load_profiles", return_value={}), \
             patch("gateway.admin.cli.get_profile", return_value=object()), \
             patch("gateway.admin.cli.admin_factory", side_effect=AdminConfigError("bad profile")):
            args = _args(["p", "delete-user", "ghost"])
            code = await _run(args)

        self.assertEqual(code, 1)


class TestCloseFailureDoesNotChangeTheOutcome(unittest.IsolatedAsyncioTestCase):
    """`finally: await admin.close()` sits outside _run()'s handlers, so a
    close() failure used to replace the real outcome and escape as a traceback.

    Reported as a secondary Warning instead, never as an "Error:" (which paired
    with exit 0 would break the contract's Error-implies-exit-1 pairing — the
    flaw that sank an earlier attempt at this fix)."""

    async def _run_with_close_error(self, *, op_fails: bool):
        mock_admin = AsyncMock()
        mock_admin.close = AsyncMock(side_effect=RuntimeError("transport died on shutdown"))
        if op_fails:
            mock_admin.delete_user = AsyncMock(side_effect=UserNotFoundError("no such user"))
        stderr = io.StringIO()
        with patch("gateway.admin.cli.load_profiles", return_value={}), \
             patch("gateway.admin.cli.get_profile", return_value=object()), \
             patch("gateway.admin.cli.admin_factory", return_value=mock_admin), \
             patch("gateway.admin.cli._configure_error_log"), \
             contextlib.redirect_stderr(stderr):
            args = _args(["p", "delete-user", "alice"])
            code = await _run(args)
        return code, stderr.getvalue()

    async def test_close_failure_does_not_turn_success_into_a_traceback(self):
        code, out = await self._run_with_close_error(op_fails=False)

        self.assertEqual(code, 0)  # the operation succeeded; keep that outcome
        self.assertIn("Warning:", out)
        self.assertIn("transport died on shutdown", out)
        # An "Error:" line alongside exit 0 would break the CLI contract.
        self.assertNotIn("Error:", out)

    async def test_close_failure_does_not_mask_the_real_error(self):
        code, out = await self._run_with_close_error(op_fails=True)

        self.assertEqual(code, 1)
        self.assertIn("no such user", out)      # original failure preserved
        self.assertIn("Warning:", out)          # cleanup problem still surfaced

    async def test_cancellation_during_close_still_propagates(self):
        # BaseException must NOT be downgraded to a warning — matching
        # PlatformAdmin.__aenter__'s contextlib.suppress(Exception).
        mock_admin = AsyncMock()
        mock_admin.close = AsyncMock(side_effect=asyncio.CancelledError())
        with patch("gateway.admin.cli.load_profiles", return_value={}), \
             patch("gateway.admin.cli.get_profile", return_value=object()), \
             patch("gateway.admin.cli.admin_factory", return_value=mock_admin), \
             patch("gateway.admin.cli._configure_error_log"), \
             contextlib.redirect_stderr(io.StringIO()):
            args = _args(["p", "delete-user", "alice"])
            with self.assertRaises(asyncio.CancelledError):
                await _run(args)


class TestConfigureErrorLog(unittest.TestCase):
    def setUp(self):
        self.error_logger = logging.getLogger("coop.admin.errors")
        self.umbrella_logger = logging.getLogger("coop")
        self._orig_error_handlers = list(self.error_logger.handlers)
        self._orig_error_propagate = self.error_logger.propagate
        self._orig_umbrella_handlers = list(self.umbrella_logger.handlers)
        self.error_logger.handlers = []
        self.umbrella_logger.handlers = []
        self.addCleanup(setattr, self.error_logger, "handlers", self._orig_error_handlers)
        self.addCleanup(setattr, self.error_logger, "propagate", self._orig_error_propagate)
        self.addCleanup(setattr, self.umbrella_logger, "handlers", self._orig_umbrella_handlers)

    def test_attaches_a_file_handler_to_error_logger(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "custom.log")
            _configure_error_log(path)

            self.assertEqual(len(self.error_logger.handlers), 1)
            self.assertIsInstance(self.error_logger.handlers[0], logging.FileHandler)
            self.assertEqual(self.error_logger.level, logging.ERROR)

    def test_disables_propagation_on_error_logger(self):
        # Otherwise log_error_response()'s explicit call would ALSO be
        # handled by the umbrella handler below (same file) — one call,
        # two lines written.
        with tempfile.TemporaryDirectory() as d:
            _configure_error_log(os.path.join(d, "custom.log"))

            self.assertFalse(self.error_logger.propagate)

    def test_attaches_a_warning_level_handler_to_umbrella_logger(self):
        # This is the actual fix: RocketChatREST/MattermostREST's own
        # logger.error() calls (on loggers named
        # "coop.connectors.<platform>.rest") would otherwise
        # find no handler anywhere in their hierarchy and fall through to
        # Python's stderr-printing "handler of last resort".
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "custom.log")
            _configure_error_log(path)

            self.assertEqual(len(self.umbrella_logger.handlers), 1)
            handler = self.umbrella_logger.handlers[0]
            self.assertIsInstance(handler, logging.FileHandler)
            self.assertEqual(handler.level, logging.WARNING)

    def test_idempotent_does_not_duplicate_either_handler_for_same_path(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "custom.log")
            _configure_error_log(path)
            _configure_error_log(path)

            self.assertEqual(len(self.error_logger.handlers), 1)
            self.assertEqual(len(self.umbrella_logger.handlers), 1)

    def test_error_logged_via_error_logger_is_written_exactly_once(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "custom.log")
            _configure_error_log(path)

            self.error_logger.error("boom")
            for handler in self.error_logger.handlers + self.umbrella_logger.handlers:
                handler.flush()

            with open(path) as f:
                lines = [line for line in f.read().splitlines() if "boom" in line]
            self.assertEqual(len(lines), 1)

    def test_rest_client_logger_error_reaches_the_file(self):
        # Simulates what MattermostREST/RocketChatREST's shared _request()
        # does on a non-2xx response — a logger under the
        # "coop.connectors.*" namespace, which has no handler
        # of its own and relies on propagation up to the umbrella logger.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "custom.log")
            _configure_error_log(path)

            rest_logger = logging.getLogger("coop.connectors.mattermost.rest")
            rest_logger.error("Mattermost API error 400 for POST users — body: {...}")
            for handler in self.umbrella_logger.handlers:
                handler.flush()

            with open(path) as f:
                content = f.read()
            self.assertIn("Mattermost API error 400", content)


class TestHttpStatusErrorHandling(unittest.IsolatedAsyncioTestCase):
    async def test_prints_friendly_message_and_logs_full_body(self):
        mock_admin = AsyncMock()
        mock_admin.create_user = AsyncMock(
            side_effect=_http_status_error(
                400,
                {
                    "id": "app.user.save.email_exists.app_error",
                    "message": "An account with that email already exists.",
                    "request_id": "req-123",
                    "status_code": 400,
                },
            )
        )
        mock_logger = MagicMock()
        stderr = io.StringIO()
        with patch("gateway.admin.cli.load_profiles", return_value={}), \
             patch("gateway.admin.cli.get_profile", return_value=object()), \
             patch("gateway.admin.cli.admin_factory", return_value=mock_admin), \
             patch("gateway.admin.cli._configure_error_log"), \
             patch("gateway.admin.cli._error_logger", mock_logger), \
             contextlib.redirect_stderr(stderr):
            args = _args(["p", "create-user", "alice", "a@x.com", "pw"])
            code = await _run(args)

        self.assertEqual(code, 1)
        mock_logger.error.assert_called_once()
        mock_admin.close.assert_awaited_once()
        output = stderr.getvalue()
        self.assertIn("An account with that email already exists.", output)
        self.assertNotIn("Client error", output)  # httpx's generic message must not leak through
        self.assertIn(args.log_file, output)


class TestUnwritableLogFile(unittest.IsolatedAsyncioTestCase):
    async def test_unwritable_log_file_returns_1_with_clean_message_not_a_traceback(self):
        # logging.FileHandler opens the file immediately — simulate a
        # read-only directory / bad path by having _configure_error_log
        # raise the same way FileHandler would.
        with patch(
            "gateway.admin.cli._configure_error_log",
            side_effect=OSError("Permission denied"),
        ):
            args = _args(["--log-file", "/no/such/dir/x.log", "p", "delete-user", "alice"])
            code = await _run(args)

        self.assertEqual(code, 1)

    async def test_unwritable_log_file_does_not_reach_admin_factory(self):
        # The failure happens before any profile/admin setup — nothing
        # downstream should be touched.
        with patch(
            "gateway.admin.cli._configure_error_log", side_effect=OSError("Permission denied")
        ), patch("gateway.admin.cli.admin_factory") as mock_factory:
            args = _args(["--log-file", "/no/such/dir/x.log", "p", "delete-user", "alice"])
            await _run(args)

        mock_factory.assert_not_called()


class TestReaderlessFifoLogFile(unittest.IsolatedAsyncioTestCase):
    async def test_fifo_log_file_with_no_reader_is_a_clean_error_not_a_hang(self):
        # logging.FileHandler opens eagerly, and open() on a reader-less FIFO
        # blocks forever — no message, no exit code, process left hung. The
        # non-blocking probe must turn that into a clean CLI error instead.
        # (unittest's own timeout isn't relied on here: if the probe were
        # missing, this test would hang, which is itself the signal.)
        with tempfile.TemporaryDirectory() as d:
            fifo = os.path.join(d, "log.fifo")
            os.mkfifo(fifo)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                args = _args(["--log-file", fifo, "p", "delete-user", "alice"])
                code = await _run(args)

        self.assertEqual(code, 1)
        self.assertIn("could not open log file", stderr.getvalue())

    def test_probe_fd_is_held_open_until_both_handlers_are_constructed(self):
        """The deterministic regression detector for the one-shot-reader hang.

        Background: the ENXIO probe originally closed its fd immediately. On a
        FIFO that drops the writer count to zero, so a one-shot reader like
        `cat fifo > log` sees EOF and exits — and the next
        logging.FileHandler's blocking open() then waits forever for a reader
        that is gone, reintroducing the exact hang the probe exists to
        prevent. Once wedged it is silent and SIGINT-proof (the blocked open()
        restarts under SA_RESTART), so it must be SIGTERM'd by hand.

        Asserted as an ORDERING invariant rather than by reproducing the hang:
        the real race window is microseconds wide, so an end-to-end attempt
        only fires ~30-68% of the time and would be exactly the kind of flaky
        test this suite has already had to fix once. The ordering is what the
        fix actually guarantees, and it is deterministic.
        """
        err_logger = logging.getLogger("coop.admin.errors")
        umb_logger = logging.getLogger("coop")
        real_close = os.close
        at_close = []

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "plain.log")
            target = os.path.abspath(path)
            # Pre-create so the probe actually opens something (a missing file
            # takes the FileNotFoundError branch and never opens a probe).
            open(path, "a").close()

            def tracking_close(fd):
                # Snapshot how many handlers for THIS path exist at the moment
                # the probe fd is released.
                at_close.append(
                    sum(
                        1
                        for lg in (err_logger, umb_logger)
                        for h in lg.handlers
                        if isinstance(h, logging.FileHandler) and h.baseFilename == target
                    )
                )
                return real_close(fd)

            try:
                with patch("gateway.admin.cli.os.close", side_effect=tracking_close):
                    _configure_error_log(path)

                self.assertEqual(
                    len(at_close), 1, "expected exactly one probe fd close"
                )
                # With the fix, both handlers are already attached (and so have
                # already opened the path) before the probe is released. With
                # the bug this is 0.
                self.assertEqual(
                    at_close[0], 2,
                    "probe fd was closed before the handlers opened the path — "
                    "on a FIFO this drops the writer count to zero and a "
                    "one-shot reader will exit, hanging the next open()",
                )
            finally:
                _detach_admin_log_handlers(target)

    def test_one_shot_reader_fifo_smoke(self):
        """End-to-end smoke check that a FIFO with a one-shot reader completes.

        NOT a reliable regression detector on its own — the race is
        microseconds wide, so a broken build still passes this most of the
        time (measured: it does). The deterministic guarantee lives in
        test_probe_fd_is_held_open_until_both_handlers_are_constructed above;
        this one exists to confirm the whole path really works against a real
        FIFO and a real reader, in a subprocess so a wedge can be killed
        rather than blocking the test run.
        """
        with tempfile.TemporaryDirectory() as d:
            fifo = os.path.join(d, "log.fifo")
            out = os.path.join(d, "out")
            os.mkfifo(fifo)

            driver = (
                "from gateway.admin.cli import _configure_error_log;"
                f"_configure_error_log({fifo!r});"
                "print('OK')"
            )
            reader = subprocess.Popen(
                f"cat {shlex.quote(fifo)} > {shlex.quote(out)}", shell=True
            )
            try:
                time.sleep(0.3)  # let the reader block in its own open()
                proc = subprocess.Popen(
                    [sys.executable, "-c", driver],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                try:
                    stdout, stderr = proc.communicate(timeout=20)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()
                    self.fail("_configure_error_log() hung on a FIFO with a one-shot reader")
                self.assertEqual(proc.returncode, 0, f"stderr={stderr!r}")
                self.assertIn("OK", stdout)
            finally:
                reader.terminate()
                reader.wait(timeout=5)

    async def test_regular_log_file_path_still_works(self):
        # Guard the flip side: the probe must not reject ordinary paths.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "fresh.log")
            with patch("gateway.admin.cli.load_profiles", side_effect=AdminConfigError("no cfg")), \
                 contextlib.redirect_stderr(io.StringIO()):
                args = _args(["--log-file", path, "p", "delete-user", "alice"])
                code = await _run(args)

            # Asserted INSIDE the tempdir context — checking after it exits
            # would test the cleanup, not the log file.
            self.assertEqual(code, 1)  # failed on config, not on the log file
            self.assertTrue(os.path.exists(path))

    async def test_dev_null_log_file_still_works(self):
        # /dev/null is a character device, not a FIFO — must pass the probe.
        with patch("gateway.admin.cli.load_profiles", side_effect=AdminConfigError("no cfg")), \
             contextlib.redirect_stderr(io.StringIO()):
            args = _args(["--log-file", "/dev/null", "p", "delete-user", "alice"])
            code = await _run(args)

        self.assertEqual(code, 1)


class TestMain(unittest.TestCase):
    def test_main_exits_with_run_result_code(self):
        with patch("gateway.admin.cli._run", new=AsyncMock(return_value=7)), \
             patch("sys.argv", ["coop-provision", "p", "delete-user", "alice"]), \
             self.assertRaises(SystemExit) as ctx:
            main()
        self.assertEqual(ctx.exception.code, 7)

    def test_keyboard_interrupt_prints_clean_error_and_re_signals(self):
        # Ctrl-C must not dump ~100 lines of asyncio internals. Re-signalling
        # (rather than returning a chosen exit code) is what lets a shell
        # seed-loop actually abort, so assert the re-signal mechanics, not an
        # exit code.
        killed = []
        stderr = io.StringIO()
        with patch("gateway.admin.cli._run", new=AsyncMock(side_effect=KeyboardInterrupt())), \
             patch("sys.argv", ["coop-provision", "p", "delete-user", "alice"]), \
             patch("gateway.admin.cli.signal.signal") as mock_signal, \
             patch("gateway.admin.cli.os.kill", side_effect=lambda *a: killed.append(a)), \
             contextlib.redirect_stderr(stderr):
            main()

        self.assertIn("Error: interrupted", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        # Default SIGINT disposition restored before re-raising, so the
        # re-signal actually kills instead of re-entering this handler.
        # assert_any_call, not assert_called_once_with: asyncio.run()
        # installs its own SIGINT handler first, so this mock sees two calls.
        mock_signal.assert_any_call(signal.SIGINT, signal.SIG_DFL)
        self.assertEqual(len(killed), 1)
        self.assertEqual(killed[0][1], signal.SIGINT)


if __name__ == "__main__":
    unittest.main()
