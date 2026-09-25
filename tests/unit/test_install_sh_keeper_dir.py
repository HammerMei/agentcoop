"""install.sh installs the coop-keeper agent directory (design §3.1).

`install_keeper_dir` is sourced out of install.sh and run against temp
directories (`tests.helpers.run_install_sh_function`), so the rule is tested
without running the installer. It runs the same manifest-driven sync as
`coop upgrade` through the repo's venv, so a re-run of the installer over an
existing install writes the owned paths and leaves everything else alone.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.helpers import INSTALL_SH, assert_tree_copied, run_install_sh_function

REPO = INSTALL_SH.parent
KEEPER_SRC = REPO / "agents" / "coop-keeper"


def _install(repo: Path, runtime: Path):
    return run_install_sh_function(
        ("install_keeper_dir",), f'install_keeper_dir "{repo}" "{runtime}"',
        capture_output=True, text=True,
    )


class TestInstallKeeperDir(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def test_copies_the_whole_shipped_tree_including_dotfiles(self):
        # A temp runtime dir is a non-default one, so every text file arrives
        # with the runtime directory written in (`keeper_text_for`, #182).
        from gateway.upgrade import keeper_text_for
        runtime = self.tmp / "runtime"
        runtime.mkdir()
        r = _install(REPO, runtime)
        self.assertEqual(r.returncode, 0, r.stderr)
        assert_tree_copied(self, KEEPER_SRC, runtime / "agents" / "builtin" / "coop-keeper",
                           transform=lambda text: keeper_text_for(text, runtime))
        self.assertTrue((runtime / "agents" / "user").is_dir())

    def test_a_second_run_leaves_the_operators_files_alone(self):
        runtime = self.tmp / "runtime"
        runtime.mkdir()
        self.assertEqual(_install(REPO, runtime).returncode, 0)
        dst = runtime / "agents" / "builtin" / "coop-keeper"
        (dst / "notes.md").write_text("mine\n")
        (dst / ".claude" / "settings.local.json").write_text("{}\n")
        r = _install(REPO, runtime)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((dst / "notes.md").read_text(), "mine\n")
        self.assertEqual((dst / ".claude" / "settings.local.json").read_text(), "{}\n")

    def test_a_repo_without_the_keeper_reports_nothing_shipped(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        runtime = self.tmp / "runtime"
        runtime.mkdir()
        r = _install(repo, runtime)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertFalse((runtime / "agents").exists())

    def test_the_installer_no_longer_knows_the_wizard(self):
        text = INSTALL_SH.read_text()
        for gone in ("--no-onboard", "NO_ONBOARD", "onboard --repo-path", "~/.agentcoop/.env"):
            self.assertNotIn(gone, text)

    def test_the_installer_ends_with_both_cli_start_lines(self):
        # Printed with the runtime directory the install actually used
        # ($COOP_HOME, default ~/.agentcoop — #182), not a spelled-out default.
        text = INSTALL_SH.read_text()
        self.assertIn("cd %s/agents/builtin/coop-keeper && opencode\\n' \"$RUNTIME_DIR\"", text)
        self.assertIn("cd %s/agents/builtin/coop-keeper && claude\\n' \"$RUNTIME_DIR\"", text)


if __name__ == "__main__":
    unittest.main()
