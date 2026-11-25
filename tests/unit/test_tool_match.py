"""Tests for gateway/core/tool_match.py.

Covers:
  - _normalize_path: path traversal prevention, relative paths, absolute paths
  - matches_rule: tool name regex, params regex, case insensitivity, params=None
  - extract_bash_subcommands: compound commands, heredoc redirects, file redirects
    (targets are parameter strings of their own, fd dups and sinks are not),
    command / process substitutions (recursed into, nested commands must match too)
  - get_param_strings_for_claude: Bash, file tools, WebFetch, unknown tools
  - get_param_strings_for_opencode: patterns list, empty patterns fallback
  - all_params_match_any: all-match semantics, partial match fails, empty allow-list
"""

from __future__ import annotations

import pathlib
import unittest

from gateway.config import ToolRule
from gateway.core.config import AgentConfig
from gateway.core.tool_match import (
    _normalize_path,
    all_params_match_any,
    extract_bash_subcommands,
    get_param_strings_for_claude,
    get_param_strings_for_opencode,
    matches_any,
    matches_rule,
)

# ── _normalize_path ─────────────────────────────────────────────────────────


class TestNormalizePath(unittest.TestCase):
    """_normalize_path prevents traversal attacks and resolves relative paths."""

    def test_path_traversal_neutralized(self):
        """/project/../../../etc/passwd must normalize to /etc/passwd, NOT match /project/.*."""
        result = _normalize_path("/project/../../../etc/passwd", "")
        # normpath collapses the traversal
        self.assertEqual(result, "/etc/passwd")
        # Verify it would NOT match a /project/ allow rule
        rule = ToolRule(tool="Read", params="/project/.*")
        self.assertFalse(matches_rule(rule, "Read", result))

    def test_path_traversal_relative_with_working_dir(self):
        """Relative traversal resolved against working_directory must normalize correctly."""
        result = _normalize_path("../../etc/passwd", "/home/user/project")
        # /home/user/project/../../etc/passwd → /home/user/../../etc/passwd → /etc/passwd
        self.assertEqual(result, "/home/etc/passwd")
        rule = ToolRule(tool="Read", params="/home/user/project/.*")
        self.assertFalse(matches_rule(rule, "Read", result))

    def test_absolute_path_unchanged(self):
        """An already-absolute path is returned as-is (after normpath)."""
        result = _normalize_path("/src/main.py", "/project")
        self.assertEqual(result, "/src/main.py")

    def test_relative_path_resolved_against_working_dir(self):
        """A relative path is joined with working_directory."""
        result = _normalize_path("src/main.py", "/project")
        self.assertEqual(result, "/project/src/main.py")

    def test_empty_value_normalized(self):
        """Empty path is normalized to '.'."""
        result = _normalize_path("", "")
        self.assertEqual(result, ".")

    def test_empty_value_with_working_dir(self):
        """Empty path + working_directory: joined then normalized."""
        # os.path.join("/project", "") gives "/project/"  → normpath → "/project"
        result = _normalize_path("", "/project")
        self.assertIn("project", result)

    def test_double_dot_in_middle(self):
        """`/project/sub/../config.py` normalizes to `/project/config.py`."""
        result = _normalize_path("/project/sub/../config.py", "")
        self.assertEqual(result, "/project/config.py")

    def test_path_traversal_does_not_match_project_rule(self):
        """A traversal that ends outside /project must NOT match a /project/.* rule."""
        dangerous = "/project/../../../etc/shadow"
        normalized = _normalize_path(dangerous, "")
        rule = ToolRule(tool="Write", params="/project/.*")
        self.assertFalse(matches_rule(rule, "Write", normalized))


# ── matches_rule ─────────────────────────────────────────────────────────────


class TestMatchesRule(unittest.TestCase):
    """matches_rule: tool regex fullmatch + params regex fullmatch."""

    def test_exact_tool_name_match(self):
        rule = ToolRule(tool="Read")
        self.assertTrue(matches_rule(rule, "Read", "/any/path"))

    def test_tool_name_case_insensitive(self):
        rule = ToolRule(tool="read")
        self.assertTrue(matches_rule(rule, "READ", "/any"))
        self.assertTrue(matches_rule(rule, "Read", "/any"))

    def test_tool_wildcard_pattern(self):
        rule = ToolRule(tool="mcp__rocketchat__.*")
        self.assertTrue(matches_rule(rule, "mcp__rocketchat__send_message", "{}"))
        self.assertFalse(matches_rule(rule, "mcp__slack__send_message", "{}"))

    def test_params_none_matches_any_param(self):
        """params=None means tool name is the only criterion."""
        rule = ToolRule(tool="Bash")
        self.assertTrue(matches_rule(rule, "Bash", "rm -rf /"))
        self.assertTrue(matches_rule(rule, "Bash", "ls -la"))

    def test_params_pattern_matched(self):
        rule = ToolRule(tool="Read", params="/project/.*")
        self.assertTrue(matches_rule(rule, "Read", "/project/src/main.py"))
        self.assertFalse(matches_rule(rule, "Read", "/etc/passwd"))

    def test_params_fullmatch_not_search(self):
        """params must fullmatch — a partial prefix match is not enough."""
        rule = ToolRule(tool="Bash", params="ls")
        # "ls -la" is not a fullmatch for "ls" (requires the entire string to match)
        self.assertFalse(matches_rule(rule, "Bash", "ls -la"))
        self.assertTrue(matches_rule(rule, "Bash", "ls"))

    def test_params_dot_star_matches_everything(self):
        rule = ToolRule(tool="Bash", params=".*")
        self.assertTrue(matches_rule(rule, "Bash", "rm -rf /"))

    def test_tool_name_partial_match_fails(self):
        """Regex fullmatch: 'Rea' must NOT match 'Read'."""
        rule = ToolRule(tool="Rea")
        self.assertFalse(matches_rule(rule, "Read", "/file"))


# ── extract_bash_subcommands ─────────────────────────────────────────────────


