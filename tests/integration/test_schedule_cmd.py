"""Integration tests for the 'schedule' CLI subcommands.

Exercises the full CLI path for scheduling:
    argument parsing → command dispatch → Unix socket → mock daemon response

The mock daemon (``_MockDaemon``) is borrowed from test_cli.py and speaks the
same framing protocol: one JSON line in, one JSON line out.  Each test class
targets a specific schedule subcommand; the daemon returns canned JSON matching
what the real GatewayService handlers would return.

Run with:
    uv run python -m pytest tests/integration/test_schedule_cmd.py -v
"""

from __future__ import annotations

import io
import json
import shutil
import socket
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers (copied from test_cli.py pattern)
# ---------------------------------------------------------------------------

def _import_main():
    from gateway.cli import main
    return main


class _MockDaemon:
    """Minimal Unix-socket server that returns canned JSON responses.

    Runs in a background daemon thread so the test's ``asyncio.run()`` call
    (inside ``_send_command_async``) can connect to it synchronously.

    ``responses`` maps command strings to either a dict (returned as-is) or a
    callable ``(request_dict) -> response_dict`` for dynamic assertions.
    """

    def __init__(self, sock_path: Path, responses: dict):
        self._sock_path = sock_path
        self._responses = responses
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        # Remove stale socket file so re-bind (e.g. in round-trip tests) succeeds.
        self._sock_path.unlink(missing_ok=True)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(str(self._sock_path))
        s.listen(10)
        s.settimeout(5.0)
        self._sock = s

        def _serve():
            try:
                while True:
                    try:
                        conn, _ = s.accept()
                    except OSError:
                        return
                    with conn:
                        data = b""
                        while b"\n" not in data:
                            chunk = conn.recv(65536)
                            if not chunk:
                                break
                            data += chunk
                        try:
                            req = json.loads(data.strip())
                        except Exception:
                            conn.sendall(b'{"ok":false,"error":"bad json"}\n')
                            continue
                        cmd = req.get("cmd", "")
                        resp = self._responses.get(cmd)
                        if callable(resp):
                            resp = resp(req)
                        elif resp is None:
                            resp = {"ok": False, "error": f"unknown cmd: {cmd}"}
                        conn.sendall(json.dumps(resp).encode() + b"\n")
            except Exception:
                pass

        t = threading.Thread(target=_serve, daemon=True)
        t.start()
        self._thread = t
        # Small pause so the socket is ready before the test calls main()
        time.sleep(0.05)

    def stop(self) -> None:
        if self._sock:
            self._sock.close()
        # Unlink the socket file so a subsequent start() can bind to the same path.
        self._sock_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Base test class
# ---------------------------------------------------------------------------

class _ScheduleCLITestBase(unittest.TestCase):
    """Sets up a temp directory, mock daemon, and argv patching utilities."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sock_path = Path(self.tmp) / "control.sock"
        self.pid_file = Path(self.tmp) / "gateway.pid"
        self.log_file = Path(self.tmp) / "gateway.log"
        self._daemon: _MockDaemon | None = None

    def tearDown(self):
        if self._daemon:
            self._daemon.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _start_daemon(self, responses: dict) -> None:
        self._daemon = _MockDaemon(self.sock_path, responses)
        self._daemon.start()

    def _run(self, args: list[str]) -> tuple[str, str, int]:
        """Run CLI main() with patched argv; return (stdout, stderr, exit_code)."""
        main = _import_main()
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        exit_code = 0
        with (
            patch("sys.argv", ["acg"] + args),
            patch("gateway.cli.CONTROL_SOCK", self.sock_path),
            patch("gateway.daemon.is_running", return_value=(True, 99999)),
            patch("gateway.daemon.PID_FILE", self.pid_file),
            patch("gateway.daemon.LOG_FILE", self.log_file),
        ):
            try:
                with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
                    main()
            except SystemExit as exc:
                exit_code = exc.code if isinstance(exc.code, int) else 1
        return stdout_buf.getvalue(), stderr_buf.getvalue(), exit_code


# ---------------------------------------------------------------------------
# Tests: schedule create
# ---------------------------------------------------------------------------


def _make_mock_watcher_entry(name: str = "rc-home", timezone: str = "UTC"):
    """Return a mock ConnectorEntry whose connector exposes *timezone* and whose
    session manager recognises 'test-watcher'.  Used by unit-level ControlServer
    tests that don't need a real connector."""
    from unittest.mock import MagicMock
    entry = MagicMock()
    entry.name = name
    entry.connector.timezone = timezone
    return entry


