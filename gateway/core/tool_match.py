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
    Command substitutions ($(...) / backticks) and process substitutions
    (<(...) / >(...)) are recursed into: the nested command is returned as its
    own sub-command and must match a rule too, so "coop fetch-history $(rm -rf x)"
    needs both "coop fetch-history $(rm -rf x)" and "rm -rf x" to be allowed.
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
import shlex
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


# Text that opens a substitution bash will execute.  The AST is not a complete
# oracle for these: with tree-sitter-bash 0.25.1 a ``$(...)`` on an indented
# heredoc line, any backtick in a heredoc body, or a backtick inside a
# ``${x:-...}`` / ``${x#...}`` expansion, comes back as a plain
# ``heredoc_body`` / ``word`` / ``regex`` leaf with no
# ``command_substitution`` child, while bash runs it.
#
# The rule for such a leaf: **an unparsed substitution is unknown code, and is
# returned as a sub-command starting at its opener.**  Nothing before the opener
# is included, so a rule anchored on a command name (every built-in rule, and
# every sensible user rule) can never match it; only an allow-everything rule
# does.  Returning the whole leaf instead would let ``coop fetch-history `rm x```
# on a heredoc line satisfy the ``coop fetch-history`` rule.
#
# Process substitution (``<(...)`` / ``>(...)``) is not a marker: bash performs
# it only as a bare word, which the parser does structure, and not inside
# double quotes, heredoc bodies or ``${x:-...}`` (checked against bash).
_SUBSTITUTION_MARKERS: tuple[str, ...] = ("$(", "`")


def _unparsed_substitution(text: str) -> str | None:
    r"""Return ``text`` from its first unescaped substitution opener, or None.

    A backslash-escaped ``\``` or ``\$`` is literal in a word, a double-quoted
    string and an unquoted heredoc body alike, so ``\x`` pairs are skipped.
    """
    i = 0
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        for marker in _SUBSTITUTION_MARKERS:
            if text.startswith(marker, i):
                return text[i:]
        i += 1
    return None

# Leaves bash never expands, so a marker inside them is literal text.  A quoted
# heredoc body is the other case and is handled where the heredoc is walked.
_NEVER_EXPANDED_LEAVES: frozenset[str] = frozenset({
    "raw_string",     # '...'
    "ansi_c_string",  # $'...'
    "comment",
})


# Redirect operators that duplicate, move or close a descriptor rather than
# open a file: ``2>&1``, ``>&2``, ``3>&1-``, ``3>&-``, ``<&0``.  Nothing is
# written anywhere.
_FD_ONLY_REDIRECT_OPERATORS: frozenset[str] = frozenset({">&", "<&", ">&-", "<&-"})
_FD_OPERAND = re.compile(r"\d*-?")  # "1", "1-" (move), "-" (close), "" (with >&-)

# Destinations that are sinks, not files: writing to them has no persistent
# effect, so a redirect to one is not a parameter to match.  /dev/stdout,
# /dev/stderr and /dev/fd/N are sinks *here* because the tool process's stdio
# are pipes owned by the agent harness (Claude Code's Bash tool, opencode's
# shell tool); a redirect to them cannot reach a file on disk.
_SINK_DESTINATIONS: frozenset[str] = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr"})
_DEV_FD = re.compile(r"/dev/fd/\d+")


# Characters after which the path bash opens is no longer knowable from the
# text: an expansion (``$``, backtick), a pathname-expansion metacharacter
# (``*``, ``?``, ``[`` — with ``globstar``, ``**`` can match zero directories,
# so ``normpath`` on the literal text would collapse the wrong components),
# and — handled in the scan — a backslash-newline.  A leading ``~`` is checked
# by the caller.
_UNKNOWABLE_FROM = "$`*?["


def _first_unknowable(text: str) -> int:
    """Index of the first unescaped character from ``_UNKNOWABLE_FROM``, or -1.

    Quotes are skipped over as characters but not as regions: a ``$`` inside
    double quotes still expands, and a ``*`` inside single quotes does not —
    that second case is a false positive we accept (it fails closed).
    """
    i = 0
    while i < len(text):
        if text[i] == "\\":
            if text[i + 1:i + 2] == "\n":
                # A line continuation: bash removes it outside single quotes
                # and keeps it inside them, and telling those apart is a
                # shell lexer this module does not carry.
                return i
            i += 2
            continue
        if text[i] in _UNKNOWABLE_FROM:
            return i
        i += 1
    return -1


