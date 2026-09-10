"""Tests for gateway/core/adapter_utils.py — build_attachment_prompt().

Run with:
    uv run python -m pytest tests/test_adapter_utils.py -v
"""

from __future__ import annotations

import unittest


class TestBuildAttachmentPrompt(unittest.TestCase):
    """build_attachment_prompt() injects file paths into the prompt."""

    def _build(self, prompt, attachments, cwd=None, instruction=None):
        from gateway.core.adapter_utils import build_attachment_prompt
        kwargs = {}
        if instruction is not None:
            kwargs["instruction"] = instruction
        return build_attachment_prompt(prompt, attachments, cwd, **kwargs)

    # ── No-op cases ───────────────────────────────────────────────────────────

    def test_none_attachments_returns_prompt_unchanged(self):
        result = self._build("Hello", None)
        self.assertEqual(result, "Hello")

    def test_empty_list_returns_prompt_unchanged(self):
        result = self._build("Hello", [])
        self.assertEqual(result, "Hello")

    def test_empty_prompt_with_no_attachments(self):
        result = self._build("", None)
        self.assertEqual(result, "")

    # ── Single attachment ─────────────────────────────────────────────────────

    def test_single_attachment_no_cwd_uses_full_path(self):
        """Without working_directory, the full path is shown as the label."""
        result = self._build("prompt", ["/tmp/docs/report.pdf"])
        self.assertIn("Attached:", result)
        self.assertIn("report.pdf", result)
        self.assertIn("/tmp/docs/report.pdf", result)

    def test_single_attachment_with_cwd_shows_relative_path(self):
        """When the file is inside working_directory, a relative path is shown."""
        result = self._build(
            "prompt",
            ["/workspace/project/data/file.txt"],
            cwd="/workspace/project",
        )
        self.assertIn("Attached:", result)
        self.assertIn("file.txt", result)
        self.assertIn("data/file.txt", result)
        # Full path should NOT appear when relative path is available
        self.assertNotIn("/workspace/project/data/file.txt", result)

    def test_single_attachment_outside_cwd_falls_back_to_absolute(self):
        """When the file is outside cwd, the absolute path is used."""
        result = self._build(
            "prompt",
            ["/other/path/report.pdf"],
            cwd="/workspace/project",
        )
        self.assertIn("/other/path/report.pdf", result)

    def test_default_instruction_is_use_read_tool(self):
        """The default hint tells the agent to use the Read tool."""
        result = self._build("prompt", ["/tmp/file.txt"])
        self.assertIn("Read tool", result)

    def test_custom_instruction(self):
        """A custom instruction replaces the default hint."""
        result = self._build(
            "prompt",
            ["/tmp/file.txt"],
            instruction="open it with the editor",
        )
        self.assertIn("open it with the editor", result)
        self.assertNotIn("Read tool", result)

    # ── Multiple attachments ──────────────────────────────────────────────────

    def test_multiple_attachments_each_on_own_line(self):
        """Each attachment produces a separate [Attached: ...] line."""
        result = self._build(
            "prompt",
            ["/tmp/a.txt", "/tmp/b.pdf", "/tmp/c.png"],
        )
        lines = result.split("\n")
        attached_lines = [line for line in lines if line.startswith("[Attached:")]
        self.assertEqual(len(attached_lines), 3)

    def test_multiple_attachments_all_filenames_present(self):
        result = self._build(
            "prompt",
            ["/workspace/img.png", "/workspace/report.pdf"],
            cwd="/workspace",
        )
        self.assertIn("img.png", result)
        self.assertIn("report.pdf", result)

    # ── Prompt structure ──────────────────────────────────────────────────────

    def test_original_prompt_preserved_as_prefix(self):
        """The original prompt text comes before the attachment notes."""
        result = self._build("What does this file say?", ["/tmp/file.txt"])
        self.assertTrue(
            result.startswith("What does this file say?"),
            f"Expected prompt at start, got: {result[:80]!r}",
        )

    def test_result_stripped_of_leading_trailing_whitespace(self):
        """The final result has no leading/trailing whitespace."""
        result = self._build("  hello  ", ["/tmp/file.txt"])
        self.assertEqual(result, result.strip())

    def test_empty_prompt_with_attachment(self):
        """An empty prompt with attachments returns only the attachment lines."""
        result = self._build("", ["/tmp/data.csv"])
        self.assertIn("Attached:", result)
        # No leading newline
        self.assertFalse(result.startswith("\n"))

    # ── Annotation format ─────────────────────────────────────────────────────

    def test_annotation_format_brackets_arrow(self):
        """Each annotation follows the [Attached: name → path — instruction] format."""
        result = self._build("p", ["/tmp/report.pdf"])
        # Should match [Attached: filename → label — instruction]
        self.assertRegex(result, r"\[Attached: report\.pdf → .+ — .+\]")

    def test_filename_extracted_correctly_from_nested_path(self):
        """Only the filename (not the full path) appears as the first part."""
        result = self._build("p", ["/very/deeply/nested/dir/myfile.txt"])
        # The name part before → should be just "myfile.txt"
        import re
        match = re.search(r"\[Attached: (.+?) →", result)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "myfile.txt")