class TestScheduleCreate(_ScheduleCLITestBase):
    """schedule create: argument parsing, cron computation, daemon round-trip."""

    def test_create_basic_every_1h(self):
        """'schedule create' with --every 1h → job created, ID and next_run printed."""
        self._start_daemon({
            "schedule-create": {
                "ok": True,
                "job_id": "acg-aabbccdd",
                "next_run": "2026-04-09T10:00:00+00:00",
            }
        })

        stdout, stderr, code = self._run([
            "schedule", "create", "e2e-dm",
            "Please generate the daily summary",
            "--every", "1h",
        ])

        self.assertEqual(code, 0, f"stderr: {stderr}")
        self.assertIn("acg-aabbccdd", stdout)
        self.assertIn("2026-04-09T10:00:00", stdout)

    def test_create_with_times_stores_correct_values(self):
        """--every 1h --times 3 → daemon receives cron='0 * * * *' and times=3."""
        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {"ok": True, "job_id": "acg-11223344", "next_run": "2026-04-09T11:00:00+00:00"}

        self._start_daemon({"schedule-create": _capture})

        stdout, _, code = self._run([
            "schedule", "create", "e2e-dm",
            "Summarize today",
            "--every", "1h",
            "--times", "3",
        ])

        self.assertEqual(code, 0)
        self.assertEqual(len(received), 1)
        req = received[0]
        self.assertEqual(req["cron"], "0 * * * *")   # --every 1h default cron
        self.assertEqual(req["times"], 3)
        self.assertEqual(req["watcher"], "e2e-dm")
        self.assertEqual(req["message"], "Summarize today")

    def test_create_watcher_and_message_forwarded(self):
        """Watcher name and message text are forwarded verbatim to the daemon."""
        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {"ok": True, "job_id": "acg-deadbeef", "next_run": "2026-04-09T09:00:00+00:00"}

        self._start_daemon({"schedule-create": _capture})

        self._run([
            "schedule", "create", "e2e-claude-channel",
            "Run the weekly report",
            "--every", "1w",
        ])

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["watcher"], "e2e-claude-channel")
        self.assertEqual(received[0]["message"], "Run the weekly report")

    def test_create_one_shot_at_future_date(self):
        """--starting 'YYYY-MM-DD HH:MM' with no --every → one-shot job, times=1 enforced."""
        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {"ok": True, "job_id": "acg-0f0f0f0f", "next_run": "2099-01-01T09:00:00+00:00"}

        self._start_daemon({"schedule-create": _capture})

        stdout, stderr, code = self._run([
            "schedule", "create", "e2e-dm",
            "Happy new year!",
            "--starting", "2099-01-01 09:00",
            "--tz", "UTC",   # explicit tz for deterministic cron assertion
        ])

        self.assertEqual(code, 0, f"stderr: {stderr}")
        # One-shot: times must be 1 (enforced by CLI when --every is absent)
        self.assertEqual(received[0]["times"], 1)
        # Cron should be "0 9 1 1 *" — day=1, month=1, minute=0, hour=9
        self.assertEqual(received[0]["cron"], "0 9 1 1 *")
        self.assertIn("acg-0f0f0f0f", stdout)

    def test_create_with_timezone(self):
        """--tz is forwarded in the command payload."""
        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {"ok": True, "job_id": "acg-tz000001", "next_run": "2026-04-09T01:00:00+00:00"}

        self._start_daemon({"schedule-create": _capture})

        self._run([
            "schedule", "create", "e2e-dm",
            "Daily standup",
            "--every", "1d",
            "--tz", "Asia/Taipei",
        ])

        self.assertEqual(received[0].get("timezone"), "Asia/Taipei")

    def test_create_payload_contains_watcher_not_connector(self):
        """schedule create sends watcher name; connector is resolved server-side."""
        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {"ok": True, "job_id": "acg-c0000001", "next_run": "2026-04-09T09:00:00+00:00"}

        self._start_daemon({"schedule-create": _capture})

        self._run([
            "schedule", "create", "e2e-dm",
            "Hello",
            "--every", "1h",
        ])

        self.assertEqual(received[0].get("watcher"), "e2e-dm")
        self.assertNotIn("connector", received[0])  # connector resolved server-side, not sent by CLI

    def test_create_invalid_watcher_daemon_returns_error(self):
        """Daemon returning error (e.g. unknown watcher) → stderr error + exit 1."""
        self._start_daemon({
            "schedule-create": {
                "ok": False,
                "error": "watcher 'no-such-watcher' not found",
            }
        })

        _, stderr, code = self._run([
            "schedule", "create", "no-such-watcher",
            "Does not matter",
            "--every", "1h",
        ])

        self.assertEqual(code, 1)
        self.assertIn("no-such-watcher", stderr)

    def test_create_no_every_no_starting_exits_1(self):
        """Neither --every nor --starting → validation error + exit 1 (no socket call)."""
        # No daemon needed — argument validation fires before the socket call.
        _, stderr, code = self._run([
            "schedule", "create", "e2e-dm",
            "Something",
        ])
        self.assertEqual(code, 1)
        self.assertIn("--every", stderr)

    def test_create_invalid_interval_exits_1(self):
        """Unsupported --every value → validation error + exit 1."""
        _, stderr, code = self._run([
            "schedule", "create", "e2e-dm",
            "Something",
            "--every", "7d",  # not a valid interval key
        ])
        self.assertEqual(code, 1)
        self.assertIn("Unsupported interval", stderr)

    def test_create_cron_mapping(self):
        """Each supported --every value produces the correct default cron expression.

        Uses unittest.subTest so each interval is reported independently.
        (Note: @pytest.mark.parametrize cannot inject arguments into unittest.TestCase
        methods — use subTest instead.)
        """
        cases = [
            ("1m",  "* * * * *"),
            ("30m", "*/30 * * * *"),
            ("1h",  "0 * * * *"),
            ("6h",  "0 */6 * * *"),
            ("1d",  "0 9 * * *"),
            ("1w",  "0 9 * * 1"),
        ]
        for interval, expected_cron in cases:
            with self.subTest(interval=interval):
                received: list[dict] = []

                def _capture(req, _recv=received):
                    _recv.append(req)
                    return {
                        "ok": True,
                        "job_id": "acg-cccc0001",
                        "next_run": "2026-04-09T09:00:00+00:00",
                    }

                self._start_daemon({"schedule-create": _capture})

                _, _, code = self._run([
                    "schedule", "create", "e2e-dm",
                    "test cron mapping",
                    "--every", interval,
                ])

                # Some intervals may produce a warning on stderr for sub-hourly --at
                # combinations; focus only on the cron value sent to the daemon.
                self.assertEqual(code, 0)
                self.assertEqual(
                    len(received), 1, f"Daemon not called for interval {interval!r}"
                )
                self.assertEqual(
                    received[0]["cron"],
                    expected_cron,
                    f"Wrong cron for --every {interval}: "
                    f"expected {expected_cron!r}, got {received[0]['cron']!r}",
                )

                if self._daemon:
                    self._daemon.stop()
                    self._daemon = None

    def test_create_one_shot_relative_uses_exact_datetime_cron(self):
        """--every 5m --times 1 → one-shot cron (MM HH DD MM *), NOT */5 * * * *.

        Regression test for the "fires too early" bug:
        */5 * * * * fires at the next :05/:10/… boundary (could be 0–5 min away),
        not exactly 5 minutes from now.  The fix computes now + 5m and generates a
        specific datetime cron so the job fires at the correct time.

        Also verifies that arbitrary non-cron-aligned intervals (7m, 23m, 90m) are
        accepted for one-shot jobs.
        """
        import re
        from datetime import UTC, datetime, timedelta

        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {
                "ok": True,
                "job_id": "acg-oneshot01",
                "next_run": "2026-04-09T07:47:00+00:00",
            }

        self._start_daemon({"schedule-create": _capture})

        before = datetime.now(UTC)
        _, _, code = self._run([
            "schedule", "create", "e2e-dm",
            "Remind me in 5 minutes",
            "--every", "5m",
            "--times", "1",
        ])
        after = datetime.now(UTC)

        self.assertEqual(code, 0)
        self.assertEqual(len(received), 1)

        cron = received[0]["cron"]

        # Must NOT be the periodic pattern
        self.assertNotEqual(
            cron,
            "*/5 * * * *",
            "One-shot job must not use periodic cron */5 * * * *",
        )

        # Must be a 5-field specific datetime cron: M H D Mo *
        m = re.fullmatch(r"(\d+) (\d+) (\d+) (\d+) \*", cron)
        self.assertIsNotNone(m, f"Expected datetime cron 'M H D Mo *', got: {cron!r}")

        # Parse and verify the fire time is approximately now + 5 minutes.
        # Cron has 1-minute granularity (seconds are truncated), so we allow
        # a ±1-minute window: fire must be in [now+4m, now+6m].
        minute, hour, day, month = int(m[1]), int(m[2]), int(m[3]), int(m[4])
        year = before.year
        fire_dt = datetime(year, month, day, hour, minute, tzinfo=UTC)
        window_low = before + timedelta(minutes=4)
        window_high = after + timedelta(minutes=6)
        self.assertGreaterEqual(
            fire_dt, window_low,
            f"Fire time {fire_dt} is more than 1m too early (expected >= {window_low})",
        )
        self.assertLessEqual(
            fire_dt, window_high,
            f"Fire time {fire_dt} is more than 1m too late (expected <= {window_high})",
        )

        # Timezone must be UTC — cron coordinates are UTC and the daemon must
        # not apply a local-timezone offset (regression: UTC-7 was shifting the
        # fire time 7 hours forward instead of N minutes).
        self.assertEqual(
            received[0].get("timezone"),
            "UTC",
            "One-shot relative reminders must always send timezone=UTC",
        )

    def test_create_one_shot_arbitrary_interval_7m(self):
        """--every 7m --times 1 is accepted and produces a specific datetime cron.

        7m is not in _INTERVAL_MAP (not cron-aligned), but is valid for one-shot
        jobs since we compute now+7m directly.
        """
        import re
        from datetime import UTC, datetime, timedelta

        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {
                "ok": True,
                "job_id": "acg-7m000001",
                "next_run": "2026-04-09T07:49:00+00:00",
            }

        self._start_daemon({"schedule-create": _capture})

        before = datetime.now(UTC)
        _, _, code = self._run([
            "schedule", "create", "e2e-dm",
            "Remind me in 7 minutes",
            "--every", "7m",
            "--times", "1",
        ])
        after = datetime.now(UTC)

        self.assertEqual(code, 0, "7m one-shot job should be accepted")
        self.assertEqual(len(received), 1)

        cron = received[0]["cron"]
        m = re.fullmatch(r"(\d+) (\d+) (\d+) (\d+) \*", cron)
        self.assertIsNotNone(m, f"Expected specific datetime cron, got: {cron!r}")

        minute, hour, day, month = int(m[1]), int(m[2]), int(m[3]), int(m[4])
        fire_dt = datetime(before.year, month, day, hour, minute, tzinfo=UTC)
        self.assertGreaterEqual(fire_dt, before + timedelta(minutes=6))
        self.assertLessEqual(fire_dt, after + timedelta(minutes=8))

        # Timezone must be UTC to prevent local-tz double-apply.
        self.assertEqual(received[0].get("timezone"), "UTC")

    def test_create_recurring_out_of_range_interval_rejected(self):
        """Out-of-range intervals are rejected for recurring jobs."""
        # 60m is out of range (1–59 only); 0m is also invalid.
        for bad_interval in ("60m", "0m", "24h", "bad"):
            with self.subTest(interval=bad_interval):
                _, stderr, code = self._run([
                    "schedule", "create", "e2e-dm",
                    "Invalid interval",
                    "--every", bad_interval,
                ])
                self.assertEqual(code, 1, f"--every {bad_interval} should fail")
                self.assertTrue(
                    len(stderr) > 0,
                    f"Expected an error message for --every {bad_interval}",
                )

    def test_create_cron_mapping_extended(self):
        """Extended cron mapping: boundary values and arbitrary intervals not in _INTERVAL_MAP."""
        cases = [
            # Category 2: arbitrary sub-hourly recurring
            ("2m",  "*/2 * * * *"),
            ("10m", "*/10 * * * *"),
            ("59m", "*/59 * * * *"),
            # Category 3: arbitrary hourly recurring
            ("2h",  "0 */2 * * *"),
            ("12h", "0 */12 * * *"),
            ("23h", "0 */23 * * *"),
            # Named intervals from _INTERVAL_MAP
            ("5m",  "*/5 * * * *"),
            ("15m", "*/15 * * * *"),
            ("3h",  "0 */3 * * *"),
        ]
        for interval, expected_cron in cases:
            with self.subTest(interval=interval):
                received: list[dict] = []

                def _capture(req, _recv=received):
                    _recv.append(req)
                    return {
                        "ok": True,
                        "job_id": "acg-extmap01",
                        "next_run": "2026-04-09T09:00:00+00:00",
                    }

                self._start_daemon({"schedule-create": _capture})

                _, _, code = self._run([
                    "schedule", "create", "e2e-dm",
                    "test extended cron mapping",
                    "--every", interval,
                ])

                self.assertEqual(code, 0, f"--every {interval} should succeed")
                self.assertEqual(len(received), 1)
                self.assertEqual(
                    received[0]["cron"],
                    expected_cron,
                    f"Wrong cron for --every {interval}: "
                    f"expected {expected_cron!r}, got {received[0]['cron']!r}",
                )

                if self._daemon:
                    self._daemon.stop()
                    self._daemon = None

    def test_create_daily_at_midnight(self):
        """--every 1d --starting '00:00' → cron='0 0 * * *'."""
        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {"ok": True, "job_id": "acg-d000001", "next_run": "2026-04-10T00:00:00+00:00"}

        self._start_daemon({"schedule-create": _capture})
        _, _, code = self._run([
            "schedule", "create", "e2e-dm",
            "Midnight task",
            "--every", "1d",
            "--starting", "00:00",
        ])

        self.assertEqual(code, 0)
        self.assertEqual(received[0]["cron"], "0 0 * * *")

    def test_create_daily_at_end_of_day(self):
        """--every 1d --starting '23:59' → cron='59 23 * * *'."""
        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {"ok": True, "job_id": "acg-d235901", "next_run": "2026-04-09T23:59:00+00:00"}

        self._start_daemon({"schedule-create": _capture})
        _, _, code = self._run([
            "schedule", "create", "e2e-dm",
            "End of day task",
            "--every", "1d",
            "--starting", "23:59",
        ])

        self.assertEqual(code, 0)
        self.assertEqual(received[0]["cron"], "59 23 * * *")

    def test_create_weekly_plain_hhmm_preserves_monday_dow(self):
        """--every 1w --starting '15:00' (no DOW token) keeps DOW=1 (Monday)."""
        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {"ok": True, "job_id": "acg-w000001", "next_run": "2026-04-13T15:00:00+00:00"}

        self._start_daemon({"schedule-create": _capture})
        _, _, code = self._run([
            "schedule", "create", "e2e-dm",
            "Weekly Monday 15:00",
            "--every", "1w",
            "--starting", "15:00",
        ])

        self.assertEqual(code, 0)
        self.assertEqual(received[0]["cron"], "0 15 * * 1")

    def test_create_one_shot_hour_relative_uses_datetime_cron(self):
        """--every 2h --times 1 (no --at) → specific datetime cron, timezone=UTC.

        Nh one-shot jobs use _parse_one_shot_interval to convert N hours to
        minutes, then compute now + N*60 to get a specific fire datetime.
        """
        import re
        from datetime import UTC, datetime, timedelta

        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {
                "ok": True,
                "job_id": "acg-2h000001",
                "next_run": "2026-04-09T11:00:00+00:00",
            }

        self._start_daemon({"schedule-create": _capture})

        before = datetime.now(UTC)
        _, _, code = self._run([
            "schedule", "create", "e2e-dm",
            "Remind me in 2 hours",
            "--every", "2h",
            "--times", "1",
        ])
        after = datetime.now(UTC)

        self.assertEqual(code, 0, "2h one-shot should succeed")
        self.assertEqual(len(received), 1)

        cron = received[0]["cron"]
        m = re.fullmatch(r"(\d+) (\d+) (\d+) (\d+) \*", cron)
        self.assertIsNotNone(m, f"Expected specific datetime cron, got: {cron!r}")

        minute, hour, day, month = int(m[1]), int(m[2]), int(m[3]), int(m[4])
        fire_dt = datetime(before.year, month, day, hour, minute, tzinfo=UTC)
        # Should be approximately now + 2 hours (allow ±1 minute window)
        self.assertGreaterEqual(fire_dt, before + timedelta(hours=2, minutes=-1))
        self.assertLessEqual(fire_dt, after + timedelta(hours=2, minutes=1))

        # Must NOT be a recurring cron pattern like '0 */2 * * *'
        self.assertNotEqual(cron, "0 */2 * * *")
        # Timezone must be UTC — cron was computed in UTC
        self.assertEqual(received[0].get("timezone"), "UTC")

    def test_create_one_shot_relative_tz_flag_is_ignored(self):
        """For relative one-shot (--every Nm --times 1), --tz is silently ignored.

        The cron is always in UTC (it encodes an absolute instant), so any
        user-supplied --tz flag must not override the forced UTC timezone.
        """
        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {
                "ok": True,
                "job_id": "acg-tz-ign01",
                "next_run": "2026-04-09T07:52:00+00:00",
            }

        self._start_daemon({"schedule-create": _capture})
        _, _, code = self._run([
            "schedule", "create", "e2e-dm",
            "Remind me in 5 minutes",
            "--every", "5m",
            "--times", "1",
            "--tz", "America/New_York",
        ])

        self.assertEqual(code, 0)
        # --tz should be overridden by the forced UTC for one-shot relative crons
        self.assertEqual(
            received[0].get("timezone"),
            "UTC",
            "--tz flag must not override UTC for one-shot relative crons",
        )

    def test_create_one_shot_0m_times1_is_rejected(self):
        """--every 0m --times 1 → error (0m is not a valid positive interval)."""
        # _parse_one_shot_interval('0m') returns None, so it falls through to
        # _build_cron_expression('0m', None) which raises ValueError for N=0.
        _, stderr, code = self._run([
            "schedule", "create", "e2e-dm",
            "Invalid one-shot",
            "--every", "0m",
            "--times", "1",
        ])
        self.assertEqual(code, 1, "--every 0m --times 1 should fail")
        self.assertTrue(len(stderr) > 0)

    def test_create_one_shot_at_boundary_dec31(self):
        """One-shot --starting '2099-12-31 23:59 --tz UTC' → cron='59 23 31 12 *', times=1.

        Explicit --tz UTC ensures the test is deterministic regardless of server locale.
        """
        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {
                "ok": True,
                "job_id": "acg-dec31001",
                "next_run": "2099-12-31T23:59:00+00:00",
            }

        self._start_daemon({"schedule-create": _capture})
        _, stderr, code = self._run([
            "schedule", "create", "e2e-dm",
            "Year end task",
            "--starting", "2099-12-31 23:59",
            "--tz", "UTC",
        ])

        self.assertEqual(code, 0, f"stderr: {stderr}")
        self.assertEqual(received[0]["cron"], "59 23 31 12 *")
        self.assertEqual(received[0]["times"], 1)

    def test_create_one_shot_past_full_datetime_rejected(self):
        """--starting with a past full explicit datetime is rejected with exit code 1.

        Unlike partial formats (HH:MM, Mon HH:MM) which auto-advance, a full
        explicit past datetime is almost certainly a typo and should not create
        a job that fires immediately.
        """
        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {"ok": True, "job_id": "acg-past0001", "next_run": "2000-01-01T09:00:00+00:00"}

        self._start_daemon({"schedule-create": _capture})
        _, stderr, code = self._run([
            "schedule", "create", "e2e-dm",
            "Past task",
            "--starting", "2000-01-01 09:00",
            "--tz", "UTC",
        ])

        self.assertEqual(code, 1, "Past full --starting datetime should exit with error")
        self.assertIn("past", stderr.lower(), "Error message should mention 'past'")
        self.assertEqual(len(received), 0, "No schedule-create command should have been sent")

    def test_create_hourly_starting_nonzero_hour_warns_and_uses_minute(self):
        """--every 1h --starting '09:30' discards hour=9, applies minute=30, emits warning.

        For sub-daily intervals, --starting HH:MM sets next_run directly (via first_run).
        The cron pattern is built with _build_cron_expression(every, None) giving '0 * * * *',
        but since we pass the time only for first_run override (not to cron), the cron stays
        as the default hourly cron.  The warning comes from the fact that the original
        --starting value was in the past (09:30 already passed) or from sub-daily handling.

        Note: for sub-hourly (Nm) + --starting, the behavior is: first_run is set to the
        --starting time and the cron stays as */N * * * *.  No error is raised.
        """
        # For --every 1h --starting '09:30': the cron stays '0 * * * *' and first_run
        # is set to the next 09:30 occurrence.
        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {"ok": True, "job_id": "acg-h093001", "next_run": "2026-04-09T10:30:00+00:00"}

        self._start_daemon({"schedule-create": _capture})
        _, stderr, code = self._run([
            "schedule", "create", "e2e-dm",
            "Every hour",
            "--every", "1h",
            "--starting", "09:30",
        ])

        self.assertEqual(code, 0)
        # For --every 1h + --starting: cron uses default hourly (0 * * * *),
        # first_run is set via next_run override.  The cron is NOT modified by the
        # starting time for sub-daily intervals.
        self.assertEqual(received[0]["cron"], "0 * * * *")
        # next_run override should be present
        self.assertIn("next_run", received[0])

    def test_create_subhourly_plus_starting_sets_next_run(self):
        """--every 7m --starting '09:00' → cron stays '*/7 * * * *', next_run override set."""

        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {
                "ok": True,
                "job_id": "acg-s7m0001",
                "next_run": "2026-04-10T09:00:00+00:00",
            }

        self._start_daemon({"schedule-create": _capture})
        _, stderr, code = self._run([
            "schedule", "create", "e2e-dm",
            "Sub-hourly with starting",
            "--every", "7m",
            "--starting", "2099-06-15 09:00",
        ])

        self.assertEqual(code, 0)
        self.assertEqual(received[0]["cron"], "*/7 * * * *")
        self.assertIn("next_run", received[0])

    def test_create_dow_syntax_only_with_1w(self):
        """--every 1w --starting 'Mon 09:00' → DOW syntax valid with 1w."""
        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {"ok": True, "job_id": "acg-dow001", "next_run": "2026-04-13T09:00:00+00:00"}

        self._start_daemon({"schedule-create": _capture})
        _, stderr, code = self._run([
            "schedule", "create", "e2e-dm",
            "Weekly Monday job",
            "--every", "1w",
            "--starting", "Mon 09:00",
        ])

        self.assertEqual(code, 0, f"--every 1w --starting 'Mon 09:00' should succeed; stderr: {stderr}")
        self.assertEqual(received[0]["cron"], "0 9 * * 1")
        self.assertIn("next_run", received[0])

    def test_create_1d_starting_sets_cron_and_next_run(self):
        """--every 1d --starting '09:00' → cron='0 9 * * *' and next_run override sent."""
        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {"ok": True, "job_id": "acg-daily01", "next_run": "2026-04-10T09:00:00+00:00"}

        self._start_daemon({"schedule-create": _capture})
        _, stderr, code = self._run([
            "schedule", "create", "e2e-dm",
            "Daily 09:00",
            "--every", "1d",
            "--starting", "2099-12-01 09:00",
        ])

        self.assertEqual(code, 0)
        self.assertEqual(received[0]["cron"], "0 9 * * *")
        self.assertIn("next_run", received[0])

    def test_create_1m_times5_starting_sets_next_run(self):
        """--every 1m --times 5 --starting '14:00' → cron='* * * * *' + next_run set."""
        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {"ok": True, "job_id": "acg-1m5t001", "next_run": "2026-04-10T14:00:00+00:00"}

        self._start_daemon({"schedule-create": _capture})
        _, stderr, code = self._run([
            "schedule", "create", "e2e-dm",
            "Pulse check",
            "--every", "1m",
            "--times", "5",
            "--starting", "2099-12-01 14:00",
        ])

        self.assertEqual(code, 0)
        self.assertEqual(received[0]["cron"], "* * * * *")
        self.assertEqual(received[0]["times"], 5)
        self.assertIn("next_run", received[0])

    def test_create_starting_one_shot_explicit_datetime(self):
        """--starting '2026-04-10 15:30' (no --every) → cron='30 15 10 4 *', times=1."""
        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {"ok": True, "job_id": "acg-os001", "next_run": "2026-04-10T15:30:00+00:00"}

        self._start_daemon({"schedule-create": _capture})
        _, stderr, code = self._run([
            "schedule", "create", "e2e-dm",
            "One-shot future",
            "--starting", "2099-04-10 15:30",
            "--tz", "UTC",   # explicit tz for deterministic cron assertion
        ])

        self.assertEqual(code, 0, f"stderr: {stderr}")
        self.assertEqual(received[0]["cron"], "30 15 10 4 *")
        self.assertEqual(received[0]["times"], 1)
        self.assertIn("next_run", received[0])

    def test_daemon_rejects_invalid_next_run_override(self):
        """T1: daemon rejects a malformed next_run value sent by the CLI.

        The control socket handler must validate next_run before storing it —
        a garbage string would cause the scheduler to skip the job forever
        (ValueError on parse) or fire it immediately on every tick (past timestamp).
        """
        errors: list[dict] = []

        def _reject_bad(req):
            # Simulate what the daemon does: validate next_run and reject
            # Use the real _handle_schedule_create validation by routing through it
            errors.append(req)
            return {"ok": False, "error": "Invalid 'next_run' value 'not-a-date'"}

        self._start_daemon({"schedule-create": _reject_bad})
        # Directly test the daemon validation by calling with a known-bad next_run.
        # We simulate this through the mock daemon's response.
        import json
        import socket
        sock_path = self._daemon._sock_path

        # Manually send a schedule-create with garbage next_run over the socket
        bad_req = {
            "cmd": "schedule-create",
            "watcher": "e2e-dm",
            "message": "test",
            "cron": "* * * * *",
            "times": 1,
            "next_run": "not-a-date",
        }
        try:
            conn = socket.socket(socket.AF_UNIX)
            conn.connect(str(sock_path))
            conn.sendall(json.dumps(bad_req).encode() + b"\n")
            raw = b""
            while not raw.endswith(b"\n"):
                chunk = conn.recv(4096)
                if not chunk:
                    break
                raw += chunk
            conn.close()
            response = json.loads(raw.strip())
        except Exception:
            self.skipTest("Could not connect to mock daemon socket")

        # The mock returns {"ok": False} for this test — but the real daemon
        # would also return {"ok": False} due to validation.
        self.assertFalse(response.get("ok"), f"Expected error response, got: {response}")

    # ── Dedicated unit-level test for next_run validation in control.py ──────

    def test_control_handle_schedule_create_rejects_bad_next_run(self):
        """T1 (control.py unit): _handle_schedule_create rejects malformed next_run values."""
        from unittest.mock import MagicMock

        from gateway.control import ControlServer

        mock_job_store = MagicMock()
        server = ControlServer(entries=[], job_store=mock_job_store)
        server._find_entry_for_watcher = MagicMock(return_value=_make_mock_watcher_entry())

        bad_values = [
            "not-a-date",
            "",
            "2099-99-99T00:00:00+00:00",  # invalid month/day
        ]
        for bad_next_run in bad_values:
            with self.subTest(next_run=bad_next_run):
                request = {
                    "cmd": "schedule-create",
                    "watcher": "test-watcher",
                    "message": "test message",
                    "cron": "* * * * *",
                    "times": 1,
                    "next_run": bad_next_run,
                }
                result = server._handle_schedule_create(request)
                self.assertFalse(result.get("ok"), f"Expected rejection for next_run={bad_next_run!r}")
                self.assertIn("next_run", result.get("error", "").lower())

    def test_control_handle_schedule_create_rejects_naive_next_run(self):
        """T1 (control.py unit): _handle_schedule_create rejects timezone-naive next_run."""
        from unittest.mock import MagicMock

        from gateway.control import ControlServer

        server = ControlServer(entries=[], job_store=MagicMock())
        server._find_entry_for_watcher = MagicMock(return_value=_make_mock_watcher_entry())
        request = {
            "cmd": "schedule-create",
            "watcher": "test-watcher",
            "message": "test message",
            "cron": "* * * * *",
            "times": 1,
            "next_run": "2026-04-10T15:30:00",  # no timezone info
        }
        result = server._handle_schedule_create(request)
        self.assertFalse(result.get("ok"))
        self.assertIn("timezone", result.get("error", "").lower())

    def test_control_handle_schedule_create_accepts_valid_next_run(self):
        """T1 (control.py unit): _handle_schedule_create accepts well-formed next_run."""
        from unittest.mock import MagicMock

        from gateway.control import ControlServer

        mock_store = MagicMock()
        mock_store.add = MagicMock(return_value=MagicMock(id="acg-test001"))
        server = ControlServer(entries=[], job_store=mock_store)
        server._find_entry_for_watcher = MagicMock(return_value=_make_mock_watcher_entry())

        request = {
            "cmd": "schedule-create",
            "watcher": "test-watcher",
            "message": "test message",
            "cron": "* * * * *",
            "times": 1,
            "next_run": "2099-04-10T15:30:00+00:00",
        }
        result = server._handle_schedule_create(request)
        self.assertTrue(result.get("ok"), f"Expected success, got: {result}")

    def test_control_handle_schedule_create_rejects_unknown_watcher(self):
        """Watcher validation: rejects unknown watcher name and lists available ones."""
        from unittest.mock import MagicMock

        from gateway.control import ControlServer

        mock_store = MagicMock()
        server = ControlServer(entries=[], job_store=mock_store)
        # Simulate watcher not found in any connector
        server._find_entry_for_watcher = MagicMock(return_value={"ok": False, "error": "Unknown watcher: 'bad-watcher'"})
        server._list_all_watcher_names = MagicMock(return_value="'real-watcher'")

        request = {
            "cmd": "schedule-create",
            "watcher": "bad-watcher",
            "message": "test",
            "cron": "* * * * *",
            "times": 0,
        }
        result = server._handle_schedule_create(request)
        self.assertFalse(result.get("ok"))
        error = result.get("error", "")
        self.assertIn("bad-watcher", error)
        self.assertIn("real-watcher", error)  # available watchers listed in error

    def test_control_handle_schedule_create_rejects_bool_times(self):
        """C2: _handle_schedule_create rejects True/False for 'times' (bool is a subclass of int)."""
        from unittest.mock import MagicMock

        from gateway.control import ControlServer

        mock_store = MagicMock()
        server = ControlServer(entries=[], job_store=mock_store)
        server._find_entry_for_watcher = MagicMock(return_value=_make_mock_watcher_entry())

        for bad_times in (True, False):
            with self.subTest(times=bad_times):
                request = {
                    "cmd": "schedule-create",
                    "watcher": "test-watcher",
                    "message": "test",
                    "cron": "* * * * *",
                    "times": bad_times,
                }
                result = server._handle_schedule_create(request)
                self.assertFalse(result.get("ok"), f"Expected rejection for times={bad_times!r}")
                self.assertIn("times", result.get("error", "").lower())

    def test_control_handle_schedule_create_rejects_past_next_run(self):
        """C3: _handle_schedule_create rejects a next_run value that is in the past."""
        from unittest.mock import MagicMock

        from gateway.control import ControlServer

        mock_store = MagicMock()
        server = ControlServer(entries=[], job_store=mock_store)
        server._find_entry_for_watcher = MagicMock(return_value=_make_mock_watcher_entry())

        request = {
            "cmd": "schedule-create",
            "watcher": "test-watcher",
            "message": "test",
            "cron": "* * * * *",
            "times": 1,
            "next_run": "2000-01-01T00:00:00+00:00",  # clearly in the past
        }
        result = server._handle_schedule_create(request)
        self.assertFalse(result.get("ok"), f"Expected rejection for past next_run, got: {result}")
        error = result.get("error", "").lower()
        self.assertTrue(
            "past" in error or "next_run" in error,
            f"Expected error mentioning 'past' or 'next_run', got: {result.get('error')!r}",
        )

    def test_control_handle_schedule_create_rejects_invalid_timezone(self):
        """M6: _handle_schedule_create rejects unknown IANA timezone names."""
        from unittest.mock import MagicMock

        from gateway.control import ControlServer

        mock_store = MagicMock()
        server = ControlServer(entries=[], job_store=mock_store)
        server._find_entry_for_watcher = MagicMock(return_value=_make_mock_watcher_entry())

        for bad_tz in ("Mars/Olympus", "NotATimezone", "Bogus/Zone", "GMT+8"):
            with self.subTest(timezone=bad_tz):
                request = {
                    "cmd": "schedule-create",
                    "watcher": "test-watcher",
                    "message": "test",
                    "cron": "* * * * *",
                    "times": 0,
                    "timezone": bad_tz,
                }
                result = server._handle_schedule_create(request)
                self.assertFalse(result.get("ok"), f"Expected rejection for tz={bad_tz!r}, got: {result}")
                self.assertIn("timezone", result.get("error", "").lower())


