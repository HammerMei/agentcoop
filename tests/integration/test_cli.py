"""Integration tests for gateway/cli.py.

Exercises the full CLI path: argument parsing → command dispatch → Unix socket
communication → output formatting.  Uses a real Unix socket server running in a
background thread so that ``_send_command_async`` makes an actual network call.

Run with:
    uv run python -m pytest tests/test_cli.py -v
"""

from __future__ import annotations

import io
import json
import shutil
import socket
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _import_main():
    from gateway.cli import main
    return main



pytestmark = pytest.mark.integration

class _MockDaemon:
    """Minimal Unix-socket server that returns canned JSON responses.

    Runs in a background daemon thread so the test's ``asyncio.run()`` call
    (inside ``_send_command_async``) can connect to it synchronously.
    """

    def __init__(self, sock_path: Path, responses: dict):
        self._sock_path = sock_path
        self._responses = responses
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
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
                        # Allow a callable for dynamic responses
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


# ---------------------------------------------------------------------------
# Base test class
# ---------------------------------------------------------------------------

class _CLITestBase(unittest.TestCase):
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
# Tests: argument parsing edge cases
# ---------------------------------------------------------------------------

class TestCLIArgParsing(unittest.TestCase):
    """Argument parsing: no command → print help + exit 1."""

    def test_no_command_exits_1(self):
        main = _import_main()
        with (
            patch("sys.argv", ["acg"]),
            self.assertRaises(SystemExit) as cm,
        ):
            main()
        self.assertEqual(cm.exception.code, 1)


class TestCLIInstructions(_CLITestBase):
    """instructions: print bundled docs without contacting the daemon."""

    def test_instructions_scheduling_prints_scheduling_doc(self):
        stdout, stderr, code = self._run(["instructions", "scheduling"])

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("# AgentCoop Scheduling Commands", stdout)
        self.assertIn("coop schedule create", stdout)

    def test_instructions_fetch_history_prints_fetch_history_doc(self):
        stdout, stderr, code = self._run(["instructions", "fetch-history"])

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("# fetch-history", stdout)
        self.assertIn("coop fetch-history", stdout)


# ---------------------------------------------------------------------------
# Tests: config (no subcommand) — launches the interactive config TUI
# ---------------------------------------------------------------------------

class TestCLIConfigLaunchesTUI(_CLITestBase):
    """'config' with no subcommand launches gateway.configtool.run_app.

    _run() redirects stdout/stderr to io.StringIO, which is never a TTY, so
    every case here exercises run_app's own TTY guard — the same guard a
    real piped/non-interactive invocation would hit. A test that needs to
    verify the *arguments* run_app receives patches run_app itself rather
    than trying to actually launch a full-screen Textual app in a test.
    """

    def test_no_subcommand_hits_tty_guard_and_exits_one(self):
        stdout, stderr, code = self._run(["config"])
        self.assertEqual(code, 1)
        self.assertIn("requires an interactive terminal", stderr)

    def test_no_subcommand_does_not_print_old_usage_message(self):
        """Regression: before this change, no-subcommand printed a plain
        usage string and exited 1 — it must now attempt to launch the TUI
        (and hit the TTY guard under test) instead."""
        stdout, stderr, code = self._run(["config"])
        self.assertNotIn("Usage: coop config", stdout + stderr)

    def test_config_and_lint_flags_are_forwarded_to_run_app(self):
        with patch("gateway.configtool.run_app") as mock_run_app:
            mock_run_app.return_value = 0
            self._run(["config", "--config", "/tmp/example-config.yaml", "--lint"])
        mock_run_app.assert_called_once_with("/tmp/example-config.yaml", lint=True)

    def test_lint_defaults_to_false(self):
        with patch("gateway.configtool.run_app") as mock_run_app:
            mock_run_app.return_value = 0
            self._run(["config", "--config", "/tmp/example-config.yaml"])
        mock_run_app.assert_called_once_with("/tmp/example-config.yaml", lint=False)

    def test_default_config_path_used_when_omitted(self):
        with patch("gateway.configtool.run_app") as mock_run_app:
            mock_run_app.return_value = 0
            self._run(["config"])
        from gateway.cli import DEFAULT_CONFIG
        mock_run_app.assert_called_once_with(DEFAULT_CONFIG, lint=False)

    def test_exit_code_propagates_from_run_app(self):
        with patch("gateway.configtool.run_app") as mock_run_app:
            mock_run_app.return_value = 1
            _, _, code = self._run(["config"])
        self.assertEqual(code, 1)

    def test_validate_subcommand_still_dispatches_normally_not_to_tui(self):
        """Non-regression: 'config validate' must never fall through to
        run_app — the two dispatch paths must stay mutually exclusive."""
        with patch("gateway.configtool.run_app") as mock_run_app:
            cfg_path = Path(self.tmp) / "config.yaml"
            cfg_path.write_text("connectors: []\nagents: {}\n")
            with patch("gateway.core.state.RUNTIME_DIR", Path(self.tmp) / "runtime"):
                self._run(["config", "validate", "--config", str(cfg_path)])
        mock_run_app.assert_not_called()

    def test_lint_before_subcommand_does_not_leak_into_validate(self):
        """Regression: --lint used to share a dest with config_validate_p's
        own --lint, so argparse's subparser dispatch silently overwrote it —
        'config --lint validate' parsed to lint=False for validate_config
        even though the flag was given. Now the two are independent, scoped
        attributes (lint_for_tui vs. validate's own lint) — placing --lint
        before the subcommand must not affect the subcommand's own value."""
        with patch("gateway.config_validate.validate_config") as mock_validate:
            mock_validate.return_value.ok = True
            mock_validate.return_value.errors = []
            mock_validate.return_value.warnings = []
            mock_validate.return_value.lint_findings = []
            mock_validate.return_value.entry_count = 0
            mock_validate.return_value.watcher_count = 0
            self._run(["config", "--lint", "validate", "--config", "/tmp/x.yaml"])
        mock_validate.assert_called_once_with("/tmp/x.yaml", lint=False)

    def test_lint_before_subcommand_sets_tui_lint_when_no_subcommand_given(self):
        """The parent --lint (scoped to launching the TUI) still works
        correctly on its own, independent of the child's own --lint."""
        with patch("gateway.configtool.run_app") as mock_run_app:
            mock_run_app.return_value = 0
            self._run(["config", "--lint", "--config", "/tmp/x.yaml"])
        mock_run_app.assert_called_once_with("/tmp/x.yaml", lint=True)


# ---------------------------------------------------------------------------
# Tests: config validate command
# ---------------------------------------------------------------------------