class TestExtractBashSubcommands(unittest.TestCase):
    """extract_bash_subcommands handles simple commands, compound commands, and heredocs."""

    def test_simple_command_returned_as_is(self):
        result = extract_bash_subcommands("ls -la")
        self.assertEqual(result, ["ls -la"])

    def test_compound_and_splits_into_two(self):
        result = extract_bash_subcommands("echo hi && rm -rf /")
        self.assertIn("echo hi", result)
        self.assertTrue(any("rm -rf /" in r for r in result))
        self.assertEqual(len(result), 2)

    def test_semicolon_splits_into_two(self):
        result = extract_bash_subcommands("ls; echo done")
        self.assertEqual(len(result), 2)

    def test_pipe_splits_into_two(self):
        result = extract_bash_subcommands("cat file.txt | grep foo")
        self.assertEqual(len(result), 2)

    # ── heredoc handling ──────────────────────────────────────────────────────

    def test_heredoc_full_text_extracted(self):
        """python3 << 'EOF'...EOF must produce a string containing the heredoc body."""
        cmd = "python3 << 'GHEOF'\nimport urllib.request\nurl = 'https://github.com/trending?since=weekly'\nGHEOF"
        result = extract_bash_subcommands(cmd)
        # Must produce exactly one entry
        self.assertEqual(len(result), 1)
        # The full text must be present so patterns can inspect the heredoc body
        self.assertIn("github.com/trending", result[0])
        self.assertTrue(result[0].startswith("python3"))

    def test_heredoc_full_text_matches_allow_pattern(self):
        """Allow-list pattern for heredoc body must match after extraction."""
        from gateway.config import ToolRule
        from gateway.core.tool_match import all_params_match_any

        cmd = (
            "python3 << 'GHEOF'\n"
            "import urllib.request, re, json, html as h\n"
            "req = urllib.request.Request('https://github.com/trending?since=weekly')\n"
            "print(req)\n"
            "GHEOF"
        )
        param_strings = extract_bash_subcommands(cmd)
        rule = ToolRule(tool="Bash", params=r"python3.*github\.com/trending.*")
        self.assertTrue(all_params_match_any([rule], "Bash", param_strings))

    def test_heredoc_dangerous_compound_still_blocked(self):
        """python3 << 'EOF'...EOF && rm -rf / — rm must be extracted separately and blocked."""
        from gateway.config import ToolRule
        from gateway.core.tool_match import all_params_match_any

        cmd = (
            "python3 << 'GHEOF'\n"
            "url = 'https://github.com/trending'\n"
            "GHEOF\n"
            "&& rm -rf /"
        )
        param_strings = extract_bash_subcommands(cmd)
        # Must have at least 2 entries: the heredoc command and the rm command
        self.assertGreaterEqual(len(param_strings), 2)
        # A pattern matching only the heredoc must NOT approve the whole compound command
        rule = ToolRule(tool="Bash", params=r"python3.*github\.com/trending.*")
        self.assertFalse(all_params_match_any([rule], "Bash", param_strings))

    def test_file_redirect_target_is_its_own_param_string(self):
        """echo hi > /tmp/out → the command and the redirect, operator kept (#173)."""
        result = extract_bash_subcommands("echo hi > /tmp/out")
        self.assertEqual(result, ["echo hi", "> /tmp/out"])

    def test_herestring_full_text_extracted(self):
        """cmd <<< 'value' — herestring content included in extracted text."""
        cmd = "grep pattern <<< 'some multiline content with https://example.com'"
        result = extract_bash_subcommands(cmd)
        self.assertEqual(len(result), 1)
        self.assertIn("example.com", result[0])


# ── substitutions are recursed into (#171) ───────────────────────────────────


class TestExtractBashSubcommandsSubstitutions(unittest.TestCase):
    """A command nested in a substitution is a sub-command of its own.

    Before #171 substitutions were opaque: ``coop legacy fetch-history --room r $(rm -rf
    /tmp/x)`` came back as one string, which the built-in fetch-history guest rule (it ends
    in ``.*``) matched, and the nested ``rm`` ran for a guest.  Every case here
    asserts the nested command is present so a rule set has to allow it explicitly.
    """

    FETCH = "coop legacy fetch-history --room r"

    def test_dollar_paren_substitution_yields_nested_command(self):
        result = extract_bash_subcommands(f"{self.FETCH} $(rm -rf /tmp/x)")
        self.assertEqual(result, [f"{self.FETCH} $(rm -rf /tmp/x)", "rm -rf /tmp/x"])

    def test_substitution_inside_double_quotes(self):
        result = extract_bash_subcommands('coop legacy fetch-history --room "$(rm -rf /tmp/x)"')
        self.assertIn("rm -rf /tmp/x", result)

    def test_backtick_substitution(self):
        result = extract_bash_subcommands(f"{self.FETCH} `id`")
        self.assertEqual(result, [f"{self.FETCH} `id`", "id"])

    def test_process_substitution(self):
        result = extract_bash_subcommands("diff <(sort a) <(sort b)")
        self.assertEqual(result, ["diff <(sort a) <(sort b)", "sort a", "sort b"])

    def test_substitution_inside_parameter_expansion(self):
        result = extract_bash_subcommands("coop legacy fetch-history --room ${X:-$(id)}")
        self.assertIn("id", result)

    def test_substitution_in_variable_assignment_prefix(self):
        result = extract_bash_subcommands(f"x=$(id) {self.FETCH}")
        self.assertIn("id", result)

    def test_substitution_in_redirect_target(self):
        """Both the redirect (#173) and the command inside its target are returned."""
        result = extract_bash_subcommands(f"{self.FETCH} > $(id)")
        self.assertEqual(result, [self.FETCH, "> $(id)", "id"])

    def test_substitution_in_herestring(self):
        result = extract_bash_subcommands(f'{self.FETCH} <<< "$(id)"')
        self.assertEqual(result, [f'{self.FETCH} <<< "$(id)"', "id"])

    def test_substitution_in_unquoted_heredoc_body(self):
        cmd = f"{self.FETCH} << EOF\n$(id)\nEOF"
        result = extract_bash_subcommands(cmd)
        self.assertEqual(result, [cmd, "id"])

    def test_quoted_heredoc_body_is_not_expanded(self):
        """bash does not expand ``$(…)`` inside ``<< 'EOF'``; neither does the splitter."""
        cmd = f"{self.FETCH} << 'EOF'\n$(id)\nEOF"
        self.assertEqual(extract_bash_subcommands(cmd), [cmd])

    def test_heredoc_command_is_not_duplicated_as_bare_command(self):
        """The heredoc statement is one string; ``python3`` alone must not also appear."""
        cmd = "python3 << 'EOF'\nprint(1)\nEOF"
        self.assertEqual(extract_bash_subcommands(cmd), [cmd])

    def test_nested_substitution_two_levels(self):
        result = extract_bash_subcommands("echo $(echo $(id))")
        self.assertEqual(result, ["echo $(echo $(id))", "echo $(id)", "id"])

    def test_substitution_in_for_loop_list(self):
        result = extract_bash_subcommands("for f in $(ls); do echo $f; done")
        self.assertEqual(result, ["ls", "echo $f"])

    # ── the rules the issue was about ────────────────────────────────────────

    def test_guest_fetch_history_rule_denies_substituted_command(self):
        """#171: the built-in guest rule must not approve ``$(rm …)`` riding on fetch-history."""
        guest_rules = AgentConfig().effective_guest_allowed_tools()
        params = extract_bash_subcommands(f"{self.FETCH} $(rm -rf /tmp/x)")
        self.assertFalse(all_params_match_any(guest_rules, "Bash", params))
        # The un-substituted command still passes, so the rule is not simply dead.
        self.assertTrue(
            all_params_match_any(guest_rules, "Bash", extract_bash_subcommands(self.FETCH))
        )

    def test_owner_send_with_date_substitution_still_passes(self):
        """The owner ``date`` rule covers the nested ``date``; ``coop legacy send … $(date)`` stays approved."""
        owner_rules = AgentConfig().effective_owner_allowed_tools()
        params = extract_bash_subcommands('coop legacy send --room r "now: $(date +%s)"')
        self.assertEqual(params, ['coop legacy send --room r "now: $(date +%s)"', "date +%s"])
        self.assertTrue(all_params_match_any(owner_rules, "Bash", params))

    def test_owner_send_with_non_date_substitution_is_not_auto_approved(self):
        owner_rules = AgentConfig().effective_owner_allowed_tools()
        params = extract_bash_subcommands("coop legacy send --room r $(cat /etc/passwd)")
        self.assertFalse(all_params_match_any(owner_rules, "Bash", params))