def _redirect_param(file_redirect, src: bytes) -> str | None:  # noqa: C901
    """Return the parameter string for a ``file_redirect`` node, or None.

    ``cmd > /tmp/x`` yields ``"> /tmp/x"``.  The operator is kept so a rule that
    allows a redirect (``>>?\\s*/tmp/.*``) cannot also allow *running* ``/tmp/x``.

    The rule: **a target is normalized and matched as a path only when the
    path bash will open is knowable from the text.**

    - A **literal** target (no unescaped ``$``, backtick, ``*``, ``?``, ``[``,
      and no leading ``~``) is shell-unquoted as a whole — ``/tmp/'..'/etc/passwd`` is
      ``/tmp/../etc/passwd`` — and then ``normpath``-ed, relative or absolute,
      so ``..`` cannot hide behind quotes or a prefix: ``> /tmp/../etc/passwd``
      matches as ``> /etc/passwd``, ``> logs/../../x`` as ``> ../x``.
    - Otherwise the value is not knowable before bash runs, so the string is
      returned from the first unknowable character onward, every literal
      prefix cut — ``> ${HOME//root/../..}/etc/passwd``, ``> **/../../x`` —
      and no rule anchored on a path prefix can match it (the same rule as
      for unparsed substitutions).

    Matching is lexical: symlinks are not resolved, exactly as for the file
    tools' ``normpath``.  A rule that confines writes to a directory confines
    the *path text*; a symlink planted inside that directory is outside what
    a path rule can see.

    Not returned, because nothing is opened by the user's choice: descriptor
    duplications, moves and closes (``2>&1``, ``3>&1-``, ``3>&-``); sinks
    (``/dev/null``, ``/dev/stdout``, ``/dev/stderr``, ``/dev/fd/N``); a
    process substitution target (``> >(cmd)`` — the nested command is
    returned by the walk).  Input redirects (``< file``) are returned — the
    file's contents reach the command.
    """
    fd = ""
    operator = ""
    dest = ""
    dest_from = file_redirect.end_byte
    dest_children: list = []
    for child in file_redirect.children:
        text = src[child.start_byte:child.end_byte].decode()
        if child.type == "file_descriptor":
            fd = text
        elif not child.is_named:
            operator = text
            dest_from = child.end_byte
        elif child.type == "ERROR":
            operator += text.strip()
            dest_from = child.end_byte
        else:
            dest_children.append(child)
    if dest_children:
        # The whole span from the end of the operator to the end of the
        # redirect — not the destination nodes only: an escaped space right
        # after the operator (``>\\ /tmp/x``, a path that starts with a space)
        # is outside the first destination node's span altogether.
        dest = src[dest_from:file_redirect.end_byte].decode()
        dest = dest.lstrip(" \t") if not dest.startswith(("\\ ", "\\\t")) else dest
        dest = dest.rstrip()
    if (
        len(dest_children) == 1
        and dest_children[0].type == "process_substitution"
        and dest == src[dest_children[0].start_byte:dest_children[0].end_byte].decode()
    ):
        # Exactly ``> >(cmd)``: a pipe, not a path; the nested command is
        # collected by the walk.  ``>\\ >(cmd)`` is not that — it opens the
        # relative path `` /dev/fd/N`` — and goes on to be matched as text.
        return None
    unknowable_at = _first_unknowable(dest)
    if dest.startswith("~"):
        unknowable_at = 0  # tilde expansion: ``~/../tmp/x`` is not ``tmp/x``
    if unknowable_at >= 0:
        # The path bash opens is not knowable from here on, so nothing before
        # this point may be matched either: cut every literal prefix
        # (``/tmp/`` in ``/tmp/${X}``, ``logs`` in ``logs${X}``) so no rule
        # anchored on a prefix can approve a value bash has not computed.  A
        # prefix that is only quote characters is kept — ``"$HOME/x"`` reads
        # better than ``$HOME/x"`` and anchors nothing.
        prefix = dest[:unknowable_at]
        if prefix.strip("'\"") == "":
            return f"{fd}{operator} {dest}"
        return f"{fd}{operator} {dest[unknowable_at:]}"
    if dest:
        try:
            parts = shlex.split(dest)
        except ValueError:
            parts = []
        if len(parts) != 1:
            return f"{fd}{operator} {dest}"  # not one plain word: match the raw text
        dest = parts[0]
    if operator in _FD_ONLY_REDIRECT_OPERATORS and _FD_OPERAND.fullmatch(dest):
        return None
    if dest:
        dest = os.path.normpath(dest)
    if dest in _SINK_DESTINATIONS or _DEV_FD.fullmatch(dest):
        return None
    if dest:
        # Quote the way a shell would print it, so a path with whitespace or
        # other special characters is visibly one word — ``> ' /tmp/x'``, not
        # ``>  /tmp/x`` that a ``\\s*`` in a rule would read as ``/tmp/x``.
        dest = shlex.quote(dest)
    return f"{fd}{operator} {dest}".rstrip()