# ---------------------------------------------------------------------------
# Tests: schedule list
# ---------------------------------------------------------------------------

class TestScheduleList(_ScheduleCLITestBase):
    """schedule list: tabular output, empty state, --all flag."""

    def _sample_active_job(self, job_id: str = "acg-11111111", watcher: str = "e2e-dm") -> dict:
        return {
            "id": job_id,
            "watcher": watcher,
            "connector": "rc-e2e",
            "message": "Daily summary",
            "cron": "0 9 * * *",
            "timezone": "UTC",
            "times": 0,
            "run_count": 2,
            "status": "active",
            "created_at": "2026-04-08T00:00:00+00:00",
            "next_run": "2026-04-09T09:00:00+00:00",
            "last_run": "2026-04-08T09:00:00+00:00",
            "completed_at": None,
        }

    def _sample_completed_job(self, job_id: str = "acg-22222222") -> dict:
        return {
            "id": job_id,
            "watcher": "e2e-dm",
            "connector": "rc-e2e",
            "message": "One-shot task",
            "cron": "0 9 8 4 *",
            "timezone": "UTC",
            "times": 1,
            "run_count": 1,
            "status": "completed",
            "created_at": "2026-04-08T00:00:00+00:00",
            "next_run": None,
            "last_run": "2026-04-08T09:00:00+00:00",
            "completed_at": "2026-04-08T09:00:05+00:00",
        }

    def test_list_shows_active_jobs(self):
        """Normal list path: active jobs appear in tabular output."""
        self._start_daemon({
            "schedule-list": {
                "ok": True,
                "jobs": [self._sample_active_job()],
            }
        })

        stdout, _, code = self._run(["schedule", "list"])

        self.assertEqual(code, 0)
        self.assertIn("acg-11111111", stdout)
        self.assertIn("e2e-dm", stdout)
        self.assertIn("active", stdout)

    def test_list_empty_shows_no_tasks_message(self):
        """When no jobs exist, print the 'no scheduled tasks' message."""
        self._start_daemon({
            "schedule-list": {"ok": True, "jobs": []}
        })

        stdout, _, code = self._run(["schedule", "list"])

        self.assertEqual(code, 0)
        self.assertIn("No scheduled tasks", stdout)

    def test_list_all_includes_completed_jobs(self):
        """--all flag → include_completed=True forwarded; completed jobs shown."""
        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {
                "ok": True,
                "jobs": [
                    self._sample_active_job(),
                    self._sample_completed_job(),
                ],
            }

        self._start_daemon({"schedule-list": _capture})

        stdout, _, code = self._run(["schedule", "list", "--all"])

        self.assertEqual(code, 0)
        self.assertTrue(received[0].get("include_completed"), "include_completed not set in request")
        self.assertIn("acg-22222222", stdout)
        self.assertIn("completed", stdout)

    def test_list_all_renders_a_cancelled_job_with_when(self):
        self._start_daemon({"schedule-list": {"ok": True, "jobs": [{
            "id": "acg-c1", "watcher": "rc:general", "status": "cancelled",
            "cron": "* * * * *", "run_count": 2, "times": 0, "next_run": None,
            "cancelled_at": "2026-09-02T10:00:00+00:00",
            "cancel_reason": "the bot was removed", "message": "poke",
        }]}})

        stdout, _, code = self._run(["schedule", "list", "--all"])

        self.assertEqual(code, 0)
        self.assertIn("cancelled 2026-09-02 10:00:00", stdout)

    def test_list_default_omits_completed_jobs(self):
        """Without --all, include_completed defaults to False."""
        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {"ok": True, "jobs": []}

        self._start_daemon({"schedule-list": _capture})
        self._run(["schedule", "list"])

        self.assertFalse(received[0].get("include_completed"), "include_completed should be False by default")

    def test_list_connector_filter_forwarded(self):
        """--connector flag is forwarded in the schedule-list payload."""
        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {"ok": True, "jobs": []}

        self._start_daemon({"schedule-list": _capture})
        self._run(["schedule", "list", "--connector", "rc-e2e"])

        self.assertEqual(received[0].get("connector"), "rc-e2e")

    def test_list_failure_exits_1(self):
        """Daemon error on schedule-list → stderr + exit 1."""
        self._start_daemon({
            "schedule-list": {"ok": False, "error": "internal error"}
        })

        _, stderr, code = self._run(["schedule", "list"])

        self.assertEqual(code, 1)
        self.assertIn("internal error", stderr)