# ── substitutions the parser misses fail closed ──────────────────────────────


class TestUnparsedSubstitutionsFailClosed(unittest.TestCase):
    """tree-sitter-bash 0.25.1 leaves some substitutions as plain text; bash runs them.

    Codex review of #174 found two: a ``$(…)`` on an indented line of an unquoted
    heredoc body, and a backtick inside a ``${x:-…}`` expansion. Each is returned
    as its own sub-command so a rule has to match the raw text — the built-in
    ``coop`` rules never do.
    """

    FETCH = "coop legacy fetch-history --room r"
    GUEST = AgentConfig().effective_guest_allowed_tools()

    # Every placement bash expands but the parser does not structure. Adding a
    # newly discovered one here is the whole fix for it.
    EXECUTED_BUT_UNPARSED = {
        "indented heredoc line (<<)": f"{FETCH} << EOF\n\t$(rm -rf /tmp/x)\nEOF",
        "indented heredoc line (<<-)": f"{FETCH} <<-EOF\n\t$(rm -rf /tmp/x)\nEOF",
        "indented second heredoc line": f"{FETCH} << EOF\nline1\n  $(rm -rf /tmp/x)\nEOF",
        "backtick in ${x:-…}": "coop legacy fetch-history --room ${x:-`rm -rf /tmp/x`}",
        "backtick in quoted ${x:-…}": 'coop legacy fetch-history --room "${x:-`rm -rf /tmp/x`}"',
        "backtick in ${x#…} pattern": "coop legacy fetch-history --room ${x#`rm -rf /tmp/x`}",
        "backtick in unquoted heredoc body": f"{FETCH} << EOF\n`rm -rf /tmp/x`\nEOF",
        # Codex round 3: the leaf itself begins with allow-listed text.
        "heredoc line prefixed with an allowed command": f"{FETCH} << EOF\n{FETCH} `rm -rf /tmp/x`\nEOF",
    }

    def test_every_unparsed_placement_yields_an_extra_sub_command(self):
        """The invariant: the extra sub-command *starts at the opener*.

        Nothing before ``$(`` / the backtick is kept, so a rule anchored on a
        command name cannot match it no matter what the attacker writes first.
        """
        for label, cmd in self.EXECUTED_BUT_UNPARSED.items():
            with self.subTest(label):
                result = extract_bash_subcommands(cmd)
                self.assertGreater(len(result), 1, result)
                extras = [x for x in result[1:] if "rm -rf /tmp/x" in x]
                self.assertTrue(extras, result)
                for extra in extras:
                    self.assertTrue(extra.startswith(("$(", "`")), extra)

    def test_every_unparsed_placement_is_denied_for_a_guest(self):
        for label, cmd in self.EXECUTED_BUT_UNPARSED.items():
            with self.subTest(label):
                params = extract_bash_subcommands(cmd)
                self.assertFalse(all_params_match_any(self.GUEST, "Bash", params), params)

    def test_backtick_in_expansion_returns_the_raw_text(self):
        result = extract_bash_subcommands("coop legacy fetch-history --room ${x:-`id`}")
        self.assertEqual(result, ["coop legacy fetch-history --room ${x:-`id`}", "`id`"])

    def test_indented_heredoc_returns_from_the_opener(self):
        cmd = f"{self.FETCH} <<-EOF\n\t$(id)\nEOF"
        self.assertEqual(extract_bash_subcommands(cmd), [cmd, "$(id)\n"])

    def test_arithmetic_in_heredoc_keeps_only_real_nested_substitutions(self):
        """The parser mis-reads ``$((…))`` in a heredoc as a subshell command; bash does arithmetic.

        ``1+2`` must not come back as a command; a genuine ``$(id)`` or backtick
        inside the expression must.
        """
        plain = f"{self.FETCH} << EOF\ntotal $((1+2))\nEOF"
        self.assertEqual(extract_bash_subcommands(plain), [plain])
        nested = f"{self.FETCH} << EOF\ntotal $((1+$(id)))\nEOF"
        self.assertEqual(extract_bash_subcommands(nested), [nested, "id"])
        backtick = f"{self.FETCH} << EOF\ntotal $((1+`id`))\nEOF"
        self.assertEqual(extract_bash_subcommands(backtick), [backtick, "id"])

    # ── places bash does not expand: a marker there is literal text ──────────

    NEVER_EXPANDED = {
        "single-quoted heredoc": f"{FETCH} << 'EOF'\n$(id)\nEOF",
        "double-quoted heredoc": f'{FETCH} << "EOF"\n$(id)\nEOF',
        "backslash-quoted heredoc": f"{FETCH} << \\EOF\n$(id)\nEOF",
        "partly quoted heredoc delimiter": f'{FETCH} << E"OF"\n$(id)\nEOF',
        "backslash inside heredoc delimiter": f"{FETCH} << E\\OF\n$(id)\nEOF",
        "ansi-c quoted heredoc delimiter": f"{FETCH} << $'EOF'\n$(id)\nEOF",
        "single-quoted word": "coop legacy fetch-history --room '$(id)'",
        "ansi-c string": "coop legacy fetch-history --room $'$(id)'",
        "arithmetic expansion": f"{FETCH} $((1+1))",
        "arithmetic expansion in heredoc": f"{FETCH} << EOF\ntotal $((1+2))\nEOF",
        # bash performs process substitution only as a bare word (which the
        # parser structures), never inside double quotes or a heredoc body.
        "process-substitution text in double quotes": f'{FETCH} "compare <(old)"',
        "process-substitution text in heredoc": f"{FETCH} << EOF\ncompare <(old) >(new)\nEOF",
        "process-substitution text in ${{x:-…}}": "coop legacy fetch-history --room ${x:-<(id)}",
    }

    def test_never_expanded_places_stay_a_single_sub_command(self):
        for label, cmd in self.NEVER_EXPANDED.items():
            with self.subTest(label):
                result = extract_bash_subcommands(cmd)
                self.assertEqual(len(result), 1, result)
                self.assertTrue(all_params_match_any(self.GUEST, "Bash", result), result)

    def test_marker_in_trailing_comment_is_ignored(self):
        self.assertEqual(extract_bash_subcommands(f"{self.FETCH} # $(id)"), [self.FETCH])

    def test_escaped_markers_are_literal(self):
        """``coop legacy send … "use \\`ls\\`"`` is markdown, not a substitution — bash does not run it."""
        cases = {
            "escaped backtick in double quotes": 'coop legacy send --room r "use \\`ls\\` here"',
            "escaped dollar-paren in double quotes": 'coop legacy send --room r "costs \\$(5)"',
            "escaped backtick in bare word": "coop legacy send --room r use\\ \\`ls\\`",
            "escaped backtick in unquoted heredoc": "coop legacy send --room r --file - << EOF\nuse \\`ls\\` here\nEOF",
        }
        owner = AgentConfig().effective_owner_allowed_tools()
        for label, cmd in cases.items():
            with self.subTest(label):
                result = extract_bash_subcommands(cmd)
                self.assertEqual(len(result), 1, result)
                self.assertTrue(all_params_match_any(owner, "Bash", result), result)

    def test_unescaped_backtick_in_unquoted_heredoc_fails_closed(self):
        """The same heredoc without the escapes runs ``ls``.

        The parser leaves a backtick in a heredoc body unparsed (only ``$(…)``
        gets a node there), so this is the raw-text path, not the AST path.
        """
        cmd = "coop legacy send --room r --file - << EOF\nuse `ls` here\nEOF"
        self.assertEqual(extract_bash_subcommands(cmd), [cmd, "`ls` here\n"])


