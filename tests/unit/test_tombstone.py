"""`agent-chat-gateway` after the rename is a tombstone, not an alias (#154)."""

import io
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

from gateway import tombstone


class TestTombstone(unittest.TestCase):
    def test_explains_the_rename_and_both_ways_out_then_exits_1(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            tombstone.main()
        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(out.getvalue(), "", "a failing command writes to stderr, not stdout")
        text = err.getvalue()
        self.assertIn("AgentCoop", text)
        self.assertIn("`coop`", text)
        self.assertIn("docs/migration-v1.md", text)                       # move to v1
        self.assertIn("git checkout v0.5.2 && uv sync && agent-chat-gateway start", text)  # stay on v0.5
        self.assertIn("~/.agent-chat-gateway/repo", text)                 # curl-install default repo
        self.assertIn('kill "$(cat ~/.agent-chat-gateway/gateway.pid)"', text)  # the v0 daemon this cannot stop

    def test_forwards_nothing(self):
        """Not an alias: `agent-chat-gateway start` must not start anything.
        The module has no dispatch — the only public symbol is main()."""
        public = [n for n in dir(tombstone) if not n.startswith("_")
                  and n not in ("sys", "json", "Path")]
        self.assertEqual(sorted(public), ["MESSAGE", "OLD_RUNTIME_DIR", "build_message", "main"])

    def test_is_wired_as_the_old_console_script(self):
        """pyproject maps the OLD command name to this module, so a v0.5
        install that pulled v1 gets the message instead of a dangling symlink."""
        import tomllib
        from pathlib import Path
        py = tomllib.loads((Path(__file__).resolve().parents[2] / "pyproject.toml").read_text())
        scripts = py["project"]["scripts"]
        self.assertEqual(scripts["agent-chat-gateway"], "gateway.tombstone:main")
        self.assertEqual(scripts["coop"], "gateway.cli:main")
        self.assertEqual(scripts["coop-provision"], "gateway.admin.cli:main")
        self.assertNotIn("acg-provision", scripts)

    def test_names_the_real_repo_of_a_local_script_install(self):
        """A local-script install keeps its repo wherever it was cloned; the v0
        install_meta.json says where. Hardcoding ~/.agent-chat-gateway/repo
        sent those users to a directory that does not exist (review)."""
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as home:
            old = Path(home) / ".agent-chat-gateway"
            old.mkdir()
            repo = Path(home) / "src" / "agent-chat-gateway"
            (old / "install_meta.json").write_text(json.dumps(
                {"method": "git", "repo_path": str(repo), "version": "0.5.2"}))
            with patch.object(tombstone, "OLD_RUNTIME_DIR", old), \
                 patch("gateway.tombstone.Path.home", return_value=Path(home)):
                text = tombstone.build_message(tombstone._old_repo_path())
            self.assertIn("cd ~/src/agent-chat-gateway && git checkout v0.5.2", text)

    def test_falls_back_to_the_curl_default_without_meta(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as home:
            with patch.object(tombstone, "OLD_RUNTIME_DIR", Path(home) / ".agent-chat-gateway"), \
                 patch("gateway.tombstone.Path.home", return_value=Path(home)):
                self.assertEqual(tombstone._old_repo_path(), "~/.agent-chat-gateway/repo")

    def test_runs_as_a_module_too(self):
        r = subprocess.run([sys.executable, "-c", "from gateway.tombstone import main; main()"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("has become AgentCoop", r.stderr)
