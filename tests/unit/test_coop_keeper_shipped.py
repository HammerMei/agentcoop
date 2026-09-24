"""The shipped coop-keeper directory (`agents/coop-keeper/`) is consistent with
itself and with the CLIs it drives.

These are enumeration tests: they walk what is actually shipped rather than
remembering to check each file, so a skill that gains a command, a manifest
that forgets a file, or a permission rule that drifts from what the skills run
fails here instead of in a review or on an operator's machine.

Design: docs/design/coop-keeper-design.md §3.1 (layout, pairing), §3.9
(permissions derived from the skills), §3.11 (skills), and the manifest rule
`coop upgrade` follows (`gateway/upgrade.py`, `_sync_keeper_dir`).
"""

from __future__ import annotations

import json
import re
import shlex
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
KEEPER = REPO / "agents" / "coop-keeper"
SKILLS_DIR = KEEPER / ".claude" / "skills"
SKILL_NAMES = ("coop-bootstrap", "coop-add-bot", "coop-remove-bot", "coop-apply-config")

CREDENTIAL_PATHS = (
    "~/.agentcoop/config.yaml",
    "~/.agentcoop/admin-profiles.yaml",
    "~/.agentcoop/.config-backups/**",
)

# Programs the skills may run from the shell. An inline `code span` whose first
# word is one of these is a command mention; anything else in backticks is a
# file name, a JSON key, a flag.
SHELL_PROGRAMS = frozenset({
    "coop", "coop-provision", "mkdir", "rmdir", "mktemp", "openssl", "rm", "ls",
    "date", "which", "cat", "cp", "mv", "sed", "head", "tail", "python3", "curl",
})
_FENCE_RE = re.compile(r"^\s*```(\w+)?\s*$")
_INLINE_RE = re.compile(r"`([^`\n]+)`")
PROSE_FENCES = {"yaml", "text", "json"}


def _skill_files() -> dict[str, Path]:
    return {d.name: d / "SKILL.md" for d in sorted(SKILLS_DIR.iterdir()) if d.is_dir()}


def _frontmatter(text: str) -> dict:
    assert text.startswith("---\n"), "SKILL.md must start with YAML frontmatter"
    end = text.index("\n---\n", 4)
    return yaml.safe_load(text[4:end]) or {}


def _commands(text: str) -> list[tuple[str, bool]]:
    """Every shell command a skill shows, as (command, strict).

    Fenced blocks not tagged as prose (yaml/text/json) are commands the keeper
    is told to run verbatim — strict. Inline code spans are strict when they
    carry a flag or four or more words; a two- or three-word span
    (`coop status`, `coop-provision init`) is a mention and is only checked
    against the allow rules.
    """
    out: list[tuple[str, bool]] = []
    in_fence = False
    prose = False
    for line in text.splitlines():
        m = _FENCE_RE.match(line)
        if m:
            in_fence = not in_fence
            prose = in_fence and (m.group(1) or "") in PROSE_FENCES
            continue
        if in_fence:
            if prose:
                continue
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            out.append((stripped, True))
            continue
        for span in _INLINE_RE.findall(line):
            words = span.split()
            if not words or words[0] not in SHELL_PROGRAMS:
                continue
            strict = any(w.startswith("--") for w in words) or len(words) >= 4
            out.append((span.strip(), strict))
    return out


def _manifest() -> dict[str, str]:
    return yaml.safe_load((KEEPER / "manifest.yaml").read_text())


class TestLayout(unittest.TestCase):
    def test_claude_md_is_exactly_the_import_line(self):
        self.assertEqual((KEEPER / "CLAUDE.md").read_text(), "@AGENTS.md\n")

    def test_agents_md_never_uses_at_file_imports(self):
        """OpenCode does not expand `@file`; the text must not lean on it."""
        text = (KEEPER / "AGENTS.md").read_text()
        self.assertNotRegex(text, r"(^|\s)@[\w./-]+\.md\b")

    def test_no_opencode_directory_is_shipped(self):
        """OpenCode v1 writes package.json/node_modules into an existing .opencode/."""
        self.assertFalse((KEEPER / ".opencode").exists())

    def test_the_four_skills_exist_and_no_others(self):
        self.assertEqual(tuple(sorted(_skill_files())), tuple(sorted(SKILL_NAMES)))

    def test_skill_frontmatter_is_name_and_description_matching_the_directory(self):
        """v1 OpenCode keys a skill by `name`, v2 by the directory; Claude Code
        reads both. Name must equal the directory and nothing else may be set."""
        for dirname, path in _skill_files().items():
            fm = _frontmatter(path.read_text())
            self.assertEqual(set(fm), {"name", "description"}, dirname)
            self.assertEqual(fm["name"], dirname)
            self.assertTrue(fm["description"].strip(), f"{dirname}: empty description")
            self.assertRegex(fm["name"], r"^[a-z0-9]+(-[a-z0-9]+)*$")

    def test_public_artifacts_carry_no_persona_voice(self):
        for path in [KEEPER / "AGENTS.md", *_skill_files().values()]:
            text = path.read_text()
            self.assertNotRegex(text, r"老哥|老妹|哇塞|老鐵", path.name)