class TestCLIConfigValidate(_CLITestBase):
    """config validate: validate config.yaml without contacting the daemon.

    gateway.core.state.RUNTIME_DIR is patched to a per-test temp dir in every
    case — otherwise the state-orphan check would read this machine's real
    ~/.agentcoop/state.*.json files and make the test non-hermetic.
    """

    def setUp(self):
        super().setUp()
        self.agent_dir = Path(self.tmp) / "work"
        self.agent_dir.mkdir()
        self.runtime_dir = Path(self.tmp) / "runtime"

    def _write(self, yaml_text: str) -> str:
        path = Path(self.tmp) / "config.yaml"
        path.write_text(textwrap.dedent(yaml_text))
        return str(path)

    def _run_validate(self, extra_args: list[str] | None = None, config_path: str | None = None):
        args = ["config", "validate", "--config", config_path] + (extra_args or [])
        with patch("gateway.core.state.RUNTIME_DIR", self.runtime_dir):
            return self._run(args)

    def test_valid_config_exits_zero(self):
        cfg_path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        stdout, stderr, code = self._run_validate(config_path=cfg_path)

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("✓", stdout)
        self.assertIn("1 watcher(s)", stdout)

    def test_missing_working_directory_exits_one(self):
        cfg_path = self._write("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agents:
              default:
                type: claude
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        stdout, stderr, code = self._run_validate(config_path=cfg_path)

        self.assertEqual(code, 1)
        self.assertIn("working_directory is required", stderr)

    def test_empty_rocketchat_credentials_flagged_as_errors(self):
        """from_connector_config silently defaults server.url/username/password
        to "" — config_validate.py must catch what from_file alone does not."""
        cfg_path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        stdout, stderr, code = self._run_validate(config_path=cfg_path)

        self.assertEqual(code, 1)
        self.assertIn("server.url is empty", stderr)
        self.assertIn("server.username is empty", stderr)
        self.assertIn("server.password is empty", stderr)

    def test_lint_flags_redundant_default(self):
        cfg_path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
                timeout: 360
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        stdout, stderr, code = self._run_validate(["--lint"], config_path=cfg_path)

        self.assertEqual(code, 0)
        self.assertIn("agents.default.timeout", stdout)
        self.assertIn("restates the built-in default", stdout)

    def test_lint_with_no_findings_says_so(self):
        cfg_path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        stdout, stderr, code = self._run_validate(["--lint"], config_path=cfg_path)

        self.assertEqual(code, 0)
        self.assertIn("no redundant defaults found", stdout)

    def test_rooms_expansion_reflected_in_summary(self):
        cfg_path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watcher_rules:
              - name: two-rooms
                agent: default
                connector: rc
                rooms:
                  include: [general, dev]
        """)
        stdout, stderr, code = self._run_validate(config_path=cfg_path)

        self.assertEqual(code, 0)
        # One rule covering two rooms is one watcher entry — the static
        # expansion ("2 watcher(s), expanded from 1 entries") died with its
        # shape; rooms materialize at runtime now.
        self.assertIn("1 watcher(s)", stdout)

    def test_state_orphan_produces_warning(self):
        cfg_path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        # Imported here, not at module scope: this file defers every gateway import
        # (see _import_main) so the CLI's own import-time behaviour stays under test.
        from gateway.core.state import STATE_FORMAT_VERSION

        self.runtime_dir.mkdir()
        (self.runtime_dir / "state.rc.json").write_text(json.dumps({
            "version": STATE_FORMAT_VERSION,
            "watchers": [{"watcher_name": "stale-watcher", "session_id": "x", "room_id": "y"}]
        }))

        stdout, stderr, code = self._run_validate(config_path=cfg_path)

        self.assertEqual(code, 0)
        self.assertIn("stale-watcher", stdout)
        # Contract, not phrasing: the warning was rewritten in plain language
        # ("pruned" meant nothing to a reader who had not seen the old format).
        self.assertIn("older version", stdout)
        self.assertIn("discard", stdout)


class TestCLIConfigMigrateEnv(_CLITestBase):
    """config migrate-env: standalone entry point for the same one-time
    migration gateway/daemon.py's start_daemon() runs automatically."""

    def setUp(self):
        super().setUp()
        self.agent_dir = Path(self.tmp) / "work"
        self.agent_dir.mkdir()

    def _write(self, yaml_text: str) -> str:
        path = Path(self.tmp) / "config.yaml"
        path.write_text(textwrap.dedent(yaml_text))
        return str(path)

    def _run_migrate(self, config_path: str):
        return self._run(["config", "migrate-env", "--config", config_path])

    def test_missing_config_path_reports_an_error_not_a_false_success(self):
        """Round-2 code-review finding: a missing config path used to be
        reported as a false 'Nothing to migrate' success (exit 0) whenever
        no .env sat alongside it — because the .env-exists check ran before
        confirming config.yaml itself existed. Must now report the missing
        file clearly and exit non-zero."""
        missing_path = str(Path(self.tmp) / "does-not-exist.yaml")
        self.assertFalse(Path(missing_path).exists())

        stdout, stderr, code = self._run_migrate(missing_path)

        self.assertEqual(code, 1)
        self.assertIn("Migration failed", stderr)
        self.assertNotIn("Nothing to migrate", stdout)

    def test_no_env_file_reports_nothing_to_do(self):
        cfg_path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        stdout, stderr, code = self._run_migrate(cfg_path)

        self.assertEqual(code, 0)
        self.assertIn("Nothing to migrate", stdout)

    def test_migrates_and_reports_the_reference_count(self):
        cfg_path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: "${{RC_PASSWORD}}"}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        (Path(self.tmp) / ".env").write_text("RC_PASSWORD=hunter2\n")

        stdout, stderr, code = self._run_migrate(cfg_path)

        self.assertEqual(code, 0)
        self.assertIn("Migrated 1 secret reference(s)", stdout)
        self.assertFalse((Path(self.tmp) / ".env").exists())
        raw = yaml.safe_load(Path(cfg_path).read_text())
        self.assertEqual(raw["connectors"][0]["server"]["password"], "hunter2")

    def test_unresolvable_reference_exits_nonzero(self):
        cfg_path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: "${{MISSING_VAR}}"}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        (Path(self.tmp) / ".env").write_text("UNRELATED=1\n")

        stdout, stderr, code = self._run_migrate(cfg_path)

        self.assertEqual(code, 1)
        self.assertIn("Migration failed", stderr)
        self.assertTrue((Path(self.tmp) / ".env").exists())

    def test_plain_oserror_is_caught_cleanly_not_a_raw_traceback(self):
        """Code-review finding: the original except clause only caught
        (ValueError, FileNotFoundError) — a plain OSError (e.g. a
        PermissionError from env_path.rename()) would have crashed with an
        unhandled traceback instead of the clean '✗ Migration failed' message."""
        cfg_path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)

        with patch(
            "gateway.config_migrate.migrate_env_to_config",
            side_effect=OSError("disk full"),
        ):
            stdout, stderr, code = self._run_migrate(cfg_path)

        self.assertEqual(code, 1)
        self.assertIn("Migration failed", stderr)
        self.assertIn("disk full", stderr)


# ---------------------------------------------------------------------------
# Tests: status command
# ---------------------------------------------------------------------------

class TestCLIStatus(_CLITestBase):
    """status command: outputs running/not-running state."""

    def _write_pid_file(self):
        self.pid_file.parent.mkdir(parents=True, exist_ok=True)
        self.pid_file.write_text("99999")

    def test_status_not_running(self):
        """When daemon is not running, print 'not running'."""
        main = _import_main()
        stdout_buf = io.StringIO()
        with (
            patch("sys.argv", ["acg", "status"]),
            patch("gateway.daemon.is_running", return_value=(False, None)),
        ):
            with redirect_stdout(stdout_buf):
                main()
        self.assertIn("not running", stdout_buf.getvalue())

    def test_status_running_shows_pid_and_uptime(self):
        """When daemon is running, print pid, uptime, and watcher count."""
        self._write_pid_file()
        self._start_daemon({"list": {"ok": True, "data": [{"x": 1}, {"x": 2}], "errors": []}})

        stdout, _, code = self._run(["status"])

        self.assertEqual(code, 0)
        self.assertIn("running", stdout)
        self.assertIn("99999", stdout)          # pid shown
        self.assertIn("Watchers: 2", stdout)     # watcher count from list response

    def test_status_counts_every_state(self):
        """`status` reports a total, so it must not inherit `list`'s narrower
        default — idle rooms would silently drop out of a number that reads as
        "how many watchers does this daemon have"."""
        self._write_pid_file()
        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {"ok": True, "data": [], "errors": []}

        self._start_daemon({"list": _capture})
        self._run(["status"])

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["states"], ["active", "idle", "paused", "failed"])


# ---------------------------------------------------------------------------
# Tests: list command  ← PRIMARY INTEGRATION TEST
# ---------------------------------------------------------------------------

class TestCLIList(_CLITestBase):
    """list: full integration path through socket, response parsing, formatting."""

    _ROWS = [
        {
            "watcher_name": "support",
            "room_name": "#eng-triage",
            "room_id": "rid-support",
            "connector": "rc-prod",
            "agent_name": "claude",
            "session_id": "sess-abc123",
            "participants": [],
            "state": "active",
        },
        {
            "watcher_name": "gdm-a3f9c1b2",
            # A group DM has no platform name, and the server has already
            # collapsed that to the room id (`room_name or room_id`) — the real
            # case for a room whose label is a hash.  (A 1:1 DM is *not* this
            # case: both connectors return the configured `@handle` as its name.)
            "room_name": "rid-gdm",
            "room_id": "rid-gdm",
            "connector": "rc-prod",
            "agent_name": "opencode",
            "session_id": "sess-def456",
            "participants": ["@alice", "@bob"],
            "state": "paused",
        },
    ]

    def test_list_normal_path_shows_watchers(self):
        """Normal path: daemon running, rows returned, table formatted."""
        self._start_daemon({
            "list": {"ok": True, "data": self._ROWS, "errors": []}
        })

        stdout, stderr, code = self._run(["list"])

        self.assertEqual(code, 0, f"stderr: {stderr}")
        header, *rows = stdout.strip().splitlines()
        for column in ("NAME", "CONNECTOR", "ROOM", "ROOM ID", "AGENT", "STATE",
                       "SESSION", "PARTICIPANTS"):
            self.assertIn(column, header)
        self.assertEqual(len(rows), 2)
        self.assertIn("support", rows[0])
        # Pinned separately from the watcher name: with both spelled "support",
        # deleting the ROOM column entirely left every assertion passing.
        self.assertIn("#eng-triage", rows[0])
        self.assertIn("rid-support", rows[0])
        self.assertIn("active", rows[0])
        self.assertIn("sess-abc123", rows[0])
        self.assertIn("paused", rows[1])
        # The participants column is how a group DM is identified, so it is in
        # the default view rather than behind a verbose flag.
        self.assertIn("@alice, @bob", rows[1])
        # And the absent-value placeholder, which nothing else pins.
        self.assertIn("—", rows[0])

    def test_list_columns_are_aligned(self):
        """A table whose columns do not line up is not a table."""
        self._start_daemon({
            "list": {"ok": True, "data": self._ROWS, "errors": []}
        })

        stdout, _, code = self._run(["list"])

        self.assertEqual(code, 0)
        header, *rows = stdout.strip().splitlines()
        state_column = header.index("STATE")
        for row, expected in zip(rows, ("active", "paused")):
            self.assertTrue(
                row[state_column:].startswith(expected),
                f"expected {expected!r} at column {state_column} in: {row!r}",
            )

    def test_a_non_string_participant_does_not_take_down_the_table(self):
        """The loader refuses these, but the CLI reads rows off a socket — it
        does not parse the state file — so a daemon on a different version can
        still hand it one. A formatter must never be the thing that loses every
        other connector's rows."""
        from gateway.cli import _print_watcher_table

        row = dict(self._ROWS[0], participants=[1, None, "@alice"])

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf):
            _print_watcher_table([row])

        out = stdout_buf.getvalue()
        self.assertIn("@alice", out)
        self.assertIn("support", out)

    def test_list_empty_names_the_states_that_were_asked_for(self):
        """"None" and "none you asked about" are different answers.

        The default case points at `--all` without restating what the default
        *is* — the server owns that, and a second copy in the CLI would go
        stale silently.
        """
        self._start_daemon({"list": {"ok": True, "data": [], "errors": []}})

        default_out, _, code = self._run(["list"])
        self.assertEqual(code, 0)
        self.assertIn("--all", default_out)

        idle_out, _, code = self._run(["list", "--idle"])
        self.assertEqual(code, 0)
        self.assertIn("idle", idle_out)
        self.assertNotIn("--all", idle_out)

    def test_a_hard_failure_does_not_get_an_empty_list_answer(self):
        """An unknown --connector comes back ok:false with no `errors` list.

        "No watchers, try --all" is a substantive answer to a query the daemon
        never ran, and it would send the operator to a flag that changes
        nothing.
        """
        self._start_daemon(
            {"list": {"ok": False, "error": "Unknown connector: bogus"}}
        )

        stdout, stderr, code = self._run(["list", "--connector", "bogus"])

        self.assertEqual(code, 1)
        self.assertEqual(stdout.strip(), "")
        self.assertIn("Unknown connector", stderr)

    def test_list_state_flags_are_forwarded(self):
        """The flags compose, and the default is expressed by sending nothing."""
        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {"ok": True, "data": [], "errors": []}

        self._start_daemon({"list": _capture})

        self._run(["list"])
        self._run(["list", "--idle"])
        self._run(["list", "--active", "--paused"])
        self._run(["list", "--all"])
        self._run(["list", "--failed"])
        self._run(["list", "--all", "--idle"])
        self._run(["list", "--connector", "rc-prod", "--idle"])

        self.assertNotIn("states", received[0], "the default lives on the server")
        self.assertEqual(received[1]["states"], ["idle"])
        self.assertEqual(received[2]["states"], ["active", "paused"])
        self.assertEqual(received[3]["states"], ["active", "idle", "paused", "failed"])
        self.assertEqual(received[4]["states"], ["failed"])
        # --all wins over a narrower flag rather than intersecting with it.
        self.assertEqual(received[5]["states"], ["active", "idle", "paused", "failed"])
        # A state filter and a connector filter compose.
        self.assertEqual(received[6]["states"], ["idle"])
        self.assertEqual(received[6]["connector"], "rc-prod")


    def test_list_with_connector_filter(self):
        """--connector flag is forwarded in the command payload."""
        received_cmds: list[dict] = []

        def _capture(req):
            received_cmds.append(req)
            return {"ok": True, "data": [], "errors": []}

        self._start_daemon({"list": _capture})
        self._run(["list", "--connector", "rc-staging"])

        self.assertEqual(len(received_cmds), 1)
        self.assertEqual(received_cmds[0].get("connector"), "rc-staging")

    def test_list_connector_error_exits_nonzero(self):
        """Partial connector failure (errors list) → stderr warning + exit 1."""
        self._start_daemon({
            "list": {
                "ok": True,
                "data": [],
                "errors": [{"connector": "rc-prod", "error": "connection refused"}],
            }
        })

        stdout, stderr, code = self._run(["list"])

        self.assertEqual(code, 1)
        self.assertIn("rc-prod", stderr)


