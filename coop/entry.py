"""The ``coop`` command: one binary, three families of commands (design §3.3, §7).

```
coop start | stop | status | list | pause | resume | reset | reload | config …   the operator's daemon (coop.cli)
coop read | send | done | bio | attachment …                                     the agent's side (coop.agent.cli)
coop legacy <command> …                                                          the current gateway's whole CLI (gateway.cli)
```

The first word routes, before any parser runs, so each family keeps its own
options and help; the agent's side imports nothing of the daemon's. ``legacy`` is
the migration's name for the frozen ``gateway`` package's commands (design §7:
the old daemon keeps its rooms until each agent has moved) and goes with that
package at step 6.
"""

from __future__ import annotations

import sys

AGENT_COMMANDS = ("read", "send", "done", "bio", "attachment")
LEGACY = "legacy"

USAGE = """\
usage: coop <command> ...

  the daemon, for the operator:   start stop status list pause resume reset reload config     coop <command> --help
  the room, for an agent:         read send done bio attachment                               coop read --help
  the current gateway's commands: coop legacy <command> ...                                   coop legacy --help
"""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE, end="", file=sys.stdout if argv else sys.stderr)
        return 0 if argv else 2
    if argv[0] in AGENT_COMMANDS:
        from .agent.cli import main as agent_main

        return agent_main(argv)
    if argv[0] == LEGACY:
        return legacy(argv[1:])
    from .cli import main as operator_main

    return operator_main(argv)


def legacy(argv: list[str]) -> int:
    """The frozen gateway's CLI, as it is: it reads ``sys.argv`` and exits itself,
    so the arguments are put where it looks and its exit becomes the return."""
    from gateway.cli import main as old_main

    sys.argv = ["coop legacy", *argv]
    try:
        old_main()
    except SystemExit as exc:
        code = exc.code
        return 0 if code is None else code if isinstance(code, int) else 1
    return 0
