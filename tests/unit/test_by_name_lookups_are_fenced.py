"""By-name watcher lookups are fenced to the operator boundary (§2.8).

A watcher HANDLE is a pure function of (connector, room) and moves when the
room is renamed; another room can take it over once the original's record is
reclaimed. Code on the runtime path — the scheduler's fire, the wake, the
failure notice, job cancellation — that looked a watcher up by handle where a
room id was available produced the same silent misdelivery SIX times across four
review rounds, each at a different site. Re-keying the lifecycle by room id made
the right lookup the cheap one; narrowing the internal signatures to room ids
made the wrong one unwritable at the seams. This test is what keeps the seams
from growing back.

It walks the runtime modules with `ast` and fails on any call to a by-name
lookup outside the allowlist below. The allowlist is the operator boundary
(control-socket verbs take a name because a human typed one) plus the ONE
runtime entry, `SessionManager.resolve_handle`, which the scheduler calls
exactly once per fire for a job written before schema 2.

A new by-name call fails here with the function it landed in, so the author
decides — widen the allowlist with a reason, or take a room id instead — rather
than a reviewer finding occurrence seven.

Run with:
    uv run python -m pytest tests/unit/test_by_name_lookups_are_fenced.py -v
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "gateway"

# The modules that make up the runtime path. `control.py` is the operator
# boundary and is deliberately NOT walked — every verb there starts from a name.
RUNTIME_MODULES = (
    "core/scheduler.py",
    "core/session_manager.py",
    "service.py",
)

# Lookups keyed by watcher name. `states()` is not here: it is iterated, not
# indexed, and iteration does not choose a room.
BY_NAME_LOOKUPS = frozenset({
    "get_watcher_state",
    "has_persisted_record",
    "get_processor",
    "processor_named",
    "resolve_handle",
})

# (module, qualified function) → why a by-name call is legitimate THERE.
ALLOWED = {
    ("core/session_manager.py", "SessionManager.resolve_handle"):
        "the one runtime by-name entry; turns a pre-schema-2 job's handle into a room id",
    ("core/session_manager.py", "SessionManager.get_watcher_state"):
        "thin delegator for the operator boundary (control.py)",
    ("core/session_manager.py", "SessionManager.get_processor"):
        "thin delegator for the operator boundary (control.py)",
    ("core/session_manager.py", "SessionManager.has_persisted_record"):
        "thin delegator for the operator boundary (control.py): tells an unloaded "
        "record from a gone one before a name is called unknown (#151)",
    ("core/session_manager.py", "SessionManager.expire_watcher"):
        "an operator verb: `agent-chat-gateway expire <name>` — the human typed the name",
    ("core/session_manager.py", "SessionManager.resume_watcher"):
        "an operator verb: `agent-chat-gateway resume <name>` — the human typed the name; "
        "the record is read to resolve ITS room id through the connector first (#145)",
    ("core/scheduler.py", "JobScheduler._resolve_target"):
        "the scheduler's single resolution seam; calls resolve_handle once per fire",
}


def _by_name_calls(module: str) -> list[tuple[str, str, int]]:
    """Every `(qualified function, lookup name, line)` in `module` that calls a
    by-name lookup."""
    tree = ast.parse((ROOT / module).read_text())
    found: list[tuple[str, str, int]] = []

    class Walker(ast.NodeVisitor):
        def __init__(self):
            self.stack: list[str] = []

        def _descend(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_ClassDef = _descend
        visit_FunctionDef = _descend

        visit_AsyncFunctionDef = _descend

        def visit_Call(self, node):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in BY_NAME_LOOKUPS:
                found.append((".".join(self.stack) or "<module>", func.attr, node.lineno))
            self.generic_visit(node)

    Walker().visit(tree)
    return found


class TestByNameLookupsStayAtTheBoundary(unittest.TestCase):
    def test_every_by_name_call_on_the_runtime_path_is_allowlisted(self):
        offenders = []
        for module in RUNTIME_MODULES:
            for qualname, lookup, line in _by_name_calls(module):
                if (module, qualname) not in ALLOWED:
                    offenders.append(f"{module}:{line} {qualname} calls {lookup}()")
        self.assertEqual(offenders, [], (
            "\n\nA by-name watcher lookup appeared on the runtime path:\n  "
            + "\n  ".join(offenders)
            + "\n\nThe routing rule (docs/design/dynamic-watcher-design.md §2.8): a job "
              "or wake is addressed by room id; a handle is resolved ONCE through "
              "SessionManager.resolve_handle. Take a room id instead, or add the "
              "function to ALLOWED in this file with the reason it needs a name."
        ))

    def test_the_allowlist_names_functions_that_exist_and_still_look_up_by_name(self):
        """A stale allowlist entry is a fence with a gap the next reader cannot
        see. Every entry must match a real by-name call, so removing the lookup
        from a function also removes its exemption."""
        live = {(m, q) for m in RUNTIME_MODULES for q, _, _ in _by_name_calls(m)}
        stale = sorted(set(ALLOWED) - live)
        self.assertEqual(stale, [], f"allowlisted but no longer looking up by name: {stale}")

    def test_the_scheduler_resolves_a_handle_at_most_once_per_fire(self):
        """One seam. Two call sites inside `_resolve_target` are expected — the
        configured-connector path and the legacy scan — but nothing else in the
        scheduler may call it, or the fire has grown a second resolution."""
        calls = [(q, line) for q, lookup, line in _by_name_calls("core/scheduler.py")
                 if lookup == "resolve_handle"]
        self.assertTrue(calls, "the seam has to exist somewhere")
        self.assertEqual({q for q, _ in calls}, {"JobScheduler._resolve_target"}, calls)
