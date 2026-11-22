"""Where the coop daemon lives on disk: the one definition of its runtime directory.

The directory is ``$COOP_HOME`` when set, otherwise ``~/.agentcoop``, the same base
the frozen ``gateway/`` package uses, so one install serves both daemons during the
migration (design §7). The file names differ (``coop.pid``, ``coop.sock``,
``coop.log``, ``state/coop/``) so the two never touch each other's files. Read once,
at import: ``COOP_HOME`` must be in the environment before ``import coop``.

The spelling rule for ``COOP_HOME`` is ``gateway/paths.py``'s: the value is embedded
verbatim by every writer downstream, so it is rejected at the source rather than
escaped per writer.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

RUNTIME_DIR_NAME = ".agentcoop"
DEFAULT_RUNTIME_DIR = Path.home() / RUNTIME_DIR_NAME

COOP_HOME_CHARS = re.compile(r"[A-Za-z0-9._/-]+")
COOP_HOME_RULE = (
    "COOP_HOME must be an absolute, canonical path (no '.', '..' or empty "
    "components, no trailing '/', not '/') using only letters, digits, '.', '_', "
    "'-' and '/'"
)


def runtime_dir_from_env(environ: dict[str, str] | None = None) -> Path:
    raw = (os.environ if environ is None else environ).get("COOP_HOME", "")
    if not raw:
        return DEFAULT_RUNTIME_DIR
    text = os.path.expanduser(raw)  # not Path(): that would collapse `//` and `/./` before the check
    if (
        not text.startswith("/")
        or text == "/"
        or text.startswith("//")
        or os.path.normpath(text) != text
        or not COOP_HOME_CHARS.fullmatch(text)
    ):
        raise ValueError(f"{COOP_HOME_RULE} (got {raw!r})")
    return Path(text)


RUNTIME_DIR = runtime_dir_from_env()

CONFIG_FILE = RUNTIME_DIR / "config.yaml"
PID_FILE = RUNTIME_DIR / "coop.pid"
SOCKET_FILE = RUNTIME_DIR / "coop.sock"
LOG_FILE = RUNTIME_DIR / "coop.log"
STATE_DIR = RUNTIME_DIR / "state" / "coop"