# ── Tests: ts_to_float (Q4) ───────────────────────────────────────────────────


class TestTsToFloat(unittest.TestCase):
    """Q4: ts_to_float — single source of truth for timestamp parsing."""

    def _f(self, ts):
        from gateway.core.adapter_utils import ts_to_float
        return ts_to_float(ts)

    def test_numeric_string_parsed(self):
        self.assertEqual(self._f("1711234567890"), 1711234567890.0)

    def test_float_string_parsed(self):
        self.assertAlmostEqual(self._f("1711234567.123"), 1711234567.123)

    def test_none_returns_none(self):
        self.assertIsNone(self._f(None))

    def test_empty_string_returns_none(self):
        self.assertIsNone(self._f(""))

    def test_non_numeric_returns_none(self):
        self.assertIsNone(self._f("not-a-ts"))

    def test_iso8601_returns_none(self):
        # ISO-8601 strings are not parseable as float
        self.assertIsNone(self._f("2024-01-01T00:00:00Z"))


# ── Tests: ts_gt (Q4) ─────────────────────────────────────────────────────────


class TestTsGt(unittest.TestCase):
    """Q4: ts_gt — numeric timestamp comparison with lexicographic fallback."""

    def _gt(self, a, b):
        from gateway.core.adapter_utils import ts_gt
        return ts_gt(a, b)

    def test_larger_numeric_ts_is_greater(self):
        self.assertTrue(self._gt("1711234567891", "1711234567890"))

    def test_equal_numeric_ts_is_not_greater(self):
        self.assertFalse(self._gt("1711234567890", "1711234567890"))

    def test_smaller_numeric_ts_is_not_greater(self):
        self.assertFalse(self._gt("1711234567889", "1711234567890"))

    def test_numeric_takes_precedence_over_string_length(self):
        # "9" < "10" lexicographically but 9 < 10 numerically —
        # ts_gt must use numeric comparison.
        self.assertFalse(self._gt("9", "10"))
        self.assertTrue(self._gt("10", "9"))

    def test_non_numeric_falls_back_to_lexicographic(self):
        # When both values can't be parsed as float, fall back to str comparison
        self.assertTrue(self._gt("b", "a"))
        self.assertFalse(self._gt("a", "b"))

    def test_mixed_numeric_and_non_numeric_falls_back(self):
        # One side is parseable, the other is not — falls back to str comparison
        # (ts_to_float returns None for non-numeric side)
        self.assertFalse(self._gt("100", "not-a-ts"))  # "100" < "not-a-ts" lexicographically

    def test_float_strings_compared_correctly(self):
        self.assertTrue(self._gt("1711234567.9", "1711234567.1"))


# ── Tests: ts_ms_to_iso_local ─────────────────────────────────────────────────


