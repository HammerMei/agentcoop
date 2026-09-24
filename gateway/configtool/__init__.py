"""coop config TUI.

Reached via ``coop config`` (no subcommand) — see
gateway/cli.py's ``_run_config``. ``coop config validate``
stays a separate, scriptable command backed by gateway/config_validate.py.

Not to be confused with gateway/tools/tui.py, an unrelated interactive REPL
for chatting with agent backends directly (naming collision only — see
docs/design/config-tool.md).
"""

from __future__ import annotations

import sys


def run_app(config_path: str, lint: bool = False) -> int:
    """Launch the config TUI. Returns a process exit code.

    Guards against a piped/non-interactive invocation (e.g.
    ``coop config | cat`` or ``ssh host cmd </dev/null``) —
    Textual's driver needs a real terminal; without one this would otherwise
    hang rather than fail fast.
    """
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(
            "Error: 'coop config' requires an interactive terminal.\n"
            "Use 'coop config validate' for a non-interactive check.",
            file=sys.stderr,
        )
        return 1

    from .app import ConfigToolApp

    ConfigToolApp(config_path=config_path, lint=lint).run()
    return 0