# ── redirect targets are parameter strings (#173) ────────────────────────────


class TestRedirectTargets(unittest.TestCase):
    """A ``> file`` redirect is a consequence of its own and must match a rule.

    Before #173 the redirect was dropped from the returned strings, so
    ``coop legacy fetch-history --room r > ~/.ssh/authorized_keys`` matched the
    built-in guest rule and truncated the file under the gateway account.
    """

    FETCH = "coop legacy fetch-history --room r"
    GUEST = AgentConfig().effective_guest_allowed_tools()
    OWNER = AgentConfig().effective_owner_allowed_tools()

    WRITES = {
        "truncate": (f"{FETCH} > /tmp/x", "> /tmp/x"),
        "append": (f"{FETCH} >> /tmp/x", ">> /tmp/x"),
        "stderr to file": (f"{FETCH} 2> err.log", "2> err.log"),
        "both streams": (f"{FETCH} &> all.log", "&> all.log"),
        "clobber": (f"{FETCH} >| f", ">| f"),
        "input from file": (f"{FETCH} < /etc/passwd", "< /etc/passwd"),
        "read-write": (f"{FETCH} <> rw", "<> rw"),
        "expansion in target": (f'{FETCH} > "$HOME/x"', '> "$HOME/x"'),
        "no space": (f"{FETCH} >/tmp/x", "> /tmp/x"),
    }

    def test_every_file_redirect_is_returned_with_its_operator(self):
        for label, (cmd, expected) in self.WRITES.items():
            with self.subTest(label):
                self.assertEqual(extract_bash_subcommands(cmd), [self.FETCH, expected])

    def test_every_file_redirect_is_denied_for_a_guest(self):
        for label, (cmd, _) in self.WRITES.items():
            with self.subTest(label):
                params = extract_bash_subcommands(cmd)
                self.assertFalse(all_params_match_any(self.GUEST, "Bash", params), params)

    def test_traversal_in_target_is_normalized(self):
        result = extract_bash_subcommands(f"{self.FETCH} > /tmp/../etc/passwd")
        self.assertEqual(result, [self.FETCH, "> /etc/passwd"])

    def test_multiple_redirects_each_returned(self):
        result = extract_bash_subcommands(f"{self.FETCH} 2> err.log >> out.log")
        self.assertEqual(result, [self.FETCH, "2> err.log", ">> out.log"])

    def test_substitution_in_target_yields_both(self):
        result = extract_bash_subcommands(f"{self.FETCH} > $(id)")
        self.assertEqual(result, [self.FETCH, "> $(id)", "id"])

    def test_heredoc_with_file_redirect(self):
        cmd = f"{self.FETCH} <<EOF > out\nbody\nEOF"
        self.assertEqual(extract_bash_subcommands(cmd), [cmd, "> out"])

    def test_quoted_heredoc_with_file_redirect_still_returns_the_redirect(self):
        """Skipping a quoted heredoc's body must not skip the `> out` on the same line."""
        cmd = f"{self.FETCH} <<'EOF' > out\n$(id)\nEOF"
        self.assertEqual(extract_bash_subcommands(cmd), [cmd, "> out"])

    def test_plainly_quoted_target_is_unquoted(self):
        self.assertEqual(extract_bash_subcommands(f'{self.FETCH} > "/tmp/x"'), [self.FETCH, "> /tmp/x"])
        self.assertEqual(extract_bash_subcommands(f"{self.FETCH} > '/dev/null'"), [self.FETCH])
        # an expansion inside quotes is kept verbatim — its value is unknown
        self.assertEqual(extract_bash_subcommands(f'{self.FETCH} > "$HOME/x"'), [self.FETCH, '> "$HOME/x"'])

    # ── not writes: nothing to match ─────────────────────────────────────────

    NOT_WRITES = {
        "stderr to stdout": f"{FETCH} 2>&1",
        "stdout to stderr": f"{FETCH} >&2",
        "close descriptor": f"{FETCH} 3>&-",
        "dev null": f"{FETCH} > /dev/null",
        "dev null no space + dup": f"{FETCH} >/dev/null 2>&1",
        "dev stderr": f"{FETCH} > /dev/stderr",
        "dev fd": f"{FETCH} > /dev/fd/1",
        "stderr to dev null": f"{FETCH} 2>/dev/null",
    }

    def test_descriptor_duplications_and_sinks_are_not_params(self):
        for label, cmd in self.NOT_WRITES.items():
            with self.subTest(label):
                self.assertEqual(extract_bash_subcommands(cmd), [self.FETCH])
                self.assertTrue(all_params_match_any(self.GUEST, "Bash", [self.FETCH]))

    # ── writing a rule for it ────────────────────────────────────────────────

    def test_redirect_rule_does_not_allow_running_the_same_path(self):
        """The operator in the string is what keeps ``>>?\\s*/tmp/.*`` from allowing ``/tmp/evil.sh``."""
        rules = [ToolRule(tool="Bash", params=r">>?\s*/tmp/.*")]
        self.assertTrue(matches_any(rules, "Bash", "> /tmp/x"))
        self.assertTrue(matches_any(rules, "Bash", ">> /tmp/x"))
        self.assertFalse(matches_any(rules, "Bash", "/tmp/evil.sh"))
        self.assertFalse(matches_any(rules, "Bash", "> /etc/passwd"))

    def test_shipped_scratch_dir_preset_allows_tmp_redirects_only(self):
        """The preset in config.example.yaml, as YAML-escaped, matches `> /tmp/x` and nothing else."""
        import yaml

        example = yaml.safe_load(
            (pathlib.Path(__file__).resolve().parents[2] / "config.example.yaml").read_text()
        )
        rules = [ToolRule(**r) for r in example["tool_presets"]["scratch-dir"]]
        self.assertTrue(all_params_match_any(rules, "Bash", ["> /tmp/x"]))
        self.assertTrue(all_params_match_any(rules, "Bash", [">> /tmp/x"]))
        self.assertTrue(all_params_match_any(rules, "Write", ["/tmp/notes.md"]))
        self.assertFalse(all_params_match_any(rules, "Bash", ["/tmp/evil.sh"]))
        self.assertFalse(all_params_match_any(rules, "Bash", ["> /etc/passwd"]))
        self.assertFalse(all_params_match_any(rules, "Bash", ["< /tmp/x"]))  # read is not granted
        self.assertFalse(all_params_match_any(rules, "Write", ["/etc/passwd"]))

    # ── Codex round 1 on #177: the target must be the path bash opens ────────

    def _preset(self):
        import yaml

        example = yaml.safe_load(
            (pathlib.Path(__file__).resolve().parents[2] / "config.example.yaml").read_text()
        )
        return self.OWNER + [ToolRule(**r) for r in example["tool_presets"]["scratch-dir"]]

    def test_expansion_in_target_is_returned_from_the_opener(self):
        """`/tmp/${HOME//root/../..}/etc/passwd` has no knowable value; no path rule may match it."""
        cmd = f"{self.FETCH} > /tmp/${{HOME//root/../..}}/etc/passwd"
        params = extract_bash_subcommands(cmd)
        self.assertEqual(params, [self.FETCH, "> ${HOME//root/../..}/etc/passwd"])
        self.assertFalse(all_params_match_any(self._preset(), "Bash", params))
        self.assertEqual(extract_bash_subcommands(f"{self.FETCH} > $HOME/x"), [self.FETCH, "> $HOME/x"])
        self.assertEqual(extract_bash_subcommands(f"{self.FETCH} > /tmp/$(id)"), [self.FETCH, "> $(id)", "id"])

    def test_quote_fragments_are_removed_before_normalizing(self):
        for cmd in (f"{self.FETCH} > /tmp/'..'/etc/passwd", f'{self.FETCH} > "/tmp/../etc/passwd"', f"{self.FETCH} > /tmp/\"..\"/etc/passwd"):
            with self.subTest(cmd):
                params = extract_bash_subcommands(cmd)
                self.assertEqual(params, [self.FETCH, "> /etc/passwd"])
                self.assertFalse(all_params_match_any(self._preset(), "Bash", params))

    def test_relative_target_is_normalized_so_traversal_shows(self):
        params = extract_bash_subcommands(f"{self.FETCH} > logs/../../secrets")
        self.assertEqual(params, [self.FETCH, "> ../secrets"])
        rule = [ToolRule(tool="Bash", params=r">>?\s*logs/.*")]
        self.assertFalse(all_params_match_any(rule, "Bash", params[1:]))
        self.assertTrue(all_params_match_any(rule, "Bash", extract_bash_subcommands("x > logs/a/../b.log")[1:]))

    def test_compact_read_write_redirect_fails_closed(self):
        """`<>/tmp/f` parses as ERROR(`<`) + `>/tmp/f`; the stray `<` is a fragment nothing matches."""
        params = extract_bash_subcommands("grep root <>/tmp/link")
        self.assertIn("<", params)
        self.assertFalse(all_params_match_any(self._preset() + [ToolRule(tool="Bash", params="grep .*")], "Bash", params))

    def test_descriptor_moves_closes_and_quoted_operands_are_not_params(self):
        for cmd in (f'{self.FETCH} 2>&"1"', f"{self.FETCH} 3>&1-", f"{self.FETCH} 3>& -", f"{self.FETCH} 4<&0"):
            with self.subTest(cmd):
                self.assertEqual(extract_bash_subcommands(cmd), [self.FETCH])

    def test_process_substitution_target_is_not_a_file(self):
        """`> >(cmd)` opens a pipe, not a path; the nested command is what must match."""
        params = extract_bash_subcommands(f"{self.FETCH} > >(coop legacy send --room r -)")
        self.assertEqual(params, [self.FETCH, "coop legacy send --room r -"])
        self.assertTrue(all_params_match_any(self.OWNER, "Bash", params))

    def test_prefixed_process_substitution_is_a_path(self):
        """`>\\ >(date)` opens the relative path ` /dev/fd/N`; only the bare `> >(cmd)` is a pipe."""
        params = extract_bash_subcommands(f"{self.FETCH} >\\ >(date)")
        self.assertIn("date", params)
        self.assertEqual(len(params), 3, params)
        self.assertFalse(all_params_match_any(self.OWNER + self._preset(), "Bash", params), params)

    def test_shipped_preset_is_case_sensitive_on_the_directory(self):
        preset = self._preset()
        self.assertTrue(all_params_match_any(preset, "Bash", ["> /tmp/x"]))
        self.assertFalse(all_params_match_any(preset, "Bash", ["> /TMP/x"]))
        self.assertFalse(all_params_match_any(preset, "Write", ["/Tmp/x"]))
        self.assertTrue(all_params_match_any(preset, "Write", ["/tmp/x"]))

    # ── Codex round 2 on #177: the value must be knowable, not just slash-prefixed ──

    def test_every_literal_prefix_before_an_expansion_is_cut(self):
        """`logs${…}` with a relative `logs.*` rule — no slash in the prefix, still cut."""
        params = extract_bash_subcommands(f"{self.FETCH} > logs${{HOME//root/../../..}}/etc/passwd")
        self.assertEqual(params, [self.FETCH, "> ${HOME//root/../../..}/etc/passwd"])
        rule = [ToolRule(tool="Bash", params=r">>?\s*logs.*")]
        self.assertFalse(all_params_match_any(rule, "Bash", params[1:]))

    def test_glob_metacharacters_make_the_target_unknowable(self):
        """`normpath` on `/tmp/a/**/../../home/u/f` collapses the wrong components when `**` matches nothing."""
        cases = {
            "globstar": (f"{self.FETCH} > /tmp/a/**/../../home/user/file", "> **/../../home/user/file"),
            "star": (f"{self.FETCH} > /tmp/*.log", "> *.log"),
            "bracket": (f"{self.FETCH} > /tmp/[ab]/../../etc/x", "> [ab]/../../etc/x"),
            "question": (f"{self.FETCH} > /tmp/?/../../etc/x", "> ?/../../etc/x"),
        }
        for label, (cmd, expected) in cases.items():
            with self.subTest(label):
                params = extract_bash_subcommands(cmd)
                self.assertEqual(params, [self.FETCH, expected])
                self.assertFalse(all_params_match_any(self._preset(), "Bash", params))

    def test_quote_only_prefix_is_kept_for_readability(self):
        self.assertEqual(extract_bash_subcommands(f'{self.FETCH} > "$HOME/x"'), [self.FETCH, '> "$HOME/x"'])
        # single quotes would make bash treat `$HOME` literally; we still fail closed on it
        self.assertEqual(extract_bash_subcommands(f"{self.FETCH} > '$HOME/x'"), [self.FETCH, "> '$HOME/x'"])

    def test_shipped_preset_covers_every_file_writing_permission_name(self):
        """Claude asks as Write/Edit/MultiEdit/NotebookEdit; OpenCode asks as `edit` for all of them."""
        preset = self._preset()
        for tool in ("Write", "Edit", "MultiEdit", "NotebookEdit", "edit"):
            with self.subTest(tool):
                self.assertTrue(all_params_match_any(preset, tool, ["/tmp/notes.md"]))
                self.assertFalse(all_params_match_any(preset, tool, ["/TMP/notes.md"]))
                self.assertFalse(all_params_match_any(preset, tool, ["/etc/passwd"]))
        self.assertFalse(all_params_match_any(preset, "Read", ["/tmp/notes.md"]))

    # ── Codex round 3 on #177 ────────────────────────────────────────────────

    def test_line_continuation_in_target_fails_closed(self):
        """`\\<newline>` in a target is matched as raw text: joined or kept depends on quoting bash
        rules this module does not lex, so neither reading is trusted."""
        cases = {
            "outside quotes": f"{self.FETCH} > /var\\\n/tmp/x",
            "inside single quotes": f"{self.FETCH} > '/tmp/sa\\\nfe/x'",
            "single quote inside double quotes": f"{self.FETCH} > \"/tmp/a'/.\\\n./../../etc/passwd\"",
        }
        for label, cmd in cases.items():
            with self.subTest(label):
                params = extract_bash_subcommands(cmd)
                self.assertEqual(len(params), 2, params)
                self.assertIn("\\\n", params[1])
                self.assertFalse(all_params_match_any(self._preset(), "Bash", params), params)
                self.assertFalse(all_params_match_any([ToolRule(tool="Bash", params=r">>?\s*/tmp/safe/.*")], "Bash", params[1:]))

    def test_grammar_failure_fails_closed_and_keeps_nested_commands(self):
        """An ERROR node is not reconstructed: its shell-syntax text is a fragment nothing matches.

        `<< E"OF"` with a plain body lands there whole — a valid quoted heredoc
        that is now over-denied, by design. What must never happen is the
        opposite: a nested `$(id)` next to it must still be collected, and
        `python3` alone must never approve a script body.
        """
        cmd = 'coop legacy send --room r - << E"OF"\nhello\nEOF'
        params = extract_bash_subcommands(cmd)
        self.assertFalse(all_params_match_any(self.OWNER, "Bash", params), params)
        script = 'python3 << E"OF"\nimport os; os.remove("/important")\nEOF'
        self.assertFalse(all_params_match_any([ToolRule(tool="Bash", params="python3")], "Bash", extract_bash_subcommands(script)))
        nested = 'coop legacy fetch-history --room r $(id) << E"OF"\nhello\nEOF'
        params = extract_bash_subcommands(nested)
        self.assertIn("id", params)
        self.assertFalse(all_params_match_any(self.GUEST, "Bash", params), params)
        # a redirect nested in the failed parse is still a redirect
        self.assertIn("> out", extract_bash_subcommands('coop legacy send --room r - << E"OF" > out\nhello\nEOF'))

    def test_here_string_after_another_redirect_is_an_accepted_over_denial(self):
        """`2>/dev/null <<< hi` is valid bash the grammar fails on; it fails closed rather than being guessed at."""
        params = extract_bash_subcommands(f"{self.FETCH} 2>/dev/null <<< hi")
        self.assertFalse(all_params_match_any(self.GUEST, "Bash", params), params)
        # the plain here-string is fine
        self.assertTrue(all_params_match_any(self.GUEST, "Bash", extract_bash_subcommands(f"{self.FETCH} <<< hi")))

    def test_leading_tilde_is_an_expansion(self):
        params = extract_bash_subcommands(f"{self.FETCH} > ~/../tmp/x")
        self.assertEqual(params, [self.FETCH, "> ~/../tmp/x"])
        self.assertFalse(all_params_match_any([ToolRule(tool="Bash", params=r">>?\s*tmp/.*")], "Bash", params[1:]))
        self.assertFalse(all_params_match_any(self._preset(), "Bash", params))

    def test_shipped_preset_covers_opencode_external_directory_ask(self):
        """OpenCode asks `external_directory` with `<dir>/*` before editing outside the cwd."""
        preset = self._preset()
        self.assertTrue(all_params_match_any(preset, "external_directory", ["/tmp/*"]))
        self.assertTrue(all_params_match_any(preset, "external_directory", ["/tmp/sub/*"]))
        self.assertFalse(all_params_match_any(preset, "external_directory", ["/tmpfoo/*"]))
        self.assertFalse(all_params_match_any(preset, "external_directory", ["/TMP/*"]))
        self.assertFalse(all_params_match_any(preset, "external_directory", ["/etc/*"]))

    def test_escaped_space_in_target_is_one_path(self):
        """A path with a space is returned quoted, so a rule sees one word."""
        self.assertEqual(extract_bash_subcommands(f"{self.FETCH} > /tmp/x\\ y"), [self.FETCH, "> '/tmp/x y'"])
        self.assertTrue(all_params_match_any(self._preset(), "Bash", ["> '/tmp/x y'"]))

    def test_escaped_space_right_after_the_operator_is_part_of_the_path(self):
        """`>\\ /tmp/x` opens the relative path ` /tmp/x` (a directory named " "), not /tmp/x."""
        params = extract_bash_subcommands(f"{self.FETCH} >\\ /tmp/x")
        self.assertEqual(params, [self.FETCH, "> ' /tmp/x'"])
        self.assertFalse(all_params_match_any(self._preset(), "Bash", params))

    def test_owner_coop_send_with_stderr_to_dev_null_still_passes(self):
        params = extract_bash_subcommands('coop legacy send --room r "hi" 2>/dev/null')
        self.assertTrue(all_params_match_any(self.OWNER, "Bash", params))


