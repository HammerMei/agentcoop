"""Unit tests for gateway.daemon (pure-logic functions).

Tests cover: is_running, _wait_for_startup_signal, _cleanup, stop_daemon,
_harden_config_permissions. start_daemon() itself uses os.fork() which is
not suitable for unit tests and is intentionally excluded —
_harden_config_permissions() is extracted as a standalone function
specifically so the logic start_daemon() calls into is still testable
without forking. sanitize_pipe_message() (imported here from
gateway.service, code-review finding: consolidated from a duplicate local
copy) is tested in tests/unit/test_gateway_service.py, alongside its other
caller _write_startup_signal().
"""

from __future__ import annotations

import io
import os
import signal
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import gateway.daemon as daemon_mod
import gateway.runtime_lock as runtime_lock_mod

# ── Helpers ───────────────────────────────────────────────────────────────────

def _patch_paths(tmpdir: str):
    """Context manager that redirects RUNTIME_DIR, PID_FILE, and LOG_FILE to tmpdir.

    daemon.PID_FILE is now an alias for runtime_lock.LOCK_FILE, so we patch
    runtime_lock.LOCK_FILE to redirect the actual liveness checks performed by
    runtime_lock.locked_pid() and runtime_lock.release().
    """
    td = Path(tmpdir)
    return (
        patch.object(daemon_mod, "RUNTIME_DIR", td),
        patch.object(runtime_lock_mod, "LOCK_FILE", td / "gateway.pid"),
        patch.object(daemon_mod, "LOG_FILE", td / "gateway.log"),
    )


# ── _harden_config_permissions ─────────────────────────────────────────────────

class TestHardenConfigPermissions(unittest.TestCase):
    def test_chmods_config_to_0600_regardless_of_starting_permissions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text("connectors: []\n")
            config_path.chmod(0o644)

            daemon_mod._harden_config_permissions(str(config_path))

            self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)


# ── is_running ────────────────────────────────────────────────────────────────