class TestTsMsToIsoLocal(unittest.TestCase):
    """ts_ms_to_iso_local — epoch-ms to local ISO 8601, kept round-trippable."""

    def _iso(self, ts_ms, tz="UTC"):
        from gateway.core.adapter_utils import ts_ms_to_iso_local
        return ts_ms_to_iso_local(ts_ms, tz)

    def test_epoch_ms_converted_to_iso_with_offset(self):
        # 2026-04-24T10:30:00 UTC in epoch ms
        result = self._iso("1777026600000", tz="UTC")
        self.assertEqual(result, "2026-04-24T10:30:00+00:00")

    def test_timezone_offset_applied(self):
        result = self._iso("1777026600000", tz="America/Los_Angeles")
        self.assertEqual(result, "2026-04-24T03:30:00-07:00")

    def test_none_ts_returns_none(self):
        self.assertIsNone(self._iso(None))

    def test_unparseable_ts_returns_none(self):
        self.assertIsNone(self._iso("not-a-ts"))

    def test_unknown_timezone_returns_none(self):
        self.assertIsNone(self._iso("1777026600000", tz="Not/A_Zone"))

    def test_result_is_fromisoformat_round_trippable(self):
        """The output must stay parseable by datetime.fromisoformat() — it is
        echoed back by agents into fetch-history --before/--after and must
        never carry non-ISO decoration (e.g. an embedded weekday)."""
        from datetime import datetime
        result = self._iso("1777026600000", tz="UTC")
        parsed = datetime.fromisoformat(result)  # raises if not round-trippable
        self.assertEqual(parsed.isoformat(timespec="seconds"), result)


# ── Tests: weekday_abbrev ─────────────────────────────────────────────────────


class TestWeekdayAbbrev(unittest.TestCase):
    """weekday_abbrev — display-only day-of-week label for the ``day:`` header field.

    See AgentCoop#53: agents infer weekday from a bare date
    unreliably, so the gateway precomputes it instead.
    """

    def _day(self, ts_iso):
        from gateway.core.adapter_utils import weekday_abbrev
        return weekday_abbrev(ts_iso)

    def test_known_dates_map_to_correct_weekday(self):
        # 2026-04-24 is a Friday; 2026-04-27 (the following Monday) confirms
        # the week rolls over correctly.
        self.assertEqual(self._day("2026-04-24T10:30:00+00:00"), "Fri")
        self.assertEqual(self._day("2026-04-25T00:00:00+00:00"), "Sat")
        self.assertEqual(self._day("2026-04-26T23:59:59+00:00"), "Sun")
        self.assertEqual(self._day("2026-04-27T00:00:00+00:00"), "Mon")

    def test_timezone_offset_does_not_affect_lookup(self):
        # Same instant, different offsets — weekday is read from the local
        # wall-clock date already baked into the ISO string, not recomputed.
        self.assertEqual(self._day("2026-04-24T23:00:00-07:00"), "Fri")

    def test_none_returns_none(self):
        self.assertIsNone(self._day(None))

    def test_empty_string_returns_none(self):
        self.assertIsNone(self._day(""))

    def test_unparseable_returns_none(self):
        self.assertIsNone(self._day("not-a-timestamp"))

    def test_result_is_always_english_abbreviation(self):
        """Uses a fixed table (not strftime) so the locale of the host process
        can never change the label agents are told to expect."""
        for ts, expected in [
            ("2026-04-20T00:00:00+00:00", "Mon"),
            ("2026-04-21T00:00:00+00:00", "Tue"),
            ("2026-04-22T00:00:00+00:00", "Wed"),
            ("2026-04-23T00:00:00+00:00", "Thu"),
            ("2026-04-24T00:00:00+00:00", "Fri"),
            ("2026-04-25T00:00:00+00:00", "Sat"),
            ("2026-04-26T00:00:00+00:00", "Sun"),
        ]:
            self.assertEqual(self._day(ts), expected)


if __name__ == "__main__":
    unittest.main()