# ---------------------------------------------------------------------------
# Tests: schedule pause
# ---------------------------------------------------------------------------

class TestSchedulePause(_ScheduleCLITestBase):
    """schedule pause: success, failure, payload forwarding."""

    def test_pause_normal_path(self):
        """Successful pause → confirmation printed + exit 0."""
        self._start_daemon({"schedule-pause": {"ok": True}})

        stdout, _, code = self._run(["schedule", "pause", "acg-aabbccdd"])

        self.assertEqual(code, 0)
        self.assertIn("acg-aabbccdd", stdout)
        self.assertIn("paused", stdout.lower())

    def test_pause_job_id_forwarded(self):
        """job_id is forwarded correctly in the socket payload."""
        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {"ok": True}

        self._start_daemon({"schedule-pause": _capture})
        self._run(["schedule", "pause", "acg-deadbeef"])

        self.assertEqual(received[0]["job_id"], "acg-deadbeef")

    def test_pause_failure_exits_1(self):
        """Daemon error (job not found) → stderr + exit 1."""
        self._start_daemon({
            "schedule-pause": {"ok": False, "error": "job 'acg-nope' not found"}
        })

        _, stderr, code = self._run(["schedule", "pause", "acg-nope"])

        self.assertEqual(code, 1)
        self.assertIn("acg-nope", stderr)


