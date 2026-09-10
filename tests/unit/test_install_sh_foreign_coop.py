"""install.sh refuses to take over a `coop` that is not ours (#154).

`AndrewDryga/coop` installs a binary named `coop` into ~/.local/bin — the same
name, the same directory. link_console_script() moves an occupying file to a
.bak and takes the path, which is right for a stale copy of OUR script and
wrong for someone else's working command. The installer therefore asks
`is_foreign_command` first. These tests source that one function out of
install.sh and exercise it against a temp dir, so the rule is pinned without
running the installer.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

INSTALL_SH = Path(__file__).resolve().parents[2] / "install.sh"


def _is_foreign(link: Path, venv_bin: Path) -> int:
    """Exit status of is_foreign_command: 0 = foreign, 1 = ours or absent."""
    script = (
        f'eval "$(sed -n \'/^is_foreign_command() {{/,/^}}/p\' "{INSTALL_SH}")"\n'
        f'is_foreign_command "{link}" "{venv_bin}"'
    )
    return subprocess.run(["bash", "-c", script]).returncode


class TestIsForeignCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.venv_bin = self.tmp / "repo" / ".venv" / "bin"
        self.venv_bin.mkdir(parents=True)
        (self.venv_bin / "coop").write_text("#!/bin/sh\n")
        self.local_bin = self.tmp / ".local" / "bin"
        self.local_bin.mkdir(parents=True)
        self.link = self.local_bin / "coop"

    def test_nothing_there_is_not_foreign(self):
        self.assertEqual(_is_foreign(self.link, self.venv_bin), 1)

    def test_our_own_symlink_is_not_foreign(self):
        self.link.symlink_to(self.venv_bin / "coop")
        self.assertEqual(_is_foreign(self.link, self.venv_bin), 1)

    def test_a_real_file_is_foreign(self):
        """AndrewDryga/coop's binary: a regular file, not a symlink."""
        self.link.write_text("#!/bin/sh\necho someone else\n")
        self.assertEqual(_is_foreign(self.link, self.venv_bin), 0)

    def test_a_symlink_elsewhere_is_foreign(self):
        other = self.tmp / "other-tool" / "coop"
        other.parent.mkdir()
        other.write_text("")
        self.link.symlink_to(other)
        self.assertEqual(_is_foreign(self.link, self.venv_bin), 0)

    def test_a_dangling_symlink_into_our_venv_is_still_ours(self):
        """A stale link from a previous install of ours (target gone after a
        re-clone) must be replaceable without --force: it points into OUR venv."""
        self.link.symlink_to(self.venv_bin / "coop")
        (self.venv_bin / "coop").unlink()
        self.assertEqual(_is_foreign(self.link, self.venv_bin), 1)

    def test_installer_wires_the_check_before_linking_and_offers_force(self):
        text = INSTALL_SH.read_text()
        self.assertIn('--force) FORCE=true ;;', text)
        check = text.index('is_foreign_command "$COOP_LINK"')
        link = text.index('link_console_script "$VENV_BIN" "$COOP_LINK"')
        self.assertLess(check, link, "the foreign check must run before the link is made")
        self.assertIn("ln -s $VENV_BIN", text)          # the manual-link instruction
        self.assertIn("use --force", text)
