"""The attachment workspace, now keyed on the room rather than the watcher name.

**There are deliberately no concurrency tests here.** Keying the link on
`(connector, room_id)` means two watchers share it only when they share a connector *and*
a room — the same bot account in the same room, which CLAUDE.md records as a degenerate
case with no practical use beyond framework-level testing. In that configuration two
concurrent starts can race and one watcher fails to start, loudly.

That outcome is accepted rather than guarded, on the owner's call and for a reason worth
keeping: `impl/uniqueness` makes one watcher per room the rule and `impl/manager` moves
the lifecycle lock to `(connector, room_id)` — the same granularity as this link — so the
race disappears with its precondition. A guard would be code whose only purpose is to
survive a state that is about to become impossible, and which someone would then have to
remember to delete. Earlier revisions of this file carried exactly that: first a
`FileExistsError` tolerance, then an atomic rename with a retry loop. Both are gone.

Run with:
    uv run python -m pytest tests/unit/test_attachment_workspace.py -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gateway.core.attachment_workspace import AttachmentWorkspace


class _FakeConnector:
    def __init__(self, cache_dir: str | None) -> None:
        self._cache_dir = cache_dir

    def attachment_cache_dir(self, room_id: str) -> str | None:
        return self._cache_dir


class TestAttachmentWorkspaceSetup(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.cache = self.tmp / "global-cache" / "room1"
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.workspace = AttachmentWorkspace(_FakeConnector(str(self.cache)))

    def test_creates_the_link_named_by_the_key(self):
        got = self.workspace.setup("ROOMKEY", "room1", str(self.work))
        link = self.work / ".acg-attachments" / "ROOMKEY"
        self.assertEqual(got, str(link))
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), self.cache.resolve())

    def test_is_idempotent(self):
        first = self.workspace.setup("ROOMKEY", "room1", str(self.work))
        second = self.workspace.setup("ROOMKEY", "room1", str(self.work))
        self.assertEqual(first, second)

    def test_an_existing_link_to_the_wrong_target_is_repointed(self):
        acg = self.work / ".acg-attachments"
        acg.mkdir()
        stale = self.tmp / "stale-cache"
        stale.mkdir()
        (acg / "ROOMKEY").symlink_to(stale)

        self.workspace.setup("ROOMKEY", "room1", str(self.work))
        self.assertEqual((acg / "ROOMKEY").resolve(), self.cache.resolve())

    def test_a_real_directory_in_the_way_is_refused_rather_than_replaced(self):
        acg = self.work / ".acg-attachments"
        acg.mkdir()
        (acg / "ROOMKEY").mkdir()
        self.assertIsNone(self.workspace.setup("ROOMKEY", "room1", str(self.work)))

    def test_the_refusal_says_what_is_actually_wrong(self):
        """Asserted on the message, because the *outcome* alone cannot distinguish this
        from a lost race.

        Removing the explicit not-a-symlink branch still returns None — the bounded retry
        exhausts and the end-state check refuses — so a test on the return value alone
        passes either way. What degrades is diagnosability: an operator told "could not
        establish after a retry" looks for a concurrent writer, when the real cause is a
        stray directory they can simply remove. That difference is the whole value of the
        branch, so it is what gets asserted.
        """
        acg = self.work / ".acg-attachments"
        acg.mkdir()
        (acg / "ROOMKEY").mkdir()
        with self.assertLogs(
            "coop.core.attachment_workspace", level="WARNING"
        ) as logs:
            self.workspace.setup("ROOMKEY", "room1", str(self.work))
        self.assertTrue(
            any("not a symlink" in line for line in logs.output),
            f"the refusal did not name the cause: {logs.output}",
        )

    def test_a_connector_without_attachment_support_gets_no_workspace(self):
        workspace = AttachmentWorkspace(_FakeConnector(None))
        self.assertIsNone(workspace.setup("ROOMKEY", "room1", str(self.work)))
        self.assertFalse((self.work / ".acg-attachments").exists())

    def test_a_hostile_key_cannot_escape_the_workspace(self):
        """`resolve_under` is what stands between an unexpected key and the filesystem."""
        for key in ("../escape", "/abs", "..", "a/b"):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    self.workspace.setup(key, "room1", str(self.work))


if __name__ == "__main__":
    unittest.main()