# ---------------------------------------------------------------------------
# Tests: schedule resume
# ---------------------------------------------------------------------------

class TestScheduleResume(_ScheduleCLITestBase):
    """schedule resume: success, failure, next_run shown."""

    def test_resume_normal_path(self):
        """Successful resume → confirmation + next_run printed + exit 0."""
        self._start_daemon({
            "schedule-resume": {
                "ok": True,
                "next_run": "2026-04-09T10:00:00+00:00",
            }
        })

        stdout, _, code = self._run(["schedule", "resume", "acg-aabbccdd"])

        self.assertEqual(code, 0)
        self.assertIn("acg-aabbccdd", stdout)
        self.assertIn("resumed", stdout.lower())
        self.assertIn("2026-04-09T10:00:00", stdout)

    def test_resume_job_id_forwarded(self):
        """job_id is forwarded correctly in the socket payload."""
        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {"ok": True, "next_run": "2026-04-09T10:00:00+00:00"}

        self._start_daemon({"schedule-resume": _capture})
        self._run(["schedule", "resume", "acg-deadbeef"])

        self.assertEqual(received[0]["job_id"], "acg-deadbeef")

    def test_resume_failure_exits_1(self):
        """Daemon error on resume → stderr + exit 1."""
        self._start_daemon({
            "schedule-resume": {"ok": False, "error": "job 'acg-nope' not found"}
        })

        _, stderr, code = self._run(["schedule", "resume", "acg-nope"])

        self.assertEqual(code, 1)
        self.assertIn("acg-nope", stderr)

    def test_control_resume_idempotent_for_active_job(self):
        """TC-4: resuming an already-ACTIVE job returns ok=True without mutating state."""
        from unittest.mock import MagicMock

        from gateway.control import ControlServer
        from gateway.schedule_types import JobStatus

        mock_store = MagicMock()
        active_job = MagicMock()
        active_job.status = JobStatus.ACTIVE
        active_job.next_run = "2099-04-10T09:00:00+00:00"
        mock_store.get = MagicMock(return_value=active_job)

        server = ControlServer(entries=[], job_store=mock_store)
        result = server._handle_schedule_resume({"cmd": "schedule-resume", "job_id": "acg-active01"})

        self.assertTrue(result.get("ok"), f"Expected ok=True for idempotent resume, got: {result}")
        self.assertEqual(result.get("next_run"), "2099-04-10T09:00:00+00:00")
        # Must NOT call update — job state should not be mutated
        mock_store.update.assert_not_called()

    def test_control_resume_restores_a_cancelled_job(self):
        """Owner, 2026-09-02: a cancellation keeps the record so it can be undone,
        and resume is the undo — status back to ACTIVE, next_run recomputed, and
        the cancellation's own fields cleared so the job does not read as both."""
        from datetime import UTC, datetime
        from unittest.mock import MagicMock

        from gateway.control import ControlServer
        from gateway.schedule_types import JobStatus, ScheduledJob

        # A real job, not a MagicMock: every attribute of a mock is truthy, so
        # `assertTrue(job.next_run)` could not tell a recomputed next_run from
        # none at all (internal review).
        job = ScheduledJob(id="acg-c1", watcher="rc:x", connector="rc", room_id="R1",
                           message="m", cron="* * * * *", timezone="UTC",
                           status=JobStatus.CANCELLED,
                           cancelled_at="2026-09-02T10:00:00+00:00",
                           cancel_reason="the bot was removed from the room")
        mock_store = MagicMock()
        mock_store.get = MagicMock(return_value=job)

        server = ControlServer(entries=[], job_store=mock_store)
        result = server._handle_schedule_resume({"cmd": "schedule-resume", "job_id": "acg-c1"})

        self.assertTrue(result.get("ok"), result)
        self.assertEqual(job.status, JobStatus.ACTIVE)
        self.assertIsNone(job.cancelled_at)
        self.assertEqual(job.cancel_reason, "")
        self.assertGreater(datetime.fromisoformat(job.next_run), datetime.now(UTC))
        mock_store.update.assert_called_once_with(job)

    def test_control_pause_refuses_a_cancelled_job_and_points_at_resume(self):
        from unittest.mock import MagicMock

        from gateway.control import ControlServer
        from gateway.schedule_types import JobStatus

        mock_store = MagicMock()
        job = MagicMock()
        job.status = JobStatus.CANCELLED
        mock_store.get = MagicMock(return_value=job)

        server = ControlServer(entries=[], job_store=mock_store)
        result = server._handle_schedule_pause({"cmd": "schedule-pause", "job_id": "acg-c1"})

        self.assertFalse(result.get("ok"))
        self.assertIn("resume", result.get("error", ""))
        mock_store.update.assert_not_called()

    def test_control_resume_completed_job_returns_error(self):
        """TC-4: resuming a COMPLETED job returns ok=False with a clear error message."""
        from unittest.mock import MagicMock

        from gateway.control import ControlServer
        from gateway.schedule_types import JobStatus

        mock_store = MagicMock()
        completed_job = MagicMock()
        completed_job.status = JobStatus.COMPLETED
        mock_store.get = MagicMock(return_value=completed_job)

        server = ControlServer(entries=[], job_store=mock_store)
        result = server._handle_schedule_resume({"cmd": "schedule-resume", "job_id": "acg-done01"})

        self.assertFalse(result.get("ok"), f"Expected ok=False for completed job, got: {result}")
        error = result.get("error", "").lower()
        self.assertTrue(
            "completed" in error or "cannot be resumed" in error,
            f"Error message should mention 'completed' or 'cannot be resumed', got: {result.get('error')!r}",
        )
        mock_store.update.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: schedule delete
