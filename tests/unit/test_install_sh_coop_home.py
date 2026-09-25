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

from tests.helpers import COOP_HOME_SPELLINGS, run_install_sh_function, subprocess_env


def _run(call: str, *, home: Path, coop_home: str | None, shell: str = "/bin/bash"):
    env = subprocess_env(home=home, coop_home=coop_home)
    env["SHELL"] = shell  # persist_coop_home checks the ACTIVE shell's rc file
    return run_install_sh_function(
        ("coop_home_dir", "rc_exports_coop_home", "persist_coop_home"), call,
        env=env, capture_output=True, text=True,
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

    def test_every_spelling_in_the_shared_table(self):
        """The same table `gateway/paths.py` is tested against: the two
        validators must not drift apart (they had, after one round)."""
        for value, accepted in COOP_HOME_SPELLINGS:
            with self.subTest(value=value):
                r = _run("coop_home_dir", home=self.home, coop_home=value)
                if accepted:
                    self.assertEqual(r.returncode, 0, r.stderr)
                    self.assertEqual(r.stdout.strip(), value.replace("~", str(self.home), 1))
                else:
                    self.assertNotEqual(r.returncode, 0, value)
                    self.assertEqual(r.stdout, "")
                    self.assertIn("COOP_HOME must be an absolute, canonical path", r.stderr)


class TestRuntimeDirIsOurs(unittest.TestCase):
    """The installer clones the repo that `~/.local/bin/coop` runs from under
    the runtime dir, so a directory another local user pre-created, links, or
    can write into would let them replace `coop` (Codex security review, PR
    #186 round 3). Absent, or a real directory of ours that others cannot
    write to, is fine. "Owned by someone else" cannot be made without root and
    is not tested."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.home, ignore_errors=True))

    def _check(self, path: Path):
        return run_install_sh_function(
            ("runtime_dir_is_ours",), f'runtime_dir_is_ours "{path}"',
            env=subprocess_env(home=self.home), capture_output=True, text=True,
        )

    def test_absent_is_fine(self):
        r = self._check(self.home / "coop")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")

    def test_our_own_private_directory_is_fine(self):
        d = self.home / "coop"
        d.mkdir(mode=0o755)
        r = self._check(d)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_symlink_is_refused(self):
        real = self.home / "real"
        real.mkdir()
        link = self.home / "coop"
        link.symlink_to(real)
        r = self._check(link)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("[ERROR]", r.stderr)
        self.assertIn("symbolic link", r.stderr)

    def test_a_file_is_refused(self):
        f = self.home / "coop"
        f.write_text("")
        r = self._check(f)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not a directory", r.stderr)

    def test_a_directory_others_can_write_to_is_refused(self):
        d = self.home / "coop"
        d.mkdir(mode=0o777)
        import os
        os.chmod(d, 0o777)  # past the umask
        r = self._check(d)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("[ERROR]", r.stderr)
        self.assertIn("writable by other users", r.stderr)


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

    def test_a_commented_out_export_is_not_an_export(self):
        # `# export COOP_HOME='/srv/coop'` used to satisfy the fixed-string
        # check: nothing written, no warning, and the next shell had no value.
        self.bashrc.write_text("# export COOP_HOME='/srv/coop'\n")
        r = _run('persist_coop_home "/srv/coop"', home=self.home, coop_home=None)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        self.assertEqual(_sourced_value(self.bashrc, "bash"), "/srv/coop")

    def test_a_prefix_of_the_value_is_another_value(self):
        # `export COOP_HOME=/srv/coop-old` is not `/srv/coop`: it is the
        # "another value" case, warned about and left alone, not a match.
        self.bashrc.write_text("export COOP_HOME=/srv/coop-old\n")
        r = _run('persist_coop_home "/srv/coop"', home=self.home, coop_home=None)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("[WARNING]", r.stderr)
        self.assertIn("another value", r.stderr)
        self.assertEqual(self.bashrc.read_text(), "export COOP_HOME=/srv/coop-old\n")

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