# ---------------------------------------------------------------------------
# Tests: a glob over watcher names (#151)
# ---------------------------------------------------------------------------

_NAMES = ["mm-wavebro:nest", "mm-wavebro:dm:glin", "rc-eng:nest", "rc-eng:general"]


def _rows(names=_NAMES, state="active"):
    return [{"watcher_name": n, "state": state} for n in names]


_ROWS = _rows()


def _listing(rows=_ROWS):
    return {"ok": True, "data": rows}


class TestCLILifecycleGlob(_CLITestBase):
    """pause/resume/reset/expire over a glob: list once, act per match, summarise.

    Spec is issue #151, items 1-7. The daemon here is the canned-response
    mock, so what these pin is the CLI's contract — which commands it sends,
    in what order, what it prints, what it exits with — not the verbs' own
    behaviour, which has its own suites.
    """

    def _capture(self, verb, outcome_for=None, rows=None):
        """A daemon that records the names a verb was sent, in order, and
        answers per name via `outcome_for(name)` (default: ok). Rows default
        to every name in the state the verb acts on, so a plain capture
        exercises the send path rather than the state skip."""
        sent: list[str] = []

        def _handler(req):
            name = req["watcher_name"]
            sent.append(name)
            return outcome_for(name) if outcome_for else {"ok": True}

        if rows is None:
            rows = _rows(state="paused" if verb == "resume" else "active")
        return sent, {"list": _listing(rows), verb: _handler}

    # ── item 1: matching is a glob over the NAME column, DMs included ────────

    def test_glob_lists_every_state_and_acts_on_each_match_in_name_order(self):
        received_lists: list[dict] = []
        sent: list[str] = []

        def _list(req):
            received_lists.append(req)
            return _listing()

        def _pause(req):
            sent.append(req["watcher_name"])
            return {"ok": True}

        self._start_daemon({"list": _list, "pause": _pause})
        stdout, stderr, code = self._run(["pause", "mm-wavebro:*"])

        self.assertEqual(code, 0, stderr)
        # One list, asking for every state — a paused or idle watcher is as
        # much a target as an active one.
        self.assertEqual(len(received_lists), 1)
        self.assertEqual(set(received_lists[0]["states"]),
                         {"active", "idle", "paused", "failed"})
        # The DM is matched like any other watcher (item 1), and the order is
        # the sorted NAME column, so a run is reproducible.
        self.assertEqual(sent, ["mm-wavebro:dm:glin", "mm-wavebro:nest"])

    def test_star_alone_matches_everything(self):
        sent, responses = self._capture("reset")
        self._start_daemon(responses)
        _, _, code = self._run(["reset", "*"])
        self.assertEqual(code, 0)
        self.assertEqual(sent, sorted(r["watcher_name"] for r in _ROWS))

    def test_connector_prefix_glob_spans_rooms_and_dms(self):
        sent, responses = self._capture("resume")
        self._start_daemon(responses)
        self._run(["resume", "mm-*"])
        self.assertEqual(sent, ["mm-wavebro:dm:glin", "mm-wavebro:nest"])

    def test_room_suffix_glob_spans_connectors(self):
        sent, responses = self._capture("expire")
        self._start_daemon(responses)
        self._run(["expire", "*:nest"])
        self.assertEqual(sent, ["mm-wavebro:nest", "rc-eng:nest"])

    def test_matching_is_case_sensitive(self):
        """Names are case-preserving, so matching is case-sensitive. Pins the
        contract; note it cannot tell `fnmatchcase` from `fnmatch` on POSIX,
        where `os.path.normcase` is the identity — the distinction only bites
        on Windows, which is why the code says `fnmatchcase` explicitly."""
        rows = [{"watcher_name": "rc-Eng:nest"}, {"watcher_name": "rc-eng:nest"}]
        sent: list[str] = []

        def _pause(req):
            sent.append(req["watcher_name"])
            return {"ok": True}

        self._start_daemon({"list": _listing(rows), "pause": _pause})
        self._run(["pause", "rc-e*"])
        self.assertEqual(sent, ["rc-eng:nest"])

    def test_literal_name_is_the_unchanged_single_path(self):
        """No metacharacter → no `list` round-trip, the historical success
        line, and the historical exit-1 on refusal. The batch summary does
        not appear."""
        received: list[dict] = []

        def _pause(req):
            received.append(req)
            return {"ok": True}

        def _list(req):
            raise AssertionError("a literal name must not list")

        self._start_daemon({"list": _list, "pause": _pause})
        stdout, _, code = self._run(["pause", "mm-wavebro:nest"])
        self.assertEqual(code, 0)
        self.assertEqual(received, [{"cmd": "pause", "watcher_name": "mm-wavebro:nest"}])
        self.assertIn("Watcher 'mm-wavebro:nest' paused", stdout)
        self.assertNotIn("For ", stdout)

    # ── item 2: one line before, one after, per watcher ──────────────────────

    def test_prints_a_line_before_and_after_each_watcher(self):
        _, responses = self._capture("resume")
        self._start_daemon(responses)
        stdout, _, _ = self._run(["resume", "rc-eng:*"])
        lines = [line for line in stdout.splitlines() if "rc-eng" in line]
        self.assertEqual(lines, [
            "Resuming watcher 'rc-eng:general'…",
            "Done resuming watcher 'rc-eng:general'",
            "Resuming watcher 'rc-eng:nest'…",
            "Done resuming watcher 'rc-eng:nest'",
        ])

    def test_participle_per_verb(self):
        for verb, word in (("pause", "pausing"), ("reset", "resetting"),
                           ("expire", "expiring")):
            with self.subTest(verb=verb):
                self._start_daemon({"list": _listing([{"watcher_name": "rc-eng:nest"}]),
                                    verb: {"ok": True}})
                stdout, _, _ = self._run([verb, "rc-*"])
                self.assertIn(f"{word.capitalize()} watcher 'rc-eng:nest'…", stdout)
                self.assertIn(f"Done {word} watcher 'rc-eng:nest'", stdout)
                self._daemon.stop()
                self.sock_path.unlink(missing_ok=True)

    # ── a no-op state is skipped before anything is sent ─────────────────────

    def test_resume_skips_watchers_that_are_not_paused_without_asking_the_daemon(self):
        """Owner on #151: `resume` over a glob is "bring the paused ones back";
        an active or idle match is reported as not paused, skipped, and
        counted as succeeded — and the daemon is never asked, so a resume of
        '*' cannot wake every idle room as a side effect."""
        rows = (_rows(["rc-eng:nest"], state="paused")
                + _rows(["rc-eng:general"], state="active")
                + _rows(["rc-eng:archive"], state="idle"))
        sent, responses = self._capture("resume", rows=rows)
        self._start_daemon(responses)
        stdout, stderr, code = self._run(["resume", "rc-eng:*"])

        self.assertEqual(code, 0, stderr)
        self.assertEqual(sent, ["rc-eng:nest"])
        self.assertIn("Watcher 'rc-eng:archive' is not paused — skipped", stdout)
        self.assertIn("Watcher 'rc-eng:general' is not paused — skipped", stdout)
        self.assertNotIn("Resuming watcher 'rc-eng:general'", stdout)
        self.assertIn("For 3 watchers: 3 succeeded, 0 failed, 0 not run.", stdout)

    def test_pause_skips_watchers_that_are_already_paused(self):
        """The mirror of the resume skip: pausing a paused watcher is a no-op
        the daemon would answer ok to anyway; the batch line says so."""
        rows = _rows(["rc-eng:nest"], state="paused") + _rows(["rc-eng:general"])
        sent, responses = self._capture("pause", rows=rows)
        self._start_daemon(responses)
        stdout, _, code = self._run(["pause", "rc-eng:*"])
        self.assertEqual(code, 0)
        self.assertEqual(sent, ["rc-eng:general"])
        self.assertIn("Watcher 'rc-eng:nest' is already paused — skipped", stdout)
        self.assertIn("For 2 watchers: 2 succeeded", stdout)

    def test_reset_and_expire_act_regardless_of_state(self):
        """Only pause/resume have a state that makes them a no-op; reset and
        expire are sent to every match, paused or idle included."""
        rows = _rows(["rc-eng:nest"], state="paused") + _rows(["rc-eng:general"], state="idle")
        for verb in ("reset", "expire"):
            with self.subTest(verb=verb):
                sent, responses = self._capture(verb, rows=rows)
                self._start_daemon(responses)
                self._run([verb, "rc-eng:*"])
                self.assertEqual(sent, ["rc-eng:general", "rc-eng:nest"])
                self._daemon.stop()
                self.sock_path.unlink(missing_ok=True)

    def test_state_skip_does_not_short_circuit_a_literal_name(self):
        """A literal `resume x` on an active watcher still goes to the daemon
        (whose own answer is the idempotent ok) — the skip is a batch-only
        courtesy, the single path is unchanged."""
        received: list[dict] = []

        def _resume(req):
            received.append(req)
            return {"ok": True}

        self._start_daemon({"resume": _resume})
        stdout, _, code = self._run(["resume", "rc-eng:nest"])
        self.assertEqual(code, 0)
        self.assertEqual(len(received), 1)
        self.assertIn("Watcher 'rc-eng:nest' resumed", stdout)

    # ── item 3: gone since the match set was collected → skip, not error ─────

    def test_watcher_gone_mid_run_is_skipped_and_counted_as_success(self):
        def _outcome(name):
            if name == "rc-eng:general":
                return {"ok": False, "code": "unknown_watcher",
                        "error": "Unknown watcher: 'rc-eng:general'"}
            return {"ok": True}

        sent, responses = self._capture("expire", _outcome)
        self._start_daemon(responses)
        stdout, stderr, code = self._run(["expire", "rc-eng:*"])

        self.assertEqual(code, 0, stderr)
        self.assertIn("Watcher 'rc-eng:general' is no longer there — skipped", stdout)
        self.assertEqual(stderr, "")
        # The run did not stop there.
        self.assertEqual(sent, ["rc-eng:general", "rc-eng:nest"])
        self.assertIn("For 2 watchers: 2 succeeded, 0 failed, 0 not run.", stdout)

    def test_skip_keys_on_the_code_not_the_text(self):
        """A refusal whose text happens to mention 'unknown' is still a
        failure: only the `code` field means "gone"."""
        def _outcome(name):
            return {"ok": False, "error": "Unknown room kind for this watcher"}

        _, responses = self._capture("pause", _outcome)
        self._start_daemon(responses)
        _, stderr, code = self._run(["pause", "rc-eng:nest*"])
        self.assertEqual(code, 1)
        self.assertIn("failed", stderr)

    # ── items 4-6: abort by default, --force continues, summary arithmetic ───

    def test_error_aborts_by_default_and_later_watchers_are_not_run(self):
        def _outcome(name):
            if name == "mm-wavebro:nest":
                return {"ok": False, "error": "connector degraded"}
            return {"ok": True}

        sent, responses = self._capture("reset", _outcome)
        self._start_daemon(responses)
        stdout, stderr, code = self._run(["reset", "*"])

        self.assertEqual(code, 1)
        # Sorted order: dm:glin ok, nest fails, the two rc-eng never sent.
        self.assertEqual(sent, ["mm-wavebro:dm:glin", "mm-wavebro:nest"])
        self.assertIn("[ERROR] Resetting watcher 'mm-wavebro:nest' failed: connector degraded",
                      stderr)
        self.assertIn("--force", stderr)
        self.assertIn("For 4 watchers: 1 succeeded, 1 failed, 2 not run.", stdout)

    def test_force_continues_past_failures_and_still_exits_nonzero(self):
        def _outcome(name):
            if name.endswith(":nest"):
                return {"ok": False, "error": "connector degraded"}
            return {"ok": True}

        sent, responses = self._capture("reset", _outcome)
        self._start_daemon(responses)
        stdout, stderr, code = self._run(["reset", "*", "--force"])

        self.assertEqual(code, 1)
        self.assertEqual(len(sent), 4)
        self.assertEqual(stderr.count("[ERROR]"), 2)
        self.assertNotIn("Aborted", stderr)
        self.assertIn("For 4 watchers: 2 succeeded, 2 failed, 0 not run.", stdout)

    def test_force_with_no_failures_exits_zero(self):
        _, responses = self._capture("pause")
        self._start_daemon(responses)
        stdout, _, code = self._run(["pause", "*", "--force"])
        self.assertEqual(code, 0)
        self.assertIn("For 4 watchers: 4 succeeded, 0 failed, 0 not run.", stdout)

    def test_gone_watcher_and_real_failure_are_counted_apart(self):
        def _outcome(name):
            if name == "mm-wavebro:dm:glin":
                return {"ok": False, "code": "unknown_watcher", "error": "Unknown watcher"}
            if name == "rc-eng:general":
                return {"ok": False, "error": "refused"}
            return {"ok": True}

        _, responses = self._capture("expire", _outcome)
        self._start_daemon(responses)
        stdout, _, code = self._run(["expire", "*", "--force"])
        self.assertEqual(code, 1)
        self.assertIn("For 4 watchers: 3 succeeded, 1 failed, 0 not run.", stdout)

    # ── item 7: zero matches is just a summary of zeros ──────────────────────

    def test_zero_matches_prints_the_zero_summary_and_exits_zero(self):
        sent, responses = self._capture("reset")
        self._start_daemon(responses)
        stdout, stderr, code = self._run(["reset", "slack-*"])
        self.assertEqual(code, 0)
        self.assertEqual(sent, [])
        self.assertEqual(stderr, "")
        self.assertIn("For 0 watchers: 0 succeeded, 0 failed, 0 not run.", stdout)

    # ── transport failure mid-run: one watcher's failure, summary still owed ─

    def test_transport_failure_mid_run_aborts_with_the_summary(self):
        """`_send_command` exits the process when the daemon does not answer.
        Inside a batch that must become the current watcher's failure — outcome
        unknown — and the summary must still print (Codex on #152)."""
        from gateway import cli as cli_mod
        real = cli_mod._send_command

        def _flaky(request, timeout=60.0):
            if request.get("watcher_name") == "mm-wavebro:nest":
                print("[ERROR] No response from the daemon within 300s", file=sys.stderr)
                raise SystemExit(2)
            return real(request, timeout=timeout)

        sent, responses = self._capture("reset")
        self._start_daemon(responses)
        with patch("gateway.cli._send_command", side_effect=_flaky):
            stdout, stderr, code = self._run(["reset", "*"])

        self.assertEqual(code, 1)
        self.assertEqual(sent, ["mm-wavebro:dm:glin"])  # nest never reached the daemon
        self.assertIn("outcome is unknown", stderr)
        self.assertIn("For 4 watchers: 1 succeeded, 1 failed, 2 not run.", stdout)

    def test_transport_failure_mid_run_with_force_tries_the_rest(self):
        from gateway import cli as cli_mod
        real = cli_mod._send_command

        def _flaky(request, timeout=60.0):
            if request.get("watcher_name") == "mm-wavebro:nest":
                raise SystemExit(2)
            return real(request, timeout=timeout)

        sent, responses = self._capture("reset")
        self._start_daemon(responses)
        with patch("gateway.cli._send_command", side_effect=_flaky):
            stdout, stderr, code = self._run(["reset", "*", "--force"])

        self.assertEqual(code, 1)
        self.assertEqual(sent, ["mm-wavebro:dm:glin", "rc-eng:general", "rc-eng:nest"])
        self.assertIn("For 4 watchers: 3 succeeded, 1 failed, 0 not run.", stdout)

    # ── the list itself failing: nothing is touched ──────────────────────────

    def test_partial_listing_aborts_before_touching_anything(self):
        """A connector that failed to list leaves the match set incomplete.
        Acting on what the others returned is not what was asked for."""
        sent, responses = self._capture("reset")
        responses["list"] = {
            "ok": False, "data": [{"watcher_name": "rc-eng:nest"}],
            "errors": [{"connector": "mm-wavebro", "error": "connection refused"}],
        }
        self._start_daemon(responses)
        stdout, stderr, code = self._run(["reset", "*"])
        self.assertEqual(code, 1)
        self.assertEqual(sent, [])
        self.assertIn("mm-wavebro", stderr)
        self.assertIn("nothing was done", stderr)
        self.assertNotIn("For ", stdout)

    def test_hard_list_failure_aborts(self):
        sent, responses = self._capture("pause")
        responses["list"] = {"ok": False, "error": "Cannot list: reload in progress"}
        self._start_daemon(responses)
        _, stderr, code = self._run(["pause", "*"])
        self.assertEqual(code, 1)
        self.assertEqual(sent, [])
        self.assertIn("reload in progress", stderr)

    def test_reset_keeps_its_long_timeout_per_watcher(self):
        """reset's 300 s wait is per command, so a glob reset of N watchers
        waits up to 300 s for EACH — not 300 s total, not 60 s each."""
        seen: list[float] = []
        from gateway import cli as cli_mod
        real = cli_mod._send_command

        def _spy(request, timeout=60.0):
            if request["cmd"] == "reset":
                seen.append(timeout)
            return real(request, timeout=timeout)

        _, responses = self._capture("reset")
        self._start_daemon(responses)
        with patch("gateway.cli._send_command", side_effect=_spy):
            self._run(["reset", "rc-eng:*"])
        self.assertEqual(seen, [300.0, 300.0])


