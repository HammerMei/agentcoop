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


def _is_foreign(link: Path) -> int:
    """Exit status of is_foreign_command: 0 = foreign, 1 = ours or absent."""
    script = (
        f'eval "$(sed -n \'/^is_foreign_command() {{/,/^}}/p\' "{INSTALL_SH}")"\n'
        f'is_foreign_command "{link}"'
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
        self.assertEqual(_is_foreign(self.link), 1)

    def test_our_own_symlink_is_not_foreign(self):
        self.link.symlink_to(self.venv_bin / "coop")
        self.assertEqual(_is_foreign(self.link), 1)

    def test_a_real_file_is_foreign(self):
        """AndrewDryga/coop's binary: a regular file, not a symlink."""
        self.link.write_text("#!/bin/sh\necho someone else\n")
        self.assertEqual(_is_foreign(self.link), 0)

    def test_a_symlink_to_another_tool_is_foreign(self):
        other = self.tmp / "other-tool" / "coop"
        other.parent.mkdir()
        other.write_text("")
        self.link.symlink_to(other)
        self.assertEqual(_is_foreign(self.link), 0)

    def test_a_symlink_to_an_earlier_install_elsewhere_is_ours(self):
        """The user installed from a clone at another path, or the repo moved:
        the link points at SOME `<repo>/.venv/bin/coop`. That is AgentCoop's
        own console-script shape, so re-running the installer repairs it
        instead of refusing (review: the first version compared against THIS
        run's venv only and refused its own earlier link)."""
        earlier = self.tmp / "elsewhere" / "agentcoop" / ".venv" / "bin" / "coop"
        earlier.parent.mkdir(parents=True)
        earlier.write_text("")
        self.link.symlink_to(earlier)
        self.assertEqual(_is_foreign(self.link), 1)

    def test_a_dangling_symlink_of_ours_is_still_ours(self):
        """Repo deleted or moved: the link dangles, but its shape says whose it
        was — replaceable without --force."""
        self.link.symlink_to(self.venv_bin / "coop")
        (self.venv_bin / "coop").unlink()
        self.assertEqual(_is_foreign(self.link), 1)

    def test_installer_decides_before_uv_sync_and_offers_force(self):
        text = INSTALL_SH.read_text()
        self.assertIn('--force) FORCE=true ;;', text)
        check = text.index('is_foreign_command "$COOP_LINK"')
        sync = text.index('uv sync --project "$REPO_DIR"')
        link = text.index('link_console_script "$VENV_BIN" "$COOP_LINK"')
        self.assertLess(check, sync, "refuse before spending minutes on uv sync")
        self.assertLess(check, link)
        self.assertIn("ln -s $VENV_BIN", text)          # the manual-link instruction
        self.assertIn("use --force", text)
        # The --force wording is honest about symlinks: only a regular file gets a .bak.
        self.assertIn("a symlink is replaced outright", text)