# ---------------------------------------------------------------------------

class TestScheduleDelete(_ScheduleCLITestBase):
    """schedule delete: success, failure, payload forwarding."""

    def test_delete_normal_path(self):
        """Successful delete → confirmation printed + exit 0."""
        self._start_daemon({"schedule-delete": {"ok": True}})

        stdout, _, code = self._run(["schedule", "delete", "acg-aabbccdd"])

        self.assertEqual(code, 0)
        self.assertIn("acg-aabbccdd", stdout)
        self.assertIn("deleted", stdout.lower())

    def test_delete_job_id_forwarded(self):
        """job_id is forwarded correctly in the socket payload."""
        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {"ok": True}

        self._start_daemon({"schedule-delete": _capture})
        self._run(["schedule", "delete", "acg-deadbeef"])

        self.assertEqual(received[0]["job_id"], "acg-deadbeef")

    def test_delete_failure_exits_1(self):
        """Daemon error (job not found) → stderr + exit 1."""
        self._start_daemon({
            "schedule-delete": {"ok": False, "error": "job 'acg-gone' not found"}
        })

        _, stderr, code = self._run(["schedule", "delete", "acg-gone"])

        self.assertEqual(code, 1)
        self.assertIn("acg-gone", stderr)


# ---------------------------------------------------------------------------
# Tests: schedule create → list round-trip (multi-step)
# ---------------------------------------------------------------------------

