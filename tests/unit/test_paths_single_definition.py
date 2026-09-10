"""The runtime directory has one definition: gateway/paths.py (#154).

Before the rename the expression `Path.home() / ".agent-chat-gateway"` was typed
in eight modules, each with a comment explaining why its copy was needed. A
module that wants the path imports `RUNTIME_DIR` from `gateway.paths` and binds
it as its own attribute (so tests keep patching the module they exercise); it
does not spell the directory name again. This walks every module under
gateway/ and fails on a second spelling — of the new name or the old one.
"""

import re
import unittest
from pathlib import Path

from gateway.paths import ATTACHMENTS_DIR_DEFAULT, RUNTIME_DIR, RUNTIME_DIR_NAME

ROOT = Path(__file__).resolve().parents[2] / "gateway"

# A second DEFINITION: the directory name used to build a path (Path.home(),
# expanduser, os.path.join) or the attachments default typed out again. Help
# text and docstrings that merely mention `~/.agentcoop/...` are prose about the
# directory, not a definition of it, and are left alone.
SPELLINGS = re.compile(
    r'(?:Path\.home\(\)|expanduser\(|os\.path\.join\(|environ)[^\n]*\.(?:agentcoop|agent-chat-gateway)\b'
    r'|["\']~/\.(?:agentcoop|agent-chat-gateway)/attachments["\']'
)


class TestRuntimeDirHasOneDefinition(unittest.TestCase):
    def test_the_one_definition_is_the_new_name(self):
        self.assertEqual(RUNTIME_DIR_NAME, ".agentcoop")
        self.assertEqual(RUNTIME_DIR, Path.home() / ".agentcoop")
        self.assertEqual(ATTACHMENTS_DIR_DEFAULT, "~/.agentcoop/attachments")

    def test_no_other_module_spells_the_runtime_directory(self):
        offenders = []
        for py in sorted(ROOT.rglob("*.py")):
            if py.name == "paths.py" and py.parent == ROOT:
                continue
            if py.name == "tombstone.py":
                continue  # its whole job is to name the OLD directory for the user
            for lineno, line in enumerate(py.read_text().splitlines(), 1):
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    continue  # prose about the directory is fine; a literal is not
                if SPELLINGS.search(line):
                    offenders.append(f"{py.relative_to(ROOT.parent)}:{lineno}: {stripped}")
        self.assertEqual(offenders, [], "\n".join(
            ["The runtime directory is spelled outside gateway/paths.py:"] + offenders
            + ["Import RUNTIME_DIR / ATTACHMENTS_DIR_DEFAULT from gateway.paths instead."]))

    def test_the_old_name_is_not_read_by_any_module(self):
        """v1 does not migrate from, fall back to, or even look at the old
        directory (owner's ruling on #154). Only the tombstone may name it."""
        hits = []
        for py in sorted(ROOT.rglob("*.py")):
            if py.name == "tombstone.py":
                continue
            for lineno, line in enumerate(py.read_text().splitlines(), 1):
                if ".agent-chat-gateway" in line:
                    hits.append(f"{py.relative_to(ROOT.parent)}:{lineno}")
        self.assertEqual(hits, [])