# ---------------------------------------------------------------------------
# Tests: pause / resume / reset commands
# ---------------------------------------------------------------------------

class TestCLIPauseResumeReset(_CLITestBase):
    """pause, resume, reset: success and failure paths."""

    def test_pause_normal_path(self):
        """Successful pause → print confirmation + exit 0."""
        self._start_daemon({"pause": {"ok": True}})
        stdout, _, code = self._run(["pause", "support"])
        self.assertEqual(code, 0)
        self.assertIn("paused", stdout.lower())

    def test_pause_failure_exits_1(self):
        """Failed pause → stderr error + exit 1."""
        self._start_daemon({"pause": {"ok": False, "error": "watcher not found"}})
        _, stderr, code = self._run(["pause", "nonexistent"])
        self.assertEqual(code, 1)
        self.assertIn("watcher not found", stderr)

    def test_expire_normal_path(self):
        """Successful expire → print confirmation + exit 0 (§2.8)."""
        self._start_daemon({"expire": {"ok": True}})
        stdout, _, code = self._run(["expire", "rc-eng"])
        self.assertEqual(code, 0)
        self.assertIn("expired", stdout.lower())

    def test_expire_does_not_claim_to_have_reclaimed_the_jobs(self):
        """The success line said "record, session and scheduled jobs reclaimed"
        after the jobs stopped being cancelled — contradicting its own `--help`,
        which was corrected in the same commit that claimed to have swept every
        operator-facing mention. An operator who believes this line stops looking
        for the job that is about to recreate the watcher."""
        self._start_daemon({"expire": {"ok": True}})
        stdout, _, code = self._run(["expire", "rc-eng"])
        self.assertEqual(code, 0)
        self.assertNotIn("scheduled jobs reclaimed", stdout)
        self.assertIn("scheduled jobs are kept", stdout)

    def test_expire_failure_exits_1(self):
        self._start_daemon({"expire": {"ok": False, "error": "no expirable record"}})
        _, stderr, code = self._run(["expire", "ghost"])
        self.assertEqual(code, 1)
        self.assertIn("no expirable record", stderr)

    def test_resume_normal_path(self):
        """Successful resume → print confirmation + exit 0."""
        self._start_daemon({"resume": {"ok": True}})
        stdout, _, code = self._run(["resume", "support"])
        self.assertEqual(code, 0)
        self.assertIn("resumed", stdout.lower())

    def test_resume_failure_exits_1(self):
        """Failed resume → stderr error + exit 1."""
        self._start_daemon({"resume": {"ok": False, "error": "not paused"}})
        _, stderr, code = self._run(["resume", "support"])
        self.assertEqual(code, 1)

    def test_reset_normal_path(self):
        """Successful reset → print confirmation + exit 0."""
        self._start_daemon({"reset": {"ok": True}})
        stdout, _, code = self._run(["reset", "support"])
        self.assertEqual(code, 0)
        self.assertIn("reset", stdout.lower())

    def test_pause_watcher_name_forwarded(self):
        """watcher_name is forwarded correctly in the socket payload."""
        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {"ok": True}

        self._start_daemon({"pause": _capture})
        self._run(["pause", "my-watcher"])
        self.assertEqual(received[0]["watcher_name"], "my-watcher")