# ── get_param_strings_for_claude ─────────────────────────────────────────────


class TestGetParamStringsForClaude(unittest.TestCase):
    """get_param_strings_for_claude extracts the correct field by tool type."""

    def test_read_returns_normalized_path(self):
        params = get_param_strings_for_claude("Read", {"file_path": "/src/main.py"})
        self.assertEqual(params, ["/src/main.py"])

    def test_read_path_traversal_normalized(self):
        params = get_param_strings_for_claude(
            "Read", {"file_path": "/project/../etc/passwd"}
        )
        self.assertEqual(params, ["/etc/passwd"])

    def test_read_with_working_directory(self):
        params = get_param_strings_for_claude(
            "Read", {"file_path": "src/main.py"}, working_directory="/project"
        )
        self.assertEqual(params, ["/project/src/main.py"])

    def test_webfetch_returns_url(self):
        params = get_param_strings_for_claude(
            "WebFetch", {"url": "https://example.com"}
        )
        self.assertEqual(params, ["https://example.com"])

    def test_unknown_tool_returns_json(self):
        params = get_param_strings_for_claude(
            "MyMCPTool", {"key": "value"}
        )
        self.assertEqual(len(params), 1)
        import json
        parsed = json.loads(params[0])
        self.assertEqual(parsed["key"], "value")

    def test_tool_name_case_insensitive(self):
        """Tool name lookup is case-insensitive."""
        p1 = get_param_strings_for_claude("read", {"file_path": "/f"})
        p2 = get_param_strings_for_claude("READ", {"file_path": "/f"})
        self.assertEqual(p1, p2)

    def test_missing_field_returns_empty_string(self):
        """If the primary field is absent, an empty string is returned."""
        params = get_param_strings_for_claude("Read", {})
        self.assertEqual(params, ["."])  # normpath("") → "."

    def test_write_uses_file_path(self):
        params = get_param_strings_for_claude("Write", {"file_path": "/out/result.txt"})
        self.assertEqual(params, ["/out/result.txt"])

    def test_skill_returns_skill_name(self):
        """Skill tool extracts the 'skill' field, not the full JSON blob."""
        params = get_param_strings_for_claude("Skill", {"skill": "daily-briefing"})
        self.assertEqual(params, ["daily-briefing"])

    def test_skill_case_insensitive(self):
        params = get_param_strings_for_claude("skill", {"skill": "daily-briefing"})
        self.assertEqual(params, ["daily-briefing"])

    def test_skill_allow_rule_matches(self):
        """The config rule params='daily-briefing' correctly auto-approves the Skill tool."""
        from gateway.core.tool_match import all_params_match_any
        rule = ToolRule(tool="Skill", params="daily-briefing")
        params = get_param_strings_for_claude("Skill", {"skill": "daily-briefing"})
        self.assertTrue(all_params_match_any([rule], "Skill", params))

    def test_skill_wrong_name_denied(self):
        """A different skill name must NOT match the daily-briefing rule."""
        from gateway.core.tool_match import all_params_match_any
        rule = ToolRule(tool="Skill", params="daily-briefing")
        params = get_param_strings_for_claude("Skill", {"skill": "some-other-skill"})
        self.assertFalse(all_params_match_any([rule], "Skill", params))


