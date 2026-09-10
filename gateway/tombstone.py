"""The `agent-chat-gateway` command after the rename: a tombstone, not an alias.

A v0.5 install's `upgrade` is `git pull` on main + `uv sync`. Once main carries
the rename, that pull replaces the `agent-chat-gateway` console script with
`coop` and the user's `~/.local/bin/agent-chat-gateway` symlink would dangle
silently — a "command not found" with no explanation. This script exists so the
next invocation explains what happened and names the two ways out. It forwards
nothing: v1 has no compatibility mode (issue #154).
"""

import sys

MESSAGE = """\
agent-chat-gateway has become AgentCoop (command: `coop`).
This installation was upgraded past the rename and no longer runs under the old name.

  - Move to v1: remove this install and reinstall — see docs/migration-v1.md
      https://github.com/HammerMei/agentcoop/blob/main/docs/migration-v1.md
  - Stay on v0.5 for now:
      cd ~/.agent-chat-gateway/repo && git checkout v0.5.2 && uv sync
"""


def main() -> None:
    print(MESSAGE, file=sys.stderr, end="")
    sys.exit(1)