# ---------------------------------------------------------------------------
# Tests: send command
# ---------------------------------------------------------------------------

class TestCLISend(_CLITestBase):
    """send: inline text, --file, validation errors."""

    def test_send_inline_text_normal_path(self):
        """Inline text message dispatched, 'Sent.' printed on success."""
        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {"ok": True}

        self._start_daemon({"send": _capture})
        stdout, _, code = self._run(["send", "general", "Hello", "world"])

        self.assertEqual(code, 0)
        self.assertIn("Sent.", stdout)
        self.assertEqual(received[0]["text"], "Hello world")
        self.assertEqual(received[0]["room"], "general")

    def test_send_from_file(self):
        """--file reads text from file and sends it."""
        msg_file = Path(self.tmp) / "msg.txt"
        msg_file.write_text("Message from file")

        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {"ok": True}

        self._start_daemon({"send": _capture})
        stdout, _, code = self._run(["send", "general", "--file", str(msg_file)])

        self.assertEqual(code, 0)
        self.assertEqual(received[0]["text"], "Message from file")

    def test_send_file_not_found_exits_1(self):
        """Missing --file → error message + exit 1 (no socket call)."""
        _, stderr, code = self._run(["send", "general", "--file", "/no/such/file.txt"])
        self.assertEqual(code, 1)
        self.assertIn("not found", stderr)

    def test_send_attach_not_found_exits_1(self):
        """Missing --attach → error + exit 1."""
        _, stderr, code = self._run(["send", "general", "hi", "--attach", "/no/file.png"])
        self.assertEqual(code, 1)
        self.assertIn("not found", stderr)

    def test_send_no_message_no_file_no_attach_exits_1(self):
        """Nothing to send → validation error + exit 1."""
        _, stderr, code = self._run(["send", "general"])
        self.assertEqual(code, 1)
        self.assertIn("provide a message", stderr)

    def test_send_inline_and_file_mutual_exclusion(self):
        """Inline text + --file together → error + exit 1."""
        msg_file = Path(self.tmp) / "m.txt"
        msg_file.write_text("x")
        _, stderr, code = self._run(
            ["send", "general", "hello", "--file", str(msg_file)]
        )
        self.assertEqual(code, 1)
        self.assertIn("cannot use both", stderr)

    def test_send_failure_exits_1(self):
        """Daemon returns error → stderr message + exit 1."""
        self._start_daemon({"send": {"ok": False, "error": "room not found"}})
        _, stderr, code = self._run(["send", "unknown-room", "hi"])
        self.assertEqual(code, 1)
        self.assertIn("room not found", stderr)

    def test_send_with_attachment_path_resolved(self):
        """--attach path is resolved to absolute before sending."""
        attach_file = Path(self.tmp) / "img.png"
        attach_file.write_bytes(b"\x89PNG")

        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {"ok": True}

        self._start_daemon({"send": _capture})
        self._run(["send", "general", "caption", "--attach", str(attach_file)])

        self.assertIn("attachment_path", received[0])
        self.assertTrue(Path(received[0]["attachment_path"]).is_absolute())


# ---------------------------------------------------------------------------
# Tests: daemon-not-running path
# ---------------------------------------------------------------------------

