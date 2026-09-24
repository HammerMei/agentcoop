"""install.sh installs the coop-keeper agent directory (design §3.1).

`install_keeper_dir` is sourced out of install.sh and run against temp
directories, the same way `test_install_sh_foreign_coop.py` pins
`is_foreign_command`, so the rule is tested without running the installer.
It runs the same manifest-driven sync as `coop upgrade` through the repo's
venv, so a re-run of the installer over an existing install writes the owned
paths and leaves everything else in the directory alone.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO / "install.sh"


def _run_install_keeper_dir(repo: Path, runtime: Path) -> subprocess.CompletedProcess:
    script = (
        f'eval "$(sed -n \'/^install_keeper_dir() {{/,/^}}/p\' "{INSTALL_SH}")"\n'
        f'install_keeper_dir "{repo}" "{runtime}"'
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


class TestInstallKeeperDir(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_copies_the_whole_shipped_tree_including_dotfiles(self):
        runtime = self.tmp / "runtime"
        runtime.mkdir()
        r = _run_install_keeper_dir(REPO, runtime)
        self.assertEqual(r.returncode, 0, r.stderr)
        dst = runtime / "agents" / "builtin" / "coop-keeper"
        src = REPO / "agents" / "coop-keeper"
        for f in src.rglob("*"):
            if f.is_file():
                rel = f.relative_to(src)
                self.assertEqual((dst / rel).read_bytes(), f.read_bytes(), rel)
        self.assertTrue((runtime / "agents" / "user").is_dir())

    def test_a_second_run_leaves_the_operators_files_alone(self):
        runtime = self.tmp / "runtime"
        runtime.mkdir()
        self.assertEqual(_run_install_keeper_dir(REPO, runtime).returncode, 0)
        dst = runtime / "agents" / "builtin" / "coop-keeper"
        (dst / "notes.md").write_text("mine\n")
        (dst / ".claude" / "settings.local.json").write_text("{}\n")
        r = _run_install_keeper_dir(REPO, runtime)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((dst / "notes.md").read_text(), "mine\n")
        self.assertEqual((dst / ".claude" / "settings.local.json").read_text(), "{}\n")

    def test_a_repo_without_the_keeper_is_a_noop(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        runtime = self.tmp / "runtime"
        runtime.mkdir()
        r = _run_install_keeper_dir(repo, runtime)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((runtime / "agents").exists())

    def test_the_installer_no_longer_knows_the_wizard(self):
        text = INSTALL_SH.read_text()
        for gone in ("--no-onboard", "NO_ONBOARD", "onboard --repo-path", "~/.agentcoop/.env"):
            self.assertNotIn(gone, text)

    def test_the_installer_ends_with_both_cli_start_lines(self):
        text = INSTALL_SH.read_text()
        self.assertIn("cd ~/.agentcoop/agents/builtin/coop-keeper && opencode", text)
        self.assertIn("cd ~/.agentcoop/agents/builtin/coop-keeper && claude", text)


if __name__ == "__main__":
    unittest.main()
