"""Tests for gateway.core.prompt_builder.build_prompt().

Extracted from MessageProcessor to make prompt logic independently testable.
"""

import unittest

from gateway.config import WatcherConfig
from gateway.core.prompt_builder import (
    build_catchup_prompt,
    build_prompt,
    build_system_header,
)


class TestBuildSystemHeader(unittest.TestCase):
    """build_system_header() — pure, no I/O. Extracted from context_injector.py
    for issue #52 (durable system prompt via --append-system-prompt-file)."""

    def _wc(self) -> WatcherConfig:
        return WatcherConfig(
            name="my-watcher",
            connector="rc-home",
            room="general",
            agent="default",
        )

    def test_identity_fields_present(self):
        header = build_system_header(self._wc(), "")
        self.assertIn("## Coop Session Identity", header)
        self.assertIn("my-watcher", header)
        self.assertIn("general", header)
        self.assertIn("rc-home", header)

    def test_never_empty_even_without_username(self):
        """The identity block is unconditional — never empty, even with no username."""
        header = build_system_header(self._wc(), "")
        self.assertTrue(header)
        self.assertIn("## Coop Session Identity", header)

    def test_addressing_rules_absent_when_username_falsy(self):
        header = build_system_header(self._wc(), "")
        self.assertNotIn("## Multi-Agent Addressing", header)
        self.assertNotIn("Your username", header)

    def test_addressing_rules_present_when_username_truthy(self):
        header = build_system_header(self._wc(), "acg_bot")
        self.assertIn("## Multi-Agent Addressing", header)
        self.assertIn("@acg_bot", header)
        self.assertIn("to: me", header)
        self.assertIn("to: @all", header)
        self.assertIn("broader fan-out is intentional", header)
        self.assertIn("ONLY `<end-of-agent-chain>`", header)
        self.assertIn("priority responders", header)

    def test_identity_precedes_addressing(self):
        header = build_system_header(self._wc(), "bot")
        self.assertLess(
            header.index("## Coop Session Identity"),
            header.index("## Multi-Agent Addressing"),
        )

    def test_pure_no_side_effects_deterministic(self):
        """Same inputs always produce the same output (no I/O, no randomness)."""
        wc = self._wc()
        self.assertEqual(build_system_header(wc, "bot"), build_system_header(wc, "bot"))


class TestBuildPrompt(unittest.TestCase):

    def test_text_only(self):
        self.assertEqual(build_prompt("hello", ""), "hello")

    def test_text_with_prefix(self):
        self.assertEqual(build_prompt("hello", "from: alice"), "from: alice hello")

    def test_text_with_none_prefix(self):
        """Empty/falsy prefix produces text only (no leading space)."""
        self.assertEqual(build_prompt("hello", ""), "hello")

    def test_text_with_warnings(self):
        result = build_prompt("hello", "", ["warn1", "warn2"])
        self.assertEqual(result, "hello\nwarn1\nwarn2")

    def test_prefix_and_warnings(self):
        result = build_prompt("hello", "from: alice", ["attachment too large"])
        self.assertEqual(result, "from: alice hello\nattachment too large")

    def test_empty_warnings_list_ignored(self):
        result = build_prompt("hello", "pfx", [])
        self.assertEqual(result, "pfx hello")

    def test_none_warnings_ignored(self):
        result = build_prompt("hello", "pfx", None)
        self.assertEqual(result, "pfx hello")

    def test_whitespace_stripped_from_prefix_join(self):
        """Prefix with trailing space doesn't produce double space."""
        result = build_prompt("hello", "prefix:")
        self.assertEqual(result, "prefix: hello")

    def test_empty_text_with_prefix(self):
        """Edge case: empty text body with a prefix."""
        result = build_prompt("", "from: alice")
        self.assertEqual(result, "from: alice")


class TestBuildCatchupPrompt(unittest.TestCase):

    def test_structure_contains_header_and_footer(self):
        result = build_catchup_prompt(["line1"], "anchor text")
        self.assertIn("[CATCH-UP:", result)
        self.assertIn("[END CATCH-UP]", result)
        self.assertIn("Latest message (respond to this):", result)

    def test_history_lines_indented(self):
        result = build_catchup_prompt(["msg A", "msg B"], "anchor")
        self.assertIn("  msg A", result)
        self.assertIn("  msg B", result)

    def test_anchor_appears_after_end_catchup(self):
        result = build_catchup_prompt(["history"], "the anchor")
        end_idx = result.index("[END CATCH-UP]")
        anchor_idx = result.index("the anchor")
        self.assertGreater(anchor_idx, end_idx)

    def test_anchor_prompt_preserved_verbatim(self):
        anchor = "[RC #room | from: alice] do the thing\nwarn: file too large"
        result = build_catchup_prompt(["hist"], anchor)
        self.assertIn(anchor, result)

    def test_warnings_in_anchor_not_history(self):
        """Warnings should be included in anchor_prompt, not in history lines."""
        history = ["[RC #room | from: bob] msg without warnings"]
        anchor = "[RC #room | from: alice] do it\nwarn: too large"
        result = build_catchup_prompt(history, anchor)
        # Warning appears
        self.assertIn("warn: too large", result)
        # History line appears without warning
        self.assertIn("msg without warnings", result)

    def test_single_history_entry(self):
        result = build_catchup_prompt(["only one"], "anchor")
        self.assertIn("  only one", result)
        self.assertIn("anchor", result)

    def test_multiple_history_entries_order_preserved(self):
        result = build_catchup_prompt(["first", "second", "third"], "anchor")
        self.assertLess(result.index("first"), result.index("second"))
        self.assertLess(result.index("second"), result.index("third"))
        self.assertLess(result.index("third"), result.index("anchor"))

    def test_empty_history_list(self):
        """Edge case: empty history (should not normally happen in _process_batch,
        but the function itself should handle it gracefully."""
        result = build_catchup_prompt([], "anchor only")
        self.assertIn("[CATCH-UP:", result)
        self.assertIn("[END CATCH-UP]", result)
        self.assertIn("anchor only", result)


if __name__ == "__main__":
    unittest.main()