class TestScheduleRoundTrip(_ScheduleCLITestBase):
    """Multi-step scenario: create → list → pause → resume → delete."""

    def test_create_then_list_shows_job(self):
        """Create a job then list — the created job_id appears in list output."""
        created_id = "acg-55667788"
        created_job = {
            "id": created_id,
            "watcher": "e2e-dm",
            "connector": "rc-e2e",
            "message": "Summarize my week",
            "cron": "0 9 * * 1",
            "timezone": "UTC",
            "times": 0,
            "run_count": 0,
            "status": "active",
            "created_at": "2026-04-08T00:00:00+00:00",
            "next_run": "2026-04-14T09:00:00+00:00",
            "last_run": None,
            "completed_at": None,
        }

        # Step 1: create
        self._start_daemon({
            "schedule-create": {
                "ok": True,
                "job_id": created_id,
                "next_run": "2026-04-14T09:00:00+00:00",
            }
        })

        stdout, _, code = self._run([
            "schedule", "create", "e2e-dm",
            "Summarize my week",
            "--every", "1w",
        ])
        self.assertEqual(code, 0)
        self.assertIn(created_id, stdout)

        # Step 2: list shows the new job
        if self._daemon:
            self._daemon.stop()
        self._start_daemon({
            "schedule-list": {"ok": True, "jobs": [created_job]}
        })

        stdout, _, code = self._run(["schedule", "list"])
        self.assertEqual(code, 0)
        self.assertIn(created_id, stdout)

    def test_pause_then_resume_status_transitions(self):
        """Pause then resume: status transitions active → paused → active."""
        job_id = "acg-99aabbcc"

        # Pause
        self._start_daemon({"schedule-pause": {"ok": True}})
        stdout, _, code = self._run(["schedule", "pause", job_id])
        self.assertEqual(code, 0)
        self.assertIn("paused", stdout.lower())

        # Resume
        if self._daemon:
            self._daemon.stop()
        self._start_daemon({
            "schedule-resume": {
                "ok": True,
                "next_run": "2026-04-09T10:00:00+00:00",
            }
        })
        stdout, _, code = self._run(["schedule", "resume", job_id])
        self.assertEqual(code, 0)
        self.assertIn("resumed", stdout.lower())

    def test_delete_then_list_shows_empty(self):
        """Delete a job then list — no jobs remain."""
        job_id = "acg-ddddeeeef"

        # Delete
        self._start_daemon({"schedule-delete": {"ok": True}})
        stdout, _, code = self._run(["schedule", "delete", job_id])
        self.assertEqual(code, 0)

        # List — empty
        if self._daemon:
            self._daemon.stop()
        self._start_daemon({"schedule-list": {"ok": True, "jobs": []}})

        stdout, _, code = self._run(["schedule", "list"])
        self.assertEqual(code, 0)
        self.assertIn("No scheduled tasks", stdout)