# ── get_param_strings_for_opencode ───────────────────────────────────────────


class TestGetParamStringsForOpencode(unittest.TestCase):
    """get_param_strings_for_opencode passes patterns through unchanged."""

    def test_non_empty_patterns_returned_as_is(self):
        patterns = ["ls", "echo hello"]
        result = get_param_strings_for_opencode(patterns)
        self.assertEqual(result, ["ls", "echo hello"])

    def test_empty_patterns_returns_single_empty_string(self):
        """Empty patterns list → [""] so tool-name-only rules still match."""
        result = get_param_strings_for_opencode([])
        self.assertEqual(result, [""])

    # ── bash: the gateway splits metadata["command"] itself (#175) ───────────

    HEREDOC_GAP = "coop legacy fetch-history --room r <<EOF\n\t$(rm -rf /tmp/x)\nEOF"

    def test_bash_command_is_split_by_the_gateway_too(self):
        """The sidecar's grammar emits one pattern for an indented heredoc `$(…)`; ours adds the opener."""
        sidecar_patterns = [self.HEREDOC_GAP]  # what opencode 1.18.13 actually emits
        result = get_param_strings_for_opencode(
            sidecar_patterns, "bash", {"command": self.HEREDOC_GAP}
        )
        self.assertEqual(result, [self.HEREDOC_GAP, "$(rm -rf /tmp/x)\n"])

    def test_bash_gap_form_is_denied_for_a_guest_only_with_the_command(self):
        guest = AgentConfig().effective_guest_allowed_tools()
        without = get_param_strings_for_opencode([self.HEREDOC_GAP], "bash", {})
        with_cmd = get_param_strings_for_opencode(
            [self.HEREDOC_GAP], "bash", {"command": self.HEREDOC_GAP}
        )
        self.assertTrue(all_params_match_any(guest, "bash", without))  # the hole
        self.assertFalse(all_params_match_any(guest, "bash", with_cmd))  # closed

    def test_bash_gateway_split_is_a_union_without_duplicates(self):
        cmd = 'coop legacy send --room r "now: $(date +%s)"'
        result = get_param_strings_for_opencode([cmd, "date +%s"], "bash", {"command": cmd})
        self.assertEqual(result, [cmd, "date +%s"])
        owner = AgentConfig().effective_owner_allowed_tools()
        self.assertTrue(all_params_match_any(owner, "bash", result))

    def test_bash_without_command_matches_sidecar_patterns_only(self):
        self.assertEqual(get_param_strings_for_opencode(["ls"], "bash", {}), ["ls"])
        self.assertEqual(get_param_strings_for_opencode(["ls"], "bash", None), ["ls"])
        self.assertEqual(get_param_strings_for_opencode(["ls"], "bash", {"command": 7}), ["ls"])

    def test_bash_command_with_empty_patterns_needs_no_placeholder(self):
        """The [""] placeholder is for a tool-name-only match; a split command replaces it."""
        result = get_param_strings_for_opencode([], "bash", {"command": "echo $(id)"})
        self.assertEqual(result, ["echo $(id)", "id"])

    def test_non_bash_permission_ignores_metadata_command(self):
        result = get_param_strings_for_opencode(["/tmp/x"], "edit", {"command": "rm -rf /"})
        self.assertEqual(result, ["/tmp/x"])

    def test_permission_name_is_case_insensitive(self):
        result = get_param_strings_for_opencode(["echo $(id)"], "Bash", {"command": "echo $(id)"})
        self.assertEqual(result, ["echo $(id)", "id"])


