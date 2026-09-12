"""Unit tests for gateway/config_migrate.py — the one-time .env -> config.yaml
migration (docs/design/config-tool.md decision 6 revisited).

Covers the migration LOGIC only; gateway/daemon.py's auto-invocation at
startup and the CLI entry point are covered separately.
"""

from __future__ import annotations

import os
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

from unittest.mock import patch

from gateway.config_migrate import has_pending_migration, migrate_env_to_config


class TestMigrateEnvToConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.agent_dir = Path(self.tmp) / "work"
        self.agent_dir.mkdir()
        self.config_path = Path(self.tmp) / "config.yaml"
        self.env_path = Path(self.tmp) / ".env"

    def _write_config(self, yaml_text: str) -> None:
        self.config_path.write_text(textwrap.dedent(yaml_text))

    def _valid_cfg_text(self, password: str) -> str:
        return f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: "{password}"}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                room: general
        """

    def test_no_env_file_is_a_no_op(self):
        self._write_config(self._valid_cfg_text("plaintext-already"))
        result = migrate_env_to_config(self.config_path)
        self.assertFalse(result.migrated)
        raw = yaml.safe_load(self.config_path.read_text())
        self.assertEqual(raw["connectors"][0]["server"]["password"], "plaintext-already")

    def test_resolves_the_reference_and_writes_the_literal_value(self):
        self._write_config(self._valid_cfg_text("${RC_PASSWORD}"))
        self.env_path.write_text("RC_PASSWORD=hunter2\n")

        result = migrate_env_to_config(self.config_path)

        self.assertTrue(result.migrated)
        self.assertEqual(result.ref_count, 1)
        raw = yaml.safe_load(self.config_path.read_text())
        self.assertEqual(raw["connectors"][0]["server"]["password"], "hunter2")

    def test_env_file_is_moved_into_config_backups_not_left_in_place(self):
        self._write_config(self._valid_cfg_text("${RC_PASSWORD}"))
        self.env_path.write_text("RC_PASSWORD=hunter2\n")

        result = migrate_env_to_config(self.config_path)

        self.assertFalse(self.env_path.exists())
        self.assertIsNotNone(result.env_backup_path)
        self.assertTrue(result.env_backup_path.exists())
        self.assertEqual(result.env_backup_path.read_text(), "RC_PASSWORD=hunter2\n")
        self.assertEqual(result.env_backup_path.parent.name, ".config-backups")

    def test_backup_and_directory_are_permissioned(self):
        self._write_config(self._valid_cfg_text("${RC_PASSWORD}"))
        self.env_path.write_text("RC_PASSWORD=hunter2\n")

        result = migrate_env_to_config(self.config_path)

        backup_dir = result.env_backup_path.parent
        self.assertEqual(oct(backup_dir.stat().st_mode)[-3:], "700")
        self.assertEqual(oct(result.env_backup_path.stat().st_mode)[-3:], "600")

    def test_config_yaml_backup_is_also_created_by_the_reused_save_path(self):
        """migrate_env_to_config() must not reimplement EditableConfig.
        save()'s own backup step — just exercise it. Tolerant of either
        backup location/convention EditableConfig.save() itself uses (that
        detail is EditableConfig's own to test, in test_configtool_model.py)."""
        self._write_config(self._valid_cfg_text("${RC_PASSWORD}"))
        self.env_path.write_text("RC_PASSWORD=hunter2\n")

        migrate_env_to_config(self.config_path)

        flat = list(self.config_path.parent.glob("config.yaml.bak.*"))
        nested = list((self.config_path.parent / ".config-backups").glob("config.yaml.bak.*"))
        self.assertEqual(len(flat) + len(nested), 1)

    def test_unresolvable_reference_raises_and_leaves_everything_untouched(self):
        self._write_config(self._valid_cfg_text("${MISSING_VAR}"))
        self.env_path.write_text("SOME_OTHER_VAR=irrelevant\n")
        original_config_text = self.config_path.read_text()
        original_env_text = self.env_path.read_text()

        with self.assertRaises(ValueError):
            migrate_env_to_config(self.config_path)

        self.assertEqual(self.config_path.read_text(), original_config_text)
        self.assertTrue(self.env_path.exists())
        self.assertEqual(self.env_path.read_text(), original_env_text)

    def test_resolves_from_ambient_environment_when_not_in_env_file(self):
        """load_dotenv() doesn't override an already-set process env var —
        matching GatewayConfig.from_file's own resolution order — so a
        reference the daemon has always resolved from the ambient
        environment (not .env) migrates to that SAME literal value, not an
        unresolved error."""
        self._write_config(self._valid_cfg_text("${RC_AMBIENT_TEST_VAR}"))
        # .env exists (so the migration triggers) but doesn't define this var.
        self.env_path.write_text("UNRELATED=1\n")
        os.environ["RC_AMBIENT_TEST_VAR"] = "from-the-shell"
        self.addCleanup(os.environ.pop, "RC_AMBIENT_TEST_VAR", None)

        result = migrate_env_to_config(self.config_path)

        self.assertTrue(result.migrated)
        raw = yaml.safe_load(self.config_path.read_text())
        self.assertEqual(raw["connectors"][0]["server"]["password"], "from-the-shell")

    def test_migrates_multiple_references_across_different_fields(self):
        text = f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "${{RC_URL}}", username: "${{RC_USER}}", password: "${{RC_PASSWORD}}"}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                room: general
        """
        self._write_config(text)
        self.env_path.write_text(
            "RC_URL=http://chat.example.com\nRC_USER=bot\nRC_PASSWORD=hunter2\n"
        )

        result = migrate_env_to_config(self.config_path)

        self.assertEqual(result.ref_count, 3)
        raw = yaml.safe_load(self.config_path.read_text())
        server = raw["connectors"][0]["server"]
        self.assertEqual(server["url"], "http://chat.example.com")
        self.assertEqual(server["username"], "bot")
        self.assertEqual(server["password"], "hunter2")

    def test_ref_count_is_zero_when_env_exists_but_nothing_references_it(self):
        """.env can exist without config.yaml referencing anything in it
        (e.g. leftover from a prior manual setup) — still migrates (removes
        the now-pointless .env), just with nothing to report."""
        self._write_config(self._valid_cfg_text("plaintext-already"))
        self.env_path.write_text("UNUSED_VAR=whatever\n")

        result = migrate_env_to_config(self.config_path)

        self.assertTrue(result.migrated)
        self.assertEqual(result.ref_count, 0)
        self.assertFalse(self.env_path.exists())

    def test_ref_count_counts_bare_dollar_form_without_braces(self):
        """The counting regex must match gateway/config.py's _expand_env_vars()
        exactly (a code-review finding caught them diverging) — $VAR (no
        braces) is one of the two forms it resolves."""
        self._write_config(self._valid_cfg_text("$RC_PASSWORD"))
        self.env_path.write_text("RC_PASSWORD=hunter2\n")

        result = migrate_env_to_config(self.config_path)

        self.assertEqual(result.ref_count, 1)
        raw = yaml.safe_load(self.config_path.read_text())
        self.assertEqual(raw["connectors"][0]["server"]["password"], "hunter2")

    def test_missing_config_file_with_no_env_either_still_raises(self):
        """Round-2 code-review finding, correcting round 1's own fix: checking
        .env's existence before config_path's own existence let a missing
        config with no .env slip through as a silent, false "nothing to
        migrate" no-op — misleading standalone via the CLI (reports success
        for a typo'd/missing path) and worse via gateway/daemon.py's
        automatic trigger, where the no-op let the very next line
        (_harden_config_permissions(), an unconditional chmod) crash on the
        nonexistent file with an unhandled traceback. config_path's own
        existence is now checked explicitly and unconditionally, regardless
        of whether .env exists — still before EditableConfig.load() ever
        runs, so the efficiency fix (no wasted YAML parse in the common
        case) is unaffected."""
        self.assertFalse(self.config_path.exists())
        self.assertFalse(self.env_path.exists())

        with self.assertRaises(FileNotFoundError):
            migrate_env_to_config(self.config_path)

    def test_missing_config_file_with_an_env_present_still_raises(self):
        """The no-op path only applies when .env is ALSO missing — if .env
        exists but config.yaml doesn't, that's a real error (nothing to
        load), not silently swallowed."""
        self.env_path.write_text("RC_PASSWORD=hunter2\n")
        self.assertFalse(self.config_path.exists())

        with self.assertRaises(FileNotFoundError):
            migrate_env_to_config(self.config_path)

    def test_resolves_symlinked_config_path_and_migrates_the_real_host_files(self):
        """Code-review finding: migrating via an unresolved symlink path
        (e.g. Docker Mode 1's runtime config.yaml/.env symlinked to a
        bind-mounted host directory) used to let EditableConfig.save()'s
        os.replace() decouple the container-local symlink from the real
        file instead of writing through to it, silently reporting success
        while the real host files were never touched. Resolving the path
        unconditionally at the top of migrate_env_to_config() fixes this
        for every caller, not just the ones that remembered to resolve
        first."""
        host_dir = Path(self.tmp) / "host"
        host_dir.mkdir()
        host_config = host_dir / "config.yaml"
        host_env = host_dir / ".env"
        host_config.write_text(textwrap.dedent(self._valid_cfg_text("${RC_PASSWORD}")))
        host_env.write_text("RC_PASSWORD=hunter2\n")

        runtime_dir = Path(self.tmp) / "runtime"
        runtime_dir.mkdir()
        symlinked_config = runtime_dir / "config.yaml"
        symlinked_config.symlink_to(host_config)
        (runtime_dir / ".env").symlink_to(host_env)

        result = migrate_env_to_config(symlinked_config)

        self.assertTrue(result.migrated)
        # The REAL host file must be updated, not just the runtime symlink.
        raw = yaml.safe_load(host_config.read_text())
        self.assertEqual(raw["connectors"][0]["server"]["password"], "hunter2")
        # The real host .env must be gone (moved into the host's own
        # .config-backups/, since config_path resolves there too).
        self.assertFalse(host_env.exists())
        self.assertTrue(result.env_backup_path.exists())
        self.assertEqual(result.env_backup_path.read_text(), "RC_PASSWORD=hunter2\n")


if __name__ == "__main__":
    unittest.main()


class TestHasPendingMigration(unittest.TestCase):
    """The predicate both the CLI preflight and the migration itself answer with.

    Its docstring promises two things — never raises, and a missing config is
    "nothing pending". Both are asserted here rather than left to the caller,
    because the CLI calls it BEFORE its own exception guard: a raise here
    reaches the operator as a traceback, and a wrong `True` sends `restart` to
    recommend `coop config migrate-env` on a file that does not exist.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)

    def test_a_config_beside_an_env_file_is_pending(self):
        (self.tmp / ".env").write_text("X=1\n")
        cfg = self.tmp / "config.yaml"
        cfg.write_text("{}\n")
        self.assertTrue(has_pending_migration(cfg))

    def test_a_config_with_no_env_beside_it_is_not_pending(self):
        cfg = self.tmp / "config.yaml"
        cfg.write_text("{}\n")
        self.assertFalse(has_pending_migration(cfg))

    def test_a_missing_config_is_not_pending_even_with_an_env_alongside(self):
        """A directory's stray `.env` is not a migration: there is nothing to
        migrate into, and `coop config migrate-env` would fail on the missing
        file it was recommended for."""
        (self.tmp / ".env").write_text("X=1\n")
        self.assertFalse(has_pending_migration(self.tmp / "absent.yaml"))

    def test_it_resolves_symlinks_before_looking_for_the_env_file(self):
        """The migration resolves first, so the predicate must too — otherwise a
        symlinked config.yaml puts the CLI's answer and the daemon's in
        different directories."""
        real_dir = self.tmp / "real"
        real_dir.mkdir()
        (real_dir / "config.yaml").write_text("{}\n")
        (real_dir / ".env").write_text("X=1\n")
        link_dir = self.tmp / "link"
        link_dir.mkdir()
        link = link_dir / "config.yaml"
        link.symlink_to(real_dir / "config.yaml")
        self.assertTrue(has_pending_migration(link),
                        "the `.env` beside the RESOLVED path is the one that counts")

    def test_a_resolve_failure_is_not_pending_rather_than_an_exception(self):
        """`Path.resolve()` raises `RuntimeError` on a symlink loop under Python
        3.12 and `OSError` for other path failures. Injected rather than built
        from real symlinks because 3.13 stopped raising for loops entirely, and
        the contract must hold on both."""
        for exc in (RuntimeError("Symlink loop from '/x'"), OSError("boom")):
            with self.subTest(exc=type(exc).__name__):
                with patch.object(Path, "resolve", side_effect=exc):
                    self.assertFalse(has_pending_migration(self.tmp / "config.yaml"))
