"""`COOP_HOME` moves the runtime directory — the base of every runtime path and,
since #182, of every relative path in `config.yaml`.

`RUNTIME_DIR` is computed once, at import, and every module binds its own copy
(the `gateway/paths.py` pattern), so the variable has to be in the environment
before `import gateway`. These tests therefore import in a subprocess with a
controlled environment rather than patching `os.environ` in-process.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.helpers import COOP_HOME_SPELLINGS, subprocess_env

REPO = Path(__file__).resolve().parents[2]
PROBE = "from gateway.paths import RUNTIME_DIR, ATTACHMENTS_DIR_DEFAULT; print(RUNTIME_DIR); print(ATTACHMENTS_DIR_DEFAULT)"


def _import_paths(coop_home: str | None, *, home: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", PROBE], cwd=REPO, capture_output=True, text=True,
        env=subprocess_env(home=home, coop_home=coop_home),
    )


class TestCoopHome(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def test_unset_means_the_users_home(self):
        r = _import_paths(None, home=str(self.tmp))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines(), [
            str(self.tmp / ".agentcoop"), str(self.tmp / ".agentcoop" / "attachments")])

    def test_empty_is_unset(self):
        r = _import_paths("", home=str(self.tmp))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines()[0], str(self.tmp / ".agentcoop"))

    def test_an_absolute_path_is_the_base_and_the_attachments_default_follows(self):
        r = _import_paths(str(self.tmp / "coop"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines(), [
            str(self.tmp / "coop"), str(self.tmp / "coop" / "attachments")])

    def test_a_tilde_is_expanded(self):
        r = _import_paths("~/elsewhere", home=str(self.tmp))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines()[0], str(self.tmp / "elsewhere"))

    def test_every_spelling_in_the_shared_table(self):
        """One rule, enforced here and in install.sh — `COOP_HOME_SPELLINGS`
        is the surface, so a new spelling is one line, run through both."""
        for value, accepted in COOP_HOME_SPELLINGS:
            with self.subTest(value=value):
                r = _import_paths(value, home=str(self.tmp))
                if accepted:
                    self.assertEqual(r.returncode, 0, r.stderr)
                    expected = value.replace("~", str(self.tmp), 1)
                    self.assertEqual(r.stdout.splitlines()[0], expected)
                else:
                    self.assertNotEqual(r.returncode, 0, value)
                    self.assertIn("COOP_HOME must be an absolute, canonical path", r.stderr)
