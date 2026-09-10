"""Tool rule matching utilities shared by all permission brokers.

Each broker extracts a list of "parameter strings" from the tool call payload
(format differs between Claude and OpenCode), then requires ALL of them to
match at least one allow rule before auto-approving.

Primary parameter field mapping (Claude tool_input):
  Bash / bash        → tool_input["command"]  (split into sub-commands via tree-sitter)
  WebFetch / webfetch → tool_input["url"]
  Read / Edit / Write → tool_input["file_path"]  (normalized via os.path.normpath)
  unknown / MCP      → full tool_input serialized as JSON

For OpenCode, patterns[] from the SSE permission event are used directly —
OpenCode already normalizes and splits compound bash commands into one pattern
per AST node.  All patterns must match for auto-approve.

Security notes:
  - Bash: compound commands (e.g. "echo hi && rm -rf /") are split by tree-sitter
    into individual sub-commands; ALL sub-commands must satisfy the params regex.
    Command substitutions ($(...) / backticks) are treated as opaque — the regex
    sees the full substitution text, not the nested command.
  - File paths: os.path.normpath() is applied before matching to prevent
    path-traversal bypasses ("/project/../../../etc/passwd").
  - WebFetch: avoid ".*" as params — it allows fetching internal network addresses
    (localhost, 169.254.169.254 AWS metadata, etc.).  Use explicit domain patterns.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import ToolRule

logger = logging.getLogger("coop.permissions.tool_match")

# Maps lowercase tool names to their primary parameter field in Claude's tool_input.
_CLAUDE_PARAM_FIELD: dict[str, str] = {
    "bash": "command",
    "webfetch": "url",
    "read": "file_path",
    "edit": "file_path",
    "write": "file_path",
    "multiedit": "file_path",
    "notebookedit": "notebook_path",
    "skill": "skill",  # Claude Code Skill tool — primary field is the skill name
}

# File tools whose primary field is a path that needs normalization.
_FILE_TOOLS: frozenset[str] = frozenset({
    "read", "edit", "write", "multiedit", "notebookedit",
})

# ── tree-sitter bash parser (optional — falls back gracefully if not installed) ──

_bash_parser = None  # set to a Parser instance on first use


def _get_bash_parser():
    """Return a cached tree-sitter bash Parser, or None if tree-sitter is unavailable."""
    global _bash_parser
    if _bash_parser is not None:
        return _bash_parser

    try:
        import tree_sitter_bash as tsbash  # type: ignore[import]
        from tree_sitter import Language, Parser  # type: ignore[import]

        lang = Language(tsbash.language())
        parser = Parser(lang)
        _bash_parser = parser
        return _bash_parser
    except ImportError:
        logger.warning(
            "tree-sitter or tree-sitter-bash not installed — compound bash command "
            "splitting is disabled.  Install with: pip install tree-sitter tree-sitter-bash"
        )
        return None


_OPAQUE_NODE_TYPES: frozenset[str] = frozenset({
    "command_substitution",
    "process_substitution",
})


def extract_bash_subcommands(command: str) -> list[str]:
    """Split a compound bash command string into individual sub-command strings.

    Uses tree-sitter-bash to parse the AST.  Each ``command`` node (i.e. a
    leaf command in the pipeline/list) is returned as a separate string so the
    caller can require ALL of them to satisfy the allow rule.

    Command substitutions (``$(...)`` / backticks) and process substitutions
    (``<(...)`` / ``>(...)`` ) are treated as **opaque** — they appear as part
    of their parent command's text but are not recursed into.  This is the same
    behavior as OpenCode.

    Heredoc redirections (``cmd << 'EOF' ... EOF``): the full
    ``redirected_statement`` text is returned as a single string so that
    allow-list patterns can inspect the heredoc body content (e.g. a Python
    script piped to the interpreter).  For regular file redirections
    (``> file``, ``< file``, etc.) only the ``command`` child is returned —
    consistent with non-redirected commands.

    Falls back to ``[command]`` (treat whole string as one command) when:
      - tree-sitter is not installed
      - the parser produces an empty result (shouldn't happen for valid bash)
    """
    parser = _get_bash_parser()
    if parser is None:
        return [command]

    src = command.encode()
    tree = parser.parse(src)
    commands: list[str] = []

    def walk(node) -> None:
        if node.type in _OPAQUE_NODE_TYPES:
            return
        if node.type == "command":
            commands.append(src[node.start_byte:node.end_byte].decode())
            return
        if node.type == "redirected_statement":
            # When a command uses a heredoc redirect (e.g. ``python3 << 'EOF'``),
            # the heredoc body is logically the command's stdin input and may
            # contain security-relevant content (e.g. a Python script).
            # Extract the full ``redirected_statement`` text so allow-list patterns
            # can inspect the heredoc body.
            #
            # For non-heredoc redirections (file I/O, e.g. ``echo hi > /tmp/f``),
            # fall through to normal child traversal so only the ``command`` node
            # text is extracted — consistent with prior behaviour.
            has_heredoc = any(
                child.type in ("heredoc_redirect", "herestring_redirect")
                for child in node.children
            )
            if has_heredoc:
                commands.append(src[node.start_byte:node.end_byte].decode())
                return
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return commands or [command]  # fallback: treat whole string as one command


def _normalize_path(value: str, working_directory: str) -> str:
    """Return a normalized absolute path string for use in regex matching.

    Resolves relative paths against ``working_directory`` and collapses any
    ``..`` components using ``os.path.normpath``.  This prevents path-traversal
    bypasses such as ``/project/../../../etc/passwd``.

    ``os.path.normpath`` (not ``os.path.realpath``) is used intentionally so
    this works for files that do not exist yet (e.g. a ``Write`` creating a new
    file).
    """
    if not os.path.isabs(value) and working_directory:
        value = os.path.join(working_directory, value)
    return os.path.normpath(value)


# ── Public API ─────────────────────────────────────────────────────────────────


def get_param_strings_for_claude(
    tool_name: str,
    tool_input: dict,
    working_directory: str = "",
) -> list[str]:
    """Return the list of parameter strings to match for a Claude PreToolUse event.

    For Bash, returns one string per AST sub-command (requires tree-sitter).
    For file tools, returns the normalized absolute path.
    For all other known tools, returns [primary_field_value].
    For unknown / MCP tools, returns [full_tool_input_as_json].

    All strings in the returned list must satisfy an allow rule for the tool
    call to be auto-approved.
    """
    tool_lower = tool_name.lower()
    field = _CLAUDE_PARAM_FIELD.get(tool_lower)

    if tool_lower == "bash":
        command = str(tool_input.get("command", ""))
        return extract_bash_subcommands(command)

    if tool_lower in _FILE_TOOLS and field:
        raw_path = str(tool_input.get(field, ""))
        return [_normalize_path(raw_path, working_directory)]

    if field:
        return [str(tool_input.get(field, ""))]

    # Unknown / MCP tool — fall back to full JSON
    return [json.dumps(tool_input, ensure_ascii=False)]


def get_param_strings_for_opencode(patterns: list) -> list[str]:
    """Return the list of parameter strings to match for an OpenCode permission event.

    OpenCode already parses compound bash commands via tree-sitter internally,
    producing one pattern per AST command node.  The gateway must require ALL
    patterns to match — not just patterns[0].

    Returns ``[""]`` for an empty patterns list so that a tool-name-only rule
    (``rule.params is None``) still matches correctly.
    """
    return list(patterns) if patterns else [""]


def matches_rule(rule: "ToolRule", tool_name: str, param_string: str) -> bool:
    """Return True if tool_name and param_string both satisfy the rule.

    Both the tool regex and the params regex use case-insensitive fullmatch,
    so the entire string must match (use .* for prefix/suffix flexibility).
    If rule.params is None, only the tool name is checked.
    """
    if not re.fullmatch(rule.tool, tool_name, re.IGNORECASE):
        return False
    if rule.params is not None:
        if not re.fullmatch(rule.params, param_string, re.IGNORECASE | re.DOTALL):
            return False
    return True


def matches_any(rules: "list[ToolRule]", tool_name: str, param_string: str) -> bool:
    """Return True if any rule in the list matches (tool_name, param_string)."""
    return any(matches_rule(r, tool_name, param_string) for r in rules)


def all_params_match_any(
    rules: "list[ToolRule]",
    tool_name: str,
    param_strings: list[str],
) -> bool:
    """Return True if every param string in param_strings matches at least one rule.

    This is the correct auto-approve check when a tool call produces multiple
    parameter strings (e.g. compound bash commands, OpenCode multi-pattern events).
    A single param string that doesn't match any rule is enough to reject.
    """
    return all(matches_any(rules, tool_name, p) for p in param_strings)