class TestCLIScheduleMigrateReporting(_CLITestBase):
    """`schedule migrate`'s output IS its product — the whole reason the
    migration is a command rather than something done invisibly at fire time.
    It had no test, which is how it came to report a migration that did not run.
    """

    _OUTCOME_OK = {"job_id": "acg-1", "watcher": "rc:general",
                   "changed": True, "detail": "room room-1 (resolved 'general')",
                   "needs_attention": False}
    _OUTCOME_STUCK = {"job_id": "acg-2", "watcher": "rc:gone",
                      "changed": False, "detail": "there is no room named 'gone'",
                      "needs_attention": True}

    def _migrate(self, **report) -> tuple[str, str, int]:
        self._start_daemon({"schedule-migrate": {"ok": True, **report}})
        return self._run(["schedule", "migrate"])

    def test_a_run_held_back_by_an_unresolved_job_does_not_claim_to_have_migrated(self):
        """The version does not move while any job needs attention, so saying
        "migrated 1 → 2" here is contradicted by the next startup warning. The
        report carries `stamped` for exactly this: `to_version` is the target,
        not the outcome."""
        stdout, _, code = self._migrate(
            from_version=1, to_version=2, stamped=False, changed=1,
            steps=["1 → 2: record each job's room id"],
            outcomes=[self._OUTCOME_OK, self._OUTCOME_STUCK])

        self.assertEqual(code, 0)
        self.assertNotIn("migrated 1 → 2", stdout)
        self.assertIn("STILL at schema version 1", stdout)
        # And it says what to do next, since the command is worth re-running.
        self.assertIn("run 'schedule migrate' again", stdout)
        self.assertIn("1 job(s) need attention", stdout)

    def test_a_clean_run_reports_the_version_it_reached(self):
        stdout, _, code = self._migrate(
            from_version=1, to_version=2, stamped=True, changed=1,
            steps=["1 → 2: record each job's room id"],
            outcomes=[self._OUTCOME_OK])

        self.assertEqual(code, 0)
        self.assertIn("migrated 1 → 2", stdout)
        self.assertNotIn("STILL", stdout)
        self.assertNotIn("need attention", stdout)

    def test_a_current_version_that_still_owed_work_shows_the_work(self):
        """`needs_migration` also looks at the jobs, so a version-2 file with a
        live job lacking a room id re-runs the 1→2 step at version 2. The CLI
        keyed "nothing to do" on the versions matching and hid that run — steps,
        outcomes, jobs needing attention — while the startup warning kept
        firing (Codex, PR #140 round 2)."""
        stdout, _, code = self._migrate(
            from_version=2, to_version=2, stamped=False, changed=0,
            steps=["1 → 2: record each job's room id"],
            outcomes=[self._OUTCOME_STUCK])

        self.assertEqual(code, 0)
        self.assertNotIn("nothing to do", stdout)
        self.assertIn("1 → 2", stdout)
        self.assertIn("1 job(s) need attention", stdout)
        self.assertIn("STILL at schema version 2", stdout)

    def test_an_already_current_file_says_so_without_a_job_list(self):
        stdout, _, code = self._migrate(
            from_version=2, to_version=2, stamped=True, changed=0, outcomes=[])

        self.assertEqual(code, 0)
        self.assertIn("already at schema version 2", stdout)

    def test_a_newer_file_is_an_error_not_a_downgrade(self):
        """`migrate` refuses rather than writing the file down to this version;
        the CLI has to surface that as a failure, not a quiet success."""
        self._start_daemon({"schedule-migrate": {
            "ok": False,
            "error": "jobs.json declares schema version 3, but this AgentCoop "
                     "understands 2. It was written by a newer version — "
                     "upgrade AgentCoop rather than migrating down."}})
        stdout, stderr, code = self._run(["schedule", "migrate"])

        self.assertEqual(code, 1)
        self.assertIn("newer version", stderr)
        self.assertNotIn("migrated", stdout)

    def test_the_marks_distinguish_changed_from_already_fine_from_stuck(self):
        """Three states, three marks. Collapsing "already had a room id" into
        the attention list would hold the schema version back forever, because a
        clean re-run reports every job as unchanged."""
        already = {"job_id": "acg-3", "watcher": "rc:ops", "changed": False,
                   "detail": "already has a room id", "needs_attention": False}
        stdout, _, _ = self._migrate(
            from_version=1, to_version=2, stamped=False, changed=1,
            outcomes=[self._OUTCOME_OK, already, self._OUTCOME_STUCK])

        self.assertIn("✓ acg-1", stdout)
        self.assertIn("· acg-3", stdout)
        self.assertIn("✗ acg-2", stdout)
        self.assertIn("1 job(s) need attention", stdout)


