"""The `agent-chat-gateway` command after the rename: a tombstone, not an alias.

A v0.5 install's `upgrade` is `git pull` on main + `uv sync`. Once main carries
the rename, that pull replaces the `agent-chat-gateway` console script with
`coop` and the user's `~/.local/bin/agent-chat-gateway` symlink would dangle
silently — a "command not found" with no explanation. This script exists so the
next invocation explains what happened and names the two ways out. It forwards
nothing: v1 has no compatibility mode (issue #154).
"""

import json
import sys
from pathlib import Path

OLD_RUNTIME_DIR = Path.home() / ".agent-chat-gateway"


def _old_repo_path() -> str:
    """Where the v0 install's repo is — from its install_meta.json when it has
    one (a local-script install keeps the repo wherever it was cloned), else the
    curl-install default. Spelled with `~` for the message."""
    try:
        meta = json.loads((OLD_RUNTIME_DIR / "install_meta.json").read_text())
        repo = str(meta.get("repo_path") or "")
    except (OSError, ValueError):
        repo = ""
    if not repo:
        repo = str(OLD_RUNTIME_DIR / "repo")
    home = str(Path.home())
    return "~" + repo[len(home):] if repo.startswith(home + "/") else repo


def build_message(repo: str) -> str:
    return f"""\
agent-chat-gateway has become AgentCoop (command: `coop`).
This installation was upgraded past the rename and no longer runs under the old name.

  - Move to v1: remove this install and reinstall — see docs/migration-v1.md
      https://github.com/HammerMei/agentcoop/blob/main/docs/migration-v1.md
  - Stay on v0.5 for now:
      cd {repo} && git checkout v0.5.2 && uv sync && agent-chat-gateway start

If a v0 daemon is still running from before the upgrade, this command can no
longer stop it:  kill "$(cat ~/.agent-chat-gateway/gateway.pid)"
"""


MESSAGE = build_message(_old_repo_path())


def main() -> None:
    print(MESSAGE, file=sys.stderr, end="")
    sys.exit(1)
