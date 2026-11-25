"""The ``coop`` command: one binary, three families of commands (design §3.3, §7).

```
coop start | stop | status | list | pause | resume | reset | reload | config …   the operator's daemon (coop.cli)
coop read | send | done | bio | attachment | agent run …                         the agent's side (coop.agent.cli)
coop legacy <command> …                                                          the current gateway's whole CLI (gateway.cli)
```

The command word routes, before any parser runs, so each family keeps its own
options and help; the agent's side imports nothing of the daemon's. The agent
CLI's connection options come before its command (``coop --gateway U --token T
read …``), so the command word is the first that is not an option or an option's
value; the operator CLI has no options before its command. ``legacy`` is
the migration's name for the frozen ``gateway`` package's commands (design §7:
the old daemon keeps its rooms until each agent has moved) and goes with that
package at step 6.
"""

from __future__ import annotations

import sys

AGENT_COMMANDS = ("read", "send", "done", "bio", "attachment", "agent")
LEGACY = "legacy"

USAGE = """\
usage: coop <command> ...

  the daemon, for the operator:   start stop status list pause resume reset reload config     coop <command> --help
  the room, for an agent:         read send done bio attachment, agent run (the utility)       coop read --help
  the current gateway's commands: coop legacy <command> ...                                   coop legacy --help
"""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    word = command_word(argv)
    if word in ("-h", "--help"):
        print(USAGE, end="")
        return 0
    if word is None:  # nothing, or options with no command: a usage error
        print(USAGE, end="", file=sys.stderr)
        return 2
    if word in AGENT_COMMANDS:
        from .agent.cli import main as agent_main

        return agent_main(argv)
    if word == LEGACY and argv[0] == LEGACY:
        return legacy(argv[1:])
    from .cli import main as operator_main

    return operator_main(argv)


def command_word(argv: list[str]) -> str | None:
    """The first word that is not an option or an option's value: ``--opt value``
    takes two, ``--opt=value`` and a flag one; ``-h``/``--help`` is the word itself."""
    i = 0
    while i < len(argv):
        word = argv[i]
        if word in ("-h", "--help"):
            return word
        if not word.startswith("-"):
            return word
        i += 1 if "=" in word else 2
    return None


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
