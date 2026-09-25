"""install.sh installs to `$COOP_HOME` (#182), default `~/.agentcoop`.

`coop_home_dir` decides the runtime directory by the same rule as
`gateway/paths.py` (`tests.helpers.COOP_HOME_SPELLINGS` is the shared surface);
`runtime_dir_is_ours` refuses a directory the installer should not trust; and
`coop_home_hint` prints, in the login shell's syntax, the line the operator adds
so later shells find a non-default directory — the installer writes no shell
startup file for it (owner's ruling on PR #186: a rare case, ask the user).
All are sourced out of install.sh and run against temp directories
(`tests.helpers.run_install_sh_function`), so the rules are pinned without
running the installer.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.helpers import COOP_HOME_SPELLINGS, INSTALL_SH, run_install_sh_function, subprocess_env


def _run(call: str, *, home: Path, coop_home: str | None, shell: str = "/bin/bash"):
    env = subprocess_env(home=home, coop_home=coop_home)
    env["SHELL"] = shell  # coop_home_hint speaks the login shell's syntax
    return run_install_sh_function(
        ("coop_home_dir", "coop_home_hint"), call, env=env, capture_output=True, text=True,
    )


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


class TestCoopHomeHint(unittest.TestCase):
    """The printed line is in the login shell's own syntax; nothing is written."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.home, ignore_errors=True))

    def test_each_shell_family_gets_its_own_syntax(self):
        for shell, expected in (
            ("/bin/bash", "export COOP_HOME='/srv/coop'"),
            ("/bin/zsh", "export COOP_HOME='/srv/coop'"),
            ("/usr/local/bin/fish", "set -gx COOP_HOME '/srv/coop'"),
            ("/bin/tcsh", "setenv COOP_HOME '/srv/coop'"),
            ("/bin/csh", "setenv COOP_HOME '/srv/coop'"),
            ("", "export COOP_HOME='/srv/coop'"),
        ):
            with self.subTest(shell=shell):
                r = _run('coop_home_hint "/srv/coop"', home=self.home, coop_home=None, shell=shell)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stdout.strip(), expected)

    def test_the_installer_exports_coop_home_only_when_the_operator_set_it(self):
        # The default must not reach gateway/paths.py as an explicit value: a
        # $HOME the explicit-value rule refuses (a space, a non-ASCII name)
        # still installs. Pinned on the script text, as the banner test is.
        text = INSTALL_SH.read_text()
        self.assertIn('if [ -n "${COOP_HOME:-}" ]; then\n  export COOP_HOME="$RUNTIME_DIR"\nfi', text)
        self.assertNotIn("persist_coop_home", text)
