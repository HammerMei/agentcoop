"""Where AgentCoop lives on disk — the one definition of the runtime directory.

Every module that needs the runtime dir imports `RUNTIME_DIR` from here and
binds it as its own module attribute (`from ..paths import RUNTIME_DIR`), so
tests keep patching the module they exercise. Before this module existed the
same expression was typed in eight places, each with a comment explaining why
its copy was necessary; the rename to `~/.agentcoop` is what made the cost of
that visible.
"""

from pathlib import Path

RUNTIME_DIR_NAME = ".agentcoop"
RUNTIME_DIR = Path.home() / RUNTIME_DIR_NAME

# Connector-global base dir for attachment downloads; the default for
# `attachments.cache_dir_global` in every connector config.
ATTACHMENTS_DIR_DEFAULT = f"~/{RUNTIME_DIR_NAME}/attachments"