def _heredoc_is_quoted(heredoc_redirect, src: bytes) -> bool:
    """True when bash will not expand the heredoc body.

    bash treats the body as quoted if *any part* of the delimiter is quoted:
    ``<< 'EOF'``, ``<< "EOF"``, ``<< \\EOF``, but also ``<< E"OF"``, ``<< E\\OF``
    and ``<< $'EOF'``.  So any quote or backslash anywhere in the delimiter
    counts, not only a leading one.
    """
    for child in heredoc_redirect.children:
        if child.type == "heredoc_start":
            start = src[child.start_byte:child.end_byte].decode()
            return any(ch in start for ch in ("'", '"', "\\"))
    return False


def extract_bash_subcommands(command: str) -> list[str]:  # noqa: C901
    """Split a compound bash command string into individual sub-command strings.

    Uses tree-sitter-bash to parse the AST.  Each ``command`` node (i.e. a
    leaf command in the pipeline/list) is returned as a separate string so the
    caller can require ALL of them to satisfy the allow rule.

    Command substitutions (``$(...)`` / backticks) and process substitutions
    (``<(...)`` / ``>(...)``) are **recursed into**: the parent command is
    returned with the substitution text still in place, and every ``command``
    nested inside the substitution is returned as well, wherever it sits — a
    bare word, a quoted string, a ``${var:-$(...)}`` expansion, a variable
    assignment prefix, a redirect target, a herestring or an unquoted heredoc
    body.  Requiring all of them to match is what stops ``$(rm -rf x)`` riding
    through a ``.*`` rule on the parent.  OpenCode's shell tool does the same
    (``descendantsOfType("command")``), so both brokers see the same list.

    The parser misses some substitutions bash executes (an indented line in an
    unquoted heredoc body; a backtick inside a ``${x:-...}`` expansion — see
    ``_SUBSTITUTION_MARKERS``).  Any named leaf that still contains an
    unescaped ``$(`` or backtick, and is not one bash never expands
    (single-quoted string, ``$'...'``, comment, quoted heredoc body), is
    returned as a sub-command **starting at that opener**: an unparsed
    substitution is unknown code, and cutting off everything before the opener
    is what stops a rule anchored on a command name from ever matching it.

    Heredoc redirections (``cmd << 'EOF' ... EOF``): the full
    ``redirected_statement`` text is returned as a single string so that
    allow-list patterns can inspect the heredoc body content (e.g. a Python
    script piped to the interpreter).

    File redirections (``> file``, ``>> file``, ``2> file``, ``< file``): the
    command is returned on its own, and each redirect is returned as a
    parameter string of its own **with its operator** — ``"> /tmp/x"`` — so
    the target has to match a rule too, and a rule that allows the redirect
    cannot be mistaken for one that allows running ``/tmp/x``.  Descriptor
    duplications (``2>&1``) and sinks (``/dev/null``) write nothing and are
    not returned.  See :func:`_redirect_param`.

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

    def descend(node) -> None:
        for child in node.children:
            walk(child)

    def arithmetic(node) -> None:
        # Never append a ``command`` from inside an arithmetic expansion; do
        # walk every substitution node and scan every leaf found under it.
        for child in node.children:
            if child.type in ("command_substitution", "process_substitution") or child.child_count == 0:
                walk(child)
            else:
                arithmetic(child)

    def walk(node) -> None:  # noqa: C901
        if node.child_count == 0:
            if node.is_named and node.type not in _NEVER_EXPANDED_LEAVES:
                text = src[node.start_byte:node.end_byte].decode()
                unparsed = _unparsed_substitution(text)
                if unparsed is not None:
                    # A substitution the parser did not turn into a node (see
                    # ``_SUBSTITUTION_MARKERS``): unknown code, returned from its
                    # opener so no command-anchored rule can approve it.
                    commands.append(unparsed)
            return
        if node.type == "command_substitution" and src.startswith(b"$((", node.start_byte):
            # Arithmetic expansion.  In a heredoc body the parser mis-reads
            # ``$((1+2))`` as a command substitution of a subshell running
            # ``1+2``.  bash evaluates it as arithmetic — an invalid expression
            # is an error and no substitution occurs (bash manual, "Arithmetic
            # Expansion") — so the inner ``command`` is not a command.  Real
            # substitutions nested in the expression (``$((1+$(id)))``) are
            # still nodes below it and are still collected.
            arithmetic(node)
            return
        if node.type == "file_redirect":
            # The target of ``> file`` is a consequence of its own — the file is
            # created or truncated under the gateway account — so it is a
            # parameter string of its own, operator included (#173).  Descend
            # too: ``> $(id)`` still has a command in it.  An ERROR child is
            # already folded into the operator by ``_redirect_param``.
            param = _redirect_param(node, src)
            if param is not None:
                commands.append(param)
            for child in node.children:
                if child.type != "ERROR":
                    walk(child)
            return
        if node.type == "ERROR":
            # Text the grammar could not place.  It is not reconstructed —
            # every attempt to guess what the parser meant has been a second
            # grammar with its own holes — so a fragment carrying shell syntax
            # (a redirect, pipe, list operator or expansion) is returned as a
            # sub-command that must match a rule on its own, which fails closed
            # for every command-anchored rule.  A stray plain word (the ``EOF``
            # the grammar drops after ``<< E"OF"``) is not returned.  The node
            # is still walked, so a command or redirect nested in it is
            # collected too.  Known over-denials, accepted: a heredoc with a
            # partly quoted delimiter (``<< E"OF"``) and a here-string that
            # follows another redirect (``2>/dev/null <<< hi``) both land here.
            text = src[node.start_byte:node.end_byte].decode().strip()
            if any(ch in text for ch in "<>|&;$`"):
                commands.append(text)
            descend(node)
            return
        if node.type == "command":
            commands.append(src[node.start_byte:node.end_byte].decode())
            # A substitution nested in this command's words, strings, assignments
            # or herestring is a ``command_substitution`` / ``process_substitution``
            # child; the ``command`` nodes inside it are collected by the descent.
            descend(node)
            return
        if node.type == "redirected_statement":
            # When a command uses a heredoc redirect (e.g. ``python3 << 'EOF'``),
            # the heredoc body is logically the command's stdin input and may
            # contain security-relevant content (e.g. a Python script).
            # Extract the full ``redirected_statement`` text so allow-list patterns
            # can inspect the heredoc body.
            #
            # For non-heredoc redirections (file I/O, e.g. ``echo hi > /tmp/f``),
            # fall through to normal child traversal: the ``command`` node and
            # each ``file_redirect`` become parameter strings of their own.
            has_heredoc = any(
                child.type in ("heredoc_redirect", "herestring_redirect")
                for child in node.children
            )
            if has_heredoc:
                commands.append(src[node.start_byte:node.end_byte].decode())
                # The ``command`` child is already covered by the full text;
                # descend into it (not ``walk`` it, which would append the bare
                # command a second time) and walk the redirects so substitutions
                # in an unquoted heredoc body are collected too.  tree-sitter
                # emits no substitution nodes inside a quoted heredoc
                # (``<< 'EOF'``), matching bash, which does not expand them.
                for child in node.children:
                    if child.type == "command":
                        descend(child)
                    elif child.type == "heredoc_redirect" and _heredoc_is_quoted(child, src):
                        # A quoted delimiter (``'EOF'``, ``E"OF"``, ``\EOF``, …): bash
                        # does not expand the body, so a ``$(`` in it is text.  Only
                        # a ``> file`` sharing the line still counts.
                        for part in child.children:
                            if part.type == "file_redirect":
                                walk(part)
                    else:
                        walk(child)
                return
        descend(node)

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


def get_param_strings_for_opencode(
    patterns: list,
    permission: str = "",
    metadata: dict | None = None,
) -> list[str]:
    """Return the list of parameter strings to match for an OpenCode permission event.

    OpenCode already parses compound bash commands via tree-sitter internally,
    producing one pattern per AST command node.  The gateway must require ALL
    patterns to match — not just patterns[0].

    For a ``bash`` ask the sidecar also sends the full command text as
    ``metadata["command"]`` (opencode ``shell.ts`` ``ask()``).  The gateway
    splits that text itself with :func:`extract_bash_subcommands` and requires
    those strings to match **as well**.  The sidecar's patterns come from the
    same tree-sitter-bash grammar and share its gaps — a ``$(...)`` on an
    indented heredoc line, a backtick in a heredoc body or inside ``${x:-...}``
    produce no nested pattern there — while the gateway's split returns the
    unparsed substitution from its opener and so fails closed (#175).  Without
    ``metadata["command"]`` only the sidecar's patterns are matched; the caller
    should log that, because it means the sidecar stopped sending the text.

    Returns ``[""]`` for an empty patterns list so that a tool-name-only rule
    (``rule.params is None``) still matches correctly.
    """
    strings = list(patterns)
    command = metadata.get("command") if isinstance(metadata, dict) else None
    if permission.lower() == "bash" and isinstance(command, str) and command:
        for sub in extract_bash_subcommands(command):
            if sub not in strings:
                strings.append(sub)
    return strings or [""]


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
