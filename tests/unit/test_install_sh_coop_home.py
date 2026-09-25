"""install.sh installs to `$COOP_HOME` (#182), default `~/.agentcoop`.

`coop_home_dir` decides the runtime directory the same way `gateway/paths.py`
does — `$COOP_HOME` when set, `~` expanded, relative refused — and
`persist_coop_home` exports a non-default choice from the shell rc files, the
way the installer already adds `~/.local/bin` to PATH, so `coop`,
`coop upgrade` and coop-keeper's CLI calls find the same directory later.
Both are sourced out of install.sh and run against temp directories
(`tests.helpers.run_install_sh_function`), so the rules are pinned without
running the installer.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.helpers import run_install_sh_function, subprocess_env


def _run(call: str, *, home: Path, coop_home: str | None, shell: str = "/bin/bash"):
    env = subprocess_env(home=home, coop_home=coop_home)
    env["SHELL"] = shell  # persist_coop_home checks the ACTIVE shell's rc file
    return run_install_sh_function(
        ("coop_home_dir", "persist_coop_home"), call, env=env, capture_output=True, text=True,
    )


def _sourced_value(rc: Path, shell: str) -> str:
    """What `COOP_HOME` is in a fresh `shell` after sourcing `rc` — the only
    test of an rc line that means anything."""
    import shutil
    import subprocess
    exe = shutil.which(shell)
    if exe is None:
        raise unittest.SkipTest(f"{shell} not installed")
    r = subprocess.run([exe, "-c", f'. "{rc}"; printf %s "$COOP_HOME"'],
                       env={"HOME": str(rc.parent), "PATH": "/usr/bin:/bin"},
                       capture_output=True, text=True)
    return r.stdout


class TestCoopHomeDir(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.home, ignore_errors=True))

    def test_unset_is_the_default(self):
        r = _run("coop_home_dir", home=self.home, coop_home=None)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), f"{self.home}/.agentcoop")

    def test_empty_is_unset(self):
        r = _run("coop_home_dir", home=self.home, coop_home="")
        self.assertEqual(r.stdout.strip(), f"{self.home}/.agentcoop")

    def test_an_absolute_path_is_used_as_given(self):
        r = _run("coop_home_dir", home=self.home, coop_home="/srv/coop")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "/srv/coop")

    def test_a_tilde_is_expanded(self):
        r = _run("coop_home_dir", home=self.home, coop_home="~/coop")
        self.assertEqual(r.stdout.strip(), f"{self.home}/coop")

    def test_the_filesystem_root_is_refused_by_name(self):
        r = _run("coop_home_dir", home=self.home, coop_home="/")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        self.assertIn("COOP_HOME", r.stderr)
        self.assertIn("root", r.stderr)

    def test_a_relative_path_is_refused_by_name(self):
        r = _run("coop_home_dir", home=self.home, coop_home="coop")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        self.assertIn("COOP_HOME", r.stderr)
        self.assertIn("absolute", r.stderr)


class TestPersistCoopHome(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.home, ignore_errors=True))
        self.bashrc = self.home / ".bashrc"
        self.zshrc = self.home / ".zshrc"
        self.bashrc.write_text("# mine\n")

    def test_the_default_location_writes_nothing(self):
        r = _run(f'persist_coop_home "{self.home}/.agentcoop"', home=self.home, coop_home=None)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.bashrc.read_text(), "# mine\n")
        self.assertFalse(self.zshrc.exists(), "no rc file is created")

    def test_a_custom_location_is_exported_from_every_rc_file_present(self):
        self.zshrc.write_text("")
        r = _run('persist_coop_home "/srv/coop"', home=self.home, coop_home=None)
        self.assertEqual(r.returncode, 0, r.stderr)
        for rc in (self.bashrc, self.zshrc):
            self.assertIn("export COOP_HOME='/srv/coop'", rc.read_text(), rc.name)
        self.assertTrue(self.bashrc.read_text().startswith("# mine\n"), "appended, not replaced")
        self.assertEqual(_sourced_value(self.bashrc, "bash"), "/srv/coop")
        self.assertEqual(_sourced_value(self.zshrc, "zsh"), "/srv/coop")

    def test_shell_significant_characters_in_the_path_stay_literal(self):
        # `$USER` must not expand and a quote must not end the string when the
        # next shell sources the file (Codex, PR #186 round 1).
        self.zshrc.write_text("")
        for path in ("/srv/$USER/coop", "/srv/o'neil/coop", "/srv/`id`/coop"):
            for rc in (self.bashrc, self.zshrc):
                rc.write_text("")
            r = _run(f"persist_coop_home '{path}'".replace("'/srv/o'neil/coop'", '"/srv/o\'neil/coop"'),
                     home=self.home, coop_home=None)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(_sourced_value(self.bashrc, "bash"), path, path)
            self.assertEqual(_sourced_value(self.zshrc, "zsh"), path, path)

    def test_a_fish_user_is_warned_even_when_a_dormant_bashrc_took_the_line(self):
        # Only bash/zsh rc files are written; a fish user's startup file is not,
        # and a dormant ~/.bashrc must not make the installer say otherwise.
        r = _run('persist_coop_home "/srv/coop"', home=self.home, coop_home=None, shell="/usr/local/bin/fish")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("[WARNING]", r.stderr)
        self.assertIn("fish", r.stderr)

    def test_a_zsh_user_without_a_zshrc_is_warned(self):
        r = _run('persist_coop_home "/srv/coop"', home=self.home, coop_home=None, shell="/bin/zsh")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("[WARNING]", r.stderr)
        self.assertIn("export COOP_HOME='/srv/coop'", self.bashrc.read_text(), "bash still gets it")
        self.assertFalse(self.zshrc.exists())

    def test_a_second_run_does_not_add_a_second_line(self):
        _run('persist_coop_home "/srv/coop"', home=self.home, coop_home=None)
        _run('persist_coop_home "/srv/coop"', home=self.home, coop_home=None)
        self.assertEqual(self.bashrc.read_text().count("COOP_HOME="), 1)

    def test_a_comment_mentioning_the_variable_is_not_an_export(self):
        self.bashrc.write_text("# COOP_HOME=/srv/coop is where coop lives\n")
        r = _run('persist_coop_home "/srv/coop"', home=self.home, coop_home=None)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        self.assertIn("export COOP_HOME='/srv/coop'", self.bashrc.read_text())

    def test_no_rc_file_present_is_reported_not_silent(self):
        # fish, or an account with neither file: the next shell's `coop` would
        # silently use ~/.agentcoop, so the installer says the export is on you.
        self.bashrc.unlink()
        r = _run('persist_coop_home "/srv/coop"', home=self.home, coop_home=None)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("[WARNING]", r.stderr)
        self.assertIn("export COOP_HOME='/srv/coop'", r.stderr)
        self.assertFalse(self.bashrc.exists(), "no rc file is created")

    def test_an_unquoted_export_of_the_same_value_counts(self):
        self.bashrc.write_text("export COOP_HOME=/srv/coop\n")
        r = _run('persist_coop_home "/srv/coop"', home=self.home, coop_home=None)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        self.assertEqual(self.bashrc.read_text(), "export COOP_HOME=/srv/coop\n")

    def test_a_different_existing_export_is_left_alone_and_reported(self):
        # A reinstall to a new location must not silently stack two exports; the
        # operator is told which line to change.
        self.bashrc.write_text('export COOP_HOME="/old/coop"\n')
        r = _run('persist_coop_home "/srv/coop"', home=self.home, coop_home=None)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.bashrc.read_text(), 'export COOP_HOME="/old/coop"\n')
        self.assertIn("[WARNING]", r.stderr)
        self.assertIn(".bashrc", r.stderr)
        self.assertIn("/srv/coop", r.stderr)