class TestCLIDaemonNotRunning(unittest.TestCase):
    """Commands that require the daemon print an error when it's not running."""

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

    def test_list_when_not_running(self):
        _, stderr, code = self._run_no_daemon(["list"])
        self.assertEqual(code, 1)
        self.assertIn("not running", stderr)

    def test_pause_when_not_running(self):
        _, stderr, code = self._run_no_daemon(["pause", "foo"])
        self.assertEqual(code, 1)
        self.assertIn("not running", stderr)

    def test_send_when_not_running(self):
        _, stderr, code = self._run_no_daemon(["send", "general", "hello"])
        self.assertEqual(code, 1)
        self.assertIn("not running", stderr)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Tests: config reload / config show / config validate --json / status digest (#144)
# ---------------------------------------------------------------------------

class _ConfigCLIBase(_CLITestBase):
    """The reload-family commands need a real config file and an isolated state
    dir, and — unlike the rest of this module — a way to say the daemon is NOT
    running, because the offline plan is a distinct code path."""

    def setUp(self):
        super().setUp()
        self.agent_dir = Path(self.tmp) / "work"
        self.agent_dir.mkdir()
        self.runtime_dir = Path(self.tmp) / "runtime"
        self.runtime_dir.mkdir()
        self.cfg_path = str(Path(self.tmp) / "config.yaml")
        self._write_config()

    def _write_config(self, rules: str = "") -> None:
        # Local on purpose: `tests.helpers.gateway_config_text` writes script
        # connectors only, and these tests need a rocketchat one — its `server`
        # block carries the password `config show` must redact and the URL the
        # validator checks.
        Path(self.cfg_path).write_text(textwrap.dedent(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: hunter2}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """) + rules)

    def _run_with(self, args: list[str], *, running: bool) -> tuple[str, str, int]:
        main = _import_main()
        stdout_buf, stderr_buf, exit_code = io.StringIO(), io.StringIO(), 0
        with (
            patch("sys.argv", ["acg"] + args),
            patch("gateway.cli.CONTROL_SOCK", self.sock_path),
            patch("gateway.daemon.is_running", return_value=(running, 99999 if running else None)),
            patch("gateway.daemon.PID_FILE", self.pid_file),
            patch("gateway.daemon.LOG_FILE", self.log_file),
            patch("gateway.core.state.RUNTIME_DIR", self.runtime_dir),
        ):
            try:
                with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
                    main()
            except SystemExit as exc:
                exit_code = exc.code if isinstance(exc.code, int) else 1
        return stdout_buf.getvalue(), stderr_buf.getvalue(), exit_code


class TestCLIConfigValidateJson(_ConfigCLIBase):

    def test_json_document_carries_ok_and_findings(self):
        stdout, _, code = self._run_with(
            ["config", "validate", "--config", self.cfg_path, "--json"], running=False)
        self.assertEqual(code, 0)
        doc = json.loads(stdout)
        self.assertTrue(doc["ok"])
        self.assertEqual(doc["watcher_count"], 1)
        self.assertEqual(doc["findings"], [])

    def test_json_findings_map_the_finding_fields_and_exit_one(self):
        Path(self.cfg_path).write_text(Path(self.cfg_path).read_text().replace(
            "agent: default", "agent: nobody"))
        stdout, _, code = self._run_with(
            ["config", "validate", "--config", self.cfg_path, "--json"], running=False)
        self.assertEqual(code, 1)
        doc = json.loads(stdout)
        self.assertFalse(doc["ok"])
        self.assertEqual(set(doc["findings"][0]),
                         {"level", "entity_kind", "entity_name", "field", "message"})
        self.assertEqual(doc["findings"][0]["level"], "error")


class TestCLILargeResponses(_ConfigCLIBase):

    def test_a_response_beyond_64_kib_is_read_whole(self):
        """asyncio's default StreamReader limit is 64 KiB and `readline` raises
        past it; a reload plan or a `list` for a few hundred rooms is bigger."""
        rows = [{"watcher_name": f"rc:room-{i}", "room_name": f"room-{i}", "room_id": f"r{i}",
                 "connector": "rc", "agent_name": "a", "session_id": "s" * 40,
                 "participants": [], "state": "active"} for i in range(600)]
        self.assertGreater(len(json.dumps({"ok": True, "data": rows})), 64 * 1024)
        self._start_daemon({"list": {"ok": True, "data": rows, "errors": []}})
        stdout, stderr, code = self._run_with(["list"], running=True)
        self.assertEqual(code, 0, stderr)
        self.assertIn("rc:room-599", stdout)


class TestCLIReadTimeout(_ConfigCLIBase):

    def test_no_response_in_time_is_exit_two_not_unreachable(self):
        """The daemon took the request and may still be applying it — neither
        "nothing changed" (1) nor done (0)."""
        def _slow(req):
            time.sleep(1.0)
            return {"ok": True}

        self._start_daemon({"config-reload": _slow})
        from gateway.cli import _send_command
        stderr_buf = io.StringIO()
        with (
            patch("gateway.cli.CONTROL_SOCK", self.sock_path),
            patch("gateway.daemon.is_running", return_value=(True, 99999)),
            redirect_stderr(stderr_buf),
        ):
            with self.assertRaises(SystemExit) as cm:
                _send_command({"cmd": "config-reload"}, timeout=0.2)
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("No response from the daemon", stderr_buf.getvalue())
        self.assertIn("may still be working", stderr_buf.getvalue())


class TestCLIConfigReload(_ConfigCLIBase):

    _PLAN = {
        "ok": True, "dry_run": True, "offline": False, "applied": False, "exit_code": 0,
        "error": "", "digest": "d" * 64, "validation": {"findings": []},
        "changes": {"connectors": {"added": [], "changed": [], "removed": []},
                    "agents": {"added": [], "changed": [], "removed": []},
                    "rules": {"added": [], "changed": ["w1"], "removed": [], "reordered": False},
                    "values": []},
        "watchers": [{"connector": "rc", "room_id": "r1", "handle": "rc:general",
                      "agent": "default", "action": "rematerialize", "from_rule": "w1",
                      "to_rule": "w1", "session_id": "", "reason": ""}],
        "notes": [], "degraded": [],
    }

    def test_running_daemon_receives_dry_run_and_the_absolute_path(self):
        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return dict(self._PLAN)

        self._start_daemon({"config-reload": _capture})
        stdout, _, code = self._run_with(
            ["config", "reload", "--config", self.cfg_path, "--dry-run"], running=True)
        self.assertEqual(code, 0)
        self.assertEqual(received[0]["dry_run"], True)
        import os
        self.assertEqual(received[0]["config_path"], os.path.abspath(self.cfg_path),
                         "absolute, not resolved — the daemon compares it the same way")
        self.assertIn("rules: ~ w1 (changed)", stdout)
        self.assertIn("rematerialize w1 → w1", stdout)
        self.assertIn("Dry run", stdout)

    def test_exit_code_comes_from_the_plan(self):
        degraded = dict(self._PLAN, dry_run=False, applied=True, exit_code=2,
                        degraded=[{"kind": "connector", "name": "rc", "error": "refused"}])
        self._start_daemon({"config-reload": degraded})
        stdout, _, code = self._run_with(
            ["config", "reload", "--config", self.cfg_path], running=True)
        self.assertEqual(code, 2)
        self.assertIn("[ERROR] connector 'rc': refused", stdout)

    def test_json_output_is_the_daemons_document(self):
        self._start_daemon({"config-reload": dict(self._PLAN)})
        stdout, _, code = self._run_with(
            ["config", "reload", "--config", self.cfg_path, "--dry-run", "--json"], running=True)
        self.assertEqual(code, 0)
        doc = json.loads(stdout)
        self.assertEqual(doc["watchers"][0]["action"], "rematerialize")
        self.assertEqual(doc["exit_code"], 0)

    def test_a_control_server_refusal_is_an_error(self):
        self._start_daemon({"config-reload": {"ok": False, "error": "nope"}})
        _, stderr, code = self._run_with(
            ["config", "reload", "--config", self.cfg_path], running=True)
        self.assertEqual(code, 1)
        self.assertIn("nope", stderr)

    def test_a_control_server_refusal_is_still_a_document_under_json(self):
        self._start_daemon({"config-reload": {"ok": False, "error": "nope"}})
        stdout, _, code = self._run_with(
            ["config", "reload", "--config", self.cfg_path, "--json"], running=True)
        self.assertEqual(code, 1)
        doc = json.loads(stdout)
        self.assertEqual((doc["ok"], doc["error"], doc["exit_code"]), (False, "nope", 1))

    def test_offline_dry_run_is_the_next_starts_plan(self):
        from gateway.core.state import save_state
        from gateway.core.watcher_manager import RoomRef
        from gateway.core.watcher_rule import RoomKind
        from tests.helpers import make_record_from_rule, make_rule

        gone = make_record_from_rule(
            make_rule(room="old", name="old-rule", connector="rc", agent="default"),
            RoomRef(id="r-old", kind=RoomKind.CHANNEL, name="old"), session_id="sess-old-1")
        with patch("gateway.core.state.RUNTIME_DIR", self.runtime_dir):
            save_state("rc", [gone])

        stdout, stderr, code = self._run_with(
            ["config", "reload", "--config", self.cfg_path, "--dry-run"], running=False)

        self.assertEqual(code, 0, stderr)
        self.assertIn("expire no-rule-matches", stdout)
        self.assertIn("sess-old-1", stdout)
        self.assertIn("next start", stdout)

    def test_offline_execute_prints_the_plan_and_refuses(self):
        stdout, stderr, code = self._run_with(
            ["config", "reload", "--config", self.cfg_path], running=False)
        self.assertEqual(code, 1)
        self.assertIn("not running", stderr)
        self.assertIn("coop start", stderr)

    def test_offline_dry_run_keeps_the_validation_warnings(self):
        self._write_config(rules=(  # w2 is shadowed by w1 — a warning, not an error
            "  - name: w2\n    connector: rc\n    agent: default\n"
            "    rooms:\n      include: [general]\n"))
        stdout, stderr, code = self._run_with(
            ["config", "reload", "--config", self.cfg_path, "--dry-run", "--json"], running=False)
        self.assertEqual(code, 0, stderr)
        doc = json.loads(stdout)
        levels = [f["level"] for f in doc["validation"]["findings"]]
        self.assertIn("warning", levels, doc["validation"])

    def test_offline_execute_in_json_is_a_refusal_carrying_the_plan(self):
        stdout, _, code = self._run_with(
            ["config", "reload", "--config", self.cfg_path, "--json"], running=False)
        self.assertEqual(code, 1)
        doc = json.loads(stdout)
        self.assertFalse(doc["ok"])
        self.assertFalse(doc["dry_run"], "no --dry-run was asked for")
        self.assertTrue(doc["offline"])
        self.assertIn("not running", doc["error"])

    def test_running_daemon_with_no_socket_is_an_error_not_an_offline_plan(self):
        # No mock daemon started: the socket path does not exist.
        stdout, stderr, code = self._run_with(
            ["config", "reload", "--config", self.cfg_path, "--dry-run"], running=True)
        self.assertEqual(code, 1)
        self.assertIn("Control socket not found", stderr)
        self.assertNotIn("next start", stdout)

    def test_an_invalid_file_is_refused_offline_too(self):
        Path(self.cfg_path).write_text(Path(self.cfg_path).read_text().replace(
            "agent: default", "agent: nobody"))
        stdout, _, code = self._run_with(
            ["config", "reload", "--config", self.cfg_path, "--dry-run"], running=False)
        self.assertEqual(code, 1)
        self.assertIn("nobody", stdout)


class TestCLIConfigShow(_ConfigCLIBase):

    def test_prints_digest_and_redacted_flattened_config(self):
        stdout, _, code = self._run_with(
            ["config", "show", "--config", self.cfg_path], running=False)
        self.assertEqual(code, 0)
        self.assertRegex(stdout, r"Digest:  [0-9a-f]{64}")
        self.assertIn("connectors.rc.raw.server.password: ***", stdout)
        self.assertNotIn("hunter2", stdout)
        self.assertNotIn("Active:", stdout, "no daemon, no active digest")

    def test_warns_when_the_running_daemon_differs(self):
        self._start_daemon({"config-show": {
            "ok": True, "digest": "0" * 64, "loaded_at": "2026-09-04T00:00:00-07:00",
            "config_path": self.cfg_path, "degraded": [], "reloading": False}})
        stdout, _, code = self._run_with(
            ["config", "show", "--config", self.cfg_path], running=True)
        self.assertEqual(code, 0)
        self.assertIn("Active:  " + "0" * 64, stdout)
        self.assertIn("differs from the file", stdout)

    def test_a_daemon_that_refuses_config_show_is_an_error_not_an_offline_show(self):
        self._start_daemon({"config-show": {"ok": False, "error": "Unknown command: config-show"}})
        stdout, stderr, code = self._run_with(
            ["config", "show", "--config", self.cfg_path], running=True)
        self.assertEqual(code, 1)
        self.assertIn("[ERROR] the daemon refused config-show", stderr)
        stdout, _, code = self._run_with(
            ["config", "show", "--config", self.cfg_path, "--json"], running=True)
        self.assertEqual(code, 1)
        doc = json.loads(stdout)
        self.assertFalse(doc["ok"])
        self.assertIn("refused", doc["error"])

    def test_config_show_json_reports_an_invalid_file_as_a_document(self):
        Path(self.cfg_path).write_text(Path(self.cfg_path).read_text().replace(
            "agent: default", "agent: nobody"))
        stdout, _, code = self._run_with(
            ["config", "show", "--config", self.cfg_path, "--json"], running=False)
        self.assertEqual(code, 1)
        doc = json.loads(stdout)
        self.assertFalse(doc["ok"])
        self.assertTrue(any("nobody" in f["message"] for f in doc["findings"]))

    def test_json_carries_digest_in_sync_and_redacted_config(self):
        self._start_daemon({"config-show": {
            "ok": True, "digest": "0" * 64, "loaded_at": "t", "config_path": self.cfg_path,
            "degraded": [], "reloading": False}})
        stdout, _, code = self._run_with(
            ["config", "show", "--config", self.cfg_path, "--json"], running=True)
        doc = json.loads(stdout)
        self.assertEqual(len(doc["digest"]), 64)
        self.assertFalse(doc["in_sync"])
        self.assertEqual(doc["config"]["connectors"][0]["raw"]["server"]["password"], "***")


class TestCLIStatusConfigLine(_ConfigCLIBase):

    def test_status_shows_the_active_digest_and_degraded_sections(self):
        self.pid_file.parent.mkdir(parents=True, exist_ok=True)
        self.pid_file.write_text("99999")
        self._start_daemon({
            "list": {"ok": True, "data": [], "errors": []},
            "config-show": {"ok": True, "digest": "abcdef0123456789" + "0" * 48,
                            "loaded_at": "2026-09-04T10:00:00-07:00", "config_path": self.cfg_path,
                            "degraded": [{"kind": "connector", "name": "mm", "error": "refused"}],
                            "reloading": False},
        })
        stdout, _, code = self._run_with(["status"], running=True)
        self.assertEqual(code, 0)
        self.assertIn("Config:   abcdef012345 (loaded 2026-09-04T10:00:00-07:00)", stdout)
        self.assertIn("[ERROR] Degraded: connector 'mm' — refused", stdout)


class TestStartValidatesConfig(unittest.TestCase):
    """`start`/`restart` refuse a config `config validate` rejects (#156).

    `start` used to hand the path straight to `start_daemon()`, whose only check
    is `GatewayConfig.from_file()` — parsing, not the cross-checks. A config the
    operator could watch `coop config validate` reject still started a gateway.
    """

    # A connector that can discover rooms, which the shared `gateway_config_text`
    # helper does not build (it emits `type: script`, where a `*` include is
    # itself an error rather than the shadowing warning this needs). Kept local
    # for that reason, not for size.
    _MM_HEADER = textwrap.dedent("""\
        connectors:
          - name: mm
            type: mattermost
            server: {url: http://localhost:8065, token: t, team: lab}
        agents:
          default:
            type: claude
            working_directory: /tmp
        watcher_rules:
        """)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # `start` now asks is_running() before validating, and the real one reads
        # the developer's own lock file — on a box with a gateway up, every test
        # here would fail with "already running" instead of exercising the gate.
        # The one test that cares about a running daemon patches it again itself.
        running = patch("gateway.daemon.is_running", return_value=(False, None))
        running.start()
        self.addCleanup(running.stop)

    def _write(self, text: str) -> str:
        path = self.tmp / "config.yaml"
        path.write_text(text)
        return str(path)

    def _bad_config(self) -> str:
        """An error: a rule bound to an agent that is not in `agents:`."""
        from tests.helpers import gateway_config_text
        return self._write(gateway_config_text(
            rules=[{"name": "w1", "agent": "nope", "connector": "script",
                    "rooms": {"include": ["script"]}}],
            working_directory=str(self.tmp)))

    def _warning_only_config(self) -> str:
        """Warnings but no errors: a second rule shadowed by a `*` first one."""
        return self._write(self._MM_HEADER + textwrap.dedent("""\
          - name: everything
            agent: default
            connector: mm
            rooms: {include: ["*"]}
          - name: never-fires
            agent: default
            connector: mm
            rooms: {include: ["eng-*"]}
        """))

    def _run(self, argv):
        main = _import_main()
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with patch.object(sys, "argv", ["coop"] + argv), \
                redirect_stdout(out), redirect_stderr(err):
            try:
                main()
            except SystemExit as e:
                code = e.code or 0
        return out.getvalue(), err.getvalue(), code

    def test_start_refuses_a_config_with_errors_and_never_forks(self):
        cfg = self._bad_config()
        with patch("gateway.daemon.start_daemon") as start:
            _, err, code = self._run(["start", "--config", cfg])
        self.assertEqual(code, 1)
        start.assert_not_called()
        self.assertIn("[ERROR]", err)
        self.assertIn("unknown agent 'nope'", err)

    def test_start_proceeds_when_the_config_only_has_warnings(self):
        cfg = self._warning_only_config()
        with patch("gateway.daemon.start_daemon") as start:
            self._run(["start", "--config", cfg])
        start.assert_called_once_with(cfg)

    def test_restart_validates_before_stopping_the_running_gateway(self):
        """The ordering half: validating inside the start would stop a healthy
        gateway and then refuse to bring it back."""
        cfg = self._bad_config()
        with patch("gateway.daemon.stop_daemon") as stop, \
                patch("gateway.daemon.start_daemon") as start:
            _, err, code = self._run(["restart", "--config", cfg])
        self.assertEqual(code, 1)
        stop.assert_not_called()
        start.assert_not_called()

    def test_restart_stops_and_starts_when_the_config_is_valid(self):
        cfg = self._warning_only_config()
        with patch("gateway.daemon.stop_daemon") as stop, \
                patch("gateway.daemon.start_daemon") as start:
            self._run(["restart", "--config", cfg])
        stop.assert_called_once()
        start.assert_called_once_with(cfg)

    # ── The preconditions the daemon's own boot sequence checks before
    # `from_file()` — is_running → lock → migrate_env_to_config → from_file.
    # Round 1 varied none of them: every test used a clean config with no
    # `.env`, no lock and valid YAML, so the preflight's blindness to all
    # three went unseen until review read it.

    _ENV_BACKED = textwrap.dedent("""\
        connectors:
          - name: rc
            type: rocketchat
            server: {url: "${RC_URL}", username: bot, password: pw}
        agents:
          default: {type: claude, working_directory: /tmp}
        watcher_rules:
          - {name: w1, agent: default, connector: rc, rooms: {include: [general]}}
        """)

    def test_a_pending_env_migration_skips_validation_rather_than_blocking_it(self):
        """An unmigrated `${VAR}` is a literal string to `validate_config()`, so
        validating before the daemon's migration would refuse every config the
        migration exists to fix — and the migration runs after the fork, so it
        could never be reached."""
        (self.tmp / ".env").write_text("RC_URL=https://chat.example.com\n")
        cfg = self._write(self._ENV_BACKED)
        # Precondition: this really is a config validate_config() rejects.
        from gateway.config_validate import validate_config
        self.assertFalse(validate_config(cfg).ok,
                         "config must be rejected while the placeholder is literal")
        with patch("gateway.daemon.start_daemon") as start:
            self._run(["start", "--config", cfg])
        start.assert_called_once_with(cfg)

    def test_once_migrated_the_same_config_is_validated_again(self):
        """The skip lasts exactly as long as `.env` does — one boot, not forever."""
        cfg = self._write(self._ENV_BACKED)          # no .env beside it
        with patch("gateway.daemon.start_daemon") as start:
            _, err, code = self._run(["start", "--config", cfg])
        self.assertEqual(code, 1)
        start.assert_not_called()
        self.assertIn("does not look like a URL", err)

    def test_malformed_yaml_is_a_cli_error_not_a_traceback(self):
        """`collect_config()` lets YAMLError escape. The daemon used to catch it
        and report a controlled failure, so an unguarded preflight would regress
        a traceback onto the most ordinary config mistake there is."""
        cfg = self._write("connectors: [unclosed\nagents: {\n")
        with patch("gateway.daemon.start_daemon") as start:
            _, err, code = self._run(["start", "--config", cfg])
        self.assertEqual(code, 1)
        start.assert_not_called()
        self.assertIn("[ERROR]", err)
        self.assertNotIn("Traceback", err)

    def test_an_already_running_gateway_is_reported_before_the_config_is_judged(self):
        """Otherwise a bare `start` while a gateway runs from another config path
        reports whatever is wrong with the default config instead of the truth."""
        cfg = self._bad_config()
        with patch("gateway.daemon.is_running", return_value=(True, 4242)), \
                patch("gateway.daemon.start_daemon") as start:
            out, err, code = self._run(["start", "--config", cfg])
        self.assertEqual(code, 1)
        start.assert_not_called()
        self.assertIn("already running (pid=4242)", out)
        self.assertNotIn("[ERROR]", err)

    def test_restart_does_not_refuse_merely_because_a_gateway_is_running(self):
        """`restart` exists to act on a running gateway — the is_running guard
        belongs to the `start` branch alone."""
        cfg = self._warning_only_config()
        with patch("gateway.daemon.is_running", return_value=(True, 4242)), \
                patch("gateway.daemon.stop_daemon") as stop, \
                patch("gateway.daemon.start_daemon") as start:
            self._run(["restart", "--config", cfg])
        stop.assert_called_once()
        start.assert_called_once_with(cfg)

    # ── Round 2 landed inside round 1's fix: the migration skip was written for
    # `start`, where nothing is running to damage, and `restart` inherited it.

    def test_restart_refuses_a_pending_migration_instead_of_stopping_first(self):
        """`restart` runs stop_daemon() next, so deferring validation the way
        `start` does would take a healthy gateway down for a config that cannot
        load — the outage the validate-before-stop order exists to prevent."""
        (self.tmp / ".env").write_text("RC_URL=https://chat.example.com\n")
        cfg = self._bad_config()
        with patch("gateway.daemon.stop_daemon") as stop, \
                patch("gateway.daemon.start_daemon") as start:
            _, err, code = self._run(["restart", "--config", cfg])
        self.assertEqual(code, 1)
        stop.assert_not_called()
        start.assert_not_called()
        self.assertIn("coop config migrate-env", err)

    def test_restart_refuses_a_pending_migration_even_when_the_config_is_valid(self):
        """Deliberate: the preflight cannot judge the post-migration document
        without resolving `.env` into the process environment, so it declines to
        guess. A stale or empty `.env` lands here too, which is the point — the
        operator is shown the file and can delete it."""
        (self.tmp / ".env").write_text("")
        cfg = self._warning_only_config()
        with patch("gateway.daemon.stop_daemon") as stop, \
                patch("gateway.daemon.start_daemon") as start:
            _, err, code = self._run(["restart", "--config", cfg])
        self.assertEqual(code, 1)
        stop.assert_not_called()
        start.assert_not_called()
        self.assertIn("delete it", err)

    def test_start_still_defers_a_pending_migration(self):
        """The refusal is `restart`'s alone — `start` has no running gateway to
        take down, so the daemon's own fail-closed migration stays in charge."""
        (self.tmp / ".env").write_text("RC_URL=https://chat.example.com\n")
        cfg = self._write(self._ENV_BACKED)
        with patch("gateway.daemon.start_daemon") as start:
            self._run(["start", "--config", cfg])
        start.assert_called_once_with(cfg)