class TestIsRunning(unittest.TestCase):
    def test_no_pid_file_returns_false(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            p1, p2, p3 = _patch_paths(tmpdir)
            with p1, p2, p3:
                running, pid = daemon_mod.is_running()
            self.assertFalse(running)
            self.assertIsNone(pid)

    def test_valid_pid_file_current_process(self):
        """Use current PID — os.kill(pid, 0) should succeed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_file = Path(tmpdir) / "gateway.pid"
            pid_file.write_text(str(os.getpid()))

            p1, p2, p3 = _patch_paths(tmpdir)
            with p1, p2, p3:
                running, pid = daemon_mod.is_running()
            self.assertTrue(running)
            self.assertEqual(pid, os.getpid())

    def test_stale_pid_file_removed(self):
        """PID that no longer exists → stale file is removed, returns (False, None)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_file = Path(tmpdir) / "gateway.pid"
            # Use a PID that is extremely unlikely to exist
            pid_file.write_text("999999999")

            p1, p2, p3 = _patch_paths(tmpdir)
            with p1, p2, p3:
                running, pid = daemon_mod.is_running()
            self.assertFalse(running)
            self.assertIsNone(pid)
            self.assertFalse(pid_file.exists())

    def test_corrupt_pid_file_returns_false(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_file = Path(tmpdir) / "gateway.pid"
            pid_file.write_text("not-a-number")

            p1, p2, p3 = _patch_paths(tmpdir)
            with p1, p2, p3:
                running, pid = daemon_mod.is_running()
            self.assertFalse(running)


# ── _cleanup ──────────────────────────────────────────────────────────────────

class TestCleanup(unittest.TestCase):
    def test_cleanup_removes_pid_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_file = Path(tmpdir) / "gateway.pid"
            pid_file.write_text("1234")

            p1, p2, p3 = _patch_paths(tmpdir)
            with p1, p2, p3:
                daemon_mod._cleanup()
            self.assertFalse(pid_file.exists())

    def test_cleanup_no_pid_file_is_noop(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            p1, p2, p3 = _patch_paths(tmpdir)
            with p1, p2, p3:
                # Should not raise
                daemon_mod._cleanup()


# ── _wait_for_startup_signal ──────────────────────────────────────────────────

class TestWaitForStartupSignal(unittest.TestCase):
    def _make_pipe_with_data(self, data: bytes) -> int:
        """Write data to a pipe and return the read fd."""
        read_fd, write_fd = os.pipe()
        os.write(write_fd, data)
        os.close(write_fd)
        return read_fd

    def test_ok_line_exits_0(self):
        read_fd = self._make_pipe_with_data(b"ok\n")
        with self.assertRaises(SystemExit) as ctx:
            daemon_mod._wait_for_startup_signal(read_fd)
        self.assertEqual(ctx.exception.code, 0)

    def test_missing_ok_exits_1(self):
        read_fd = self._make_pipe_with_data(b"some other output\n")
        with self.assertRaises(SystemExit) as ctx:
            daemon_mod._wait_for_startup_signal(read_fd)
        self.assertEqual(ctx.exception.code, 1)

    def test_ok_with_errors_exits_0_with_warning(self):
        data = b"error:Config A failed\nerror:Config B failed\nok\n"
        read_fd = self._make_pipe_with_data(data)
        with self.assertRaises(SystemExit) as ctx:
            daemon_mod._wait_for_startup_signal(read_fd)
        self.assertEqual(ctx.exception.code, 0)

    def test_empty_pipe_exits_1(self):
        read_fd = self._make_pipe_with_data(b"")
        with self.assertRaises(SystemExit) as ctx:
            daemon_mod._wait_for_startup_signal(read_fd)
        self.assertEqual(ctx.exception.code, 1)

    def test_info_line_is_printed_to_the_console_on_success(self):
        """A startup notice reports via an 'info:' line — must reach stdout,
        not just the log file, per the user-requested "not a completely silent
        operation" ask."""
        data = b"info:Migrated 1 secret reference(s) into config.yaml.\nok\n"
        read_fd = self._make_pipe_with_data(data)
        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            with self.assertRaises(SystemExit) as ctx:
                daemon_mod._wait_for_startup_signal(read_fd)
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("Migrated 1 secret reference(s)", mock_stdout.getvalue())

    def test_info_line_is_printed_even_when_degraded(self):
        data = (
            b"info:Migrated 1 secret reference(s) into config.yaml.\n"
            b"error:Some sidecar failed\nok\n"
        )
        read_fd = self._make_pipe_with_data(data)
        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            with self.assertRaises(SystemExit):
                daemon_mod._wait_for_startup_signal(read_fd)
        self.assertIn("Migrated 1 secret reference(s)", mock_stdout.getvalue())


# ── stop_daemon ───────────────────────────────────────────────────────────────

class TestStopDaemon(unittest.TestCase):
    def test_stop_not_running_prints_message(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            p1, p2, p3 = _patch_paths(tmpdir)
            with p1, p2, p3:
                with patch("builtins.print") as mock_print:
                    daemon_mod.stop_daemon()
                mock_print.assert_called_once()
                args = mock_print.call_args[0][0]
                self.assertIn("not running", args)

    def test_stop_sends_sigterm_to_running_pid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_pid = 99999

            # Patch is_running to pretend a process is running
            p1, p2, p3 = _patch_paths(tmpdir)
            with p1, p2, p3:
                with patch.object(daemon_mod, "is_running", return_value=(True, fake_pid)):
                    with patch("os.kill") as mock_kill:
                        # Make kill(pid, 0) raise ProcessLookupError immediately
                        # so the wait loop exits
                        def kill_side_effect(p, sig):
                            if sig == 0:
                                raise ProcessLookupError
                        mock_kill.side_effect = kill_side_effect

                        with patch("builtins.print"):
                            daemon_mod.stop_daemon()

                        # First kill call should be SIGTERM
                        first_call = mock_kill.call_args_list[0]
                        self.assertEqual(first_call[0], (fake_pid, signal.SIGTERM))

    def test_stop_calls_cleanup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            p1, p2, p3 = _patch_paths(tmpdir)
            with p1, p2, p3:
                with patch.object(daemon_mod, "is_running", return_value=(False, None)):
                    with patch.object(daemon_mod, "_cleanup") as mock_cleanup:
                        with patch("builtins.print"):
                            daemon_mod.stop_daemon()
                        # _cleanup is NOT called when already stopped
                        # (it's only called after successful stop)
                        # This verifies the not-running early-return path
                        mock_cleanup.assert_not_called()


# ── _setup_logging ────────────────────────────────────────────────────────────


class TestSetupLogging(unittest.TestCase):
    def test_setup_logging_creates_runtime_dir(self):
        """_setup_logging must create RUNTIME_DIR if it does not exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_dir = Path(tmpdir) / "runtime"
            log_file = runtime_dir / "gateway.log"
            self.assertFalse(runtime_dir.exists())

            p1, p2, p3 = _patch_paths(tmpdir)
            with (
                p1,
                patch.object(daemon_mod, "RUNTIME_DIR", runtime_dir),
                patch.object(daemon_mod, "LOG_FILE", log_file),
                patch("logging.basicConfig"),  # avoid polluting root logger
            ):
                daemon_mod._setup_logging()

            self.assertTrue(runtime_dir.exists())

    def test_setup_logging_adds_file_handler(self):
        """_setup_logging must call basicConfig with a FileHandler pointing at LOG_FILE."""
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_dir = Path(tmpdir) / "runtime"
            log_file = runtime_dir / "gateway.log"

            with (
                patch.object(daemon_mod, "RUNTIME_DIR", runtime_dir),
                patch.object(daemon_mod, "LOG_FILE", log_file),
                patch("logging.basicConfig") as mock_basic,
                patch("logging.FileHandler") as mock_fh,
            ):
                daemon_mod._setup_logging()

            mock_fh.assert_called_once_with(str(log_file), encoding="utf-8")
            mock_basic.assert_called_once()


# ── _wait_for_startup_signal — OSError paths ──────────────────────────────────


class TestWaitForStartupSignalOSError(unittest.TestCase):
    """Test the OSError exception paths inside _wait_for_startup_signal."""

    def test_oserror_during_read_is_swallowed(self):
        """An OSError while reading the pipe must be silently swallowed.

        This simulates the parent-side read_fd being closed before the daemon
        writes anything (e.g. if the daemon crashes immediately after forking).
        The function should still reach the no-ok path and call sys.exit(1).
        """
        read_fd, write_fd = os.pipe()
        os.close(write_fd)  # close write end — ensures read returns EOF

        # Patch os.read to raise OSError on the first call to simulate a
        # broken pipe, then return b"" to signal EOF.
        call_count = 0

        def _read_side_effect(fd, n):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError("simulated broken pipe")
            return b""

        with patch("os.read", side_effect=_read_side_effect):
            with self.assertRaises(SystemExit) as ctx:
                daemon_mod._wait_for_startup_signal(read_fd)

        # No "ok" was read, so should exit with code 1.
        self.assertEqual(ctx.exception.code, 1)

    def test_oserror_during_close_is_swallowed(self):
        """An OSError on os.close(read_fd) in the finally block must be swallowed."""
        read_fd = self._make_pipe_with_data(b"ok\n")

        original_close = os.close
        close_calls: list[int] = []

        def _close_side_effect(fd):
            close_calls.append(fd)
            if fd == read_fd:
                raise OSError("simulated bad fd on close")
            original_close(fd)

        with patch("os.close", side_effect=_close_side_effect):
            with self.assertRaises(SystemExit) as ctx:
                daemon_mod._wait_for_startup_signal(read_fd)

        # Despite the OSError in finally, should exit 0 because "ok" was read.
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn(read_fd, close_calls)

    def _make_pipe_with_data(self, data: bytes) -> int:
        read_fd, write_fd = os.pipe()
        os.write(write_fd, data)
        os.close(write_fd)
        return read_fd


# ── stop_daemon — PID mismatch ────────────────────────────────────────────────


class TestStopDaemonPidMismatch(unittest.TestCase):
    def test_lock_not_released_when_pid_changed(self):
        """If the PID file was overwritten by a new daemon before stop completes,
        _lock_release() must NOT be called (to avoid clobbering the new daemon).
        """
        original_pid = 11111
        new_pid = 22222  # a different process claimed the lock after SIGTERM

        with tempfile.TemporaryDirectory() as tmpdir:
            p1, p2, p3 = _patch_paths(tmpdir)
            with p1, p2, p3:
                with patch.object(daemon_mod, "is_running", return_value=(True, original_pid)):
                    with patch("os.kill") as mock_kill:
                        def kill_side_effect(p, sig):
                            if sig == 0:
                                raise ProcessLookupError

                        mock_kill.side_effect = kill_side_effect

                        # locked_pid now returns a *different* PID (new daemon started)
                        with patch.object(
                            runtime_lock_mod, "locked_pid", return_value=new_pid
                        ):
                            with patch.object(daemon_mod, "_lock_release") as mock_release:
                                with patch("builtins.print"):
                                    daemon_mod.stop_daemon()

                        # Because current_pid (new_pid) != original_pid,
                        # the lock must NOT be released.
                        mock_release.assert_not_called()


if __name__ == "__main__":
    unittest.main()
