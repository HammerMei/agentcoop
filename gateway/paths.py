"""Where AgentCoop lives on disk — the one definition of the runtime directory.

Every module that needs the runtime dir imports `RUNTIME_DIR` from here and
binds it as its own module attribute (`from ..paths import RUNTIME_DIR`), so
tests keep patching the module they exercise. Before this module existed the
same expression was typed in eight places, each with a comment explaining why
its copy was necessary; the rename to `~/.agentcoop` is what made the cost of
that visible.

The directory is `$COOP_HOME` when set, otherwise `~/.agentcoop` (#182). It is
the base of every runtime path — state, control socket, logs, `config.yaml`'s
default location, `admin-profiles.yaml` — and of every relative path written in
`config.yaml`. Read once, at import: `COOP_HOME` must be in the environment
before `import gateway`; setting it later changes nothing.
"""

import os
from pathlib import Path

RUNTIME_DIR_NAME = ".agentcoop"


def _runtime_dir_from_env() -> Path:
    raw = os.environ.get("COOP_HOME", "")
    if not raw:
        return Path.home() / RUNTIME_DIR_NAME
    path = Path(raw).expanduser()
    if not path.is_absolute():
        # A relative base would mean whatever the importing process's CWD was —
        # the ambiguity a fixed base exists to remove.
        raise ValueError(f"COOP_HOME must be an absolute path (got {raw!r})")
    return path


RUNTIME_DIR = _runtime_dir_from_env()

# Connector-global base dir for attachment downloads; the default for
# `attachments.cache_dir_global` in every connector config. Derived from the
# base so it follows `COOP_HOME`.
ATTACHMENTS_DIR_DEFAULT = str(RUNTIME_DIR / "attachments")