class TestManifest(unittest.TestCase):
    def test_every_shipped_file_is_under_exactly_one_in_use_entry(self):
        manifest = _manifest()
        in_use = [Path(p) for p, st in manifest.items() if st == "in-use"]
        for f in KEEPER.rglob("*"):
            if not f.is_file():
                continue
            rel = f.relative_to(KEEPER)
            covering = [p for p in in_use if rel == p or p in rel.parents]
            self.assertEqual(len(covering), 1, f"{rel}: covered by {covering}")

    def test_statuses_are_only_in_use_or_obsolete_and_no_path_is_both(self):
        manifest = _manifest()
        self.assertTrue(set(manifest.values()) <= {"in-use", "obsolete"}, manifest)
        # YAML mapping keys are unique by construction; the check that matters
        # is that no obsolete path is also inside an in-use directory.
        in_use_dirs = [Path(p) for p, st in manifest.items() if st == "in-use"]
        for p, st in manifest.items():
            if st == "obsolete":
                self.assertFalse(
                    any(d in Path(p).parents for d in in_use_dirs),
                    f"{p} is obsolete but inside an in-use directory",
                )

    def test_no_obsolete_path_is_still_shipped(self):
        for p, st in _manifest().items():
            if st == "obsolete":
                self.assertFalse((KEEPER / p).exists(), f"{p} is obsolete yet shipped")

    def test_manifest_lists_itself(self):
        self.assertEqual(_manifest().get("manifest.yaml"), "in-use")


class TestPermissionFiles(unittest.TestCase):
    def setUp(self):
        self.settings = json.loads((KEEPER / ".claude" / "settings.json").read_text())
        self.opencode = json.loads((KEEPER / "opencode.json").read_text())

    def test_both_files_pin_the_default_agent_to_the_cli_builtin(self):
        """An operator whose CLI defaults to a persona agent of their own would
        otherwise run the keeper *as* that persona. The keeper is its own agent."""
        self.assertEqual(self.settings.get("agent"), "")
        self.assertEqual(self.opencode.get("default_agent"), "build")

    def test_claude_settings_deny_the_credential_files(self):
        deny = set(self.settings["permissions"]["deny"])
        for p in CREDENTIAL_PATHS:
            self.assertIn(f"Read({p})", deny)

    def test_opencode_uses_the_v1_permission_object(self):
        """v2 migrates the v1 object; v1 hard-fails on a v2 `permissions` array."""
        self.assertIn("permission", self.opencode)
        self.assertNotIn("permissions", self.opencode)
        perm = self.opencode["permission"]
        # external_directory is matched against `<dir>/*`, so it can only allow
        # or deny directories — the paths the keeper writes under.
        ext = perm["external_directory"]
        self.assertEqual(ext.get("~/.agentcoop/agents/*"), "allow")
        self.assertEqual(ext.get("~/.agentcoop/plan.lock/*"), "allow")
        self.assertTrue(all(v == "allow" for v in ext.values()), ext)
        # read/edit rules see the path relative to the working directory
        # (`../../.agentcoop/config.yaml`), so a `*/` prefix is what matches.
        for tool in ("read", "edit"):
            rules = perm[tool]
            for p in ("*/.agentcoop/config.yaml", "*/.agentcoop/admin-profiles.yaml",
                      "*/.agentcoop/.config-backups/*"):
                self.assertEqual(rules.get(p), "deny", (tool, p))
        self.assertNotIn("bash", perm, "bash stays at OpenCode's default")

    def test_every_command_a_skill_runs_matches_a_claude_allow_rule(self):
        """§3.9: the allow list is derived from what the skills execute. A skill
        that gains a command adds a rule here in the same change."""
        allow = self.settings["permissions"]["allow"]
        bash_rules = [r[5:-1] for r in allow if r.startswith("Bash(")]

        def allowed(cmd: str) -> bool:
            for rule in bash_rules:
                if rule.endswith(" *"):
                    prefix = rule[:-2]
                    if cmd == prefix or cmd.startswith(prefix + " "):
                        return True
                elif cmd == rule:
                    return True
            return False

        for dirname, path in _skill_files().items():
            for cmd, _strict in _commands(path.read_text()):
                self.assertTrue(allowed(cmd), f"{dirname}: no allow rule for `{cmd}`")

    def test_no_skill_command_is_a_compound(self):
        """Claude Code matches permissions per sub-command; a chained line would
        need every part allowed and reads worse in a plan."""
        for dirname, path in _skill_files().items():
            for cmd, _strict in _commands(path.read_text()):
                self.assertNotRegex(cmd, r"&&|\|\||;|\|(?!\|)", f"{dirname}: `{cmd}`")


class TestSkillCommandsParse(unittest.TestCase):
    """Every `coop …` / `coop-provision …` line in a skill is accepted by the real
    argparse tree, so a renamed flag fails here, not in an operator's session."""

    def _parse(self, cmd: str) -> None:
        import io
        from contextlib import redirect_stderr, redirect_stdout

        # `<placeholder>` stands for a value the keeper fills in; `…` likewise.
        argv = [re.sub(r"[<>]", "", a) for a in shlex.split(cmd)]
        if argv[0] == "coop":
            from gateway.cli import _build_parser
            parse = _build_parser().parse_args
        elif argv[0] == "coop-provision":
            # `init`/`profiles` take the place of the profile word and are
            # routed before argparse; parse_argv is the entry main() uses.
            from gateway.admin.cli import parse_argv
            parse = parse_argv
        else:
            return
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            try:
                parse(argv[1:])
            except SystemExit as e:
                self.fail(f"`{cmd}` does not parse (exit {e.code})")

    def test_all_skill_commands_parse(self):
        seen = 0
        for dirname, path in _skill_files().items():
            for cmd, strict in _commands(path.read_text()):
                if strict and cmd.split()[0] in ("coop", "coop-provision"):
                    seen += 1
                    with self.subTest(skill=dirname, cmd=cmd):
                        self._parse(cmd)
        self.assertGreaterEqual(seen, 12, "the extractor found too few commands to be trusted")


if __name__ == "__main__":
    unittest.main()