# ── all_params_match_any ─────────────────────────────────────────────────────


class TestAllParamsMatchAny(unittest.TestCase):
    """all_params_match_any requires ALL param strings to satisfy at least one rule."""

    def _rules(self, *specs) -> list[ToolRule]:
        """Build a list of ToolRule from (tool, params?) tuples."""
        result = []
        for spec in specs:
            if isinstance(spec, tuple):
                result.append(ToolRule(tool=spec[0], params=spec[1]))
            else:
                result.append(ToolRule(tool=spec))
        return result

    def test_single_param_matches(self):
        rules = self._rules(("Bash", "ls.*"))
        self.assertTrue(all_params_match_any(rules, "Bash", ["ls -la"]))

    def test_all_params_must_match(self):
        """Two params: both must match (compound bash command)."""
        rules = self._rules(("Bash", "ls.*"), ("Bash", "echo.*"))
        # "ls -la" matches "ls.*" but "rm -rf /" does not
        self.assertFalse(all_params_match_any(rules, "Bash", ["ls -la", "rm -rf /"]))

    def test_both_params_match(self):
        """Two params, both covered by different rules → approved."""
        rules = self._rules(("Bash", "ls.*"), ("Bash", "echo.*"))
        self.assertTrue(all_params_match_any(rules, "Bash", ["ls -la", "echo hello"]))

    def test_empty_allow_list_denies_all(self):
        self.assertFalse(all_params_match_any([], "Bash", ["ls"]))

    def test_tool_mismatch_denies(self):
        rules = self._rules(("Read", None))
        self.assertFalse(all_params_match_any(rules, "Bash", ["ls"]))

    def test_single_wildcard_rule_allows_all(self):
        rules = self._rules((".*", ".*"))
        self.assertTrue(all_params_match_any(rules, "Bash", ["rm -rf /"]))

    def test_case_insensitive_tool_match(self):
        rules = self._rules(("bash", "ls"))
        self.assertTrue(all_params_match_any(rules, "Bash", ["ls"]))
        self.assertTrue(all_params_match_any(rules, "BASH", ["ls"]))


if __name__ == "__main__":
    unittest.main()