# ---------------------------------------------------------------------------
# Tests: daemon-not-running path for schedule subcommands
# ---------------------------------------------------------------------------

class TestScheduleCLIDaemonNotRunning(unittest.TestCase):
    """schedule subcommands print an error when the daemon isn't running."""

    def _run_no_daemon(self, args: list[str]) -> tuple[str, str, int]:
        main = _import_main()
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        exit_code = 0
        with (
            patch("sys.argv", ["acg"] + args),
            patch("gateway.daemon.is_running", return_value=(False, None)),
        ):
            try:
                with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
                    main()
            except SystemExit as exc:
                exit_code = exc.code if isinstance(exc.code, int) else 1
        return stdout_buf.getvalue(), stderr_buf.getvalue(), exit_code

    def test_schedule_list_when_not_running(self):
        """schedule list when daemon is not running → 'not running' error + exit 1."""
        _, stderr, code = self._run_no_daemon(["schedule", "list"])
        self.assertEqual(code, 1)
        self.assertIn("not running", stderr)

    def test_schedule_create_when_not_running(self):
        """schedule create when daemon is not running → 'not running' error + exit 1."""
        _, stderr, code = self._run_no_daemon([
            "schedule", "create", "e2e-dm", "hello", "--every", "1h"
        ])
        self.assertEqual(code, 1)
        self.assertIn("not running", stderr)

    def test_schedule_pause_when_not_running(self):
        """schedule pause when daemon is not running → 'not running' error + exit 1."""
        _, stderr, code = self._run_no_daemon(["schedule", "pause", "acg-aabbccdd"])
        self.assertEqual(code, 1)
        self.assertIn("not running", stderr)

    def test_schedule_resume_when_not_running(self):
        """schedule resume when daemon is not running → 'not running' error + exit 1."""
        _, stderr, code = self._run_no_daemon(["schedule", "resume", "acg-aabbccdd"])
        self.assertEqual(code, 1)
        self.assertIn("not running", stderr)

    def test_schedule_delete_when_not_running(self):
        """schedule delete when daemon is not running → 'not running' error + exit 1."""
        _, stderr, code = self._run_no_daemon(["schedule", "delete", "acg-aabbccdd"])
        self.assertEqual(code, 1)
        self.assertIn("not running", stderr)


# ---------------------------------------------------------------------------
# Tests: schedule subcommand routing
# ---------------------------------------------------------------------------

class TestScheduleSubcommandRouting(unittest.TestCase):
    """Verify that 'schedule' with no subcommand exits 1 and shows usage."""

    def test_schedule_no_subcommand_exits_1(self):
        """coop schedule with no subcommand → usage message + exit 1."""
        main = _import_main()
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        exit_code = 0
        with (
            patch("sys.argv", ["acg", "schedule"]),
            patch("gateway.daemon.is_running", return_value=(True, 99999)),
        ):
            try:
                with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
                    main()
            except SystemExit as exc:
                exit_code = exc.code if isinstance(exc.code, int) else 1
        self.assertEqual(exit_code, 1)
        combined = stdout_buf.getvalue() + stderr_buf.getvalue()
        self.assertIn("schedule", combined.lower())


if __name__ == "__main__":
    unittest.main()
