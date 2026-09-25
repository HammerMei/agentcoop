"""A static check: every f-string interpolated into a markup-parsing sink
must escape its dynamic parts.

Why static, on top of the behavioural sweep in
`test_configtool_markup_safety.py`: that suite walks screens, so it can only
catch a site some keystroke actually reaches. Three separate Codex rounds on
PR #129 found unescaped sites, and the last of them (`MessageModal(str(exc))`
on ten paths, the save/create collision modals) sat behind failure branches
no walk-through naturally visits — I had also *excluded* `str(exc)` from my
own grep sweep by hand, on the wrong assumption that an exception message is
ours rather than a quote of the operator's config.

So this reads the source instead of running it, and fails when a NEW
unescaped interpolation appears. The two instruments are complementary:
this one is exhaustive over f-strings but blind to values assembled
elsewhere; the behavioural one covers assembled content but only on paths it
walks.

To satisfy it, either wrap the value — `markup_safe(x)`,
`gateway/configtool/formatting.py` — or, if the value provably cannot carry
operator text, add its exact expression source to `_NOT_OPERATOR_DATA` below
with a reason. Growing that list is the deliberate, reviewable step; missing
an escape silently is what this exists to prevent.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

_CONFIGTOOL = Path(__file__).resolve().parents[2] / "gateway" / "configtool"

# Callables whose text Rich/Textual parses as markup. `notify` is absent on
# purpose: `ConfigToolApp.notify()` forces `markup=False` for the whole
# package (see its docstring), so toasts are plain text by construction.
_SINK_NAMES = frozenset(
    {
        "MessageModal",
        "ConfirmModal",
        "Static",
        "Label",
        "update",
        "add_row",
        "add_column",
    }
)

# Expressions that cannot carry operator-authored text, so they need no
# escaping. Each entry is the exact source of an interpolated expression.
_NOT_OPERATOR_DATA = frozenset(
    {
        # Literal-typed values from closed sets declared in this package.
        "kind",
        "self.kind",
        "self._entity_noun()",
        "self._delete_blocker_noun()",
        # The DISPLAY name for one of those kinds. Strictly narrower than the
        # bare `kind` above it: `kind_label()` returns either its argument or a
        # literal from `_KIND_LABELS`, so no operator value can reach a sink
        # through it. Present as its own entries because the lint matches on
        # exact expression source, not on what the expression resolves to.
        "kind_label(kind)",
        "kind_label(self.kind)",
        # Built one line above its sink out of the literal field names
        # ("connector"/"agent") the rule form requires — no config value can
        # reach it.
        "missing_text",
        # Counts and positions computed here, never read from config.
        "index + 1",
        "len(rules)",
        "inherit_count",
        "override_count",
        "row",
        "i",
        # Already-escaped composites assembled immediately above the sink
        # (each is built from markup_safe() parts at its own site).
        "type_suffix",
        "prov_text",
        "blast_text",
        # A Python type NAME, from type(...).__name__ — never a value.
        "type(existing).__name__",
        # Section headings taken from literal tuples declared at the call
        # site (the two tool-list headings, and the picker's "(current)"
        # marker) — no config value can reach them.
        "label",
        "suffix",
        # Class-level label constants — literals; no config value reaches them.
        "self._INLINE_LABEL",
        "self._NEW_PRESET_LABEL",
        "self._NONE_LABEL",
        "self._NEW_TEMPLATE_LABEL",
        # A modal's OWN wrapper around caller-supplied text. Escaping is the
        # CALLER's job here by design: a few callers pass deliberate markup
        # (the validation-details view's coloured section headers), so the
        # modal cannot escape without breaking them. Every caller that passes
        # operator data escapes at its own site — which is exactly what the
        # rest of this check enforces.
        "self.message",
        "self.title_text",
        # Assembled from already-escaped parts inside the named method.
        "self._field_annotation(spec, entry, provenance)",
        "self._delete_confirm_message()",
        "_working_directory_warning(event.input.value)",
        # Deliberate markup from a closed set of literals.
        'status_badge(status.status_for("connector", name))',
        'status_badge(status.status_for("agent", name))',
        "status_badge(status.status_for_rule(i, entry))",
        # Plain integers rendered as text.
        "str(i + 1)",
        "str(len(rules))",
        "str(len(block))",
        "str(len(used_by))",
    }
)


# Whole-argument expressions (not f-string parts) that render content
# assembled elsewhere and verified there. Kept separate from
# `_NOT_OPERATOR_DATA` because the reason differs: these DO carry operator
# text, and each is escaped at the point it is built — which only the
# behavioural sweep (`test_configtool_markup_safety.py`) can confirm, since
# this checker cannot follow a value across functions. Adding one is a
# statement that its builder escapes, and that the sweep covers it.
_ESCAPED_AT_THE_BUILDER = frozenset(
    {
        # Detail-screen bodies: every interpolation inside them is escaped at
        # its own site, and the sweep opens every row of every tab.
        "self._body_text()",
        "self._header_text()",
        # The overview banner, assembled from escaped parts a few lines up.
        "summary",
        # Section text built from already-escaped pieces in the same method.
        # RAW strings throughout this set: entries are compared against
        # SOURCE TEXT, where `\n` is two characters, not a newline.
        r'"\n\n".join(sections)',
        # The blast-radius confirm body: every name and key inside `lines`
        # is escaped where `lines` is built, a few statements above.
        r'"This changes the EFFECTIVE value for —\n" + "\n".join(lines) + "\n\nContinue?"',
        # The working-directory heads-up, which escapes the path it names.
        r'_working_directory_warning(str(self._initial_values.get(spec.key) or ""))',
    }
)


def _is_allowed(expr: str) -> bool:
    """Is this expression on either allow-list, ignoring line wrapping?

    ALL whitespace is stripped, not merely collapsed: the same call is flat
    on one line or wrapped across three depending on its argument lengths,
    and wrapping also inserts spaces just inside the parentheses. An
    allow-list that matched only one spelling would quietly stop covering
    the other the first time a formatter touched the call.
    """
    flat = "".join(expr.split())
    allowed = _NOT_OPERATOR_DATA | _ESCAPED_AT_THE_BUILDER
    return flat in {"".join(a.split()) for a in allowed}


def _iter_python_files():
    return sorted(_CONFIGTOOL.rglob("*.py"))


def _sink_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


# Keyword arguments that are NOT rendered content: widget identity, CSS and
# row keys. `title=` is deliberately absent — MessageModal renders its title
# through a Static, so a title is content like any other.
_NON_CONTENT_KEYWORDS = frozenset({"id", "classes", "name", "key", "placeholder"})


def _is_escaped(node: ast.expr, source: str) -> bool:
    """True when this interpolated expression escapes what it renders.

    A direct `markup_safe(x)` is the common case. The looser test — the
    expression's source mentioning `markup_safe(` at all — covers the join
    idiom (`", ".join(markup_safe(b) for b in blockers)`), where the escape
    is applied per item inside a comprehension. Loose on purpose: one
    interpolation is one expression, so a `markup_safe(` inside it is
    escaping the value being rendered rather than some bystander.
    """
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "markup_safe"
    ):
        return True
    segment = ast.get_source_segment(source, node) or ""
    return "markup_safe(" in segment


def _unescaped_interpolations(path: Path) -> list[tuple[int, str, str]]:
    """(line, sink, expression source) for every unescaped dynamic part of an
    f-string passed to a markup sink in `path`."""
    source = path.read_text()
    tree = ast.parse(source)
    findings: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        sink = _sink_name(node)
        if sink not in _SINK_NAMES:
            continue
        args = list(node.args) + [
            kw.value
            for kw in node.keywords
            if kw.arg not in _NON_CONTENT_KEYWORDS
        ]
        for arg in args:
            if not isinstance(arg, ast.JoinedStr):
                # A NON-f-string argument, e.g.
                # `MessageModal(self._last_field_error or "...")`. This was
                # the checker's blind spot in round 9, and round 10 came in
                # through exactly it: `read_widget_value()` quotes the
                # operator's text into that message, so the report of a bad
                # value crashed on the value. Checked the same way now —
                # escaped, literal, or allow-listed.
                if isinstance(arg, ast.Constant):
                    continue  # a plain literal renders itself
                if _is_escaped(arg, source):
                    continue
                expr = (ast.get_source_segment(source, arg) or ast.dump(arg)).strip()
                if _is_allowed(expr):
                    continue
                findings.append((arg.lineno, sink, expr))
                continue
            for part in arg.values:
                if not isinstance(part, ast.FormattedValue):
                    continue
                if _is_escaped(part.value, source):
                    continue
                expr = ast.get_source_segment(source, part.value) or ast.dump(part.value)
                if _is_allowed(expr):
                    continue
                findings.append((part.lineno, sink, expr.strip()))
    return findings


class TestNoUnescapedMarkupInterpolations(unittest.TestCase):
    def test_every_fstring_into_a_markup_sink_escapes_its_values(self):
        offenders: list[str] = []
        for path in _iter_python_files():
            for line, sink, expr in _unescaped_interpolations(path):
                rel = path.relative_to(_CONFIGTOOL.parents[1])
                offenders.append(f"{rel}:{line}  {sink}(... {{{expr}}} ...)")
        self.assertEqual(
            offenders,
            [],
            "unescaped interpolation into a markup-parsing sink — wrap it in "
            "markup_safe(), or add the expression to _NOT_OPERATOR_DATA with a "
            "reason if it provably cannot carry operator text:\n  "
            + "\n  ".join(offenders),
        )

    def test_the_checker_actually_detects_an_unescaped_value(self):
        """A guard on the guard: a checker that silently matches nothing
        would pass this suite forever while enforcing nothing."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "probe.py"
            probe.write_text(
                'name = "x"\n'
                'MessageModal(f"A rule named {name} exists.")\n'
                'Static(f"safe: {markup_safe(name)}")\n'
                'MessageModal(self._err or "fallback")\n'
                'Static(markup_safe(self._err))\n'
                'Static("a plain literal")\n'
            )
            found = _unescaped_interpolations(probe)
        self.assertEqual(
            [
                (2, "MessageModal", "name"),                     # f-string part
                (4, "MessageModal", 'self._err or "fallback"'),   # whole argument
            ],
            found,
        )


if __name__ == "__main__":
    unittest.main()
