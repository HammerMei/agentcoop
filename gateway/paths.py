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
import re
from pathlib import Path

RUNTIME_DIR_NAME = ".agentcoop"
DEFAULT_RUNTIME_DIR = Path.home() / RUNTIME_DIR_NAME

# What a COOP_HOME may be spelled with. The value is embedded verbatim by every
# writer downstream — a shell rc line, coop-keeper's two permission files
# (JSON, and glob-matched), install_meta.json, the skills' prose — and each of
# those has its own set of characters that mean something. Rejecting at the
# source is one rule; escaping at each writer is one rule per writer, and two
# review rounds found a new one each time. install.sh's `coop_home_dir` enforces
# the same rule; `tests/helpers.py` COOP_HOME_SPELLINGS drives both.
COOP_HOME_CHARS = re.compile(r"[A-Za-z0-9._/-]+")
COOP_HOME_RULE = (
    "COOP_HOME must be an absolute, canonical path (no '.', '..' or empty "
    "components, no trailing '/', not '/') using only letters, digits, '.', '_', "
    "'-' and '/'"
)


def _runtime_dir_from_env() -> Path:
    raw = os.environ.get("COOP_HOME", "")
    if not raw:
        return DEFAULT_RUNTIME_DIR
    text = os.path.expanduser(raw)  # not Path(): that would collapse `//` and `/./` before the check
    if (
        not text.startswith("/")            # relative: the importing CWD, i.e. ambiguity
        or text == "/"                      # no name for the keeper's globs, nothing belongs there
        or text.startswith("//")            # POSIX keeps a leading `//`; normpath does too
        or os.path.normpath(text) != text   # `.`/`..`/empty components, trailing `/`
        or not COOP_HOME_CHARS.fullmatch(text)
    ):
        raise ValueError(f"{COOP_HOME_RULE} (got {raw!r})")
    return Path(text)


RUNTIME_DIR = _runtime_dir_from_env()

# Connector-global base dir for attachment downloads; the default for
# `attachments.cache_dir_global` in every connector config. Derived from the
# base so it follows `COOP_HOME`.
ATTACHMENTS_DIR_DEFAULT = str(RUNTIME_DIR / "attachments")
